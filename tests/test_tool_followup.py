import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_engine import ModelsFixture, build

from buwubot.coordinator import WorkPaused
from buwubot.models import Completion

NOW = datetime(2026, 10, 24, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
URL = 'https://docs.python.org/3/library/asyncio.html'


def setup(folder, calls, models=None):
    store, engine, qq, _ = build(folder, lambda:NOW)
    async def search(args):
        calls.append(('search', args))
        return {'state':'succeeded', 'data':{'results':[{'url':URL}],
                'pages':[{'url':'https://www.python.org/', 'text':'Unrelated homepage'}]}}
    async def read(args):
        calls.append(('read', args))
        return {'state':'succeeded', 'data':{'url':args['url'], 'text':'asyncio suits IO-bound concurrency',
                                           'coverage':'static_text'}}
    for name, handler, field in [('web_search',search,'query'), ('web_read',read,'url')]:
        engine.tools.register(name, handler, description=name, input_schema={'type':'object',
            'properties':{field:{'type':'string','maxLength':300}},'required':[field],
            'additionalProperties':False})
    engine.models = models or FollowupModels()
    return store, engine, qq


def enqueue(engine):
    return engine.queue.enqueue('tool', {'group_id':'123', 'persona_id':'assistant',
        'messages':[], 'reply_id':'100', 'tool_name':'web_search', 'tool_args':{'query':'asyncio'}},
        dedupe_key='followup', priority=0, source_time=1)


class FollowupModels(ModelsFixture):
    async def call(self, role, contents, **kwargs):
        data = json.loads(contents[-1]['content'])
        if 'results' in data['result'].get('data', {}):
            assert any(t['name']=='web_read' and t['enabled'] for t in data['tools'])
            result = {'tool':'web_read', 'tool_args':{'url':URL}}
        else:
            assert data['tool_history'][0]['tool_result']['data']['pages'][0]['text']=='Unrelated homepage'
            assert data['result']['data']['text']=='asyncio suits IO-bound concurrency'
            result = {'text':'根据实际文档，asyncio适合IO密集并发：'+URL}
        return Completion('', 'cn:deepseek-v4.1-flash', result)


async def test_followup_survives_restart_with_single_final_send(tmp_path):
    calls = []
    store, engine, qq = setup(tmp_path, calls)
    jid = enqueue(engine)
    await engine.process(engine.queue.get(jid))
    assert engine.queue.get(jid)['state']=='pending' and not qq.sent
    assert [x[0] for x in calls]==['search']
    store.close()
    store, engine, qq = setup(tmp_path, calls)
    try:
        await engine.process(engine.queue.get(jid))
        await engine.process(engine.queue.get(jid))
        assert [x[0] for x in calls]==['search','read']
        assert calls[0][1]['request_id']!=calls[1][1]['request_id']
        assert len(qq.sent)==1 and engine.queue.get(jid)['state']=='succeeded'
    finally:
        store.close()


@pytest.mark.parametrize('decision', [
    {'tool':'missing','tool_args':{}},
    {'tool':'web_read','tool_args':{'url':URL,'request_id':'forged'}},
    {'tool':'web_read','tool_args':{'url':URL,'extra':True}},
    {'tool':'web_read','tool_args':{'url':42}},
    {'tool':'web_read','tool_args':{}},
    {'tool':'web_read','tool_args':'not an object'},
    {'tool':'web_read','tool_args':{'url':URL},'text':'ambiguous final'},
])
async def test_invalid_followup_never_starts_or_sends(tmp_path, decision):
    class Models(FollowupModels):
        async def call(self, *args, **kwargs):
            return Completion('', 'cn:deepseek-v4.1-flash', decision)
    calls = []
    store, engine, qq = setup(tmp_path,calls,Models())
    try:
        jid = enqueue(engine)
        with pytest.raises(ValueError):
            await engine.process(engine.queue.get(jid))
        assert [x[0] for x in calls]==['search'] and not qq.sent
        assert engine.queue.get(jid)['payload']['tool_result']['state']=='succeeded'
    finally:
        store.close()


async def test_repeated_call_holds_checkpoint_instead_of_looping(tmp_path):
    class Models(FollowupModels):
        async def call(self, *args, **kwargs):
            return Completion('', 'cn:deepseek-v4.1-flash',
                              {'tool':'web_search','tool_args':{'query':'asyncio'}})
    calls = []
    store, engine, qq = setup(tmp_path,calls,Models())
    try:
        jid = enqueue(engine)
        await engine.process(engine.queue.get(jid))
        assert engine.queue.get(jid)['state']=='needs_attention'
        assert len(calls)==1 and not qq.sent
        assert engine.queue.get(jid)['payload']['tool_result']['state']=='succeeded'
    finally:
        store.close()


async def test_repeated_failed_tool_gets_primary_failure_reply_without_reinvocation(tmp_path):
    class Models(FollowupModels):
        count = 0
        async def call(self, role, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                self.material = messages[-1]['content']
            else:
                assert messages[-1]['content'] == self.material
                assert 'already_sent_for_request' in json.loads(self.material)
            data = ({'tool':'web_search','tool_args':{'query':'asyncio'}} if self.count == 1
                    else {'text':'这个地址暂时读不了，尚未取得视频内容'})
            return Completion('', 'cn:deepseek-v4.1-flash', data)
    calls = []
    store, engine, qq = setup(tmp_path, calls, Models())
    async def failed(args):
        calls.append(('failed', args))
        return {'state':'failed','message':'unsupported URL'}
    engine.tools.handlers['web_search'] = failed
    try:
        jid = enqueue(engine)
        await engine.process(engine.queue.get(jid))
        assert len(calls) == 1 and len(qq.sent) == 1
        assert engine.queue.get(jid)['state'] == 'succeeded'
        assert engine.queue.get(jid)['payload']['tool_result']['state'] == 'failed'
    finally:
        store.close()


async def test_routine_pause_between_tools_keeps_completed_source(tmp_path):
    calls = []
    store, engine, qq = setup(tmp_path,calls)
    try:
        jid = enqueue(engine)
        await engine.process(engine.queue.get(jid))
        engine.clock = lambda:NOW.replace(hour=23)
        with pytest.raises(WorkPaused):
            await engine.process(engine.queue.get(jid))
        assert len(calls)==1 and not qq.sent
        engine.clock = lambda:NOW
        await engine.process(engine.queue.get(jid))
        assert [x[0] for x in calls]==['search','read'] and len(qq.sent)==1
    finally:
        store.close()


@pytest.mark.parametrize('attempts',[1,8])
@pytest.mark.parametrize('core_changed',[False,True])
async def test_pending_next_tool_keeps_its_own_operation_and_request_ids(tmp_path,core_changed,attempts):
    calls = []
    store, engine, qq = setup(tmp_path,calls)
    async def pending(args):
        calls.append(('pending-read',args))
        return {'state':'pending','operation_id':'saved-read'}
    engine.tools.handlers['web_read'] = pending
    jid = enqueue(engine)
    await engine.process(engine.queue.get(jid))
    await engine.process(engine.queue.get(jid))
    saved_id = calls[-1][1]['request_id']
    with store.transaction() as db:
        db.execute('UPDATE jobs SET attempts=? WHERE id=?',(attempts,jid))
    await engine.process(engine.queue.get(jid))
    saved = engine.queue.get(jid)
    assert not qq.sent and saved['state']=='pending'
    assert saved['next_attempt']==NOW.timestamp()+30
    assert saved['attempts']==0 and saved['last_error'] is None
    store.close()
    store, engine, qq = setup(tmp_path,calls)
    try:
        if core_changed:
            cfg, revision = store.settings()
            cfg = cfg.model_copy(update={'persona':cfg.persona+' updated while source pending'})
            store.save_settings(cfg,revision)
            engine.settings = cfg
        await engine.process(engine.queue.get(jid))
        assert calls[-1][1]['operation_id']=='saved-read'
        assert calls[-1][1]['request_id']==saved_id
        assert [x[0] for x in calls]==['search','pending-read','pending-read','read']
        assert len(qq.sent)==1
    finally:
        store.close()


async def test_model_failure_after_next_read_does_not_reexecute_sources(tmp_path):
    class FailFinal(FollowupModels):
        async def call(self, role, contents, **kwargs):
            data = json.loads(contents[-1]['content'])
            if 'results' not in data['result'].get('data',{}):
                raise OSError('temporary model failure')
            return await super().call(role,contents,**kwargs)
    calls = []
    store, engine, qq = setup(tmp_path,calls,FailFinal())
    jid = enqueue(engine)
    await engine.process(engine.queue.get(jid))
    with pytest.raises(OSError):
        await engine.process(engine.queue.get(jid))
    assert not qq.sent
    store.close()
    store, engine, qq = setup(tmp_path,calls)
    try:
        await engine.process(engine.queue.get(jid))
        assert [x[0] for x in calls]==['search','read'] and len(qq.sent)==1
    finally:
        store.close()


async def test_core_edit_redecides_unstarted_followup_without_rereading_source(tmp_path):
    calls = []
    store, engine, qq = setup(tmp_path,calls)
    try:
        jid = enqueue(engine)
        await engine.process(engine.queue.get(jid))
        cfg, revision = store.settings()
        cfg = cfg.model_copy(update={'persona':cfg.persona+' updated'})
        store.save_settings(cfg,revision)
        engine.settings = cfg
        class Updated(FollowupModels):
            async def call(self,*args,**kwargs):
                return Completion('',cfg.primary_model,{'tool':'web_read','tool_args':{'url':URL+'#updated'}})
        engine.models = Updated()
        await engine.process(engine.queue.get(jid))
        assert len(calls)==1 and not qq.sent
        saved = engine.queue.get(jid)['payload']
        assert saved['tool_args']['url']==URL+'#updated' and len(saved['tool_history'])==1
    finally:
        store.close()


async def test_core_change_during_decision_cannot_commit_stale_next_call(tmp_path):
    calls = []
    store, engine, qq = setup(tmp_path,calls)
    class Changed(FollowupModels):
        async def call(self,*args,**kwargs):
            cfg, revision = store.settings()
            store.save_settings(cfg.model_copy(update={'persona':cfg.persona+' changed'}),revision)
            return Completion('',cfg.primary_model,{'tool':'web_read','tool_args':{'url':URL}})
    engine.models = Changed()
    try:
        jid = enqueue(engine)
        with pytest.raises(WorkPaused):
            await engine.process(engine.queue.get(jid))
        assert len(calls)==1 and not qq.sent
        assert engine.queue.get(jid)['payload']['tool_result']['state']=='succeeded'
        assert not engine.queue.get(jid)['payload'].get('tool_history')
    finally:
        store.close()


async def test_selection_checkpoint_is_atomic_when_transition_fails(tmp_path,monkeypatch):
    calls = []
    store, engine, qq = setup(tmp_path,calls)
    jid = enqueue(engine)
    finish = engine.queue.finish
    def failed(*args,**kwargs):
        raise OSError('injected checkpoint interruption')
    monkeypatch.setattr(engine.queue,'finish',failed)
    with pytest.raises(OSError):
        await engine.process(engine.queue.get(jid))
    saved = engine.queue.get(jid)['payload']
    assert saved['tool_name']=='web_search' and 'tool_result' in saved and not saved.get('tool_history')
    monkeypatch.setattr(engine.queue,'finish',finish)
    try:
        await engine.process(engine.queue.get(jid))
        await engine.process(engine.queue.get(jid))
        assert [x[0] for x in calls]==['search','read'] and len(qq.sent)==1
    finally:
        store.close()


@pytest.mark.parametrize('limit',['count','bytes'])
async def test_flow_budget_holds_actual_results_without_sending(tmp_path,limit):
    calls = []
    store, engine, qq = setup(tmp_path,calls)
    try:
        jid = enqueue(engine)
        job = engine.queue.get(jid)
        job['payload']['tool_history'] = [{'tool_name':'web_read','tool_args':{'url':URL+str(i)},
            'tool_result':{'state':'succeeded','data':{'text':'x'*(60000 if limit=='bytes' else 1)}}}
            for i in range(5 if limit=='bytes' else 32)]
        job['payload']['tool_flow_stamp'] = engine.draft_fingerprint()
        engine.queue.payload(jid,job['payload'])
        await engine.process(engine.queue.get(jid))
        saved = engine.queue.get(jid)
        assert saved['state']=='needs_attention' and saved['payload']['tool_result']['state']=='succeeded'
        assert len(calls)==1 and not qq.sent
    finally:
        store.close()


async def test_call_during_followup_read_preserves_history_and_retry_identity(tmp_path):
    calls = []
    store, engine, qq = setup(tmp_path,calls)
    started = asyncio.Event()
    read = engine.tools.handlers['web_read']
    requests = []
    async def interrupted(args):
        requests.append(args)
        started.set()
        await asyncio.Event().wait()
    try:
        jid = enqueue(engine)
        await engine.process(engine.queue.get(jid))
        engine.tools.handlers['web_read'] = interrupted
        task = asyncio.create_task(engine.process(engine.queue.get(jid)))
        await asyncio.wait_for(started.wait(),1)
        engine.pause_tools()
        with pytest.raises(WorkPaused):
            await asyncio.wait_for(task,1)
        assert len(engine.queue.get(jid)['payload']['tool_history'])==1 and not qq.sent
        engine._tool_pause_until = 0
        engine.tools.handlers['web_read'] = read
        await engine.process(engine.queue.get(jid))
        assert calls[-1][1]['request_id']==requests[0]['request_id']
        assert [x[0] for x in calls]==['search','read'] and len(qq.sent)==1
    finally:
        store.close()


@pytest.mark.parametrize('change',['disabled','auxiliary'])
async def test_followup_requires_current_enabled_tool_and_primary_decision(tmp_path,change):
    calls = []
    store, engine, qq = setup(tmp_path,calls)
    class Models(FollowupModels):
        async def call(self,*args,**kwargs):
            if change=='disabled':
                engine.tools.disable('web_read')
            return Completion('', 'glm' if change=='auxiliary' else engine.settings.primary_model,
                              {'tool':'web_read','tool_args':{'url':URL}})
    engine.models = Models()
    try:
        jid = enqueue(engine)
        with pytest.raises(ValueError):
            await engine.process(engine.queue.get(jid))
        assert len(calls)==1 and not qq.sent
    finally:
        store.close()
