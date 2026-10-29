import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_context_memory import batch
from test_engine import build

from buwubot.models import Completion
from buwubot.store import Conflict


async def test_auxiliary_summary_is_scoped_complete_and_invalidates_on_send(tmp_path):
    now=datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=12)
    store, engine, qq, models=build(tmp_path,lambda:now)
    scan, msg=batch(engine,1)
    engine.queue.finish(scan)
    engine.enqueue_context_summaries()
    job=store.rows("SELECT id FROM jobs WHERE kind='context'")[0][0]
    calls=[]
    async def summarize(role,payload,**kwargs):
        assert role == "auxiliary"
        snapshot=json.loads(payload[-1]["content"])
        assert snapshot["id"] == scan
        calls.append(role)
        return Completion("", "glm", {"items":[{"source_message_ids":[msg["id"]],"summary":"甲在询问时间"}]})
    models.call=summarize
    await engine.process(engine.queue.get(job))
    context=engine.memory.context_memory()
    assert len(context) == 1 and context[0]["summary"]["items"][0]["summary"] == "甲在询问时间"
    assert context[0]["messages"][0]["id"] == msg["id"]
    engine.enqueue_context_summaries()
    assert len(store.rows("SELECT id FROM jobs WHERE kind='context'")) == 1
    reply=engine.queue.enqueue("reply",{"group_id":"123","persona_id":"assistant","batch_id":scan,"persona_hash":engine.draft_fingerprint(),
        "messages":[msg],"source_message_ids":[msg["id"]]},dedupe_key="summary-reply",priority=0,source_time=msg["time"])
    await engine.coordinator.send(reply,[{"type":"text","data":{"text":"现在两点"}}])
    changed=engine.memory.context_memory()[0]
    assert changed["summary"] is None and changed["replies"][0]["send_status"] == "sent"
    engine.enqueue_context_summaries()
    assert len(store.rows("SELECT id FROM jobs WHERE kind='context'")) == 2
    with pytest.raises(Conflict):
        await engine.coordinator.commit(lambda:engine.memory.store_context_summary(context[0],
            {"items":[{"source_message_ids":[msg["id"]],"summary":"旧摘要"}]}))
    assert calls == ["auxiliary"] and len(qq.sent)==1
    store.close()


async def test_summary_cannot_drop_messages_or_invent_sources(tmp_path):
    now=datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=12)
    store, engine, _, _=build(tmp_path,lambda:now)
    batch(engine,1)
    snapshot=engine.memory.context_memory()[0]
    for data in ({"items":[]},{"items":[{"source_message_ids":["invented"],"summary":"无依据"}]}):
        with pytest.raises(ValueError):
            await engine.coordinator.commit(lambda data=data:engine.memory.store_context_summary(snapshot,data))
    assert engine.memory.context_memory()[0]["summary"] is None
    assert not store.rows("SELECT id FROM memories")
    store.close()


async def test_summary_paused_at_commit_resumes_without_recalling_model(tmp_path):
    from buwubot.coordinator import WorkPaused

    now=datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=12)
    store, engine, _, models=build(tmp_path,lambda:now)
    scan,msg=batch(engine,1)
    engine.queue.finish(scan)
    engine.enqueue_context_summaries()
    jid=store.rows("SELECT id FROM jobs WHERE kind='context'")[0][0]
    calls=[]
    async def summarize(*args,**kwargs):
        calls.append("auxiliary")
        engine.coordinator.allowed=lambda:False
        return Completion("", "glm", {"items":[{"source_message_ids":[msg["id"]],"summary":"甲问时间"}]})
    models.call=summarize
    with pytest.raises(WorkPaused):
        await engine.process(engine.queue.get(jid))
    assert "result" in engine.queue.get(jid)["payload"]
    assert engine.memory.context_memory()[0]["summary"] is None
    engine.coordinator.allowed=lambda:True
    await engine.process(engine.queue.get(jid))
    assert calls == ["auxiliary"]
    assert engine.memory.context_memory()[0]["summary"] is not None
    store.close()


async def test_summary_accepts_only_unambiguous_native_source_ids(tmp_path):
    now=datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=12)
    store, engine, _, _=build(tmp_path,lambda:now)
    jid,msg=batch(engine,1)
    snapshot=engine.memory.context_memory()[0]
    data={"items":[{"source_message_ids":["1"],"summary":"甲询问时间"}]}
    await engine.coordinator.commit(lambda:engine.memory.store_context_summary(snapshot,data))
    assert engine.memory.context_memory()[0]["summary"]["items"][0]["source_message_ids"] == [msg["id"]]
    payload=engine.queue.get(jid)["payload"]
    payload["messages"].append(msg|{"id":"123:seq:999"})
    engine.queue.payload(jid,payload)
    snapshot=engine.memory.context_memory()[0]
    with pytest.raises(ValueError):
        await engine.coordinator.commit(lambda:engine.memory.store_context_summary(snapshot,data))
    store.close()
