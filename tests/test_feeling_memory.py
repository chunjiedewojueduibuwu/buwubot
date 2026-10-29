import pytest
from fastapi.testclient import TestClient
from test_memory import proposal, setup_memory

from buwubot.app import create_app
from buwubot.memory import MemoryService, ReviewRequired


async def test_feelings_are_reviewed_personal_and_scoped(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    p = proposal().model_copy(update={"tier":"feeling", "subject_id":"",
        "persona_id":memory.settings.persona_id, "content":"这次交流让我感到被理解，想继续聊"})
    with pytest.raises(ReviewRequired):
        await coordinator.commit(lambda:memory.commit(p))
    mid = await coordinator.commit(lambda:memory.commit(p,reviewed=True))
    assert memory.get(mid)["tier"] == "feeling"
    other = MemoryService(store,memory.settings.model_copy(update={"persona_id":"other"}))
    assert other.get(mid) is None
    assert other.search_text("理解",group_id="123") == []
    for changes in ({"persona_id":None}, {"subject_id":"22"}):
        with pytest.raises(ValueError):
            await coordinator.commit(lambda changes=changes:memory.commit(p.model_copy(update=changes),reviewed=True))
    store.close()


def test_feeling_admin_lifecycle_and_role_isolation(tmp_path):
    with TestClient(create_app(tmp_path,run_bot=False),base_url="http://127.0.0.1:8765") as client:
        cfg=client.get("/api/config").json()
        cfg=client.patch("/api/config",json={"revision":cfg["revision"],"changes":{"group_id":"123"}}).json()
        body={"config_revision":cfg["revision"],"tier":"feeling","content":"收到关心觉得温暖"}
        r=client.post("/api/memories",json=body)
        assert r.status_code == 200, r.text
        item=r.json()
        assert item["persona_id"] == "assistant" and not item["subject_id"]
        assert client.get("/api/memories?tier=feeling").json()["total"] == 1
        assert client.post("/api/memories",json=body|{"role_memory":False}).status_code == 422
        r=client.patch("/api/memories/"+item["id"],json={"revision":1,"content":"心情已经平静","tier":"feeling"})
        assert r.status_code == 200, r.text
        other=client.post("/api/personas",json={"id":"other","name":"其他角色","persona":"其他设定","aliases":[]}).json()
        original=next(x for x in client.get("/api/personas").json()["items"] if x["id"] == "assistant")
        cfg=client.post("/api/personas/other/switch",json={"revision":cfg["revision"],"persona_revision":other["revision"]}).json()
        assert client.get("/api/memories?tier=feeling").json()["total"] == 0
        assert client.get("/api/memories").json()["total"] == 0
        assert client.delete("/api/memories/"+item["id"]+"?revision=2",headers={"Content-Type":"application/json"}).status_code == 404
        client.post("/api/personas/assistant/switch",json={"revision":cfg["revision"],"persona_revision":original["revision"]})
        assert client.delete("/api/memories/"+item["id"]+"?revision=2",headers={"Content-Type":"application/json"}).status_code == 200
        assert client.get("/api/memories?tier=feeling").json()["total"] == 0
