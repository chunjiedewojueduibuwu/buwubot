import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_ambient_participation import scan
from test_engine import build

from buwubot.models import Completion


@pytest.mark.parametrize('style,budget',[('casual',1500),('help',3000),('deliverable',3000),('auto',3000)])
async def test_planned_reply_style_reaches_actual_generation_without_changing_request(tmp_path,style,budget):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    packets=[]
    answer='complete answer' if style not in ('help','deliverable') else '说明\n```python\nprint(123456789)\n```\nhttps://example.com/a?q=42'
    async def call(role,messages,**kwargs):
        if role=='chat':
            packet=json.loads(messages[-1]['content'][0]['text'])
            packets.append((packet,kwargs))
            return Completion('',engine.settings.primary_model,{'text':answer})
        return Completion('','helper',{'terms':[],'ids':[]})
    try:
        jid=scan(engine,now.timestamp(),called=True)
        scan_job=engine.queue.get(jid)
        topic=scan_job['payload']['topics'][0].copy()
        topic['reason']='只回应当前图片评价，不接邻近话题'
        if style!='auto':
            topic['reply_style']=style
        engine.queue.payload(jid,scan_job['payload']|{'topics':[topic]})
        await engine.process(engine.queue.get(jid))
        reply=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='reply'")[0]['id'])
        assert reply['payload']['reply_style']==style
        assert reply['payload']['reply_purpose']==topic['reason']
        models.call=call
        await engine.process(reply)
        assert packets[0][0]['reply_style']==style and packets[0][1]['max_tokens']==budget
        assert packets[0][0]['targets'][0]['text']=='今天这个怎么样'
        assert packets[0][0]['reply_purpose']==topic['reason']
        assert packets[0][0]['reply_destination']=='123'
        assert qq.sent[0][1][0]['data']['text']==answer
    finally:
        store.close()


@pytest.mark.parametrize('style',['unbounded',{},None])
def test_invalid_model_style_does_not_silently_expand_a_topic(tmp_path,style):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,_,_=build(tmp_path,lambda:now)
    try:
        jid=scan(engine,now.timestamp(),called=True)
        from buwubot.conversations import Message
        message=Message(**engine.queue.get(jid)['payload']['messages'][0])
        with pytest.raises(ValueError,match='reply style'):
            engine.validate_plan({'replies':[{'source_message_ids':[message.id],
                'reply_source_id':message.id,'reply_style':style}]},[message],now.timestamp())
    finally:
        store.close()

async def test_failed_casual_generation_retains_original_request_and_sends_nothing(tmp_path):
    from buwubot.models import ModelUnavailable

    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,models=build(tmp_path,lambda:now)
    try:
        scan_id=scan(engine,now.timestamp(),called=True)
        await engine.process(engine.queue.get(scan_id))
        reply=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='reply'")[0]['id'])
        engine.queue.payload(reply['id'],reply['payload']|{'reply_style':'casual'})
        async def unavailable(role,*args,**kwargs):
            if role=='chat':
                raise ModelUnavailable('invalid or truncated model response',shared=False)
            return Completion('','helper',{'terms':[],'ids':[]})
        models.call=unavailable
        with pytest.raises(ModelUnavailable):
            await engine.process(engine.queue.get(reply['id']))
        saved=engine.queue.get(reply['id'])
        assert saved['state']=='pending' and saved['payload']['reply_style']=='casual'
        assert saved['payload']['messages'][0]['text']=='今天这个怎么样'
        assert 'segments' not in saved['payload'] and qq.sent==[]
    finally:
        store.close()


@pytest.mark.parametrize('style,budget',[('casual',1500),('help',3000),('deliverable',3000),('auto',3000)])
async def test_tool_followup_retains_expression_style_and_actual_evidence_after_restart(tmp_path,style,budget):
    from test_tool_followup import FollowupModels, enqueue, setup

    from buwubot.engine import REPLY_STYLE_TASKS

    packets=[]
    class Models(FollowupModels):
        async def call(self,role,contents,**kwargs):
            packet=json.loads(contents[-1]['content'])
            if role=='auxiliary':
                return Completion('','helper',{'texts':['并发：https://docs.python.org/3/library/asyncio.html'],
                    'issues':['casual_length']})
            packets.append((packet,kwargs,contents[0]['content']))
            return await super().call(role,contents,**kwargs)
    calls=[]
    store,engine,qq=setup(tmp_path,calls,Models())
    marker=[{'id':'growth-tool','kind':'view','content':'助手对并发有自己的看法。','revision':1}]
    engine.memory.growth_context=lambda **kwargs:marker
    jid=enqueue(engine)
    payload=engine.queue.get(jid)['payload']
    payload['reply_purpose']='查阅实际文档，回答当前请求'
    engine.queue.payload(jid,payload)
    if style!='auto':
        payload['reply_style']=style
        engine.queue.payload(jid,payload)
    await engine.process(engine.queue.get(jid))
    assert not qq.sent
    store.close()
    store,engine,qq=setup(tmp_path,calls,Models())
    engine.memory.growth_context=lambda **kwargs:marker
    try:
        await engine.process(engine.queue.get(jid))
        if style=='casual':
            assert not qq.sent
            scene=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='scene'")[0]['id'])
            await engine.process(scene)
            await engine.process(engine.queue.get(jid))
        assert [item[0] for item in calls]==['search','read']
        assert len(qq.sent)==1 and qq.sent[0][2]=='100'
        if style=='casual':
            assert sum(len(p['data']['text']) for p in qq.sent[0][1] if p['type']=='text')<=50
        for packet,kwargs,system in packets:
            assert packet['persona_growth']==marker
            assert packet['reply_style']==style and kwargs['max_tokens']==budget
            assert packet['reply_purpose']=='查阅实际文档，回答当前请求'
            assert REPLY_STYLE_TASKS[style] in system
        assert packets[1][0]['result']['data']['text']=='asyncio suits IO-bound concurrency'
    finally:
        store.close()
