import json
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from test_engine import build
from test_forward_pipeline import forwarded_event

from buwubot.coordinator import WorkPaused
from buwubot.models import Completion


@pytest.mark.parametrize('interrupt',['image','summary','malformed-analysis'])
async def test_restart_resumes_saved_images_and_does_not_block_unrelated_reply(tmp_path,interrupt):
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,qq,_models=build(tmp_path,lambda:now)
    async def forwarded(*_args):
        return {'messages':[{'message':[{'type':'image','data':{'file':f'page-{i}'}} for i in range(16)]}]}
    qq.forwarded=forwarded
    inspected=[]
    source=engine.ingest(forwarded_event(now))
    await engine.process(engine.queue.claim(now.timestamp(),kinds=['forward']))
    output=BytesIO()
    Image.new('RGB',(8,8),'red').save(output,format='PNG')
    asset=engine.media.save(output.getvalue())
    async def acquire(segment,*_args):
        position=segment['data']['file']
        if interrupt=='image' and position=='page-3' and 'page-3' not in inspected:
            raise WorkPaused('新呼叫打断')
        inspected.append(position)
        return asset
    async def analyze(*_args,**_kwargs):
        if interrupt=='malformed-analysis':
            raise ValueError('malformed model result')
        return {'description':'页面材料','visible_text':''}
    class Models:
        failed=False
        async def call(self,role,payload,**_kwargs):
            if role=='auxiliary':
                if interrupt=='summary' and not self.failed:
                    self.failed=True
                    raise OSError('temporary auxiliary failure')
                return Completion('fixture','glm-fixture',{'items':[{'position':item['position'],
                    'description':'页面材料','visible_text':''}
                    for item in json.loads(payload[1]['content'])['images']]})
            return Completion('fixture','cn:deepseek-v4.1-flash',
                {'text':'收到','tool':None,'sticker_id':None} if role=='chat' else {'terms':[],'ids':[]})
    engine.media.acquire,engine.media.analyze=acquire,analyze
    engine.models=Models()
    request={'id':'request','payload':{'messages':[vars(source)]}}
    with pytest.raises(WorkPaused):
        engine._forward_image_results([engine.memory.archive.get(source.id)],request)
    job=engine.queue.claim(now.timestamp(),kinds=['forward-images'])
    expected_error={'image':WorkPaused,'summary':OSError,'malformed-analysis':ValueError}[interrupt]
    with pytest.raises(expected_error):
        await engine.process(job)
    saved=engine.queue.get(job['id'])['payload']
    assert len(saved.get('batch_inputs',[]))=={'image':3,'summary':8,'malformed-analysis':0}[interrupt]
    assert saved['offset']==0 and not qq.sent
    store.close()

    # Reopen the same persistent queue and saved media through a new engine.
    store,engine,qq,models=build(tmp_path,lambda:now)
    async def resumed_acquire(segment,*_args):
        inspected.append(segment['data']['file'])
        return asset
    async def resumed_analyze(*_args,**_kwargs):
        return {'description':'页面材料','visible_text':''}
    models.call=Models().call
    if interrupt=='summary':
        models.call.__self__.failed=True
    engine.media.acquire,engine.media.analyze=resumed_acquire,resumed_analyze
    try:
        unrelated=engine.ingest(forwarded_event(now)|{'message_id':2,'message':[
            {'type':'text','data':{'text':'现在几点'}}]})
        jid=engine.queue.enqueue('reply',{'group_id':'123','messages':[vars(unrelated)],
            'reply_id':unrelated.message_id,'reply_source_id':unrelated.id,'source_message_ids':[unrelated.id]},
            dedupe_key='unrelated',priority=0,source_time=now.timestamp())
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent)==1
        await engine.process(engine.queue.get(job['id']))
        saved=engine.queue.get(job['id'])
        assert saved['payload']['offset']==8 and saved['state']=='pending'
        await engine.process(saved)
        saved=engine.queue.get(job['id'])
        assert saved['state']=='succeeded' and len(saved['payload']['notes'])==16
        expected=[f'page-{i}' for i in range(16)]
        assert inspected==(['page-0',*expected] if interrupt=='malformed-analysis' else expected)
        assert len(qq.sent)==1  # Auxiliary work itself never sends anything.
    finally:
        store.close()
