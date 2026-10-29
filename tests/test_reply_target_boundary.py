import json

import pytest
from test_reply_batches import setup_reply

from buwubot.models import Completion


def test_context_excludes_later_incoming_but_keeps_actual_self_replies():
    from buwubot.conversations import reply_context
    target={'id':'old','time':10,'source_order':5,'user_id':'22'}
    context=[target,{'id':'later','time':10,'source_order':6,'user_id':'22'},
             {'id':'own','time':12,'role':'assistant','user_id':'11','text':'已实际回复别的话题'}]
    assert [x['id'] for x in reply_context(context,[target])]==['old','own']


async def test_old_reply_generation_does_not_receive_later_call_as_context(tmp_path):
    store,engine,_,jid,models=setup_reply(tmp_path)
    future=engine.memory.archive.accept({'post_type':'message','message_type':'group',
        'self_id':11,'group_id':123,'user_id':22,'message_id':101,
        'time':engine.clock().timestamp()+7,'message':[{'type':'text','data':{'text':'助手，吃吃助手'}}]},engine.settings)
    original=models.call;seen=[]
    async def call(role,messages,**kwargs):
        if role=='chat':
            seen.append(json.loads(messages[-1]['content'][0]['text']))
            return Completion('',engine.settings.primary_model,{'text':'关于原话'})
        return await original(role,messages,**kwargs)
    models.call=call
    try:
        await engine.process(engine.queue.get(jid))
        assert seen and future.id not in {m['id'] for m in seen[0]['recent_chat']}
        assert seen[0]['targets'][0]['id']!=future.id
    finally:store.close()


def test_ambient_wrong_target_veto_finishes_without_sending_or_memory_receipt(tmp_path):
    store,engine,qq,jid,_=setup_reply(tmp_path)
    engine.settings=engine.settings.model_copy(update={'scene_analysis_enabled':True})
    payload=engine.queue.get(jid)['payload']|{'ambient_participation':True,
        'segments':[{'type':'text','data':{'text':'吃我？那我先咬回去'}}],
        'persona_hash':engine.draft_fingerprint()}
    engine.queue.payload(jid,payload)
    engine.scene_material=lambda *args,**kwargs:{'texts':['吃我？那我先咬回去'],
        'issues':['wrong_target'],'silence_reason':'原目标在回复别人，草稿回应了后续点名消息'}
    try:
        assert engine.review_reply_ready(engine.queue.get(jid),engine.memory) is False
        assert engine.queue.get(jid)['state']=='skipped_silent'
        assert qq.sent==[] and not store.rows('SELECT * FROM outgoing')
        assert not store.rows("SELECT id FROM jobs WHERE kind='memory'")
    finally:store.close()


async def test_directed_reply_cannot_be_silently_vetoed():
    from buwubot.scene_analysis import review_reply
    class Models:
        async def call(self,*args,**kwargs):
            return Completion('','helper',{'texts':['收到'],'issues':['repeated_reply'],
                'silence_reason':'刚刚答过相似的话'})
    with pytest.raises(ValueError,match='directed'):
        await review_reply(Models(),{'texts':['收到'],'ambient_participation':False})


async def test_already_sent_child_prevents_vetoing_the_remaining_reply(tmp_path):
    store,engine,qq,jid,_=setup_reply(tmp_path)
    job=engine.queue.get(jid);engine.queue.payload(jid,job['payload']|{'ambient_participation':True})
    try:
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent)==1 and len(engine.coordinator.response_history(jid))==1
        engine.memory.archive.accept({'post_type':'message','message_type':'group','self_id':11,
            'group_id':123,'user_id':33,'message_id':102,'time':engine.clock().timestamp()+1,
            'message':[{'type':'text','data':{'text':'旁边的新话题'}}]},engine.settings)
        assert engine.coordinator.refresh_ambient(engine.queue.get(jid))
        engine.settings=engine.settings.model_copy(update={'scene_analysis_enabled':True})
        p=engine.queue.get(jid)['payload']|{'segments':[{'type':'text','data':{'text':'剩余正文'}}],
            'persona_hash':engine.draft_fingerprint()}
        engine.queue.payload(jid,p);packets=[]
        def review(job,packet,**kwargs):
            packets.append(packet)
            return {'texts':['剩余正文'],'issues':['repeated_reply'],'silence_reason':'已经聊过了'}
        engine.scene_material=review
        with pytest.raises(ValueError,match='already-started'):
            engine.review_reply_ready(engine.queue.get(jid),engine.memory)
        assert packets[0]['allow_silence'] is False
        assert engine.queue.get(jid)['state']=='pending' and len(qq.sent)==1
    finally:store.close()
