from fastapi.testclient import TestClient
from test_admin_memory_maintenance import prepare

from buwubot.app import create_app


def test_local_migration_requires_pause_current_revision_and_explicit_apply(tmp_path):
    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        plan,mid=prepare(client)
        preview=client.post('/api/memory-migration/preview',json={'revision':plan['config_revision']})
        assert preview.status_code==200
        data=preview.json()
        assert data['status']=='prepared' and data['items'][0]['memory_id']==mid
        assert client.app.state.memory.schema_version()==1
        assert client.post('/api/memory-migration/apply',json={'token':data['token'],'confirm':False}).status_code==422
        applied=client.post('/api/memory-migration/apply',json={'token':data['token'],'confirm':True})
        assert applied.status_code==200
        assert client.app.state.memory.get(mid)['tier']=='long_term'
        assert client.post('/api/memory-migration/apply',json={'token':data['token'],'confirm':True}).json()==applied.json()
        config=client.get('/api/config').json()
        assert config['memory_schema_version']==2 and not config['settings']['runtime_enabled']
        assert set(config['settings']['memory_policies'])=={'core','context','recent','long_term','feeling'}
        assert client.post('/api/memory-migration/preview',json={'revision':plan['config_revision']},
            headers={'Origin':'https://other.invalid'}).status_code==403


def test_migration_is_refused_while_active_or_stale(tmp_path):
    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        initial=client.get('/api/config').json()
        assert client.patch('/api/config',json={'revision':initial['revision'],
            'changes':{'runtime_enabled':True}}).status_code==200
        revision=client.get('/api/config').json()['revision']
        assert client.post('/api/memory-migration/preview',json={'revision':revision}).status_code==409
        assert client.post('/api/memory-migration/preview',json={'revision':revision+1}).status_code==409


def test_v2_policy_change_and_manual_meaning_edit_preserve_remaining_levels(tmp_path,monkeypatch):
    import time

    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        plan,mid=prepare(client)
        preview=client.post('/api/memory-migration/preview',json={'revision':plan['config_revision']}).json()
        assert client.post('/api/memory-migration/apply',json={'token':preview['token'],'confirm':True}).status_code==200
        memory=client.app.state.memory
        fixed_now=time.time()+86400
        before=memory.with_reading_policy(memory.get(mid),now=fixed_now)
        monkeypatch.setattr('buwubot.admin.time.time',lambda:fixed_now)
        cfg=client.get('/api/config').json()
        policies=cfg['settings']['memory_policies']
        policies['long_term']['half_life_minutes']=360*1440
        changed=client.patch('/api/config',json={'revision':cfg['revision'],'changes':{'memory_policies':policies}})
        assert changed.status_code==200
        assert set(changed.json()['settings']['memory_policies'])=={'core','context','recent','long_term','feeling'}
        after=memory.with_reading_policy(memory.get(mid),now=fixed_now)
        assert abs(after['memory_remaining']-before['memory_remaining'])<1e-12
        assert abs(after['detail_integrity']-before['detail_integrity'])<1e-12
        assert after['memory_half_life_minutes']>before['memory_half_life_minutes']
        response=client.patch('/api/memories/'+mid,json={'revision':1,'content':memory.get(mid)['content'],'significance':.9})
        assert response.status_code==200
        final=memory.with_reading_policy(memory.get(mid),now=fixed_now)
        assert abs(final['memory_remaining']-after['memory_remaining'])<1e-12
        assert not client.app.state.store.rows('SELECT * FROM memory_signal_events')
