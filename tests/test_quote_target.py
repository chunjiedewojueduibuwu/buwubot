from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_engine import build

from buwubot.models import Completion


@pytest.mark.parametrize('variant',['request_before','request_after','other_author','media_anchor','substantive_anchor','alone'])
def test_bare_mention_quotes_same_author_request_without_redirecting_other_content(tmp_path,variant):
    now=datetime(2026,10,24,14,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,_,_=build(tmp_path,lambda:now)
    def message(mid,text,*,author=22,segments=None,age=0):
        return engine.memory.archive.accept({'post_type':'message','message_type':'group',
            'self_id':11,'group_id':123,'user_id':author,'message_id':mid,
            'time':now.timestamp()-age,'sender':{'nickname':'fixture'},
            'message':segments or [{'type':'text','data':{'text':text}}]},engine.settings)
    try:
        request=message(1,'Please answer in two short messages',author=33 if variant=='other_author' else 22,
                        age=0 if variant=='request_after' else 94)
        parts=[{'type':'at','data':{'qq':'11'}},{'type':'text','data':{'text':' '}}]
        if variant=='media_anchor':
            parts.append({'type':'image','data':{'file':'fixture'}})
        if variant=='substantive_anchor':
            parts.append({'type':'text','data':{'text':'another concrete question'}})
        anchor=message(2,'',segments=parts,age=94 if variant=='request_after' else 0)
        messages=[anchor] if variant=='alone' else [request,anchor]
        plan=engine.validate_plan({'replies':[{'source_message_ids':[m.id for m in messages],
            'reply_source_id':anchor.id,'reason':'called'}]},messages,now.timestamp())
        expected=request.id if variant in {'request_before','request_after'} else anchor.id
        assert len(plan)==1 and plan[0]['reply_source_id']==expected
        assert plan[0]['source_message_ids']==[m.id for m in messages]
    finally:
        store.close()


async def test_planning_guard_propagates_request_native_quote_to_every_short_reply(tmp_path):
    now=datetime(2026,10,24,14,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,_=build(tmp_path,lambda:now)
    messages=[]
    for mid,age,parts in [(100,94,[{'type':'text','data':{'text':'two short messages please'}}]),
                          (101,0,[{'type':'at','data':{'qq':'11'}}])]:
        messages.append(engine.memory.archive.accept({'post_type':'message','message_type':'group',
            'self_id':11,'group_id':123,'user_id':22,'message_id':mid,'time':now.timestamp()-age,
            'sender':{'nickname':'fixture'},'message':parts},engine.settings))
    async def call(role,payload,**kwargs):
        if role=='planning':
            data={'replies':[{'source_message_ids':[m.id for m in messages],
                             'reply_source_id':messages[-1].id,'reason':'called'}]}
        elif role=='chat':
            data={'messages':[{'text':'hello'},{'text':'what shall we discuss'}]}
        else:
            data={'terms':[],'ids':[]}
        return Completion('',engine.settings.primary_model,data)
    engine.models.call=call
    async def send(group,segments,reply):
        qq.sent.append((group,segments,reply))
        return str(len(qq.sent))
    qq.send=send
    try:
        scan=engine.queue.enqueue('scan',{'group_id':'123','messages':[vars(m) for m in messages]},
            dedupe_key='quote-scan',priority=0,source_time=messages[0].time)
        await engine.process(engine.queue.get(scan))
        reply=engine.queue.get(store.rows("SELECT id FROM jobs WHERE kind='reply'")[0]['id'])
        assert reply['priority']==0
        await engine.process(reply)
        await engine.process(engine.queue.get(reply['id']))
        assert len(qq.sent)==2 and [row[2] for row in qq.sent]==['100','100']
    finally:
        store.close()
