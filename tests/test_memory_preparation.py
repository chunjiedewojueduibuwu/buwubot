import json

import pytest
from test_memory import proposal, setup_memory

from buwubot.conversations import RecallEvent
from buwubot.models import Completion
from buwubot.store import Conflict


def draft(**changes):
    return {'subject_id': '22', 'content': '甲喜欢茶', 'tier': 'important',
            'source_message_ids': ['123:id:1'], 'confidence': .95, **changes}


async def test_auxiliary_material_cannot_replace_primary_decision_or_modify_memory(tmp_path):
    store, service, _ = setup_memory(tmp_path)
    calls = []
    class Models:
        async def call(self, role, payload, **kwargs):
            data = json.loads(payload[-1]['content'])
            calls.append(role)
            if role == 'auxiliary':
                assert 'prepared_candidates' not in data
                return Completion('', 'aux-fixture', {'memories': [draft(content='辅助错误推断甲爱咖啡')]})
            assert role == 'memory'
            assert data['prepared_candidates'][0]['content'] == '辅助错误推断甲爱咖啡'
            assert data['messages'][0]['text'] == '我喜欢茶，不喜欢咖啡'
            assert not store.rows('SELECT * FROM memories')
            return Completion('', service.settings.primary_model, {'memories': [draft()]})
    service.models = Models()
    try:
        result, = await service.propose([service.archive.get('123:id:1')])
        assert calls == ['auxiliary', 'memory']
        assert result.content == '甲喜欢茶' and result.origin_model == service.settings.primary_model
        assert not store.rows('SELECT * FROM memories')
    finally:
        store.close()


@pytest.mark.parametrize('data', [{}, [], {'memories': {}}, {'memories': ['bad']},
                                {'memories': [draft(tier='core')]},
                                {'memories': [draft(persona_id='other-persona')]},
                                {'memories': [draft(reinforcement_source_ids=['123:id:1'])]},
                                {'memories': [draft(source_message_ids=['other-group:seq:9'])]}])
async def test_bad_auxiliary_material_is_a_failure_not_an_empty_success(tmp_path, data):
    store, service, _ = setup_memory(tmp_path)
    roles = []
    class Models:
        async def call(self, role, payload, **kwargs):
            roles.append(role)
            return Completion('', 'aux-fixture', data)
    service.models = Models()
    try:
        with pytest.raises(ValueError):
            await service.propose([service.archive.get('123:id:1')])
        assert roles == ['auxiliary']
        assert not store.rows('SELECT * FROM memories')
    finally:
        store.close()


async def test_empty_auxiliary_material_does_not_suppress_primary_extraction(tmp_path):
    store, service, _ = setup_memory(tmp_path)
    class Models:
        async def call(self, role, payload, **kwargs):
            if role == 'auxiliary':
                return Completion('', 'aux-fixture', {'memories': []})
            assert json.loads(payload[-1]['content'])['prepared_candidates'] == []
            return Completion('', service.settings.primary_model, {'memories': [draft()]})
    service.models = Models()
    try:
        result, = await service.propose([service.archive.get('123:id:1')])
        assert result.content == '甲喜欢茶'
    finally:
        store.close()


async def test_auxiliary_cannot_advance_snapshot_past_a_manual_change(tmp_path):
    store, service, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: service.commit(proposal()))
    epoch = service.epoch()
    class Models:
        async def call(self, role, payload, **kwargs):
            if role == 'auxiliary':
                await coordinator.commit(lambda: service.delete(mid, expected_revision=1))
            return Completion('', service.settings.primary_model, {'memories': [draft()]})
    service.models = Models()
    try:
        candidate, = await service.propose([service.archive.get('123:id:1')])
        assert candidate.snapshot_epoch == epoch < service.epoch()
        with pytest.raises(Conflict):
            await coordinator.commit(lambda: service.commit(candidate, reviewed=True))
        assert service.get(mid) is None
    finally:
        store.close()


async def test_non_ds_completion_cannot_become_formal_proposals(tmp_path):
    store, service, _ = setup_memory(tmp_path)
    class Models:
        async def call(self, role, payload, **kwargs):
            return Completion('', 'aux-fixture', {'memories': [draft()]})
    service.models = Models()
    try:
        with pytest.raises(ValueError, match='primary'):
            await service.propose([service.archive.get('123:id:1')])
    finally:
        store.close()


async def test_cross_conversation_input_never_reaches_auxiliary_model(tmp_path):
    store, service, _ = setup_memory(tmp_path)
    class Models:
        async def call(self, *args, **kwargs):
            pytest.fail('private/other-group input reached a model')
    service.models = Models()
    message = service.archive.get('123:id:1')
    message.group_id = 'private:22'
    try:
        with pytest.raises(ValueError, match='conversation'):
            await service.propose([message])
    finally:
        store.close()


async def test_recall_during_auxiliary_preparation_cannot_form_a_memory(tmp_path):
    store, service, _ = setup_memory(tmp_path)
    class Models:
        async def call(self, role, payload, **kwargs):
            if role == 'auxiliary':
                service.archive.accept(RecallEvent('123', '1'), service.settings)
            return Completion('', service.settings.primary_model, {'memories': [draft()]})
    service.models = Models()
    try:
        assert await service.propose([service.archive.get('123:id:1')]) == []
        assert not store.rows('SELECT * FROM memories')
    finally:
        store.close()


async def test_recall_after_review_blocks_persisted_candidate_at_coordinator_commit(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    from buwubot.memory import MemoryProposal

    now = datetime(2026, 10, 26, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, _, _ = build(tmp_path, lambda: now)
    source = engine.ingest({'time': now.timestamp(), 'post_type': 'message', 'message_type': 'group',
        'group_id': 123, 'self_id': 11, 'user_id': 22, 'message_id': 1,
        'message': [{'type': 'text', 'data': {'text': '我一直喜欢喝茶'}}]})
    candidate = MemoryProposal(group_id='123', **draft(), snapshot_epoch=engine.memory.epoch())
    async def review(*_args):
        engine.ingest(RecallEvent('123', '1'))
        return True, 'fixture'
    engine.memory.review = review
    jid = engine.queue.enqueue('memory', {'group_id': '123', 'messages': [vars(source)],
        'proposals': [candidate.model_dump()], 'applied': []},
        dedupe_key='recall-after-review', priority=2, source_time=source.time)
    try:
        await engine.process(engine.queue.get(jid))
        assert not store.rows('SELECT * FROM memories')
        assert engine.queue.get(jid)['state'] == 'succeeded'
    finally:
        store.close()
