import pytest

from buwubot.models import Completion
from buwubot.scene_analysis import SceneBrief, analyze_scene


def valid():
    return {'target_ids':['g:1'],'addressee':'bot','speech_act':'请求私聊',
        'known_facts':['明确叫到助手'],'unknowns':[],
        'excluded_topics':['查文献'],'execution_gap':'当前工具无跨会话发送'}


def test_scene_rejects_wrong_ids_unknown_fields_and_nontext():
    packet={'targets':[{'id':'g:1','user_id':'22'}]}
    assert SceneBrief.accept(valid(),packet).target_ids==['g:1']
    for change in ({'target_ids':['22']},{'target_ids':[]},{'text':'直接发这句'},
                   {'addressee':'owner'},{'known_facts':[{}]}):
        with pytest.raises(ValueError):
            SceneBrief.accept(valid()|change,packet)


async def test_auxiliary_scene_has_no_persona_or_sender_authority_and_keeps_original_data():
    packet={'targets':[{'id':'g:1','user_id':'22','text':'去私聊'}],'recent_chat':[]}
    calls=[]
    class Models:
        async def call(self,role,messages,**kwargs):
            calls.append((role,messages,kwargs))
            return Completion('','glm',valid())
    result=await analyze_scene(Models(),packet)
    assert result.target_ids==['g:1']
    assert calls[0][0]=='auxiliary' and calls[0][2]['json_mode'] is True
    assert 'g:1' in calls[0][1][-1]['content']
    assert packet['targets'][0]['text']=='去私聊'


async def test_durable_scene_waits_for_worker_reuses_result_and_rejects_stale_context(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    now=datetime(2026,10,27,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    cfg=engine.settings.model_copy(update={'scene_analysis_enabled':True})
    engine.settings=cfg
    packet={'targets':[{'id':'g:1','user_id':'22'}],'recent_chat':[]}
    jid=engine.queue.enqueue('reply',{'group_id':'123','persona_id':cfg.persona_id,'ambient_participation':True},
                             dedupe_key='scene-parent',priority=0,source_time=now.timestamp())
    try:
        assert engine.scene_material(engine.queue.get(jid),packet) is None
        scene=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='scene'")[0]['id'])
        async def analyze(role,messages,**kwargs):
            assert role=='auxiliary'
            return Completion('','glm',valid())
        models.call=analyze
        await engine.process(scene)
        assert engine.scene_material(engine.queue.get(jid),packet)['speech_act']=='请求私聊'
        store.close()
        store,engine,qq,models=build(tmp_path,lambda:now)
        engine.settings=cfg
        assert engine.scene_material(engine.queue.get(jid),packet)['speech_act']=='请求私聊'
        assert not models.calls
        assert not qq.sent and not store.rows('SELECT id FROM memories')
        changed=packet|{'recent_chat':[{'id':'g:2','text':'后续说明'}]}
        assert engine.scene_material(engine.queue.get(jid),changed) is None
        assert len(store.rows("SELECT id FROM jobs WHERE kind='scene'"))==2
    finally:
        store.close()


@pytest.mark.parametrize('change',['new_input','pause','invalid'])
async def test_changed_or_invalid_auxiliary_result_never_enters_reply_or_memory(tmp_path,change):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    from buwubot.coordinator import WorkPaused

    now=datetime(2026,10,27,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    engine.settings=engine.settings.model_copy(update={'scene_analysis_enabled':True})
    packet={'targets':[{'id':'g:1','user_id':'22'}]}
    jid=engine.queue.enqueue('reply',{'group_id':'123','persona_id':engine.settings.persona_id,'ambient_participation':True},
        dedupe_key='scene-parent',priority=0,source_time=now.timestamp())
    try:
        engine.scene_material(engine.queue.get(jid),packet)
        scene=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='scene'")[0]['id'])
        async def analyze(*args,**kwargs):
            if change=='new_input':
                engine.memory.archive.accept({'post_type':'message','message_type':'group','self_id':11,
                    'group_id':123,'user_id':22,'message_id':2,'time':now.timestamp(),
                    'message':[{'type':'text','data':{'text':'新消息'}}]},engine.settings)
            elif change=='pause':
                engine.settings=engine.settings.model_copy(update={'runtime_enabled':False})
            return Completion('','glm',valid()|({'target_ids':['22']} if change=='invalid' else {}))
        models.call=analyze
        if change in ('pause','invalid'):
            with pytest.raises(WorkPaused if change=='pause' else ValueError):
                await engine.process(scene)
        else:
            await engine.process(scene)
            assert engine.queue.get(scene['id'])['state']=='skipped_expired'
            assert engine.scene_material(engine.queue.get(jid),packet) is None
            assert len(store.rows("SELECT id FROM jobs WHERE kind='scene'"))==2
        assert 'result' not in engine.queue.get(scene['id'])['payload']
        assert not qq.sent and not store.rows('SELECT id FROM memories')
    finally:
        store.close()


def test_native_message_id_resolves_only_when_unique_in_current_targets():
    packet={'targets':[{'id':'g:1','message_id':'100'}]}
    assert SceneBrief.accept(valid()|{'target_ids':['100']},packet).target_ids==['g:1']
    packet['targets'].append({'id':'g:2','message_id':'100'})
    with pytest.raises(ValueError):
        SceneBrief.accept(valid()|{'target_ids':['100']},packet)


@pytest.mark.parametrize('outcome',['success','stale','other_block'])
async def test_retrying_failed_scene_resumes_only_its_waiting_parent(tmp_path,outcome):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    now=datetime(2026,10,27,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    engine.settings=engine.settings.model_copy(update={'scene_analysis_enabled':True})
    jid=engine.queue.enqueue('reply',{'group_id':'123','persona_id':engine.settings.persona_id},
        dedupe_key='scene-parent',priority=0,source_time=now.timestamp())
    packet={'targets':[{'id':'g:1'}]}
    try:
        engine.scene_material(engine.queue.get(jid),packet)
        scene=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='scene'")[0]['id'])
        engine.queue.defer(scene['id'],'invalid result',now=now.timestamp(),needs_attention=True)
        engine.scene_material(engine.queue.get(jid),packet)
        assert engine.queue.get(jid)['state']=='needs_attention'
        if outcome=='other_block':
            engine.queue.payload(jid,engine.queue.get(jid)['payload']|{'waiting_scene_id':'another-scene'})
        elif outcome=='stale':
            settings,revision=store.settings()
            store.save_settings(settings.model_copy(update={'persona':'changed persona'}),revision)
        # Same individual-job retry operation as the management endpoint.
        with store.transaction() as db:
            db.execute("UPDATE jobs SET state='pending',next_attempt=0 WHERE id=?",(scene['id'],))
        async def analyze(*args,**kwargs):
            return Completion('','glm',valid())
        models.call=analyze
        await engine.process(engine.queue.get(scene['id']))
        assert engine.queue.get(jid)['state']==('needs_attention' if outcome=='other_block' else 'pending')
        if outcome=='stale':
            assert engine.queue.get(scene['id'])['state']=='skipped_expired'
            assert engine.scene_material(engine.queue.get(jid),packet) is None
            assert len(store.rows("SELECT id FROM jobs WHERE kind='scene'"))==2
        elif outcome=='success':
            assert engine.scene_material(engine.queue.get(jid),packet)['target_ids']==['g:1']
        assert not qq.sent and not store.rows('SELECT id FROM memories')
    finally:
        store.close()


async def test_terminal_parent_does_not_start_auxiliary_call(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    now=datetime(2026,10,27,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    engine.settings=engine.settings.model_copy(update={'scene_analysis_enabled':True})
    jid=engine.queue.enqueue('reply',{'group_id':'123','persona_id':engine.settings.persona_id},
        dedupe_key='scene-parent',priority=0,source_time=now.timestamp())
    try:
        engine.scene_material(engine.queue.get(jid),{'targets':[{'id':'g:1'}]})
        scene=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='scene'")[0]['id'])
        engine.queue.finish(jid,'skipped_silent')
        await engine.process(scene)
        assert not models.calls and not qq.sent
        assert engine.queue.get(scene['id'])['state']=='skipped_expired'
    finally:
        store.close()


async def test_direct_request_not_starved_by_unrelated_live_chat_but_changed_targets_reanalyze(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    now=datetime(2026,10,27,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    engine.settings=engine.settings.model_copy(update={'scene_analysis_enabled':True})
    packet={'targets':[{'id':'g:1','user_id':'22','text':'明确请求'}],'recent_chat':[]}
    jid=engine.queue.enqueue('reply',{'group_id':'123','persona_id':engine.settings.persona_id,
        'ambient_participation':False},dedupe_key='scene-parent',priority=0,source_time=now.timestamp())
    try:
        engine.scene_material(engine.queue.get(jid),packet)
        scene=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='scene'")[0]['id'])
        async def analyze(*args,**kwargs):
            engine.memory.archive.accept({'post_type':'message','message_type':'group','self_id':11,
                'group_id':123,'user_id':33,'message_id':2,'time':now.timestamp(),
                'message':[{'type':'text','data':{'text':'其他话题'}}]},engine.settings)
            return Completion('','glm',valid())
        models.call=analyze
        await engine.process(scene)
        assert engine.queue.get(scene['id'])['state']=='succeeded'
        changed=packet|{'recent_chat':[{'id':'g:2','text':'其他话题'}]}
        assert engine.scene_material(engine.queue.get(jid),changed)['target_ids']==['g:1']
        assert len(store.rows("SELECT id FROM jobs WHERE kind='scene'"))==1
        changed['targets']=[packet['targets'][0]|{'text':'请求被补充/更正'}]
        assert engine.scene_material(engine.queue.get(jid),changed) is None
        assert len(store.rows("SELECT id FROM jobs WHERE kind='scene'"))==2
        assert not qq.sent
    finally:
        store.close()
