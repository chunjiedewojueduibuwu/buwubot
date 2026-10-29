import sqlite3
import time
from io import BytesIO

import pytest
from PIL import Image

from buwubot.config import Settings
from buwubot.coordinator import Coordinator
from buwubot.sticker_classification import StickerAssessment
from buwubot.stickers import MediaStore, StickerService
from buwubot.store import Store


def setup(tmp_path):
    store=Store(tmp_path/'bot.db')
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id='123',self_id='11',sticker_per_category=2,sticker_total=5)
    media=MediaStore(store,tmp_path/'media')
    output=BytesIO()
    Image.new('RGB',(8,8),'red').save(output,format='GIF')
    asset=media.save(output.getvalue())
    service=StickerService(store,cfg,media)
    candidate=StickerAssessment(asset_hash=asset['hash'],image_kind='reaction',category='点赞',
        meaning='认可',tags=['动漫'],is_sticker=True,confidence=.96,context_sensitive=False)
    return store,service,Coordinator(store,None,cfg),candidate,output.getvalue()


async def test_reclassify_preserves_fifo_source_file_and_receipt_survives_restart(tmp_path):
    store,service,coordinator,candidate,original=setup(tmp_path)
    await coordinator.commit(lambda:service.commit(candidate,now=10))
    updated=candidate.model_copy(update={'category':'开心'})
    async def apply():
        return await coordinator.commit(lambda:service.apply_classification(updated,action='keep',
            expected_revision=1,started_at=20,operation_id='operation',policy_version='v1'))
    first=await apply()
    assert first['outcome']=='updated'
    row=service.list()[0]
    assert row['category']=='开心' and row['collected_at']==10 and row['group_id']=='123' and row['revision']==2
    assert store.get_state(f'sticker_policy:11:{candidate.asset_hash}')=={'version':'v1','revision':2}
    assert service.media.path(candidate.asset_hash).read_bytes()==original
    store.close()
    store=Store(tmp_path/'bot.db')
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id='123',self_id='11')
    service=StickerService(store,cfg,MediaStore(store,tmp_path/'media'))
    coordinator=Coordinator(store,None,cfg)
    assert await apply()==first and service.list()[0]['revision']==2
    store.close()


@pytest.mark.parametrize('manual',['edit','delete'])
async def test_manual_changes_win_over_old_auxiliary_work(tmp_path,manual):
    store,service,coordinator,candidate,_=setup(tmp_path)
    await coordinator.commit(lambda:service.commit(candidate,now=10))
    started=time.time()-1
    if manual=='edit':
        await coordinator.commit(lambda:service.edit(candidate.asset_hash,{'category':'人工分类'},1))
    else:
        await coordinator.commit(lambda:service.delete(candidate.asset_hash,1))
    result=await coordinator.commit(lambda:service.apply_classification(candidate,action='keep',
        expected_revision=1,started_at=started,operation_id='old-work'))
    assert result['outcome']=='superseded'
    assert [row['category'] for row in service.list()]==(['人工分类'] if manual=='edit' else [])
    # An older incoming image must not resurrect a manually removed item either.
    if manual=='delete':
        result=await coordinator.commit(lambda:service.apply_classification(candidate,action='collect',
            expected_revision=None,started_at=started,operation_id='old-incoming'))
        assert result['outcome']=='superseded' and not service.list()
    store.close()


async def test_confirmed_nonreaction_leaves_catalog_but_preserves_image(tmp_path):
    store,service,coordinator,candidate,original=setup(tmp_path)
    await coordinator.commit(lambda:service.commit(candidate,now=10))
    photo=StickerAssessment.model_validate(candidate.model_dump()|{'image_kind':'photo','is_sticker':False})
    result=await coordinator.commit(lambda:service.apply_classification(photo,action='remove',
        expected_revision=1,started_at=time.time(),operation_id='remove-photo'))
    assert result['outcome']=='removed' and not service.list()
    assert service.media.path(candidate.asset_hash).read_bytes()==original
    store.close()


async def test_failed_receipt_is_atomic_and_account_scope_is_preserved(tmp_path):
    store,service,coordinator,candidate,_=setup(tmp_path)
    await coordinator.commit(lambda:service.commit(candidate,now=10))
    store.connection.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON state "
        "WHEN NEW.key LIKE 'sticker_effect:%' BEGIN SELECT RAISE(ABORT,'receipt failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await coordinator.commit(lambda:service.apply_classification(candidate.model_copy(update={'category':'开心'}),
            action='keep',expected_revision=1,started_at=time.time(),operation_id='failed'))
    assert service.list()[0]['category']=='赞同' and service.list()[0]['revision']==1
    store.connection.execute('DROP TRIGGER fail_receipt')
    service.settings=service.settings.model_copy(update={'self_id':'other'})
    result=await coordinator.commit(lambda:service.apply_classification(candidate,action='remove',
        expected_revision=1,started_at=time.time(),operation_id='other-account'))
    assert result['outcome']=='superseded'
    assert store.rows('SELECT bot_id FROM stickers')[0]['bot_id']=='11'
    store.close()
