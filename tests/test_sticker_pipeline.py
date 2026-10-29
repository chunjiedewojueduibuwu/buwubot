import json
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from test_engine import ModelsFixture, QQFixture, build
from test_sticker_classification import assessment

from buwubot.models import Completion
from buwubot.stickers import StickerCandidate

NOW=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))


def asset_for(engine):
    output=BytesIO()
    Image.new('RGB',(8,8),'red').save(output,format='PNG')
    return engine.media.save(output.getvalue())


async def test_reconfiguration_does_not_automatically_rescan_existing_sticker_library(tmp_path, monkeypatch):
    import buwubot.engine as runtime
    store, engine, _, _ = build(tmp_path, lambda: NOW)
    asset = asset_for(engine)
    await engine.coordinator.commit(lambda: engine.stickers.commit(StickerCandidate(
        asset_hash=asset['hash'], category='赞同', meaning='用户已经整理', is_sticker=True, confidence=1)))
    original = engine.stickers.list()
    engine.channels = runtime.ChannelRegistry()
    engine.channels.register('qq', lambda _settings: QQFixture())
    monkeypatch.setattr(runtime, 'ModelClient', lambda settings, **kwargs: ModelsFixture())
    try:
        await engine._configure()
        assert not store.rows("SELECT id FROM jobs WHERE kind='sticker'")
        assert engine.stickers.list() == original
    finally:
        await engine.close()
        store.close()


async def test_image_reading_queues_separate_classifier_then_primary_adopts(tmp_path):
    store,engine,_qq,models=build(tmp_path,lambda:NOW)
    asset=asset_for(engine)
    async def acquire(*_args):return asset
    engine.media.acquire=acquire
    engine.ingest({'post_type':'message','message_type':'group','group_id':123,'self_id':11,
        'user_id':22,'message_id':1,'time':NOW.timestamp(),'message':[{'type':'image','data':{'file':'fixture'}}]})
    job=engine.queue.claim(NOW.timestamp(),kinds=['image'])
    job['payload']['analysis']=assessment(asset_hash=asset['hash'],is_sticker=False,
        description='画面可读',visible_text='文字')
    engine.queue.payload(job['id'],job['payload'])
    calls=[]
    async def call(role,payload,**_kwargs):
        calls.append(role)
        if role=='vision':return Completion('fixture','glm-fixture',assessment())
        if role=='memory':return Completion('fixture',engine.settings.primary_model,{'memories':[]})
        assert role=='primary'
        data=json.loads(payload[1]['content'][0]['text'])
        assert data['classification']['category']=='赞同'
        return Completion('fixture',engine.settings.primary_model,{'action':'collect','reason':'含义明确'})
    models.call=call
    try:
        await engine.process(job)
        assert calls==[] and not engine.stickers.list()
        classifier=engine.queue.claim(NOW.timestamp(),kinds=['sticker'])
        assert classifier and engine.queue.get(job['id'])['state']=='succeeded'
        await engine.process(classifier)
        assert calls==['vision','primary']
        assert engine.stickers.list()[0]['category']=='赞同'
        assert engine.stickers.list()[0]['tags']==['动漫','像素风']
    finally:store.close()


@pytest.mark.parametrize('decision',['keep','remove'])
async def test_existing_catalog_reclassification_preserves_manual_edits_and_age(tmp_path,decision):
    store,engine,_qq,models=build(tmp_path,lambda:NOW)
    asset=asset_for(engine)
    seed=StickerCandidate(asset_hash=asset['hash'],category='动物萌图',meaning='旧泛类',is_sticker=True,confidence=1)
    await engine.coordinator.commit(lambda:engine.stickers.commit(seed,now=10))
    async def call(role,_payload,**_kwargs):
        return Completion('fixture',engine.settings.primary_model,
            assessment(image_kind='reaction' if decision=='keep' else 'photo') if role=='vision'
            else {'action':decision,'reason':'已核对图片用途'})
    models.call=call
    try:
        engine.enqueue_sticker_reviews()
        job=engine.queue.claim(NOW.timestamp(),kinds=['sticker'])
        assert job
        await engine.process(job)
        if decision=='keep':
            assert engine.stickers.list()[0]['collected_at']==10
            assert engine.stickers.list()[0]['category']=='赞同'
            await engine.coordinator.commit(lambda:engine.stickers.edit(asset['hash'],{'category':'人工选择'},2))
        else:assert not engine.stickers.list()
        engine.enqueue_sticker_reviews()
        assert engine.queue.claim(NOW.timestamp(),kinds=['sticker']) is None
        assert engine.media.path(asset['hash']).is_file()
    finally:store.close()


@pytest.mark.parametrize('blocked',['manual','disabled'])
async def test_sticker_work_cannot_run_for_disabled_group_or_over_manual_change(tmp_path,blocked):
    store,engine,_qq,models=build(tmp_path,lambda:NOW)
    asset=asset_for(engine)
    seed=StickerCandidate(asset_hash=asset['hash'],category='旧类',meaning='明确',is_sticker=True,confidence=1)
    await engine.coordinator.commit(lambda:engine.stickers.commit(seed,now=10))
    async def forbidden(*_args,**_kwargs):pytest.fail('manual edit or disabled scope must stop old classifier')
    models.call=forbidden
    try:
        engine.enqueue_sticker_reviews()
        job=engine.queue.claim(NOW.timestamp(),kinds=['sticker'])
        if blocked=='manual':
            await engine.coordinator.commit(lambda:engine.stickers.edit(asset['hash'],{'category':'人工选择'},1))
        else:
            engine.settings.qq_enabled=False
        await engine.process(job)
        assert engine.queue.get(job['id'])['state']==('skipped_expired' if blocked=='manual' else 'retry_wait')
        assert engine.stickers.list()[0]['category']==('人工选择' if blocked=='manual' else '旧类')
    finally:store.close()


async def test_classifier_restart_uses_effect_receipt_after_commit_before_job_finish(tmp_path):
    store,engine,_qq,models=build(tmp_path,lambda:NOW)
    asset=asset_for(engine)
    seed=StickerCandidate(asset_hash=asset['hash'],category='旧类',meaning='明确',is_sticker=True,confidence=1)
    await engine.coordinator.commit(lambda:engine.stickers.commit(seed,now=10))
    async def call(role,_payload,**_kwargs):
        return Completion('fixture',engine.settings.primary_model,
            assessment() if role=='vision' else {'action':'keep','reason':'含义明确'})
    models.call=call
    engine.enqueue_sticker_reviews()
    job=engine.queue.claim(NOW.timestamp(),kinds=['sticker'])
    original_commit=engine.coordinator.commit
    async def crash_after_commit(operation,**kwargs):
        result=await original_commit(operation,**kwargs)
        assert result['outcome']=='updated'
        raise OSError('interrupted after durable commit')
    engine.coordinator.commit=crash_after_commit
    with pytest.raises(OSError):
        await engine.process(job)
    assert engine.stickers.list()[0]['revision']==2
    store.close()
    store,engine,_qq,models=build(tmp_path,lambda:NOW)
    async def forbidden(*_args,**_kwargs):pytest.fail('applied sticker must not be classified or committed twice')
    models.call=forbidden
    try:
        await engine.process(engine.queue.get(job['id']))
        assert engine.queue.get(job['id'])['state']=='succeeded'
        assert engine.stickers.list()[0]['revision']==2 and engine.stickers.list()[0]['collected_at']==10
    finally:store.close()
