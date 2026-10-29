from datetime import datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from test_engine import build

from buwubot.app import create_app


def test_plugin_listing_and_channel_switch_persist_without_secrets(tmp_path):
    with TestClient(create_app(tmp_path,run_bot=False),base_url="http://127.0.0.1:8765") as client:
        plugins=client.get("/api/plugins").json()
        assert [p["id"] for p in plugins["items"]]==["qq"]
        assert plugins["items"][0]["kind"]=="activity"
        assert plugins["items"][0]["page_id"]=="qq"
        assert not any(p["installed"] for p in plugins["available_tools"])
        cfg=client.get("/api/config").json()
        result=client.patch("/api/config",json={"revision":cfg["revision"],"changes":{"qq_enabled":False}})
        assert result.status_code==200 and not result.json()["settings"]["qq_enabled"]
        assert not client.get("/api/plugins").json()["items"][0]["enabled"]
        assert client.patch("/api/config",json={"revision":cfg["revision"],"changes":{"qq_enabled":True}}).status_code==409
    with TestClient(create_app(tmp_path,run_bot=False),base_url="http://127.0.0.1:8765") as client:
        assert not client.get("/api/plugins").json()["items"][0]["enabled"]


async def test_disabled_qq_blocks_reception_and_final_send_but_preserves_job(tmp_path):
    now=datetime(2026,10,22,14,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,qq,_=build(tmp_path,lambda:now)
    engine.settings.qq_enabled=False
    engine.coordinator.settings.qq_enabled=False
    try:
        assert engine.ingest({"post_type":"message","message_type":"group","group_id":123,
            "self_id":11,"user_id":22,"message_id":1,"time":now.timestamp(),
            "message":[{"type":"at","data":{"qq":"11"}}]}) is None
        jid=engine.queue.enqueue("reply",{"group_id":"123","reply_id":"1",
            "persona_hash":engine.draft_fingerprint(engine.settings)},
            dedupe_key="disabled-channel",priority=0,source_time=now.timestamp())
        await engine.coordinator.send(jid,[{"type":"text","data":{"text":"fixture"}}],now=now.timestamp())
        assert not qq.sent and engine.queue.get(jid)["state"] in {"pending","retry_wait"}
        await engine.process(engine.queue.get(jid))
        assert not qq.sent and engine.queue.get(jid)["state"] in {"pending","retry_wait"}
        engine.settings.qq_enabled=True
        engine.coordinator.settings.qq_enabled=True
        await engine.coordinator.send(jid,[{"type":"text","data":{"text":"fixture"}}],now=now.timestamp()+301)
        assert len(qq.sent)==1 and engine.queue.get(jid)["state"]=="succeeded"
        engine.settings.qq_enabled=False
        await engine.process(engine.queue.get(jid))
        assert engine.queue.get(jid)["state"]=="succeeded" and len(qq.sent)==1
    finally:
        store.close()


async def test_core_routine_stops_heartbeat_and_work_then_resumes_without_greetings(tmp_path):
    import asyncio

    now=datetime(2026,10,22,23,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,qq,models=build(tmp_path,lambda:now)
    engine._last_heartbeat=now.timestamp()-3600
    previous_heartbeat=engine._last_heartbeat
    engine._qq_attention=True
    store.set_state("maintenance_day",str(now.date()))
    jid=engine.queue.enqueue("memory",{"group_id":"123","messages":[]},
        dedupe_key="night-memory",priority=2,source_time=now.timestamp())
    supervisor=asyncio.create_task(engine._supervise())
    worker=asyncio.create_task(engine._worker(["memory"]))
    try:
        await asyncio.sleep(0.05)
        assert not engine.allowed() and not engine.channel_allowed("desktop:private:owner")
        assert engine.status()["mode"]=="night_pause"
        assert engine.status()["last_heartbeat"]==previous_heartbeat
        assert engine.queue.get(jid)["state"]=="pending"
        assert not models.calls and not qq.sent
        now=now.replace(day=23,hour=7,minute=30)
        store.set_state("maintenance_day",str(now.date()))
        async with asyncio.timeout(3):
            while engine.queue.get(jid)["state"]!="succeeded" or engine._last_heartbeat==previous_heartbeat:
                await asyncio.sleep(0.01)
        assert engine.allowed() and engine.queue.get(jid)["state"]=="succeeded"
        assert not store.rows("SELECT id FROM jobs WHERE kind='greeting'") and not qq.sent
    finally:
        supervisor.cancel();worker.cancel()
        await asyncio.gather(supervisor,worker,return_exceptions=True)
        store.close()


async def test_reply_finishing_after_qq_bedtime_waits_until_channel_reopens(tmp_path):
    from test_engine import ModelsFixture

    now=datetime(2026,10,22,22,29,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,qq,_=build(tmp_path,lambda:now)
    class Delayed(ModelsFixture):
        async def call(self,role,payload,**kwargs):
            nonlocal now
            result=await super().call(role,payload,**kwargs)
            if role=="chat":now=now.replace(minute=31)
            return result
    engine.models=Delayed()
    try:
        message=engine.ingest({"post_type":"message","message_type":"group","group_id":123,
            "self_id":11,"user_id":22,"message_id":1,"time":now.timestamp(),
            "message":[{"type":"at","data":{"qq":"11"}}]})
        jid=engine.queue.enqueue("reply",{"group_id":"123","messages":[vars(message)],
            "reply_id":"1","reply_source_id":message.id,"source_message_ids":[message.id]},
            dedupe_key="channel-bedtime",priority=0,source_time=now.timestamp())
        await engine.process(engine.queue.get(jid))
        assert not qq.sent and engine.queue.get(jid)["state"]=="pending"
        assert engine.queue.get(jid)["payload"]["segments"]
        assert not engine.allowed() and not engine.qq_allowed()
        now=now.replace(day=23,hour=8,minute=0)
        await engine.process(engine.queue.get(jid))
        assert len(qq.sent)==1 and engine.models.calls.count("chat")==1
    finally:
        store.close()


def test_plugin_api_uses_actual_registry_instead_of_inventing_qq(tmp_path):
    from buwubot.channels import ChannelRegistry

    class Fixture:
        management: ClassVar[dict[str, str]] = {"name":"测试范围","description":"只用于清单验证。"}
        capabilities=frozenset()
    registry=ChannelRegistry()
    registry.register("fixture",Fixture)
    class Runtime:
        channels=registry
        async def close(self):pass
    app=create_app(tmp_path,run_bot=False)
    with TestClient(app,base_url="http://127.0.0.1:8765") as client:
        app.state.bot=Runtime()
        result=client.get("/api/plugins")
        assert result.status_code==200
        assert [p["id"] for p in result.json()["items"]]==["fixture"]
        assert result.json()["items"][0]["page_id"]=="fixture"


def test_qq_settings_endpoint_rejects_core_fields_and_preserves_versioning(tmp_path):
    with TestClient(create_app(tmp_path,run_bot=False),base_url="http://127.0.0.1:8765") as client:
        cfg=client.get("/api/config").json()
        endpoint="/api/plugins/qq/settings"
        denied=client.patch(endpoint,json={"revision":cfg["revision"],"changes":{"persona":"must not change"}})
        assert denied.status_code==422
        assert client.get("/api/config").json()==cfg
        saved=client.patch(endpoint,json={"revision":cfg["revision"],"changes":{"qq_enabled":False}})
        assert saved.status_code==200 and not saved.json()["settings"]["qq_enabled"]
        assert saved.json()["settings"]["persona"]==cfg["settings"]["persona"]
        assert client.patch(endpoint,json={"revision":cfg["revision"],"changes":{"qq_enabled":True}}).status_code==409
        assert client.patch("/api/plugins/missing/settings",json={"revision":cfg["revision"],"changes":{}}).status_code==404
        plugin=client.get("/api/plugins").json()["items"][0]
        fields={field["key"] for section in plugin["settings_sections"] for field in section["fields"]}
        assert {"qq_enabled","qq_token"}<=fields
        assert not fields & {"routine_enabled","bedtime","reboot_wakeup","continuous_wakeup","morning_template","evening_template"}
        assert not fields & {"persona","persona_id","aliases","active_poll_seconds","workbuddy_key"}
