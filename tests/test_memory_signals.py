import json
from dataclasses import asdict

import pytest
from test_memory import proposal, setup_memory

from buwubot.memory_dynamics import DAY, DynamicsPolicy, MemoryAnchor, advance
from buwubot.memory_signals import MemorySignals
from buwubot.store import Conflict, Store


async def spaced_sources(directory,monkeypatch,events,*,delay=0):
    store,memory,coordinator=setup_memory(directory)
    store.set_state('memory_schema_version',2)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    base=proposal().model_copy(update={'tier':'recent','significance':.5,'memory_schema_version':2,'event_time':100})
    mid=await coordinator.commit(lambda:memory.commit(base))
    template=memory.archive.get('123:id:1')
    for index,when in events:
        memory.archive.accept(template.__class__(**(vars(template)|{'id':f'123:id:{index}',
            'message_id':str(index),'time':when,'text':'我还是更喜欢茶。',
            'segments':[{'type':'text','data':{'text':'我还是更喜欢茶。'}}]})),memory.settings)
        monkeypatch.setattr('buwubot.memory.time.time',lambda when=when:max(when,10*DAY)+delay)
        current=memory.get(mid)
        update=base.model_copy(update={'memory_id':mid,'expected_revision':current['revision'],
            'source_message_ids':list(dict.fromkeys(current['source_message_ids']+[f'123:id:{index}'])),
            'reinforcement_source_ids':[f'123:id:{index}'],'snapshot_epoch':memory.epoch()})
        await coordinator.commit(lambda update=update:memory.commit(update,reviewed=True))
    current=memory.get(mid)
    level=memory.lifecycle.signals.view(current,now=20*DAY)['consolidation']
    receipts=[dict(row) for row in store.rows('SELECT * FROM memory_signal_events ORDER BY time,event_id')]
    return store,memory,coordinator,current,level,receipts,update


async def test_v2_spaced_confirmations_consolidate_more_than_same_second_sources(tmp_path,monkeypatch):
    dense=await spaced_sources(tmp_path/'dense',monkeypatch,[(2,200),(3,200)])
    spaced=await spaced_sources(tmp_path/'spaced',monkeypatch,[(2,100+DAY),(3,100+2*DAY)])
    try:
        assert len(dense[5])==len(spaced[5])==2
        assert 0<dense[4]<.2
        assert spaced[4]>.3 and spaced[4]>dense[4]
        assert all(row['spacing_half_life']>0 for row in dense[5])
    finally:
        dense[0].close();spaced[0].close()


async def test_v2_late_sources_and_processing_delay_do_not_change_final_consolidation(tmp_path,monkeypatch):
    chronological=await spaced_sources(tmp_path/'ordered',monkeypatch,[(2,200),(3,100+DAY)])
    late=await spaced_sources(tmp_path/'late',monkeypatch,[(3,100+DAY),(2,200)],delay=10*DAY)
    try:
        assert len(late[5])==2
        assert late[4]==pytest.approx(chronological[4])
        assert [r['time'] for r in late[5]]==[200,100+DAY]
        count=len(late[5])
        await late[2].commit(lambda:late[1].commit(late[6],reviewed=True))
        assert len(late[0].rows('SELECT * FROM memory_signal_events'))==count
    finally:
        chronological[0].close();late[0].close()


async def initialized(tmp_path):
    store,memory,coordinator=setup_memory(tmp_path)
    mid=await coordinator.commit(lambda:memory.commit(proposal()))
    signals=MemorySignals(store)
    item=memory.get(mid)
    anchor=MemoryAnchor(significance=.7,emotion=.8,valence=-.6,since=100)
    await coordinator.commit(lambda:signals.initialize(item,anchor,DynamicsPolicy()))
    return store,memory,coordinator,signals,item,anchor


async def test_signals_persist_without_touching_canonical_facts_or_read_side_writes(tmp_path):
    store,memory,_,signals,item,anchor=await initialized(tmp_path)
    before=[tuple(row) for row in store.rows('SELECT * FROM memory_signals')]
    view=signals.view(item,now=100+7*DAY)
    assert view['significance']==pytest.approx(advance(anchor,DynamicsPolicy(),now=100+7*DAY)['significance'])
    assert view['valence']==-.6
    assert memory.get(item['id'])==item
    assert before==[tuple(row) for row in store.rows('SELECT * FROM memory_signals')]
    store.close()
    reopened=Store(tmp_path/'bot.db')
    try:
        assert MemorySignals(reopened).view(item,now=100+7*DAY)==view
    finally:
        reopened.close()


async def test_reinforcement_receipt_is_atomic_and_only_new_events_strengthen(tmp_path):
    store,memory,coordinator,signals,item,_=await initialized(tmp_path)
    when=100+7*DAY
    before=signals.view(item,now=when)
    assert await coordinator.commit(lambda:signals.recall(item,event_id='verified:new-source',now=when))
    after=signals.view(item,now=when)
    assert after['remaining']>before['remaining'] and after['consolidation']>0
    assert not await coordinator.commit(lambda:signals.recall(item,event_id='verified:new-source',now=when+DAY))
    assert len(store.rows('SELECT * FROM memory_signal_events'))==1
    assert memory.get(item['id'])==item
    with pytest.raises(RuntimeError,match='rollback'):
        def failing():
            with store.transaction():
                signals.recall(item,event_id='verified:rollback',now=when+DAY)
                raise RuntimeError('rollback')
        await coordinator.commit(failing)
    assert len(store.rows('SELECT * FROM memory_signal_events'))==1
    assert signals.view(item,now=when)==after
    store.close()


async def test_spacing_preserves_legacy_receipts_and_rolls_back_new_source_atomically(tmp_path):
    store,memory,coordinator,signals,item,_=await initialized(tmp_path)
    await coordinator.commit(lambda:signals.recall(item,event_id='source:legacy',now=200))
    before=signals.view(item,now=201)
    await coordinator.commit(lambda:signals.recall(item,event_id='source:new',now=201,spacing_half_life=21600))
    assert signals.view(item,now=201)['consolidation']>before['consolidation']
    rows=[dict(row) for row in store.rows('SELECT * FROM memory_signal_events ORDER BY time')]
    assert rows[0]['spacing_half_life'] is None and rows[1]['spacing_half_life']==21600
    with pytest.raises(RuntimeError,match='rollback'):
        def failed():
            with store.transaction():
                signals.recall(item,event_id='source:rollback',now=202,spacing_half_life=21600)
                raise RuntimeError('rollback')
        await coordinator.commit(failed)
    assert [dict(row) for row in store.rows('SELECT * FROM memory_signal_events ORDER BY time')]==rows
    assert memory.get(item['id'])==item
    store.close()


def test_old_signal_event_table_gains_nullable_spacing_without_changing_rows(tmp_path):
    store=Store(tmp_path/'old.db')
    store.connection.executescript("CREATE TABLE memory_signal_events(memory_id TEXT,event_id TEXT,time REAL,PRIMARY KEY(memory_id,event_id));"
        "INSERT INTO memory_signal_events VALUES('old','source:one',100);")
    MemorySignals(store)
    row=dict(store.rows('SELECT * FROM memory_signal_events')[0])
    assert row=={'memory_id':'old','event_id':'source:one','time':100.,'spacing_half_life':None,'effective_gain':None}
    store.close()


@pytest.mark.parametrize('value',[True,float('nan'),float('inf'),0,10081])
def test_spacing_policy_rejects_invalid_intervals(value):
    from buwubot.config import FactualPolicy
    with pytest.raises(ValueError):FactualPolicy(spacing_half_life_minutes=value)


async def test_stale_revision_and_deleted_record_cannot_be_reinforced(tmp_path):
    store,memory,coordinator,signals,item,_=await initialized(tmp_path)
    await coordinator.commit(lambda:memory.edit(item['id'],content='更新过的事实',expected_revision=1))
    with pytest.raises(Conflict):
        await coordinator.commit(lambda:signals.recall(item,event_id='old-revision',now=DAY))
    assert not store.rows('SELECT * FROM memory_signal_events')
    assert signals.view(memory.get(item['id']),now=DAY)['valence']==-.6
    with pytest.raises(Conflict):
        signals.view(item,now=DAY)
    current=memory.get(item['id'])
    await coordinator.commit(lambda:memory.delete(item['id'],expected_revision=current['revision']))
    with pytest.raises(Conflict):
        signals.view(item,now=DAY)
    with pytest.raises(Conflict):
        await coordinator.commit(lambda:signals.recall(current,event_id='deleted',now=DAY))
    store.close()


async def test_mutations_require_coordinator_and_policy_change_preserves_elapsed_levels(tmp_path):
    store,_,coordinator,signals,item,_=await initialized(tmp_path)
    with pytest.raises(RuntimeError,match='coordinator'):
        signals.recall(item,event_id='untrusted',now=DAY)
    with pytest.raises(RuntimeError,match='coordinator'):
        signals.initialize(item,MemoryAnchor(),DynamicsPolicy())
    when=100+5*DAY
    before=signals.view(item,now=when)
    policy=DynamicsPolicy(base_half_life=180*DAY)
    await coordinator.commit(lambda:signals.reanchor(item,policy=policy,now=when))
    assert signals.view(item,now=when)==pytest.approx(before)
    row=store.rows('SELECT * FROM memory_signals')[0]
    assert json.loads(row['policy'])==asdict(policy)
    assert not store.rows('SELECT * FROM memory_signal_events')
    assert signals.view(item,now=when+DAY)['remaining']>signals.view(item,now=when)['remaining']*.99
    with pytest.raises(Conflict):
        await coordinator.commit(lambda:signals.initialize(item,MemoryAnchor(),policy))
    store.close()


async def test_canonical_edit_keeps_active_signals_and_merge_recall_uses_same_curves(tmp_path):
    store,memory,coordinator,signals,item,_=await initialized(tmp_path)
    before=signals.view(item,now=DAY)
    view=memory.with_reading_policy(item,now=DAY)
    assert view['memory_remaining']==pytest.approx(before['remaining'])
    assert view['meaning_strength']==pytest.approx(before['significance'])
    assert view['detail_integrity']==pytest.approx(before['detail'])
    await coordinator.commit(lambda:memory.edit(item['id'],content='甲偏爱茶',expected_revision=1))
    updated=memory.get(item['id'])
    assert signals.view(updated,now=DAY)==before
    before_merge=signals.view(updated,now=10*DAY)
    await coordinator.commit(lambda:memory.lifecycle.recall_merge(updated,memory.settings.memory_policies,
        operation_id='real-merge',count=1,now=10*DAY))
    strengthened=signals.view(updated,now=10*DAY)
    assert strengthened['remaining']>before_merge['remaining']
    assert strengthened['consolidation']>0
    await coordinator.commit(lambda:memory.lifecycle.recall_merge(updated,memory.settings.memory_policies,
        operation_id='real-merge',count=1,now=11*DAY))
    assert len(store.rows('SELECT * FROM memory_signal_events'))==1
    store.close()


async def test_two_real_sources_in_same_second_are_two_reinforcements(tmp_path):
    store,memory,coordinator,signals,item,_=await initialized(tmp_path)
    source=memory.archive.get('123:id:1')
    for number in [2,3]:
        message=source.__class__(**(vars(source)|{'id':f'123:id:{number}',
            'message_id':str(number),'time':200,
            'text':f'我又想起来喜欢茶{number}',
            'segments':[{'type':'text','data':{'text':f'我又想起来喜欢茶{number}'}}]}))
        memory.archive.accept(message,memory.settings)
    update=proposal().model_copy(update={'memory_id':item['id'],'expected_revision':1,
        'source_message_ids':['123:id:1','123:id:2','123:id:3'],
        'reinforcement_source_ids':['123:id:2','123:id:3'],'snapshot_epoch':memory.epoch()})
    await coordinator.commit(lambda:memory.commit(update,reviewed=True))
    assert len(store.rows('SELECT * FROM memory_signal_events'))==2
    updated=memory.get(item['id'])
    assert signals.view(updated,now=200)['consolidation']>.4
    await coordinator.commit(lambda:memory.commit(update,reviewed=True))
    assert len(store.rows('SELECT * FROM memory_signal_events'))==2
    store.close()


async def test_distilling_recent_v2_preserves_current_strength_and_lengthens_its_half_life(tmp_path,monkeypatch):
    from test_memory_distillation import distilled
    store,memory,coordinator=setup_memory(tmp_path)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    mid=await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={'tier':'recent'})))
    signals=MemorySignals(store)
    item=memory.get(mid)
    await coordinator.commit(lambda:signals.initialize(item,MemoryAnchor(since=100),DynamicsPolicy()))
    when=100+7*DAY
    before=signals.view(item,now=when)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:when)
    await coordinator.commit(lambda:memory.commit(distilled(memory,mid),reviewed=True))
    assert signals.view(item,now=when)==pytest.approx(before)
    assert memory.with_reading_policy(item,now=when)['memory_half_life_minutes']==pytest.approx(14*1440)
    assert signals.view(item,now=when+14*DAY)['remaining']==pytest.approx(.25)
    store.close()


async def test_v2_tier_change_preserves_elapsed_level_and_uses_destination_base(tmp_path,monkeypatch):
    store,memory,coordinator=setup_memory(tmp_path)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    mid=await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={'tier':'recent'})))
    signals=MemorySignals(store)
    item=memory.get(mid)
    await coordinator.commit(lambda:signals.initialize(item,MemoryAnchor(since=100),DynamicsPolicy()))
    when=100+7*DAY
    before=signals.view(item,now=when)
    monkeypatch.setattr('buwubot.memory.time.time',lambda:when)
    await coordinator.commit(lambda:memory.edit(mid,content=item['content'],tier='secondary',expected_revision=1))
    updated=memory.get(mid)
    assert signals.view(updated,now=when)==pytest.approx(before)
    assert memory.with_reading_policy(updated,now=when)['memory_half_life_minutes']==pytest.approx(180*1440)
    assert signals.view(updated,now=when+180*DAY)['remaining']==pytest.approx(.25)
    assert memory.get(mid)['confidence']==item['confidence']
    store.close()
