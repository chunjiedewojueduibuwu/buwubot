import json

import pytest

from buwubot.forward_images import summarize_batch
from buwubot.models import Completion


def inputs():
    return [{'source_id':'source','position':f'forward:0.{i}:0','asset_hash':str(i)*64,
             'analysis':{'description':f'page-{i}','visible_text':f'text-{i}'}} for i in range(8)]


def response():
    return {'items':[{'position':item['position'],'description':f'材料{i}',
                     'visible_text':f'文字{i}'} for i,item in enumerate(inputs())]}


class Models:
    def __init__(self,data):
        self.data,self.calls=data,0

    async def call(self,role,payload,**_kwargs):
        assert role=='auxiliary'
        assert len(json.loads(payload[1]['content'])['images'])==8
        self.calls+=1
        return Completion('fixture','glm-fixture',self.data)


async def test_batch_keeps_all_positions_in_source_order_and_uses_server_asset_ids():
    data=response()
    data['items'].reverse()
    data['items'][0]['asset_hash']='forged'
    result=await summarize_batch(inputs(),'总结这个转发',Models(data))
    assert [item['position'] for item in result]==[item['position'] for item in inputs()]
    assert [item['asset_hash'] for item in result]==[item['asset_hash'] for item in inputs()]
    assert all(item['compressed'] and item['source_id']=='source' for item in result)


@pytest.mark.parametrize('fault',['missing','duplicate','invented','oversize','malformed'])
async def test_invalid_batch_cannot_claim_all_pages_were_read(fault):
    data=response()
    if fault=='missing':data['items'].pop()
    elif fault=='duplicate':data['items'][-1]=data['items'][0]
    elif fault=='invented':data['items'][-1]['position']='not-received'
    elif fault=='oversize':data['items'][0]['description']='a'*10000
    else:data={'items':'wrong'}
    with pytest.raises(ValueError):
        await summarize_batch(inputs(),'总结',Models(data))


async def test_unreadable_images_remain_explicit_without_calling_model():
    class Forbidden:
        async def call(self,*_args,**_kwargs):
            pytest.fail('no readable material')
    result=await summarize_batch([{'source_id':'s','position':'p','error':'无法读取'}],'总结',Forbidden())
    assert result==[{'source_id':'s','position':'p','error':'无法读取','compressed':True}]


async def test_more_than_one_batch_is_rejected_before_model_call():
    model=Models(response())
    with pytest.raises(ValueError):
        await summarize_batch(inputs()*2,'总结',model)
    assert model.calls==0


async def test_notes_have_a_joint_text_budget_that_fits_a_full_forward():
    data=response()
    data['items'][0].update(description='字'*151,visible_text='')
    with pytest.raises(ValueError):
        await summarize_batch(inputs(),'总结',Models(data))


async def test_request_can_assign_smaller_per_image_budget_across_multiple_forwards():
    data=response()
    data['items'][0].update(description='字'*45,visible_text='字'*10)
    with pytest.raises(ValueError):
        await summarize_batch(inputs(),'总结',Models(data),note_chars=50)


def test_multiple_forwards_share_a_single_request_notes_budget(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    from buwubot.conversations import Message
    from buwubot.coordinator import WorkPaused
    from buwubot.forward_images import REQUEST_NOTE_CHARS
    now=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,_qq,_models=build(tmp_path,lambda:now)
    sources=[Message(str(i),str(i),'123','22','11',100,'作者','作者','',[],forwarded={
        'nodes':[{'path':[0,0],'images':[{'type':'image','data':{'file':str(n)}} for n in range(128)]}]})
        for i in range(3)]
    try:
        with pytest.raises(WorkPaused):
            engine._forward_image_results(sources,{'id':'request','payload':{'messages':[]}})
        job=engine.queue.claim(now.timestamp(),kinds=['forward-images'])
        assert job['payload']['note_chars']*384 <= REQUEST_NOTE_CHARS
    finally:
        store.close()
