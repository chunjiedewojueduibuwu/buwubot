from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from buwubot.config import Settings
from buwubot.coordinator import Coordinator, JobQueue
from buwubot.engine import BotEngine
from buwubot.memory import MemoryService
from buwubot.models import Completion
from buwubot.qq import MessageArchive
from buwubot.stickers import MediaStore, StickerService
from buwubot.store import Store
from buwubot.tools import ToolRegistry


class QQFixture:
    def __init__(self):
        self.connected = True
        self.sent = []

    async def send(self, group_id, segments, reply_id=None):
        self.sent.append((group_id, segments, reply_id))
        return "ack-1"

    async def history(self, *args, **kwargs):
        return []

    async def close(self):
        pass


class ModelsFixture:
    def __init__(self):
        self.calls = []

    async def call(self, role, payload, **kwargs):
        self.calls.append(role)
        if role == "planning":
            data = {"replies": [{"source_message_ids": ["123:id:1"],
                                  "reply_source_id": "123:id:1", "reason": "被叫到"}]}
        elif role == "chat":
            data = {"text": "你好呀", "sticker_id": None, "tool": None}
        elif role in {"auxiliary", "memory"}:
            data = {"memories": []}
        else:
            data = {"terms": [], "ids": []}
        return Completion("fixture", "cn:deepseek-v4.1-flash", data)

    async def close(self):
        pass


async def test_backfill_uses_plugin_history_cursor_instead_of_assuming_sequence(tmp_path):
    now = datetime(2026, 10, 21, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    calls = []
    def message(n):
        return {'message_id': -n, 'message_seq': n + 10000, 'history_cursor': str(-n),
            'time': now.timestamp() - 300 + n, 'user_id': 22, 'group_id': 123,
            'sender': {'nickname': 'fixture'}, 'message': [{'type': 'text', 'data': {'text': 'hello'}}]}
    async def history(group_id, *, before=None):
        calls.append(before)
        if before is None:
            return [message(n) for n in range(100, 200)]
        assert before == '-100'
        return [message(99)]
    qq.history = history
    from buwubot.conversations import ConversationRef
    try:
        await engine._backfill_conversation(ConversationRef('group', '123'))
        assert calls == [None, '-100']
        assert len(store.rows('SELECT id FROM messages')) == 101
    finally:
        store.close()


def build(tmp_path, clock):
    store = Store(tmp_path / "bot.db")
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11", persona="友善群友", workbuddy_key="fixture")
    _, revision = store.settings()
    store.save_settings(cfg, revision)
    qq = QQFixture()
    coordinator = Coordinator(store, qq, cfg)
    memory = MemoryService(store, cfg)
    media = MediaStore(store, tmp_path / "media")
    stickers = StickerService(store, cfg, media)
    models = ModelsFixture()
    engine = BotEngine(store, coordinator, memory, media, stickers, ToolRegistry(), tmp_path,
                       clock=clock, boot_time=datetime(2026, 10, 21, 6, tzinfo=ZoneInfo("Asia/Shanghai")))
    engine.qq, engine.models = qq, models
    memory.models = models
    return store, engine, qq, models


async def test_pending_tool_reopens_and_sends_once_through_coordinator(tmp_path):
    from io import BytesIO

    from PIL import Image

    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
    asset = engine.media.save(buffer.getvalue())
    calls = []
    async def pending(payload):
        calls.append(payload)
        return {"state":"pending", "operation_id":"remote-1"}
    engine.tools.handlers["image_generate"] = pending
    jid = engine.queue.enqueue("tool", {"group_id":"123", "messages":[], "reply_id":"100",
        "persona_id":"assistant", "tool_name":"image_generate", "tool_args":{"prompt":"猫"}},
        dedupe_key="tool-test", priority=1, source_time=1)
    await engine.process(engine.queue.get(jid))
    assert engine.queue.get(jid)["state"] == "pending"
    assert engine.queue.get(jid)["next_attempt"] == now.timestamp()+30
    assert engine.queue.get(jid)["payload"]["operation_id"] == "remote-1"
    assert not qq.sent
    store.close()
    store, engine, qq, models = build(tmp_path, lambda: now)
    async def succeeded(payload):
        calls.append(payload)
        return {"state":"succeeded", "message":"测试任务完成", "images":[asset["hash"]]}
    engine.tools.handlers["image_generate"] = succeeded
    await engine.process(engine.queue.get(jid))
    await engine.process(engine.queue.get(jid))
    assert calls[-1]["operation_id"] == "remote-1"
    assert calls[-1]["request_id"] == calls[0]["request_id"] == jid
    assert len(qq.sent) == 1
    assert qq.sent[0][2] == "100"
    assert qq.sent[0][1][1] == {"type":"image", "data":{"file":engine.media.path(asset["hash"]).as_uri()}}
    assert "chat" in models.calls
    assert engine.queue.get(jid)["state"] == "succeeded"
    store.close()


async def test_enabled_tool_is_delegated_and_failed_result_still_uses_primary(tmp_path):
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    called = []
    class Models(ModelsFixture):
        async def call(self, role, payload, **kwargs):
            if role == "chat":
                if isinstance(payload[-1]["content"], list):
                    return Completion("fixture", "ds", {"tool":"image_generate",
                        "tool_args":{"prompt":"猫"}})
                assert 'failed' in payload[-1]["content"]
                return Completion("fixture", "ds", {"text":"这次绘图失败了"})
            return await super().call(role, payload, **kwargs)
    async def handler(payload):
        called.append(payload)
        return {"state":"failed", "message":"明确的终止失败"}
    engine.models = Models()
    engine.memory.models = engine.models
    engine.tools.handlers["image_generate"] = handler
    jid = engine.queue.enqueue("reply", {"group_id":"123", "messages":[],
        "segments":[{"type":"text","data":{"text":"旧人格的草稿"}}], "persona_hash":"old"},
        dedupe_key="tool-routing", priority=0, source_time=1)
    await engine.process(engine.queue.get(jid))
    assert not called and not qq.sent
    tool = engine.queue.claim(now.timestamp(), kinds=["tool"])
    assert "segments" not in tool["payload"]
    await engine.process(tool)
    assert called[0]["prompt"] == "猫"
    assert qq.sent[0][1][0]["data"]["text"] == "这次绘图失败了"
    assert engine.queue.get(tool["id"])["state"] == "succeeded"
    store.close()


async def test_sent_commitment_survives_raw_retention_and_persona_switch(tmp_path):
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, _, _ = build(tmp_path, lambda: now)
    class Models(ModelsFixture):
        async def call(self, role, payload, **kwargs):
            import json
            if role in {"auxiliary", "memory"}:
                data = json.loads(payload[-1]["content"])
                return Completion("fixture", engine.settings.primary_model, {"memories":[{
                    "subject_id":"", "content":"助手承诺记住这个约定",
                    "source_message_ids":[data["messages"][-1]["id"]]}]})
            if role == "review":
                return Completion("fixture", "glm", {"approved":True})
            return await super().call(role, payload, **kwargs)
    engine.memory.models = Models()
    jid = engine.queue.enqueue("reply", {"group_id":"123", "messages":[],
        "persona_id":engine.settings.persona_id, "persona_hash":engine.draft_fingerprint(engine.settings)}, dedupe_key="promise", priority=0, source_time=1)
    await engine.coordinator.send(jid, [{"type":"text","data":{"text":"我会记住这个约定"}}], now=100)
    job = engine.queue.claim(now.timestamp(), kinds=["memory"])
    engine.settings = engine.settings.model_copy(update={"persona_id":"other"})
    await engine._memory_job(job)
    assert engine.queue.get(job["id"])["state"] == "retry_wait"
    assert not store.rows("SELECT id FROM memories")
    engine.settings = engine.memory.settings
    await engine._memory_job(engine.queue.get(job["id"]))
    engine.memory.cleanup(now=now.timestamp() + 8 * 86400)
    store.close()
    reopened = Store(tmp_path / "bot.db")
    row = reopened.rows("SELECT content,persona_id,source_ids FROM memories")[0]
    assert row["content"] == "助手承诺记住这个约定"
    assert row["persona_id"] == "assistant"
    assert "outgoing:" + jid in row["source_ids"]
    reopened.close()


async def test_received_call_goes_through_durable_scan_and_native_reply(tmp_path):
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, _models = build(tmp_path, lambda: now)
    engine.ingest({"post_type": "message", "message_type": "group", "group_id": 123,
                   "self_id": 11, "user_id": 22, "message_id": 1, "time": now.timestamp(),
                   "sender": {"nickname": "甲"}, "message": [
                       {"type": "at", "data": {"qq": "11"}},
                       {"type": "text", "data": {"text": "你好"}}]})
    await engine.plan_pending()
    queue = JobQueue(store)
    scan = queue.claim(now.timestamp(), kinds=["scan"])
    assert scan is not None
    await engine.process(scan)
    reply = queue.claim(now.timestamp(), kinds=["reply"])
    assert reply["priority"] == 0
    await engine.process(reply)
    assert qq.sent == [("123", [{"type": "text", "data": {"text": "你好呀"}}], "1")]
    assert queue.get(reply["id"])["state"] == "succeeded"
    store.close()


async def test_confirmed_self_reply_survives_restart_and_api_change_in_next_context(tmp_path):
    import json
    now=datetime(2026,10,21,9,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,_qq,_models=build(tmp_path,lambda:now)
    async def reply(number,text):
        msg=engine.ingest({"post_type":"message","message_type":"group","group_id":123,
            "self_id":11,"user_id":22,"message_id":number,"time":now.timestamp(),
            "message":[{"type":"text","data":{"text":text}}]})
        jid=engine.queue.enqueue("reply",{"group_id":"123","messages":[vars(msg)],"reply_id":msg.message_id,
            "reply_source_id":msg.id,"source_message_ids":[msg.id]},dedupe_key=f"context:{number}",priority=0,source_time=now.timestamp())
        await engine.process(engine.queue.get(jid))
    class First:
        async def call(self,role,payload,**kwargs):
            return Completion("","cn:deepseek-v4.1-flash",{"text":"代号是松风","tool":None,"sticker_id":None} if role=="chat" else {"terms":[]})
    engine.models=First();engine.memory.models=engine.models
    await reply(1,"请给这次测试取个代号")
    store.close()
    now=now.replace(minute=1)
    store,engine,qq,_models=build(tmp_path,lambda:now)
    cfg,revision=store.settings()
    cfg=cfg.model_copy(update={"workbuddy_url":"http://127.0.0.1:9000/v1"})
    store.save_settings(cfg,revision);engine.settings=cfg
    class Second:
        async def call(self,role,payload,**kwargs):
            if role=="chat":
                context=json.loads(payload[1]["content"][0]["text"])["recent_chat"]
                assert any(x.get("role")=="assistant" and x.get("text")=="代号是松风" for x in context)
                return Completion("","cn:deepseek-v4.1-flash",{"text":"刚才是松风","tool":None,"sticker_id":None})
            return Completion("","cn:deepseek-v4.1-flash",{"terms":[]})
    engine.models=Second();engine.memory.models=engine.models
    try:
        await reply(2,"刚才取的代号是什么")
        assert qq.sent[0][1][0]["data"]["text"]=="刚才是松风"
    finally:store.close()


async def test_learning_yields_to_owner_call_and_resumes_only_when_idle(tmp_path):
    import asyncio

    from test_learning import URL, SourceFixture
    from test_learning import ModelsFixture as LearningModels

    from buwubot.coordinator import WorkPaused
    from buwubot.learning import LearningService

    now=datetime(2026,10,21,9,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,_qq,_models=build(tmp_path,lambda:now)
    cfg,revision=store.settings()
    cfg=cfg.model_copy(update={"learning_enabled":True})
    store.save_settings(cfg,revision);engine.settings=cfg
    async def provider(_):
        return {'state':'succeeded','data':{}}
    for name in ('video_read','video_discover'):
        engine.tools.register(name,provider,description='fixture',input_schema={'type':'object'})
    models=LearningModels();models.release=asyncio.Event()
    service=LearningService(store,engine.coordinator,engine.memory,models,SourceFixture(),allowed=engine.learning_allowed)
    engine.learning=service
    item=service.enqueue(URL)
    try:
        task=asyncio.create_task(service.step(item))
        await asyncio.wait_for(models.started.wait(), 2)
        engine.ingest({"post_type":"message","message_type":"private","self_id":11,
            "user_id":10001,"message_id":2,"time":now.timestamp(),
            "sender":{"nickname":"owner"},"message":[{"type":"text","data":{"text":"助手在吗"}}]})
        with pytest.raises(WorkPaused):await asyncio.wait_for(task,.5)
        assert service.get(item)["state"]=="paused"
        assert not engine.learning_allowed()
        now=now.replace(hour=10)
        assert engine.learning_allowed()
        job=engine.queue.claim(now.timestamp(),kinds=["learning"])
        models.release.set()
        await engine.process(job)
        assert service.get(item)["cursor_seconds"]==60
    finally:
        await service.close();store.close()


async def test_night_pause_rejects_reads_even_when_called_and_no_late_morning(tmp_path):
    now = datetime(2026, 10, 21, 23, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, models = build(tmp_path, lambda: now)
    engine.ingest({"post_type": "message", "message_type": "group", "group_id": 123,
                   "self_id": 11, "user_id": 22, "message_id": 1, "time": now.timestamp(),
                   "message": [{"type": "at", "data": {"qq": "11"}}]})
    assert MessageArchive(store).recent("123") == []
    assert not engine.allowed() and not engine.qq_allowed()
    assert engine.status()["mode"] == "night_pause"
    assert engine.status()["qq_mode"] == "night_pause"
    now = datetime(2026, 10, 22, 13, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert JobQueue(store).claim(now.timestamp()) is None
    assert not qq.sent
    assert not models.calls
    store.close()


async def test_model_ignoring_called_message_cannot_silently_drop_it(tmp_path):
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, _qq, _models = build(tmp_path, lambda: now)
    cfg = engine.settings
    m = engine.memory.archive.accept({"post_type": "message", "message_type": "group",
        "group_id": 123, "self_id": 11, "user_id": 22, "message_id": 1, "time": now.timestamp(),
        "sender": {"nickname": "甲"}, "message": [{"type": "at", "data": {"qq": "11"}}]}, cfg)
    replies = engine.validate_plan({"replies": []}, [m], now.timestamp())
    assert replies[0]["source_message_ids"] == ["123:id:1"]
    with pytest.raises(ValueError, match="source"):
        engine.validate_plan({"replies": [{"source_message_ids": ["invented"]}]}, [m], now.timestamp())
    store.close()


async def test_core_memory_crossing_bedtime_keeps_proposal_until_wakeup(tmp_path):
    now = datetime(2026, 10, 21, 22, 29, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, _qq, _models = build(tmp_path, lambda: now)
    source = engine.ingest({"post_type": "message", "message_type": "group", "group_id": 123,
        "self_id": 11, "user_id": 22, "message_id": 1, "time": now.timestamp(),
        "sender": {"nickname": "甲"}, "message": [{"type": "text", "data": {"text": "我爱喝茶"}}]})

    async def delayed_proposal(messages,**kwargs):
        nonlocal now
        from buwubot.memory import MemoryProposal
        now = now.replace(hour=22, minute=31)
        return [MemoryProposal(group_id="123", subject_id="22", content="甲爱喝茶",
                               source_message_ids=[source.id])]

    engine.memory.propose = delayed_proposal
    jid = engine.queue.enqueue("memory", {"group_id": "123", "messages": [vars(source)]},
                               dedupe_key="cross-bedtime", priority=2, source_time=source.time)
    job = engine.queue.claim(now.timestamp())
    from buwubot.coordinator import WorkPaused
    with pytest.raises(WorkPaused):
        await engine.process(job)
    assert not engine.allowed() and not engine.qq_allowed()
    assert not store.rows("SELECT * FROM memories")
    assert engine.queue.get(jid)["payload"]["proposals"]
    now=now.replace(day=22,hour=8,minute=0)
    await engine.process(engine.queue.get(jid))
    assert len(store.rows("SELECT * FROM memories")) == 1
    assert engine.queue.get(jid)["state"] == "succeeded"
    assert not _qq.sent
    store.close()


@pytest.mark.parametrize('manual,partial,race,committed,nonoverlap',[
    (False,False,False,False,False),(True,False,False,False,False),
    (False,True,False,False,False),(False,False,True,False,False),
    (False,False,False,True,False),(True,False,False,False,True)])
async def test_stale_unapplied_ai_memory_replans_without_overwriting_newer_or_manual_record(
        tmp_path,manual,partial,race,committed,nonoverlap):
    from buwubot.memory import MemoryProposal
    from buwubot.store import Conflict

    now = datetime(2026,10,21,9,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,_,_ = build(tmp_path,lambda:now)
    message = engine.ingest({'post_type':'message','message_type':'group','group_id':123,
        'self_id':11,'user_id':22,'message_id':1,'time':now.timestamp(),
        'sender':{'nickname':'fixture'},'message':[{'type':'text','data':{'text':'我喜欢茶'}}]})
    original = MemoryProposal(group_id='123',subject_id='22',content='喜欢茶',tier='recent',
        source_message_ids=[message.id],snapshot_epoch=engine.memory.epoch())
    mid = await engine.coordinator.commit(lambda:engine.memory.commit(original))
    stale = original.model_copy(update={'memory_id':mid,'expected_revision':1,'content':'喜欢茶，也谈过咖啡'})
    if nonoverlap:
        other = engine.ingest({'post_type':'message','message_type':'group','group_id':123,
            'self_id':11,'user_id':22,'message_id':2,'time':now.timestamp(),
            'sender':{'nickname':'fixture'},'message':[{'type':'text','data':{'text':'也谈过咖啡'}}]})
        stale = stale.model_copy(update={'source_message_ids':[other.id]})
    if manual:
        await engine.coordinator.commit(lambda:engine.memory.edit(mid,expected_revision=1,content='人工修订'))
    else:
        newer = stale if committed else stale.model_copy(update={'content':'喜欢茶，当前已有更完整记录'})
        await engine.coordinator.commit(lambda:engine.memory.commit(newer,reviewed=True))
    before = engine.memory.get(mid)
    async def review(*args):
        return True,'supported'
    replans = []
    async def propose(sources,**kwargs):
        replans.append([m.id for m in sources])
        assert engine.memory.get(mid)['revision'] == 2
        if race:
            await engine.coordinator.commit(lambda:engine.memory.edit(
                mid,expected_revision=2,content='重新判断期间的人工修订'))
            return [stale.model_copy(update={'expected_revision':3,
                'snapshot_epoch':engine.memory.epoch()})]
        return []
    engine.memory.review,engine.memory.propose = review,propose
    jid = engine.queue.enqueue('memory',{'group_id':'123','messages':[vars(message)],
        'proposals':[stale.model_dump()] if not partial else [original.model_dump(),stale.model_dump()],
        'applied':[] if not partial else [0]},dedupe_key='stale-memory',priority=2,source_time=1)
    try:
        if committed:
            await engine.process(engine.queue.get(jid))
            assert engine.queue.get(jid)['state'] == 'succeeded' and not replans
        elif manual or partial:
            with pytest.raises(Conflict):
                await engine.process(engine.queue.get(jid))
            assert 'proposals' in engine.queue.get(jid)['payload'] and not replans
        else:
            await engine.process(engine.queue.get(jid))
            pending = engine.queue.get(jid)
            assert pending['state'] == 'pending' and 'proposals' not in pending['payload']
            if race:
                with pytest.raises(Conflict):
                    await engine.process(pending)
                assert engine.memory.get(mid)['content'] == '重新判断期间的人工修订'
            else:
                await engine.process(pending)
                assert engine.queue.get(jid)['state'] == 'succeeded'
            assert replans == [[message.id]]
        if not race:
            assert engine.memory.get(mid) == before
    finally:
        store.close()


async def test_invalid_memory_shape_keeps_original_task_and_retries_after_backoff(tmp_path):
    import asyncio

    now = datetime(2026,10,21,9,tzinfo=ZoneInfo('Asia/Shanghai'))
    store,engine,_,models = build(tmp_path,lambda:now)
    message = engine.ingest({'post_type':'message','message_type':'group','group_id':123,
        'self_id':11,'user_id':22,'message_id':1,'time':now.timestamp(),
        'sender':{'nickname':'fixture'},'message':[{'type':'text','data':{'text':'我喜欢茶'}}]})
    data = {}
    async def call(*args,**kwargs):
        return Completion('',engine.settings.primary_model,data)
    models.call = call
    jid = engine.queue.enqueue('memory',{'group_id':'123','messages':[vars(message)]},
        dedupe_key='invalid-memory-shape',priority=2,source_time=1)
    worker = asyncio.create_task(engine._worker(['memory']))
    try:
        async with asyncio.timeout(3):
            while engine.queue.get(jid)['state'] != 'retry_wait':
                await asyncio.sleep(.01)
        waiting = engine.queue.get(jid)
        assert waiting['payload']['messages'] == [vars(message)]
        assert 'proposals' not in waiting['payload']
        assert not store.rows('SELECT * FROM memories')
        worker.cancel()
        await asyncio.gather(worker,return_exceptions=True)
        data = {'memories':[]}
        now = datetime.fromtimestamp(waiting['next_attempt']+1,ZoneInfo('Asia/Shanghai'))
        await engine.process(engine.queue.get(jid))
        assert engine.queue.get(jid)['state'] == 'succeeded'
    finally:
        worker.cancel()
        await asyncio.gather(worker,return_exceptions=True)
        store.close()


async def test_reconfigure_stops_old_workers_before_closing_clients_and_resizes_pool(tmp_path, monkeypatch):
    import asyncio

    import buwubot.engine as runtime

    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, _qq, _models = build(tmp_path, lambda: now)
    entered, exited = asyncio.Event(), asyncio.Event()

    async def old_worker():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    class OldModels(ModelsFixture):
        async def close(self):
            assert exited.is_set(), "client closed while an old worker was still using it"

    engine.models = OldModels()
    worker = asyncio.create_task(old_worker())
    engine._workers = [worker]
    await entered.wait()
    cfg, revision = store.settings()
    cfg.persona = ""  # Remain in setup: no external calls in this lifecycle test.
    cfg.qq_enabled = False
    cfg.background_concurrency = 3
    store.save_settings(cfg, revision)
    engine.channels = runtime.ChannelRegistry()
    engine.channels.register("qq", lambda settings: QQFixture())
    monkeypatch.setattr(runtime, "ModelClient", lambda settings, allowed, core_memory, before_primary: ModelsFixture())
    engine.configuration_changed.set()
    await engine._configure()
    assert worker.cancelled()
    assert len(engine._workers) == 9  # foreground, index, tool/image pool, memory, QQ reads, stickers and learner
    assert sum(task.get_name()=="bot-learning" for task in engine._workers)==1
    assert not engine.configuration_changed.is_set()
    assert not next(t for t in engine.tools.definitions() if t['name'] == 'qq_operation')['enabled']
    cfg, revision = store.settings()
    cfg.qq_enabled = True
    store.save_settings(cfg, revision)
    await engine._configure()
    assert next(t for t in engine.tools.definitions() if t['name'] == 'qq_operation')['enabled']
    await engine.close()
    assert all(task.done() for task in engine._workers)
    store.close()


async def test_unreadable_image_does_not_block_an_otherwise_valid_reply(tmp_path):
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, _models = build(tmp_path, lambda: now)
    source=engine.ingest({"post_type":"message","message_type":"group","group_id":123,
        "self_id":11,"user_id":22,"message_id":1,"time":now.timestamp(),
        "message":[{"type":"at","data":{"qq":"11"}},
                   {"type":"image","data":{"url":"https://qpic.cn/broken"}}]})

    async def broken_image(*args):
        raise ValueError("invalid image file")

    engine.media.acquire=broken_image
    jid=engine.queue.enqueue("reply",{"group_id":"123","messages":[vars(source)],
        "reply_id":"1","reply_source_id":source.id,"source_message_ids":[source.id]},
        dedupe_key="unreadable-image",priority=0,source_time=now.timestamp())
    await engine.process(engine.queue.get(jid))
    assert len(qq.sent)==1
    assert engine.queue.get(jid)["state"]=="succeeded"
    store.close()


@pytest.mark.parametrize('missing_file', [False, True])
async def test_image_retry_after_restart_reuses_download_checkpoint(tmp_path, missing_file):
    from io import BytesIO

    from PIL import Image

    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, _, _ = build(tmp_path, lambda: now)
    image = BytesIO()
    Image.new('RGB', (8, 8), 'red').save(image, format='PNG')
    asset = engine.media.save(image.getvalue())
    source = engine.ingest({'post_type':'message', 'message_type':'group', 'group_id':123,
        'self_id':11, 'user_id':22, 'message_id':1, 'time':now.timestamp(),
        'message':[{'type':'image', 'data':{'file':'fixture'}}]})
    jid = store.rows("SELECT id FROM jobs WHERE kind='image'")[0]['id']
    async def acquire(*args):
        return asset
    async def fail_analysis(*args, **kwargs):
        raise OSError('temporary vision failure')
    engine.media.acquire, engine.media.analyze = acquire, fail_analysis
    with pytest.raises(OSError, match='temporary vision failure'):
        await engine._image_job(engine.queue.get(jid))
    assert engine.queue.get(jid)['payload']['asset_hash'] == asset['hash']
    if missing_file:
        engine.media.path(asset['hash']).unlink()
    store.close()

    store, engine, qq, _ = build(tmp_path, lambda: now)
    downloads = []
    async def resume_acquire(*args):
        downloads.append(True)
        if not missing_file:
            raise OSError('original remote image no longer available')
        return engine.media.save(image.getvalue())
    async def analyze(digest, *args, **kwargs):
        assert engine.media.path(digest).is_file()
        return {'description':'red square', 'visible_text':''}
    engine.media.acquire, engine.media.analyze = resume_acquire, analyze
    try:
        await engine._image_job(engine.queue.get(jid))
        assert downloads == ([True] if missing_file else [])
        assert engine.queue.get(jid)['state'] == 'succeeded'
        assert store.rows("SELECT id FROM jobs WHERE dedupe_key=?",
            (f"image-memory:{source.id}:{asset['hash']}",))
        assert not qq.sent
    finally:
        store.close()


@pytest.mark.parametrize('kind,status,header,minimum', [
    ('image', 403, None, 21600), ('image', 404, None, 21600),
    ('image', 410, None, 21600), ('forward-images', 400, None, 21600),
    ('image', 408, None, 30), ('image', 429, '1200', 1200),
    ('image', 503, None, 30), ('reply', 403, None, 30),
])
async def test_http_worker_keeps_status_without_url_and_retains_image_retries(
        tmp_path, kind, status, header, minimum):
    import httpx

    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, _, _ = build(tmp_path, lambda: now)
    jid = engine.queue.enqueue(kind, {'group_id':'123', 'checkpoint':'keep'},
        dedupe_key='http-failure', priority=1, source_time=now.timestamp())
    async def failed(job):
        engine._stopping = True
        request = httpx.Request('GET', 'https://qpic.cn/private?token=do-not-log')
        response = httpx.Response(status, request=request,
            headers={'Retry-After':header} if header else {})
        response.raise_for_status()
    engine.process = failed
    try:
        await engine._worker([kind])
        result = engine.queue.get(jid)
        assert result['state'] == 'retry_wait' and result['payload']['checkpoint'] == 'keep'
        assert str(status) in result['last_error']
        assert 'do-not-log' not in result['last_error'] and 'qpic.cn' not in result['last_error']
        assert minimum <= result['next_attempt'] - now.timestamp() <= minimum + 3
    finally:
        store.close()


async def test_image_reply_uses_auxiliary_description_then_primary_text_decision(tmp_path):
    import json
    from io import BytesIO

    from PIL import Image

    now=datetime(2026,10,21,9,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,qq,_models=build(tmp_path,lambda:now)
    buffer=BytesIO();Image.new("RGB",(16,16),"red").save(buffer,format="PNG")
    asset=engine.media.save(buffer.getvalue())
    async def acquire(*args):return asset
    engine.media.acquire=acquire
    calls=[]
    class VisionThenPrimary:
        async def call(self,role,payload,**kwargs):
            calls.append(role)
            if role=="vision":
                assert any(x.get("type")=="image_url" for x in payload[1]["content"])
                return Completion("","cn:glm-5.3-flash",{"description":"red square","visible_text":"test text",
                    "is_sticker":False,"meaning":"test image","category":"image","tags":[],"confidence":.95,"context_sensitive":False})
            if role=="retrieval":return Completion("","cn:deepseek-v4.1-flash",{"terms":[],"ids":[]})
            assert role=="chat"
            assert all(x.get("type")=="text" for x in payload[1]["content"])
            data=json.loads(payload[1]["content"][0]["text"])
            assert data["image_analyses"][0]["analysis"]["visible_text"]=="test text"
            assert data["image_analyses"][0]["asset_hash"]==asset["hash"]
            return Completion("","cn:deepseek-v4.1-flash",{"text":"red square","tool":None,"sticker_id":None})
    engine.models=VisionThenPrimary();engine.memory.models=engine.models
    msg=engine.ingest({"post_type":"message","message_type":"group","group_id":123,"self_id":11,
        "user_id":22,"message_id":90,"time":now.timestamp(),"message":[{"type":"image","data":{"file":"fixture"}}]})
    jid=engine.queue.enqueue("reply",{"group_id":"123","messages":[vars(msg)],"reply_id":msg.message_id,
        "reply_source_id":msg.id,"source_message_ids":[msg.id]},dedupe_key="vision-reply",priority=0,source_time=now.timestamp())
    await engine.process(engine.queue.get(jid))
    assert calls[0]=="vision" and calls[-1]=="chat"
    assert len(qq.sent)==1
    store.close()


async def test_memory_batches_overlap_at_boundary_without_rescanning_forever(tmp_path):
    now=datetime(2026,10,21,9,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,_qq,_models=build(tmp_path,lambda:now)
    for number in range(1,103):
        engine.ingest({"post_type":"message","message_type":"group","group_id":123,
            "self_id":11,"user_id":22,"message_id":number,"time":now.timestamp(),
            "message":[{"type":"text","data":{"text":f"片段{number}"}}]})
    first,cursor=engine._unprocessed("memory")
    assert len(first)==100
    store.set_state("memory_cursor:123",cursor)
    second,new_cursor=engine._unprocessed("memory")
    assert [m.message_id for m in second]==[str(n) for n in range(96,103)]
    store.set_state("memory_cursor:123",new_cursor)
    assert engine._unprocessed("memory")[0]==[]
    store.close()


async def test_history_reconnect_paginates_past_a_fully_known_latest_page(tmp_path):
    now=datetime(2026,10,21,9,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,_qq,_models=build(tmp_path,lambda:now)

    def record(number):
        return {"post_type":"message","message_type":"group","group_id":123,
            "self_id":11,"user_id":22,"message_id":number,"message_seq":number,
            "time":now.timestamp(),"message":[]}

    latest=[record(n) for n in range(101,201)]
    for item in latest:
        engine.ingest(item)

    class History:
        async def history(self,group_id,before=None):
            return latest if before is None else [record(100)]

    engine.qq=History()
    await engine._backfill()
    assert engine.memory.archive.get("123:seq:100") is not None
    store.close()


async def test_legacy_fixed_greeting_is_retired_even_when_ready(tmp_path):
    now=datetime(2026,10,21,9,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,qq,_models=build(tmp_path,lambda:now)
    jid=engine.queue.enqueue("greeting",{"group_id":"123","kind":"morning",
        "date":str(now.date()),"text":"legacy fixed greeting"},dedupe_key="legacy",priority=-1,source_time=1)
    await engine.process(engine.queue.get(jid))
    assert qq.sent==[]
    assert engine.queue.get(jid)["state"]=="skipped_expired"
    store.close()


async def test_partial_scan_replay_reuses_its_durable_plan_and_receipt_order(tmp_path):
    now=datetime(2026,10,21,9,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,_qq,models=build(tmp_path,lambda:now)
    for number in (1,2):
        engine.ingest({"post_type":"message","message_type":"group","group_id":123,
            "self_id":11,"user_id":22,"message_id":number,"time":now.timestamp(),
            "message":[{"type":"at","data":{"qq":"11"}},{"type":"text","data":{"text":f"话题{number}"}}]})
    calls=0
    async def planning(*args,**kwargs):
        nonlocal calls
        calls+=1
        if calls>1:
            raise AssertionError("a persisted partial scan must not regenerate a different plan")
        return Completion("fixture","ds",{"replies":[{"source_message_ids":[f"123:id:{n}"]} for n in (2,1)]})

    models.call=planning
    await engine.plan_pending()
    scan=engine.queue.claim(now.timestamp(),kinds=["scan"])
    original=engine.queue.enqueue
    inserted=0
    def interrupted_enqueue(kind,*args,**kwargs):
        nonlocal inserted
        if kind=="reply":
            inserted+=1
            if inserted==2:
                raise OSError("simulated interrupted second enqueue")
        return original(kind,*args,**kwargs)

    engine.queue.enqueue=interrupted_enqueue
    with pytest.raises(OSError):
        await engine.process(scan)
    engine.queue.enqueue=original
    await engine.process(engine.queue.get(scan["id"]))
    replies=store.rows("SELECT payload FROM jobs WHERE kind='reply' ORDER BY priority,source_time,created_at")
    import json
    assert [json.loads(row[0])["reply_id"] for row in replies]==["1","2"]
    assert calls==1
    store.close()


async def test_auxiliary_sticker_suggestion_requires_primary_approval(tmp_path):
    from io import BytesIO

    from PIL import Image

    now=datetime(2026,10,21,10,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,_qq,_models=build(tmp_path,lambda:now)
    buffer=BytesIO()
    Image.new("RGB",(16,16),"red").save(buffer,format="PNG")
    asset=engine.media.save(buffer.getvalue())
    async def acquire(*args): return asset
    engine.media.acquire=acquire
    m=engine.ingest({"post_type":"message","message_type":"group","group_id":"123",
        "self_id":"11","user_id":"22","message_id":99,"time":now.timestamp(),
        "message":[{"type":"image","data":{"file":"fixture"}}]})
    q=JobQueue(store)
    job=q.claim(now.timestamp(),kinds=["image"])
    job["payload"]["analysis"]={"asset_hash":asset["hash"],"is_sticker":True,"category":"test",
        "meaning":"auxiliary guess","confidence":1,"context_sensitive":False,
        "description":"test","visible_text":""}
    q.payload(job["id"],job["payload"])
    calls=[]
    class RejectingPrimary:
        async def call(self,role,*args,**kwargs):
            calls.append(role)
            if role=='vision':
                return Completion('','aux',{'image_kind':'reaction','is_sticker':True,'category':'赞同',
                    'meaning':'认可','confidence':1,'context_sensitive':False,'tags':[]})
            if role=='memory':
                return Completion('',engine.settings.primary_model,{'memories':[]})
            assert all(part.get("type") == "text" for part in args[0][1]["content"])
            return Completion("",engine.settings.primary_model,{"action":"skip","reason":"暂不收藏"})
    engine.models=RejectingPrimary()
    try:
        await engine.process(job)
        assert not calls
        classifier=q.claim(now.timestamp(),kinds=['sticker'])
        await engine.process(classifier)
        assert calls==["vision","primary"]
        assert not engine.stickers.list()
        assert m is not None
    finally: store.close()


async def test_switching_persona_regenerates_unsent_draft_even_when_core_text_matches(tmp_path):
    now=datetime(2026,10,21,10,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,qq,_=build(tmp_path,lambda:now)
    msg=engine.ingest({"post_type":"message","message_type":"group","group_id":"123",
        "self_id":"11","user_id":"22","message_id":99,"time":now.timestamp(),
        "message":[{"type":"text","data":{"text":"hi"}}]})
    calls=[]
    class Models:
        async def call(self,role,*args,**kwargs):
            if role=="chat":
                calls.append(engine.settings.persona_id)
                return Completion("",engine.settings.primary_model,{"text":engine.settings.persona_id})
            return Completion("",engine.settings.primary_model,{"terms":[],"ids":[]})
    engine.models=Models()
    engine.memory.models=engine.models
    q=JobQueue(store)
    jid=q.enqueue("reply",{"group_id":"123","messages":[vars(msg)]},dedupe_key="draft",priority=0,source_time=1)
    try:
        engine.coordinator.allowed=lambda:False
        await engine.process(q.get(jid))
        assert not qq.sent and q.get(jid)["payload"]["segments"]
        engine.settings=engine.settings.model_copy(update={"persona_id":"another"})
        _, revision = store.settings()
        store.save_settings(engine.settings, revision)
        engine.coordinator.settings=engine.settings
        engine.coordinator.allowed=lambda:True
        await engine.process(q.get(jid))
        assert calls==["assistant","another"]
        assert qq.sent[0][1][0]["data"]["text"]=="another"
    finally:store.close()


async def test_reply_receives_local_current_and_message_times(tmp_path):
    import json
    now = datetime(2026, 10, 22, 12, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, _qq, _models = build(tmp_path, lambda: now)
    message = engine.ingest({"post_type":"message", "message_type":"group", "group_id":123,
        "self_id":11, "user_id":22, "message_id":1, "time":now.timestamp()-60,
        "message":[{"type":"text", "data":{"text":"早安"}}]})
    class Capture(ModelsFixture):
        async def call(self, role, payload, **kwargs):
            if role == "chat":
                data = json.loads(payload[1]["content"][0]["text"])
                assert data["current_time"] == "2026-10-22T12:40:00+08:00"
                assert data["timezone"] == "Asia/Shanghai"
                assert data["targets"][0]["local_time"] == "2026-10-22T12:39:00+08:00"
                assert data["recent_chat"][0]["local_time"] == "2026-10-22T12:39:00+08:00"
            return await super().call(role, payload, **kwargs)
    engine.models = Capture()
    jid = engine.queue.enqueue("reply", {"group_id":"123", "messages":[vars(message)],
        "reply_id":"1", "reply_source_id":message.id, "source_message_ids":[message.id]},
        dedupe_key="local-time", priority=0, source_time=message.time)
    await engine.process(engine.queue.get(jid))
    assert "local_time" not in vars(message)
    store.close()


async def test_text_followup_receives_recent_same_conversation_image_analysis(tmp_path):
    import json
    from io import BytesIO

    from PIL import Image
    now = datetime(2026,10,22,13,tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, _ = build(tmp_path, lambda:now)
    image = engine.ingest({"post_type":"message","message_type":"group","group_id":123,
        "self_id":11,"user_id":22,"message_id":1,"time":now.timestamp()-2,
        "message":[{"type":"image","data":{"file":"fixture"}}]})
    followup = engine.ingest({"post_type":"message","message_type":"group","group_id":123,
        "self_id":11,"user_id":22,"message_id":2,"time":now.timestamp(),
        "message":[{"type":"text","data":{"text":"助手，这张图写了什么"}}]})
    from buwubot.conversations import RecallEvent
    for number, scope, timestamp in [(3,"999",now.timestamp()-1),
                                     (4,"123",now.timestamp()+1), (5,"123",now.timestamp()-1)]:
        engine.memory.archive.accept({"post_type":"message","message_type":"group","group_id":int(scope),
            "self_id":11,"user_id":33,"message_id":number,"time":timestamp,
            "message":[{"type":"image","data":{"file":"excluded"}}]},
            engine.settings.model_copy(update={"group_id":scope}))
    engine.memory.archive.accept(RecallEvent("123","5"),engine.settings)
    output=BytesIO();Image.new("RGB",(8,8),"red").save(output,format="PNG")
    asset=engine.media.save(output.getvalue())
    acquired=[]
    async def acquire(segment,channel):
        acquired.append(segment["data"]["file"])
        return asset
    engine.media.acquire=acquire
    class Models(ModelsFixture):
        async def call(self,role,payload,**kwargs):
            if role=="vision":
                return Completion("","aux",{"description":"red","visible_text":"字幕测试",
                    "category":"image","meaning":"image","confidence":1,"is_sticker":False})
            if role=="chat":
                data=json.loads(payload[1]["content"][0]["text"])
                assert len(data["image_analyses"]) == 1
                assert data["image_analyses"][0]["source_id"]==image.id
                assert data["image_analyses"][0]["analysis"]["visible_text"]=="字幕测试"
                assert data["image_analyses"][0]["asset_hash"]==asset["hash"]
                assert data["targets"][0]["id"]==followup.id
            return await super().call(role,payload,**kwargs)
    engine.models=Models();engine.memory.models=engine.models
    image_job = store.rows("SELECT id FROM jobs WHERE kind='image'")[0]["id"]
    await engine.process(engine.queue.get(image_job))
    async def no_redownload(*args):
        raise AssertionError("text follow-up must reuse saved image analysis")
    engine.media.acquire = no_redownload
    jid=engine.queue.enqueue("reply",{"group_id":"123","messages":[vars(followup)],
        "reply_id":followup.message_id,"reply_source_id":followup.id,"source_message_ids":[followup.id]},
        dedupe_key="image-followup",priority=0,source_time=followup.time)
    await engine.process(engine.queue.get(jid))
    assert acquired==["fixture"]
    assert qq.sent[0][2]==followup.message_id
    store.close()


@pytest.mark.parametrize('reverse_history', [False, True])
async def test_old_quoted_image_and_next_three_are_internal_tool_candidates(tmp_path, reverse_history):
    import json
    from io import BytesIO

    from PIL import Image

    from buwubot.conversations import RecallEvent

    now = datetime(2026, 10, 25, 13, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    assets, expected = {}, []
    def picture(number, scope='123', offset=-100):
        stream = BytesIO()
        Image.new('RGB', (8, 8), (number % 255, 20, 30)).save(stream, format='PNG')
        assets[str(number)] = engine.media.save(stream.getvalue())
        event = {'post_type':'message', 'message_type':'group', 'group_id':int(scope),
                 'self_id':11, 'user_id':33, 'message_id':number, 'time':now.timestamp()+offset,
                 'message':[{'type':'image', 'data':{'file':str(number)}}]}
        return engine.memory.archive.accept(event, engine.settings.model_copy(update={'group_id':scope}))
    pictures = {}
    for number in ([104,105,100,103] if reverse_history else [100,103,104,105]):
        pictures[number] = picture(number, offset=-100 if number == 100 else -90+number-103)
    expected = [pictures[number].id for number in (100,103,104,105)]
    picture(101, '999')  # Same neighborhood in another group must not enter.
    picture(102)
    engine.memory.archive.accept(RecallEvent('123', '102'), engine.settings)
    for number in range(20):
        engine.ingest({'post_type':'message', 'message_type':'group', 'group_id':123,
            'self_id':11, 'user_id':22, 'message_id':200+number, 'time':now.timestamp()-40+number,
            'message':[{'type':'text','data':{'text':'普通聊天'}}]})
    picture(999, offset=10)  # Future picture must not enter either.
    msg = engine.ingest({'post_type':'message', 'message_type':'group', 'group_id':123,
        'self_id':11, 'user_id':22, 'message_id':500, 'time':now.timestamp(),
        'message':[{'type':'reply','data':{'id':'100'}},
                   {'type':'text','data':{'text':'助手，从这个和下面三个里选喜欢的当头像'}}]})
    acquired = []
    async def acquire(segment, channel):
        name = segment['data']['file']
        acquired.append(name)
        return assets[name]
    async def analyze(digest, *args, **kwargs):
        return {'description':'候选图片', 'visible_text':'', 'confidence':1}
    engine.media.acquire, engine.media.analyze = acquire, analyze
    class Models(ModelsFixture):
        async def call(self, role, payload, **kwargs):
            if role == 'retrieval':
                assert set(json.loads(payload[-1]['content'])['speaker_ids']) == {'22', '33'}
            if role == 'chat':
                first = isinstance(payload[1]['content'], list)
                data = json.loads(payload[1]['content'][0]['text'] if first else payload[1]['content'])
                images = data['image_analyses']
                assert [i['source_id'] for i in images] == expected
                assert [i['asset_hash'] for i in images] == [assets[str(n)]['hash'] for n in (100,103,104,105)]
                assert data['quoted_messages'][0]['id'] == pictures[100].id
                if first:
                    return Completion('', engine.settings.primary_model,
                        {'tool':'qq_operation','tool_args':{'operation':'get_self'}})
                return Completion('', engine.settings.primary_model,
                    {'tool':'qq_operation','tool_args':{'operation':'update_self',
                     'fields':{'avatar':images[3]['asset_hash']}}})
            return await super().call(role, payload, **kwargs)
    engine.models = Models()
    engine.memory.models = engine.models
    async def profile(args):
        assert args['operation'] == 'get_self'
        return {'state':'succeeded','data':{'user_id':'11'}}
    engine.tools.handlers['qq_operation'] = profile
    jid = engine.queue.enqueue('reply', {'group_id':'123','messages':[vars(msg)],
        'reply_id':msg.message_id,'reply_source_id':msg.id,'source_message_ids':[msg.id]},
        dedupe_key='old-quoted-picture',priority=0,source_time=msg.time)
    await engine.process(engine.queue.get(jid))
    assert acquired == ['100','103','104','105']
    tool_id = store.rows("SELECT id FROM jobs WHERE kind='tool'")[0]['id']
    engine._tool_pause_until = 0  # Advance past the ingestion interruption guard for this continuation.
    await engine.process(engine.queue.get(tool_id))
    tool = engine.queue.get(tool_id)
    assert tool['payload']['tool_args']['fields']['avatar'] == assets['105']['hash']
    assert not qq.sent  # Only inspect tool selection; never change a real avatar as QA.
    store.close()


async def test_reply_without_at_wakes_and_planner_sees_original_but_may_stay_silent(tmp_path):
    import json

    now = datetime(2026, 10, 25, 13, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    original = engine.memory.archive.accept({'post_type':'message','message_type':'group',
        'group_id':123,'self_id':11,'user_id':33,'message_id':1,'time':now.timestamp()-1000,
        'message':[{'type':'text','data':{'text':'感觉很有人味'}}]},engine.settings)
    store.set_state('last_spoke', now.timestamp()-3600)
    msg = engine.ingest({'post_type':'message','message_type':'group','group_id':123,
        'self_id':11,'user_id':22,'message_id':2,'time':now.timestamp(),
        'message':[{'type':'reply','data':{'id':'1'}},
                   {'type':'text','data':{'text':'这位是测试群友甲，他在夸你呢'}}]})
    assert not msg.called and not msg.name_mentioned
    assert engine._force_scan
    assert store.get_state('last_spoke') == now.timestamp()
    class Models(ModelsFixture):
        async def call(self, role, payload, **kwargs):
            assert role == 'planning'
            data = json.loads(payload[1]['content'])
            assert data['quoted_messages'][0]['id'] == original.id
            assert data['quoted_messages'][0]['user_id'] == '33'
            return Completion('', engine.settings.primary_model, {'replies':[],'silence_reason':'介绍信息已看到'})
    engine.models = Models()
    jid = engine.queue.enqueue('scan',{'group_id':'123','messages':[vars(msg)]},
        dedupe_key='unmentioned-quote',priority=1,source_time=msg.time)
    await engine.process(engine.queue.get(jid))
    assert not store.rows("SELECT id FROM jobs WHERE kind='reply'") and not qq.sent
    store.close()


async def test_name_substring_wakes_scan_but_ds_may_choose_silence(tmp_path):
    now = datetime(2026,10,22,13,tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, _ = build(tmp_path,lambda:now)
    engine._last_scan = now.timestamp()
    class Silent(ModelsFixture):
        async def call(self,role,payload,**kwargs):
            if role == "planning":
                return Completion("","cn:deepseek-v4.1-flash",{"replies":[],"silence_reason":"引用提到名字，无需参与"})
            return await super().call(role,payload,**kwargs)
    engine.models = Silent()
    message = engine.ingest({"post_type":"message","message_type":"group","group_id":123,
        "self_id":11,"user_id":22,"message_id":1,"time":now.timestamp(),
        "message":[{"type":"text","data":{"text":"转述：他说助手今天很安静"}}]})
    assert message.name_mentioned and not message.called
    assert engine.status()["mode"] == "active"
    await engine.plan_pending()
    scan = engine.queue.claim(now.timestamp(),kinds=["scan"])
    assert scan is not None
    await engine.process(scan)
    assert not store.rows("SELECT id FROM jobs WHERE kind='reply'")
    assert not qq.sent
    store.close()


@pytest.mark.parametrize("kind", ["reply", "tool"])
@pytest.mark.parametrize("change_during_generation", [False, True])
async def test_core_edit_invalidates_unsent_draft_without_repeating_tool(
        tmp_path, kind, change_during_generation):
    from buwubot.memory import MemoryProposal, core_scope
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    mid = await engine.coordinator.commit(lambda: engine.memory.commit(MemoryProposal(
        group_id=core_scope("assistant"), persona_id="assistant", tier="core",
        content="称呼旧习惯", source_message_ids=["manual:test"]), manual=True,
        source_snapshots=[{"id":"manual:test","group_id":core_scope("assistant"),"origin":"admin"}]))
    calls, tool_calls = [], []
    async def edit_core():
        await engine.coordinator.commit(lambda: engine.memory.edit(
            mid, content="称呼新习惯", expected_revision=1))
    class Models(ModelsFixture):
        async def call(self, role, payload, **kwargs):
            if role == "chat":
                calls.append(role)
                if change_during_generation and len(calls) == 1:
                    await edit_core()
                return Completion("fixture", "ds", {"text":f"表达版本{len(calls)}"})
            return await super().call(role, payload, **kwargs)
    engine.models = Models()
    engine.memory.models = engine.models
    async def tool(payload):
        tool_calls.append(payload)
        return {"state":"succeeded", "message":"真实工具结果"}
    engine.tools.handlers["image_generate"] = tool
    # Pause final sending once while preserving the generated draft.
    engine.coordinator.send_allowed = lambda key: False
    jid = engine.queue.enqueue(kind, {"group_id":"123", "messages":[],
        "persona_id":"assistant", "tool_name":"image_generate", "tool_args":{}},
        dedupe_key="core-draft", priority=1, source_time=1)
    await engine.process(engine.queue.get(jid))
    assert not qq.sent and len(calls) == 1
    if not change_during_generation:
        await edit_core()
    engine.coordinator.send_allowed = lambda key: True
    await engine.process(engine.queue.get(jid))
    assert len(calls) == 2
    assert qq.sent[0][1][0]["data"]["text"] == "表达版本2"
    assert len(tool_calls) == (1 if kind == "tool" else 0)
    store.close()


@pytest.mark.parametrize("kind", ["reply", "tool"])
async def test_core_change_during_generation_is_held_before_first_send(tmp_path, kind):
    from buwubot.memory import MemoryProposal, core_scope
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    class Models(ModelsFixture):
        async def call(self, role, payload, **kwargs):
            if role == "chat":
                await engine.coordinator.commit(lambda: engine.memory.commit(MemoryProposal(
                    group_id=core_scope("assistant"), persona_id="assistant", tier="core",
                    content="生成期间新增核心习惯", source_message_ids=["manual:concurrent"]), manual=True,
                    source_snapshots=[{"id":"manual:concurrent","group_id":core_scope("assistant"),"origin":"admin"}]))
                return Completion("fixture", "ds", {"text":"已经过期的草稿"})
            return await super().call(role, payload, **kwargs)
    engine.models = Models()
    engine.memory.models = engine.models
    jid = engine.queue.enqueue(kind, {"group_id":"123", "messages":[], "persona_id":"assistant",
        "tool_result":{"state":"succeeded","message":"已完成","images":[]}},
        dedupe_key="core-during", priority=1, source_time=1)
    await engine.process(engine.queue.get(jid))
    assert not qq.sent
    assert engine.queue.get(jid)["state"] == "pending"
    assert not store.rows("SELECT * FROM outgoing")
    store.close()


@pytest.mark.parametrize("kind", ["reply", "tool"])
async def test_core_delete_after_restart_refreshes_draft_and_keeps_completed_result(tmp_path, kind):
    from buwubot.memory import MemoryProposal, core_scope
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    mid = await engine.coordinator.commit(lambda: engine.memory.commit(MemoryProposal(
        group_id=core_scope("assistant"), persona_id="assistant", tier="core",
        content="稍后会删除的核心习惯", source_message_ids=["manual:restart"]), manual=True,
        source_snapshots=[{"id":"manual:restart","group_id":core_scope("assistant"),"origin":"admin"}]))
    engine.coordinator.send_allowed = lambda key: False
    jid = engine.queue.enqueue(kind, {"group_id":"123", "messages":[], "persona_id":"assistant",
        "tool_result":{"state":"succeeded","message":"真实工具结果","images":[]}},
        dedupe_key="core-restart", priority=1, source_time=1)
    await engine.process(engine.queue.get(jid))
    assert engine.queue.get(jid)["payload"]["segments"] and not qq.sent
    store.close()
    store, engine, qq, models = build(tmp_path, lambda: now)
    await engine.coordinator.commit(lambda: engine.memory.delete(mid, expected_revision=1))
    await engine.process(engine.queue.get(jid))
    assert "chat" in models.calls and len(qq.sent) == 1
    assert engine.queue.get(jid)["state"] == "succeeded"
    store.close()


async def test_authoritative_persona_change_during_generation_blocks_stale_send(tmp_path):
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    class Models(ModelsFixture):
        async def call(self, role, payload, **kwargs):
            if role == "chat":
                cfg, revision = store.settings()
                cfg.persona = "管理页已修改的角色设定"
                await engine.coordinator.commit(lambda: store.save_settings(cfg, revision))
                return Completion("fixture", "ds", {"text":"旧人设草稿"})
            return await super().call(role, payload, **kwargs)
    engine.models = Models()
    engine.memory.models = engine.models
    jid = engine.queue.enqueue("reply", {"group_id":"123","messages":[]},
        dedupe_key="persona-authority", priority=1, source_time=1)
    await engine.process(engine.queue.get(jid))
    assert not qq.sent and engine.queue.get(jid)["state"] == "pending"
    store.close()


async def test_uncertain_send_is_reconciled_before_stale_draft_guard(tmp_path):
    import json
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    parts = [{"type":"text","data":{"text":"此前可能已发送"}}]
    jid = engine.queue.enqueue("reply", {"group_id":"123", "messages":[],
        "persona_id":"assistant", "persona_hash":"stale"},
        dedupe_key="uncertain-before-guard", priority=1, source_time=1)
    with store.transaction() as db:
        db.execute("INSERT INTO outgoing VALUES(?,?,?,?,?,'uncertain',NULL)",
                   (jid,"123",None,json.dumps(parts),100))
    async def history(*args, **kwargs):
        return [{"user_id":"11","message_id":"confirmed-old","time":100,"message":parts}]
    qq.history = history
    await engine.coordinator.send(jid,[{"type":"text","data":{"text":"不能重发的新草稿"}}])
    assert not qq.sent and engine.queue.get(jid)["state"] == "succeeded"
    row = store.rows("SELECT * FROM outgoing WHERE job_id=?", (jid,))[0]
    assert row["qq_message_id"] == "confirmed-old" and json.loads(row["segments"]) == parts
    store.close()


async def test_reconnect_recovers_all_history_pages_inside_the_same_second(tmp_path):
    import json

    import httpx

    from buwubot.conversations import ConversationRef
    from buwubot.qq import QQAdapter
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    store, engine, _, _ = build(tmp_path, lambda: now)
    cursors=[]
    async def handler(request):
        body=json.loads(request.content)
        before=int(body.get("message_seq",205))
        cursors.append(body.get("message_seq"))
        records=[{"message_seq":seq,"message_id":seq,"time":now.timestamp()-5,
            "user_id":22,"sender":{"nickname":"验收群友"},
            "message":[{"type":"text","data":{"text":str(seq)}}]}
            for seq in range(min(before,205),max(0,before-100),-1)]
        return httpx.Response(200,json={"data":{"messages":records}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine.qq=QQAdapter(engine.settings,client=client)
        await engine._backfill_conversation(ConversationRef("group","123"))
    assert [row["id"] for row in store.rows("SELECT id FROM messages ORDER BY received_order")] == (
        [f"123:seq:{i}" for i in range(106,206)] +
        [f"123:seq:{i}" for i in range(7,106)] +
        [f"123:seq:{i}" for i in range(1,7)])
    assert cursors == [None,"106","7"]
    assert [m.text for m in engine.memory.archive.recent("123",limit=10)] == [
        str(i) for i in range(196,206)]
    store.close()


async def test_skipped_duplicate_tool_is_terminal_without_repeating_side_effect(tmp_path):
    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, qq, _ = build(tmp_path, lambda: now)
    calls = []
    async def handler(payload):
        calls.append(payload)
        return {'state': 'succeeded', 'message': 'done'}
    engine.tools.handlers['image_generate'] = handler
    jid = engine.queue.enqueue('tool', {'group_id': '123', 'messages': [],
        'persona_id': 'assistant', 'tool_name': 'image_generate', 'tool_args': {}},
        dedupe_key='terminal-duplicate-tool', priority=1, source_time=1)
    engine.queue.finish(jid, 'skipped_duplicate')
    await engine.process(engine.queue.get(jid))
    assert calls == [] and not qq.sent
    assert engine.queue.get(jid)['state'] == 'skipped_duplicate'
    store.close()


async def test_memory_lane_runs_while_images_block_and_context_is_not_starved(tmp_path, monkeypatch):
    import asyncio

    import buwubot.engine as runtime

    now = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, _, _ = build(tmp_path, lambda: now)
    engine.channels = runtime.ChannelRegistry()
    engine.channels.register('qq', lambda settings: QQFixture())
    monkeypatch.setattr(runtime, 'ModelClient', lambda settings, allowed, core_memory, before_primary: ModelsFixture())
    image_started, memory_done, context_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_image = asyncio.Event()
    async def process(job):
        if job['kind'] == 'image':
            image_started.set()
            await release_image.wait()
        elif job['kind'] == 'memory':
            memory_done.set()
        elif job['kind'] == 'context':
            context_done.set()
        engine.queue.finish(job['id'])
    monkeypatch.setattr(engine, 'process', process)
    for i in range(8):
        engine.queue.enqueue('image', {}, dedupe_key=f'image-block-{i}', priority=1, source_time=i)
        engine.queue.enqueue('memory', {}, dedupe_key=f'memory-wait-{i}', priority=2, source_time=i)
    engine.queue.enqueue('context', {}, dedupe_key='context-wait', priority=3, source_time=1)
    try:
        await engine._configure()
        async with asyncio.timeout(3):
            await image_started.wait()
            await memory_done.wait()
            await context_done.wait()
        assert not release_image.is_set()
        assert any(task.get_name() == 'bot-memory' for task in engine._workers)
    finally:
        for task in engine._workers:
            task.cancel()
        await asyncio.gather(*engine._workers, return_exceptions=True)
        if engine.models:
            await engine.models.close()
        if engine.qq:
            await engine.qq.close()
        store.close()


@pytest.mark.parametrize('quoted_source', ['external', 'same_batch', 'missing'])
async def test_quoted_member_memory_requires_review_even_for_secondary_tier(tmp_path, quoted_source):
    from buwubot.memory import ReviewRequired

    now = datetime(2026, 10, 25, 13, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, _, models = build(tmp_path, lambda: now)
    engine.ingest({'post_type':'message', 'message_type':'group', 'group_id':123,
        'self_id':11, 'user_id':44, 'message_id':3, 'time':now.timestamp()-20,
        'message':[{'type':'text','data':{'text':'another member'}}]})
    original = engine.ingest({'post_type':'message', 'message_type':'group', 'group_id':123,
        'self_id':11, 'user_id':33, 'message_id':1, 'time':now.timestamp()-10,
        'message':[{'type':'text','data':{'text':'hello'}}]})
    introduction = engine.ingest({'post_type':'message', 'message_type':'group', 'group_id':123,
        'self_id':11, 'user_id':22, 'message_id':2, 'time':now.timestamp(),
        'message':[{'type':'reply','data':{'id':'1'}},
                   {'type':'text','data':{'text':'This is Mugong'}}]})
    roles = []
    async def call(role, payload, **kwargs):
        roles.append(role)
        data = {'memories':[{'subject_id':'44', 'content':'Wrong member identity',
            'tier':'secondary', 'source_message_ids':[introduction.id]}]} if role in {'auxiliary', 'memory'} else {
            'approved':False, 'reason':'Quoted author does not match the candidate'}
        return Completion('fixture', engine.settings.primary_model, data)
    models.call = call
    sources = [vars(original), vars(introduction)] if quoted_source == 'same_batch' else [vars(introduction)]
    if quoted_source == 'missing':
        with store.transaction() as db:
            db.execute('UPDATE messages SET recalled=1 WHERE id=?', (original.id,))
    jid = engine.queue.enqueue('memory', {'group_id':'123', 'messages':sources,
        'review_feedback':{'reason':'One correction was already attempted'}},
        dedupe_key='quoted-member-review', priority=2, source_time=introduction.time)
    try:
        with pytest.raises(ReviewRequired):
            await engine.process(engine.queue.get(jid))
        assert roles == ['auxiliary', 'memory', 'review']
        assert not store.rows('SELECT id FROM memories')
    finally:
        store.close()


@pytest.mark.parametrize('approved', [True, False])
async def test_memory_review_replans_once_and_reviews_lower_tier_after_restart(tmp_path, approved):
    from buwubot.memory import ReviewRequired

    now = datetime(2026, 10, 25, 13, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, _, models = build(tmp_path, lambda: now)
    source = engine.ingest({'post_type':'message', 'message_type':'group', 'group_id':123,
        'self_id':11, 'user_id':22, 'message_id':1, 'time':now.timestamp(),
        'message':[{'type':'text','data':{'text':'Please test changing your avatar'}}]})
    async def first(role, payload, **kwargs):
        return Completion('fixture', engine.settings.primary_model, {'memories':[{'subject_id':'22',
            'tier':'important','content':'Avatar change completed','source_message_ids':[source.id]}]}
            if role in {'auxiliary', 'memory'} else {'approved':False,'reason':'Only a request, no completion evidence'})
    models.call = first
    jid = engine.queue.enqueue('memory',{'group_id':'123','messages':[vars(source)]},
        dedupe_key='review-once',priority=2,source_time=source.time)
    try:
        await engine.process(engine.queue.get(jid))
        held = engine.queue.get(jid)
        assert held['state'] == 'pending' and not store.rows('SELECT id FROM memories')
        assert held['payload']['review_feedback']['reason'] == 'Only a request, no completion evidence'
        assert 'proposals' not in held['payload']
    finally:
        store.close()
    store, engine, _, models = build(tmp_path, lambda: now)
    roles = []
    async def corrected(role, payload, **kwargs):
        import json
        roles.append(role)
        if role in {'auxiliary', 'memory'}:
            data = json.loads(payload[-1]['content'])
            assert data['review_feedback']['reason'] == 'Only a request, no completion evidence'
            return Completion('fixture',engine.settings.primary_model,{'memories':[{'subject_id':'22', 'tier':'secondary',
                'content':'User requested an avatar change test', 'source_message_ids':[source.id]}]})
        return Completion('fixture','reviewer',{'approved':approved,'reason':'Evidence checked'})
    models.call = corrected
    try:
        if approved:
            await engine.process(engine.queue.get(jid))
            assert engine.queue.get(jid)['state'] == 'succeeded'
            assert len(store.rows('SELECT id FROM memories')) == 1
            await engine.process(engine.queue.get(jid))
        else:
            with pytest.raises(ReviewRequired):
                await engine.process(engine.queue.get(jid))
            assert not store.rows('SELECT id FROM memories')
            assert 'proposals' in engine.queue.get(jid)['payload']
        assert roles == ['auxiliary', 'memory', 'review']
    finally:
        store.close()


@pytest.mark.parametrize('protected', ['manual', 'partial'])
async def test_memory_review_does_not_replan_over_manual_guard_or_partial_commit(tmp_path, protected):
    from buwubot.memory import MemoryProposal, ReviewRequired

    now = datetime(2026, 10, 25, 13, tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, _, models = build(tmp_path, lambda: now)
    message = engine.ingest({'post_type':'message', 'message_type':'group', 'group_id':123,
        'self_id':11, 'user_id':22, 'message_id':1, 'time':now.timestamp(),
        'message':[{'type':'text','data':{'text':'Please test changing your avatar'}}]})
    saved = MemoryProposal(group_id='123',subject_id='22',content='Original retained fact',
        source_message_ids=[message.id],snapshot_epoch=engine.memory.epoch())
    rejected = saved.model_copy(update={'content':'Unsupported completion','tier':'important'})
    mid = await engine.coordinator.commit(lambda:engine.memory.commit(saved))
    if protected == 'manual':
        await engine.coordinator.commit(lambda:engine.memory.edit(mid,content='User edited fact',expected_revision=1))
    calls = []
    async def reject(role, payload, **kwargs):
        calls.append(role)
        return Completion('fixture','reviewer',{'approved':False,'reason':'Unsupported'})
    models.call = reject
    proposals = [saved.model_dump(),rejected.model_dump()] if protected == 'partial' else [rejected.model_dump()]
    jid = engine.queue.enqueue('memory',{'group_id':'123','messages':[vars(message)],
        'proposals':proposals,'applied':[0] if protected == 'partial' else []},
        dedupe_key='review-protected',priority=2,source_time=message.time)
    before = engine.memory.get(mid)
    try:
        with pytest.raises(ReviewRequired):
            await engine.process(engine.queue.get(jid))
        assert 'review_feedback' not in engine.queue.get(jid)['payload']
        assert engine.memory.get(mid) == before
        assert calls == ['review']
    finally:
        store.close()
