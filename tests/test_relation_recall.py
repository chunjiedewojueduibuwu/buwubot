import json

import pytest
from test_memory import proposal, setup_memory

from buwubot.consolidation import MemoryConsolidation
from buwubot.models import Completion


async def chain(tmp_path, count=3):
    store, memory, coordinator = setup_memory(tmp_path)
    ids = []
    for text in ('甲的茶叶放进红盒子', '红盒子位于书架顶层', '书架买于去年', '书架来自北店', '北店在河边')[:count]:
        ids.append(await coordinator.commit(lambda text=text: memory.commit(proposal().model_copy(update={
            'content': text, 'tier': 'important'}), reviewed=True)))
    service = MemoryConsolidation(memory)
    links = [{'source_id': ids[i], 'target_id': ids[i+1], 'kind': 'related', 'summary': '位置线索' if i == 0 else '物品线索'}
             for i in range(count - 1)]
    await coordinator.commit(lambda: service.commit(service.snapshot(), {'relations': links}, reviewed=True))
    return store, memory, coordinator, ids


async def test_one_hop_read_only_expansion_respects_material_budget(tmp_path):
    store, memory, _, ids = await chain(tmp_path)
    service = MemoryConsolidation(memory)
    before = [tuple(row) for row in store.rows('SELECT * FROM memory_lifecycle')]
    try:
        found = service.neighbors([ids[0]], max_hops=1)
        assert [item['id'] for item in found] == [ids[1]]
        assert found[0]['relation_recall']['seed_id'] == ids[0]
        assert service.neighbors([ids[0]], max_chars=0) == []
        assert service.neighbors([ids[0]], max_chars=10) == []
        assert before == [tuple(row) for row in store.rows('SELECT * FROM memory_lifecycle')]
    finally:
        store.close()


@pytest.mark.parametrize('change', ['revision', 'delete', 'persona', 'group', 'expired'])
async def test_stale_or_out_of_scope_edges_cannot_recall_other_endpoint(tmp_path, change):
    store, memory, coordinator, ids = await chain(tmp_path)
    try:
        if change == 'revision':
            await coordinator.commit(lambda: memory.edit(ids[1], content='位置已修改', expected_revision=1))
        elif change == 'delete':
            await coordinator.commit(lambda: memory.delete(ids[1], expected_revision=1))
        elif change == 'expired':
            store.connection.execute("UPDATE memories SET tier='recent' WHERE id=?", (ids[1],))
            store.connection.execute('UPDATE memory_lifecycle SET since=0 WHERE memory_id=?', (ids[1],))
        else:
            column, value = ('persona_id', 'another') if change == 'persona' else ('group_id', '999')
            store.connection.execute(f'UPDATE memories SET {column}=? WHERE id=?', (value, ids[1]))
        assert MemoryConsolidation(memory).neighbors([ids[0]]) == []
        assert MemoryConsolidation(memory).relations(ids[0]) == []
    finally:
        store.close()


async def test_retrieval_supplies_linked_fact_to_model_and_switch_disables_expansion(tmp_path):
    store, memory, _, ids = await chain(tmp_path)
    packets = []
    class Models:
        async def call(self, role, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            if 'candidates' not in data:
                return Completion('', 'helper', {'terms': ['茶叶']})
            packets.append(data['candidates'])
            return Completion('', 'helper', {'ids': [ids[1]]})
    memory.models = Models()
    try:
        found = await memory.retrieve('茶叶在哪')
        assert [item['id'] for item in found] == [ids[1]]
        assert next(item for item in packets[-1] if item['id'] == ids[1])['relation_recall']['seed_id'] == ids[0]
        memory.settings = memory.settings.model_copy(update={'memory_relation_expansion': False})
        assert await memory.retrieve('茶叶在哪') == []
        assert ids[1] not in {item['id'] for item in packets[-1]}
    finally:
        store.close()


@pytest.mark.parametrize('return_neighbor', [False, True])
async def test_memory_changed_while_selection_waits_is_not_returned(tmp_path, return_neighbor):
    store, memory, coordinator, ids = await chain(tmp_path)
    class Models:
        async def call(self, role, messages, **kwargs):
            if 'candidates' not in json.loads(messages[-1]['content']):
                return Completion('', 'helper', {'terms': ['茶叶']})
            await coordinator.commit(lambda: memory.delete(ids[0], expected_revision=1))
            return Completion('', 'helper', {'ids': [ids[1] if return_neighbor else ids[0]]})
    memory.models = Models()
    try:
        assert await memory.retrieve('茶叶在哪') == []
    finally:
        store.close()


@pytest.mark.parametrize('depth', [1, 2, 3])
async def test_multihop_returns_traceable_shortest_paths_with_depth_limit(tmp_path, depth):
    store, memory, _, ids = await chain(tmp_path, count=5)
    service = MemoryConsolidation(memory)
    before = store.connection.total_changes
    try:
        found = service.neighbors([ids[0]], max_hops=depth)
        assert [item['id'] for item in found] == ids[1:depth+1]
        for distance, item in enumerate(found, 1):
            path = item['relation_recall']
            assert path['seed_id'] == ids[0] and len(path['steps']) == distance
            assert path['steps'][-1]['target_id'] == item['id']
            assert service.path_current(path)
        assert store.connection.total_changes == before
        assert len(service.neighbors([ids[0]])) == 3
    finally:
        store.close()


async def test_multihop_selection_rechecks_intermediate_node_after_model_wait(tmp_path):
    store, memory, coordinator, ids = await chain(tmp_path)
    class Models:
        async def call(self, role, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            if 'candidates' not in data:
                return Completion('', 'helper', {'terms': ['tea']})
            assert ids[2] in {item['id'] for item in data['candidates']}
            await coordinator.commit(lambda: memory.delete(ids[1], expected_revision=1))
            return Completion('', 'helper', {'ids': [ids[2]]})
    memory.models = Models()
    # Keep the query deterministic without relying on language tokenization.
    original_search = memory.search_text
    memory.search_text = lambda *args, **kwargs: [memory.get(ids[0])]
    try:
        assert await memory.retrieve('tea') == []
    finally:
        memory.search_text = original_search
        store.close()


async def star(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    ids = []
    for n in range(37):
        text = f'branch-{n} ' + 'detail ' * 60
        ids.append(await coordinator.commit(lambda text=text: memory.commit(proposal().model_copy(update={
            'content': text, 'tier': 'important'}), reviewed=True)))
    service = MemoryConsolidation(memory)
    snapshot = service.snapshot()
    snapshot['records'] = [service._record(mid) for mid in ids]
    snapshot['focus_ids'] = ids
    snapshot['fingerprint'] = service.fingerprint(snapshot)
    links = []
    for n in range(1, 13):
        for source, target in ((ids[0], ids[n]), (ids[n], ids[n+12]), (ids[n+12], ids[n+24])):
            links.append({'source_id': source, 'target_id': target, 'kind': 'related', 'summary': 'source-backed link'})
    await coordinator.commit(lambda: service.commit(snapshot, {'relations': links}, reviewed=True))
    return store, memory, ids


async def test_hub_reserves_deep_material_and_fills_unused_leaf_budget(tmp_path):
    store, memory, ids = await star(tmp_path)
    service = MemoryConsolidation(memory)
    try:
        found = service.neighbors([ids[0]])
        assert max(len(item['relation_recall']['steps']) for item in found) == 3
        assert sum(len(json.dumps(item, ensure_ascii=False)) + 2 for item in found) <= 12000
        assert len({item['id'] for item in found}) == len(found)
        # A real leaf-star still uses the leftover budget rather than returning only a third.
        store.connection.execute('DELETE FROM memory_relations WHERE source_id<>? AND target_id<>?', (ids[0], ids[0]))
        one = service.neighbors([ids[0]], max_hops=1)
        three = service.neighbors([ids[0]], max_hops=3)
        assert len(three) == len(one)
    finally:
        store.close()


async def test_hub_prioritizes_query_match_before_unrelated_neighbors(tmp_path):
    store, memory, ids = await star(tmp_path)
    try:
        found = MemoryConsolidation(memory).neighbors([ids[0]], query='branch-12')
        assert found[0]['id'] == ids[12]
    finally:
        store.close()
