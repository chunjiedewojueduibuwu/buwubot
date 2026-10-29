import json
from datetime import timedelta

import pytest
from test_consolidation import engine_pair
from test_memory import proposal, setup_memory

from buwubot.consolidation import MemoryConsolidation
from buwubot.coordinator import Coordinator, WorkPaused
from buwubot.memory import MemoryService
from buwubot.models import Completion
from buwubot.store import Store


def output(mid):
    return {'relations': [], 'distillations': [{'source_memory_ids': [mid],
        'content': '甲偏好喝茶', 'tier': 'secondary', 'confidence': .9}]}


async def test_periodic_distillation_requires_mutable_scope_and_is_restart_idempotent(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent'})))
    service = MemoryConsolidation(memory)
    try:
        readonly = service.snapshot()
        with pytest.raises(ValueError, match='mutable'):
            await coordinator.commit(lambda: service.commit(readonly, output(mid), reviewed=True))
        snapshot = service.snapshot(mutation_since=0)
        assert snapshot['mutable_ids'] == [mid]
        await coordinator.commit(lambda: service.commit(snapshot, output(mid), reviewed=True))
        assert memory.get(mid)['tier'] == 'recent'
        assert len(store.rows('SELECT * FROM memory_distillations')) == 1
        # Reopen the actual database; a persisted job checkpoint cannot create another target.
        cfg = memory.settings
        store.close()
        store = Store(tmp_path / 'bot.db')
        memory = MemoryService(store, cfg)
        coordinator = Coordinator(store, None, cfg)
        await coordinator.commit(lambda: MemoryConsolidation(memory).commit(snapshot, output(mid), reviewed=True))
        assert len(store.rows('SELECT * FROM memories')) == 2
        assert len(store.rows('SELECT * FROM memory_distillations')) == 1
    finally:
        store.close()


async def test_mutable_scope_excludes_old_manually_maintained_and_emotional_records(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent'})))
    service = MemoryConsolidation(memory)
    try:
        after = memory.get(mid)['created_at'] + 1
        assert service.snapshot(mutation_since=after)['mutable_ids'] == []
        await coordinator.commit(lambda: memory.edit(mid, content='用户维护的普通事件', expected_revision=1))
        assert service.snapshot(mutation_since=0)['mutable_ids'] == []
        p = proposal().model_copy(update={'tier': 'feeling', 'subject_id': '',
            'persona_id': memory.settings.persona_id, 'content': '这次聊天有些开心'})
        await coordinator.commit(lambda: memory.commit(p, reviewed=True))
        assert service.snapshot(mutation_since=0)['mutable_ids'] == []
    finally:
        store.close()


async def test_bad_distillation_rolls_back_relations_and_focus_checkpoint(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    ids = []
    for text in ('甲喜欢喝茶', '甲正在下载游戏'):
        ids.append(await coordinator.commit(lambda text=text: memory.commit(proposal().model_copy(update={
            'tier': 'recent', 'content': text}))))
    service = MemoryConsolidation(memory)
    snapshot = service.snapshot(mutation_since=0)
    bad = output('not-in-snapshot')
    bad['distillations'].insert(0, output(ids[0])['distillations'][0])
    bad['relations'] = [{'source_id': ids[0], 'target_id': ids[1], 'kind': 'related', 'summary': '同次聊天'}]
    try:
        with pytest.raises(ValueError):
            await coordinator.commit(lambda: service.commit(snapshot, bad, reviewed=True))
        assert not store.rows('SELECT * FROM memory_relations')
        assert not store.rows('SELECT * FROM memory_consolidation_seen')
        assert len(store.rows('SELECT * FROM memories')) == 2
    finally:
        store.close()


async def test_auxiliary_is_material_only_and_survives_pause_checkpoint(tmp_path):
    store, engine, qq, models, ids = await engine_pair(tmp_path)
    calls = []
    async def call(role, messages, **kwargs):
        calls.append(role)
        data = json.loads(messages[-1]['content'])
        if role == 'auxiliary':
            return Completion('', 'helper', {'relations': [], 'distillations': []})
        if role == 'memory':
            assert data['prepared_candidates'] == {'relations': [], 'distillations': [], 'merges': [], 'entity_mentions': [], 'entity_edges': []}
            assert len(data['snapshot']['records']) == 2
            return Completion('', engine.settings.primary_model, {'relations': [], 'distillations': []})
        raise AssertionError('empty result needs no review call')
    models.call = call
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    engine.coordinator.allowed = lambda: False
    try:
        with pytest.raises(WorkPaused):
            await engine.process(engine.queue.get(jid))
        assert calls == ['auxiliary', 'memory']
        engine.coordinator.allowed = engine.allowed
        await engine.process(engine.queue.get(jid))
        assert calls == ['auxiliary', 'memory']
        assert qq.sent == []
        assert len(store.rows('SELECT * FROM memories')) == len(ids)
    finally:
        store.close()


@pytest.mark.parametrize('bad_aux,foreign_primary', [(True, False), (False, True)])
async def test_bad_aux_or_non_ds_adoption_never_marks_sources_processed(tmp_path, bad_aux, foreign_primary):
    store, engine, _, models, _ = await engine_pair(tmp_path)
    calls = []
    async def call(role, *args, **kwargs):
        calls.append(role)
        if role == 'auxiliary':
            return Completion('', 'helper', {} if bad_aux else {'relations': [], 'distillations': []})
        return Completion('', 'helper' if foreign_primary else engine.settings.primary_model,
                          {'relations': [], 'distillations': []})
    models.call = call
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    try:
        with pytest.raises(ValueError):
            await engine.process(engine.queue.get(jid))
        assert calls == (['auxiliary'] if bad_aux else ['auxiliary', 'memory'])
        assert not store.rows('SELECT * FROM memory_consolidation_seen')
    finally:
        store.close()


async def test_engine_distills_only_ds_adopted_reviewed_material_without_sending(tmp_path):
    store, engine, qq, models, _ = await engine_pair(tmp_path)
    mid = await engine.coordinator.commit(lambda: engine.memory.commit(proposal().model_copy(update={
        'tier': 'recent', 'content': '甲曾多次说明偏爱茶，这次也提到了'})))
    store.set_state('memory_consolidation_mutation_since', 0)
    calls = []
    async def call(role, messages, **kwargs):
        calls.append(role)
        if role == 'auxiliary':
            # Auxiliary proposes a wrong layer; DS must be able to replace it.
            draft = output(mid)
            draft['distillations'][0]['tier'] = 'important'
            return Completion('', 'helper', draft)
        if role == 'memory':
            assert json.loads(messages[-1]['content'])['prepared_candidates']['distillations'][0]['tier'] == 'important'
            return Completion('', engine.settings.primary_model, output(mid))
        assert role == 'review' and kwargs['exclude_model'] == engine.settings.primary_model
        return Completion('', 'checker', {'approved': True})
    models.call = call
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    try:
        await engine.process(engine.queue.get(jid))
        assert calls == ['auxiliary', 'memory', 'review']
        rows = store.rows('SELECT tier FROM memories WHERE content=?', ('甲偏好喝茶',))
        assert [r['tier'] for r in rows] == ['secondary']
        assert engine.memory.get(mid)['tier'] == 'recent'
        assert len(store.rows('SELECT * FROM memory_distillations')) == 1
        assert qq.sent == []
    finally:
        store.close()


async def test_pause_between_preparation_and_adoption_reuses_saved_auxiliary(tmp_path):
    store, engine, _, models, _ = await engine_pair(tmp_path)
    calls = []
    async def call(role, *args, **kwargs):
        calls.append(role)
        if role == 'memory' and calls.count('memory') == 1:
            raise WorkPaused('主意识先回应呼叫')
        return Completion('', 'helper' if role == 'auxiliary' else engine.settings.primary_model,
                          {'relations': [], 'distillations': []})
    models.call = call
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    try:
        with pytest.raises(WorkPaused):
            await engine.process(engine.queue.get(jid))
        assert 'prepared_candidates' in engine.queue.get(jid)['payload']
        await engine.process(engine.queue.get(jid))
        assert calls == ['auxiliary', 'memory', 'memory']
    finally:
        store.close()


async def test_activation_boundary_is_persisted_and_old_facts_stay_read_only(tmp_path):
    store, engine, _, _, ids = await engine_pair(tmp_path)
    try:
        store.connection.execute("UPDATE memories SET tier='recent',created_at=0 WHERE id=?", (ids[0],))
        engine.enqueue_consolidations()
        since = store.get_state('memory_consolidation_mutation_since')
        assert since == engine.clock().timestamp()
        jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
        snap = engine.queue.get(jid)['payload']['snapshot']
        assert not set(ids) & set(snap['mutable_ids'])
        later = engine.clock() + timedelta(hours=1)
        engine.clock = lambda: later
        # Re-entering the scheduler must not move the boundary forward after restart.
        engine.enqueue_consolidations()
        assert store.get_state('memory_consolidation_mutation_since') == since
    finally:
        store.close()


async def test_unrelated_memory_epoch_does_not_duplicate_an_unchanged_idle_job(tmp_path):
    store, engine, _, _, _ = await engine_pair(tmp_path)
    try:
        engine.enqueue_consolidations()
        store.set_state('memory_epoch', engine.memory.epoch() + 1)
        engine.enqueue_consolidations()
        assert len(store.rows("SELECT id FROM jobs WHERE kind='consolidate'")) == 1
    finally:
        store.close()
