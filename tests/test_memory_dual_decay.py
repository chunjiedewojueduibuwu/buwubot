import pytest

from buwubot.memory_dynamics import (
    DAY,
    DynamicsPolicy,
    MemoryAnchor,
    advance,
    half_lives,
    reinforced,
)


def dual(**kwargs):
    return DynamicsPolicy(curve_version=2, **kwargs)


def test_dual_curve_has_calibrated_half_life_and_a_real_relative_tail():
    p=dual();a=MemoryAnchor()
    assert advance(a,p,now=7*DAY)['remaining']==pytest.approx(.5)
    values=[advance(a,p,now=t*DAY)['remaining'] for t in [0,7,14,70,77]]
    assert values[1]/values[0]<values[4]/values[3]<1
    assert half_lives(a,p)['meaning']==pytest.approx(7*DAY)


def test_meaning_mainly_changes_fast_trace_consolidation_mainly_slow():
    p=dual()
    base=half_lives(MemoryAnchor(),p)
    meaningful=half_lives(MemoryAnchor(significance=.8),p)
    consolidated=half_lives(MemoryAnchor(consolidation=.5),p)
    assert meaningful['fast']/base['fast']>meaningful['slow']/base['slow']>1
    assert consolidated['slow']/base['slow']>consolidated['fast']/base['fast']>1


def test_dual_reanchoring_preserves_tail_composition_and_every_current_level():
    p=dual();a=MemoryAnchor(significance=.7,emotion=.6,consolidation=.2)
    direct=advance(a,p,now=45*DAY)
    split=MemoryAnchor(**(advance(a,p,now=10*DAY)|{'since':10*DAY}))
    assert advance(split,p,now=45*DAY)==pytest.approx(direct)


def test_dual_recall_rewinds_age_but_never_restores_missing_details_or_changes_base_meaning():
    p=dual();a=MemoryAnchor(significance=.5)
    before=advance(a,p,now=14*DAY)
    recalled=reinforced(a,p,now=14*DAY,consolidation=.2)
    assert before['remaining']<recalled.remaining<1
    assert recalled.detail==before['detail']
    assert recalled.initial_significance==a.initial_significance
    assert 0<=recalled.recall_meaning<=.05
    assert advance(recalled,p,now=40*DAY)['remaining']>advance(a,p,now=40*DAY)['remaining']


def test_repeated_recall_meaning_bonus_is_bounded():
    p=dual();a=MemoryAnchor()
    for n in range(200):
        a=reinforced(a,p,now=(n+1)*DAY,consolidation=.9)
    assert a.initial_significance==.5
    assert 0<a.recall_meaning<=.05


async def recalled(directory, times):
    from test_memory import proposal, setup_memory
    store,memory,coordinator=setup_memory(directory)
    mid=await coordinator.commit(lambda:memory.commit(proposal(event_time=100)))
    item=memory.get(mid);signals=memory.lifecycle.signals
    await coordinator.commit(lambda:signals.initialize(item,MemoryAnchor(since=100),dual()))
    for index,when in enumerate(times):
        await coordinator.commit(lambda index=index,when=when:signals.recall(
            item,event_id=f'source:real-{index}',now=when,spacing_half_life=6*3600))
    return store,memory,coordinator,signals,item


async def test_dense_sources_share_one_gain_and_one_consolidation_credit(tmp_path):
    one=await recalled(tmp_path/'one',[100+DAY])
    dense=await recalled(tmp_path/'dense',[100+DAY]*100)
    spaced=await recalled(tmp_path/'spaced',[100+DAY,100+2*DAY])
    try:
        a=one[3].view(one[4],now=100+3*DAY)
        b=dense[3].view(dense[4],now=100+3*DAY)
        c=spaced[3].view(spaced[4],now=100+3*DAY)
        assert a==pytest.approx(b)
        assert c['consolidation']>b['consolidation']
        assert c['remaining']>b['remaining']
        assert len(dense[0].rows('SELECT * FROM memory_signal_events'))==100
    finally:
        for data in [one,dense,spaced]:data[0].close()


async def test_merge_cannot_bypass_dense_recall_limit(tmp_path):
    store,memory,coordinator,signals,item=await recalled(tmp_path,[100+DAY])
    before=signals.view(item,now=100+DAY)
    for index in range(10):
        await coordinator.commit(lambda index=index:memory.lifecycle.recall_merge(
            item,memory.settings.memory_policies,operation_id=f'merge-{index}',count=2,now=100+DAY))
    assert signals.view(item,now=100+DAY)==pytest.approx(before)
    store.close()


async def test_curve_selection_preserves_current_values_and_forecast_matches_actual_curve(tmp_path):
    from test_memory_signals import initialized

    from buwubot.config import MemoryPolicies
    store,memory,coordinator,signals,item,_=await initialized(tmp_path)
    policies=MemoryPolicies()
    policies.secondary.curve_shape='dual'
    before=signals.view(item,now=100+DAY)
    await coordinator.commit(lambda:signals.reanchor(item,policy=policies.secondary.curve(),now=100+DAY))
    assert signals.view(item,now=100+DAY)==pytest.approx(before)
    view=memory.lifecycle.view(item,policies,now=100+DAY)
    forecast=memory.lifecycle.observation(item,view,policies)
    when=forecast['memory_threshold_at']
    assert signals.view(item,now=when)['remaining']==pytest.approx(policies.secondary.forget_threshold)
    assert signals._read(item)[1].curve_version==2
    store.close()


async def test_dense_ignored_receipts_do_not_claim_a_new_effective_recall(tmp_path):
    store,memory,_,_signals,item=await recalled(tmp_path,[100+DAY,110+DAY])
    view=memory.lifecycle.view(item,memory.settings.memory_policies,now=200+DAY)
    result=memory.lifecycle.observation(item,view,memory.settings.memory_policies)
    assert result['memory_last_reinforced_at']==100+DAY
    store.close()


async def test_delayed_real_recall_survives_a_pure_policy_reanchor(tmp_path):
    store,memory,coordinator,signals,item=await recalled(tmp_path,[])
    await coordinator.commit(lambda:signals.reanchor(item,policy=dual(),now=100+2*DAY))
    before=signals.view(item,now=100+2*DAY)
    await coordinator.commit(lambda:signals.recall(item,event_id='source:delayed',now=100+DAY,
                                                spacing_half_life=6*3600))
    after=signals.view(item,now=100+2*DAY)
    assert after['consolidation']>before['consolidation']
    assert after['remaining']>before['remaining']
    assert after['detail']==before['detail']
    assert store.rows('SELECT time FROM memory_signal_events')[0][0]==100+DAY
    assert memory.get(item['id'])['event_time']==100
    store.close()


@pytest.mark.parametrize('gateway_change',[False,True])
def test_saving_memory_curves_does_not_reactivate_unrelated_failed_jobs(tmp_path,gateway_change):
    from fastapi.testclient import TestClient

    from buwubot.app import create_app
    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        queue=client.app.state.coordinator.queue
        jid=queue.enqueue('scan',{'group_id':'123'},dedupe_key='failed-chat',priority=0,source_time=100)
        queue.defer(jid,'keep diagnostic',now=100,needs_attention=True)
        before=queue.get(jid)
        cfg=client.get('/api/config').json()
        policies=cfg['settings']['memory_policies']
        policies['recent']['curve_shape']='dual'
        changes={'memory_policies':policies,
            **{key:cfg['settings'][key] for key in ('memory_deleted_retention_days',
                'memory_relation_expansion','memory_relation_depth','mood_half_life_minutes')}}
        if gateway_change:changes['workbuddy_url']='http://127.0.0.1:4000/v1'
        result=client.patch('/api/config',json={'revision':cfg['revision'],'changes':changes})
        assert result.status_code==200
        if gateway_change:assert queue.get(jid)['state']=='pending'
        else:assert queue.get(jid)==before


async def test_delayed_merge_uses_original_event_time_not_maintenance_time(tmp_path,monkeypatch):
    from test_memory_merge import duplicates, merged

    from buwubot.consolidation import MemoryConsolidation
    store,memory,coordinator,ids=await duplicates(tmp_path)
    original=[memory.get(mid) for mid in ids]
    for item in original:
        await coordinator.commit(lambda item=item:memory.lifecycle.signals.initialize(
            item,MemoryAnchor(since=item['event_time']),dual()))
    service=MemoryConsolidation(memory)
    snapshot=service.snapshot(mutation_since=0)
    monkeypatch.setattr('buwubot.consolidation.time.time',lambda:original[1]['event_time']+10*DAY)
    await coordinator.commit(lambda:service.commit(snapshot,merged(ids),reviewed=True))
    receipt=store.rows('SELECT * FROM memory_signal_events WHERE memory_id=?',(ids[0],))[0]
    assert receipt['time']==original[1]['event_time']
    target=memory.get(ids[0])
    actual=memory.lifecycle.signals.view(target,now=original[1]['event_time']+10*DAY)['remaining']
    assert store.rows('SELECT after_strength FROM memory_merge_recalls')[0][0]==pytest.approx(actual)
    store.close()
