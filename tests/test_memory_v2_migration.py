import json
import sqlite3

import pytest
from test_memory import proposal, setup_memory

from buwubot.memory_migration import MemoryMigration
from buwubot.store import Conflict

DAY=86400


async def prepared(tmp_path,monkeypatch):
    store,memory,coordinator=setup_memory(tmp_path)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    originals=[]
    for tier in ['recent','secondary','important']:
        p=proposal().model_copy(update={'tier':tier,'content':f'甲的{tier}记录','event_time':100})
        mid=await coordinator.commit(lambda p=p:memory.commit(p,reviewed=True))
        originals.append(memory.get(mid))
    cfg,revision=store.settings()
    store.save_settings(cfg.model_copy(update={'runtime_enabled':False}),revision)
    migration=MemoryMigration(memory)
    preview=await coordinator.commit(lambda:migration.preview(now=100+7*DAY))
    return store,memory,coordinator,migration,preview,originals


async def test_preview_then_atomic_migration_preserves_content_ids_indexes_and_old_receipts(tmp_path,monkeypatch):
    store,memory,coordinator,migration,preview,originals=await prepared(tmp_path,monkeypatch)
    assert memory.schema_version()==1
    assert preview['counts']=={'recent':1,'secondary':1,'important':1}
    assert 'records' not in preview and 'settings' not in preview
    index_before=[tuple(row) for row in store.rows('SELECT * FROM index_outbox ORDER BY id')]
    receipt=await coordinator.commit(lambda:migration.apply(preview['token']))
    assert memory.schema_version()==2
    assert receipt['migrated']==3
    for original in originals:
        current=memory.get(original['id'])
        assert current==original|{'tier':'recent' if original['tier']=='recent' else 'long_term'}
        assert memory.with_reading_policy(current,now=100+7*DAY)['dynamics_version']==2
        rows=store.rows('SELECT before_payload FROM memory_schema_origins WHERE memory_id=?',(original['id'],))
        assert json.loads(rows[0][0])==original
    recent=memory.get(originals[0]['id'])
    assert memory.with_reading_policy(recent,now=100+7*DAY)['memory_remaining']==pytest.approx(.5)
    assert [tuple(row) for row in store.rows('SELECT * FROM index_outbox ORDER BY id')]==index_before
    assert not store.rows('SELECT * FROM memory_signal_events')  # Migration is not a recalled experience.
    assert await coordinator.commit(lambda:migration.apply(preview['token']))==receipt
    backup=sqlite3.connect(receipt['backup'])
    try:
        assert backup.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert {row[0] for row in backup.execute('SELECT tier FROM memories')}=={'recent','important','secondary'}
    finally:
        backup.close()
    store.close()


async def test_legacy_merge_receipts_migrate_with_unknown_spacing_and_no_new_experience(tmp_path,monkeypatch):
    from test_memory_merge import duplicates, merged

    from buwubot.consolidation import MemoryConsolidation

    monkeypatch.setattr('buwubot.memory.time.time',lambda:1_000_000_000)
    store,memory,coordinator,ids=await duplicates(tmp_path)
    service=MemoryConsolidation(memory)
    await coordinator.commit(lambda:service.commit(service.snapshot(mutation_since=0),merged(ids),reviewed=True))
    original=memory.get(ids[0])
    cfg,revision=store.settings();store.save_settings(cfg.model_copy(update={'runtime_enabled':False}),revision)
    migration=MemoryMigration(memory)
    preview=await coordinator.commit(lambda:migration.preview(now=1_000_000_001))
    await coordinator.commit(lambda:migration.apply(preview['token']))
    assert memory.schema_version()==2 and memory.get(ids[0])==original
    receipts=[dict(row) for row in store.rows('SELECT * FROM memory_signal_events')]
    assert len(receipts)==1 and receipts[0]['event_id'].startswith('merge:')
    assert receipts[0]['spacing_half_life'] is None
    assert not store.rows('SELECT * FROM mood_events')
    store.close()


@pytest.mark.parametrize('change',['resume','fact','job'])
async def test_changed_preview_or_running_work_prevents_migration(tmp_path,monkeypatch,change):
    store,memory,coordinator,migration,preview,_=await prepared(tmp_path,monkeypatch)
    if change=='resume':
        cfg,revision=store.settings();store.save_settings(cfg.model_copy(update={'runtime_enabled':True}),revision)
    elif change=='fact':
        await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={'content':'新事实'})))
    else:
        jid=coordinator.queue.enqueue('memory',{},dedupe_key='running',priority=1,source_time=1)
        store.connection.execute("UPDATE jobs SET state='running' WHERE id=?",(jid,))
    with pytest.raises(Conflict):
        await coordinator.commit(lambda:migration.apply(preview['token']))
    assert memory.schema_version()==1 and not store.rows('SELECT * FROM memory_signals')
    store.close()


async def test_partial_migration_failure_rolls_back_all_rows_and_version(tmp_path,monkeypatch):
    store,memory,coordinator,migration,preview,originals=await prepared(tmp_path,monkeypatch)
    # Trigger is schema, not application data; prepare a fresh preview after installing it.
    second=sorted(item['id'] for item in originals)[1]
    store.connection.execute("CREATE TRIGGER fixture_signal_failure BEFORE INSERT ON memory_signals "
        f"WHEN NEW.memory_id='{second}' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    preview=await coordinator.commit(lambda:migration.preview(now=100+7*DAY))
    with pytest.raises(sqlite3.IntegrityError):
        await coordinator.commit(lambda:migration.apply(preview['token']))
    assert memory.schema_version()==1
    assert all(memory.get(item['id'])==item for item in originals)
    assert not store.rows('SELECT * FROM memory_schema_origins')
    assert not store.rows('SELECT * FROM memory_signals')
    store.close()


async def test_migration_with_clock_rollback_does_not_start_decay_before_existing_anchor(tmp_path,monkeypatch):
    store,memory,coordinator,migration,_,originals=await prepared(tmp_path,monkeypatch)
    preview=await coordinator.commit(lambda:migration.preview(now=50))
    await coordinator.commit(lambda:migration.apply(preview['token']))
    recent=memory.get(originals[0]['id'])
    assert memory.lifecycle.signals._read(recent)[0].since==100
    assert memory.with_reading_policy(recent,now=75)['memory_remaining']==1
    assert memory.with_reading_policy(recent,now=100)['memory_remaining']==1
    store.close()


async def test_nonempty_graph_and_existing_vector_hit_remain_valid_after_migration(tmp_path):
    import time

    from test_memory_graph import facts, mention

    from buwubot.memory_graph import MemoryGraph

    store,memory,coordinator,ids=await facts(tmp_path)
    graph=MemoryGraph(memory)
    drafts=[mention(ids[0]),mention(ids[0],node_type='place',name='Harbor',aliases=[]),mention(ids[1])]
    edge={'source_index':0,'target_index':1,'relation_type':'located_at',
        'evidence_memory_ids':[ids[0]],'summary':'Alpha lives in Harbor','confidence':.9}
    await coordinator.commit(lambda:graph.apply(drafts,edges=[edge],origin_model=memory.settings.primary_model,reviewed=True))
    before=graph.entity_relations('Alpha')
    assert len(before)==1
    cfg,revision=store.settings();store.save_settings(cfg.model_copy(update={'runtime_enabled':False}),revision)
    migration=MemoryMigration(memory)
    preview=await coordinator.commit(lambda:migration.preview(now=time.time()))
    await coordinator.commit(lambda:migration.apply(preview['token']))
    assert graph.entity_relations('Alpha')==before
    assert len(graph.lookup('Alpha'))==2  # Same name still refers to two distinct QQ accounts.
    class ExistingVector:
        async def search(self,query,scope):
            return [{'app_id':ids[0],'revision':1,'tier':'important'}] if scope=='123' else []
    memory.index=ExistingVector()
    memory.search_text=lambda *args,**kwargs:[]
    memory.settings=memory.settings.model_copy(update={'memory_relation_expansion':False})
    found=await memory.retrieve('Alpha')
    assert len(found)==1 and found[0]['id']==ids[0] and found[0]['tier']=='long_term'
    memory.index=None
    assert await memory.retrieve('Alpha')==[]
    store.close()
