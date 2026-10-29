import pytest
from test_memory import proposal, setup_memory

from buwubot.memory import MemoryService
from buwubot.store import Conflict


async def test_emotion_state_restart_explicit_recall_and_idempotence(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    p=proposal().model_copy(update={"tier":"feeling","persona_id":memory.settings.persona_id,
        "subject_id":"","content":"这次讨论让我开心","feeling_strength":.6,"event_time":100})
    mid=await coordinator.commit(lambda:memory.commit(p,reviewed=True))
    memory=MemoryService(store,memory.settings)
    original=memory.with_reading_policy(memory.get(mid),now=1900)
    assert original["initial_feeling"] == .6
    assert original["feeling_level"] == pytest.approx(.3)
    assert original["memory_remaining"] == 1
    assert memory.with_reading_policy(memory.get(mid),now=1900) == original
    with pytest.raises(RuntimeError,match="coordinator"):
        memory.recall_feeling(mid,operation_id="explicit:one",expected_revision=1,now=1900)
    await coordinator.commit(lambda:memory.recall_feeling(mid,operation_id="explicit:one",expected_revision=1,now=1900))
    after=memory.with_reading_policy(memory.get(mid),now=1900)
    assert after["initial_feeling"] > original["initial_feeling"]
    assert after["feeling_level"] > original["feeling_level"]
    await coordinator.commit(lambda:memory.recall_feeling(mid,operation_id="explicit:one",expected_revision=1,now=2000))
    assert memory.with_reading_policy(memory.get(mid),now=1900) == after
    with pytest.raises(Conflict):
        await coordinator.commit(lambda:memory.recall_feeling(mid,operation_id="explicit:two",expected_revision=1,now=2000))
    store.close()


async def test_expiry_removes_memory_queues_index_delete_and_blocks_old_proposal(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    p=proposal().model_copy(update={"tier":"feeling","persona_id":memory.settings.persona_id,
        "subject_id":"","content":"当时有点开心","feeling_strength":.1,"event_time":100})
    mid=await coordinator.commit(lambda:memory.commit(p,reviewed=True))
    assert memory.with_reading_policy(memory.get(mid),now=100+500*86400)["memory_remaining"] == 0
    assert await coordinator.commit(lambda:memory.expire_feelings(now=100+500*86400)) == 1
    assert memory.get(mid) is None
    assert memory.search_text("开心",group_id="123") == []
    assert store.rows("SELECT operation FROM index_outbox WHERE memory_id=? ORDER BY id DESC",(mid,))[0][0] == "delete"
    stale=p.model_copy(update={"content":"那次有些愉快"})
    with pytest.raises(Conflict):
        await coordinator.commit(lambda:memory.commit(stale,reviewed=True))
    assert await coordinator.commit(lambda:memory.expire_feelings(now=100+500*86400)) == 0
    store.close()


@pytest.mark.parametrize("explicit", [False, True])
async def test_chat_recall_requires_explicit_ds_choice_not_just_retrieval(tmp_path, explicit):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    from buwubot.models import Completion

    now=datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=12)
    store, engine, qq, models=build(tmp_path,lambda:now)
    msg=engine.ingest({"post_type":"message","message_type":"group","group_id":123,
        "self_id":11,"user_id":22,"message_id":1,"time":now.timestamp(),"sender":{"nickname":"甲"},
        "message":[{"type":"text","data":{"text":"助手还记得那次聊天吗"}}]})
    p=proposal().model_copy(update={"tier":"feeling","persona_id":engine.settings.persona_id,
        "subject_id":"","content":"那次讨论很开心","feeling_strength":.6})
    mid=await engine.coordinator.commit(lambda:engine.memory.commit(p,reviewed=True))
    async def retrieve(*args,**kwargs):
        return [engine.memory.with_reading_policy(engine.memory.get(mid),now=now.timestamp())]
    engine.memory.retrieve=retrieve
    async def chat(*args,**kwargs):
        return Completion("",engine.settings.primary_model,{"text":"记得那次讨论",
            "recall_feelings":[mid] if explicit else []})
    models.call=chat
    jid=engine.queue.enqueue("reply",{"group_id":"123","messages":[vars(msg)],
        "source_message_ids":[msg.id],"persona_id":engine.settings.persona_id},
        dedupe_key="recall-reply",priority=0,source_time=now.timestamp())
    await engine.process(engine.queue.get(jid))
    level=engine.memory.with_reading_policy(engine.memory.get(mid),now=now.timestamp())
    assert (level["initial_feeling"] > .6) is explicit
    assert len(qq.sent) == 1
    store.close()


async def test_old_proposal_receipt_survives_new_optional_strength_field(tmp_path):
    import hashlib
    import json

    store, memory, coordinator=setup_memory(tmp_path)
    p=proposal()
    mid=await coordinator.commit(lambda:memory.commit(p))
    # Fixed historical schema: newly introduced fields must never enter the old receipt.
    legacy_data={key:getattr(p,key) for key in ('group_id','subject_id','content','tier','tags',
        'source_message_ids','confidence','memory_id','expected_revision','snapshot_epoch',
        'event_time','reason','origin_model')}
    legacy=hashlib.sha256(json.dumps(legacy_data,
        sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    assert memory.proposal_key(p)==legacy
    assert memory.proposal_key(p.model_copy(update={'maintenance_epoch':100}))==legacy
    with store.transaction() as db:
        db.execute("UPDATE memory_applied SET proposal_key=? WHERE memory_id=?",(legacy,mid))
    memory.cleanup(now=8*86400)
    assert await coordinator.commit(lambda:memory.commit(p)) == mid
    store.close()
