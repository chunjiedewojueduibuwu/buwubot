import pytest

from buwubot.config import Settings
from buwubot.coordinator import Coordinator
from buwubot.embedding import EmbeddingIndex
from buwubot.memory import MemoryProposal, MemoryService
from buwubot.qq import MessageArchive
from buwubot.store import Store


class SmallEncoderFixture:
    """An external inference substitute; Mem0, Qdrant, and SQLite stay real."""
    def embed(self, text, memory_action=None):
        return [float("茶" in text), float("咖啡" in text), 0.1]

    def close(self):
        pass


async def test_mem0_outbox_replay_update_delete_and_stale_index_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    store = Store(tmp_path / "bot.db")
    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123", embedding_provider="api", embedding_dimensions=3,
                        embedding_url="http://127.0.0.1:9/v1", embedding_model="fixture")
    archive = MessageArchive(store)
    archive.accept({"post_type": "message", "message_type": "group", "group_id": 123,
                    "self_id": 11, "user_id": 22, "message_id": 1, "time": 100,
                    "sender": {"nickname": "甲"}, "message": []}, settings)
    memory = MemoryService(store, settings)
    coordinator = Coordinator(store, None, settings)
    p = MemoryProposal(group_id="123", subject_id="22", content="甲喜欢茶", tier="secondary",
                       source_message_ids=["123:id:1"])
    mid = await coordinator.commit(lambda: memory.commit(p))
    index = EmbeddingIndex(store, settings, tmp_path, encoder=SmallEncoderFixture())
    await index.start()
    assert index.status["state"] == "ready"
    assert await index.sync_pending() == 1
    assert (await index.search("茶", "123"))[0]["app_id"] == mid
    # Canonical deletion takes effect before the slower derived index is synchronized.
    await coordinator.commit(lambda: memory.delete(mid, expected_revision=1))
    memory.index = index
    assert await memory.retrieve("茶") == []
    assert await index.sync_pending() == 1
    assert await index.search("茶", "123") == []
    await index.close()
    store.close()


@pytest.mark.parametrize("damage",["metadata","sqlite"])
async def test_corrupted_qdrant_metadata_rebuilds_from_canonical_outbox(tmp_path,monkeypatch,damage):
    monkeypatch.setenv("MEM0_TELEMETRY","false")
    store=Store(tmp_path/"bot.db")
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",embedding_provider="api",embedding_dimensions=3,
                 embedding_url="http://127.0.0.1:9/v1",embedding_model="fixture")
    memory=MemoryService(store,cfg)
    memory.archive.accept({"post_type":"message","message_type":"group","group_id":123,
        "self_id":11,"user_id":22,"message_id":1,"time":100,"message":[]},cfg)
    coordinator=Coordinator(store,None,cfg)
    mid=await coordinator.commit(lambda:memory.commit(MemoryProposal(group_id="123",subject_id="22",
        content="甲喜欢茶",source_message_ids=["123:id:1"])))
    index=EmbeddingIndex(store,cfg,tmp_path,encoder=SmallEncoderFixture())
    await index.start()
    await index.sync_pending()
    await index.close()
    directory=tmp_path/"indexes"/index.fingerprint
    path=directory/"qdrant"/("meta.json" if damage=="metadata" else "collection/buwubot/storage.sqlite")
    path.write_bytes(b"corrupt data")
    restored=EmbeddingIndex(store,cfg,tmp_path,encoder=SmallEncoderFixture())
    try:
        await restored.start()
        assert restored.status["state"]=="ready",restored.status
        assert memory.get(mid)["revision"]==1
        assert await restored.sync_pending()==1
        assert (await restored.search("茶","123"))[0]["app_id"]==mid
        assert len(list((tmp_path/"indexes").glob(index.fingerprint+".corrupt-*")))==1
    finally:
        await restored.close()
        store.close()
