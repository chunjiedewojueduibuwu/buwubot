import hashlib
import json

import pytest
from test_consolidation import engine_pair, result

from buwubot.consolidation import MemoryConsolidation
from buwubot.memory import ReviewRequired
from buwubot.models import Completion


@pytest.mark.parametrize('failure', ['structure', 'semantic'])
async def test_bad_plan_is_returned_once_before_any_formal_write(tmp_path, failure):
    store, engine, qq, models, ids = await engine_pair(tmp_path)
    calls = []
    attempts = 0
    async def call(role, messages, **kwargs):
        nonlocal attempts
        calls.append(role)
        data = json.loads(messages[-1]['content'])
        if role == 'auxiliary':
            if attempts:
                assert data['review_feedback']['stage'] == failure
            return Completion('', 'helper', {'relations': []})
        if role == 'memory':
            attempts += 1
            if attempts > 1:
                assert data['review_feedback']['stage'] == failure
                return Completion('', engine.settings.primary_model, {'relations': []})
            output = result(ids)
            if failure == 'structure':
                output['relations'][0]['target_id'] = 'not-in-snapshot'
            return Completion('', engine.settings.primary_model, output)
        return Completion('', 'checker', {'approved': False, 'reason': '与原来源不一致'})
    models.call = call
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    try:
        await engine.process(engine.queue.get(jid))
        saved = engine.queue.get(jid)
        assert saved['state'] == 'pending'
        assert saved['payload']['review_feedback']['stage'] == failure
        assert 'result' not in saved['payload'] and 'prepared_candidates' not in saved['payload']
        assert not store.rows('SELECT * FROM memory_consolidation_seen')
        assert not store.rows('SELECT * FROM memory_relations')
        assert calls == (['auxiliary', 'memory'] if failure == 'structure' else ['auxiliary', 'memory', 'review'])
        await engine.process(saved)
        assert engine.queue.get(jid)['state'] == 'succeeded'
        assert calls[-2:] == ['auxiliary', 'memory']
        assert qq.sent == []
    finally:
        store.close()


async def test_second_rejection_retains_material_for_attention_instead_of_replanning_forever(tmp_path):
    store, engine, _, models, ids = await engine_pair(tmp_path)
    async def call(role, *args, **kwargs):
        return Completion('', engine.settings.primary_model if role == 'memory' else 'checker',
            result(ids) if role in {'auxiliary', 'memory'} else {'approved': False, 'reason': '仍无依据'})
    models.call = call
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    try:
        await engine.process(engine.queue.get(jid))
        with pytest.raises(ReviewRequired):
            await engine.process(engine.queue.get(jid))
        payload = engine.queue.get(jid)['payload']
        assert 'result' in payload and 'review_feedback' in payload
        assert not store.rows('SELECT * FROM memory_consolidation_seen')
    finally:
        store.close()


async def test_preflight_is_read_only_and_rejects_nonfocus_relations(tmp_path):
    store, engine, _, _, ids = await engine_pair(tmp_path)
    service = MemoryConsolidation(engine.memory)
    snapshot = service.snapshot()
    snapshot['focus_ids'] = []
    snapshot['fingerprint'] = service.fingerprint(snapshot)
    try:
        before = store.connection.total_changes
        packet = service.model_context(snapshot)
        assert all(item['focus'] is False for item in packet['records'])
        assert all('focus' not in item for item in snapshot['records'])
        with pytest.raises(ValueError):
            service.validate_plan(snapshot, result(ids))
        assert store.connection.total_changes == before
        assert not store.rows('SELECT * FROM memory_relations')
    finally:
        store.close()


async def test_preflight_reports_all_invalid_operations_without_writes(tmp_path):
    store, engine, _, _, ids = await engine_pair(tmp_path)
    service = MemoryConsolidation(engine.memory)
    snapshot = service.snapshot()
    snapshot['focus_ids'] = [ids[0]]
    snapshot['fingerprint'] = service.fingerprint(snapshot)
    output = {'relations': [{'source_id': ids[0], 'target_id': 'missing',
        'kind': 'related', 'summary': 'invalid relation'}], 'distillations': [
        {'source_memory_ids': [ids[1]], 'content': 'no current focus',
         'tier': 'secondary', 'confidence': .9},
        {'source_memory_ids': ['missing'], 'content': 'not in snapshot',
         'tier': 'secondary', 'confidence': .9}]}
    try:
        before = store.connection.total_changes
        with pytest.raises(ValueError) as error:
            service.validate_plan(snapshot, output)
        message = str(error.value)
        assert 'relations[0]' in message
        assert 'distillations[0]' in message and 'distillations[1]' in message
        assert 'focus' in message
        assert store.connection.total_changes == before
        assert not store.rows('SELECT * FROM memory_consolidation_seen')
    finally:
        store.close()


def test_model_context_groups_distillation_by_exact_identity_and_current_focus():
    def record(mid, subject):
        return {'id': mid, 'group_id': 'g', 'subject_id': subject, 'persona_id': 'p'}
    snapshot = {'focus_ids': ['a'], 'mutable_ids': ['a', 'b', 'c'], 'mergeable_ids': [],
        'records': [record('a', 'owner'), record('b', 'owner'), record('c', '')]}
    packet = MemoryConsolidation.model_context(snapshot)
    assert packet['distillation_groups'] == [['a', 'b']]
    snapshot['focus_ids'].append('c')
    packet = MemoryConsolidation.model_context(snapshot)
    assert packet['distillation_groups'] == [['a', 'b'], ['c']]
    assert 'distillation_groups' not in snapshot


def test_context_sources_are_not_advertised_as_independent_operation_roots():
    def record(mid):
        return {'id': mid, 'group_id': 'g', 'subject_id': '22', 'persona_id': 'p'}
    snapshot = {'focus_ids': ['a'], 'mutable_ids': ['a', 'b'], 'mergeable_ids': ['a', 'b'],
                'records': [record('a'), record('b')]}
    packet = MemoryConsolidation.model_context(snapshot)
    assert packet['operation_roots']['distillation'] == ['a']
    rows = {item['id']: item for item in packet['records']}
    assert rows['a']['can_distill'] is True
    assert rows['b']['can_distill'] is False
    assert rows['b']['can_support_distillation'] is True
    assert packet['distillation_groups'] == [['a', 'b']]


def test_consolidation_model_projection_shows_original_local_time_without_mutating_snapshot():
    record = {'id':'a','group_id':'g','subject_id':'22','persona_id':'p','event_time':1792926000}
    snapshot = {'focus_ids':['a'],'mutable_ids':['a'],'mergeable_ids':[], 'records':[record]}
    context = MemoryConsolidation.model_context(snapshot)
    assert context['records'][0]['local_time'].startswith('2026-10-25T')
    assert 'local_time' not in record


def test_review_material_binds_each_distillation_to_its_declared_sources_only():
    records = [{'id':mid,'group_id':'g','subject_id':'22','persona_id':'p','content':text}
               for mid,text in [('a','asked a question'),('b','unrelated extra claim')]]
    snapshot = {'focus_ids':['a'],'mutable_ids':['a','b'],'mergeable_ids':[],'records':records}
    output = {'relations':[], 'distillations':[{'source_memory_ids':['a'],
        'content':'asked a question','tier':'secondary','confidence':.9}]}
    material = MemoryConsolidation.review_context(snapshot, output)
    bound = material['operation_evidence']['distillations'][0]['records']
    assert [item['id'] for item in bound] == ['a']
    assert 'unrelated extra claim' not in json.dumps(bound)


async def test_preflight_identifies_source_reused_by_merge_and_distillation(tmp_path):
    from test_memory_merge import duplicates, merged
    store, memory, _, ids = await duplicates(tmp_path)
    service = MemoryConsolidation(memory)
    output = merged(ids)
    output['distillations'] = [{'source_memory_ids':[ids[0]],'content':'a distilled fact',
                                'tier':'secondary','confidence':.9}]
    try:
        with pytest.raises(ValueError) as error:
            service.validate_plan(service.snapshot(mutation_since=0), output)
        assert 'overlap' in str(error.value) and ids[0] in str(error.value)
    finally:
        store.close()


async def test_review_batches_checkpoint_approved_units_and_commit_only_after_all(tmp_path):
    from buwubot.coordinator import WorkPaused
    store, engine, _, models, ids = await engine_pair(tmp_path)
    review_calls = 0
    output = {'relations':[result(ids)['relations'][0] | {'summary':f'evidence summary {n}'} for n in range(5)]}
    async def call(role, messages, **kwargs):
        nonlocal review_calls
        if role in {'auxiliary','memory'}:
            return Completion('', engine.settings.primary_model if role=='memory' else 'helper', output)
        data=json.loads(messages[-1]['content'])
        assert len(data['proposal']['relations']) <= 4
        assert len(data['snapshot']['records']) <= 2
        review_calls += 1
        assert not store.rows('SELECT * FROM memory_consolidation_seen')
        if review_calls == 2:
            raise WorkPaused('test interruption')
        return Completion('', 'checker', {'approved':True})
    models.call=call
    engine.enqueue_consolidations()
    jid=store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    try:
        with pytest.raises(WorkPaused):
            await engine.process(engine.queue.get(jid))
        assert len(engine.queue.get(jid)['payload']['reviewed_batches']) == 1
        await engine.process(engine.queue.get(jid))
        assert review_calls == 3
        assert engine.queue.get(jid)['state'] == 'succeeded'
    finally:
        store.close()


def test_merge_groups_keep_scope_identity_and_all_original_records():
    def record(mid, group='g', subject='22', persona=None):
        return {'id':mid,'group_id':group,'subject_id':subject,'persona_id':persona}
    records = [record('a'),record('b'),record('public',subject=''),
               record('other',subject='33'),record('persona',persona='p'),record('group',group='h')]
    snapshot = {'focus_ids':['a','public'], 'mutable_ids':['a'],
                'mergeable_ids':[r['id'] for r in records], 'records':records}
    material = MemoryConsolidation.model_context(snapshot)
    assert material['merge_groups'] == [['a','b'],['public']]
    assert material['records'] and len(material['records']) == len(records)
    assert 'merge_groups' not in snapshot


@pytest.mark.parametrize('fully_reviewed', [False, True])
async def test_old_review_policy_receipts_cannot_authorize_a_pending_commit(tmp_path, fully_reviewed):
    store, engine, _, models, ids = await engine_pair(tmp_path)
    engine.enqueue_consolidations()
    jid = store.rows("SELECT id FROM jobs WHERE kind='consolidate'")[0]['id']
    payload = engine.queue.get(jid)['payload']
    output = result(ids)
    packet = next(MemoryConsolidation.review_batches(payload['snapshot'],output))
    old_receipt = hashlib.sha256(json.dumps(packet,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    payload.update(result=output,origin_model=engine.settings.primary_model,
                   reviewed_batches=[old_receipt])
    if fully_reviewed:
        payload.update(reviewed=True,review_policy='obsolete-policy')
    engine.queue.payload(jid,payload)
    calls = []
    async def call(role, *args, **kwargs):
        calls.append(role)
        return Completion('', 'checker', {'approved':False,'reason':'new rules reject old approval'})
    models.call = call
    try:
        await engine.process(engine.queue.get(jid))
        assert calls == ['review']
        assert engine.queue.get(jid)['payload']['review_feedback']['stage'] == 'semantic'
        assert not store.rows('SELECT * FROM memory_relations')
        assert not store.rows('SELECT * FROM memory_consolidation_seen')
    finally:
        store.close()
