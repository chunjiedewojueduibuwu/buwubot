import pytest

from buwubot.models import Completion


async def test_expression_contract_preserves_unflagged_text_and_rejects_scope_expansion():
    from buwubot.scene_analysis import review_reply

    packet={'texts':['晚上八点到，https://example.com/42'],'targets':[{'id':'g:1'}]}
    class Models:
        def __init__(self):
            self.output={'texts':['无理由改写'],'issues':[]}
        async def call(self,role,*args,**kwargs):
            assert role=='auxiliary'
            return Completion('','glm',self.output)
    model=Models()
    result=await review_reply(model,packet)
    assert result.texts==packet['texts']
    for invalid in ({'texts':[],'issues':['invented_state']},
                    {'texts':['ok'],'issues':['invented_state'],'tool':'send'},
                    {'texts':[''],'issues':['filler']},
                    {'texts':['  '],'issues':['filler']},
                    {'texts':['ok'],'issues':['unknown']}):
        model.output=invalid
        with pytest.raises(ValueError):
            await review_reply(model,packet)


@pytest.mark.parametrize('change',['success','pause','stale','invalid'])
async def test_expression_worker_persists_draft_and_only_sends_valid_replacement(tmp_path,change):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    from buwubot.coordinator import WorkPaused

    now=datetime(2026,10,27,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    engine.settings=engine.settings.model_copy(update={'scene_analysis_enabled':True})
    original=[[{'type':'text','data':{'text':'腿都站麻了吧'}}],
              [{'type':'text','data':{'text':'我已经去私聊了'}}]]
    jid=engine.queue.enqueue('reply',{'group_id':'123','persona_id':engine.settings.persona_id,
        'persona_hash':engine.draft_fingerprint(),'messages':[],
        'segments':original[0],'response_batches':original},
        dedupe_key='expression-parent',priority=0,source_time=now.timestamp())
    try:
        assert not engine.review_reply_ready(engine.queue.get(jid),engine.memory)
        assert not qq.sent
        child=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='scene'")[0]['id'])
        assert child['payload']['stage']=='expression'
        async def analyze(*args,**kwargs):
            if change=='pause':
                engine.settings=engine.settings.model_copy(update={'runtime_enabled':False})
            elif change=='stale':
                cfg,revision=store.settings()
                store.save_settings(cfg.model_copy(update={'persona':'new persona'}),revision)
            return Completion('','glm',{'texts':['排得好久','你来私聊说吧'],
                'issues':['invented_state','unperformed_action']} if change!='invalid' else {'texts':[]})
        models.call=analyze
        if change in ('pause','invalid'):
            with pytest.raises(WorkPaused if change=='pause' else ValueError):
                await engine.process(child)
        else:
            await engine.process(child)
        if change=='success':
            assert engine.review_reply_ready(engine.queue.get(jid),engine.memory)
            saved=engine.queue.get(jid)
            assert [batch[0]['data']['text'] for batch in saved['payload']['response_batches']]==['排得好久','你来私聊说吧']
            await engine.process(saved)
            assert qq.sent[0][1][0]['data']['text']=='排得好久'
        else:
            assert engine.queue.get(jid)['payload']['response_batches']==original
            assert not qq.sent
    finally:
        store.close()


async def test_expression_result_reused_after_restart_and_keeps_images(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    now=datetime(2026,10,27,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    cfg=engine.settings.model_copy(update={'scene_analysis_enabled':True})
    _,revision=store.settings()
    store.save_settings(cfg,revision)
    engine.settings=cfg
    image={'type':'image','data':{'file':'file:///unchanged.gif'}}
    jid=engine.queue.enqueue('reply',{'group_id':'123','persona_id':cfg.persona_id,
        'persona_hash':engine.draft_fingerprint(),'messages':[],
        'segments':[{'type':'text','data':{'text':'站麻了吧'}},image]},
        dedupe_key='restart-expression',priority=0,source_time=now.timestamp())
    try:
        assert not engine.review_reply_ready(engine.queue.get(jid),engine.memory)
        child=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='scene'")[0]['id'])
        async def analyze(*args,**kwargs):
            return Completion('','glm',{'texts':['这队排得有点久'],'issues':['invented_state']})
        models.call=analyze
        await engine.process(child)
        store.close()
        store,engine,qq,models=build(tmp_path,lambda:now)
        engine.settings=cfg
        assert engine.review_reply_ready(engine.queue.get(jid),engine.memory)
        assert engine.queue.get(jid)['payload']['segments'][1]==image
        await engine.process(engine.queue.get(jid))
        assert not models.calls
        assert len(qq.sent)==1
        assert qq.sent[0][1][0]['data']['text']=='这队排得有点久'
    finally:
        store.close()


def test_existing_uncertain_receipt_goes_to_reconciliation_without_new_review(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    now=datetime(2026,10,27,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,_qq,_models=build(tmp_path,lambda:now)
    engine.settings=engine.settings.model_copy(update={'scene_analysis_enabled':True})
    jid=engine.queue.enqueue('reply',{'group_id':'123','persona_id':engine.settings.persona_id,
        'persona_hash':engine.draft_fingerprint(),'messages':[],
        'segments':[{'type':'text','data':{'text':'已尝试发送的正文'}}]},
        dedupe_key='uncertain-expression',priority=0,source_time=now.timestamp())
    try:
        with store.transaction() as db:
            db.execute("INSERT INTO outgoing(job_id,group_id,segments,started_at,status) VALUES(?,?,?,?,?)",
                (jid,'123','[]',now.timestamp(),'uncertain'))
        assert engine.review_reply_ready(engine.queue.get(jid),engine.memory)
        assert not store.rows("SELECT id FROM jobs WHERE kind='scene'")
    finally:
        store.close()


async def test_tool_ack_review_finishes_before_send_or_tool_execution(tmp_path):
    from test_reply_batches import queued_ack

    store,engine,qq,jid,calls=queued_ack(tmp_path)
    engine.settings=engine.settings.model_copy(update={'scene_analysis_enabled':True})
    payload=engine.queue.get(jid)['payload']
    engine.queue.payload(jid,payload|{'tool_ack':'已经私聊你了'})
    async def analyze(role,*args,**kwargs):
        assert role=='auxiliary'
        return Completion('','glm',{'texts':['收到，我看看'],'issues':['unperformed_action']})
    engine.models.call=analyze
    try:
        await engine.process(engine.queue.get(jid))
        assert not qq.sent and not calls
        child=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='scene'")[0]['id'])
        await engine.process(child)
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent)==1 and qq.sent[0][1][0]['data']['text']=='收到，我看看'
        assert not calls
        assert engine.queue.get(jid)['payload']['tool_ack_done']
        assert 'segments' not in engine.queue.get(jid)['payload']
    finally:
        store.close()


async def test_tool_final_review_uses_actual_evidence_without_repeating_tool(tmp_path):
    import json

    from test_tool_followup import enqueue, setup

    calls=[]
    store,engine,qq=setup(tmp_path,calls)
    engine.settings=engine.settings.model_copy(update={'scene_analysis_enabled':True})
    packets=[]
    async def model(role,contents,**kwargs):
        if role=='auxiliary':
            packet=json.loads(contents[-1]['content'])
            packets.append(packet)
            return Completion('','glm',{'texts':packet['texts'],'issues':[]})
        return Completion('',engine.settings.primary_model,{'text':'根据实际搜索结果，入口为 https://www.python.org/'})
    engine.models.call=model
    jid=enqueue(engine)
    try:
        await engine.process(engine.queue.get(jid))
        assert not qq.sent and len(calls)==1
        child=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='scene'")[0]['id'])
        await engine.process(child)
        assert packets[0]['executed_actions'][0]['tool_name']=='web_search'
        assert packets[0]['executed_actions'][0]['result']['state']=='succeeded'
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent)==1 and len(calls)==1
    finally:
        store.close()
