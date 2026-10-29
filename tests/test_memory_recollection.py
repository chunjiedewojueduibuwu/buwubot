import json

import pytest
from test_memory import proposal
from test_memory_v2_recall import remembered

from buwubot.models import Completion


def note(mid,revision=1):
    return {'interpretations':[{'text':'可能是在那次服务排查中，我们逐渐确认了访问范围。',
        'source_refs':[{'memory_id':mid,'revision':revision}]}],
        'unknowns':['没有提供当时逐字说法。'],'unknown_refs':[{'memory_id':mid,'revision':revision}]}


async def test_auxiliary_recollection_uses_actual_outline_and_stays_out_of_canonical_memory(tmp_path,monkeypatch):
    store,memory,_,original,summary=await remembered(tmp_path,monkeypatch)
    calls=[]
    class Models:
        async def call(self,role,messages,**kwargs):
            packet=json.loads(messages[-1]['content']);calls.append(packet)
            if 'candidates' not in packet:return Completion('','helper',{'terms':['服务']})
            return Completion('','helper',{'ids':[original],'recollection':note(summary)})
    memory.models=Models()
    stable=['memories','memory_signals','memory_versions','mood_events','memory_signal_events']
    before={name:[tuple(row) for row in store.rows('SELECT * FROM '+name)] for name in stable}
    found=await memory.retrieve('回忆服务的访问范围，记得大概就行',subjects=['22'])
    assert len(calls)==2
    draft=found[0]['recollection']
    assert draft['kind']=='reconstructed_reference'
    assert draft['interpretations'][0]['status']=='inferred'
    assert draft['interpretations'][0]['source_refs']==[{'memory_id':summary,'revision':1}]
    assert draft['unknowns']==['没有提供当时逐字说法。'] and draft['unknown_refs']==[{'memory_id':summary,'revision':1}]
    assert found[0]['recall_representation']['evidence_refs']==[{'memory_id':summary,'revision':1}]
    assert {name:[tuple(row) for row in store.rows('SELECT * FROM '+name)] for name in stable}==before
    store.close()


async def test_requesting_unseen_original_details_does_not_make_them_prior_reconstruction_evidence(tmp_path,monkeypatch):
    store,memory,_,original,_=await remembered(tmp_path,monkeypatch)
    class Models:
        async def call(self,role,messages,**kwargs):
            packet=json.loads(messages[-1]['content'])
            if 'candidates' not in packet:return Completion('','helper',{'terms':['服务']})
            refs=[ref for item in packet['candidates'] for ref in item['recall_representation']['evidence_refs']]
            assert {'memory_id':original,'revision':1} not in refs
            return Completion('','helper',{'ids':[original],'detail_ids':[original],'recollection':note(original)})
    memory.models=Models()
    found=await memory.retrieve('回忆服务访问范围',subjects=['22'])
    assert found[0]['content']==memory.get(original)['content']
    assert 'recollection' not in found[0]
    store.close()


@pytest.mark.parametrize('binding',['missing','stale'])
async def test_partial_invalidation_does_not_export_unbound_unknown_details(tmp_path,monkeypatch,binding):
    store,memory,coordinator,original,summary=await remembered(tmp_path,monkeypatch)
    p=proposal().model_copy(update={'tier':'long_term','significance':.8,'memory_schema_version':2,
        'content':'甲在服务排查中负责确认本机访问。','snapshot_epoch':memory.epoch()})
    stable=await coordinator.commit(lambda:memory.commit(p,reviewed=True))
    draft=note(stable)
    draft['interpretations']+=note(summary)['interpretations']
    draft['unknowns']=['旧摘要相关的具体细节尚不确定。']
    if binding=='missing':draft.pop('unknown_refs')
    else:draft['unknown_refs']=[{'memory_id':summary,'revision':1}]
    class Models:
        async def call(self,role,messages,**kwargs):
            if 'candidates' not in json.loads(messages[-1]['content']):return Completion('','helper',{'terms':['服务']})
            await coordinator.commit(lambda:memory.edit(summary,content='人工已更新要点。',expected_revision=1))
            return Completion('','helper',{'ids':[original,stable],'recollection':draft})
    memory.models=Models()
    found=await memory.retrieve('回忆服务访问范围',subjects=['22'])
    bound=found[0]['recollection']
    assert len(bound['interpretations'])==1
    assert bound['interpretations'][0]['source_refs']==[{'memory_id':stable,'revision':1}]
    assert bound['unknowns']==[]
    store.close()


@pytest.mark.parametrize('revoke',[False,True])
@pytest.mark.parametrize('malformed',[False,True])
async def test_explicit_recollection_intent_gets_focused_assistance_and_rechecks_after_wait(tmp_path,monkeypatch,revoke,malformed):
    store,memory,_,original,summary=await remembered(tmp_path,monkeypatch)
    calls=[]
    class Models:
        async def call(self,role,messages,**kwargs):
            packet=json.loads(messages[-1]['content']);calls.append(packet)
            if len(calls)==1:return Completion('','helper',{'terms':['服务'],'recollection_needed':True})
            if len(calls)==2:return Completion('','helper',{'ids':[original]}|({'recollection':{'interpretations':'bad'}} if malformed else {}))
            assert packet['memories']
            if revoke:store.connection.execute('UPDATE messages SET recalled=1')
            return Completion('','helper',{'recollection':note(summary)})
    memory.models=Models()
    found=await memory.retrieve('回忆服务访问范围',subjects=['22'])
    assert len(calls)==3
    if revoke:assert not found
    else:assert found[0]['recollection']['interpretations'][0]['status']=='inferred'
    store.close()


@pytest.mark.parametrize('reference',['original_instead_of_outline','unselected','wrong_version'])
async def test_recollection_only_uses_material_actually_provided_at_current_version(tmp_path,monkeypatch,reference):
    store,memory,_,original,summary=await remembered(tmp_path,monkeypatch)
    other=next(row['id'] for row in store.rows("SELECT id FROM memories WHERE tier='recent'") if row['id']!=original)
    draft=note(original if reference=='original_instead_of_outline' else other if reference=='unselected' else summary,
               revision=2 if reference=='wrong_version' else 1)
    class Models:
        async def call(self,role,messages,**kwargs):
            if 'candidates' not in json.loads(messages[-1]['content']):return Completion('','helper',{'terms':['服务']})
            return Completion('','helper',{'ids':[original],'recollection':draft})
    memory.models=Models()
    found=await memory.retrieve('回忆服务访问范围',subjects=['22'])
    assert found and 'recollection' not in found[0]
    store.close()


@pytest.mark.parametrize('change',['outline_edit','source_recall','permission'])
async def test_reconstruction_is_invalidated_after_selection_wait(tmp_path,monkeypatch,change):
    store,memory,coordinator,original,summary=await remembered(tmp_path,monkeypatch)
    class Models:
        async def call(self,role,messages,**kwargs):
            if 'candidates' not in json.loads(messages[-1]['content']):return Completion('','helper',{'terms':['服务']})
            if change=='outline_edit':
                await coordinator.commit(lambda:memory.edit(summary,content='新的人工要点。',expected_revision=1))
            elif change=='source_recall':store.connection.execute('UPDATE messages SET recalled=1')
            else:
                cfg,revision=store.settings();store.save_settings(cfg.model_copy(update={'group_id':'999'}),revision)
            return Completion('','helper',{'ids':[original],'recollection':note(summary)})
    memory.models=Models()
    found=await memory.retrieve('回忆服务访问范围',subjects=['22'])
    assert all('recollection' not in item for item in found)
    if change=='outline_edit':assert found[0]['content']==memory.get(original)['content']
    else:assert not found
    store.close()


@pytest.mark.parametrize('invalid',[{'interpretations':'bad'},
    {'interpretations':[{'text':'x','source_refs':[{'memory_id':'x','revision':True}]}]},
    {'interpretations':[{'text':'x'*601,'source_refs':[{'memory_id':'x','revision':1}]}]},
    {'interpretations':[],'chain_of_thought':'do not expose'}])
async def test_malformed_reconstruction_does_not_discard_valid_retrieved_fact(tmp_path,monkeypatch,invalid):
    store,memory,_,original,_=await remembered(tmp_path,monkeypatch)
    class Models:
        async def call(self,role,messages,**kwargs):
            if 'candidates' not in json.loads(messages[-1]['content']):return Completion('','helper',{'terms':['服务']})
            return Completion('','helper',{'ids':[original],'recollection':invalid})
    memory.models=Models()
    found=await memory.retrieve('回忆服务访问范围',subjects=['22'])
    assert found and 'recollection' not in found[0]
    store.close()
