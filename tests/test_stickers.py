import base64
import hashlib
from io import BytesIO

import pytest
from PIL import Image

from buwubot.config import Settings
from buwubot.coordinator import Coordinator
from buwubot.stickers import MediaStore, StickerCandidate, StickerService
from buwubot.store import Store


def image_bytes(color):
    output = BytesIO()
    Image.new("RGB", (16, 16), color).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize('state', ['present', 'missing', 'malformed', 'unregistered', 'escaped', 'none'])
def test_cached_asset_reuses_only_registered_files_inside_media_directory(tmp_path, state):
    store = Store(tmp_path / 'bot.db')
    media = MediaStore(store, tmp_path / 'media')
    asset = media.save(image_bytes('red'))
    digest = asset['hash']
    if state == 'missing':
        media.path(digest).unlink()
    elif state == 'malformed':
        digest = '../outside'
    elif state == 'unregistered':
        digest = 'f' * 64
    elif state == 'escaped':
        with store.transaction() as db:
            db.execute('UPDATE media SET filename=? WHERE hash=?', ('../outside.png', digest))
        (tmp_path / 'outside.png').write_bytes(image_bytes('red'))
    elif state == 'none':
        digest = None
    try:
        assert media.cached_asset(digest) == ({'hash':digest} if state == 'present' else None)
    finally:
        store.close()


@pytest.mark.parametrize('wrapped',[False,True])
async def test_image_analysis_cache_tracks_model_and_prompt_changes(tmp_path,monkeypatch,wrapped):
    from types import SimpleNamespace

    from buwubot import stickers as module
    from buwubot.models import Completion

    store = Store(tmp_path / 'bot.db')
    media = MediaStore(store,tmp_path / 'media')
    asset = media.save(image_bytes('red'))
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], auxiliary_model='old-vision')
    class Models:
        calls = 0
        async def call(self,*args,**kwargs):
            self.calls += 1
            return Completion('',cfg.auxiliary_model,{'category':'other','meaning':'red',
                'confidence':1,'is_sticker':False,'description':str(self.calls)})
    models = Models()
    if wrapped:
        models.client = SimpleNamespace(settings=cfg)
    else:
        models.settings = cfg
    try:
        first = await media.analyze(asset['hash'],'same context',models,scope_key='123')
        assert await media.analyze(asset['hash'],'same context',models,scope_key='123') == first
        cfg.auxiliary_model = 'new-vision'
        second = await media.analyze(asset['hash'],'same context',models,scope_key='123')
        assert models.calls == 2 and second['description'] != first['description']
        monkeypatch.setattr(module,'VISION_PROMPT',module.VISION_PROMPT+'\nUpdated analysis rule.')
        await media.analyze(asset['hash'],'same context',models,scope_key='123')
        assert models.calls == 3
    finally:
        store.close()


async def test_sticker_caps_replace_oldest_without_double_deletion(tmp_path):
    store = Store(tmp_path / "bot.db")
    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", sticker_per_category=2, sticker_total=3)
    media = MediaStore(store, tmp_path / "media")
    stickers = StickerService(store, settings, media)
    coordinator = Coordinator(store, None, settings)
    collected = []
    for i, (color, category) in enumerate([("red", "开心"), ("green", "开心"),
                                           ("blue", "疑问"), ("yellow", "开心")]):
        asset = media.save(image_bytes(color))
        candidate = StickerCandidate(asset_hash=asset["hash"], category=category,
                                     meaning="明确表达开心或疑问", confidence=0.99,
                                     is_sticker=True, tags=[category])
        collected.append(await coordinator.commit(lambda c=candidate, n=i: stickers.commit(c, now=n)))
    actual = stickers.list()
    assert len(actual) == 3
    assert {item["id"] for item in actual} == set(collected[1:])
    assert len([item for item in actual if item["category"] == "开心"]) == 2
    store.close()


async def test_duplicates_do_not_refresh_age_and_ambiguous_images_are_not_collected(tmp_path):
    store = Store(tmp_path / "bot.db")
    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123")
    media = MediaStore(store, tmp_path / "media")
    service = StickerService(store, settings, media)
    coordinator = Coordinator(store, None, settings)
    asset = media.save(image_bytes("red"))
    candidate = StickerCandidate(asset_hash=asset["hash"], category="开心", meaning="开心",
                                 confidence=0.99, is_sticker=True)
    first = await coordinator.commit(lambda: service.commit(candidate, now=10))
    assert await coordinator.commit(lambda: service.commit(candidate, now=20)) == first
    assert service.list()[0]["collected_at"] == 10
    ambiguous = candidate.model_copy(update={"context_sensitive": True})
    assert await coordinator.commit(lambda: service.commit(ambiguous)) is None
    assert len(service.list()) == 1
    store.close()


def test_media_rejects_bad_images_and_preserves_hash_checked_original(tmp_path):
    store = Store(tmp_path / "bot.db")
    media = MediaStore(store, tmp_path / "media")
    with pytest.raises(ValueError, match="image"):
        media.save(b"<script>not an image</script>")
    content = image_bytes("blue")
    asset = media.save(content)
    assert asset["hash"] == hashlib.sha256(content).hexdigest()
    assert media.path(asset["hash"]).read_bytes() == content
    store.close()


def test_animated_sticker_samples_first_middle_last_without_changing_original(tmp_path):
    frames = [Image.new("RGB", (16, 16), color)
              for color in ("red", "yellow", "green", "cyan", "blue")]
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    content = output.getvalue()
    store = Store(tmp_path / "bot.db")
    try:
        media = MediaStore(store, tmp_path / "media")
        asset = media.save(content)
        parts = media.vision_parts(asset["hash"])
        colors = []
        for part in parts:
            encoded = part["image_url"]["url"].split(",", 1)[1]
            with Image.open(BytesIO(base64.b64decode(encoded))) as frame:
                colors.append(frame.convert("RGB").getpixel((0, 0)))
        assert colors == [(255, 0, 0), (0, 128, 0), (0, 0, 255)]
        path = media.path(asset["hash"])
        assert asset["mime"] == "image/gif" and path.read_bytes() == content
        with Image.open(path) as original:
            assert original.n_frames == 5 and original.info["loop"] == 0
    finally:
        store.close()


async def test_media_retention_pins_stickers_and_unfinished_job_snapshots(tmp_path):
    store = Store(tmp_path / "bot.db")
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123")
    media = MediaStore(store, tmp_path / "media")
    stickers = StickerService(store, cfg, media)
    coordinator = Coordinator(store, None, cfg)
    assets = [media.save(image_bytes(color))["hash"] for color in ("red","green","blue")]
    await coordinator.commit(lambda:stickers.commit(StickerCandidate(asset_hash=assets[0],
        category="开心",meaning="开心",confidence=1,is_sticker=True)))
    coordinator.queue.enqueue("image", {"asset_hash":assets[1]}, dedupe_key="unfinished-image",
                              priority=2,source_time=0)
    with store.transaction() as db:
        db.execute("UPDATE media SET last_seen=0")
    removable = media.path(assets[2])
    assert media.cleanup(now=8*86400, retention_days=7) == 1
    assert not removable.exists()
    assert media.path(assets[0]).exists() and media.path(assets[1]).exists()
    store.close()


async def test_collection_and_fifo_eviction_are_one_atomic_commit(tmp_path):
    import sqlite3

    store=Store(tmp_path/"bot.db")
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",sticker_per_category=1,sticker_total=1)
    media=MediaStore(store,tmp_path/"media")
    service=StickerService(store,cfg,media)
    coordinator=Coordinator(store,None,cfg)
    assets=[media.save(image_bytes(color))["hash"] for color in ("red","green")]
    def candidate(digest):
        return StickerCandidate(asset_hash=digest,category="开心",meaning="开心",is_sticker=True,confidence=1)

    await coordinator.commit(lambda:service.commit(candidate(assets[0]),now=1))
    store.connection.execute("CREATE TRIGGER injected_failure BEFORE DELETE ON stickers "
                             "BEGIN SELECT RAISE(ABORT,'injected eviction failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await coordinator.commit(lambda:service.commit(candidate(assets[1]),now=2))
    assert [item["id"] for item in service.list()]==[assets[0]]
    store.close()


@pytest.mark.parametrize("legacy", [False, True])
async def test_account_stickers_are_shared_across_groups_with_original_source(tmp_path, legacy):
    store = Store(tmp_path / "bot.db")
    media = MediaStore(store, tmp_path / "media")
    if legacy:
        store.connection.execute("CREATE TABLE stickers(id TEXT PRIMARY KEY REFERENCES media(hash),"
            "group_id TEXT NOT NULL,category TEXT NOT NULL,meaning TEXT NOT NULL,tags TEXT NOT NULL,"
            "collected_at REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 1)")
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    service = StickerService(store, cfg, media)
    coordinator = Coordinator(store, None, cfg)
    asset = media.save(image_bytes("blue"))["hash"]
    candidate = StickerCandidate(asset_hash=asset, category="开心", meaning="开心", is_sticker=True, confidence=1)
    await coordinator.commit(lambda: service.commit(candidate, now=1))
    service.settings = cfg.model_copy(update={"group_id":"456"})
    assert service.list()[0]["group_id"] == "123"
    await coordinator.commit(lambda: service.commit(candidate, now=2))
    assert len(service.list()) == 1 and service.list()[0]["collected_at"] == 1
    await coordinator.commit(lambda: service.edit(asset, {"meaning":"高兴"}, 1))
    service.settings = cfg
    assert service.list()[0]["meaning"] == "高兴"
    service.settings = cfg.model_copy(update={"self_id":"99"})
    assert service.list() == []
    await coordinator.commit(lambda: service.commit(candidate, now=3))
    await coordinator.commit(lambda: service.delete(asset, 1))
    service.settings = cfg
    assert service.list()[0]["meaning"] == "高兴"
    await coordinator.commit(lambda: service.delete(asset, 2))
    assert not service.list()
    store.close()


async def test_image_analysis_cache_does_not_carry_private_context_into_another_scope(tmp_path):
    from buwubot.models import Completion
    store=Store(tmp_path/"bot.db")
    media=MediaStore(store,tmp_path/"media")
    asset=media.save(image_bytes("blue"))
    calls=[]
    class Vision:
        async def call(self,role,payload,**kwargs):
            context=payload[1]["content"][0]["text"]
            calls.append(context)
            return Completion("", "auxiliary", {"is_sticker":False,"confidence":1,
                "category":"other","meaning":context,"description":context,"visible_text":""})
    models=Vision()
    try:
        private=await media.analyze(asset["hash"],"private context",models,scope_key="private:10001")
        public=await media.analyze(asset["hash"],"group context",models,scope_key="123")
        cached=await media.analyze(asset["hash"],"group context",models,scope_key="123")
        assert private["description"]=="private context"
        assert public["description"]==cached["description"]=="group context"
        assert calls==["private context","group context"]
    finally:store.close()


async def test_global_sticker_caps_count_unique_files_and_keep_age_across_scopes(tmp_path):
    from copy import copy
    store=Store(tmp_path/"bot.db")
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",group_ids=["456"],sticker_per_category=2,sticker_total=3)
    media=MediaStore(store,tmp_path/"media")
    a=StickerService(store,cfg,media)
    b=copy(a);b.settings=cfg.scoped("456")
    c=Coordinator(store,None,cfg)
    assets=[media.save(image_bytes(color))["hash"] for color in ["red","green","blue","yellow"]]
    def item(digest,category="happy"):
        return StickerCandidate(asset_hash=digest,category=category,meaning="clear",confidence=1,is_sticker=True)
    try:
        await c.commit(lambda:a.commit(item(assets[0]),now=1))
        await c.commit(lambda:b.commit(item(assets[0]),now=20))
        assert b.list()[0]["collected_at"]==1
        await c.commit(lambda:b.commit(item(assets[1]),now=2))
        await c.commit(lambda:a.commit(item(assets[2],"question"),now=3))
        assert not a.eviction_ids()  # The two scope rows for one file consume one slot.
        await c.commit(lambda:b.commit(item(assets[3]),now=4))
        assert not store.rows("SELECT * FROM stickers WHERE id=?",(assets[0],))
        assert {r["id"] for r in store.rows("SELECT DISTINCT id FROM stickers")}==set(assets[1:])
        remove=a.eviction_ids(total=1)
        assert set(remove)==set(assets[1:3])
    finally:store.close()


async def test_existing_group_collections_migrate_once_preserving_first_meaning(tmp_path):
    store = Store(tmp_path / "bot.db")
    media = MediaStore(store, tmp_path / "media")
    asset = media.save(image_bytes("blue"))["hash"]
    store.connection.execute("CREATE TABLE stickers(id TEXT NOT NULL REFERENCES media(hash),"
        "group_id TEXT NOT NULL,category TEXT NOT NULL,meaning TEXT NOT NULL,tags TEXT NOT NULL,"
        "collected_at REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(group_id,id))")
    store.connection.executemany("INSERT INTO stickers VALUES(?,?,?,?,?,?,?)", [
        (asset,"456","开心","later","[]",2,1), (asset,"123","开心","first","[]",1,3)])
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="456", self_id="11")
    service = StickerService(store, cfg, media)
    assert len(service.list()) == 1
    assert service.list()[0]["meaning"] == "first"
    assert service.list()[0]["revision"] == 3
    assert service.list()[0]["collected_at"] == 1
    other = StickerService(store, cfg.model_copy(update={"self_id":"99"}), media)
    assert other.list() == []
    assert len(service.list()) == 1
    store.close()
