import copy
import hashlib
import json

from buwubot.conversations import message_context


def test_upgrade_invalidates_unfinished_four_image_drafts_without_changing_persona(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    from buwubot.models import INPUT_POLICY_VERSION
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,_qq,_models=build(tmp_path,lambda:now)
    try:
        legacy=hashlib.sha256(json.dumps([engine.settings.persona_fingerprint,engine.memory.core_context(),
            INPUT_POLICY_VERSION,'quoted-media-context-v1'],ensure_ascii=False).encode()).hexdigest()
        assert engine.draft_fingerprint()!=legacy
    finally:
        store.close()


def test_model_context_omits_bulk_image_transport_locators_without_losing_positions_or_archive():
    source={'id':'s','text':'原文','segments':[{'type':'forward','data':{'id':'received'}}],
        'forwarded':{'version':'v2','complete':True,'receipt_epoch':3,'nodes':[
            {'path':[0,0],'author_id':'22','time':100,'text':'节点原文',
             'images':[{'type':'image','data':{'file':str(i),'url':'https://gchat.qpic.cn/'+'a'*2000}}
                       for i in range(128)]}]}}
    original=copy.deepcopy(source)
    result=message_context(source)
    node=result['forwarded']['nodes'][0]
    assert len(node['images'])==128 and node['images'][-1]['position']==127
    assert node['author_id']=='22' and node['text']=='节点原文'
    assert 'qpic' not in json.dumps(result) and len(json.dumps(result))<10000
    assert result['forwarded']['receipt_epoch']==3 and source==original


async def test_tool_continuation_projects_transport_data_but_keeps_persisted_source(tmp_path):
    from test_tool_followup import NOW, enqueue, setup

    from buwubot.models import Completion
    class Models:
        async def call(self,role,payload,**_kwargs):
            assert role=='chat'
            data=json.loads(payload[1]['content'])
            assert 'qpic' not in json.dumps(data)
            assert data['request'][0]['forwarded']['nodes'][0]['images'][0]['position']==0
            assert data['quoted_messages'][0]['forwarded']['nodes'][0]['images'][0]['position']==0
            return Completion('fixture','cn:deepseek-v4.1-flash',{'text':'已核对'})
    store,engine,_qq=setup(tmp_path,[],Models())
    try:
        source=engine.ingest({'post_type':'message','message_type':'group','group_id':123,'self_id':11,
            'user_id':22,'message_id':1,'time':NOW.timestamp(),'message':[{'type':'text','data':{'text':'查看一下'}}]})
        raw=vars(source)|{'forwarded':{'nodes':[{'images':[{'type':'image',
            'data':{'file':'image','url':'https://gchat.qpic.cn/'+'a'*2000}}]}]}}
        jid=enqueue(engine)
        payload=engine.queue.get(jid)['payload']
        payload.update(messages=[raw],quoted_messages=[raw],reply_id=source.message_id,
                       reply_source_id=source.id,source_message_ids=[source.id])
        engine.queue.payload(jid,payload)
        await engine.process(engine.queue.get(jid))
        assert 'qpic' in json.dumps(engine.queue.get(jid)['payload']['messages'])
    finally:
        store.close()
