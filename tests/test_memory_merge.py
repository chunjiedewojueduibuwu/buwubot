import json
import sqlite3
import time

import pytest
from test_memory import proposal, setup_memory
from test_memory_reinforcement import mention

from buwubot.consolidation import MemoryConsolidation
from buwubot.coordinator import Coordinator
from buwubot.memory import MemoryService
from buwubot.store import Conflict, Store


async def duplicates(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    base = time.time() - 300
    mention(memory, 2, base + 200, text='我还是更喜欢喝茶')
    ids = []
    for content, source, when in [('甲喜欢茶', '123:id:1', 100), ('甲偏好喝茶', '123:id:2', 200)]:
        ids.append(await coordinator.commit(lambda content=content, source=source, when=when: memory.commit(proposal().model_copy(update={
            'content': content, 'tier': 'recent', 'source_message_ids': [source], 'event_time': base + when,
            'tags': ['茶'] if when == 100 else ['饮品']}))))
    return store, memory, coordinator, ids


def merged(ids):
    return {'relations': [{'source_id': ids[1], 'target_id': ids[0], 'kind': 'duplicate', 'summary': '同一饮品偏好'}],
        'merges': [{'target_id': ids[0], 'source_memory_ids': [ids[1]], 'confidence': .95}]}


async def test_merge_unions_provenance_preserves_versions_time_and_retention_after_reopen(tmp_path):
    store, memory, coordinator, ids = await duplicates(tmp_path)
    service = MemoryConsolidation(memory)
    snapshot = service.snapshot(mutation_since=0)
    before = [memory.get(mid) for mid in ids]
    state = memory.lifecycle.state(before[0])
    try:
        await coordinator.commit(lambda: service.commit(snapshot, merged(ids), reviewed=True))
        target = memory.get(ids[0])
        assert target['source_message_ids'] == ['123:id:1', '123:id:2']
        assert target['tags'] == ['茶', '饮品']
        assert target['content'] == before[0]['content'] and target['event_time'] == before[0]['event_time']
        assert target['confidence'] == before[0]['confidence'] and target['revision'] == 2
        after = memory.lifecycle.state(target)
        assert after['strength'] <= 1 and after['since'] >= state['since']
        assert after['half_life_override'] == state['half_life_override']
        assert len(store.rows('SELECT * FROM memory_merge_recalls')) == 1
        assert memory.get(ids[1]) is None
        versions = [json.loads(row['payload']) for row in store.rows('SELECT payload FROM memory_versions')]
        assert all(item in versions for item in before)
        assert len(store.rows('SELECT * FROM memory_merges')) == 1
        assert {row['source_id'] for row in store.rows('SELECT source_id FROM memory_evidence_seen WHERE memory_id=?',
                (ids[0],))} == {'123:id:1', '123:id:2'}
        cfg = memory.settings
    finally:
        store.close()
    store = Store(tmp_path / 'bot.db')
    memory = MemoryService(store, cfg)
    coordinator = Coordinator(store, None, cfg)
    try:
        await coordinator.commit(lambda: MemoryConsolidation(memory).commit(snapshot, merged(ids), reviewed=True))
        assert memory.get(ids[0]) == target
        assert len(store.rows('SELECT * FROM memory_merges')) == 1
        assert memory.lifecycle.state(target) == after
    finally:
        store.close()


@pytest.mark.parametrize('fault', ['related', 'unreviewed', 'legacy', 'low_confidence', 'overlap', 'manual', 'other_person'])
async def test_merge_never_uses_similarity_alone_or_bypasses_scope(tmp_path, fault):
    store, memory, coordinator, ids = await duplicates(tmp_path)
    output = merged(ids)
    if fault == 'related':
        output['relations'][0]['kind'] = 'related'
    elif fault == 'low_confidence':
        output['merges'][0]['confidence'] = .5
    elif fault == 'overlap':
        output['merges'].append(output['merges'][0].copy())
    elif fault == 'manual':
        await coordinator.commit(lambda: memory.edit(ids[1], content='甲偏好喝茶（人工维护）', expected_revision=1))
    elif fault == 'other_person':
        store.connection.execute("UPDATE memories SET subject_id='33' WHERE id=?", (ids[1],))
    service = MemoryConsolidation(memory)
    snapshot = service.snapshot(mutation_since=None if fault == 'legacy' else 0)
    before = [memory.get(mid) for mid in ids]
    try:
        with pytest.raises((ValueError, Conflict)):
            await coordinator.commit(lambda: service.commit(snapshot, output, reviewed=fault != 'unreviewed'))
        assert before == [memory.get(mid) for mid in ids]
        assert not store.rows('SELECT * FROM memory_merges')
        assert not store.rows('SELECT * FROM memory_consolidation_seen')
    finally:
        store.close()


async def test_delete_index_failure_rolls_back_target_and_tombstone(tmp_path):
    store, memory, coordinator, ids = await duplicates(tmp_path)
    service = MemoryConsolidation(memory)
    snapshot = service.snapshot(mutation_since=0)
    before = [memory.get(mid) for mid in ids]
    state = [tuple(row) for row in store.rows('SELECT * FROM memory_lifecycle')]
    store.connection.execute("CREATE TRIGGER fail_delete BEFORE INSERT ON index_outbox WHEN NEW.operation='delete' "
                             "BEGIN SELECT RAISE(ABORT,'fixture delete failure'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await coordinator.commit(lambda: service.commit(snapshot, merged(ids), reviewed=True))
        assert before == [memory.get(mid) for mid in ids]
        assert [tuple(row) for row in store.rows('SELECT * FROM memory_lifecycle')] == state
        assert not store.rows('SELECT * FROM memory_merges')
        assert not store.rows('SELECT * FROM memory_consolidation_seen')
        assert not store.rows('SELECT * FROM memory_consolidation_applied')
    finally:
        store.close()


async def test_revoking_scope_after_snapshot_blocks_merge_and_keeps_all_records(tmp_path):
    store, memory, coordinator, ids = await duplicates(tmp_path)
    store.save_settings(memory.settings, store.settings()[1])
    service = MemoryConsolidation(memory)
    snapshot = service.snapshot(mutation_since=0)
    cfg, revision = store.settings()
    store.save_settings(cfg.model_copy(update={'group_id': '456'}), revision)
    try:
        with pytest.raises(Conflict):
            await coordinator.commit(lambda: service.commit(snapshot, merged(ids), reviewed=True))
        assert all(memory.get(mid) for mid in ids)
        assert not store.rows('SELECT * FROM memory_merges')
    finally:
        store.close()


async def test_later_independent_duplicate_can_merge_past_prior_tombstone_barrier(tmp_path):
    store, memory, coordinator, ids = await duplicates(tmp_path)
    service = MemoryConsolidation(memory)
    try:
        mention(memory, 3, 300, text='还是爱喝茶')
        third = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={
            'tier': 'recent', 'content': '甲喝饮品还是喜欢茶', 'source_message_ids': ['123:id:3'], 'tags': ['茶']})))
        first = service.snapshot(mutation_since=0)
        # The third record is context for the first bounded batch, then becomes the next focus.
        first['focus_ids'] = ids
        first['fingerprint'] = service.fingerprint(first)
        await coordinator.commit(lambda: service.commit(first, merged(ids), reviewed=True))
        snap = service.snapshot(mutation_since=0)
        assert ids[0] in {item['id'] for item in snap['records']}
        await coordinator.commit(lambda: service.commit(snap, merged([ids[0], third]), reviewed=True))
        assert memory.get(third) is None
        assert memory.get(ids[0])['source_message_ids'] == ['123:id:1', '123:id:2', '123:id:3']
        assert len(store.rows('SELECT * FROM memory_merges')) == 2
    finally:
        store.close()


@pytest.mark.parametrize('source_tier', ['secondary', 'important'])
async def test_merge_keeps_fuller_target_content_without_losing_higher_source_tier(tmp_path, source_tier):
    store, memory, coordinator, ids = await duplicates(tmp_path)
    store.connection.execute('UPDATE memories SET tier=? WHERE id=?', (source_tier, ids[1]))
    service = MemoryConsolidation(memory)
    snapshot = service.snapshot(mutation_since=0)
    original = memory.get(ids[0])
    output = merged(ids)
    try:
        packet = service.review_context(snapshot, output)
        assert packet['operation_evidence']['merges'][0]['effective_target_tier'] == source_tier
        await coordinator.commit(lambda: service.commit(snapshot, output, reviewed=True))
        target = memory.get(ids[0])
        assert target['tier'] == source_tier
        assert target['content'] == original['content'] and target['event_time'] == original['event_time']
        assert memory.get(ids[1]) is None
        assert len(store.rows('SELECT * FROM memory_merge_recalls')) == 1
        await coordinator.commit(lambda: service.commit(snapshot, output, reviewed=True))
        assert memory.get(ids[0]) == target
    finally:
        store.close()
