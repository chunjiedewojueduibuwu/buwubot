import asyncio
import json

import httpx
import pytest
from test_engine import build
from test_sticker_classification import assessment
from test_sticker_pipeline import NOW, asset_for

from buwubot.conversations import RecallEvent
from buwubot.coordinator import WorkPaused
from buwubot.models import Completion, HealthModelClient, ModelClient
from buwubot.stickers import StickerCandidate


@pytest.mark.parametrize('kind',['scan','memory'])
@pytest.mark.parametrize('lane',['direct','healthy','recovering'])
async def test_foreground_or_memory_preempts_sticker_primary_slot_even_if_transport_ignores_cancel(tmp_path,kind,lane):
    store,engine,_qq,_models=build(tmp_path,lambda:NOW)
    asset=asset_for(engine)
    await engine.coordinator.commit(lambda:engine.stickers.commit(StickerCandidate(asset_hash=asset['hash'],
        category='旧类',meaning='明确',confidence=1,is_sticker=True),now=10))
    source=engine.ingest({'post_type':'message','message_type':'group','group_id':123,'self_id':11,
        'user_id':22,'message_id':1,'time':NOW.timestamp(),'message':[{'type':'text','data':{'text':'普通发言'}}]})
    engine.enqueue_sticker_reviews()
    sticker=engine.queue.claim(NOW.timestamp(),kinds=['sticker'])
    sticker['payload']['classification']=assessment(asset_hash=asset['hash'])
    engine.queue.payload(sticker['id'],sticker['payload'])
    started,release,entered=asyncio.Event(),asyncio.Event(),asyncio.Event()
    async def handler(request):
        body=json.loads(request.content)
        if '决定是否采纳表情分类' in body['messages'][0]['content']:
            started.set()
            try:await release.wait()
            except asyncio.CancelledError:await release.wait()
            data={'action':'keep','reason':'明确'}
        else:
            entered.set()
            data={'replies':[],'memories':[],'silence_reason':'fixture'}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(data)}}]})
    http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client=ModelClient(engine.settings,http=http,before_primary=engine.pause_sticker_adoption)
    engine.models=client if lane=='direct' else HealthModelClient(client)
    if lane=='recovering':
        engine.models.states['primary'].update(status='unavailable',retry_at=0)
    engine.memory.models=engine.models
    background=asyncio.create_task(engine.process(sticker))
    try:
        await asyncio.wait_for(started.wait(),1)
        jid=engine.queue.enqueue(kind,{'group_id':'123','persona_id':'assistant','messages':[vars(source)]},
            dedupe_key='higher-priority',priority=1,source_time=NOW.timestamp())
        await asyncio.wait_for(engine.process(engine.queue.get(jid)),1)
        assert entered.is_set()
        with pytest.raises(WorkPaused):await background
        assert engine.stickers.list()[0]['category']=='旧类'
    finally:
        release.set()
        await asyncio.gather(background,return_exceptions=True)
        await client.close()
        await http.aclose()
        store.close()


async def test_recall_during_primary_decision_prevents_formal_collection(tmp_path):
    store,engine,_qq,models=build(tmp_path,lambda:NOW)
    asset=asset_for(engine)
    source=engine.ingest({'post_type':'message','message_type':'group','group_id':123,'self_id':11,
        'user_id':22,'message_id':1,'time':NOW.timestamp(),'message':[{'type':'image','data':{'file':'fixture'}}]})
    async def call(role,_payload,**_kwargs):
        if role=='vision':return Completion('fixture','glm-fixture',assessment())
        engine.ingest(RecallEvent('123',source.message_id))
        return Completion('fixture',engine.settings.primary_model,{'action':'collect','reason':'明确'})
    models.call=call
    jid=engine.queue.enqueue('sticker',{'group_id':'123','bot_id':'11','source_id':source.id,
        'asset_hash':asset['hash'],'mode':'collect','expected_revision':None,'context':'',
        'started_at':NOW.timestamp()},dedupe_key='recall-race',priority=2,source_time=NOW.timestamp())
    try:
        await engine.process(engine.queue.get(jid))
        assert not engine.stickers.list()
        assert engine.queue.get(jid)['state']=='skipped_expired'
    finally:store.close()


async def test_pending_reply_waiting_for_material_does_not_starve_sticker_adoption(tmp_path):
    store,engine,_qq,models=build(tmp_path,lambda:NOW)
    asset=asset_for(engine)
    await engine.coordinator.commit(lambda:engine.stickers.commit(StickerCandidate(asset_hash=asset['hash'],
        category='旧类',meaning='明确',confidence=1,is_sticker=True),now=10))
    engine.queue.enqueue('reply',{'group_id':'123','messages':[]},dedupe_key='waiting-for-material',
                         priority=0,source_time=NOW.timestamp())
    engine.enqueue_sticker_reviews()
    job=engine.queue.claim(NOW.timestamp(),kinds=['sticker'])
    async def call(role,_payload,**_kwargs):
        return Completion('fixture',engine.settings.primary_model,
            assessment() if role=='vision' else {'action':'keep','reason':'明确'})
    models.call=call
    try:
        await engine.process(job)
        assert engine.stickers.list()[0]['category']=='赞同'
    finally:store.close()
