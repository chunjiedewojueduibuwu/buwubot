import math

import pytest

from buwubot.memory_dynamics import (
    DynamicsPolicy,
    MemoryAnchor,
    advance,
    consolidation_level,
    half_lives,
    recall_score,
)

DAY=86400


def test_significance_and_real_consolidation_participate_in_half_life():
    policy=DynamicsPolicy(base_half_life=7*DAY)
    ordinary=MemoryAnchor(significance=.5)
    assert half_lives(ordinary,policy)['meaning']==pytest.approx(7*DAY)
    assert half_lives(MemoryAnchor(significance=.9),policy)['meaning']>half_lives(ordinary,policy)['meaning']
    assert half_lives(MemoryAnchor(significance=.1),policy)['meaning']<half_lives(ordinary,policy)['meaning']
    assert half_lives(MemoryAnchor(significance=.5,consolidation=.5),policy)['meaning']>7*DAY


def test_detail_curve_uses_emotion_consolidation_interference_and_stays_faster_than_meaning():
    p=DynamicsPolicy();base=half_lives(MemoryAnchor(),p)
    emotional=half_lives(MemoryAnchor(emotion=.8),p)
    consolidated=half_lives(MemoryAnchor(consolidation=.8),p)
    interference=half_lives(MemoryAnchor(interference=.8),p)
    assert emotional['detail']>base['detail'] and consolidated['detail']>base['detail']
    assert interference['detail']<base['detail']
    for value in [base,emotional,consolidated,interference]:assert 0<value['detail']<value['meaning']


def test_each_curve_reaches_half_at_its_half_life_and_does_not_grow_with_time():
    a=MemoryAnchor(significance=.65,detail=.9,emotion=.7,valence=-.6,since=100)
    p=DynamicsPolicy();h=half_lives(a,p)
    assert advance(a,p,now=100+h['meaning'])['significance']==pytest.approx(a.significance/2)
    assert advance(a,p,now=100+h['detail'])['detail']==pytest.approx(a.detail/2)
    assert advance(a,p,now=100+h['emotion'])['emotion']==pytest.approx(a.emotion/2)
    previous=advance(a,p,now=100)
    for days in [1,7,30,180,3650]:
        current=advance(a,p,now=100+days*DAY)
        assert all(current[k]<=previous[k] for k in ['significance','detail','emotion','remaining'])
        assert current['valence']==-.6
        previous=current
    assert advance(a,p,now=0)==advance(a,p,now=100)


def test_positive_and_negative_emotion_have_equal_retention_when_intensity_matches():
    p=DynamicsPolicy()
    positive=MemoryAnchor(emotion=.8,valence=.9)
    negative=MemoryAnchor(emotion=.8,valence=-.9)
    assert half_lives(positive,p)==half_lives(negative,p)
    assert advance(positive,p,now=DAY)['emotion']==advance(negative,p,now=DAY)['emotion']


def test_cue_can_raise_old_memory_recall_without_pretending_it_is_recent_or_certain():
    p=DynamicsPolicy()
    values={'significance':.1,'emotion':.05,'recency':0.,'recall':0.,'interference':.1}
    weak=recall_score(**values,cue=0,policy=p)
    strong=recall_score(**values,cue=1,policy=p)
    assert weak<p.recall_threshold<strong
    assert recall_score(**(values|{'interference':.9}),cue=1,policy=p)<strong
    assert 0<=weak<strong<=1


def test_consolidation_is_bounded_and_only_depends_on_real_event_count():
    assert consolidation_level(0)==0
    values=[consolidation_level(n) for n in range(30)]
    assert values==sorted(values) and values[-1]<1
    assert consolidation_level(2)==consolidation_level(2)


@pytest.mark.parametrize('data',[
    {'significance':-1},{'emotion':1.1},{'detail':math.nan},{'valence':math.inf},
    {'consolidation':-.1},{'interference':1.01},{'since':math.nan},
])
def test_invalid_signal_values_are_rejected(data):
    with pytest.raises(ValueError):MemoryAnchor(**data)


@pytest.mark.parametrize('data',[
    {'base_half_life':0},{'meaning_influence':math.inf},{'emotion_detail_protection':1},
    {'consolidation_detail_protection':-1},{'minimum_half_life':10,'maximum_half_life':1},
    {'recall_threshold':2},{'cue_weight':math.nan},
])
def test_invalid_or_explosive_policy_values_are_rejected(data):
    with pytest.raises(ValueError):DynamicsPolicy(**data)


def test_extreme_elapsed_time_and_coefficients_stay_finite_and_bounded():
    p=DynamicsPolicy(meaning_influence=16,maximum_half_life=36500*DAY)
    a=MemoryAnchor(significance=1,emotion=1,consolidation=1,since=-1e308)
    assert half_lives(a,p)['meaning']<=p.maximum_half_life
    values=advance(a,p,now=1e308)
    assert all(math.isfinite(v) for v in values.values())
    assert values['remaining']==0


def test_reanchoring_without_new_event_does_not_reinterpret_elapsed_history():
    policy=DynamicsPolicy()
    original=MemoryAnchor(significance=.7,emotion=.8,valence=.5,consolidation=.2)
    once=advance(original,policy,now=6*DAY)
    halfway=MemoryAnchor(**(advance(original,policy,now=3*DAY)|{'since':3*DAY}))
    split=advance(halfway,policy,now=6*DAY)
    for key in ['significance','detail','emotion','remaining']:
        assert split[key]==pytest.approx(once[key])


def test_actual_reinforcement_never_makes_future_retention_weaker():
    from buwubot.memory_dynamics import reinforced
    policy=DynamicsPolicy()
    original=MemoryAnchor(significance=.65,emotion=.8)
    recalled=reinforced(original,policy,now=7*DAY,consolidation=consolidation_level(1))
    old_future=advance(original,policy,now=30*DAY)
    new_future=advance(recalled,policy,now=30*DAY)
    assert recalled.since==7*DAY
    assert recalled.remaining>advance(original,policy,now=7*DAY)['remaining']
    for key in ['significance','detail','emotion','remaining']:
        assert new_future[key]>=old_future[key]


def test_detail_reanchoring_with_high_protection_and_rate_floor():
    from dataclasses import replace
    policy=DynamicsPolicy(detail_ratio=.9,emotion_detail_protection=.999,
                          consolidation_detail_protection=.999)
    for significance in [0,.2,.8,1]:
        for consolidation in [0,.5,1]:
            original=MemoryAnchor(significance=significance,emotion=.99,consolidation=consolidation)
            for days in [.001,1,30,10000]:
                now=days*DAY
                first=MemoryAnchor(**(advance(original,policy,now=now/3)|{'since':now/3}))
                split=advance(first,policy,now=now)
                once=advance(original,policy,now=now)
                assert split['detail']==pytest.approx(once['detail'],rel=1e-9,abs=1e-14)
                assert split['remaining']==pytest.approx(once['remaining'])
            # Both policies may reach the same safety floor under strong protection.
            assert half_lives(original,replace(policy,detail_ratio=.1))['detail']<=half_lives(original,policy)['detail']


def test_zero_feeling_stays_zero_on_recall_and_zero_gains_only_consolidate():
    from buwubot.memory_dynamics import reinforced
    p=DynamicsPolicy()
    a=MemoryAnchor()
    result=reinforced(a,p,now=DAY,consolidation=.5)
    assert result.emotion==result.initial_emotion==0
    plain=reinforced(a,p,now=DAY,consolidation=0,significance_gain=0,detail_gain=0,emotion_gain=0)
    assert advance(plain,p,now=7*DAY)==pytest.approx(advance(a,p,now=7*DAY))
