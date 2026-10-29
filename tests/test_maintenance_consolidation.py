import json

import pytest
from test_memory_maintenance import Reviewer
from test_memory_merge import duplicates, merged

from buwubot.memory_maintenance import MemoryMaintenance
from buwubot.models import Completion
from buwubot.store import Conflict


class ConsolidationReviewer(Reviewer):
    async def call(self,role,messages,**kwargs):
        packet=json.loads(messages[-1]['content'])
        if 'review_unit' not in packet:
            return await super().call(role,messages,**kwargs)
        assert role=='review'
        self.calls.append(packet)
        return Completion('','independent-helper',{'approved':self.approve,'reason':'来源和层级相符'})


async def fixture(tmp_path,kind='merge'):
    store,memory,coordinator,ids=await duplicates(tmp_path)
    cfg,revision=store.settings()
    revision=store.save_settings(cfg.model_copy(update={'runtime_enabled':False}),revision)
    proposal=merged(ids) if kind=='merge' else {'relations':[],'distillations':[{
        'source_memory_ids':ids,'content':'甲较偏爱茶类饮品','tier':'secondary','tags':['饮食'],'confidence':.9}]}
    plan={'config_revision':revision,'persona_id':cfg.persona_id,'changes':[],
          'consolidations':[{'group_id':'123','records':[{'memory_id':mid,'expected_revision':1} for mid in ids],
                             'proposal':proposal}]}
    return store,memory,coordinator,MemoryMaintenance(memory),plan,ids


@pytest.mark.parametrize('kind',['merge','distill'])
async def test_maintenance_reuses_reviewed_merge_and_distillation_without_changing_core(tmp_path,kind):
    store,memory,coordinator,service,plan,ids=await fixture(tmp_path,kind)
    try:
        before=[memory.get(mid) for mid in ids]
        preview=service.preview(plan)
        assert len(preview['consolidations'])==1
        models=ConsolidationReviewer()
        approved=await service.review(plan,models,coordinator)
        assert approved['status']=='ready' and models.calls
        result=await coordinator.commit(lambda:service.apply(approved['token']))
        assert result['consolidated'][0]['group_id']=='123'
        if kind=='merge':
            assert memory.get(ids[1]) is None
            assert memory.get(ids[0])['source_message_ids']==['123:id:1','123:id:2']
            assert len(store.rows('SELECT * FROM memory_merge_recalls'))==1
            assert not memory.lifecycle.state(memory.get(ids[0]))['protected']
        else:
            assert [memory.get(mid) for mid in ids]==before
            assert len(store.rows('SELECT * FROM memory_distillations'))==2
            assert len(store.rows('SELECT * FROM memories WHERE deleted=0'))==3
            for mid in ids:
                assert memory.with_reading_policy(memory.get(mid))['memory_half_life_minutes']==14*1440
        assert await coordinator.commit(lambda:service.apply(approved['token']))==result
        assert not store.settings()[0].runtime_enabled
    finally:
        store.close()


@pytest.mark.parametrize('fault',['revision','cross_scope','overlap','duplicate_scope','empty'])
async def test_invalid_consolidation_import_never_reaches_review_or_mutation(tmp_path,fault):
    store,memory,_coordinator,service,plan,ids=await fixture(tmp_path)
    try:
        group=plan['consolidations'][0]
        if fault=='revision':group['records'][0]['expected_revision']=2
        elif fault=='cross_scope':group['group_id']='999'
        elif fault=='overlap':plan['changes']=[{'memory_id':ids[0],'expected_revision':1,
            'content':memory.get(ids[0])['content'],'tier':'important','tags':['饮食']}]
        elif fault=='duplicate_scope':plan['consolidations'].append(group.copy())
        else:group['proposal']={'relations':[]}
        with pytest.raises((ValueError,Conflict)):service.preview(plan)
        assert all(memory.get(mid)['revision']==1 for mid in ids)
    finally:
        store.close()


async def test_failed_consolidation_rolls_back_prior_changes_and_tombstones(tmp_path,monkeypatch):
    from test_memory import proposal

    from buwubot.consolidation import MemoryConsolidation

    store,memory,coordinator,service,plan,ids=await fixture(tmp_path)
    try:
        third=await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={
            'content':'甲今天提到排队','tier':'secondary'})))
        plan['changes']=[{'memory_id':third,'expected_revision':1,'content':'甲今天提到排队',
                         'tier':'recent','tags':['日常']}]
        approved=await service.review(plan,ConsolidationReviewer(),coordinator)
        commit=MemoryConsolidation.commit
        def fail(self,*args,**kwargs):
            commit(self,*args,**kwargs)
            raise OSError('fixture failure after merge')
        monkeypatch.setattr(MemoryConsolidation,'commit',fail)
        with pytest.raises(OSError):
            await coordinator.commit(lambda:service.apply(approved['token']))
        assert all(memory.get(mid)['revision']==1 for mid in [*ids,third])
        assert not store.rows('SELECT * FROM memory_versions')
        assert not store.rows('SELECT * FROM memory_merges')
        assert not store.rows('SELECT * FROM memory_maintenance_guards')
        assert service.get(approved['token'])['status']=='ready'
    finally:
        store.close()
