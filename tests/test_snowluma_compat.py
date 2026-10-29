import json

import httpx
import pytest

from buwubot.config import Settings
from buwubot.qq import QQAdapter


@pytest.mark.parametrize('scope,action,target', [
    ('123', 'get_group_msg_history', {'group_id': '123'}),
    ('private:10001', 'get_friend_msg_history', {'user_id': '10001'}),
])
async def test_snowluma_history_uses_signed_message_id_and_exposes_native_cursor(scope, action, target):
    seen = []
    async def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'status': 'ok', 'retcode': 0, 'data': {'messages': [
            {'message_id': -30, 'message_seq': 80, 'time': 2},
            {'message_id': -31, 'message_seq': 79, 'time': 1},
        ]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        qq = QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], qq_provider='snowluma'), client=http)
        rows = await qq.history(scope, count=2, before='-32')
    assert seen == [('/' + action, target | {'count': 2, 'reverse_order': True, 'message_id': -32})]
    assert [row['history_cursor'] for row in rows] == ['-31', '-30']


async def test_default_napcat_history_keeps_sequence_contract():
    async def handler(request):
        assert json.loads(request.content) == {'group_id': '123', 'count': 100,
                                               'reverseOrder': True, 'message_seq': '79'}
        return httpx.Response(200, json={'data': {'messages': []}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']).qq_provider == 'napcat'
        assert await QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']), client=http).history('123', before='79') == []


async def test_snowluma_quote_reply_preserves_signed_native_ids():
    async def handler(request):
        assert request.url.path == '/send_private_msg'
        assert json.loads(request.content) == {'user_id': '10001', 'message': [
            {'type': 'reply', 'data': {'id': '-31'}}, {'type': 'text', 'data': {'text': '收到'}}]}
        return httpx.Response(200, json={'status': 'ok', 'retcode': 0, 'data': {'message_id': -123}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], qq_provider='snowluma'), client=http).send(
            'private:10001', [{'type': 'text', 'data': {'text': '收到'}}], '-31')
    assert result == '-123'


async def test_snowluma_image_info_keeps_original_gif_and_does_not_send_token_to_cdn():
    gif = b'GIF89a' + bytes(range(20))
    async def handler(request):
        if request.url.path == '/get_image':
            assert json.loads(request.content) == {'file': 'native.gif'}
            return httpx.Response(200, json={'retcode': 0, 'data': {
                'file': 'https://qpic.cn/original.gif', 'url': 'https://qpic.cn/original.gif',
                'file_size': str(len(gif)), 'file_name': 'native.gif'}})
        assert str(request.url) == 'https://qpic.cn/original.gif'
        assert 'authorization' not in request.headers
        return httpx.Response(200, content=gif, headers={'Content-Type': 'image/gif'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        qq = QQAdapter(Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], qq_provider='snowluma', qq_token='local-only'), client=http)
        assert await qq.fetch_image({'type': 'image', 'data': {'file': 'native.gif'}}, max_bytes=100) == gif


def test_provider_choice_is_persistent_and_owned_by_qq_plugin(tmp_path):
    from fastapi.testclient import TestClient

    from buwubot.app import create_app
    with TestClient(create_app(tmp_path, run_bot=False), base_url='http://127.0.0.1:8765') as client:
        before = client.get('/api/config').json()
        plugins = client.get('/api/plugins').json()['items']
        qq = next(p for p in plugins if p['id'] == 'qq')
        fields = [f for section in qq['settings_sections'] for f in section['fields']]
        assert next(f for f in fields if f['key'] == 'qq_provider')['options'] == [
            ['napcat', 'NapCat'], ['snowluma', 'SnowLuma']]
        result = client.patch('/api/plugins/qq/settings', json={'revision': before['revision'],
            'changes': {'qq_provider': 'snowluma'}})
        assert result.status_code == 200
        assert result.json()['settings']['persona'] == before['settings']['persona']
        assert client.patch('/api/plugins/qq/settings', json={'revision': result.json()['revision'],
            'changes': {'qq_provider': 'unknown'}}).status_code == 422
    with TestClient(create_app(tmp_path, run_bot=False), base_url='http://127.0.0.1:8765') as client:
        assert client.get('/api/config').json()['settings']['qq_provider'] == 'snowluma'


def test_startup_reads_provider_without_creating_or_modifying_settings(tmp_path):
    import hashlib

    from buwubot.runtime import configured_qq_provider
    from buwubot.store import Store
    missing = tmp_path / 'missing'
    assert configured_qq_provider(missing) == 'napcat'
    assert not missing.exists()
    store = Store(tmp_path / 'bot.db')
    cfg, revision = store.settings()
    cfg.qq_provider = 'snowluma'
    store.save_settings(cfg, revision)
    store.close()
    before = hashlib.sha256((tmp_path / 'bot.db').read_bytes()).digest()
    assert configured_qq_provider(tmp_path) == 'snowluma'
    assert hashlib.sha256((tmp_path / 'bot.db').read_bytes()).digest() == before


def test_windows_dependency_launcher_does_not_start_napcat_for_snowluma(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from buwubot.store import Store
    if os.name != 'nt':
        pytest.skip('Windows dependency launcher')
    store = Store(tmp_path / 'bot.db')
    cfg, revision = store.settings()
    cfg.qq_provider = 'snowluma'
    store.save_settings(cfg, revision)
    store.close()
    script = Path(__file__).parents[1] / 'scripts' / 'Manage-BuwuBot.ps1'
    command = r'''
param($scriptFile, $pythonPath, $testData)
$ErrorActionPreference='Stop'
$ast=[System.Management.Automation.Language.Parser]::ParseFile($scriptFile,[ref]$null,[ref]$null)
$fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Start-Dependencies'},$true)
Invoke-Expression $fn.Extent.Text
$python=$pythonPath; $DataDir=$testData; $root=Split-Path (Split-Path $scriptFile)
function Test-Path {param($LiteralPath) return $LiteralPath -like '*Start-NapCat.ps1'}
function New-Object {
  $fake=[pscustomobject]@{Connected=$false}
  $fake | Add-Member ScriptMethod ConnectAsync {return [System.Threading.Tasks.Task]::Delay(1)}
  $fake | Add-Member ScriptMethod Dispose {}
  return $fake
}
function Start-Process {throw 'Unexpected NapCat process launch'}
Start-Dependencies
Write-Output 'selected provider respected'
'''
    check = tmp_path / 'check.ps1'
    check.write_text(command, encoding='utf-8')
    env = os.environ | {'PYTHONPATH': str(Path(__file__).parents[1] / 'src')}
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
        'Bypass', '-File', str(check), str(script), sys.executable, str(tmp_path)],
        capture_output=True, timeout=15, env=env, check=False)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert b'selected provider respected' in result.stdout


async def test_snowluma_same_second_pages_preserve_native_cursor_and_latest_context(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    from buwubot.conversations import ConversationRef
    now=datetime(2026,10,21,9,tzinfo=ZoneInfo("Asia/Shanghai"))
    store,engine,_,_=build(tmp_path,lambda:now)
    engine.settings=engine.settings.model_copy(update={"qq_provider":"snowluma"})
    cursors=[]
    async def handler(request):
        body=json.loads(request.content)
        before=-int(body.get("message_id",-205))
        assert body["reverse_order"] is True and "message_seq" not in body
        cursors.append(body.get("message_id"))
        records=[{"message_seq":str(seq+1000),"message_id":-seq,"time":now.timestamp()-5,
            "user_id":22,"sender":{"nickname":"验收群友"},
            "message":[{"type":"text","data":{"text":str(seq)}}]}
            for seq in range(min(before,205),max(0,before-100),-1)]
        return httpx.Response(200,json={"data":{"messages":records}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine.qq=QQAdapter(engine.settings,client=client)
        await engine._backfill_conversation(ConversationRef("group","123"))
    assert len(store.rows("SELECT id FROM messages"))==205
    assert cursors==[None,-106,-7]
    assert [m.text for m in engine.memory.archive.recent("123",limit=10)]==[str(i) for i in range(196,206)]
    store.close()
