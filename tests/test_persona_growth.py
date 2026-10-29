import time

import pytest
from test_memory import proposal, setup_memory

from buwubot.consolidation import MemoryConsolidation
from buwubot.store import Conflict


async def growth_sources(tmp_path, *, same_event=False, subject='', persona=True):
    store,memory,coordinator=setup_memory(tmp_path)
    ids=[]
    event_start=time.time()-2*86400
    for index in range(2):
        source=memory.archive.get('123:id:1')
        source=source.__class__(**(vars(source)|{'id':f'123:id:{index+10}',
            'message_id':str(index+10),'time':event_start+index*86400,'user_id':'22',
            'text':'助手主动选择学习轨道知识','segments':[{'type':'text','data':{'text':'助手主动选择学习轨道知识'}}]}))
        memory.archive.accept(source,memory.settings)
        ids.append(await coordinator.commit(lambda index=index,source=source:memory.commit(proposal().model_copy(update={
            'subject_id':subject,'persona_id':memory.settings.persona_id if persona else None,
            'tier':'recent','content':f'助手第{index+1}次主动学习轨道知识',
            'event_time':event_start if same_event else source.time,
            'source_message_ids':['123:id:10'] if same_event else [source.id]}),reviewed=True)))
    snapshot=MemoryConsolidation(memory).snapshot(mutation_since=time.time()-60)
    output={'relations':[],'distillations':[{'source_memory_ids':ids,'content':'助手对轨道知识产生了兴趣。',
        'tier':'secondary','tags':[],'confidence':.9,'growth_kind':'interest'}]}
    return store,memory,coordinator,ids,snapshot,output


async def test_growth_distillation_is_core_versioned_and_keeps_originals(tmp_path):
    store,memory,coordinator,ids,snapshot,output=await growth_sources(tmp_path)
    try:
        service=MemoryConsolidation(memory)
        before=[memory.get(mid) for mid in ids]
        plan=service.validate_plan(snapshot,output)
        await coordinator.commit(lambda:service.commit(snapshot,plan.model_dump(),reviewed=True))
        await coordinator.commit(lambda:service.commit(snapshot,plan.model_dump(),reviewed=True))
        rows=store.rows("SELECT id FROM memories WHERE tier='core'")
        assert len(rows)==1
        result=memory.get(rows[0]['id'])
        assert result['persona_id']==memory.settings.persona_id and result['subject_id']==''
        assert 'persona_growth:interest' in result['tags']
        assert [memory.get(mid) for mid in ids]==before
        assert len(store.rows("SELECT id FROM memories WHERE tier='core'"))==1
    finally:store.close()


async def test_growth_review_snapshot_binds_core_and_persona_configuration(tmp_path):
    store,memory,_,_,snapshot,output=await growth_sources(tmp_path)
    try:
        cfg,revision=store.settings()
        store.save_settings(cfg.model_copy(update={'persona':cfg.persona+' 新的人工设定'}),revision)
        with pytest.raises(Conflict):MemoryConsolidation(memory).validate_plan(snapshot,output)
    finally:store.close()


async def test_growth_context_is_scoped_version_bound_and_user_deletable(tmp_path):
    store,memory,coordinator,ids,snapshot,output=await growth_sources(tmp_path)
    try:
        await coordinator.commit(lambda:MemoryConsolidation(memory).commit(snapshot,output,reviewed=True))
        context=memory.growth_context()
        assert len(context)==1 and context[0]['kind']=='interest'
        assert context[0]['content']=='助手对轨道知识产生了兴趣。'
        from copy import copy
        other=copy(memory);other.settings=memory.settings.model_copy(update={'group_id':'999'})
        assert other.core_context()==memory.core_context()  # shared personal traits, never raw private events
        await coordinator.commit(lambda:memory.edit(ids[0],content='人工纠正，尚未开始学习',expected_revision=1))
        assert memory.growth_context()==[]
        assert context[0]['content'] not in memory.core_context()
    finally:store.close()


async def test_growth_has_cooldown_and_does_not_accept_direct_marker_injection(tmp_path):
    store,memory,coordinator,_ids,snapshot,output=await growth_sources(tmp_path)
    try:
        await coordinator.commit(lambda:MemoryConsolidation(memory).commit(snapshot,output,reviewed=True))
        assert 'interest' in memory.growth_cooldowns()
        # Even a re-signed snapshot cannot bypass the commit-side cooldown.
        replay=snapshot|{'fingerprint':'','protected_core':memory.core_context(),
            'core_records':[dict(row) for row in store.rows("SELECT id,revision,content FROM memories WHERE tier='core' AND deleted=0")]}
        replay['fingerprint']=MemoryConsolidation.fingerprint(replay)
        changed={'relations':[],'distillations':[output['distillations'][0]|{'content':'助手现在喜欢研究轨道。'}]}
        with pytest.raises(ValueError,match='cooldown'):
            MemoryConsolidation(memory).validate_plan(replay,changed)
        with pytest.raises(ValueError,match='growth'):
            await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={
                'tags':['persona_growth:habit'],'content':'助手的新习惯','subject_id':'',
                'persona_id':memory.settings.persona_id}),reviewed=True))
    finally:store.close()


async def test_growth_user_and_reviewed_updates_preserve_existing_lineage(tmp_path):
    store,memory,coordinator,_,snapshot,output=await growth_sources(tmp_path)
    try:
        await coordinator.commit(lambda:MemoryConsolidation(memory).commit(snapshot,output,reviewed=True))
        mid=memory.growth_context()[0]['id']
        await coordinator.commit(lambda:memory.edit(mid,content='助手目前喜欢研究航天轨道。',expected_revision=1))
        assert memory.growth_context()[0]['revision']==2
    finally:store.close()


async def test_same_kind_growth_batch_is_rejected_before_review_and_write(tmp_path):
    store,memory,_,_,snapshot,output=await growth_sources(tmp_path)
    output['distillations'].append(output['distillations'][0]|{'content':'助手开始关注航天。'})
    try:
        with pytest.raises(ValueError,match='growth'):
            MemoryConsolidation(memory).validate_plan(snapshot,output)
    finally:store.close()


@pytest.mark.parametrize('fault',['same_event','other_subject','unscoped','unknown_kind','reserved_tag'])
async def test_growth_cannot_relabel_one_event_or_another_person_as_personality(tmp_path,fault):
    store,memory,_,ids,snapshot,output=await growth_sources(tmp_path,
        same_event=fault=='same_event',subject='22' if fault=='other_subject' else '',persona=fault!='unscoped')
    if fault=='unknown_kind':output['distillations'][0]['growth_kind']='core_identity'
    if fault=='reserved_tag':
        output['distillations'][0].pop('growth_kind')
        output['distillations'][0]['tags']=['persona_growth:interest']
    try:
        with pytest.raises(ValueError):MemoryConsolidation(memory).validate_plan(snapshot,output)
        assert len(store.rows('SELECT id FROM memories'))==len(ids)
    finally:store.close()
