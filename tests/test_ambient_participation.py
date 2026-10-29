from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_engine import build

from buwubot.conversations import Message
from buwubot.models import Completion


def scan(engine, when, *, called=False, named=False):
    text='助手，看看' if named else '今天这个怎么样'
    segments=[{'type':'text','data':{'text':text}}]
    if called:
        segments.append({'type':'at','data':{'qq':'11'}})
    message=engine.memory.archive.accept({'post_type':'message','message_type':'group',
        'group_id':123,'self_id':11,'user_id':22,'message_id':1,'time':when,
        'sender':{'nickname':'甲'},'message':segments},engine.settings)
    jid=engine.queue.enqueue('scan',{'group_id':'123','messages':[vars(message)],
        'topics':[{'source_message_ids':[message.id],'reply_source_id':message.id}]},
        dedupe_key='scan-test',priority=1,source_time=when)
    return jid


@pytest.mark.parametrize('direct', ['ambient','at','name'])
async def test_only_ambient_reply_waits_and_does_not_block_direct_calls(tmp_path,direct):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    try:
        jid=scan(engine,now.timestamp(),called=direct=='at',named=direct=='name')
        await engine.process(engine.queue.get(jid))
        job=store.rows("SELECT id FROM jobs WHERE kind='reply'")[0]['id']
        pending=engine.queue.get(job)
        assert pending['payload']['ambient_participation'] is (direct=='ambient')
        if direct=='ambient':
            assert pending['next_attempt']==now.timestamp()+15
            await engine.process(pending)
            assert models.calls==[] and qq.sent==[]
        else:
            assert pending['next_attempt']<=now.timestamp()
    finally:
        store.close()


async def test_after_observation_model_may_remain_silent_without_fake_sent_receipt(tmp_path):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    try:
        jid=scan(engine,now.timestamp())
        await engine.process(engine.queue.get(jid))
        job=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='reply'")[0]['id'])
        now+=timedelta(seconds=16)
        async def call(role,*args,**kwargs):
            if role=='chat':
                return Completion('',engine.settings.primary_model,{'text':None,'messages':[],
                    'silence_reason':'现在的上下文表明是在问其他群友'})
            return Completion('','helper',{'terms':[],'ids':[]})
        models.call=call
        await engine.process(job)
        saved=engine.queue.get(job['id'])
        assert saved['state']=='skipped_silent'
        assert saved['payload']['silence_reason']=='现在的上下文表明是在问其他群友'
        assert qq.sent==[]
    finally:
        store.close()


def test_chat_contract_update_invalidates_old_draft(tmp_path,monkeypatch):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,_,_=build(tmp_path,lambda:now)
    try:
        original=engine.draft_fingerprint()
        monkeypatch.setattr('buwubot.engine.REPLY_PROMPT','different reply contract')
        assert engine.draft_fingerprint()!=original
    finally:
        store.close()


async def test_new_chat_while_ambient_draft_is_generated_requires_a_fresh_decision(tmp_path):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    try:
        jid=scan(engine,now.timestamp())
        await engine.process(engine.queue.get(jid))
        reply_id=store.rows("SELECT id FROM jobs WHERE kind='reply'")[0]['id']
        now+=timedelta(seconds=16)
        chat_calls=0
        async def call(role,*args,**kwargs):
            nonlocal chat_calls
            if role!='chat':
                return Completion('','helper',{'terms':[],'ids':[]})
            chat_calls+=1
            if chat_calls==1:
                engine.memory.archive.accept({'post_type':'message','message_type':'group',
                    'group_id':123,'self_id':11,'user_id':22,'message_id':2,'time':now.timestamp(),
                    'message':[{'type':'text','data':{'text':'没喊你，问旁边那个人呢'}}]},engine.settings)
                return Completion('',engine.settings.primary_model,{'text':'过时的插话'})
            return Completion('',engine.settings.primary_model,{'text':None,'messages':[],
                'silence_reason':'后续已说明不是在问助手'})
        models.call=call
        await engine.process(engine.queue.get(reply_id))
        saved=engine.queue.get(reply_id)
        assert saved['state']=='pending' and 'segments' not in saved['payload']
        assert qq.sent==[]
        await engine.process(saved)
        assert engine.queue.get(reply_id)['state']=='skipped_silent' and chat_calls==2
        assert qq.sent==[]
    finally:
        store.close()


@pytest.mark.parametrize('immediate_author', ['11','33'])
async def test_only_immediate_quote_of_bot_bypasses_observation(tmp_path,immediate_author):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,_,_=build(tmp_path,lambda:now)
    try:
        own=Message(id='123:id:90',message_id='90',group_id='123',user_id='11',self_id='11',
            time=now.timestamp()-2,display_name='助手',nickname='助手',text='之前的话',segments=[])
        prior=engine.queue.enqueue('reply',{'group_id':'123'},dedupe_key='prior-send',
            priority=1,source_time=own.time)
        store.connection.execute("INSERT INTO outgoing VALUES(?,? ,NULL,?,?,?,?)",
            (prior,'123','[]',own.time,'sent','90'))
        engine.queue.finish(prior)
        if immediate_author!='11':
            other=replace(own,id='123:id:91',message_id='91',user_id='33',time=now.timestamp()-1,
                text='在转述助手以前的话',segments=[{'type':'reply','data':{'id':'90'}}])
            engine.memory.archive.accept(other,engine.settings)
        source=engine.memory.archive.accept(replace(own,id='123:id:1',message_id='1',user_id='22',
            time=now.timestamp(),text='继续说',segments=[{'type':'reply','data':{
                'id':'90' if immediate_author=='11' else '91'}}]),engine.settings)
        jid=engine.queue.enqueue('scan',{'group_id':'123','messages':[vars(source)],
            'topics':[{'source_message_ids':[source.id],'reply_source_id':source.id}]},
            dedupe_key='quoted-scan',priority=1,source_time=source.time)
        await engine.process(engine.queue.get(jid))
        job=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='reply' AND id<>?",(prior,))[0]['id'])
        assert job['payload']['ambient_participation'] is (immediate_author!='11')
    finally:
        store.close()

@pytest.mark.parametrize('boundary', ['single','batch','delivery'])
async def test_ambient_context_is_checked_again_at_final_send_boundary(tmp_path,boundary):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,_=build(tmp_path,lambda:now)
    try:
        scan(engine,now.timestamp())
        order=engine.memory.archive.incoming_order('123','11')
        segments=[{'type':'text','data':{'text':'尚未发送的旧插话'}}]
        payload={'group_id':'123','messages':[], 'ambient_participation':True,
            'ambient_order':order,'persona_hash':engine.draft_fingerprint(),'segments':segments}
        if boundary!='single':payload['response_batches']=[segments]
        jid=engine.queue.enqueue('reply',payload,dedupe_key='stale-boundary',priority=1,source_time=now.timestamp())
        child=None
        if boundary=='delivery':
            child=engine.queue.enqueue('delivery',payload|{'delivery_parent':jid},
                dedupe_key='stale-child',priority=1,source_time=now.timestamp())
            engine.queue.payload(jid,payload|{'delivery_job_id':child})
        engine.memory.archive.accept({'post_type':'message','message_type':'group',
            'group_id':123,'self_id':11,'user_id':22,'message_id':2,'time':now.timestamp(),
            'message':[{'type':'text','data':{'text':'没喊你'}}]},engine.settings)
        if boundary=='batch':
            await engine.coordinator.continue_response(jid,now=now.timestamp())
        else:
            await engine.coordinator.send(child or jid,segments,now=now.timestamp())
        assert qq.sent==[]
        root=engine.queue.get(jid)
        assert root['state']=='pending'
        assert 'segments' not in root['payload'] and 'response_batches' not in root['payload']
        if child:assert engine.queue.get(child)['state']=='skipped_expired'
    finally:
        store.close()

async def test_new_chat_after_first_batch_replans_remainder_without_resending_prefix(tmp_path):
    import json

    from test_reply_batches import setup_reply
    store,engine,qq,jid,models=setup_reply(tmp_path)
    try:
        job=engine.queue.get(jid)
        engine.queue.payload(jid,job['payload']|{'ambient_participation':True})
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent)==1
        first=qq.sent[0]
        engine.memory.archive.accept({'post_type':'message','message_type':'group',
            'group_id':123,'self_id':11,'user_id':22,'message_id':101,
            'time':engine.clock().timestamp(),
            'message':[{'type':'text','data':{'text':'no need to continue'}}]},engine.settings)
        chat_calls=0
        async def call(role,messages,**kwargs):
            nonlocal chat_calls
            if role!='chat':return Completion('','helper',{'terms':[],'ids':[]})
            chat_calls+=1
            data=json.loads(messages[-1]['content'][0]['text'])
            assert len(data['already_sent_for_request'])==1
            assert any(m['text']=='no need to continue' for m in data['recent_chat'])
            return Completion('',engine.settings.primary_model,{'messages':[],'text':None})
        models.call=call
        await engine.process(engine.queue.get(jid))
        assert qq.sent==[first] and chat_calls==1
        assert engine.queue.get(jid)['state']=='succeeded'
        assert len(store.rows("SELECT job_id FROM outgoing WHERE status='sent'"))==1
    finally:
        store.close()

async def test_accepted_tool_result_is_not_discarded_as_an_ambient_draft(tmp_path):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,_=build(tmp_path,lambda:now)
    try:
        segments=[{'type':'text','data':{'text':'actual tool result'}}]
        jid=engine.queue.enqueue('tool',{'group_id':'123','ambient_participation':True,
            'tool_result':{'text':'actual'},'segments':segments,'persona_hash':engine.draft_fingerprint()},
            dedupe_key='accepted-tool',priority=1,source_time=now.timestamp())
        await engine.coordinator.send(jid,segments,now=now.timestamp())
        assert len(qq.sent)==1 and engine.queue.get(jid)['state']=='succeeded'
    finally:
        store.close()

def test_tool_reply_contract_update_invalidates_pending_drafts(tmp_path,monkeypatch):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,_,_=build(tmp_path,lambda:now)
    try:
        original=engine.draft_fingerprint()
        monkeypatch.setattr('buwubot.engine.TOOL_REPLY_PROMPT','updated evidence contract',raising=False)
        assert engine.draft_fingerprint()!=original
    finally:
        store.close()

def test_silent_reply_payload_obeys_raw_chat_retention_without_erasing_unfinished_jobs(tmp_path):
    import time

    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,_,_=build(tmp_path,lambda:now)
    try:
        payload={'group_id':'123','messages':[{'text':'raw chat that must expire'}],
                 'silence_reason':'private reason from old chat'}
        silent=engine.queue.enqueue('reply',payload,dedupe_key='old-silent',priority=1,source_time=1)
        pending=engine.queue.enqueue('reply',payload,dedupe_key='old-pending',priority=1,source_time=1)
        engine.queue.finish(silent,'skipped_silent')
        old=time.time()-(engine.settings.retention_days+1)*86400
        store.connection.execute('UPDATE jobs SET updated_at=? WHERE id IN (?,?)',(old,silent,pending))
        engine.memory.cleanup(now=time.time())
        assert engine.queue.get(silent)['payload']=={}
        assert engine.queue.get(silent)['state']=='skipped_silent'
        assert engine.queue.get(pending)['payload']==payload
    finally:
        store.close()

@pytest.mark.parametrize('terminal',['skipped_silent','skipped_expired'])
async def test_terminal_reply_cannot_be_reprocessed_or_sent_directly(tmp_path,terminal):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    try:
        jid=scan(engine,now.timestamp())
        await engine.process(engine.queue.get(jid))
        reply=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='reply'")[0]['id'])
        engine.queue.payload(reply['id'],reply['payload']|{'persona_hash':engine.draft_fingerprint()})
        engine.queue.finish(reply['id'],terminal)
        await engine.process(engine.queue.get(reply['id']))
        assert engine.queue.get(reply['id'])['state']==terminal and models.calls==[]
        await engine.coordinator.send(reply['id'],[{'type':'text','data':{'text':'obsolete'}}],now=now.timestamp())
        assert qq.sent==[] and not store.rows('SELECT * FROM outgoing')
        assert engine.queue.get(reply['id'])['state']==terminal
    finally:
        store.close()
