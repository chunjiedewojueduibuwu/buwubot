import pytest
from pydantic import ValidationError
from test_memory import proposal, setup_memory

from buwubot.config import Settings


def test_layer_policies_validate_special_layers():
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'])
    policies = cfg.memory_policies.model_dump()
    assert policies["core"]["strategy"] == "always"
    assert policies["context"]["strategy"] == "rolling"
    assert policies["feeling"]["half_life_minutes"] == 30
    with pytest.raises(ValidationError):
        Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], memory_policies=policies|{"core":{"strategy":"recency"}})
    with pytest.raises(ValidationError):
        Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], memory_policies=policies|{"feeling":{"half_life_minutes":-1}})
    with pytest.raises(ValidationError):
        Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], memory_policies=policies|{"unexpected":{}})


async def test_emotion_decay_uses_event_time_not_reorganization_and_keeps_history(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    p = proposal().model_copy(update={"tier":"feeling","persona_id":memory.settings.persona_id,
        "subject_id":"","content":"当时交流很愉快","event_time":100})
    mid = await coordinator.commit(lambda:memory.commit(p,reviewed=True))
    original = memory.get(mid)
    view = memory.with_reading_policy(original,now=1900)
    assert view["decay_factor"] == pytest.approx(.5)
    assert view["reading_strategy"] == "recency"
    assert memory.with_reading_policy(original|{"updated_at":1899},now=1900)["decay_factor"] == pytest.approx(.5)
    assert memory.with_reading_policy(original|{"event_time":None,"created_at":100},now=1900)["decay_factor"] == pytest.approx(.5)
    policies=memory.settings.memory_policies.model_dump()
    policies["feeling"]["half_life_minutes"]=60
    memory.settings=Settings.model_validate(memory.settings.storage()|{"memory_policies":policies})
    assert memory.with_reading_policy(original,now=3700)["decay_factor"] == pytest.approx(.5)
    assert memory.with_reading_policy(original|{"tier":"important"},now=10**9)["decay_factor"] == 1
    assert memory.get(mid)["content"] == original["content"]
    assert "decay_factor" not in memory.get(mid)
    store.close()


def test_policy_admin_persistence_and_feeling_decay_display(tmp_path):
    from fastapi.testclient import TestClient

    from buwubot.app import create_app

    with TestClient(create_app(tmp_path,run_bot=False),base_url="http://127.0.0.1:8765") as client:
        cfg=client.get("/api/config").json()
        policies=cfg["settings"]["memory_policies"]
        policies["feeling"]["half_life_minutes"]=15
        policies["secondary"]["strategy"]="importance"
        policies["context"]["window_batches"]=3
        r=client.patch("/api/config",json={"revision":cfg["revision"],"changes":{
            "group_id":"123","memory_policies":policies}})
        assert r.status_code == 200
        assert client.get("/api/config").json()["settings"]["memory_policies"] == policies
        r=client.post("/api/memories",json={"config_revision":r.json()["revision"],"tier":"feeling","content":"收到关心很开心"})
        assert r.status_code == 200
        view=client.get("/api/memories?tier=feeling").json()["items"][0]
        assert 0 < view["decay_factor"] <= 1 and view["reading_strategy"] == "recency"
        bad=policies|{"context":{"strategy":"relevance"}}
        r=client.patch("/api/config",json={"revision":client.get("/api/config").json()["revision"],"changes":{"memory_policies":bad}})
        assert r.status_code == 422
    with TestClient(create_app(tmp_path,run_bot=False),base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/config").json()["settings"]["memory_policies"] == policies
