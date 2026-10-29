from fastapi.testclient import TestClient

from buwubot.app import create_app


def test_management_mood_has_revision_origin_and_persona_guards(tmp_path):
    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        cfg=client.get('/api/config').json()
        assert client.get('/api/mood').json()['intensity']==0
        body={'revision':cfg['revision'],'operation_id':'test-once','valence':-.5,'intensity':.8}
        assert client.post('/api/mood',json=body,headers={'Origin':'https://other.invalid'}).status_code==403
        assert client.post('/api/mood',json=body|{'revision':cfg['revision']+1}).status_code==409
        result=client.post('/api/mood',json=body)
        assert result.status_code==200
        assert result.json()['valence']==-.5 and result.json()['intensity']>0
        change=client.get('/api/mood').json()['changes'][0]
        assert change['kind']=='manual' and change['reason'] and change['memory_id'] is None
        assert client.post('/api/mood',json=body).status_code==200
        assert len(client.app.state.store.rows('SELECT * FROM mood_events'))==1
        changed=client.post('/api/mood',json=body|{'operation_id':'replace','valence':.5,'intensity':.2}).json()
        assert changed['valence']==.5 and abs(changed['intensity']-.2)<.001
        assert client.post('/api/mood',json=body|{'valence':2}).status_code==422
        assert client.post('/api/mood',json={'revision':cfg['revision'],'operation_id':'reset-once','reset':True}).status_code==200
        assert client.get('/api/mood').json()['intensity']==0
        assert not client.app.state.store.rows('SELECT * FROM memories')
