import pytest
from test_memory import proposal
from test_memory_graph import facts, mention

from buwubot.memory_graph import MemoryGraph
from buwubot.store import Conflict


async def temporal_pair(tmp_path, new_time=200):
    store, memory, coordinator, ids = await facts(tmp_path)
    store.connection.execute('UPDATE memories SET event_time=100 WHERE id=?', (ids[0],))
    graph = MemoryGraph(memory)
    await coordinator.commit(lambda: graph.apply([
        mention(ids[0]), mention(ids[0], node_type='place', name='Harbor', aliases=[])],
        edges=[{'source_index': 0, 'target_index': 1, 'relation_type': 'located_at',
                'evidence_memory_ids': [ids[0]], 'summary': 'lives in Harbor', 'confidence': .9}],
        origin_model=memory.settings.primary_model, reviewed=True))
    old = graph.entity_relations('Alpha')[0]
    new_id = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={
        'content': 'Alias Alpha moved from Harbor to River.', 'tier': 'important', 'event_time': new_time}), reviewed=True))
    drafts = [mention(new_id), mention(new_id, node_type='place', name='River', aliases=[])]
    edge = {'source_index': 0, 'target_index': 1, 'relation_type': 'located_at',
            'evidence_memory_ids': [new_id], 'summary': 'now lives in River, moved from Harbor',
            'confidence': .9, 'supersedes': old['id']}
    return store, memory, coordinator, graph, ids[0], new_id, old, drafts, edge


async def test_explicit_supersession_closes_old_edge_and_keeps_history_and_idempotency(tmp_path):
    store, memory, coordinator, graph, old_id, new_id, old, drafts, edge = await temporal_pair(tmp_path)
    original = memory.get(old_id)
    try:
        await coordinator.commit(lambda: graph.apply(drafts, edges=[edge],
            origin_model=memory.settings.primary_model, reviewed=True))
        current = graph.entity_relations('Alpha')
        assert len(current) == 1 and current[0]['id'] != old['id']
        history = graph.entity_relations('Alpha', include_history=True)
        assert len(history) == 2 and history[0]['valid_to'] == 200
        assert history[0]['is_current'] is False and history[1]['is_current'] is True
        assert memory.get(old_id) == original
        await coordinator.commit(lambda: graph.apply(drafts, edges=[edge],
            origin_model=memory.settings.primary_model, reviewed=True))
        assert graph.entity_relations('Alpha', include_history=True) == history
        await coordinator.commit(lambda: memory.edit(new_id, content='corrected new fact', expected_revision=1))
        assert graph.entity_relations('Alpha') == []
        assert len(graph.entity_relations('Alpha', include_history=True)) == 1
    finally:
        store.close()


@pytest.mark.parametrize('new_time', [None, 50])
async def test_unknown_or_backward_replacement_time_is_not_guessed(tmp_path, new_time):
    store, memory, coordinator, graph, _, _, old, drafts, edge = await temporal_pair(tmp_path, new_time)
    try:
        with pytest.raises(ValueError):
            await coordinator.commit(lambda: graph.apply(drafts, edges=[edge],
                origin_model=memory.settings.primary_model, reviewed=True))
        assert [item['id'] for item in graph.entity_relations('Alpha')] == [old['id']]
    finally:
        store.close()


async def test_conflicting_second_replacement_cannot_overwrite_original_closure(tmp_path):
    store, memory, coordinator, graph, _, _, _old, drafts, edge = await temporal_pair(tmp_path)
    try:
        await coordinator.commit(lambda: graph.apply(drafts, edges=[edge],
            origin_model=memory.settings.primary_model, reviewed=True))
        other = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={
            'content': 'Alias Alpha now lives in Hill.', 'tier': 'important', 'event_time': 300}), reviewed=True))
        with pytest.raises(Conflict):
            await coordinator.commit(lambda: graph.apply([
                mention(other), mention(other, node_type='place', name='Hill', aliases=[])], edges=[edge | {
                    'evidence_memory_ids': [other], 'summary': 'lives in Hill'}],
                origin_model=memory.settings.primary_model, reviewed=True))
        assert len(graph.entity_relations('Alpha', include_history=True)) == 2
    finally:
        store.close()


async def test_same_batch_cannot_close_one_edge_with_two_replacements(tmp_path):
    store, memory, coordinator, graph, _, _, old, drafts, edge = await temporal_pair(tmp_path)
    other = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={
        'content': 'Alias Alpha now lives in Hill.', 'tier': 'important', 'event_time': 300}), reviewed=True))
    try:
        with pytest.raises(ValueError, match='same batch'):
            await coordinator.commit(lambda: graph.apply(drafts + [
                mention(other), mention(other, node_type='place', name='Hill', aliases=[])], edges=[edge, edge | {
                    'source_index': 2, 'target_index': 3, 'evidence_memory_ids': [other], 'summary': 'Hill'}],
                origin_model=memory.settings.primary_model, reviewed=True))
        assert [item['id'] for item in graph.entity_relations('Alpha')] == [old['id']]
    finally:
        store.close()


async def test_new_focus_snapshot_supplies_existing_same_person_relation_for_explicit_replacement(tmp_path):
    from buwubot.consolidation import MemoryConsolidation
    store, memory, coordinator, graph, old_id, _new_id, old, drafts, edge = await temporal_pair(tmp_path)
    service = MemoryConsolidation(memory)
    try:
        # Simulate a previously processed old fact, then a later new fact about the same QQ.
        store.connection.execute('INSERT INTO memory_consolidation_seen VALUES(?,?,?)',
                                 (memory.settings.persona_id, old_id, 1))
        snapshot = service.snapshot(mutation_since=0)
        context = service.model_context(snapshot)
        assert any(item['id'] == old['id'] for item in context['existing_entity_edges'])
        output = {'relations': [], 'entity_mentions': drafts, 'entity_edges': [edge]}
        service.validate_plan(snapshot, output)
        await coordinator.commit(lambda: service.commit(snapshot, output, reviewed=True))
        assert len(graph.entity_relations('Alpha')) == 1
        timeline = graph.query({'mode': 'timeline', 'entities': ['Alpha']})
        assert any(not edge['is_current'] for item in timeline for edge in item.get('entity_relations', []))
    finally:
        store.close()


async def test_timeline_selection_refreshes_relation_closed_during_model_wait(tmp_path):
    import json

    from buwubot.models import Completion

    store, memory, coordinator, graph, old_id, _, old, drafts, edge = await temporal_pair(tmp_path)
    class Models:
        async def call(self, role, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            if 'candidates' not in data:
                return Completion('', 'helper', {'terms': ['no-match'],
                    'graph_query': {'mode': 'timeline', 'entities': ['Alpha']}})
            await coordinator.commit(lambda: graph.apply(drafts, edges=[edge],
                origin_model=memory.settings.primary_model, reviewed=True))
            return Completion('', 'helper', {'ids': [old_id]})
    memory.models = Models()
    memory.search_text = lambda *args, **kwargs: []
    try:
        found = await memory.retrieve('Alpha history')
        assert len(found) == 1
        relation = next(item for item in found[0]['entity_relations'] if item['id'] == old['id'])
        assert relation['is_current'] is False and relation['valid_to'] == 200
    finally:
        store.close()
