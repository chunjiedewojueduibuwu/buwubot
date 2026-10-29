import sys

from buwubot.config import Settings
from buwubot.embedding import EmbeddingIndex
from buwubot.store import Store


async def test_disabled_embedding_never_loads_model_or_calls_provider(tmp_path):
    store = Store(tmp_path / "bot.db")
    index = EmbeddingIndex(store, Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']), tmp_path)
    await index.start()
    assert index.status["state"] == "disabled"
    assert not index.status["loaded"]
    assert await index.search("茶", "123") == []
    assert "sentence_transformers" not in sys.modules
    await index.close()
    store.close()
async def test_cancellation_waits_for_index_thread_before_releasing_resources():
    import asyncio
    import threading

    from buwubot.embedding import _in_thread

    started, release = threading.Event(), threading.Event()

    def operation():
        started.set()
        release.wait(3)

    task = asyncio.create_task(_in_thread(operation))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
