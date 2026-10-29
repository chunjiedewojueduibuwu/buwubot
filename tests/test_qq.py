import httpx
import pytest

from buwubot.config import Settings
from buwubot.qq import MessageArchive, QQAdapter, normalize_message
from buwubot.store import Store


def event(mid="100", *, user="22", nickname="小明", card="阿明", text="大家好", segments=None):
    return {"time": 1792540000, "post_type": "message", "message_type": "group",
            "group_id": 123, "self_id": 11, "user_id": int(user), "message_id": int(mid),
            "message_seq": int(mid) + 1000,
            "sender": {"user_id": int(user), "nickname": nickname, "card": card},
            "message": segments if segments is not None else [{"type": "text", "data": {"text": text}}]}


async def test_forward_api_is_scoped_to_enabled_conversation():
    from buwubot.qq import QQError
    calls=[]
    async def handler(request):
        import json
        calls.append((request.url.path,json.loads(request.content)))
        return httpx.Response(200,json={'data':{'messages':[]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        qq=QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id='123'),client=client)
        with pytest.raises(QQError):
            await qq.forwarded('999','received-forward')
        assert await qq.forwarded('123','received-forward')=={'messages':[]}
        qq.settings.qq_enabled=False
        with pytest.raises(QQError):
            await qq.forwarded('123','received-forward')
    assert calls==[('/get_forward_msg',{'id':'received-forward'})]


def test_video_share_card_retains_original_and_exposes_normalized_reference():
    parts = [{'type':'json','data':{'data':'{"meta":{"detail_1":{"qqdocurl":'
        '"https:\\/\\/m.bilibili.com\\/video\\/BV1234567890?p=2&share_source=qq"}}}'}}]
    message = normalize_message(event(segments=parts),Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id='123',self_id='11'))
    assert message.text == '' and message.segments == parts
    assert message.source_video_urls == ['https://www.bilibili.com/video/BV1234567890/?p=2']


def test_archive_dedupes_updates_aliases_and_excludes_other_channels(tmp_path):
    store = Store(tmp_path / "bot.db")
    archive = MessageArchive(store)
    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    first = archive.accept(event(), settings)
    assert first and first.user_id == "22" and first.display_name == "阿明"
    assert archive.accept(event(), settings) is None
    archive.accept(event("101", card="新名字"), settings)
    member = archive.members("123")[0]
    assert member["user_id"] == "22"
    assert member["display_name"] == "新名字"
    assert set(member["aliases"]) == {"小明", "阿明", "新名字"}
    assert archive.accept(event("102", user="11"), settings) is None
    private = event("103") | {"message_type": "private"}
    assert archive.accept(private, settings) is None
    assert archive.accept(event("104") | {"group_id": 999}, settings) is None
    assert len(archive.recent("123")) == 2
    store.close()


def test_self_context_excludes_uncertain_sends_other_scopes_and_other_personas(tmp_path):
    import json

    from buwubot.coordinator import Coordinator
    store=Store(tmp_path/"bot.db")
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",self_id="11",persona="fixed")
    archive=MessageArchive(store);coordinator=Coordinator(store,None,cfg)
    archive.accept(event(),cfg)
    for index,(scope,persona,status,text) in enumerate([
        ("123",None,"sent","legacy reply"),("999","assistant","sent","other scope"),
        ("123","other","sent","other persona reply"),("123","assistant","uncertain","uncertain reply")]):
        payload={"persona_hash":cfg.persona_fingerprint} if persona is None else {"persona_id":persona}
        jid=coordinator.queue.enqueue("reply",payload,dedupe_key=f"context:{index}",priority=0,source_time=100)
        with store.transaction() as db:
            db.execute("INSERT INTO outgoing VALUES(?,?,?,?,?,?,?)",(jid,scope,None,
                json.dumps([{"type":"text","data":{"text":text}}]),1792540001+index,status,f"reply:{index}"))
    try:
        assert [r["text"] for r in archive.context(cfg) if r["role"]=="assistant"]==["legacy reply"]
        archive.tag_self_replies(cfg)
        renamed=cfg.model_copy(update={"aliases":["new alias"]})
        assert [r["text"] for r in archive.context(renamed) if r["role"]=="assistant"]==["legacy reply"]
        changed=cfg.model_copy(update={"persona_id":"other"})
        assert [r["text"] for r in archive.context(changed) if r["role"]=="assistant"]==["other persona reply"]
    finally:store.close()


def test_direct_call_matches_alias_substring_anywhere_but_not_everyone(tmp_path):
    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', group_id="123", self_id="11", aliases=["助手"])
    direct = normalize_message(event(segments=[{"type":"at","data":{"qq":"11"}}]), settings)
    assert direct.called
    everyone = normalize_message(event(segments=[{"type":"at","data":{"qq":"all"}}]), settings)
    assert not everyone.called
    store = Store(tmp_path / "names.db")
    archive = MessageArchive(store)
    for i,text in enumerate(("助手，帮忙看看", "助手发个点赞表情", "助手识别这张图", "问问助手吃什么",
                 '他说“助手帮忙看看”', "助手敌", "https://example.test/助手")):
        message = archive.accept(normalize_message(event(str(200+i),text=text), settings),settings)
        assert message.name_mentioned and not message.called, text
    assert not normalize_message(event(text="小 无，帮忙看看"), settings).called
    assert not normalize_message(event(text="大家好"), settings).called
    store.close()


def test_history_uses_stable_sequence_even_if_reply_id_changes(tmp_path):
    store = Store(tmp_path / "bot.db")
    archive = MessageArchive(store)
    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    first = archive.accept(event(), settings)
    assert archive.accept(event() | {"message_id": 5555}, settings) is None
    assert archive.get(first.id).message_id == "5555"
    store.close()


def test_receipt_cursor_never_rewinds_after_retention_cleanup(tmp_path):
    store = Store(tmp_path / "bot.db")
    archive = MessageArchive(store)
    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    archive.accept(event(), settings)
    previous = store.connection.execute("SELECT max(received_order) FROM messages").fetchone()[0]
    with store.transaction() as db:
        db.execute("DELETE FROM messages")
    archive.accept(event("101"), settings)
    assert store.connection.execute("SELECT min(received_order) FROM messages").fetchone()[0] > previous
    store.close()


async def test_qq_send_builds_native_reply_and_marks_transport_uncertainty():
    seen = []

    async def handler(request):
        seen.append((request.url.path, __import__("json").loads(request.content)))
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message_id": 88}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        qq = QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']), client=client)
        assert await qq.send("123", [{"type": "text", "data": {"text": "你好"}}], "100") == "88"
    assert seen == [("/send_group_msg", {"group_id": "123", "message": [
        {"type": "reply", "data": {"id": "100"}}, {"type": "text", "data": {"text": "你好"}}]})]

    from buwubot.qq import SendUncertain

    async def broken(request):
        raise httpx.ReadTimeout("connection closed after upload", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
        with pytest.raises(SendUncertain):
            await QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']), client=client).send("123", [], "100")


def test_member_sync_keeps_current_name_when_older_history_arrives(tmp_path):
    store = Store(tmp_path / "bot.db")
    archive = MessageArchive(store)
    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    archive.sync_members([{"user_id":22,"nickname":"小明","card":"现在的名字"},
                          {"user_id":33,"nickname":"安静群友"},
                          {"group_id":999,"user_id":44,"nickname":"别群"}],
                         settings, observed_at=1792541000)
    archive.accept(event(card="旧名字"), settings)
    members = {row["user_id"]:row for row in archive.members("123")}
    assert set(members) == {"22","33"}
    assert members["22"]["display_name"] == "现在的名字"
    assert "旧名字" in members["22"]["aliases"]
    assert archive.recent("123")[0].display_name == "旧名字"
    store.close()


@pytest.mark.parametrize("sequence_field", ["message_seq", "real_seq"])
async def test_history_orders_same_second_by_numeric_sequence(sequence_field):
    async def handler(request):
        return httpx.Response(200,json={"data":{"messages":[
            {"time":100,sequence_field:"10","message_id":-2},
            {"time":100,sequence_field:"9","message_id":10000},
            {"time":99,sequence_field:"8","message_id":2}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        qq=QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']),client=client)
        rows=await qq.history("123")
    assert [row[sequence_field] for row in rows] == ["8","9","10"]


@pytest.mark.parametrize("data", [{}, {"messages":{}}, {"messages":[None]},
                                     {"messages":[{"time":100,"message_seq":"bad"}]}])
async def test_invalid_history_raises_channel_error_instead_of_finishing_backfill(data):
    from buwubot.qq import QQError
    async def handler(request):
        return httpx.Response(200,json={"status":"ok","data":data})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QQError):
            await QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']),client=client).history("123")


def test_history_replay_adds_order_to_legacy_record_without_rewriting_content(tmp_path):
    store=Store(tmp_path/"bot.db")
    settings=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",self_id="11")
    archive=MessageArchive(store)
    first=archive.accept(event(text="保留原文"),settings)
    with store.transaction() as db:
        db.execute("UPDATE messages SET payload=json_remove(payload,'$.source_order')")
    assert archive.get(first.id).source_order is None
    assert archive.accept(event(text="历史重放不能覆盖原文"),settings) is None
    restored=archive.get(first.id)
    assert restored.source_order==1100 and restored.text=="保留原文"
    store.close()


def test_archive_without_source_order_preserves_receipt_order(tmp_path):
    from buwubot.conversations import Message
    store=Store(tmp_path/"bot.db")
    settings=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",self_id="11")
    archive=MessageArchive(store)
    for i in range(3):
        archive.accept(Message(str(i),str(i),"123","22","11",100,"群友","群友",str(i),[]),settings)
    assert [m.text for m in archive.recent("123",limit=2)]==["1","2"]
    store.close()


def test_transport_switch_keeps_one_canonical_message_and_current_quote_id(tmp_path):
    store = Store(tmp_path / "bot.db")
    archive = MessageArchive(store)
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    first = archive.accept(event("100"), cfg)
    # The observed pre-metadata archive has no reliable native source order.
    with store.transaction() as db:
        db.execute("UPDATE messages SET payload=json_remove(payload,'$.source_order') WHERE id=?", (first.id,))
    switched = cfg.model_copy(update={"qq_provider": "snowluma"})
    incoming = event("200")
    assert archive.accept(incoming, switched) is None
    assert archive.get(first.id).message_id == "200"
    assert [m.id for m in archive.recent("123")] == [first.id]
    # A second, actually distinct identical message in the same second is retained.
    second = archive.accept(event("201"), switched)
    assert second is not None
    assert len(archive.recent("123")) == 2
    store.close()


def test_transport_switch_ambiguous_same_second_messages_are_not_merged(tmp_path):
    store = Store(tmp_path / "bot.db")
    archive = MessageArchive(store)
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    archive.accept(event("100"), cfg)
    archive.accept(event("101"), cfg)
    assert archive.accept(event("200"), cfg.model_copy(update={"qq_provider": "snowluma"}))
    assert len(archive.recent("123")) == 3
    store.close()


def test_cross_provider_replay_preserves_distinct_reliable_sequences(tmp_path):
    store = Store(tmp_path / 'bot.db')
    archive = MessageArchive(store)
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id='123', self_id='11')
    originals = [archive.accept(event(mid), cfg) for mid in ['100', '101']]
    switched = cfg.model_copy(update={'qq_provider': 'snowluma'})
    for mid, seq in [('200', 1100), ('201', 1101)]:
        assert archive.accept(event(mid) | {'message_seq': seq}, switched) is None
    assert [m.id for m in archive.recent('123')] == [m.id for m in originals]
    assert [m.message_id for m in archive.recent('123')] == ['200', '201']
    # Same sender, second and text still do not identify a new sequence as an old message.
    assert archive.accept(event('202') | {'message_seq': 1102}, switched)
    assert len(archive.recent('123')) == 3
    store.close()


def test_new_reliable_sequence_does_not_match_single_identical_old_message(tmp_path):
    store = Store(tmp_path / 'bot.db')
    archive = MessageArchive(store)
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id='123', self_id='11')
    archive.accept(event('100'), cfg)
    assert archive.accept(event('200'), cfg.model_copy(update={'qq_provider': 'snowluma'}))
    assert len(archive.recent('123')) == 2
    store.close()


def test_replayed_legacy_duplicate_is_linked_without_deleting_raw_record(tmp_path):
    store = Store(tmp_path / "bot.db")
    archive = MessageArchive(store)
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    first = archive.accept(event("100"), cfg)
    duplicate = archive.accept(event("200"), cfg)
    # Existing archive predates provider metadata, as in the observed migration.
    with store.transaction() as db:
        db.execute("UPDATE messages SET payload=json_remove(payload,'$.source_provider')")
        db.execute("UPDATE messages SET payload=json_remove(payload,'$.source_order') WHERE id=?", (first.id,))
    assert archive.accept(event("200"), cfg.model_copy(update={"qq_provider": "snowluma"})) is None
    assert [m.id for m in archive.recent("123")] == [first.id]
    assert archive.get(duplicate.id).id == first.id
    assert store.rows("SELECT count(*) n FROM messages")[0]['n'] == 2
    store.close()


async def test_queued_alias_of_already_answered_message_is_not_sent_again(tmp_path):
    from buwubot.coordinator import Coordinator

    class QQ:
        def __init__(self):
            self.sent = 0

        async def send(self, *args):
            self.sent += 1
            return "reply"

    store = Store(tmp_path / "bot.db")
    archive = MessageArchive(store)
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    original = archive.accept(event("100"), cfg)
    duplicate = archive.accept(event("200"), cfg)
    with store.transaction() as db:
        db.execute("UPDATE messages SET payload=json_remove(payload,'$.source_provider')")
        db.execute("UPDATE messages SET payload=json_remove(payload,'$.source_order') WHERE id=?", (original.id,))
    archive.accept(event("200"), cfg.model_copy(update={"qq_provider": "snowluma"}))
    qq = QQ()
    coordinator = Coordinator(store, qq, cfg)
    def job(source):
        return coordinator.queue.enqueue("reply", {"group_id": "123", "source_message_ids": [source.id],
            "reply_source_id": source.id}, dedupe_key=source.id, priority=0, source_time=source.time)
    old_job = job(original)
    await coordinator.send(old_job, [{"type": "text", "data": {"text": "done"}}])
    new_job = job(duplicate)
    await coordinator.send(new_job, [{"type": "text", "data": {"text": "duplicate"}}])
    assert qq.sent == 1
    assert coordinator.queue.get(new_job)['state'] == 'skipped_duplicate'
    from buwubot.memory import MemoryService
    with store.transaction() as db:
        db.execute('UPDATE jobs SET updated_at=1')
    MemoryService(store, cfg).cleanup(now=original.time + 8 * 86400)
    assert not store.rows('SELECT id FROM messages')
    assert archive.canonical_id(duplicate.id) == original.id
    assert coordinator.queue.get(new_job)['payload'] == {}
    store.close()


@pytest.mark.parametrize('same_file', [True, False])
def test_transport_image_identity_uses_file_hash_not_temporary_url(tmp_path, same_file):
    store = Store(tmp_path / 'bot.db')
    archive = MessageArchive(store)
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id='123', self_id='11')
    old_file = 'a' * 32 + '.jpg'
    first = archive.accept(event('100', segments=[{'type': 'image', 'data': {
        'file': old_file, 'url': 'https://example.invalid/old', 'file_size': 123}}]), cfg)
    with store.transaction() as db:
        db.execute("UPDATE messages SET payload=json_remove(payload,'$.source_order') WHERE id=?", (first.id,))
    replay = event('200', segments=[{'type': 'image', 'data': {
        'file': old_file if same_file else 'b' * 32 + '.jpg', 'url': 'https://example.invalid/new'}}])
    accepted = archive.accept(replay, cfg.model_copy(update={'qq_provider': 'snowluma'}))
    assert (accepted is None) == same_file
    assert len(archive.recent('123')) == (1 if same_file else 2)
    assert archive.get(first.id).text == first.text
    store.close()


@pytest.mark.parametrize("ws_token", ["", "ws-only-token"])
async def test_distinct_websocket_token_is_private_persistent_and_used_for_events(monkeypatch, ws_token):
    import json

    from buwubot import qq as module

    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], qq_token="http-only-token", qq_ws_token=ws_token)
    stored = settings.storage()
    assert stored["qq_ws_token"] == ws_token
    restored = Settings.model_validate(stored)
    assert "qq_ws_token" not in restored.public()
    assert restored.public()["qq_ws_token_set"] == bool(ws_token)
    assert "ws-only-token" not in json.dumps(restored.public())

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def __aiter__(self):
            yield json.dumps({"post_type": "meta_event", "self_id": 11})

    def connect(url, **kwargs):
        assert kwargs["additional_headers"]["Authorization"] == "Bearer " + (ws_token or "http-only-token")
        return Socket()

    monkeypatch.setattr(module, "connect", connect)

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer http-only-token"
        return httpx.Response(200, json={"status": "ok", "data": {"user_id": 11}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQAdapter(restored, client=client)
        assert (await adapter.login_info())["user_id"] == 11
        assert len([row async for row in adapter.events()]) == 1
