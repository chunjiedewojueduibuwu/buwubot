import pytest
from test_memory import proposal
from test_memory_merge import duplicates, merged

from buwubot.consolidation import MemoryConsolidation


@pytest.mark.parametrize('extra_count', [1, 2])
async def test_merge_recall_counts_additional_records_once_and_preserves_event_time(tmp_path, monkeypatch, extra_count):
    now = 2_000_000_000
    monkeypatch.setattr('buwubot.memory.time.time', lambda: now)
    store, memory, coordinator, ids = await duplicates(tmp_path)
    old_event = memory.get(ids[0])['event_time']
    if extra_count == 2:
        ids.append(await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={
            'tier': 'recent', 'content': '甲的饮品选择偏茶', 'tags': ['茶']}))))
    store.connection.execute('UPDATE memory_lifecycle SET since=? WHERE memory_id=?', (now - 7 * 86400, ids[0]))
    service = MemoryConsolidation(memory)
    snapshot = service.snapshot(mutation_since=0)
    output = merged(ids)
    if extra_count == 2:
        output['relations'].append({'source_id': ids[2], 'target_id': ids[0], 'kind': 'duplicate', 'summary': '同一偏好'})
        output['merges'][0]['source_memory_ids'].append(ids[2])
    try:
        assert memory.with_reading_policy(memory.get(ids[0]), now=now)['memory_remaining'] == pytest.approx(.5)
        await coordinator.commit(lambda: service.commit(snapshot, output, reviewed=True))
        expected = 1 - .5 * (1 - memory.settings.memory_policies.recent.merge_recall_gain) ** extra_count
        view = memory.with_reading_policy(memory.get(ids[0]), now=now)
        assert view['memory_remaining'] == pytest.approx(expected)
        assert view['decay_basis_time'] == now
        assert memory.get(ids[0])['event_time'] == old_event
        assert store.rows('SELECT recall_count FROM memory_merge_recalls')[0]['recall_count'] == extra_count
        await coordinator.commit(lambda: service.commit(snapshot, output, reviewed=True))
        assert memory.with_reading_policy(memory.get(ids[0]), now=now) == view
        assert len(store.rows('SELECT * FROM memory_merge_recalls')) == 1
    finally:
        store.close()


async def test_zero_merge_gain_preserves_original_decay_anchor(tmp_path):
    store, memory, coordinator, ids = await duplicates(tmp_path)
    memory.settings.memory_policies.recent.merge_recall_gain = 0
    service = MemoryConsolidation(memory)
    before = memory.lifecycle.state(memory.get(ids[0]))
    try:
        await coordinator.commit(lambda: service.commit(service.snapshot(mutation_since=0), merged(ids), reviewed=True))
        assert memory.lifecycle.state(memory.get(ids[0])) == before
        assert not store.rows('SELECT * FROM memory_merge_recalls')
    finally:
        store.close()
