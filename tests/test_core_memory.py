import json
from copy import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from buwubot.app import create_app
from buwubot.memory import MemoryProposal
from buwubot.models import PRIMARY_ROLES, ModelClient


def test_core_memory_is_role_wide_manual_only_and_survives_restart(tmp_path):
    with TestClient(create_app(tmp_path, run_bot=False), base_url="http://127.0.0.1:8765") as client:
        cfg = client.get('/api/config').json()
        cfg = client.patch('/api/config', json={'revision': cfg['revision'], 'changes': {
            'group_id': '123', 'group_ids': ['456']}}).json()
        response = client.post('/api/memories', json={'config_revision': cfg['revision'],
            'content': '使用已确认的称呼', 'tier': 'core', 'tags': ['初始设定']})
        assert response.status_code == 200
        record = response.json()
        assert record['group_id'] == 'persona:core:assistant' and record['persona_id'] == 'assistant'
        for scope in ['123', '456', 'private:10001']:
            visible = client.get('/api/memories', params={'conversation_id': scope, 'tier': 'core'}).json()
            assert [m['id'] for m in visible['items']] == [record['id']]
        state = client.app.state
        assert state.memory.core_context() == ['使用已确认的称呼']
        attempted = MemoryProposal(group_id='persona:core:assistant', persona_id='assistant',
            content='自动覆盖核心', tier='core', source_message_ids=['manual:fake'])
        with pytest.raises(ValueError, match='manual'):
            client.portal.call(state.coordinator.commit, lambda: state.memory.commit(attempted, reviewed=True))
        assert client.patch('/api/memories/' + record['id'], json={'revision': 1,
            'content': '称呼仍稳定', 'tier': 'important'}).status_code == 422
        assert not state.store.rows('SELECT * FROM index_outbox WHERE memory_id=?', (record['id'],))
        other = copy(state.memory)
        other.settings = state.memory.settings.model_copy(update={'persona_id': 'other'})
        assert other.core_context() == []
        assert client.post('/api/memories', json={'config_revision': cfg['revision'],
            'content': '不能共享给全部角色', 'tier': 'core', 'role_memory': False}).status_code == 422
    with TestClient(create_app(tmp_path, run_bot=False), base_url="http://127.0.0.1:8765") as client:
        assert client.app.state.memory.core_context() == ['使用已确认的称呼']
        r = client.patch('/api/memories/' + record['id'], json={'revision': 1,
            'content': '称呼已经人工调整', 'tier': 'core'})
        assert r.status_code == 200
        assert client.app.state.memory.core_context() == ['称呼已经人工调整']
        assert client.request('DELETE', '/api/memories/' + record['id'], params={'revision': 2}, json={}).status_code == 200
        assert client.app.state.memory.core_context() == []


@pytest.mark.parametrize('role', sorted(PRIMARY_ROLES) + ['vision'])
async def test_all_primary_calls_receive_current_core_memory(role):
    from buwubot.config import Settings
    contents = ['称呼习惯']
    async def handler(request):
        messages = json.loads(request.content)['messages']
        assert ('称呼习惯' in messages[0]['content']) == (role != 'vision')
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelClient(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']), http=http, core_memory=lambda: contents)
        await client.call(role, [{'role': 'user', 'content': 'test'}])
