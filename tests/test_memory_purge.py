import pytest
from test_memory import proposal, setup_memory

from buwubot.memory_dynamics import DAY


async def test_deleted_body_and_versions_are_purged_after_grace_but_barrier_remains(tmp_path,monkeypatch):
    store,memory,coordinator=setup_memory(tmp_path)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    mid=await coordinator.commit(lambda:memory.commit(proposal(event_time=100)))
    original=memory.get(mid)
    await coordinator.commit(lambda:memory.delete(mid,expected_revision=1))
    store.connection.execute('CREATE TABLE memory_schema_origins(memory_id TEXT PRIMARY KEY,before_payload TEXT)')
    store.connection.execute('INSERT INTO memory_schema_origins VALUES(?,?)',(mid,original['content']))
    store.set_state('memory-migration:test',{'records':[{'before':original}]})
    store.set_state('memory-maintenance:test',{'records':[{'before':original}]})
    assert await coordinator.commit(lambda:memory.purge_deleted(now=100+6*DAY))==0
    assert memory.get(mid,include_deleted=True)['content']==original['content']
    assert await coordinator.commit(lambda:memory.purge_deleted(now=100+8*DAY))==1
    tombstone=memory.get(mid,include_deleted=True)
    assert tombstone['deleted'] and tombstone['content']=='' and tombstone['tags']==[]
    assert tombstone['content_key']==original['content_key']
    assert not store.rows('SELECT * FROM memory_versions WHERE memory_id=?',(mid,))
    assert not store.rows('SELECT * FROM memory_schema_origins WHERE memory_id=?',(mid,))
    assert store.get_state('memory-migration:test') is None
    assert store.get_state('memory-maintenance:test') is None
    assert store.rows('SELECT * FROM memory_manual_guards WHERE memory_id=?',(mid,))
    await coordinator.commit(lambda:memory.commit(proposal(event_time=100),reviewed=True))
    assert memory.get(mid) is None
    with pytest.raises(ValueError):
        await coordinator.commit(lambda:memory.commit(proposal(event_time=100,reason='late retry'),reviewed=True))
    assert await coordinator.commit(lambda:memory.purge_deleted(now=100+9*DAY))==0
    assert store.rows('PRAGMA quick_check')[0][0]=='ok'
    store.close()


def test_compaction_returns_free_database_pages_to_filesystem(tmp_path):
    from buwubot.store import Store
    store=Store(tmp_path/'space.db')
    with store.transaction() as db:
        db.execute('CREATE TABLE bulk(value BLOB)')
        for _ in range(8):db.execute('INSERT INTO bulk VALUES(zeroblob(262144))')
    store.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    before=store.path.stat().st_size
    with store.transaction() as db:db.execute('DELETE FROM bulk')
    assert store.compact_if_needed()
    assert store.path.stat().st_size<before/2
    assert not store.compact_if_needed()
    store.close()
