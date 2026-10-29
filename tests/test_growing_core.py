import hashlib
import json
import time

import pytest
from test_persona_growth import growth_sources

from buwubot.consolidation import MemoryConsolidation
from buwubot.store import Conflict


async def test_growth_enters_shared_core_and_source_correction_invalidates_it(tmp_path):
    store,memory,coordinator,ids,snapshot,output=await growth_sources(tmp_path)
    try:
        service=MemoryConsolidation(memory)
        await coordinator.commit(lambda:service.commit(snapshot,output,reviewed=True))
        rows=store.rows("SELECT id FROM memories WHERE tier='core'")
        assert len(rows)==1
        item=memory.get(rows[0]['id'])
        assert item['group_id']=='persona:core:assistant'
        assert item['content'] in memory.core_context()
        assert all(memory.get(mid)['tier']=='recent' for mid in ids)
        await coordinator.commit(lambda:memory.edit(ids[0],content='纠正：没有实际学习',expected_revision=1))
        assert item['content'] not in memory.core_context()
    finally:store.close()


async def test_same_text_core_revision_change_expires_snapshot(tmp_path):
    from buwubot.memory import MemoryProposal, core_scope
    store,memory,coordinator,_,_,output=await growth_sources(tmp_path)
    try:
        mid=await coordinator.commit(lambda:memory.commit(MemoryProposal(group_id=core_scope('assistant'),
            persona_id='assistant',tier='core',content='助手的场景参照',source_message_ids=['manual:scene']),
            manual=True,reviewed=True,source_snapshots=[{'id':'manual:scene','group_id':core_scope('assistant')}]))
        service=MemoryConsolidation(memory);snapshot=service.snapshot(mutation_since=0)
        await coordinator.commit(lambda:memory.edit(mid,content='助手的场景参照',tags=['人工核对'],expected_revision=1))
        with pytest.raises(Conflict):service.validate_plan(snapshot,output)
    finally:store.close()


async def test_fresh_growth_evidence_can_repair_a_stale_core_entry(tmp_path):
    from test_memory import proposal
    store,memory,coordinator,ids,snapshot,output=await growth_sources(tmp_path)
    try:
        service=MemoryConsolidation(memory)
        await coordinator.commit(lambda:service.commit(snapshot,output,reviewed=True))
        mid=memory.growth_context()[0]['id']
        await coordinator.commit(lambda:memory.edit(ids[0],content='旧来源有误',expected_revision=1))
        fresh=[]
        for index in range(2):
            old=memory.archive.get('123:id:10')
            event=time.time()-3600+index*1800
            message=old.__class__(**(vars(old)|{'id':f'123:id:{40+index}','message_id':str(40+index),'time':event}))
            memory.archive.accept(message,memory.settings)
            fresh.append(await coordinator.commit(lambda message=message,event=event,index=index:memory.commit(
                proposal().model_copy(update={'subject_id':'','persona_id':'assistant','tier':'recent',
                    'content':f'新的主动学习经历{index}','event_time':event,'source_message_ids':[message.id]}),reviewed=True)))
        await coordinator.commit(lambda:store.connection.execute('UPDATE memories SET updated_at=? WHERE id=?',(time.time()-90000,mid)))
        latest=service.snapshot(mutation_since=0)
        draft=output['distillations'][0]|{'source_memory_ids':fresh,'target_core_id':mid,'content':'助手重新形成了对轨道的兴趣。'}
        await coordinator.commit(lambda:service.commit(latest,{'relations':[],'distillations':[draft]},reviewed=True))
        assert memory.get(mid)['revision']==2
        assert '助手重新形成了对轨道的兴趣。' in memory.core_context()
    finally:store.close()


async def test_legacy_distillation_receipt_prevents_replaying_old_events_into_core(tmp_path):
    store,memory,coordinator,_,snapshot,output=await growth_sources(tmp_path)
    try:
        from buwubot.consolidation import ConsolidationPlan
        plan=ConsolidationPlan.parse_material(output).model_dump()
        for item in plan['distillations']:
            item.pop('significance',None)  # This fixture is an actual pre-V2 serialized receipt.
            item.pop('target_core_id',None)
            item.pop('teaching_scope',None)
            item.pop('teaching_source_ids',None)
        operation=hashlib.sha256(json.dumps([snapshot['fingerprint'],plan],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        await coordinator.commit(lambda:store.connection.execute('INSERT INTO memory_consolidation_applied VALUES(?,?)',(operation,time.time())))
        store.set_state('memory_schema_version',2)
        await coordinator.commit(lambda:MemoryConsolidation(memory).commit(snapshot,output,reviewed=True))
        assert store.rows("SELECT id FROM memories WHERE tier='core'")==[]
    finally:store.close()


async def test_growth_can_update_an_existing_core_entry_but_not_fixed_persona(tmp_path):
    from buwubot.memory import MemoryProposal, core_scope
    store,memory,coordinator,_,_,output=await growth_sources(tmp_path)
    try:
        mid=await coordinator.commit(lambda:memory.commit(MemoryProposal(group_id=core_scope('assistant'),
            persona_id='assistant',tier='core',content='助手暂时对轨道不熟悉。',
            source_message_ids=['manual:core']),manual=True,reviewed=True,
            source_snapshots=[{'id':'manual:core','group_id':core_scope('assistant')}]))
        snapshot=MemoryConsolidation(memory).snapshot(mutation_since=0)
        output['distillations'][0]['target_core_id']=mid
        before=store.settings()[0].persona
        await coordinator.commit(lambda:MemoryConsolidation(memory).commit(snapshot,output,reviewed=True))
        assert memory.get(mid)['revision']==2
        assert memory.get(mid)['content']=='助手对轨道知识产生了兴趣。'
        assert store.settings()[0].persona==before
        assert len(store.rows("SELECT id FROM memories WHERE tier='core'"))==1
    finally:store.close()
