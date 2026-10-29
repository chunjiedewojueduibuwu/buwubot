from datetime import datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest

from buwubot.channels import ChannelError, ChannelRegistry, SendUncertain, builtin_channels
from buwubot.config import Settings
from buwubot.coordinator import Coordinator
from buwubot.engine import BotEngine
from buwubot.memory import MemoryService
from buwubot.qq import QQAdapter, QQError
from buwubot.stickers import MediaStore, StickerService
from buwubot.store import Store
from buwubot.tools import ToolRegistry


async def test_only_qq_is_a_builtin_channel_and_error_identity_is_preserved():
    registry = builtin_channels()
    assert registry.names == ("qq",)
    adapter = registry.create("qq", Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']))
    assert isinstance(adapter, QQAdapter) and adapter.plugin_id == "qq"
    assert {"quote", "history", "members", "images"} <= adapter.capabilities
    assert QQError is ChannelError
    assert issubclass(SendUncertain, ChannelError)
    with pytest.raises(ValueError):
        registry.create("desktop", Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']))
    await adapter.close()


async def test_engine_reconfigures_registered_channel_without_replacing_persona_or_jobs(tmp_path):
    store = Store(tmp_path / "bot.db")
    cfg = Settings(qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], runtime_enabled=False, persona="same persona")
    store.save_settings(cfg, store.settings()[1])
    created = []
    class Adapter:
        plugin_id = "qq"
        connected = False
        capabilities = frozenset()
        def __init__(self, settings):
            self.persona_id = settings.persona_id
            self.closed = False
            created.append(self)
        async def close(self):
            self.closed = True
    registry = ChannelRegistry()
    registry.register("qq", Adapter)
    with pytest.raises(ValueError):
        registry.register("qq", Adapter)
    with pytest.raises(ValueError):
        registry.register("../bad", Adapter)
    memory = MemoryService(store, cfg)
    media = MediaStore(store, tmp_path / "media")
    coordinator = Coordinator(store, None, cfg)
    engine = BotEngine(store, coordinator, memory, media, StickerService(store,cfg,media),
        ToolRegistry(), tmp_path, channels=registry,
        clock=lambda:datetime(2026,10,22,10,tzinfo=ZoneInfo("Asia/Shanghai")))
    job = engine.queue.enqueue("reply", {"fixture":True}, dedupe_key="keep-me",priority=0,source_time=1)
    try:
        await engine._configure()
        assert engine.qq is created[0] and coordinator.qq is created[0]
        await engine._configure()
        assert created[0].closed and engine.qq is created[1]
        assert all(item.persona_id == cfg.persona_id for item in created)
        assert engine.queue.get(job)["payload"] == {"fixture":True}
        assert engine.settings.persona == "same persona"
    finally:
        await engine.close()
        store.close()
    assert created[-1].closed



def test_canonical_events_and_future_namespace_do_not_need_a_qq_transport(tmp_path):
    from buwubot.conversations import ConversationRef, Message, MessageArchive, RecallEvent
    from buwubot.qq import MessageArchive as LegacyArchive
    assert LegacyArchive is MessageArchive
    ref = ConversationRef("private","owner",channel_id="fixture")
    assert ref.storage_key == "fixture:private:owner"
    assert ConversationRef.from_key(ref.storage_key) == ref
    assert ConversationRef("group","123").storage_key == "123"
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], self_id="11").model_copy(update={"group_id":ref.storage_key})
    store = Store(tmp_path/"bot.db")
    archive = MessageArchive(store)
    event = Message("fixture:message:1","1",ref.storage_key,"fixture:user","11",1,"person","person","hello",[])
    assert archive.accept(event,cfg).id == event.id
    assert archive.accept(event,cfg) is None
    archive.accept(RecallEvent(ref.storage_key,"1"),cfg)
    assert archive.get(event.id).recalled
    assert len(archive.recent(ref.storage_key)) == 1
    store.close()


def test_qq_normalization_is_declared_by_the_plugin_before_archiving():
    from buwubot.conversations import Message, RecallEvent
    registry = builtin_channels()
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",self_id="11")
    event = {"post_type":"message","message_type":"group","group_id":"123","message_id":"1",
             "self_id":"11","user_id":"22","message":[{"type":"text","data":{"text":"hello"}}]}
    assert isinstance(registry.normalize("qq",event,cfg),Message)
    recall = registry.normalize("qq",{"notice_type":"group_recall","group_id":"123","message_id":"1"},cfg)
    assert recall == RecallEvent("123","1")
    wrapped = ChannelRegistry()
    wrapped.register("qq",lambda settings:QQAdapter(settings),decoder=QQAdapter.normalize_event)
    assert wrapped.normalize("qq",event,cfg).id == "123:id:1"


async def test_qq_adapter_rejects_other_channel_targets_before_network():
    import httpx

    from buwubot.conversations import ConversationRef
    async def handler(request):
        raise AssertionError("a different channel target must not reach QQ")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']),client=http)
        target = ConversationRef("private","owner",channel_id="fixture")
        with pytest.raises(ChannelError):
            await adapter.send_to(target,[])
        with pytest.raises(ChannelError):
            await adapter.history_for(target)


async def test_qq_image_fetch_keeps_native_metadata_and_redirect_policy_inside_plugin():
    import httpx

    seen = []
    async def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/get_image":
            return httpx.Response(200,json={"data":{"url":"https://qpic.cn/image"}})
        assert "authorization" not in request.headers
        return httpx.Response(302,headers={"location":"http://127.0.0.1/private"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], qq_token="private-fixture"),client=http)
        with pytest.raises(ValueError,match="redirect"):
            await adapter.fetch_image({"data":{"file":"native-id"}},max_bytes=100)
    assert len(seen) == 2
    assert seen[-1] == "https://qpic.cn/image"


@pytest.mark.parametrize('status', [400, 401, 403, 404, 410])
async def test_qq_image_refreshes_expired_embedded_url_once(status):
    import json

    import httpx

    seen = []
    async def handler(request):
        seen.append(request.url.path)
        if request.url.path == '/old':
            return httpx.Response(status)
        if request.url.path == '/get_image':
            assert json.loads(request.content) == {'file':'native-id'}
            return httpx.Response(200, json={'data':{'url':'https://qpic.cn/new'}})
        assert request.url.path == '/new'
        assert 'authorization' not in request.headers
        return httpx.Response(200, content=b'image')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], qq_token='fixture'), client=http)
        assert await adapter.fetch_image({'data':{'file':'native-id',
            'url':'https://qpic.cn/old'}}, max_bytes=100) == b'image'
    assert seen == ['/old', '/get_image', '/new']


@pytest.mark.parametrize('case,paths,error', [
    ('same', ['/old', '/get_image'], 'http'),
    ('still-expired', ['/old', '/get_image', '/new'], 'http'),
    ('unsafe', ['/old', '/get_image'], 'url'),
    ('limited', ['/old'], 'http'),
    ('no-file', ['/old'], 'http'),
    ('native-only', ['/get_image', '/new'], 'http'),
])
async def test_qq_image_refresh_is_bounded_and_keeps_download_guards(case, paths, error):
    import httpx

    seen = []
    async def handler(request):
        seen.append(request.url.path)
        if request.url.path == '/get_image':
            target = {'same':'https://qpic.cn/old', 'unsafe':'http://127.0.0.1/private'}.get(
                case, 'https://qpic.cn/new')
            return httpx.Response(200, json={'data':{'url':target}})
        return httpx.Response(429 if case == 'limited' else 403)
    data = {'file':'native-id', 'url':'https://qpic.cn/old'}
    if case == 'no-file':
        data.pop('file')
    if case == 'native-only':
        data.pop('url')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']), client=http)
        with pytest.raises(ValueError if error == 'url' else httpx.HTTPStatusError):
            await adapter.fetch_image({'data':data}, max_bytes=100)
    assert seen == paths


async def test_core_media_can_acquire_bytes_from_a_non_qq_channel(tmp_path):
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB",(8,8),"red").save(buffer,format="PNG")
    class Adapter:
        async def fetch_image(self, segment, *, max_bytes):
            assert segment == {"fixture":"image"} and max_bytes == MediaStore.MAX_BYTES
            return buffer.getvalue()
    store = Store(tmp_path/"bot.db")
    media = MediaStore(store,tmp_path/"media")
    result = await media.acquire({"fixture":"image"},Adapter())
    assert media.path(result["hash"]).is_file()
    store.close()


async def test_qq_media_uses_separate_transport_without_onebot_credentials():
    import httpx
    seen = []
    async def local(request):
        assert request.url.host == "127.0.0.1"
        assert request.headers["authorization"] == "Bearer local-only"
        return httpx.Response(200, json={"data":{"url":"https://qpic.cn/image"}})
    async def public(request):
        seen.append(request.url.host)
        assert request.url.host == "qpic.cn"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"image-bytes")
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(local)) as local_http,
        httpx.AsyncClient(transport=httpx.MockTransport(public)) as media_http,
    ):
        adapter = QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], qq_token="local-only"), client=local_http, media_client=media_http)
        assert await adapter.fetch_image({"data":{"file":"native-id"}}, max_bytes=100) == b"image-bytes"
        await adapter.close()
        assert not local_http.is_closed and not media_http.is_closed
    assert seen == ["qpic.cn"]


def test_activity_manifests_come_from_registered_plugins_without_constructing_them():
    calls=[]
    class Fixture:
        management: ClassVar[dict[str, str]] = {"name":"测试活动范围","description":"测试独立设置页。","enabled_setting":"qq_enabled"}
        capabilities=frozenset({"text"})
        connected=True
        def __init__(self,settings):calls.append(settings)
    registry=ChannelRegistry()
    registry.register("fixture",Fixture)
    cfg=Settings(runtime_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], qq_enabled=False)
    first=registry.manifests(cfg)
    assert not calls
    assert first==[{"id":"fixture","name":"测试活动范围","kind":"activity","page_id":"fixture",
        "description":"测试独立设置页。","installed":True,"enabled":False,"connected":False,"capabilities":["text"],"settings_sections":[]}]
    registry.create("fixture",cfg)
    assert registry.manifests(cfg)[0]["connected"]
    cfg.qq_enabled=True
    assert registry.manifests(cfg)[0]["enabled"]
    assert len(calls)==1
