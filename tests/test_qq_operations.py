import asyncio
from io import BytesIO

import pytest
from PIL import Image

from buwubot.config import Settings
from buwubot.coordinator import Coordinator, WorkPaused
from buwubot.qq import QQError
from buwubot.qq_operations import QQOperations
from buwubot.stickers import MediaStore
from buwubot.store import Store


def build(tmp_path):
    store = Store(tmp_path / 'bot.db')
    cfg = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, persona_name='助手', aliases=['助手'], self_id='11', owner_id='22', group_id='123')
    coordinator = Coordinator(store, None, cfg)
    media = MediaStore(store, tmp_path / 'media')
    stream = BytesIO()
    Image.new('RGB', (8, 8), 'blue').save(stream, format='PNG')
    asset = media.save(stream.getvalue())['hash']
    class QQ:
        connected = True
        identity = '11'
        fail = False
        def __init__(self):
            self.calls = []
        async def call(self, action, payload=None):
            assert coordinator.lock.locked()
            self.calls.append((action, payload))
            if action == 'get_login_info':
                return {'user_id': self.identity, 'nickname': '助手', 'token': 'not-for-model'}
            if self.fail:
                raise QQError('interrupted')
            assert action == 'set_qq_avatar'
            return {}
    qq = QQ()
    allowed = [True]
    tool = QQOperations(store, coordinator, media, lambda: qq, lambda: cfg,
                        allowed=lambda: allowed[0])
    return store, coordinator, media, qq, allowed, tool, asset


async def test_own_profile_update_is_serialized_durable_and_filtered(tmp_path):
    store, coordinator, media, qq, allowed, tool, asset = build(tmp_path)
    args = {'operation': 'update_self', 'fields': {'avatar': asset}, 'request_id': 'one'}
    result = await tool(args)
    assert result['state'] == 'succeeded'
    assert qq.calls == [('get_login_info', None),
                        ('set_qq_avatar', {'file': media.path(asset).as_uri()})]
    rebuilt = QQOperations(store, coordinator, media, lambda: qq, lambda: coordinator.settings,
                           allowed=lambda: allowed[0])
    assert await rebuilt(args) == result
    assert len(qq.calls) == 2
    profile = await tool({'operation': 'get_self', 'request_id': 'read'})
    assert profile['data'] == {'user_id': '11', 'nickname': '助手'}
    store.close()


async def test_wrong_account_pause_and_unregistered_image_never_write(tmp_path):
    store, _, _, qq, allowed, tool, asset = build(tmp_path)
    args = {'operation': 'update_self', 'fields': {'avatar': asset}, 'request_id': 'one'}
    qq.identity = '22'
    with pytest.raises(QQError):
        await tool(args)
    assert all(action != 'set_qq_avatar' for action, _ in qq.calls)
    qq.identity = '11'
    allowed[0] = False
    with pytest.raises(WorkPaused):
        await tool(args)
    allowed[0] = True
    with pytest.raises((ValueError, FileNotFoundError)):
        await tool(args | {'fields': {'avatar': 'f' * 64}})
    for fields in [{'avatar': 'https://example.com/a.png'}, {'file': 'C:/secret'}, {'avatar': asset, 'user_id': '22'}]:
        with pytest.raises(ValueError):
            await tool(args | {'fields': fields})
    assert all(action != 'set_qq_avatar' for action, _ in qq.calls)
    store.close()


async def test_uncertain_retry_same_assignment_and_newer_update_blocks_old_replay(tmp_path):
    store, _, _, qq, _, tool, asset = build(tmp_path)
    args = {'operation': 'update_self', 'fields': {'avatar': asset}, 'request_id': 'one'}
    qq.fail = True
    with pytest.raises(QQError):
        await tool(args)
    qq.fail = False
    assert (await tool(args))['state'] == 'succeeded'
    qq.fail = True
    pending = args | {'request_id': 'two'}
    with pytest.raises(QQError):
        await tool(pending)
    qq.fail = False
    assert (await tool(args | {'request_id': 'three'}))['state'] == 'succeeded'
    before = len(qq.calls)
    assert (await tool(pending))['state'] == 'failed'
    assert len(qq.calls) == before
    with pytest.raises(ValueError):
        await tool(args | {'fields': {'avatar': 'f' * 64}})
    store.close()


async def test_pause_during_identity_probe_prevents_mutation(tmp_path):
    store, _, _, qq, allowed, tool, asset = build(tmp_path)
    original = qq.call
    async def call(action, payload=None):
        result = await original(action, payload)
        allowed[0] = False
        return result
    qq.call = call
    with pytest.raises(WorkPaused):
        await tool({'operation': 'update_self', 'fields': {'avatar': asset}, 'request_id': 'one'})
    assert [a for a, _ in qq.calls] == ['get_login_info']
    store.close()


async def test_cancellation_retains_intent_and_retries_without_background_task(tmp_path):
    store, _, _, qq, _, tool, asset = build(tmp_path)
    started = asyncio.Event()
    original = qq.call
    async def call(action, payload=None):
        if action == 'set_qq_avatar':
            started.set()
            await asyncio.Event().wait()
        return await original(action, payload)
    qq.call = call
    args = {'operation': 'update_self', 'fields': {'avatar': asset}, 'request_id': 'one'}
    task = asyncio.create_task(tool(args))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    qq.call = original
    assert (await tool(args))['state'] == 'succeeded'
    store.close()


async def test_intent_and_latest_marker_commit_atomically(tmp_path, monkeypatch):
    store, _, _, qq, _, tool, asset = build(tmp_path)
    original = store.set_state
    def crash(key, value):
        if key.startswith('qq_self_latest:'):
            raise OSError('simulated storage interruption')
        original(key, value)
    with monkeypatch.context() as patch:
        patch.setattr(store, 'set_state', crash)
        with pytest.raises(OSError):
            await tool({'operation': 'update_self', 'fields': {'avatar': asset}, 'request_id': 'one'})
    assert not store.rows("SELECT key FROM state WHERE key LIKE 'qq_self_%'")
    assert all(a != 'set_qq_avatar' for a, _ in qq.calls)
    assert (await tool({'operation': 'update_self', 'fields': {'avatar': asset},
                        'request_id': 'one'}))['state'] == 'succeeded'
    store.close()
