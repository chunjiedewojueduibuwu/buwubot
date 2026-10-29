import pytest
from test_memory import proposal, setup_memory

from buwubot.config import MemoryPolicies
from buwubot.memory_dynamics import DAY, advance


def legacy_policies():
    data=MemoryPolicies().model_dump()
    for tier,days in [('recent',60),('long_term',1095)]:
        data[tier].update(curve_shape='dual',max_idle_days=days)
    return MemoryPolicies.model_validate(data)


def test_legacy_idle_fields_load_but_are_discarded_and_cannot_be_enabled():
    policies=legacy_policies()
    assert all('max_idle_days' not in value for value in policies.model_dump().values())
    assert not hasattr(policies.recent,'max_idle_days')


def test_management_cannot_reenable_a_legacy_idle_limit(tmp_path):
    import json

    from fastapi.testclient import TestClient

    from buwubot.app import create_app
    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        store=client.app.state.store
        saved=json.loads(store.rows('SELECT value FROM settings')[0][0])
        saved['memory_policies']['recent']['max_idle_days']=60
        saved['memory_policies']['long_term']['max_idle_days']=1095
        with store.transaction() as db:
            db.execute('UPDATE settings SET value=?',(json.dumps(saved),))
        cfg=client.get('/api/config').json()
        assert 'max_idle_days' not in cfg['settings']['memory_policies']['recent']
        posted=cfg['settings']['memory_policies']
        posted['recent']['max_idle_days']=1
        result=client.patch('/api/config',json={'revision':cfg['revision'],'changes':{'memory_policies':posted}})
        assert result.status_code==200
        actual=json.loads(store.rows('SELECT value FROM settings')[0][0])
        for tier in ['recent','long_term']:
            saved['memory_policies'][tier].pop('max_idle_days')
        assert actual==saved


@pytest.mark.parametrize('tier,meaning,days',[
    ('recent',.5,61),('long_term',.9,3*365+1),('long_term',.9,5*365),
])
async def test_old_deadlines_do_not_hide_positive_curve_memory(tmp_path,monkeypatch,tier,meaning,days):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    memory.settings.memory_policies=legacy_policies()
    store.save_settings(memory.settings,store.settings()[1])
    monkeypatch.setattr('buwubot.memory.time.time',lambda:100)
    candidate=proposal().model_copy(update={'tier':tier,'event_time':100,
        'significance':meaning,'memory_schema_version':2})
    mid=await coordinator.commit(lambda:memory.commit(candidate,reviewed=True))
    item=memory.get(mid);now=100+days*DAY
    monkeypatch.setattr('buwubot.memory.time.time',lambda:now)
    expected=advance(*memory.lifecycle.signals._read(item),now=now)['remaining']
    view=memory.with_reading_policy(item,now=now)
    assert expected>getattr(memory.settings.memory_policies,tier).forget_threshold
    assert view['memory_remaining']==pytest.approx(expected)
    assert mid in {row['id'] for row in await memory.retrieve('喜欢茶')}
    forecast=memory.lifecycle.observation(item,view,memory.settings.memory_policies)
    when=forecast['memory_threshold_at']
    assert when>now
    assert advance(*memory.lifecycle.signals._read(item),now=when)['remaining']==pytest.approx(.01)
    assert await coordinator.commit(lambda:memory.expire_ordinary(now=now))==0
    assert await coordinator.commit(lambda:memory.expire_ordinary(now=when+DAY))==1
    assert memory.get(mid) is None
    assert await coordinator.commit(lambda:memory.purge_deleted(now=when+10*DAY))==1
    assert memory.get(mid,include_deleted=True)['content']==''
    store.close()
