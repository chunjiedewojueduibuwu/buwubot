import json

import pytest
from test_memory import proposal, setup_memory

from buwubot.memory_graph import MemoryGraph
from buwubot.models import Completion
from buwubot.store import Conflict


async def facts(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    memory.archive.accept({'time': 101, 'post_type': 'message', 'message_type': 'group',
        'group_id': 123, 'user_id': 33, 'self_id': 11, 'message_id': 2,
        'sender': {'nickname': 'Alpha'}, 'message': [{'type': 'text', 'data': {'text': 'hello'}}]}, memory.settings)
    ids = []
    for subject, text in [('22', 'Alias Alpha is preferred; lives in Harbor. Harbor north differs from Harbor south.'),
                          ('33', 'Alias Alpha belongs to another account.')]:
        ids.append(await coordinator.commit(lambda subject=subject, text=text: memory.commit(
            proposal().model_copy(update={'subject_id': subject, 'content': text, 'tier': 'important'}), reviewed=True)))
    return store, memory, coordinator, ids


def mention(mid, **updates):
    return {'memory_id': mid, 'expected_revision': 1, 'node_type': 'person',
            'name': 'Alias Alpha', 'aliases': ['Alpha'], 'confidence': .9} | updates


async def test_exact_aliases_keep_same_names_on_different_accounts_separate_and_read_only(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    graph = MemoryGraph(memory)
    try:
        await coordinator.commit(lambda: graph.apply([mention(mid) for mid in ids],
            origin_model=memory.settings.primary_model, reviewed=True))
        before = store.connection.total_changes
        anchors = graph.lookup('  ALPHA  ')
        assert len(anchors) == 2 and len({item['id'] for item in anchors}) == 2
        assert {item['identity_key'] for item in anchors} == {'qq:22', 'qq:33'}
        assert graph.lookup('Alp') == []
        assert store.connection.total_changes == before
        await coordinator.commit(lambda: graph.apply([mention(mid) for mid in ids],
            origin_model=memory.settings.primary_model, reviewed=True))
        assert len(graph.lookup('Alpha')) == 2
    finally:
        store.close()


@pytest.mark.parametrize('change', ['edit', 'delete', 'scope', 'persona', 'expired'])
async def test_alias_evidence_cannot_outlive_fact_revision_or_scope(tmp_path, change):
    store, memory, coordinator, ids = await facts(tmp_path)
    graph = MemoryGraph(memory)
    try:
        await coordinator.commit(lambda: graph.apply([mention(ids[0])],
            origin_model=memory.settings.primary_model, reviewed=True))
        if change == 'edit':
            await coordinator.commit(lambda: memory.edit(ids[0], content='Alias has changed', expected_revision=1))
        elif change == 'delete':
            await coordinator.commit(lambda: memory.delete(ids[0], expected_revision=1))
        elif change == 'scope':
            store.connection.execute("UPDATE memories SET group_id='999' WHERE id=?", (ids[0],))
        elif change == 'persona':
            store.connection.execute("UPDATE memories SET persona_id='other' WHERE id=?", (ids[0],))
        else:
            store.connection.execute("UPDATE memories SET tier='recent' WHERE id=?", (ids[0],))
            store.connection.execute('UPDATE memory_lifecycle SET since=0 WHERE memory_id=?', (ids[0],))
        assert graph.lookup('Alpha') == []
    finally:
        store.close()


async def test_graph_apply_requires_ds_review_and_coordinator_and_is_atomic(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    graph = MemoryGraph(memory)
    try:
        with pytest.raises(RuntimeError, match='coordinator'):
            graph.apply([mention(ids[0])], origin_model=memory.settings.primary_model, reviewed=True)
        for origin, reviewed in [('helper', True), (memory.settings.primary_model, False)]:
            with pytest.raises(ValueError):
                await coordinator.commit(lambda origin=origin, reviewed=reviewed: graph.apply([mention(ids[0])], origin_model=origin, reviewed=reviewed))
        with pytest.raises(ValueError):
            await coordinator.commit(lambda: graph.apply([mention(ids[0]), mention(ids[1], aliases=['Not in source'])],
                origin_model=memory.settings.primary_model, reviewed=True))
        assert graph.lookup('Alpha') == []
        with pytest.raises(Conflict):
            await coordinator.commit(lambda: graph.apply([mention(ids[0], expected_revision=7)],
                origin_model=memory.settings.primary_model, reviewed=True))
        assert graph.lookup('Alpha') == []
    finally:
        store.close()


async def test_nonperson_aliases_are_normalized_but_discriminators_are_not_merged(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    graph = MemoryGraph(memory)
    try:
        drafts = [mention(ids[0], node_type='place', name='Harbor', aliases=[], discriminator=key)
                  for key in ('north', 'south')]
        await coordinator.commit(lambda: graph.apply(drafts, origin_model=memory.settings.primary_model, reviewed=True))
        assert len(graph.lookup('harbor')) == 2
    finally:
        store.close()


async def test_retrieval_uses_exact_entity_alias_and_preserves_graph_toggle(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    graph = MemoryGraph(memory)
    await coordinator.commit(lambda: graph.apply([mention(ids[0])],
        origin_model=memory.settings.primary_model, reviewed=True))
    packets = []
    class Models:
        async def call(self, role, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            if 'candidates' not in data:
                return Completion('', 'helper', {'terms': ['Alpha']})
            packets.append(data['candidates'])
            return Completion('', 'helper', {'ids': [ids[0]]})
    memory.models = Models()
    memory.search_text = lambda *args, **kwargs: []
    try:
        found = await memory.retrieve('Alpha')
        assert [item['id'] for item in found] == [ids[0]]
        assert packets[-1][0]['entity_recall']['identity_key'] == 'qq:22'
        memory.settings = memory.settings.model_copy(update={'memory_relation_expansion': False})
        assert await memory.retrieve('Alpha') == []
    finally:
        store.close()


async def test_consolidation_commits_revision_bound_entity_mentions_in_same_transaction(tmp_path):
    from buwubot.consolidation import MemoryConsolidation

    store, memory, coordinator, ids = await facts(tmp_path)
    service = MemoryConsolidation(memory)
    snapshot = service.snapshot(mutation_since=0)
    output = {'relations': [], 'entity_mentions': [mention(ids[0])]}
    try:
        before = store.connection.total_changes
        service.validate_plan(snapshot, output)
        assert store.connection.total_changes == before
        assert MemoryGraph(memory).lookup('Alpha') == []
        await coordinator.commit(lambda: service.commit(snapshot, output, reviewed=True))
        assert len(MemoryGraph(memory).lookup('Alpha')) == 1
        await coordinator.commit(lambda: service.commit(snapshot, output, reviewed=True))
        assert len(MemoryGraph(memory).lookup('Alpha')) == 1
    finally:
        store.close()


async def test_entity_timeline_and_network_keep_event_time_and_evidence_paths(tmp_path):
    from test_relation_recall import chain
    store, memory, coordinator, ids = await chain(tmp_path)
    graph = MemoryGraph(memory)
    drafts = [mention(mid, node_type='topic', name=memory.get(mid)['content'], aliases=[])
              for mid in ids]
    try:
        await coordinator.commit(lambda: graph.apply(drafts, origin_model=memory.settings.primary_model, reviewed=True))
        start, end = memory.get(ids[0])['content'], memory.get(ids[2])['content']
        assert graph.paths(start, end, max_hops=1) == []
        paths = graph.paths(start, end, max_hops=3)
        assert len(paths) == 1
        assert [step['target_id'] for step in paths[0]['relation_recall']['steps']] == ids[1:]
        assert [item['id'] for item in graph.timeline(start)] == [ids[0]]
        await coordinator.commit(lambda: memory.delete(ids[1], expected_revision=1))
        assert graph.paths(start, end) == []
    finally:
        store.close()


async def test_timeline_orders_original_events_not_update_time_and_discards_stale_aliases(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    graph = MemoryGraph(memory)
    try:
        for mid, event, updated in [(ids[0], 20, 1), (ids[1], 10, 9999)]:
            store.connection.execute('UPDATE memories SET event_time=?,updated_at=? WHERE id=?', (event, updated, mid))
        await coordinator.commit(lambda: graph.apply([mention(mid, node_type='topic') for mid in ids],
            origin_model=memory.settings.primary_model, reviewed=True))
        assert [item['id'] for item in graph.timeline('Alpha')] == [ids[1], ids[0]]
        await coordinator.commit(lambda: memory.edit(ids[1], content='removed alias', expected_revision=1))
        assert [item['id'] for item in graph.timeline('Alpha')] == [ids[0]]
    finally:
        store.close()


async def test_entity_only_engine_plan_requires_independent_review_before_indexing(tmp_path):
    from test_consolidation import engine_pair
    store, engine, qq, models, ids = await engine_pair(tmp_path)
    calls = []
    name = engine.memory.get(ids[0])['content']
    output = {'relations': [], 'entity_mentions': [mention(ids[0], node_type='topic', name=name, aliases=[])]}
    async def call(role, messages, **kwargs):
        calls.append(role)
        if role == 'review':
            assert MemoryGraph(engine.memory).lookup(name) == []
            assert kwargs['exclude_model'] == engine.settings.primary_model
            return Completion('', 'independent', {'approved': True})
        return Completion('', engine.settings.primary_model if role == 'memory' else 'helper', output)
    models.call = call
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    try:
        await engine.process(engine.queue.get(jid))
        assert calls == ['auxiliary', 'memory', 'review']
        assert len(MemoryGraph(engine.memory).lookup(name)) == 1
        assert qq.sent == []
    finally:
        store.close()


async def test_entity_discriminator_requires_bound_source_evidence(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    try:
        with pytest.raises(ValueError, match='evidence'):
            await coordinator.commit(lambda: MemoryGraph(memory).apply([
                mention(ids[0], node_type='place', name='Harbor', aliases=[], discriminator='invented neighborhood')],
                origin_model=memory.settings.primary_model, reviewed=True))
        assert MemoryGraph(memory).lookup('Harbor') == []
    finally:
        store.close()


async def test_retrieval_routes_entity_timeline_from_existing_keyword_call(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    graph = MemoryGraph(memory)
    await coordinator.commit(lambda: graph.apply([mention(mid, node_type='topic') for mid in ids],
        origin_model=memory.settings.primary_model, reviewed=True))
    packets = []
    class Models:
        async def call(self, role, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            if 'candidates' not in data:
                return Completion('', 'helper', {'terms': ['unrelated'],
                    'graph_query': {'mode': 'timeline', 'entities': ['Alpha'], 'depth': 3}})
            packets.append(data['candidates'])
            return Completion('', 'helper', {'ids': ids})
    memory.models = Models()
    memory.search_text = lambda *args, **kwargs: []
    try:
        found = await memory.retrieve('What happened to Alpha over time?')
        assert {item['id'] for item in found} == set(ids)
        assert all(item['entity_recall']['mode'] == 'timeline' for item in packets[-1])
        memory.settings = memory.settings.model_copy(update={'memory_relation_expansion': False})
        assert await memory.retrieve('What happened to Alpha over time?') == []
    finally:
        store.close()


def test_graph_query_validation_does_not_accept_scope_override():
    from pydantic import ValidationError

    from buwubot.memory_graph import GraphQuery
    with pytest.raises(ValidationError):
        GraphQuery.model_validate({'mode': 'timeline', 'entities': ['Alpha'], 'group_id': 'other'})
    with pytest.raises(ValidationError):
        GraphQuery.model_validate({'mode': 'network', 'entities': ['Alpha'], 'depth': 9})


async def test_typed_entity_edges_bind_real_evidence_and_review(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    graph = MemoryGraph(memory)
    drafts = [mention(ids[0]), mention(ids[0], node_type='place', name='Harbor', aliases=[])]
    edges = [{'source_index': 0, 'target_index': 1, 'relation_type': 'located_at',
              'evidence_memory_ids': [ids[0]], 'summary': 'Alpha lives in Harbor', 'confidence': .9}]
    try:
        await coordinator.commit(lambda: graph.apply(drafts, edges=edges,
            origin_model=memory.settings.primary_model, reviewed=True))
        links = graph.entity_relations('Alpha')
        assert len(links) == 1 and links[0]['relation_type'] == 'located_at'
        assert links[0]['source_memory_ids'] == [ids[0]]
        assert links[0]['valid_from'] == memory.get(ids[0])['event_time']
        await coordinator.commit(lambda: memory.edit(ids[0], content='no longer lives there', expected_revision=1))
        assert graph.entity_relations('Alpha') == []
    finally:
        store.close()


async def test_bad_typed_edge_rolls_back_whole_entity_batch(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    graph = MemoryGraph(memory)
    drafts = [mention(ids[0]), mention(ids[0], node_type='place', name='Harbor', aliases=[])]
    edge = {'source_index': 0, 'target_index': 7, 'relation_type': 'located_at',
            'evidence_memory_ids': [ids[0]], 'summary': 'invalid target', 'confidence': .9}
    try:
        with pytest.raises(ValueError):
            await coordinator.commit(lambda: graph.apply(drafts, edges=[edge],
                origin_model=memory.settings.primary_model, reviewed=True))
        assert graph.lookup('Alpha') == []
        edge['target_index'] = 1
        edge['evidence_memory_ids'] = [ids[1]]
        with pytest.raises(ValueError):
            await coordinator.commit(lambda: graph.apply(drafts, edges=[edge],
                origin_model=memory.settings.primary_model, reviewed=True))
        assert graph.lookup('Alpha') == []
    finally:
        store.close()


async def test_consolidation_typed_edges_are_checked_and_committed_with_entities(tmp_path):
    from buwubot.consolidation import MemoryConsolidation
    store, memory, coordinator, ids = await facts(tmp_path)
    service = MemoryConsolidation(memory)
    output = {'relations': [], 'entity_mentions': [mention(ids[0]),
        mention(ids[0], node_type='place', name='Harbor', aliases=[])], 'entity_edges': [
        {'source_index': 0, 'target_index': 1, 'relation_type': 'located_at',
         'evidence_memory_ids': [ids[0]], 'summary': 'Alpha lives in Harbor', 'confidence': .9}]}
    snapshot = service.snapshot(mutation_since=0)
    try:
        before = store.connection.total_changes
        service.validate_plan(snapshot, output)
        assert store.connection.total_changes == before
        await coordinator.commit(lambda: service.commit(snapshot, output, reviewed=True))
        assert len(MemoryGraph(memory).entity_relations('Alpha')) == 1
        packet = MemoryGraph(memory).query({'mode': 'neighbors', 'entities': ['Alpha']})
        assert any(item.get('entity_relations') for item in packet)
    finally:
        store.close()


async def test_typed_edge_evidence_changed_during_selection_is_removed(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    graph = MemoryGraph(memory)
    drafts = [mention(ids[0]), mention(ids[0], node_type='place', name='Harbor', aliases=[])]
    await coordinator.commit(lambda: graph.apply(drafts, edges=[{'source_index': 0, 'target_index': 1,
        'relation_type': 'located_at', 'evidence_memory_ids': ids, 'summary': 'source-bound relation', 'confidence': .9}],
        origin_model=memory.settings.primary_model, reviewed=True))
    class Models:
        async def call(self, role, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            if 'candidates' not in data:
                return Completion('', 'helper', {'terms': ['other'],
                    'graph_query': {'mode': 'neighbors', 'entities': ['Alpha']}})
            assert any(item.get('entity_relations') for item in data['candidates'])
            await coordinator.commit(lambda: memory.edit(ids[1], content='changed other evidence', expected_revision=1))
            return Completion('', 'helper', {'ids': [ids[0]]})
    memory.models = Models()
    memory.search_text = lambda *args, **kwargs: []
    try:
        found = await memory.retrieve('Alpha')
        assert len(found) == 1 and not found[0].get('entity_relations')
    finally:
        store.close()


async def test_participation_edge_cannot_treat_a_place_or_topic_as_activity(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    try:
        drafts = [mention(ids[0]), mention(ids[0], node_type='topic', name='Harbor', aliases=[])]
        with pytest.raises(ValueError, match='endpoint type'):
            await coordinator.commit(lambda: MemoryGraph(memory).apply(drafts, edges=[{
                'source_index': 0, 'target_index': 1, 'relation_type': 'participates_in',
                'evidence_memory_ids': [ids[0]], 'summary': 'topic is not an activity', 'confidence': .9}],
                origin_model=memory.settings.primary_model, reviewed=True))
        assert MemoryGraph(memory).lookup('Alpha') == []
    finally:
        store.close()


async def test_network_mode_traverses_typed_entities_even_when_both_share_one_memory(tmp_path):
    store, memory, coordinator, ids = await facts(tmp_path)
    graph = MemoryGraph(memory)
    drafts = [mention(ids[0]), mention(ids[0], node_type='place', name='Harbor', aliases=[])]
    try:
        await coordinator.commit(lambda: graph.apply(drafts, edges=[{'source_index': 0, 'target_index': 1,
            'relation_type': 'located_at', 'evidence_memory_ids': [ids[0]], 'summary': 'lives in Harbor', 'confidence': .9}],
            origin_model=memory.settings.primary_model, reviewed=True))
        found = graph.query({'mode': 'network', 'entities': ['Alpha', 'Harbor']})
        assert len(found) == 1 and found[0]['id'] == ids[0]
        path = found[0]['entity_path']
        assert len(path['steps']) == 1 and path['steps'][0]['direction'] == 'forward'
        reverse = graph.query({'mode': 'network', 'entities': ['Harbor', 'Alpha']})
        assert reverse[0]['entity_path']['steps'][0]['direction'] == 'reverse'
        assert graph.entity_path_current(path)
        await coordinator.commit(lambda: memory.delete(ids[0], expected_revision=1))
        assert not graph.entity_path_current(path)
    finally:
        store.close()
