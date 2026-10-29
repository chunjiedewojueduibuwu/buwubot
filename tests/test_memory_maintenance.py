import json

import pytest
from test_memory import proposal, setup_memory

from buwubot.models import Completion
from buwubot.store import Conflict


class Reviewer:
    def __init__(self, *, approve=True, invalid=False, after=None):
        self.calls=[]
        self.approve,self.invalid,self.after=approve,invalid,after

    async def call(self,role,messages,**kwargs):
        assert role=='review'
        packet=json.loads(messages[-1]['content'])
        self.calls.append(packet)
        if self.after:
            await self.after()
        data={'checks':[{'memory_id':item['before']['id'],'approved':self.approve,
                         'reason':'与原事实一致'} for item in packet['changes']]}
        return Completion('','independent-helper',{'approved':True} if self.invalid else data)


async def test_tier_only_maintenance_preserves_original_whitespace_in_content_and_tags(tmp_path):
    from buwubot.memory_maintenance import MemoryMaintenance

    store,memory,coordinator=setup_memory(tmp_path)
    text='  甲喜欢茶，不喜欢咖啡\n'
    tags=[' 群友称呼 ', '饮品']
    try:
        mid=await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={
            'content':text,'tags':tags})))
        cfg,revision=store.settings()
        revision=store.save_settings(cfg.model_copy(update={'runtime_enabled':False}),revision)
        service=MemoryMaintenance(memory)
        plan={'config_revision':revision,'persona_id':cfg.persona_id,'changes':[{
            'memory_id':mid,'expected_revision':1,'content':text,'tags':tags,'tier':'recent'}]}
        preview=service.preview(plan)
        assert preview['changes'][0]['after']['content']==text
        assert preview['changes'][0]['after']['tags']==tags
        assert preview['changes'][0]['retention_only'] is True
        rewritten=plan|{'changes':[plan['changes'][0]|{'content':'甲喜欢茶。'}]}
        assert service.preview(rewritten)['changes'][0]['retention_only'] is False
        reviewed=await service.review(plan,Reviewer(),coordinator)
        await coordinator.commit(lambda:service.apply(reviewed['token']))
        assert memory.get(mid)['content']==text and memory.get(mid)['tags']==tags
    finally:
        store.close()


async def test_maintenance_keeps_recent_original_instead_of_promoting_it_in_place(tmp_path):
    from buwubot.memory_maintenance import MemoryMaintenance

    store,memory,_coordinator,_service,plan,mid=await prepared(tmp_path)
    try:
        store.connection.execute("UPDATE memories SET tier='recent' WHERE id=?",(mid,))
        with pytest.raises(Conflict,match='近期'):
            MemoryMaintenance(memory).preview(plan)
    finally:
        store.close()


async def test_preview_includes_completed_same_scope_forward_reading_without_media_urls(tmp_path):
    store,_memory,_coordinator,service,plan,_mid=await prepared(tmp_path)
    try:
        from buwubot.coordinator import JobQueue

        queue=JobQueue(store)
        for group,source,label in [('999','123:id:1','wrong scope'),('123','123:id:1','相亲对象①号')]:
            payload={'group_id':group,'source_id':source,'offset':1,'notes':[{
                'source_id':source,'position':'forward:0:0','asset_hash':'abc',
                'analysis':{'visible_text':label,'description':'梗图'},'url':'https://private.invalid/secret'}]}
            jid=queue.enqueue('forward-images',payload,dedupe_key=group,priority=1,source_time=100)
            queue.finish(jid,'succeeded')
        source=service.preview(plan)['changes'][0]['sources'][0]
        assert source['forward_reading']['images_read']==1
        assert source['forward_reading']['notes'][0]['analysis']['visible_text']=='相亲对象①号'
        assert 'private.invalid' not in json.dumps(source)
    finally:
        store.close()


async def test_restoring_recent_original_exposes_verified_history_without_inventing_evidence(tmp_path):
    store,memory,_coordinator,service,plan,mid=await prepared(tmp_path)
    try:
        before=memory.get(mid)
        prior=before|{'tier':'recent'}
        store.connection.execute('INSERT INTO memory_versions VALUES(?,?,?)',(mid,1,json.dumps(prior)))
        store.connection.execute("UPDATE memories SET tier='important',revision=2 WHERE id=?",(mid,))
        plan['changes'][0].update(expected_revision=2,tier='recent')
        assert service.preview(plan)['changes'][0]['preserved_recent_version']=={'revision':1,'tier':'recent'}
        prior['source_message_ids']=['another-source']
        store.connection.execute('UPDATE memory_versions SET payload=? WHERE memory_id=?',(json.dumps(prior),mid))
        assert 'preserved_recent_version' not in service.preview(plan)['changes'][0]
    finally:
        store.close()


@pytest.mark.parametrize('group,status,available',[('123','sent',True),('999','sent',False),('123','uncertain',False)])
async def test_maintenance_reads_only_confirmed_same_scope_outgoing_source(tmp_path,group,status,available):
    store,memory,_coordinator,service,_plan,mid=await prepared(tmp_path)
    try:
        from buwubot.coordinator import JobQueue

        jid=JobQueue(store).enqueue('reply',{'group_id':group},dedupe_key='out-source',priority=1,source_time=100)
        store.connection.execute('INSERT INTO outgoing(job_id,group_id,segments,started_at,status) VALUES(?,?,?,?,?)',
            (jid,group,json.dumps([{'type':'text','data':{'text':'我记住这个称呼了'}}]),100,status))
        item=memory.get(mid)|{'source_message_ids':['outgoing:'+jid]}
        source=service._sources(item)[0]
        assert source['available']==available
        if available:
            assert source['text']=='我记住这个称呼了' and source['origin']=='confirmed_outgoing'
        else:
            assert 'text' not in source
    finally:
        store.close()


async def prepared(tmp_path):
    from buwubot.memory_maintenance import MemoryMaintenance

    store,memory,coordinator=setup_memory(tmp_path)
    mid=await coordinator.commit(lambda:memory.commit(proposal()))
    cfg,revision=store.settings()
    revision=store.save_settings(cfg.model_copy(update={'runtime_enabled':False}),revision)
    service=MemoryMaintenance(memory)
    plan={'config_revision':revision,'persona_id':cfg.persona_id,
          'changes':[{'memory_id':mid,'expected_revision':1,'content':'甲喜欢茶，不喜欢咖啡',
                      'tier':'important','tags':['饮食']} ]}
    return store,memory,coordinator,service,plan,mid


async def test_preview_is_readonly_and_reviewed_apply_is_atomic_idempotent_and_not_manual(tmp_path):
    store,memory,coordinator,service,plan,mid=await prepared(tmp_path)
    try:
        before=memory.get(mid)
        states=store.rows('SELECT * FROM state')
        preview=service.preview(plan)
        assert preview['changes'][0]['before']['tier']=='secondary'
        assert preview['changes'][0]['sources'][0]['text']=='我喜欢茶，不喜欢咖啡'
        assert memory.get(mid)==before and store.rows('SELECT * FROM state')==states
        with pytest.raises(RuntimeError):
            service.apply(preview['token'])
        with pytest.raises(Conflict):
            await coordinator.commit(lambda:service.apply(preview['token']))
        reviewed=await service.review(plan,Reviewer(),coordinator)
        assert reviewed['status']=='ready' and memory.get(mid)==before
        result=await coordinator.commit(lambda:service.apply(reviewed['token']))
        assert result['status']=='applied' and memory.get(mid)['tier']=='important'
        assert memory.get(mid)['revision']==2 and memory.get(mid)['event_time']==before['event_time']
        assert not memory.lifecycle.state(memory.get(mid))['protected']
        assert not store.rows('SELECT * FROM memory_manual_guards WHERE memory_id=?',(mid,))
        assert store.rows('SELECT * FROM memory_versions WHERE memory_id=?',(mid,))
        assert list((tmp_path/'backups').glob('maintenance-*.db'))
        assert await coordinator.commit(lambda:service.apply(reviewed['token']))==result
        assert memory.get(mid)['revision']==2
    finally:
        store.close()


@pytest.mark.parametrize('change',['revision','resume','manual','scope','persona'])
async def test_maintenance_rejects_stale_or_out_of_scope_plan_without_writes(tmp_path,change):
    store,memory,coordinator,service,plan,mid=await prepared(tmp_path)
    try:
        if change=='revision':plan['changes'][0]['expected_revision']=2
        elif change=='resume':
            cfg,revision=store.settings();store.save_settings(cfg.model_copy(update={'runtime_enabled':True}),revision)
        elif change=='manual':
            await coordinator.commit(lambda:memory.edit(mid,content='甲喜欢红茶',expected_revision=1))
        elif change=='scope':
            cfg,revision=store.settings();plan['config_revision']=store.save_settings(cfg.model_copy(update={'group_id':'999'}),revision)
        else:plan['persona_id']='another'
        before=memory.get(mid)
        reviewer=Reviewer()
        with pytest.raises(Conflict):await service.review(plan,reviewer,coordinator)
        assert not reviewer.calls and memory.get(mid)==before
    finally:
        store.close()


@pytest.mark.parametrize('invalid',[False,True])
async def test_rejected_or_malformed_review_never_authorizes_apply_and_retains_evidence(tmp_path,invalid):
    store,memory,coordinator,service,plan,mid=await prepared(tmp_path)
    try:
        preview=service.preview(plan)
        if invalid:
            with pytest.raises(ValueError):await service.review(plan,Reviewer(invalid=True),coordinator)
        else:
            result=await service.review(plan,Reviewer(approve=False),coordinator)
            assert result['status']=='rejected'
        assert service.get(preview['token'])['status'] in {'rejected','failed'}
        if invalid:
            assert json.loads(service.get(preview['token'])['failed_output'])=={'approved':True}
        with pytest.raises(Conflict):
            await coordinator.commit(lambda:service.apply(preview['token']))
        assert memory.get(mid)['revision']==1
    finally:
        store.close()


async def test_change_during_review_does_not_create_ready_receipt(tmp_path):
    store,memory,coordinator,service,plan,mid=await prepared(tmp_path)
    async def edit():
        await coordinator.commit(lambda:memory.edit(mid,content='甲喜欢红茶',expected_revision=1))
    try:
        token=service.preview(plan)['token']
        with pytest.raises(Conflict):await service.review(plan,Reviewer(after=edit),coordinator)
        assert service.get(token)['status']=='failed'
        assert memory.get(mid)['content']=='甲喜欢红茶'
    finally:
        store.close()


async def test_middle_commit_failure_rolls_back_versions_indexes_and_all_changes(tmp_path):
    store,memory,coordinator,service,plan,mid=await prepared(tmp_path)
    try:
        second=await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={'content':'甲今天下载了游戏'})))
        plan['changes'].append({'memory_id':second,'expected_revision':1,'content':'甲今天下载了游戏',
                                'tier':'recent','tags':['游戏']})
        token=(await service.review(plan,Reviewer(),coordinator))['token']
        outbox=len(store.rows('SELECT * FROM index_outbox'))
        original=memory.commit
        calls=[]
        def fail(proposal,**kwargs):
            calls.append(proposal.memory_id)
            if len(calls)==2:raise OSError('disk fixture')
            return original(proposal,**kwargs)
        memory.commit=fail
        with pytest.raises(OSError):await coordinator.commit(lambda:service.apply(token))
        assert memory.get(mid)['revision']==1 and memory.get(second)['revision']==1
        assert not store.rows('SELECT * FROM memory_versions')
        assert len(store.rows('SELECT * FROM index_outbox'))==outbox
        assert service.get(token)['status']=='ready'
    finally:
        store.close()


@pytest.mark.parametrize('tier',['core','feeling'])
async def test_core_and_emotion_anchors_cannot_be_reclassified(tmp_path,tier):
    from buwubot.memory import MemoryProposal, core_scope

    store,memory,coordinator,service,plan,_=await prepared(tmp_path)
    try:
        cfg,_=store.settings()
        group=core_scope(cfg.persona_id) if tier=='core' else '123'
        item=MemoryProposal(group_id=group,persona_id=cfg.persona_id,content='角色锚点',tier=tier,
                            source_message_ids=['manual:anchor'])
        mid=await coordinator.commit(lambda:memory.commit(item,reviewed=True,manual=True,
            source_snapshots=[{'id':'manual:anchor','group_id':group}]))
        plan['changes'][0].update(memory_id=mid)
        with pytest.raises(Conflict):service.preview(plan)
        assert memory.get(mid)['tier']==tier
    finally:
        store.close()


async def test_review_receipt_survives_restart_and_policy_change_requires_new_review(tmp_path,monkeypatch):
    from buwubot import memory_maintenance
    from buwubot.memory import MemoryService
    from buwubot.store import Store

    store,memory,coordinator,service,plan,mid=await prepared(tmp_path)
    token=(await service.review(plan,Reviewer(),coordinator))['token']
    store.close()
    store=Store(tmp_path/'bot.db')
    memory=MemoryService(store,store.settings()[0]);service=memory_maintenance.MemoryMaintenance(memory)
    try:
        assert service.get(token)['status']=='ready'
        monkeypatch.setattr(memory_maintenance,'REVIEW_PROMPT','updated review rules')
        with pytest.raises(Conflict):service._validate(service.get(token))
        assert memory.get(mid)['revision']==1
    finally:
        store.close()


async def test_old_same_source_candidate_cannot_restore_premaintenance_content(tmp_path):
    store,memory,coordinator,service,plan,mid=await prepared(tmp_path)
    try:
        old=proposal().model_copy(update={'snapshot_epoch':memory.epoch(),'tags':['old-candidate']})
        plan['changes'][0]['content']='甲不喜欢咖啡，喜欢茶'
        token=(await service.review(plan,Reviewer(),coordinator))['token']
        await coordinator.commit(lambda:service.apply(token))
        with pytest.raises(Conflict):
            await coordinator.commit(lambda:memory.commit(old,reviewed=True))
        assert len(store.rows("SELECT * FROM memories WHERE deleted=0"))==1
        assert not memory.lifecycle.state(memory.get(mid))['protected']
        assert not store.rows('SELECT * FROM memory_manual_guards WHERE memory_id=?',(mid,))
    finally:
        store.close()


async def test_old_pending_batch_is_replanned_once_without_losing_manual_epoch_guard(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    from buwubot.memory_maintenance import MemoryMaintenance

    now=datetime(2026,10,27,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    memory,coordinator=engine.memory,engine.coordinator
    try:
        source=memory.archive.accept({'post_type':'message','message_type':'group','self_id':11,
            'group_id':123,'user_id':22,'message_id':1,'time':now.timestamp(),
            'message':[{'type':'text','data':{'text':'我喜欢茶，不喜欢咖啡'}}]},engine.settings)
        mid=await coordinator.commit(lambda:memory.commit(proposal()))
        old_epoch=memory.epoch()
        old=proposal().model_copy(update={'snapshot_epoch':old_epoch,'tags':['stale']})
        cfg,revision=store.settings();revision=store.save_settings(cfg.model_copy(update={'runtime_enabled':False}),revision)
        plan={'config_revision':revision,'persona_id':cfg.persona_id,'changes':[{
            'memory_id':mid,'expected_revision':1,'content':'甲不喜欢咖啡，喜欢茶','tier':'important','tags':['饮食']}]}
        service=MemoryMaintenance(memory)
        token=(await service.review(plan,Reviewer(),coordinator))['token']
        await coordinator.commit(lambda:service.apply(token))
        jid=engine.queue.enqueue('memory',{'group_id':'123','persona_id':cfg.persona_id,
            'messages':[vars(source)],'proposals':[old.model_dump()],'applied':[]},
            dedupe_key='stale-maintenance-batch',priority=1,source_time=source.time)
        await engine.process(engine.queue.get(jid))
        saved=engine.queue.get(jid)
        assert saved['state']=='pending' and 'proposals' not in saved['payload']
        assert saved['payload']['replan_guard_epoch']==old_epoch and not models.calls
        async def fresh(*args,**kwargs):
            return [proposal().model_copy(update={'memory_id':mid,'expected_revision':2,
                'content':'甲不喜欢咖啡，喜欢茶','tier':'important','tags':['饮食','已核对'],
                'snapshot_epoch':memory.epoch(),'maintenance_epoch':memory.epoch()})]
        async def reviewed(*args,**kwargs):return True,'一致'
        memory.propose=fresh;memory.review=reviewed
        await engine.process(saved)
        assert engine.queue.get(jid)['state']=='succeeded'
        assert len(store.rows('SELECT id FROM memories WHERE deleted=0'))==1
        assert not memory.lifecycle.state(memory.get(mid))['protected'] and not qq.sent
    finally:
        store.close()


async def test_reviewed_batch_can_update_two_facts_with_the_same_original_source(tmp_path):
    store,memory,coordinator,service,plan,mid=await prepared(tmp_path)
    try:
        other=await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={'content':'甲不喜欢咖啡'})))
        plan['changes'].append({'memory_id':other,'expected_revision':1,'content':'甲不喜欢咖啡',
                                'tier':'important','tags':['饮食']})
        token=(await service.review(plan,Reviewer(),coordinator))['token']
        await coordinator.commit(lambda:service.apply(token))
        assert memory.get(mid)['revision']==memory.get(other)['revision']==2
        assert len(store.rows('SELECT * FROM memory_maintenance_guards'))==2
    finally:
        store.close()
