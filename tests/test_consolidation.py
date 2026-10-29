import asyncio
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_memory import proposal, setup_memory

from buwubot.consolidation import MemoryConsolidation
from buwubot.coordinator import WorkPaused
from buwubot.models import Completion
from buwubot.store import Conflict


async def pair(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    ids = []
    for text in ('甲以前喜欢茶', '甲现在改喝咖啡，之前喜欢茶'):
        ids.append(await coordinator.commit(lambda text=text: memory.commit(
            proposal().model_copy(update={'content': text}))))
    return store, memory, coordinator, ids


def result(ids, kind='correction'):
    return {'relations': [{'source_id': ids[1], 'target_id': ids[0], 'kind': kind,
                           'summary': '后来明确改变了饮品偏好，旧记录保留为历史'}]}


async def test_consolidation_preserves_originals_sources_and_versions(tmp_path):
    store, memory, coordinator, ids = await pair(tmp_path)
    service = MemoryConsolidation(memory)
    before = [memory.get(mid) for mid in ids]
    snapshot = service.snapshot()
    assert {row['id'] for row in snapshot['records']} == set(ids)
    with pytest.raises(RuntimeError, match='coordinator'):
        service.commit(snapshot, result(ids), reviewed=True)
    with pytest.raises(ValueError, match='review'):
        await coordinator.commit(lambda: service.commit(snapshot, result(ids), reviewed=False))
    await coordinator.commit(lambda: service.commit(snapshot, result(ids), reviewed=True))
    await coordinator.commit(lambda: service.commit(snapshot, result(ids), reviewed=True))
    links = service.relations(ids[1])
    assert len(links) == 1 and links[0]['kind'] == 'correction'
    assert links[0]['source_message_ids'] == ['123:id:1']
    assert before == [memory.get(mid) for mid in ids]
    assert service.snapshot() is None
    memory.cleanup(now=8 * 86400)
    assert len(service.relations(ids[1])) == 1
    await coordinator.commit(lambda: memory.edit(ids[0], content='人工修正', expected_revision=1))
    assert service.relations(ids[1]) == []
    # A receipt only acknowledges the already-applied operation; it cannot overwrite the edit.
    await coordinator.commit(lambda: service.commit(snapshot, result(ids), reviewed=True))
    changed = result(ids)
    changed['relations'][0]['summary'] = '修改后的新提案仍须重新核对来源版本'
    with pytest.raises(Conflict):
        await coordinator.commit(lambda: service.commit(snapshot, changed, reviewed=True))
    assert memory.get(ids[0])['content'] == '人工修正'
    store.close()


@pytest.mark.parametrize('kind', ['unknown', 'self', 'foreign', 'second_invalid'])
async def test_invalid_relation_batch_is_atomic(tmp_path, kind):
    store, memory, coordinator, ids = await pair(tmp_path)
    service = MemoryConsolidation(memory)
    snapshot = service.snapshot()
    output = result(ids)
    if kind == 'unknown':
        output['relations'][0]['kind'] = 'invented'
    elif kind == 'self':
        output['relations'][0]['target_id'] = ids[1]
    elif kind == 'foreign':
        output['relations'][0]['target_id'] = 'other-group-memory'
    else:
        output['relations'].append({'source_id': 'missing'})
    with pytest.raises(ValueError):
        await coordinator.commit(lambda: service.commit(snapshot, output, reviewed=True))
    assert service.relations(ids[1]) == []
    assert service.snapshot() is not None
    store.close()


async def test_snapshot_excludes_core_other_persona_and_scope(tmp_path):
    store, memory, _coordinator, ids = await pair(tmp_path)
    service = MemoryConsolidation(memory)
    store.connection.execute("UPDATE memories SET persona_id='other' WHERE id=?", (ids[0],))
    assert {row['id'] for row in service.snapshot()['records']} == {ids[1]}
    store.connection.execute("UPDATE memories SET tier='core' WHERE id=?", (ids[1],))
    assert service.snapshot() is None
    store.close()


async def test_empty_consolidation_marks_only_unchanged_revisions(tmp_path):
    store, memory, coordinator, ids = await pair(tmp_path)
    service = MemoryConsolidation(memory)
    snapshot = service.snapshot()
    await coordinator.commit(lambda: memory.delete(ids[0], expected_revision=1))
    with pytest.raises(Conflict):
        await coordinator.commit(lambda: service.commit(snapshot, {'relations': []}, reviewed=False))
    assert service.snapshot() is not None
    store.close()


async def engine_pair(tmp_path):
    from test_engine import build
    now = datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=12)
    store, engine, qq, models = build(tmp_path, lambda: now)
    engine.ingest({'time': now.timestamp(), 'post_type': 'message', 'message_type': 'group',
        'group_id': 123, 'self_id': 11, 'user_id': 22, 'message_id': 1,
        'sender': {'nickname': '甲'}, 'message': [{'type': 'text', 'data': {'text': '偏好变化'}}]})
    ids = []
    for content in ('甲以前喜欢茶', '甲现在喝咖啡，原来喜欢茶'):
        ids.append(await engine.coordinator.commit(lambda content=content: engine.memory.commit(
            proposal().model_copy(update={'content': content}))))
    return store, engine, qq, models, ids


async def test_idle_job_review_checkpoint_and_no_repeated_calls_after_pause(tmp_path):
    store, engine, qq, models, ids = await engine_pair(tmp_path)
    calls = []
    async def call(role, payload, **kwargs):
        calls.append(role)
        if role == 'auxiliary':
            return Completion('', 'glm', {'relations': [], 'distillations': []})
        if role == 'memory':
            assert len(json.loads(payload[-1]['content'])['snapshot']['records']) == 2
            return Completion('', engine.settings.primary_model, result(ids))
        assert role == 'review' and kwargs['exclude_model'] == engine.settings.primary_model
        return Completion('', 'glm', {'approved': True})
    models.call = call
    engine.enqueue_consolidations()
    engine.enqueue_consolidations()
    jobs = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")
    assert len(jobs) == 1
    job = engine.queue.get(jobs[0]['id'])
    engine.coordinator.allowed = lambda: False
    with pytest.raises(WorkPaused):
        await engine.process(job)
    assert calls == ['auxiliary', 'memory', 'review']
    engine.coordinator.allowed = engine.allowed
    await engine.process(engine.queue.get(job['id']))
    assert calls == ['auxiliary', 'memory', 'review']
    assert len(MemoryConsolidation(engine.memory).relations(ids[1])) == 1
    assert qq.sent == []
    engine.enqueue_consolidations()
    assert len(store.rows("SELECT id FROM jobs WHERE kind='consolidate'")) == 1
    store.close()


async def test_direct_call_preempts_idle_primary_and_preserves_job(tmp_path):
    store, engine, _qq, models, ids = await engine_pair(tmp_path)
    entered = asyncio.Event()
    async def call(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    models.call = call
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    task = asyncio.create_task(engine.process(engine.queue.get(jid)))
    await asyncio.wait_for(entered.wait(), 1)
    engine.ingest({'time': engine.clock().timestamp(), 'post_type': 'message', 'message_type': 'group',
        'group_id': 123, 'self_id': 11, 'user_id': 22, 'message_id': 2,
        'sender': {'nickname': '甲'}, 'message': [{'type': 'text', 'data': {'text': engine.settings.aliases[0] + '你好'}}]})
    with pytest.raises(WorkPaused):
        await asyncio.wait_for(task, 1)
    assert engine.queue.get(jid)['state'] == 'pending'
    assert not MemoryConsolidation(engine.memory).relations(ids[1])
    store.close()


async def test_retrieval_receives_only_current_relations_and_original_evidence(tmp_path):
    store, memory, coordinator, ids = await pair(tmp_path)
    service = MemoryConsolidation(memory)
    await coordinator.commit(lambda: service.commit(service.snapshot(), result(ids), reviewed=True))
    selections = []
    class Models:
        async def call(self, role, payload, **kwargs):
            data = json.loads(payload[-1]['content'])
            if 'candidates' not in data:
                return Completion('', 'ds', {'terms': ['甲']})
            selections.append(data['candidates'])
            return Completion('', 'ds', {'ids': ids})
    memory.models = Models()
    selected = await memory.retrieve('甲现在喝什么', subjects=['22'])
    assert len(selected) == 2
    item = next(row for row in selections[0] if row['id'] == ids[1])
    assert item['relations'][0]['target_content'] == '甲以前喜欢茶'
    await coordinator.commit(lambda: memory.edit(ids[0], content='人工修正', expected_revision=1))
    await memory.retrieve('甲现在喝什么', subjects=['22'])
    assert all(not row['relations'] for row in selections[-1])
    store.close()


def test_admin_exposes_scoped_current_relations_after_edit_invalidates(tmp_path):
    from copy import copy

    from fastapi.testclient import TestClient

    from buwubot.app import create_app
    with TestClient(create_app(tmp_path, run_bot=False), base_url='http://127.0.0.1:8765') as client:
        cfg = client.get('/api/config').json()
        cfg = client.patch('/api/config', json={'revision': cfg['revision'],
            'changes': {'group_id': '123'}}).json()
        ids = []
        for content in ('过去偏爱茶', '后来改喝咖啡'):
            response = client.post('/api/memories', json={'config_revision': cfg['revision'],
                'tier': 'secondary', 'content': content})
            assert response.status_code == 200, response.text
            ids.append(response.json()['id'])
        memory = copy(client.app.state.memory)
        memory.settings = client.app.state.store.settings()[0]
        service = MemoryConsolidation(memory)
        client.portal.call(client.app.state.coordinator.commit,
            lambda: service.commit(service.snapshot(), result(ids), reviewed=True))
        for query in ('', '&query=咖啡'):
            items = client.get('/api/memories?tier=secondary' + query).json()['items']
            assert next(item for item in items if item['id'] == ids[1])['relations'][0]['kind'] == 'correction'
        assert client.get('/api/memories?conversation_id=456').json()['items'] == []
        edited = client.patch('/api/memories/' + ids[0], json={'revision': 1,
            'content': '人工修正', 'tier': 'secondary'})
        assert edited.status_code == 200
        assert all(not item['relations'] for item in client.get('/api/memories').json()['items'])


def test_constructing_scoped_reader_does_not_commit_another_transaction(tmp_path):
    store, memory, _ = setup_memory(tmp_path)
    with pytest.raises(RuntimeError, match='rollback'), store.transaction() as db:
        db.execute("INSERT INTO state VALUES('reader-must-not-commit','true')")
        MemoryConsolidation(memory)
        raise RuntimeError('rollback')
    assert store.get_state('reader-must-not-commit') is None
    store.close()


async def test_consolidating_feelings_does_not_recall_or_rewrite_them(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    ids = []
    for text in ('那次交流很开心', '后来又聊起同一话题，也很开心'):
        ids.append(await coordinator.commit(lambda text=text: memory.commit(proposal().model_copy(update={
            'content': text, 'subject_id': '', 'persona_id': memory.settings.persona_id,
            'tier': 'feeling', 'feeling_strength': .6, 'event_time': time.time()}), reviewed=True)))
    anchors = [dict(row) for row in store.rows('SELECT * FROM emotion_state ORDER BY memory_id')]
    epoch = memory.epoch()
    service = MemoryConsolidation(memory)
    await coordinator.commit(lambda: service.commit(service.snapshot(), result(ids, 'related'), reviewed=True))
    assert len(service.relations(ids[1])) == 1
    assert anchors == [dict(row) for row in store.rows('SELECT * FROM emotion_state ORDER BY memory_id')]
    assert memory.epoch() == epoch
    assert not store.rows('SELECT * FROM emotion_recalls')
    store.close()


async def test_bounded_focus_progresses_past_first_batch(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    for number in range(30):
        await coordinator.commit(lambda number=number: memory.commit(
            proposal().model_copy(update={'content': f'甲的独立事实 {number}'})))
    service = MemoryConsolidation(memory)
    first = service.snapshot()
    assert len(first['focus_ids']) == 24
    await coordinator.commit(lambda: service.commit(first, {'relations': []}, reviewed=False))
    second = service.snapshot()
    assert len(second['focus_ids']) == 6
    assert not set(first['focus_ids']) & set(second['focus_ids'])
    await coordinator.commit(lambda: service.commit(second, {'relations': []}, reviewed=False))
    assert service.snapshot() is None
    store.close()


async def test_schedule_pause_cancels_idle_model_without_completing_job(tmp_path):
    store, engine, _, models, _ = await engine_pair(tmp_path)
    entered = asyncio.Event()
    async def call(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    models.call = call
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    task = asyncio.create_task(engine.process(engine.queue.get(jid)))
    await asyncio.wait_for(entered.wait(), 1)
    night = engine.clock().replace(hour=23)
    engine.clock = lambda: night
    supervisor = asyncio.create_task(engine._supervise())
    try:
        with pytest.raises(WorkPaused):
            await asyncio.wait_for(task, 1)
        assert engine.queue.get(jid)['state'] == 'pending'
    finally:
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
        store.close()


async def test_background_backlog_defers_next_stage_without_discarding_inflight_result(tmp_path):
    store, engine, _, models, _ = await engine_pair(tmp_path)
    entered, release, supervised = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def call(*args, **kwargs):
        entered.set()
        await release.wait()
        return Completion('', 'glm', {'relations': [], 'distillations': []})

    async def plan_pending():
        supervised.set()

    models.call = call
    engine.plan_pending = plan_pending
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    task = asyncio.create_task(engine.process(engine.queue.get(jid)))
    await asyncio.wait_for(entered.wait(), 1)
    engine.queue.enqueue('context', {}, dedupe_key='new-background-material', priority=2,
                         source_time=engine.clock().timestamp())
    assert not engine.consolidation_allowed()
    supervisor = asyncio.create_task(engine._supervise())
    try:
        await asyncio.wait_for(supervised.wait(), 1)
        release.set()
        with pytest.raises(WorkPaused):
            await asyncio.wait_for(task, 1)
        assert 'prepared_candidates' in engine.queue.get(jid)['payload']
        assert engine.queue.get(jid)['state'] == 'pending'
    finally:
        supervisor.cancel()
        task.cancel()
        await asyncio.gather(supervisor, task, return_exceptions=True)
        store.close()


async def test_direct_call_while_waiting_for_commit_lock_defers_saved_result(tmp_path):
    store, engine, _, models, ids = await engine_pair(tmp_path)
    async def call(role, *args, **kwargs):
        if role == 'auxiliary':
            return Completion('', 'glm', {'relations': [], 'distillations': []})
        return Completion('', engine.settings.primary_model if role == 'memory' else 'glm',
                          result(ids) if role == 'memory' else {'approved': True})
    models.call = call
    entered_commit = asyncio.Event()
    original_commit = engine.coordinator.commit
    async def commit(operation, **kwargs):
        entered_commit.set()
        return await original_commit(operation, **kwargs)
    engine.coordinator.commit = commit
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    await engine.coordinator.lock.acquire()
    task = asyncio.create_task(engine.process(engine.queue.get(jid)))
    await asyncio.wait_for(entered_commit.wait(), 1)
    engine.store.set_state('last_spoke', engine.clock().timestamp())
    engine.pause_consolidation()
    engine.coordinator.lock.release()
    with pytest.raises(WorkPaused):
        await task
    assert engine.queue.get(jid)['payload']['reviewed'] is True
    assert not MemoryConsolidation(engine.memory).relations(ids[1])
    store.close()


async def test_relation_reads_and_prompt_annotations_are_bounded(tmp_path):
    store, memory, coordinator, ids = await pair(tmp_path)
    service = MemoryConsolidation(memory)
    for number in range(60):
        mid = await coordinator.commit(lambda number=number: memory.commit(
            proposal().model_copy(update={'content': f'甲的经历{number} ' + '长期记忆内容' * 30})))
        with store.transaction() as db:
            db.execute('INSERT INTO memory_relations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (f'budget-{number}', memory.settings.persona_id, '123', mid, 1, ids[0], 1,
                 'correction' if number == 0 else 'related', '简短关系说明', '["123:id:1"]', number))
    links = service.relations(ids[0], max_chars=2000)
    assert links and links[0]['kind'] == 'correction'
    assert len(json.dumps(links, ensure_ascii=False)) <= 2000
    assert len(service.relations(ids[0])) <= 48
    assert service.relations(ids[0], max_chars=0) == []
    store.close()
