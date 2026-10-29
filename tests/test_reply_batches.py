import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_engine import build

from buwubot.models import Completion


def setup_reply(tmp_path):
    now = datetime(2026,10,24,13,tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, qq, _ = build(tmp_path,lambda:now)
    async def send(group,segments,reply):
        qq.sent.append((group,segments,reply))
        return 'native-' + str(len(qq.sent))
    qq.send = send
    message = engine.memory.archive.accept({'post_type':'message','message_type':'group',
        'self_id':11,'group_id':123,'user_id':22,'message_id':100,'time':now.timestamp(),
        'sender':{'nickname':'fixture'},'message':[{'type':'text','data':{'text':'助手，聊会儿'}}]},engine.settings)
    jid = engine.queue.enqueue('reply',{'group_id':'123','persona_id':engine.settings.persona_id,
        'messages':[vars(message)],'source_message_ids':[message.id],'reply_id':'100',
        'reply_source_id':message.id},dedupe_key='batch',priority=0,source_time=now.timestamp())
    class Models:
        calls = 0
        async def call(self,role,payload,**kwargs):
            if role != 'chat':
                return Completion('',engine.settings.primary_model,{'terms':[],'ids':[]})
            self.calls += 1
            return Completion('',engine.settings.primary_model,
                              {'messages':[{'text':'来啦'},{'text':'想聊什么'}]})
    models = Models()
    engine.models = models
    engine.memory.models = models
    return store, engine, qq, jid, models


async def test_two_short_messages_have_distinct_real_receipts_and_original_native_quote(tmp_path):
    store, engine, qq, jid, _models = setup_reply(tmp_path)
    try:
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent) == 1
        assert engine.queue.get(jid)['state'] == 'pending'
        await engine.process(engine.queue.get(jid))
        assert [row[1][0]['data']['text'] for row in qq.sent] == ['来啦','想聊什么']
        assert all(row[2] == '100' for row in qq.sent)
        assert engine.queue.get(jid)['state'] == 'succeeded' and _models.calls == 1
        assert len(store.rows("SELECT job_id FROM outgoing WHERE status='sent'")) == 2
        assert len({r['qq_message_id'] for r in store.rows('SELECT qq_message_id FROM outgoing')})==2
        assert len(store.rows("SELECT id FROM jobs WHERE kind='memory'")) == 2
        own = [r for r in engine.memory.archive.context(engine.settings) if r['role']=='assistant']
        assert [r['text'] for r in own] == ['来啦','想聊什么']
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent) == 2
    finally:
        store.close()


@pytest.mark.parametrize('field',['acknowledgement','text'])
async def test_model_acknowledgement_is_sent_once_before_tool_starts(tmp_path,field):
    store, engine, qq, jid, models = setup_reply(tmp_path)
    calls = []
    async def read(arguments):
        calls.append(arguments)
        assert len(qq.sent) == 1 and qq.sent[0][1][0]['data']['text'] == '收到，我看看'
        return {'state':'succeeded','data':{'text':'actual content'}}
    engine.tools.register('read',read,description='read',input_schema={'type':'object','properties':{}})
    async def call(role,payload,**kwargs):
        if role != 'chat':
            return Completion('',engine.settings.auxiliary_model,{'terms':[],'ids':[]})
        if isinstance(payload[-1]['content'], list):
            return Completion('',engine.settings.primary_model,{'tool':'read','tool_args':{},
                field:'收到，我看看'})
        data = json.loads(payload[-1]['content'])
        assert data['already_sent_for_request'][0]['segments'][0]['data']['text'] == '收到，我看看'
        return Completion('',engine.settings.primary_model,{'text':'读完了，实际内容是……'})
    models.call = call
    try:
        await engine.process(engine.queue.get(jid))
        tool_id = store.rows("SELECT id FROM jobs WHERE kind='tool'")[0]['id']
        await engine.process(engine.queue.get(tool_id))
        assert len(qq.sent) == 1 and not calls
        assert engine.queue.get(tool_id)['state'] == 'pending'
        await engine.process(engine.queue.get(tool_id))
        assert len(calls) == 1 and len(qq.sent) == 2
        assert all(row[2] == '100' for row in qq.sent)
        assert engine.queue.get(tool_id)['state'] == 'succeeded'
        await engine.process(engine.queue.get(tool_id))
        assert len(calls) == 1 and len(qq.sent) == 2
    finally:
        store.close()


def queued_ack(tmp_path):
    now = datetime(2026,10,24,13,tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, qq, models = build(tmp_path,lambda:now)
    async def send(group,segments,reply):
        qq.sent.append((group,segments,reply))
        return 'native-' + str(len(store.rows('SELECT job_id FROM outgoing')))
    qq.send = send
    calls = []
    async def read(arguments):
        calls.append(arguments)
        return {'state':'succeeded','data':{'text':'actual'}}
    engine.tools.register('read',read,description='read',input_schema={'type':'object','properties':{}})
    async def call(role,payload,**kwargs):
        return Completion('',engine.settings.primary_model,{'text':'当前设定的回应'})
    models.call = call
    jid = engine.queue.enqueue('tool',{'group_id':'123','persona_id':engine.settings.persona_id,
        'messages':[],'reply_id':'100','tool_name':'read','tool_args':{},
        'tool_ack':'收到，我看看','tool_ack_stamp':engine.draft_fingerprint()},
        dedupe_key='ack-tool',priority=0,source_time=engine.clock().timestamp())
    return store,engine,qq,jid,calls


@pytest.mark.parametrize('core_changed',[False,True])
async def test_ack_receipt_survives_crash_before_ack_done_checkpoint(tmp_path,core_changed):
    store, engine, qq, jid, calls = queued_ack(tmp_path)
    original = engine.queue.payload
    def interrupt(jid,payload):
        if payload.get('tool_ack_done'):
            raise OSError('simulated crash after receipt')
        original(jid,payload)
    engine.queue.payload = interrupt
    try:
        with pytest.raises(OSError,match='simulated crash'):
            await engine.process(engine.queue.get(jid))
        assert len(qq.sent) == 1 and not calls
        assert engine.queue.get(jid)['state'] == 'pending'
    finally:
        store.close()
    store, engine, qq, same, calls = queued_ack(tmp_path)
    try:
        assert same == jid
        if core_changed:
            change_core(store,engine)
        await engine.process(engine.queue.get(jid))
        assert not qq.sent and not calls
        await engine.process(engine.queue.get(jid))
        assert len(calls) == 1 and len(qq.sent) == 1
        assert engine.queue.get(jid)['state'] == 'succeeded'
    finally:
        store.close()


async def test_ack_send_failure_waits_before_any_tool_invocation(tmp_path):
    from buwubot.channels import ChannelError
    store, engine, qq, jid, calls = queued_ack(tmp_path)
    send = qq.send
    async def fail(*args):
        raise ChannelError('offline')
    qq.send = fail
    try:
        await engine.process(engine.queue.get(jid))
        waiting = engine.queue.get(jid)
        assert waiting['state'] == 'retry_wait' and not calls and not qq.sent
        qq.send = send
        await engine.process(waiting)
        assert not qq.sent and not calls
        engine.clock = lambda:datetime.fromtimestamp(waiting['next_attempt']+1,ZoneInfo('Asia/Shanghai'))
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent) == 1 and not calls
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent) == 2 and len(calls) == 1
    finally:
        store.close()


async def test_unsent_ack_redecides_after_core_change_without_old_prefix(tmp_path):
    store, engine, qq, jid, calls = queued_ack(tmp_path)
    try:
        change_core(store,engine)
        await engine.process(engine.queue.get(jid))
        assert qq.sent[0][1][0]['data']['text'] == '当前设定的回应'
        assert not calls
    finally:
        store.close()


@pytest.mark.parametrize('confirmed',[False,True])
async def test_uncertain_ack_is_never_resent_and_does_not_discard_tool_work(tmp_path,confirmed):
    from buwubot.channels import SendUncertain
    store, engine, qq, jid, calls = queued_ack(tmp_path)
    send = qq.send
    async def ambiguous(group,parts,reply):
        await send(group,parts,reply)
        raise SendUncertain('unknown receipt')
    async def history(*args,**kwargs):
        return [{'user_id':11,'time':engine.clock().timestamp(),'message_id':'native-1',
            'message':[{'type':'reply','data':{'id':'100'}},*qq.sent[0][1]]}] if confirmed else []
    qq.send, qq.history = ambiguous, history
    try:
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent) == 1 and not calls
        qq.send = send
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent) == 1 and not calls
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent) == 2 and len(calls) == 1
        assert engine.queue.get(jid)['state'] == 'succeeded'
    finally:
        store.close()


@pytest.mark.parametrize('messages', [[{'text':'ok'},{'text':''}],
    [{'text':'ok'},{'sticker_id':'invented'}],[{'text':7}], [{'text':'a'}]*4])
async def test_entire_batch_is_validated_before_any_send(tmp_path,messages):
    store, engine, qq, jid, _models = setup_reply(tmp_path)
    async def call(role,*args,**kwargs):
        return Completion('',engine.settings.primary_model,
                          {'messages':messages} if role=='chat' else {'terms':[],'ids':[]})
    engine.models.call = call
    try:
        with pytest.raises((ValueError,TypeError)):
            await engine.process(engine.queue.get(jid))
        assert not qq.sent
    finally:
        store.close()


def change_core(store,engine):
    cfg, rev = store.settings()
    cfg = cfg.model_copy(update={'persona':'新的核心设定'})
    store.save_settings(cfg,rev)
    engine.settings = engine.memory.settings = engine.coordinator.settings = cfg


async def test_core_change_during_generation_requeues_claimed_parent(tmp_path):
    store, engine, qq, jid, _models = setup_reply(tmp_path)
    async def call(role,*args,**kwargs):
        if role=='chat':
            change_core(store,engine)
            return Completion('',engine.settings.primary_model,{'messages':[{'text':'old1'},{'text':'old2'}]})
        return Completion('',engine.settings.primary_model,{'terms':[],'ids':[]})
    engine.models.call = call
    try:
        claimed = engine.queue.claim(engine.clock().timestamp(),kinds=['reply'])
        await engine.process(claimed)
        assert not qq.sent
        assert engine.queue.get(jid)['state']=='pending'
    finally:
        store.close()


async def test_partial_core_change_regenerates_tail_with_confirmed_prefix(tmp_path):
    store, engine, qq, jid, _models = setup_reply(tmp_path)
    try:
        await engine.process(engine.queue.get(jid))
        change_core(store,engine)
        async def call(role,payload,**kwargs):
            if role=='chat':
                data = json.loads(payload[-1]['content'][0]['text'])
                assert data['already_sent_for_request'][0]['segments'][0]['data']['text']=='来啦'
                return Completion('',engine.settings.primary_model,{'messages':[{'text':'新设定的补充'}]})
            return Completion('',engine.settings.primary_model,{'terms':[],'ids':[]})
        engine.models.call = call
        await engine.process(engine.queue.get(jid))
        assert [r[1][0]['data']['text'] for r in qq.sent]==['来啦','新设定的补充']
        assert engine.queue.get(jid)['state']=='succeeded'
    finally:
        store.close()


async def test_explicit_part_failure_keeps_backoff_then_retries_without_replaying_prefix(tmp_path):
    from buwubot.channels import ChannelError
    store, engine, qq, jid, _models = setup_reply(tmp_path)
    send = qq.send
    async def offline(*args):
        raise ChannelError('offline')
    try:
        await engine.process(engine.queue.get(jid))
        qq.send = offline
        await engine.process(engine.queue.get(jid))
        parent = engine.queue.get(jid)
        assert parent['state']=='retry_wait' and len(qq.sent)==1
        child = engine.queue.get(parent['payload']['delivery_job_id'])
        assert child['attempts']==1 and parent['next_attempt']==child['next_attempt']
        qq.send = send
        await engine.process(parent)
        assert len(qq.sent)==1, 'must respect retry due even if invoked early'
        engine.clock = lambda:datetime.fromtimestamp(child['next_attempt']+1,ZoneInfo('Asia/Shanghai'))
        await engine.process(engine.queue.get(jid))
        assert [r[1][0]['data']['text'] for r in qq.sent]==['来啦','想聊什么']
        assert engine.queue.get(jid)['state']=='succeeded'
    finally:
        store.close()


@pytest.mark.parametrize('confirmed',[True,False])
async def test_uncertain_first_part_is_checked_before_any_tail(tmp_path,confirmed):
    from buwubot.channels import SendUncertain
    store, engine, qq, jid, _models = setup_reply(tmp_path)
    normal = qq.send
    async def uncertain(group,parts,reply):
        await normal(group,parts,reply)
        raise SendUncertain('ambiguous')
    async def history(*args,**kwargs):
        return [{'user_id':11,'time':engine.clock().timestamp(),'message_id':'native-1',
                 'message':[{'type':'reply','data':{'id':'100'}},*qq.sent[0][1]]}] if confirmed else []
    qq.send, qq.history = uncertain, history
    try:
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent)==1
        qq.send = normal
        await engine.coordinator.recover()
        await engine.process(engine.queue.get(jid))
        if confirmed:
            await engine.process(engine.queue.get(jid))
            assert len(qq.sent)==2 and engine.queue.get(jid)['state']=='succeeded'
        else:
            assert len(qq.sent)==1 and engine.queue.get(jid)['state']=='skipped_uncertain'
        assert _models.calls==1
    finally:
        store.close()


@pytest.mark.parametrize('control',['recalled','paused','disabled'])
async def test_each_later_part_rechecks_recall_pause_and_channel_permission(tmp_path,control):
    store, engine, qq, jid, _models = setup_reply(tmp_path)
    try:
        await engine.process(engine.queue.get(jid))
        if control=='recalled':
            store.connection.execute("UPDATE messages SET recalled=1 WHERE message_id='100'")
        elif control=='paused':
            engine.coordinator.allowed = lambda:False
        else:
            engine.coordinator.settings = engine.settings.model_copy(update={'group_id':'999','group_ids':[]})
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent)==1
        assert engine.queue.get(jid)['state']!='succeeded'
        if control=='disabled':
            assert engine.queue.get(jid)['state']=='needs_attention'
    finally:
        store.close()


async def test_restart_reuses_sent_child_without_model_or_prefix_replay(tmp_path):
    store, engine, qq, jid, _models = setup_reply(tmp_path)
    await engine.process(engine.queue.get(jid))
    assert len(qq.sent)==1
    store.close()
    store, restarted, qq, models = build(tmp_path,engine.clock)
    try:
        await restarted.coordinator.recover()
        await restarted.process(restarted.queue.get(jid))
        assert [r[1][0]['data']['text'] for r in qq.sent]==['想聊什么']
        assert not models.calls
        assert len(store.rows("SELECT job_id FROM outgoing WHERE status='sent'"))==2
        assert restarted.queue.get(jid)['state']=='succeeded'
    finally:
        store.close()


async def test_empty_continuation_only_finishes_after_confirmed_prefix(tmp_path):
    store, engine, qq, jid, _models = setup_reply(tmp_path)
    try:
        await engine.process(engine.queue.get(jid))
        change_core(store,engine)
        async def call(role,*args,**kwargs):
            return Completion('',engine.settings.primary_model,
                              {'messages':[]} if role=='chat' else {'terms':[],'ids':[]})
        engine.models.call = call
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent)==1 and engine.queue.get(jid)['state']=='succeeded'
        assert len(store.rows('SELECT job_id FROM outgoing'))==1
    finally:
        store.close()


async def test_dynamic_context_lists_each_confirmed_part(tmp_path):
    store, engine, _qq, jid, _models = setup_reply(tmp_path)
    try:
        payload = engine.queue.get(jid)['payload']
        bid = engine.queue.enqueue('scan',{'group_id':'123','persona_id':engine.settings.persona_id,
            'messages':payload['messages']},dedupe_key='context-batch',priority=0,source_time=1)
        engine.queue.finish(bid)
        payload['batch_id']=bid
        engine.queue.payload(jid,payload)
        await engine.process(engine.queue.get(jid))
        await engine.process(engine.queue.get(jid))
        context = engine.memory.context_memory(now=engine.clock().timestamp())
        sent=[r for r in context[0]['replies'] if r['send_status']=='sent']
        assert len(sent)==2
        assert [r['segments'][0]['data']['text'] for r in sent]==['来啦','想聊什么']
    finally:
        store.close()


async def test_transport_alias_does_not_suppress_later_part_of_same_response(tmp_path):
    store, engine, qq, jid, _models = setup_reply(tmp_path)
    try:
        await engine.process(engine.queue.get(jid))
        payload = engine.queue.get(jid)['payload']
        event={'post_type':'message','message_type':'group','self_id':11,'group_id':123,
            'user_id':22,'message_id':101,'time':engine.clock().timestamp(),
            'sender':{'nickname':'fixture'},'message':[{'type':'text','data':{'text':'助手，聊会儿'}}]}
        canonical = engine.memory.archive.accept(event,engine.settings)
        store.connection.execute('INSERT INTO message_aliases VALUES(?,?,?)',
                                 (payload['source_message_ids'][0],canonical.id,'other'))
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent)==2 and qq.sent[-1][2]=='101'
        assert engine.queue.get(jid)['state']=='succeeded'
    finally:
        store.close()

async def test_partial_reply_tool_handoff_preserves_prefix_across_transport_alias(tmp_path):
    store, engine, qq, jid, _models = setup_reply(tmp_path)
    async def lookup(arguments):
        return {'state':'succeeded','message':'检索材料'}
    engine.tools.handlers['lookup'] = lookup
    try:
        await engine.process(engine.queue.get(jid))
        original = engine.queue.get(jid)['payload']['source_message_ids'][0]
        canonical = engine.memory.archive.accept({'post_type':'message','message_type':'group',
            'self_id':11,'group_id':123,'user_id':22,'message_id':101,
            'time':engine.clock().timestamp(),'sender':{'nickname':'fixture'},
            'message':[{'type':'text','data':{'text':'助手，聊会儿'}}]},engine.settings)
        store.connection.execute('INSERT INTO message_aliases VALUES(?,?,?)',(original,canonical.id,'other'))
        change_core(store,engine)
        async def call(role,payload,**kwargs):
            if role!='chat':
                return Completion('',engine.settings.primary_model,{'terms':[],'ids':[]})
            raw=payload[-1]['content']
            data=json.loads(raw if isinstance(raw,str) else raw[0]['text'])
            if 'result' in data:
                assert data['already_sent_for_request'][0]['segments'][0]['data']['text']=='来啦'
                return Completion('',engine.settings.primary_model,{'text':'查到的补充'})
            return Completion('',engine.settings.primary_model,{'tool':'lookup','tool_args':{}})
        engine.models.call=call
        await engine.process(engine.queue.get(jid))
        tool=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='tool'")[0]['id'])
        await engine.process(tool)
        assert [r[1][0]['data']['text'] for r in qq.sent]==['来啦','查到的补充']
        assert qq.sent[-1][2]=='101'
        assert engine.queue.get(tool['id'])['state']=='succeeded'
    finally:
        store.close()
