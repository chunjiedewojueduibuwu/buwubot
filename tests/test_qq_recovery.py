import json

import httpx
import pytest

from buwubot.config import Settings
from buwubot.qq_recovery import RecoveryError, SnowLumaRecovery


def configured(tmp_path):
    folder = tmp_path / 'installed'
    (folder / 'config').mkdir(parents=True)
    for name in ('node.exe', 'index.mjs'):
        (folder / name).write_text('fixture')
    for name, data in {
        'runtime.json':{'hookAutoLoad':False,'webuiHost':'127.0.0.1','webuiPort':5099},
        'consent.json':{'version':'fixture','acceptedAt':'accepted'},
        'webui.json':{'mustChangePassword':False},
    }.items():
        (folder / 'config' / name).write_text(json.dumps(data))
    return Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, persona_name='助手', aliases=['助手'], qq_provider='snowluma',self_id='11',owner_id='22',
                    snowluma_auto_connect=True,snowluma_directory=str(folder),
                    snowluma_password='private-password')


@pytest.mark.parametrize("session_uin", ["", "0", 0])
async def test_recovery_only_loads_confirmed_robot_and_uses_ephemeral_token(tmp_path, monkeypatch, session_uin):
    cfg = configured(tmp_path)
    calls = []
    async def handle(request):
        calls.append(request)
        assert request.url.host == '127.0.0.1'
        if request.url.path == '/api/login':
            assert json.loads(request.content) == {'password':'private-password'}
            return httpx.Response(200,json={'success':True,'token':'ephemeral'})
        assert request.headers['x-snowluma-token'] == 'ephemeral'
        if request.method == 'GET':
            return httpx.Response(200,json={'list':[
                {'pid':10,'account':{'uin':'22'},'injected':False},
                {'pid':20,'account':None,'injected':False},
                {'pid':30,'account':{'uin':'11'},'uin':session_uin,'injected':False}]})
        assert request.url.path == '/api/processes/30/load'
        return httpx.Response(200,json={'success':True,'process':{'pid':30,'uin':'11'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        recovery = SnowLumaRecovery(tmp_path,http=client)
        monkeypatch.setattr(recovery,'_listener_matches',lambda *args:True)
        monkeypatch.setattr(recovery,'_qq_identity',lambda pid:(pid,1))
        await recovery.ensure(cfg, allowed=lambda:True)
    assert len([r for r in calls if r.method == 'GET']) == 2
    assert len([r for r in calls if r.url.path.endswith('/load')]) == 1


@pytest.mark.parametrize('session_uin', ['', '0'])
@pytest.mark.parametrize('mode', ['unknown','owner','duplicate','changed','injected','contradiction'])
async def test_recovery_does_not_load_unconfirmed_or_already_injected_process(tmp_path, monkeypatch, mode, session_uin):
    cfg = configured(tmp_path)
    reads = 0
    async def handle(request):
        nonlocal reads
        if request.url.path == '/api/login':
            return httpx.Response(200,json={'success':True,'token':'temporary'})
        assert request.method == 'GET', 'must not load any process'
        reads += 1
        rows = [{'pid':30,'account':{'uin':'11'},'uin':session_uin,'injected':False}]
        if mode in {'owner','unknown'} or (mode == 'changed' and reads == 2):
            rows[0]['account'] = {'uin':'22'} if mode != 'unknown' else None
        elif mode == 'duplicate':
            rows += [dict(rows[0],pid=31)]
        elif mode == 'injected':
            rows[0]['injected'] = True
        elif mode == 'contradiction':
            rows[0]['uin'] = '22'
        return httpx.Response(200,json={'list':rows})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        recovery = SnowLumaRecovery(tmp_path,http=client)
        monkeypatch.setattr(recovery,'_listener_matches',lambda *args:True)
        monkeypatch.setattr(recovery,'_qq_identity',lambda pid:(pid,1))
        if mode == 'injected':
            await recovery.ensure(cfg,allowed=lambda:True)
        else:
            with pytest.raises(RecoveryError):
                await recovery.ensure(cfg,allowed=lambda:True)


@pytest.mark.parametrize('patch', [
    {'hookAutoLoad':True}, {'webuiHost':'0.0.0.0'}, {'webuiPort':True},
    {'webuiPort':0}, {'webuiTls':{'enabled':True}},
])
async def test_invalid_bridge_setup_never_receives_password_or_launches(tmp_path, patch):
    cfg = configured(tmp_path)
    config = tmp_path / 'installed/config/runtime.json'
    data = json.loads(config.read_text()) | patch
    config.write_text(json.dumps(data))
    async def handle(request):
        pytest.fail('invalid installation must not receive requests')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RecoveryError) as error:
            await SnowLumaRecovery(tmp_path,http=client).ensure(cfg,allowed=lambda:True)
        assert error.value.needs_attention


@pytest.mark.parametrize('response', [
    httpx.Response(401,json={'message':'private-password'}),
    httpx.Response(200,json={'success':False,'needsTotp':True}),
    httpx.Response(200,json={'success':True,'token':'temporary','mustChangePassword':True}),
    httpx.Response(302,headers={'location':'https://example.com/leak'}),
])
async def test_login_failure_is_sanitized_and_never_follows_redirect(tmp_path, monkeypatch, response):
    cfg = configured(tmp_path)
    calls = 0
    async def handle(request):
        nonlocal calls
        calls += 1
        assert request.url.path == '/api/login'
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        recovery = SnowLumaRecovery(tmp_path,http=client)
        monkeypatch.setattr(recovery,'_listener_matches',lambda *args:True)
        with pytest.raises(RecoveryError) as error:
            await recovery.ensure(cfg,allowed=lambda:True)
        assert 'private-password' not in str(error.value)
        assert error.value.needs_attention
        assert calls == 1


async def test_disabled_or_paused_recovery_has_no_side_effect_and_secret_is_not_public(tmp_path):
    cfg = configured(tmp_path)
    async def handle(request):
        pytest.fail('recovery should be inactive')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        recovery = SnowLumaRecovery(tmp_path,http=client)
        await recovery.ensure(cfg.model_copy(update={'snowluma_auto_connect':False}),allowed=lambda:True)
        await recovery.ensure(cfg,allowed=lambda:False)
    assert 'snowluma_password' not in cfg.public()
    assert cfg.public()['snowluma_password_set']
    assert cfg.storage()['snowluma_password'] == 'private-password'


async def test_foreign_listener_never_receives_webui_password(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    async def handle(request):
        pytest.fail('foreign listener must not receive any credentials')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        recovery = SnowLumaRecovery(tmp_path,http=client)
        monkeypatch.setattr(recovery,'_listener_matches',lambda *args:False)
        monkeypatch.setattr(recovery,'_start',lambda *args:None)
        with pytest.raises(RecoveryError):
            await recovery.ensure(cfg,allowed=lambda:True)


async def test_pause_during_login_stops_before_account_requests(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    allowed = True
    async def handle(request):
        nonlocal allowed
        assert request.url.path == '/api/login'
        allowed = False
        return httpx.Response(200,json={'success':True,'token':'temporary'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        recovery = SnowLumaRecovery(tmp_path,http=client)
        monkeypatch.setattr(recovery,'_listener_matches',lambda *args:True)
        await recovery.ensure(cfg,allowed=lambda:allowed)


async def test_recycled_qq_pid_is_not_loaded(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    async def handle(request):
        if request.url.path == '/api/login':
            return httpx.Response(200,json={'success':True,'token':'temporary'})
        assert request.method == 'GET'
        return httpx.Response(200,json={'list':[{'pid':30,'account':{'uin':'11'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        recovery = SnowLumaRecovery(tmp_path,http=client)
        identities = iter([(30,1),(30,2)])
        monkeypatch.setattr(recovery,'_qq_identity',lambda pid:next(identities))
        monkeypatch.setattr(recovery,'_listener_matches',lambda *args:True)
        with pytest.raises(RecoveryError):
            await recovery.ensure(cfg,allowed=lambda:True)


def test_qq_settings_own_recovery_and_never_return_password(tmp_path):
    from fastapi.testclient import TestClient

    from buwubot.app import create_app

    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        before = client.get('/api/config').json()
        plugin = next(p for p in client.get('/api/plugins').json()['items'] if p['id'] == 'qq')
        fields = {f['key'] for s in plugin['settings_sections'] for f in s['fields']}
        assert {'snowluma_auto_connect','snowluma_directory','snowluma_password'} <= fields
        changed = client.patch('/api/plugins/qq/settings',json={'revision':before['revision'],
            'changes':{'snowluma_password':'not-public','snowluma_directory':'D:/installed'}})
        assert changed.status_code == 200
        assert 'not-public' not in changed.text
        assert changed.json()['settings']['snowluma_password_set']
        assert changed.json()['settings']['persona'] == before['settings']['persona']
    with TestClient(create_app(tmp_path,run_bot=False),base_url='http://127.0.0.1:8765') as client:
        assert client.get('/api/config').json()['settings']['snowluma_password_set']


def test_hidden_start_of_existing_installation_pins_environment_and_reuses_process(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    import buwubot.qq_recovery as module

    if os.name != 'nt':
        pytest.skip('Windows start flags')
    cfg = configured(tmp_path)
    folder, port = SnowLumaRecovery._installation(cfg)
    monkeypatch.setattr(module.psutil,'net_connections',lambda **kwargs:[])
    monkeypatch.setattr(module.psutil,'process_iter',list)
    monkeypatch.setenv('SNOWLUMA_HOOK_AUTOLOAD','true')
    monkeypatch.setenv('NODE_OPTIONS','--require untrusted.js')
    calls = []
    monkeypatch.setattr(module.subprocess,'Popen',lambda args,**kwargs:calls.append((args,kwargs)))
    recovery = SnowLumaRecovery(tmp_path)
    recovery._start(folder,port)
    args, options = calls[0]
    assert [Path(p) for p in args] == [folder/'node.exe',folder/'index.mjs']
    assert options['creationflags'] == module.subprocess.CREATE_NO_WINDOW
    assert options['cwd'] == folder and 'shell' not in options
    assert options['env']['SNOWLUMA_HOOK_AUTOLOAD'] == 'false'
    assert 'NODE_OPTIONS' not in options['env']
    monkeypatch.setattr(module.psutil,'process_iter',lambda:[object()])
    monkeypatch.setattr(recovery,'_process_matches',lambda *args:True)
    recovery._start(folder,port)
    assert len(calls) == 1


@pytest.mark.parametrize('arguments,autoload,expected', [
    (['node','./index.mjs'],'false',True),
    (['node','./index.mjs'],'true',False),
    (['node','other.mjs','./index.mjs'],'false',False),
])
def test_listener_identity_rejects_global_autoload_and_unrelated_node_script(tmp_path,arguments,autoload,expected):
    from types import SimpleNamespace
    process = SimpleNamespace(cwd=lambda:str(tmp_path),exe=lambda:str(tmp_path/'node.exe'),
        cmdline=lambda:arguments,environ=lambda:{'SNOWLUMA_HOOK_AUTOLOAD':autoload})
    assert SnowLumaRecovery._process_matches(process,tmp_path) is expected


@pytest.mark.parametrize('foreign,environment', [
    (True,{}), (False,{'NODE_OPTIONS':'--require other.js'}), (False,{'NODE_PATH':'other'}),
])
def test_existing_listener_cannot_substitute_node_binary_or_preload_code(tmp_path,foreign,environment):
    from types import SimpleNamespace
    exe = tmp_path / ('other/node.exe' if foreign else 'node.exe')
    process = SimpleNamespace(cwd=lambda:str(tmp_path),exe=lambda:str(exe),
        cmdline=lambda:['node','index.mjs'],environ=lambda:environment)
    assert not SnowLumaRecovery._process_matches(process,tmp_path)


@pytest.mark.parametrize('error,night,expected', [('offline',False,1),('HTTP 401',False,0),('offline',True,0)])
async def test_reader_recovery_uses_existing_retry_and_respects_routine_and_authentication(
        tmp_path,monkeypatch,error,night,expected):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from test_engine import build

    from buwubot.channels import ChannelError

    now = datetime(2026,10,24,23 if night else 10,tzinfo=ZoneInfo('Asia/Shanghai'))
    store, engine, qq, _ = build(tmp_path,lambda:now)
    engine.settings = engine.settings.model_copy(update={'snowluma_auto_connect':True,'routine_enabled':True})
    attempts = []
    async def login():
        raise ChannelError(error)
    async def recover(*args,**kwargs):
        attempts.append(1)
        raise RecoveryError('waiting for configured account')
    qq.login_info = login
    monkeypatch.setattr(engine._qq_recovery,'ensure',recover)
    try:
        await engine._receive()
        assert len(attempts) == expected
        if not night:
            assert engine._qq_attention is ('401' in error)
            assert engine._qq_retry_at > 0
    finally:
        store.close()


@pytest.mark.parametrize('scenario', ['owned','unowned','expired','real_conflict','changed_account',
    'duplicate','reused_pid','changed_directory','pause_second_snapshot','changed_listener',
    'expired_second_snapshot','changed_second_account','reused_second_pid','session_recovers',
    'ambiguous_unload','ambiguous_load'])
async def test_owned_invalid_login_gets_only_one_bounded_module_retry(tmp_path, monkeypatch, scenario):
    cfg = configured(tmp_path)
    actions, reads = [], [0]
    now, permitted, identity, listener = [10], [True], [(30,1)], [True]
    row = {'pid':30,'account':{'uin':'11'},'uin':'0','injected':False,
           'connected':False,'loggedIn':False}
    extras = []
    async def handle(request):
        if request.url.path == '/api/login':
            return httpx.Response(200,json={'success':True,'token':'temporary'})
        if request.method == 'GET':
            reads[0] += 1
            if scenario == 'pause_second_snapshot' and reads[0] == 4:
                permitted[0] = False
            if scenario == 'changed_listener' and reads[0] == 4:
                listener[0] = False
            if scenario == 'expired_second_snapshot' and reads[0] == 4:
                now[0] += 91
            if scenario == 'changed_second_account' and reads[0] == 4:
                row['account'] = {'uin':'22'}
            if scenario == 'reused_second_pid' and reads[0] == 4:
                identity[0] = (30,2)
            if scenario == 'session_recovers' and reads[0] == 4:
                row.update(uin='11',loggedIn=True)
            return httpx.Response(200,json={'list':[dict(row),*extras]})
        action = request.url.path.rsplit('/',1)[-1]
        actions.append(action)
        assert request.url.path == '/api/processes/30/' + action
        row.update(uin='1982149502528' if action == 'load' else '0',
                   injected=action=='load',connected=action=='load',loggedIn=False)
        return httpx.Response(500 if scenario == 'ambiguous_' + action else 200,json={'success':True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        recovery = SnowLumaRecovery(tmp_path,http=client)
        monkeypatch.setattr(recovery,'clock',lambda:now[0],raising=False)
        monkeypatch.setattr(recovery,'_listener_matches',lambda *args:listener[0])
        monkeypatch.setattr(recovery,'_qq_identity',lambda pid:identity[0])
        if scenario == 'ambiguous_load':
            with pytest.raises(RecoveryError):
                await recovery.ensure(cfg,allowed=lambda:True)
        else:
            await recovery.ensure(cfg,allowed=lambda:permitted[0])
        assert actions == ['load']
        if scenario == 'unowned':
            recovery = SnowLumaRecovery(tmp_path,http=client)
            monkeypatch.setattr(recovery,'_listener_matches',lambda *args:True)
            monkeypatch.setattr(recovery,'_qq_identity',lambda pid:identity[0])
        elif scenario == 'expired':
            now[0] += 91
        elif scenario == 'real_conflict':
            row['uin'] = '22'
        elif scenario == 'changed_account':
            row['account'] = {'uin':'22'}
        elif scenario == 'duplicate':
            extras.append(dict(row,pid=31))
        elif scenario == 'reused_pid':
            identity[0] = (30,2)
        elif scenario == 'changed_directory':
            other = configured(tmp_path/'other')
            cfg = cfg.model_copy(update={'snowluma_directory':other.snowluma_directory})
        try:
            await recovery.ensure(cfg,allowed=lambda:permitted[0])
        except RecoveryError:
            pass
        if scenario not in {'owned','ambiguous_unload'}:
            assert actions == ['load'], scenario
            return
        assert actions == ['load','unload']
        await recovery.ensure(cfg,allowed=lambda:True)
        assert actions == ['load','unload','load']
        for _ in range(2):
            try:
                await recovery.ensure(cfg,allowed=lambda:True)
            except RecoveryError:
                pass
        assert actions == ['load','unload','load'], 'never loop unload/load'


def test_duplicate_account_cannot_hide_behind_conflicting_session():
    rows = [{'pid':30,'account':{'uin':'11'},'uin':'11'},
            {'pid':31,'account':{'uin':'11'},'uin':'1982149502528'}]
    with pytest.raises(RecoveryError):
        SnowLumaRecovery._select({'list':rows},'11')
