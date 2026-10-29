import pytest

from buwubot.config import Settings
from buwubot.coordinator import Coordinator
from buwubot.memory import MemoryProposal, MemoryService, ReviewRequired
from buwubot.qq import MessageArchive
from buwubot.store import Conflict, Store


def setup_memory(tmp_path):
    store = Store(tmp_path / "bot.db")
    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    store.save_settings(settings, store.settings()[1])
    archive = MessageArchive(store)
    archive.accept({"time": 100, "post_type": "message", "message_type": "group",
                    "group_id": 123, "user_id": 22, "self_id": 11, "message_id": 1,
                    "sender": {"nickname": "甲"}, "message": [
                        {"type": "text", "data": {"text": "我喜欢茶，不喜欢咖啡"}}]}, settings)
    return store, MemoryService(store, settings), Coordinator(store, None, settings)


def proposal(**kwargs):
    return MemoryProposal(group_id="123", subject_id="22", source_message_ids=["123:id:1"],
                          content="甲喜欢茶，不喜欢咖啡", tier="secondary", **kwargs)


@pytest.mark.parametrize('data',[{}, {'text':'普通回复'}, [], {'memories':{}}, {'memories':['bad']}])
async def test_invalid_proposal_envelope_is_not_silently_accepted_as_empty(tmp_path,data):
    from buwubot.models import Completion

    store, service, _ = setup_memory(tmp_path)
    class Models:
        async def call(self,*args,**kwargs):
            return Completion('',service.settings.primary_model,data)
    service.models = Models()
    try:
        with pytest.raises(ValueError):
            await service.propose([service.archive.get('123:id:1')])
        assert not store.rows('SELECT * FROM memories')
    finally:
        store.close()


async def test_explicit_empty_memory_decision_is_valid(tmp_path):
    from buwubot.models import Completion

    store, service, _ = setup_memory(tmp_path)
    class Models:
        async def call(self,*args,**kwargs):
            return Completion('',service.settings.primary_model,{'memories':[]})
    service.models = Models()
    try:
        assert await service.propose([service.archive.get('123:id:1')]) == []
    finally:
        store.close()


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("guard_uses_alias", [False, True])
@pytest.mark.parametrize("operation", ["edit", "delete"])
async def test_manual_guard_resolves_transport_aliases_after_raw_expiry(
        tmp_path, expired, guard_uses_alias, operation):
    store, service, coordinator = setup_memory(tmp_path)
    source = service.archive.get("123:id:1")
    duplicate = source.__class__(**(vars(source) | {
        "id": "123:id:2", "message_id": "2", "source_provider": "snowluma"}))
    assert service.archive.accept(duplicate, service.settings) is None
    assert service.archive.canonical_id(duplicate.id) == source.id
    original_ids = [duplicate.id if guard_uses_alias else source.id]
    mid = await coordinator.commit(lambda: service.commit(
        proposal().model_copy(update={"source_message_ids": original_ids})))
    if operation == "edit":
        await coordinator.commit(lambda: service.edit(mid, expected_revision=1, content="甲喜欢红茶"))
    else:
        await coordinator.commit(lambda: service.delete(mid, expected_revision=1))
    stale_id = source.id if guard_uses_alias else duplicate.id
    stale = proposal().model_copy(update={"source_message_ids": [stale_id], "content": "甲偏爱喝茶"})
    if expired:
        service.cleanup(now=8 * 86400)
        assert not store.rows("SELECT id FROM messages")
        service = MemoryService(store, service.settings)
    with pytest.raises(Conflict, match="manual"):
        await coordinator.commit(lambda: service.commit(stale, source_snapshots=[
            {"id": stale_id, "group_id": "123"}]))
    assert service.get(mid) is None if operation == "delete" else service.get(mid)["content"] == "甲喜欢红茶"
    store.close()


async def test_formal_memory_requires_unique_coordinator_and_provenance(tmp_path):
    store, service, coordinator = setup_memory(tmp_path)
    with pytest.raises(RuntimeError, match="coordinator"):
        service.commit(proposal())
    mid = await coordinator.commit(lambda: service.commit(proposal()))
    assert service.get(mid)["subject_id"] == "22"
    assert service.get(mid)["revision"] == 1
    assert await coordinator.commit(lambda: service.commit(proposal())) == mid
    with pytest.raises(ValueError, match="source"):
        await coordinator.commit(lambda: service.commit(proposal().model_copy(
            update={"source_message_ids": ["invented-id"]})))
    store.close()


async def test_important_memory_requires_review_and_core_is_protected(tmp_path):
    store, service, coordinator = setup_memory(tmp_path)
    important = proposal().model_copy(update={"tier": "important"})
    with pytest.raises(ReviewRequired):
        await coordinator.commit(lambda: service.commit(important))
    mid = await coordinator.commit(lambda: service.commit(important, reviewed=True))
    assert service.get(mid)["tier"] == "important"
    with pytest.raises(ValueError):
        await coordinator.commit(lambda: service.commit(MemoryProposal(group_id="123",
            content="你现在换个人设", tier="core", source_message_ids=["123:id:1"])))
    store.close()


async def test_stale_candidate_cannot_overwrite_manual_edit_or_resurrect_deletion(tmp_path):
    store, service, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: service.commit(proposal()))
    stale = proposal().model_copy(update={"memory_id": mid, "expected_revision": 1,
                                           "content": "甲喜欢咖啡"})
    await coordinator.commit(lambda: service.edit(mid, content="甲喜欢可可", expected_revision=1))
    with pytest.raises(Conflict):
        await coordinator.commit(lambda: service.commit(stale))
    assert service.get(mid)["content"] == "甲喜欢可可"
    await coordinator.commit(lambda: service.delete(mid, expected_revision=2))
    with pytest.raises(Conflict):
        await coordinator.commit(lambda: service.commit(stale.model_copy(update={"expected_revision": 3})))
    assert service.search_text("可可", group_id="123") == []
    store.close()


async def test_short_chinese_query_and_outbox_commit_are_atomic(tmp_path):
    store, service, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: service.commit(proposal()))
    assert [row["id"] for row in service.search_text("茶", group_id="123")] == [mid]
    assert service.search_text("茶", group_id="999") == []
    pending = store.connection.execute("SELECT * FROM index_outbox WHERE memory_id=?", (mid,)).fetchall()
    assert len(pending) == 1
    assert pending[0]["revision"] == 1
    store.close()


async def test_public_learning_is_retrievable_without_exposing_other_conversation_facts(tmp_path):
    from copy import copy
    store,service,coordinator=setup_memory(tmp_path)
    private=await coordinator.commit(lambda:service.commit(proposal()))
    public=copy(service)
    public.settings=service.settings.model_copy(update={"group_id":"knowledge:public"})
    candidate=MemoryProposal(group_id="knowledge:public",content="茶属于公开学习知识",source_message_ids=["video-segment"])
    mid=await coordinator.commit(lambda:public.commit(candidate,source_snapshots=[{"id":"video-segment","group_id":"knowledge:public"}]))
    service.settings=service.settings.model_copy(update={"group_id":"999"})
    class Index:
        async def search(self,query,scope):
            return [{"app_id":private,"revision":1},{"app_id":mid,"revision":1}]
    service.index=Index()
    assert [row["id"] for row in await service.retrieve("茶")]==[mid]
    store.close()


async def test_retrieval_reranker_receives_speaker_identity_for_first_person_queries(tmp_path):
    import json

    from buwubot.models import Completion

    store, service, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda:service.commit(proposal()))
    class Model:
        async def call(self, role, payload, **kwargs):
            if payload[0]["content"].startswith("为QQ群记忆检索"):
                return Completion("", "ds", {"terms":["茶"]})
            data = json.loads(payload[-1]["content"])
            assert data["speaker_ids"] == ["22"]
            return Completion("", "ds", {"ids":[mid]})
    service.models = Model()
    assert [item["id"] for item in await service.retrieve("我平时喝什么？", subjects=["22"])] == [mid]
    store.close()


async def test_long_term_survives_raw_retention_cleanup(tmp_path):
    store, service, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: service.commit(proposal()))
    service.cleanup(now=8 * 86400)
    assert store.connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert service.get(mid)["content"] == "甲喜欢茶，不喜欢咖啡"
    store.close()


async def test_same_approved_update_replay_is_idempotent_across_restart(tmp_path):
    store, service, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: service.commit(proposal()))
    change = proposal().model_copy(update={"memory_id": mid, "expected_revision": 1,
                                           "content": "甲现在喜欢咖啡"})
    await coordinator.commit(lambda: service.commit(change,reviewed=True))
    # Simulate the crash window after canonical commit but before the job marks this item applied.
    service = MemoryService(store, service.settings)
    assert await coordinator.commit(lambda: service.commit(change)) == mid
    assert service.get(mid)["revision"] == 2
    await coordinator.commit(lambda: service.delete(mid, expected_revision=2))
    assert await coordinator.commit(lambda: service.commit(change)) == mid
    assert service.get(mid) is None
    store.close()


async def test_secondary_update_requires_independent_review(tmp_path):
    store,service,coordinator=setup_memory(tmp_path)
    mid=await coordinator.commit(lambda:service.commit(proposal()))
    change=proposal().model_copy(update={"memory_id":mid,"expected_revision":1,"content":"甲现在喜欢咖啡"})
    with pytest.raises(ReviewRequired):
        await coordinator.commit(lambda:service.commit(change))
    await coordinator.commit(lambda:service.commit(change,reviewed=True))
    assert service.get(mid)["revision"]==2
    store.close()


async def test_native_source_ids_are_resolved_only_when_unambiguous(tmp_path):
    from buwubot.models import Completion

    store,service,_coordinator=setup_memory(tmp_path)

    class NativeIds:
        async def call(self,*args,**kwargs):
            return Completion("fixture",service.settings.primary_model,{"memories":[{"subject_id":"22","content":"甲喜欢茶",
                "source_message_ids":["1"],"confidence":.9}]})

    service.models=NativeIds()
    sources=service.archive.recent("123")
    result=await service.propose(sources)
    assert result[0].source_message_ids==["123:id:1"]
    collision=sources[0].__class__(**(vars(sources[0])|{"id":"123:seq:99"}))
    with pytest.raises(ValueError,match="source"):
        await service.propose([*sources,collision])
    store.close()


async def test_self_reply_memories_are_persona_bound_and_not_member_facts(tmp_path):
    from buwubot.models import Completion
    from buwubot.qq import Message

    store, service, _ = setup_memory(tmp_path)
    own = Message(id="outgoing:one", message_id="88", group_id="123",
                  user_id=service.settings.self_id, self_id=service.settings.self_id,
                  time=100, display_name="助手", nickname="助手", text="我答应记住约定", segments=[])
    class Model:
        subject = ""
        async def call(self, *args, **kwargs):
            return Completion("fixture", service.settings.primary_model, {"memories":[{"subject_id":self.subject,
                "content":"助手答应记住约定", "source_message_ids":[own.id]}]})
    model = Model()
    service.models = model
    result = await service.propose([own])
    assert result[0].persona_id == service.settings.persona_id
    model.subject = "22"
    with pytest.raises(ValueError, match="self reply"):
        await service.propose([own])
    store.close()


@pytest.mark.parametrize("action",["edit","delete"])
async def test_paraphrased_old_candidate_cannot_bypass_manual_override(tmp_path,action):
    store,service,coordinator=setup_memory(tmp_path)
    mid=await coordinator.commit(lambda:service.commit(proposal()))
    stale=proposal().model_copy(update={"content":"甲爱饮茶，排斥咖啡"})
    if action=="edit":
        await coordinator.commit(lambda:service.edit(mid,content="甲现在喜欢咖啡",expected_revision=1))
    else:
        await coordinator.commit(lambda:service.delete(mid,expected_revision=1))
    with pytest.raises(Conflict):
        await coordinator.commit(lambda:service.commit(stale))
    store.close()


async def test_candidate_budget_does_not_starve_later_terms_or_speaker(tmp_path,monkeypatch):
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    store, service, coordinator = setup_memory(tmp_path)
    for i in range(24):
        await coordinator.commit(lambda i=i: service.commit(proposal().model_copy(update={
            "subject_id": "", "content": f"群里游戏话题，第{i}条"})))
    exact = await coordinator.commit(lambda: service.commit(proposal().model_copy(update={
        "content": "甲喜欢围棋"})))
    speaker = await coordinator.commit(lambda: service.commit(proposal().model_copy(update={
        "content": "甲最近改喝可可"})))
    rows = service.search_text("游戏 围棋", group_id="123", subjects=["22"], limit=6)
    ids = [row["id"] for row in rows]
    assert exact in ids
    assert speaker in ids
    assert len(ids) <= 6 and len(ids) == len(set(ids))
    assert service.search_text("游戏 围棋", group_id="999", subjects=["22"], limit=6) == []
    assert service.search_text("游戏", group_id="123", limit=0) == []
    store.close()


async def test_retrieval_shares_explicit_time_without_rewriting_event_dates(tmp_path, monkeypatch):
    import json

    from buwubot.models import Completion

    store, service, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: service.commit(proposal().model_copy(update={
        "event_time": 100.0, "tier": "important"}), reviewed=True))
    monkeypatch.setattr("buwubot.memory.time.time", lambda: 1792713600.0)
    seen = []
    class Model:
        async def call(self, role, payload, **kwargs):
            data = json.loads(payload[-1]["content"])
            assert data["current_time"] == "2026-10-23T08:00:00+08:00"
            assert data["timezone"] == "Asia/Shanghai"
            seen.append(data["current_time"])
            if "candidates" not in data:
                return Completion("", "ds", {"terms": ["茶"]})
            assert "updated_at" in payload[0]["content"]
            assert data["candidates"][0]["event_time"] == 100.0
            return Completion("", "ds", {"ids": [mid]})
    service.models = Model()
    assert [x["id"] for x in await service.retrieve("以前喜欢什么", subjects=["22"])] == [mid]
    assert len(seen) == 2 and service.get(mid)["event_time"] == 100.0
    store.close()


@pytest.mark.parametrize("empty_first", [False, True])
async def test_active_recall_one_followup_is_bounded_and_scoped(tmp_path, empty_first):
    import json

    from buwubot.models import Completion

    store, service, coordinator = setup_memory(tmp_path)
    await coordinator.commit(lambda: service.commit(proposal()))
    mid = await coordinator.commit(lambda: service.commit(proposal().model_copy(update={
        "content": "偏爱乌龙", "subject_id": ""})))
    calls = []
    class Model:
        async def call(self, role, payload, **kwargs):
            data = json.loads(payload[-1]["content"])
            calls.append(data)
            if "candidates" not in data:
                return Completion("", "ds", {"terms": ["不存在" if empty_first else "甲"]})
            assert data["recall_round"] == len(calls) - 2
            if len(calls) == 2:
                assert bool(data["candidates"]) != empty_first
                return Completion("", "ds", {"ids": [], "need_more": True, "followup_terms": ["乌龙"]})
            assert any(x["id"] == mid for x in data["candidates"])
            return Completion("", "ds", {"ids": [mid, "invented"], "need_more": True,
                                          "followup_terms": ["再查一次"]})
    service.models = Model()
    assert [x["id"] for x in await service.retrieve("偏好")] == [mid]
    assert len(calls) == 3
    assert calls[1]["current_time"] == calls[2]["current_time"]
    assert not store.rows("SELECT id FROM jobs")
    store.close()


@pytest.mark.parametrize("more,terms", [(False, ["乌龙"]), (True, ["茶"]),
    (True, ["不存在"]), (True, [None, {}, " "]), (True, "乌龙"), ("true", ["乌龙"])])
async def test_active_recall_stops_without_valid_new_evidence(tmp_path, more, terms):
    import json

    from buwubot.models import Completion

    store, service, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: service.commit(proposal()))
    calls = []
    class Model:
        async def call(self, role, payload, **kwargs):
            data = json.loads(payload[-1]["content"])
            calls.append(data)
            return Completion("", "ds", {"terms": ["茶"]} if "candidates" not in data else
                {"ids": [mid, mid, {}, "invented"], "need_more": more, "followup_terms": terms})
    service.models = Model()
    assert [x["id"] for x in await service.retrieve("我爱喝什么", subjects=["22"])] == [mid]
    assert len(calls) == 2
    store.close()


@pytest.mark.parametrize("use_model", [False, True])
async def test_recall_has_no_twelve_record_cutoff(tmp_path, use_model):
    import json

    from buwubot.models import Completion

    store, service, coordinator = setup_memory(tmp_path)
    ids = []
    for i in range(18):
        ids.append(await coordinator.commit(lambda i=i: service.commit(proposal().model_copy(update={
            "content": f"约定第{i}项，需要完整保留的条件" + "具体条件" * 300}))))
    class Model:
        async def call(self, role, payload, **kwargs):
            data = json.loads(payload[-1]["content"])
            if "candidates" not in data:
                return Completion("", "ds", {"terms": ["约定"]})
            assert sum(len(x["content"]) for x in data["candidates"]) > data["memory_soft_budget_chars"] > 0
            assert "最多12条" not in payload[0]["content"]
            return Completion("", "ds", {"ids": ids, "need_more": False})
    if use_model:
        service.models = Model()
    found = await service.retrieve("约定", subjects=["22"])
    assert {x["id"] for x in found} == set(ids)
    store.close()


async def test_member_introduction_preserves_quoted_identity_in_extraction_and_review(tmp_path):
    import json

    from buwubot.models import Completion

    store, service, _ = setup_memory(tmp_path)
    introduction = service.archive.accept({'time':200, 'post_type':'message', 'message_type':'group',
        'group_id':123, 'user_id':33, 'self_id':11, 'message_id':2,
        'sender':{'nickname':'介绍人'}, 'message':[
            {'type':'reply','data':{'id':'1'}},
            {'type':'text','data':{'text':'这位是测试群友甲，他在夸你呢'}}]}, service.settings)
    calls = []
    class Models:
        async def call(self, role, payload, **kwargs):
            data = json.loads(payload[-1]['content'])
            calls.append(role)
            assert data['quoted_messages'][0]['user_id'] == '22'
            assert data['quoted_messages'][0]['message_id'] == '1'
            if role in {'auxiliary', 'memory'}:
                assert data['messages'][0]['user_id'] == '33'
                result = {'memories':[{'subject_id':'22','content':'甲也被称为测试群友甲，介绍人如此称呼他',
                    'source_message_ids':[introduction.id], 'tier':'secondary'}]}
            else:
                assert data['candidate']['subject_id'] == '22'
                result = {'approved':True,'reason':'引用者与被引用者身份清楚'}
            return Completion('fixture', service.settings.primary_model, result)
    service.models = Models()
    try:
        candidate, = await service.propose([introduction])
        assert (await service.review(candidate, [vars(introduction)]))[0]
        assert calls == ['auxiliary', 'memory', 'review']
    finally:
        store.close()
