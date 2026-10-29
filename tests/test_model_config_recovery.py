from fastapi.testclient import TestClient

from buwubot.app import create_app


def test_key_repair_only_retries_model_failures_preserving_quality_reviews(tmp_path):
    with TestClient(create_app(tmp_path, run_bot=False), base_url='http://127.0.0.1:8765') as client:
        queue = client.app.state.coordinator.queue
        errors = ['gateway HTTP 401', '模型认证或配置需要修复',
                  '审核未通过：人物指向错误', 'uncertain fact must remain a candidate']
        ids = []
        for i, error in enumerate(errors):
            jid = queue.enqueue('memory', {}, dedupe_key=f'fixture:{i}', priority=1, source_time=i)
            queue.defer(jid, error, now=100, needs_attention=True)
            ids.append(jid)
        cfg = client.get('/api/config').json()
        response = client.patch('/api/config', json={'revision': cfg['revision'],
                                                     'changes': {'workbuddy_key': 'fixture-new-key'}})
        assert response.status_code == 200
        assert 'fixture-new-key' not in response.text
        assert [queue.get(jid)['state'] for jid in ids] == ['pending', 'pending', 'needs_attention', 'needs_attention']
        assert [queue.get(jid)['last_error'] for jid in ids] == errors
