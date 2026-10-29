import math

import pytest
from test_memory import proposal, setup_memory


@pytest.mark.parametrize('mode',['active','protected','disabled','no_decay','reached'])
async def test_retention_observation_explains_threshold_without_mutation(tmp_path,mode):
    store,memory,coordinator=setup_memory(tmp_path)
    mid=await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={'tier':'recent','event_time':100})))
    item=memory.get(mid)
    view=memory.with_reading_policy(item,now=100)
    if mode=='protected':view['memory_protected']=True
    if mode=='disabled':memory.settings.memory_policies.recent.forget_threshold=0
    if mode=='no_decay':view['memory_half_life_minutes']=None
    if mode=='reached':view['memory_strength']=.005
    before=[tuple(r) for r in store.rows('SELECT * FROM memory_lifecycle')]
    result=memory.lifecycle.observation(item,view,memory.settings.memory_policies)
    assert result['memory_retention_status']==mode
    if mode=='active':
        assert result['memory_threshold_at']==pytest.approx(100+7*86400*math.log2(100))
    elif mode=='reached':assert result['memory_threshold_at']==100
    else:assert result['memory_threshold_at'] is None
    assert result['memory_last_reinforced_at'] is None
    assert [tuple(r) for r in store.rows('SELECT * FROM memory_lifecycle')]==before
    assert 'memory_threshold_at' not in memory.with_reading_policy(item,now=100)
    store.close()


async def test_observation_uses_real_receipts_not_edit_or_read_timestamp(tmp_path):
    store,memory,coordinator=setup_memory(tmp_path)
    store.set_state('memory_schema_version',2)
    p=proposal().model_copy(update={'tier':'recent','memory_schema_version':2,'event_time':100,'significance':.5})
    mid=await coordinator.commit(lambda:memory.commit(p))
    item=memory.get(mid)
    await coordinator.commit(lambda:memory.lifecycle.signals.recall(item,event_id='source:real-confirmation',now=200))
    view=memory.with_reading_policy(item,now=300)
    expected=memory.lifecycle.observation(item,view,memory.settings.memory_policies)
    assert expected['memory_last_reinforced_at']==200
    await coordinator.commit(lambda:memory.edit(mid,content='甲偏好红茶',expected_revision=1))
    changed=memory.get(mid)
    result=memory.lifecycle.observation(changed,memory.with_reading_policy(changed,now=400),memory.settings.memory_policies)
    assert result['memory_last_reinforced_at']==200
    assert result['memory_retention_status']=='protected'
    store.close()


async def test_no_forecast_for_core_feeling_and_out_of_range_dates(tmp_path):
    store,memory,coordinator=setup_memory(tmp_path)
    for tier in ['core','feeling','context']:
        assert memory.lifecycle.observation({'tier':tier},{},memory.settings.memory_policies)=={}
    mid=await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={'tier':'recent','event_time':100})))
    item=memory.get(mid);view=memory.with_reading_policy(item,now=100)
    view.update(memory_half_life_minutes=1e20)
    result=memory.lifecycle.observation(item,view,memory.settings.memory_policies)
    assert result['memory_retention_status']=='out_of_range' and result['memory_threshold_at'] is None
    store.close()


def test_admin_lists_and_searches_show_observation_only_in_management(tmp_path):
    from fastapi.testclient import TestClient

    from buwubot.app import create_app
    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        cfg=client.get('/api/config').json()
        response=client.patch('/api/config',json={'revision':cfg['revision'],'changes':{'group_id':'123'}})
        assert response.status_code==200
        cfg=response.json()
        created=client.post('/api/memories',json={'config_revision':cfg['revision'],'subject_id':'',
            'tier':'recent','role_memory':False,'content':'甲喜欢红茶','tags':[]})
        assert created.status_code==200, created.text
        for suffix in ['', '?query=红茶']:
            item=client.get('/api/memories'+suffix).json()['items'][0]
            assert item['memory_retention_status']=='protected'
            assert item['memory_threshold_at'] is None and item['memory_last_reinforced_at'] is None


async def test_tiny_legal_threshold_does_not_overflow_intermediate_ratio(tmp_path):
    store,memory,coordinator=setup_memory(tmp_path)
    mid=await coordinator.commit(lambda:memory.commit(proposal().model_copy(update={'tier':'recent','event_time':100})))
    item=memory.get(mid);view=memory.with_reading_policy(item,now=100)
    memory.settings.memory_policies.recent.forget_threshold=1e-320
    result=memory.lifecycle.observation(item,view,memory.settings.memory_policies)
    assert result['memory_retention_status']=='active'
    assert result['memory_threshold_at']==pytest.approx(100+7*86400*(-math.log2(1e-320)))
    store.close()
