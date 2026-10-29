from fastapi.testclient import TestClient

from buwubot.app import create_app


def test_learning_management_is_persistent_paused_and_never_starts_network_in_setup(tmp_path):
    with TestClient(create_app(tmp_path),base_url="http://127.0.0.1:8765") as client:
        current=client.get("/api/config").json()
        assert not current["settings"]["learning_enabled"]
        response=client.post("/api/learning",json={"url":"https://www.bilibili.com/video/BV1234567890/","part":2})
        assert response.status_code==200
        item=response.json()
        assert item["part"]==2 and item["cursor_seconds"]==0
        assert client.post(f"/api/learning/{item['id']}/pause",json={}).status_code==200
        assert client.get("/api/learning").json()["items"][0]["state"]=="held"
        assert client.post(f"/api/learning/{item['id']}/resume",json={}).status_code==200
        assert client.get("/api/learning").json()["items"][0]["state"]=="queued"
        assert client.post("/api/learning",json={"url":"http://127.0.0.1/private"}).status_code==422
        assert client.get("/api/memories?conversation_id=knowledge:public").status_code==200
        assert client.get("/api/stickers?conversation_id=knowledge:public").status_code==422
        assert client.get("/api/status").json()["mode"]=="setup"
    with TestClient(create_app(tmp_path),base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/learning").json()["items"][0]["part"]==2


def test_config_changes_are_local_versioned_private_and_restart_persistent(tmp_path):
    base = "http://127.0.0.1:8765"
    with TestClient(create_app(tmp_path, run_bot=False), base_url=base) as client:
        initial = client.get("/api/config").json()
        response = client.patch("/api/config", json={"revision": initial["revision"], "changes": {
            "persona": "保持固定人设", "group_id": "123", "workbuddy_key": "never-return-this"}})
        assert response.status_code == 200
        assert "never-return-this" not in response.text
        assert response.json()["settings"]["workbuddy_key_set"]
        stale = client.patch("/api/config", json={"revision": initial["revision"],
                                                 "changes": {"persona": "错误覆盖"}})
        assert stale.status_code == 409
        cross_site = client.patch("/api/config", headers={"Origin": "https://evil.invalid"},
                                  json={"revision": 2, "changes": {"group_id": "999"}})
        assert cross_site.status_code == 403
        invalid = client.patch("/api/config", json={"revision": 2,
                                                    "changes": {"sticker_total": 0}})
        assert invalid.status_code == 422
    with TestClient(create_app(tmp_path, run_bot=False), base_url=base) as client:
        actual = client.get("/api/config").json()
        assert actual["settings"]["persona"] == "保持固定人设"
        assert actual["settings"]["group_id"] == "123"
        assert actual["revision"] == 2


def test_embedding_toggle_persists_without_loading_a_model_in_setup(tmp_path):
    with TestClient(create_app(tmp_path, run_bot=False), base_url="http://127.0.0.1:8765") as client:
        current = client.get("/api/config").json()
        changed = client.put("/api/embedding/settings", json={"revision": current["revision"],
                              "changes": {"embedding_provider": "local"}})
        assert changed.status_code == 200
        assert client.get("/api/config").json()["settings"]["embedding_provider"] == "local"
        assert client.get("/api/status").json()["embedding_loaded"] is False
        assert client.get("/api/tools").json()["items"][0]["enabled"] is False


def test_management_routes_reject_external_host_and_serve_no_secret_to_html(tmp_path):
    with TestClient(create_app(tmp_path, run_bot=False), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/", headers={"Host": "evil.invalid"}).status_code == 403
        assert client.get("/").status_code == 200
        assert "workbuddy_key" not in client.get("/").text


def test_manual_retry_does_not_replay_finished_or_uncertain_sends(tmp_path):
    with TestClient(create_app(tmp_path, run_bot=False), base_url="http://127.0.0.1:8765") as client:
        queue = client.app.state.coordinator.queue
        jid = queue.enqueue("memory", {}, dedupe_key="manual-retry", priority=2, source_time=1)
        queue.defer(jid, "需要检查", now=1, needs_attention=True)
        assert client.post(f"/api/jobs/{jid}/retry", json={}).status_code == 200
        assert queue.get(jid)["state"] == "pending"
        queue.finish(jid, "skipped_uncertain")
        assert client.post(f"/api/jobs/{jid}/retry", json={}).status_code == 409
        assert queue.get(jid)["state"] == "skipped_uncertain"
        assert client.get("/api/jobs?pending_only=true").json()["items"] == []


def test_capacity_confirmation_tracks_global_eviction_set_and_rolls_back_on_failure(tmp_path):
    from io import BytesIO

    from PIL import Image

    from buwubot.config import Settings
    from buwubot.stickers import StickerCandidate
    with TestClient(create_app(tmp_path,run_bot=False),base_url="http://127.0.0.1:8765") as client:
        state=client.app.state
        state.stickers.settings=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123")
        def collect(color,when):
            buffer=BytesIO();Image.new("RGB",(16,16),color).save(buffer,format="PNG")
            asset=state.media.save(buffer.getvalue())
            client.portal.call(state.coordinator.commit,lambda:state.stickers.commit(StickerCandidate(
                asset_hash=asset["hash"],category="happy",meaning="clear",confidence=1,is_sticker=True),now=when))
        collect("red",1);collect("green",2)
        cfg=client.get("/api/config").json()
        body={"revision":cfg["revision"],"changes":{"sticker_total":1}}
        preview=client.patch("/api/config",json=body)
        assert preview.status_code==409
        first=preview.json()["detail"]["eviction_preview"]
        collect("blue",3)
        stale=client.patch("/api/config",json=body|{"confirm_evictions":True,"eviction_preview":first})
        assert stale.status_code==409
        assert client.get("/api/config").json()["revision"]==cfg["revision"]
        fresh=stale.json()["detail"]["eviction_preview"]
        state.store.connection.execute("CREATE TRIGGER fail_evict BEFORE DELETE ON stickers "
            "BEGIN SELECT RAISE(ABORT,'injected'); END")
        import sqlite3

        import pytest
        with pytest.raises(sqlite3.IntegrityError):
            client.patch("/api/config",json=body|{"confirm_evictions":True,"eviction_preview":fresh})
        assert client.get("/api/config").json()["revision"]==cfg["revision"]
        assert len(state.store.rows("SELECT * FROM stickers"))==3
        state.store.connection.execute("DROP TRIGGER fail_evict")
        saved=client.patch("/api/config",json=body|{"confirm_evictions":True,"eviction_preview":fresh})
        assert saved.status_code==200
        assert len(state.store.rows("SELECT DISTINCT id FROM stickers"))==1


def test_manual_memory_creation_has_honest_provenance_scope_and_version_guard(tmp_path):
    with TestClient(create_app(tmp_path,run_bot=False),base_url="http://127.0.0.1:8765") as client:
        cfg=client.get("/api/config").json()
        cfg=client.patch("/api/config",json={"revision":cfg["revision"],"changes":{"group_id":"123"}}).json()
        state=client.app.state
        state.memory.archive.accept({"post_type":"message","message_type":"group","group_id":123,
            "self_id":11,"user_id":22,"message_id":1,"time":1,"sender":{"nickname":"fixture"},
            "message":[{"type":"text","data":{"text":"hello"}}]},state.store.settings()[0])
        body={"config_revision":cfg["revision"],"content":"用户设定中的人物关系，不是共同经历",
              "subject_id":"22","role_memory":True,"tier":"important","tags":["初始设定"]}
        response=client.post("/api/memories?conversation_id=123",json=body)
        assert response.status_code==200
        memory=response.json()
        assert memory["group_id"]=="123" and memory["subject_id"]=="22"
        assert memory["tier"]=="important" and memory["persona_id"]==cfg["settings"]["persona_id"]
        assert all(s.startswith("manual:") for s in memory["source_message_ids"])
        assert state.store.rows("SELECT memory_id FROM memory_manual_guards WHERE memory_id=?",(memory["id"],))
        assert len(state.store.rows("SELECT id FROM messages"))==1
        again=client.post("/api/memories?conversation_id=123",json=body)
        assert again.status_code==200 and again.json()["id"]==memory["id"]
        assert client.post("/api/memories?conversation_id=999",json=body).status_code==422
        assert client.post("/api/memories?conversation_id=knowledge:public",json=body).status_code==422
        assert client.post("/api/memories?conversation_id=123",json=body|{"source_message_ids":["fake"]}).status_code==422
        client.patch("/api/config",json={"revision":cfg["revision"],"changes":{"persona":"changed"}})
        assert client.post("/api/memories?conversation_id=123",json=body|{"content":"stale"}).status_code==409
        assert len(state.store.rows("SELECT id FROM memories WHERE deleted=0"))==1
def test_memory_lineage_is_revision_bound_scoped_and_read_only(tmp_path):
    with TestClient(create_app(tmp_path,run_bot=False),base_url="http://127.0.0.1:8765") as client:
        cfg=client.get('/api/config').json()
        cfg=client.patch('/api/config',json={'revision':cfg['revision'],
            'changes':{'group_id':'123','group_ids':['456']}}).json()
        def create(scope,content,tier):
            return client.post('/api/memories?conversation_id='+scope,json={
                'config_revision':cfg['revision'],'content':content,'tier':tier}).json()['id']
        parent=create('123','近期原文','recent')
        target=create('123','精炼要点','secondary')
        foreign=create('456','其他群私有材料','secondary')
        store=client.app.state.store
        store.connection.executemany('INSERT INTO memory_distillations VALUES(?,?,?,?,?)',
            [(parent,1,target,1,100),(parent,1,foreign,1,100)])
        before=store.connection.total_changes
        items=client.get('/api/memories?conversation_id=123').json()['items']
        by_id={x['id']:x for x in items}
        assert by_id[target]['distillation_sources']==[{'memory_id':parent,'revision':1,
            'created_at':100,'current':True,'content':'近期原文','tier':'recent'}]
        assert by_id[parent]['distillation_targets'][0]['memory_id']==target
        assert foreign not in str(items) and '其他群私有材料' not in str(items)
        assert store.connection.total_changes==before
        changed=client.patch('/api/memories/'+parent,json={'revision':1,'content':'人工改过的原文'})
        assert changed.status_code==200
        items=client.get('/api/memories?conversation_id=123').json()['items']
        source=next(x for x in items if x['id']==target)['distillation_sources'][0]
        assert source['current'] is False and source['content'] is None
