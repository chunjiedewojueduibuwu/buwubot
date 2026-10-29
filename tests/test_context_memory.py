import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from test_engine import build

from buwubot.app import create_app
from buwubot.memory import MemoryService
from buwubot.models import Completion


def batch(engine, number, *, group="123", persona="assistant"):
    message = {"id": f"{group}:id:{number}", "message_id": str(number),
               "group_id": group, "user_id": "22", "self_id": "11",
               "nickname": "甲", "display_name": "甲", "text": "助手现在几点", "time": time.time(),
               "segments": [], "called": True}
    jid = engine.queue.enqueue("scan", {"group_id": group, "persona_id": persona,
        "messages": [message], "topics": [{"source_message_ids": [message["id"]]}]},
        dedupe_key=f"batch:{group}:{persona}:{number}", priority=0, source_time=message["time"])
    return jid, message


async def test_context_window_is_five_scoped_batches_with_live_reply_state(tmp_path):
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=12)
    store, engine, qq, _ = build(tmp_path, lambda: now)
    entries = [batch(engine, n) for n in range(7)]
    batch(engine, 99, group="private:999")
    batch(engine, 100, persona="another")
    scan_id, message = entries[-1]
    jid = engine.queue.enqueue("reply", {"group_id": "123", "persona_id": "assistant",
        "batch_id": scan_id, "persona_hash":engine.draft_fingerprint(),
        "messages": [message], "source_message_ids": [message["id"]]},
        dedupe_key="reply:context", priority=0, source_time=message["time"])
    context = engine.memory.context_memory()
    assert [x["id"] for x in context] == [x[0] for x in entries[-5:]]
    assert context[-1]["replies"][0]["state"] == "pending"
    assert context[-1]["replies"][0]["segments"] == []
    await engine.coordinator.send(jid, [{"type": "text", "data": {"text": "两点半"}}])
    context = MemoryService(store, engine.settings).context_memory()
    assert context[-1]["replies"][0]["send_status"] == "sent"
    assert context[-1]["replies"][0]["segments"][0]["data"]["text"] == "两点半"
    assert len(qq.sent) == 1
    assert not store.rows("SELECT id FROM memories")
    store.close()
    store, engine, _qq, _models = build(tmp_path, lambda: now)
    assert [x['id'] for x in engine.memory.context_memory()] == [x[0] for x in entries[-5:]]
    assert engine.memory.context_memory()[-1]['replies'][0]['send_status'] == 'sent'
    engine.memory.cleanup(now=time.time() + 8 * 86400)
    assert engine.memory.context_memory(now=time.time() + 8 * 86400) == []
    store.close()


async def test_planning_uses_context_and_only_sent_same_author_can_cover_repeat(tmp_path):
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=12)
    store, engine, qq, models = build(tmp_path, lambda: now)
    previous, original = batch(engine, 1)
    reply = engine.queue.enqueue("reply", {"group_id": "123", "persona_id": "assistant",
        "batch_id": previous, "persona_hash":engine.draft_fingerprint(),
        "messages": [original], "source_message_ids": [original["id"]]},
        dedupe_key="old-answer", priority=0, source_time=original["time"])
    await engine.coordinator.send(reply, [{"type": "text", "data": {"text": "现在两点半"}}])
    scan, message = batch(engine, 2)
    payload = engine.queue.get(scan)["payload"]
    del payload["topics"]
    engine.queue.payload(scan, payload)
    async def planning(role, contents, **kwargs):
        assert role == "planning"
        data = json.loads(contents[-1]["content"])
        assert data["context_memory"][0]["id"] == previous
        return Completion("fixture", "ds", {"replies": [], "already_answered": [{
            "source_message_ids": [message["id"]], "reply_job_id": reply,
            "reason": "同一人的重复补问，前一条已答复"}]})
    models.call = planning
    await engine.process(engine.queue.get(scan))
    assert len(store.rows("SELECT id FROM jobs WHERE kind='reply'")) == 1
    assert len(qq.sent) == 1
    from buwubot.conversations import Message
    repeated = Message(**message)
    data = {"replies": [], "already_answered": [{"source_message_ids": [message["id"]],
        "reply_job_id": reply, "reason": "重复"}]}
    context = engine.memory.context_memory()
    for status in (None, "failed", "inflight", "uncertain", "unconfirmed"):
        context[0]["replies"][0]["send_status"] = status
        with pytest.raises(ValueError, match="sent"):
            engine.validate_plan(data, [repeated], now.timestamp(), context_memory=context)
    context = engine.memory.context_memory()
    with pytest.raises(ValueError, match="author"):
        engine.validate_plan(data, [Message(**(message | {"user_id": "33"}))],
                             now.timestamp(), context_memory=context)
    with pytest.raises(ValueError, match="covered"):
        engine.validate_plan({'replies':[
            {'source_message_ids':[message['id']]}, {'source_message_ids':[message['id']]}]},
            [repeated], now.timestamp())
    assert len(engine.validate_plan({'replies':[{'source_message_ids':[message['id']]}]},
                                   [repeated], now.timestamp(), context_memory=context)) == 1
    store.close()


async def test_chat_receives_same_rolling_context_without_draft_text(tmp_path):
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=12)
    store, engine, qq, models = build(tmp_path, lambda: now)
    scan, message = batch(engine, 1)
    jid = engine.queue.enqueue('reply', {'group_id':'123', 'persona_id':'assistant',
        'segments':[{'type':'text','data':{'text':'尚未发送的草稿'}}], 'persona_hash':'old',
        'batch_id':scan, 'messages':[message], 'source_message_ids':[message['id']]},
        dedupe_key='context-chat', priority=0, source_time=message['time'])
    async def chat(role, contents, **kwargs):
        if role != 'chat':
            return Completion('fixture', 'ds', {'terms':[], 'ids':[]})
        data = json.loads(contents[-1]['content'][0]['text'])
        assert data['context_memory'][0]['id'] == scan
        assert data['context_memory'][0]['replies'][0]['segments'] == []
        return Completion('fixture', 'ds', {'text':'接上了'})
    models.call = chat
    await engine.process(engine.queue.get(jid))
    assert len(qq.sent) == 1
    assert engine.memory.context_memory()[0]['replies'][0]['send_status'] == 'sent'
    store.close()


def test_context_layer_is_read_only_and_filtered_in_management(tmp_path):
    app = create_app(tmp_path, run_bot=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        cfg, revision = app.state.store.settings()
        app.state.store.save_settings(cfg.model_copy(update={"group_id": "123"}), revision)
        # The management-only preview has no engine; seed normal queue records in this test DB.
        class QueueOwner:
            queue = app.state.coordinator.queue
        batch(QueueOwner(), 1)
        result = client.get("/api/memories?tier=context").json()
        assert result["total"] == 1 and result["items"][0]["tier"] == "context"
        assert result["items"][0]["read_only"] is True
        assert client.get("/api/memories?tier=context&query=不存在").json()["total"] == 0
        assert client.get("/api/memories?tier=recent").json()["total"] == 0
        assert client.get("/api/memories?tier=context&conversation_id=999").json()["total"] == 0
        assert client.post("/api/memories", json={"tier": "context", "content": "伪造", "config_revision": 2}).status_code == 422


async def test_elliptical_retrieval_reads_same_context_and_speaker_identity(tmp_path):
    from test_memory import proposal, setup_memory

    store, service, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda:service.commit(proposal()))
    class QueueOwner:
        queue = coordinator.queue
    current, _ = batch(QueueOwner(), 101)
    batch(QueueOwner(), 2, group='private:999')
    batch(QueueOwner(), 3, persona='another')
    calls = []
    class Models:
        async def call(self, role, contents, **kwargs):
            assert 'context_memory' in contents[-1]['content']
            data = json.loads(contents[-1]['content'])
            assert data['query'] == '那我之前喜欢的那个呢'
            assert data['speaker_ids'] == ['22']
            assert [item['id'] for item in data['context_memory']] == [current]
            assert data['context_memory'][0]['group_id'] == '123'
            calls.append(contents[0]['content'])
            if contents[0]['content'].startswith('为QQ群记忆检索'):
                return Completion('fixture', 'ds', {'terms':['茶']})
            return Completion('fixture', 'ds', {'ids':[mid]})
    service.models = Models()
    result = await service.retrieve('那我之前喜欢的那个呢', subjects=['22'])
    assert [item['id'] for item in result] == [mid]
    assert len(calls) == 2
    store.close()
