import json

import pytest

from buwubot.config import Settings
from buwubot.coordinator import Coordinator, JobQueue
from buwubot.qq import MessageArchive, QQError, SendUncertain
from buwubot.store import Store


def test_queue_priority_deduplication_and_failure_survive_restart(tmp_path):
    path = tmp_path / "bot.db"
    store = Store(path)
    queue = JobQueue(store)
    ordinary = queue.enqueue("reply", {"text": "old"}, dedupe_key="a", priority=1, source_time=1)
    called = queue.enqueue("reply", {"text": "called"}, dedupe_key="b", priority=0, source_time=2)
    assert queue.enqueue("reply", {"text": "duplicate"}, dedupe_key="a", priority=1,
                         source_time=1) == ordinary
    assert queue.claim(100)["id"] == called
    queue.defer(called, "rate limited", now=100, retry_after=120)
    store.close()
    store = Store(path)
    queue = JobQueue(store)
    assert queue.claim(101)["id"] == ordinary
    queue.finish(ordinary)
    assert queue.claim(219) is None
    assert queue.claim(221)["id"] == called
    store.close()


class QQFixture:
    def __init__(self, result="ok", history=None):
        self.result = result
        self.sent = []
        self.records = history or []

    async def send(self, group_id, segments, reply_id=None):
        self.sent.append((group_id, segments, reply_id))
        if isinstance(self.result, Exception):
            raise self.result
        return "qq-88"

    async def history(self, *args, **kwargs):
        return self.records


async def test_confirmed_reply_atomically_preserves_one_persona_memory_task(tmp_path):
    store = Store(tmp_path / "bot.db")
    MessageArchive(store)
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    coordinator = Coordinator(store, QQFixture(), cfg)
    jid = coordinator.queue.enqueue("reply", {"group_id":"123", "persona_id":cfg.persona_id,
        "messages":[], "reply_id":"100"}, dedupe_key="commitment", priority=0, source_time=1)
    await coordinator.send(jid, [{"type":"text","data":{"text":"我会记住这个约定"}}], now=100)
    rows = store.rows("SELECT payload FROM jobs WHERE kind='memory'")
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["persona_id"] == cfg.persona_id
    assert payload["messages"][-1]["id"] == "outgoing:" + jid
    assert payload["messages"][-1]["message_id"] == "qq-88"
    coordinator._sent(jid, "qq-88")
    assert len(store.rows("SELECT id FROM jobs WHERE kind='memory'")) == 1
    store.close()


async def test_memory_enqueue_failure_leaves_send_reconcilable(tmp_path, monkeypatch):
    store = Store(tmp_path / "bot.db")
    MessageArchive(store)
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    segments = [{"type":"text","data":{"text":"我会记住"}}]
    qq = QQFixture(history=[{"user_id":11,"time":101,"message_id":88,"message":segments}])
    coordinator = Coordinator(store, qq, cfg)
    jid = coordinator.queue.enqueue("reply", {"group_id":"123", "persona_id":cfg.persona_id,
        "messages":[]}, dedupe_key="fault", priority=0, source_time=1)
    enqueue = coordinator.queue.enqueue
    def fail(*args, **kwargs):
        raise OSError("simulated storage failure")
    monkeypatch.setattr(coordinator.queue, "enqueue", fail)
    with pytest.raises(OSError):
        await coordinator.send(jid, segments, now=100)
    assert store.rows("SELECT status FROM outgoing")[0][0] == "inflight"
    monkeypatch.setattr(coordinator.queue, "enqueue", enqueue)
    await coordinator.recover()
    assert len(qq.sent) == 1
    assert coordinator.queue.get(jid)["state"] == "succeeded"
    assert len(store.rows("SELECT id FROM jobs WHERE kind='memory'")) == 1
    store.close()


async def test_explicit_send_failure_remains_pending_then_succeeds(tmp_path):
    store = Store(tmp_path / "bot.db")
    MessageArchive(store)
    queue = JobQueue(store)
    jid = queue.enqueue("reply", {"group_id": "123", "reply_id": "100"}, dedupe_key="test",
                        priority=0, source_time=1)
    qq = QQFixture(QQError("offline"))
    coordinator = Coordinator(store, qq, Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11"))
    await coordinator.send(jid, [{"type": "text", "data": {"text": "你好"}}], now=100)
    assert queue.get(jid)["state"] == "retry_wait"
    qq.result = "ok"
    await coordinator.send(jid, [{"type": "text", "data": {"text": "你好"}}], now=200)
    assert queue.get(jid)["state"] == "succeeded"
    # A duplicate completion must never send again.
    await coordinator.send(jid, [], now=201)
    assert len(qq.sent) == 2
    store.close()


async def test_uncertain_send_is_reconciled_without_duplicate(tmp_path):
    store = Store(tmp_path / "bot.db")
    MessageArchive(store)
    queue = JobQueue(store)
    jid = queue.enqueue("reply", {"group_id": "123", "reply_id": "100"}, dedupe_key="test",
                        priority=0, source_time=1)
    qq = QQFixture(SendUncertain("response lost"))
    coordinator = Coordinator(store, qq, Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11"))
    await coordinator.send(jid, [{"type": "text", "data": {"text": "你好"}}], now=100)
    assert queue.get(jid)["state"] == "running"
    qq.records = [{"user_id": 11, "time": 101, "message_id": 88, "message": [
        {"type": "reply", "data": {"id": "100"}}, {"type": "text", "data": {"text": "你好"}}]}]
    await coordinator.recover()
    assert queue.get(jid)["state"] == "succeeded"
    assert len(qq.sent) == 1
    store.close()


async def test_unverifiable_send_is_skipped_but_unsent_running_job_recovers(tmp_path):
    store = Store(tmp_path / "bot.db")
    MessageArchive(store)
    queue = JobQueue(store)
    first = queue.enqueue("reply", {"group_id": "123"}, dedupe_key="uncertain", priority=0,
                          source_time=1)
    coordinator = Coordinator(store, QQFixture(SendUncertain("lost")), Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123"))
    await coordinator.send(first, [{"type": "text", "data": {"text": "hello"}}], now=100)
    second = queue.enqueue("reply", {"group_id": "123"}, dedupe_key="not-sent", priority=0,
                           source_time=2)
    queue.claim(200)
    await coordinator.recover()
    assert queue.get(first)["state"] == "skipped_uncertain"
    assert queue.get(second)["state"] == "pending"
    events_before = store.connection.execute("SELECT count(*) FROM events").fetchone()[0]
    await coordinator.recover()
    assert store.connection.execute("SELECT count(*) FROM events").fetchone()[0] == events_before
    store.close()


async def test_reconnection_does_not_reset_a_live_background_job(tmp_path):
    store = Store(tmp_path / "bot.db")
    queue = JobQueue(store)
    jid = queue.enqueue("memory", {}, dedupe_key="live-memory", priority=1, source_time=1)
    queue.claim(200)
    coordinator = Coordinator(store, QQFixture(), Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123"))
    await coordinator.recover(reset_running=False)
    assert queue.get(jid)["state"] == "running"
    store.close()


async def test_recall_of_other_message_does_not_cancel_valid_reply_anchor(tmp_path):
    store = Store(tmp_path / "bot.db")
    archive = MessageArchive(store)
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", self_id="11")
    for number in (1, 2):
        archive.accept({"post_type":"message", "message_type":"group", "group_id":123,
                        "self_id":11, "user_id":22, "message_id":number,
                        "message":[{"type":"text","data":{"text":"同话题补充"}}]}, cfg)
    archive.accept({"notice_type":"group_recall", "group_id":123, "message_id":2}, cfg)
    qq = QQFixture()
    coordinator = Coordinator(store, qq, cfg)
    jid = coordinator.queue.enqueue("reply", {"group_id":"123", "reply_id":"1",
        "reply_source_id":"123:id:1", "source_message_ids":["123:id:1","123:id:2"]},
        dedupe_key="anchor", priority=0, source_time=1)
    await coordinator.send(jid, [{"type":"text","data":{"text":"回应同话题"}}])
    assert coordinator.queue.get(jid)["state"] == "succeeded"
    assert qq.sent[0][2] == "1"
    store.close()
