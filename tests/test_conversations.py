import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from buwubot.config import Settings
from buwubot.coordinator import Coordinator, JobQueue
from buwubot.engine import BotEngine
from buwubot.memory import MemoryProposal, MemoryService
from buwubot.models import Completion
from buwubot.qq import ConversationRef, MessageArchive, QQAdapter
from buwubot.stickers import MediaStore, StickerService
from buwubot.store import Store
from buwubot.tools import ToolRegistry


def message(kind="group",target="123",user="22",mid=1):
    return {"post_type":"message","message_type":kind,"group_id":target,
            "self_id":"11","user_id":user,"message_id":mid,"time":100,
            "sender":{"nickname":"sender"},"message":[{"type":"at","data":{"qq":"11"}},
            {"type":"text","data":{"text":"hello"}}]}


def test_authorized_conversations_and_legacy_identity_remain_separate(tmp_path):
    cfg=Settings(runtime_enabled=True, qq_enabled=True, persona_name='助手', aliases=['助手'], group_id="123",group_ids=["456"],owner_id="10001",owner_private_enabled=True,self_id="11")
    store=Store(tmp_path/"bot.db")
    archive=MessageArchive(store)
    try:
        first=archive.accept(message(),cfg)
        second=archive.accept(message(target="456"),cfg)
        private=archive.accept(message("private",user=cfg.owner_id),cfg)
        assert first.id=="123:id:1"  # Keep existing durable references valid.
        assert first.conversation_id=="group:123"
        assert second.conversation_id=="group:456"
        assert private.conversation_id=="private:10001" and private.called
        assert len({first.id,second.id,private.id})==3
        assert archive.accept(message("private",user="22",mid=2),cfg) is None
        assert archive.accept(message(target="789",mid=2),cfg) is None
        assert archive.accept(message(user="11",mid=3),cfg) is None
        archive.accept({"notice_type":"friend_recall","user_id":cfg.owner_id,"message_id":1},cfg)
        assert archive.get(private.id).recalled
        assert not archive.get(first.id).recalled
    finally:
        store.close()


def test_quoted_context_follows_old_text_identity_without_cross_scope_or_recall(tmp_path):
    from buwubot.conversations import RecallEvent

    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id='123', group_ids=['456'], self_id='11')
    store = Store(tmp_path/'quotes.db')
    archive = MessageArchive(store)
    def put(mid, text, reply=None, *, scope='123', user='22', timestamp=100):
        event = message(target=scope, user=user, mid=mid)
        event['time'] = timestamp
        event['message'] = [{'type':'text','data':{'text':text}}]
        if reply is not None:
            event['message'].insert(0, {'type':'reply','data':{'id':str(reply)}})
        return archive.accept(event, cfg)
    original = put(1, '感觉很有人味', user='33', timestamp=80)
    intro = put(2, '这位是测试群友甲，他在夸你', 1, timestamp=90)
    root = put(3, '这个刚刚没看到吗', 2)
    put(2, '其他群的同号内容', scope='456')
    put(4, '之后才出现的内容', timestamp=120)
    put(5, '已撤回的内容')
    archive.accept(RecallEvent('123','5'), cfg)
    for target in (4, 5, 999):
        root.segments.append({'type':'reply','data':{'id':str(target)}})
    try:
        quoted = archive.quoted([root])
        assert [m.id for m in quoted] == [intro.id, original.id]
        assert quoted[1].user_id == '33' and quoted[1].text == '感觉很有人味'
        assert not intro.called and not original.called
        assert len(archive.quoted([root], limit=1)) == 1
    finally:
        store.close()


def test_private_only_configuration_is_ready_and_rejects_invalid_targets():
    assert Settings(runtime_enabled=True, qq_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], owner_private_enabled=True,persona="助手",workbuddy_key="fixture").ready
    assert not Settings(runtime_enabled=True, qq_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], owner_private_enabled=False,persona="助手",workbuddy_key="fixture").ready
    assert ConversationRef("group","123").id=="group:123"
    with pytest.raises(ValueError):
        ConversationRef("private","x")


def test_private_targets_are_explicitly_added_removed_and_retained_by_identity(tmp_path):
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], owner_private_enabled=False,private_ids=["22","33","22"], self_id="11")
    assert cfg.conversation_keys == ["private:22","private:33"]
    store = Store(tmp_path/"bot.db")
    archive = MessageArchive(store)
    first = archive.accept(message("private",user="22"),cfg)
    assert first and first.called
    assert archive.accept(message("private",user="44"),cfg) is None
    removed = cfg.model_copy(update={"private_ids":["33"]})
    assert archive.accept(message("private",user="22",mid=2),removed) is None
    assert archive.get(first.id).text == "hello"
    assert Settings.model_validate(cfg.storage()).conversation_keys == cfg.conversation_keys
    assert cfg.persona_fingerprint == removed.persona_fingerprint
    with pytest.raises(ValueError):
        Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], private_ids=["not-a-qq"])
    store.close()


async def test_private_transport_uses_private_actions_and_uncertainty():
    calls=[]
    async def handler(request):
        calls.append((request.url.path,json.loads(request.content)))
        return httpx.Response(200,json={"data":{"message_id":77,"messages":[]}})
    target=ConversationRef("private","10001")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        qq=QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']),client=http)
        assert await qq.send_to(target,[],"42")=="77"
        assert await qq.history_for(target)==[]
    assert calls[0]==("/send_private_msg",{"user_id":"10001","message":[{"type":"reply","data":{"id":"42"}}]})
    assert calls[1][0]=="/get_friend_msg_history"
    assert calls[1][1]["user_id"]==target.target_id


async def test_multi_conversation_runtime_does_not_mix_context_or_memory(tmp_path):
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",group_ids=["456"],self_id="11",persona="助手",workbuddy_key="fixture")
    store=Store(tmp_path/"bot.db")
    store.save_settings(cfg,store.settings()[1])
    memory=MemoryService(store,cfg)
    media=MediaStore(store,tmp_path/"media")
    stickers=StickerService(store,cfg,media)
    qq=QQAdapter(cfg)
    sent=[]
    captured=[]
    async def send(target,parts,reply_id=None):
        sent.append((target,parts,reply_id))
        return str(len(sent))
    qq.send_to=send
    coordinator=Coordinator(store,qq,cfg)
    now=datetime(2026,10,21,10,tzinfo=ZoneInfo("Asia/Shanghai"))
    engine=BotEngine(store,coordinator,memory,media,stickers,ToolRegistry(),tmp_path,clock=lambda:now)
    engine.qq=qq
    class Models:
        async def call(self,role,payload,**kwargs):
            if role=="planning": return Completion("",cfg.primary_model,{"replies":[]})
            if role=="chat":
                captured.append(json.loads(payload[1]["content"][0]["text"]))
                return Completion("",cfg.primary_model,{"text":"hi"})
            data=json.loads(payload[-1]["content"]) if role=="retrieval" and "candidates" in payload[-1]["content"] else None
            return Completion("",cfg.primary_model,{"terms":[],"ids":[m["id"] for m in data["candidates"]] if data else []})
    engine.models=Models()
    memory.models=engine.models
    try:
        for kind,target,user in [("group","123","22"),("group","456","33"),("private","",cfg.owner_id)]:
            raw=message(kind,target,user)
            raw["time"]=now.timestamp()
            accepted=engine.ingest(raw)
            scoped=MemoryService(store,cfg.scoped(accepted.group_id))
            await coordinator.commit(lambda m=scoped, source=accepted:m.commit(MemoryProposal(
                group_id=source.group_id,subject_id=source.user_id,content="hello in "+source.group_id,
                source_message_ids=[source.id])))
        await engine.plan_pending()
        queue=JobQueue(store)
        while job:=queue.claim(now.timestamp(),kinds=["scan","reply"]):
            await engine.process(job)
        assert {x[0].id for x in sent}=={"group:123","group:456","private:10001"}
        assert len(captured)==3
        for body in captured:
            source=body["targets"][0]["group_id"]
            assert all(m["group_id"]==source for m in body["recent_chat"])
            assert body["memory"]
            assert all(m["group_id"]==source for m in body["memory"])
        # A disabled target cannot send a previously queued reply.
        cfg.group_ids=[]
        coordinator.settings=cfg
        jid=queue.enqueue("reply",{"group_id":"456"},dedupe_key="disabled",priority=0,source_time=1)
        await coordinator.send(jid,[])
        assert len(sent)==3
        assert queue.get(jid)["state"]=="needs_attention"
    finally:
        await qq.close()
        store.close()


async def test_vector_scope_is_checked_against_canonical_memory_before_model_input(tmp_path):
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",group_ids=["456"],self_id="11")
    store=Store(tmp_path/"bot.db")
    coordinator=Coordinator(store,None,cfg)
    archive=MessageArchive(store)
    own=archive.accept(message(target="123"),cfg)
    other=archive.accept(message(target="456"),cfg)
    own_service=MemoryService(store,cfg.scoped("123"))
    other_service=MemoryService(store,cfg.scoped("456"))
    try:
        mid=await coordinator.commit(lambda:other_service.commit(MemoryProposal(group_id="456",
            subject_id=other.user_id,content="private fact in another group",source_message_ids=[other.id])))
        class BadIndex:
            async def search(self,*args): return [{"app_id":mid,"revision":1}]
        own_service.index=BadIndex()
        assert await own_service.retrieve("unrelated")==[]
        assert own.group_id=="123"
    finally:
        store.close()


async def test_disabled_conversation_is_not_read_during_uncertain_send_recovery(tmp_path):
    from buwubot.qq import SendUncertain
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",group_ids=["456"],self_id="11",persona="助手",workbuddy_key="fixture")
    reads=[]
    class QQ:
        async def send(self,*args): raise SendUncertain("unknown")
        async def history(self,key): reads.append(key); return []
    store=Store(tmp_path/"bot.db")
    c=Coordinator(store,QQ(),cfg)
    jid=c.queue.enqueue("reply",{"group_id":"456"},dedupe_key="unknown",priority=0,source_time=1)
    try:
        await c.send(jid,[])
        cfg.group_ids=[]
        await c.recover()
        assert not reads
        assert c.queue.get(jid)["state"]=="needs_attention"
    finally: store.close()


def test_management_can_select_private_and_group_archives(tmp_path):
    from fastapi.testclient import TestClient

    from buwubot.app import create_app
    with TestClient(create_app(tmp_path,run_bot=False),base_url="http://127.0.0.1:8765") as client:
        cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",group_ids=["456"],self_id="11")
        store=client.app.state.store
        store.save_settings(cfg,store.settings()[1])
        archive=MessageArchive(store)
        archive.accept(message(),cfg)
        archive.accept(message("private",user=cfg.owner_id),cfg)
        private=client.get("/api/messages?conversation_id=private:10001").json()
        assert len(private["items"])==1
        assert private["items"][0]["group_id"]=="private:10001"
        assert len(client.get("/api/messages?conversation_id=group:123").json()["items"])==1
        assert client.get("/api/messages?conversation_id=private:evil").status_code==422
        assert client.get("/api/memories?conversation_id=private:10001").status_code==200


async def test_manual_pause_prevents_even_static_greetings(tmp_path):
    cfg=Settings(qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",persona="助手",workbuddy_key="fixture",runtime_enabled=False)
    store=Store(tmp_path/"bot.db")
    c=Coordinator(store,None,cfg)
    jid=c.queue.enqueue("greeting",{"group_id":"123"},dedupe_key="paused",priority=-1,source_time=1)
    try:
        await c.send(jid,[],greeting=True)
        assert c.queue.get(jid)["state"]=="skipped_expired"
    finally: store.close()
