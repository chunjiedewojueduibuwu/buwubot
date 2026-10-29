import pytest

from buwubot.emotion import emotion_levels, reinforce


def test_feeling_reaches_zero_before_forgetting_starts_and_eventually_expires():
    args={"strength":.5,"since":100,"half_life_minutes":30,"low_days":2,"high_days":365}
    current, remaining=emotion_levels(now=100,**args)
    assert current == .5 and remaining == 1
    assert emotion_levels(now=100+1800,**args)[0] == pytest.approx(.25)
    assert emotion_levels(now=100+1800,**args)[1] == 1
    current, remaining=emotion_levels(now=100+365*86400+86400,**args)
    assert current == 0 and remaining == 0


def test_strong_memories_persist_longer_and_optionally_never_forget():
    values=[emotion_levels(strength=s,since=0,now=30*86400,half_life_minutes=30,
        low_days=2,high_days=365)[1] for s in [.1,.5,.9]]
    assert values[0] <= values[1] < values[2]
    assert emotion_levels(strength=.99,since=0,now=10000*86400,half_life_minutes=30,
        low_days=2,high_days=365,permanent_threshold=.95) == (0,1)
    assert .8 < reinforce(.8) < 1
    assert reinforce(1) == 1


def test_invalid_parameters_are_rejected():
    with pytest.raises(ValueError):
        emotion_levels(strength=2,since=0,now=10)
    with pytest.raises(ValueError):
        emotion_levels(strength=.5,since=0,now=10,low_days=20,high_days=2)
