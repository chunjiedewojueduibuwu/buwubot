from fastapi.testclient import TestClient

from buwubot.app import create_app


def test_personas_are_global_versioned_and_preserved_across_switch_and_restart(tmp_path):
    base="http://127.0.0.1:8765"
    with TestClient(create_app(tmp_path,run_bot=False),base_url=base) as client:
        cfg=client.get("/api/config").json()
        assert cfg["settings"]["persona_id"]=="assistant"
        assert cfg["settings"]["persona_name"]=="AI助手"
        response=client.patch("/api/config",json={"revision":cfg["revision"],"changes":{
            "persona":"保持第一份人格","aliases":["助手"],"group_id":"123","group_ids":["456"]}})
        assert response.status_code==200
        config_revision=response.json()["revision"]
        created=client.post("/api/personas",json={"id":"another","name":"另一个角色",
            "persona":"保持第二份人格","aliases":["另一个"]})
        assert created.status_code==200
        another=created.json()
        switched=client.post("/api/personas/another/switch",json={"revision":config_revision,
            "persona_revision":another["revision"]})
        assert switched.status_code==200
        assert switched.json()["settings"]["persona"]=="保持第二份人格"
        assert switched.json()["settings"]["group_ids"]==["456"]
        stale=client.post("/api/personas/assistant/switch",json={"revision":config_revision,"persona_revision":1})
        assert stale.status_code==409
        direct=client.patch("/api/config",json={"revision":switched.json()["revision"],"changes":{"persona_id":"assistant"}})
        assert direct.status_code==422
    with TestClient(create_app(tmp_path,run_bot=False),base_url=base) as client:
        listing=client.get("/api/personas").json()
        assert listing["active_id"]=="another"
        original=next(p for p in listing["items"] if p["id"]=="assistant")
        assert original["persona"]=="保持第一份人格"
        cfg=client.get("/api/config").json()
        back=client.post("/api/personas/assistant/switch",json={"revision":cfg["revision"],"persona_revision":original["revision"]})
        assert back.status_code==200
        assert back.json()["settings"]["persona"]==original["persona"]


async def test_persona_experience_does_not_follow_switch_but_member_facts_do(tmp_path):
    from buwubot.config import Settings
    from buwubot.coordinator import Coordinator
    from buwubot.memory import MemoryProposal, MemoryService
    from buwubot.qq import MessageArchive
    from buwubot.store import Store
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",self_id="11")
    store=Store(tmp_path/"bot.db")
    archive=MessageArchive(store)
    source=archive.accept({"post_type":"message","message_type":"group","group_id":"123",
        "self_id":"11","user_id":"22","message_id":1,"time":100,
        "message":[{"type":"text","data":{"text":"shared evidence"}}]},cfg)
    service=MemoryService(store,cfg)
    c=Coordinator(store,None,cfg)
    try:
        common=await c.commit(lambda:service.commit(MemoryProposal(group_id="123",subject_id="22",
            content="member fact",source_message_ids=[source.id])))
        experience=await c.commit(lambda:service.commit(MemoryProposal(group_id="123",persona_id="assistant",
            content="role experience",source_message_ids=[source.id])))
        assert {m["id"] for m in await service.retrieve("fact experience")}=={common,experience}
        service.settings=cfg.model_copy(update={"persona_id":"another"})
        assert {m["id"] for m in await service.retrieve("fact experience")}=={common}
        class StaleIndex:
            async def search(self,*args):return [{"app_id":experience,"revision":1}]
        service.index=StaleIndex()
        assert await service.retrieve("unrelated")==[]
        service.settings=cfg
        await c.commit(lambda:service.edit(experience,content="edited role experience",expected_revision=1))
        assert service.get(experience)["persona_id"]=="assistant"
    finally:store.close()
