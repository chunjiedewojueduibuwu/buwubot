from fastapi.testclient import TestClient

from buwubot.app import create_app


def test_core_unification_is_atomic_versioned_and_preserves_noncore(tmp_path):
    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        cfg=client.get('/api/config').json()
        old=client.post('/api/memories',json={'config_revision':cfg['revision'],
            'tier':'core','content':'助手是AI；平时简短。'}).json()
        body={'revision':cfg['revision'],'expected_core_versions':{old['id']:1},
              'persona':'助手是AI。','items':[{'content':'日常表达简短。','tags':['表达习惯']} ]}
        stale=body|{'expected_core_versions':{old['id']:99}}
        assert client.post('/api/persona-core/unify',json=stale).status_code==409
        assert client.get('/api/config').json()==cfg
        response=client.post('/api/persona-core/unify',json=body)
        assert response.status_code==200,response.text
        after=client.get('/api/config').json()
        assert after['settings']['persona']=='助手是AI。'
        assert after['revision']==cfg['revision']+1
        core=client.get('/api/memories?tier=core').json()['items']
        assert [item['content'] for item in core]==['日常表达简短。']
        state=client.app.state
        assert state.memory.get(old['id']) is None
        assert state.store.rows('SELECT * FROM memory_versions WHERE memory_id=?',(old['id'],))
        assert client.post('/api/persona-core/unify',json=body).status_code==409
