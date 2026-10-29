import io
import json
import stat
import zipfile

import pytest

from buwubot.config import Settings
from buwubot.coordinator import Coordinator
from buwubot.skill_plugins import SkillPlugins
from buwubot.store import Conflict, Store
from buwubot.tools import ToolRegistry

SCRIPT = '''import json,sys,pathlib
request=json.load(sys.stdin)
pathlib.Path('invoked').write_text('yes')
print(json.dumps({'state':'succeeded','message':request['config']['prefix'],
    'data':{'request_id':request['arguments']['request_id'],
            'operation_id':request['arguments']['operation_id']}}))
'''


def package(script=SCRIPT, **changes):
    manifest = {'format_version':1,'id':'sample','name':'测试技能','version':'1.0',
        'description':'隔离插件包','entrypoint':'main.py',
        'tools':[{'name':'sample_read','description':'读取测试数据','input':{'type':'object'}}],
        'settings':[{'name':'prefix','label':'回复前缀','type':'string','default':'完成'},
                    {'name':'token','label':'凭证','type':'string','default':'','secret':True}]}
    manifest.update(changes)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('plugin.json', json.dumps(manifest,ensure_ascii=False))
        archive.writestr('main.py', script)
    return buf.getvalue()


def setup(tmp_path):
    store = Store(tmp_path / 'bot.db')
    coordinator = Coordinator(store,None,Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']))
    tools = ToolRegistry()
    manager = SkillPlugins(store,tools,tmp_path / 'skills')
    return store, coordinator, tools, manager


async def test_install_configure_reload_invoke_disable_and_remove(tmp_path):
    store, coordinator, tools, manager = setup(tmp_path)
    installed = await coordinator.commit(lambda:manager.install(package()))
    assert not installed['enabled'] and installed['revision'] == 1
    assert not list((tmp_path/'skills').rglob('invoked'))
    assert not tools.definitions()[-1]['enabled']
    with pytest.raises(Conflict):
        await coordinator.commit(lambda:manager.install(package()))
    configured = await coordinator.commit(lambda:manager.configure('sample',
        {'prefix':'你好','token':'do-not-return-this'},expected_revision=1))
    assert configured['secret_fields_set'] == ['token']
    assert 'do-not-return-this' not in json.dumps(manager.list())
    assert 'token' not in configured['config']
    with pytest.raises(Conflict):
        await coordinator.commit(lambda:manager.set_enabled('sample',True,expected_revision=1))
    enabled = await coordinator.commit(lambda:manager.set_enabled('sample',True,expected_revision=2))
    assert enabled['revision'] == 3
    store.close()


    store, coordinator, tools, manager = setup(tmp_path)
    assert tools.definitions()[-1]['enabled']
    result = await tools.invoke('sample_read',{},request_id='job-1',operation_id='op-1')
    assert result['message'] == '你好'
    assert result['data'] == {'request_id':'job-1','operation_id':'op-1'}
    await coordinator.commit(lambda:manager.set_enabled('sample',False,expected_revision=3))
    assert (await tools.invoke('sample_read',{}))['state'] == 'unavailable'
    await coordinator.commit(lambda:manager.uninstall('sample',expected_revision=4))
    assert manager.list() == []
    assert all(t['name'] != 'sample_read' for t in tools.definitions())
    assert not list((tmp_path/'skills'/'objects').glob('*/main.py'))


async def test_package_cannot_shadow_qq_channel_operation(tmp_path):
    store, coordinator, _, manager = setup(tmp_path)
    try:
        with pytest.raises(ValueError, match='QQ channel'):
            await coordinator.commit(lambda: manager.install(package(tools=[{
                'name': 'qq_operation', 'description': 'shadow', 'input': {'type': 'object'}}])))
        assert manager.list() == []
    finally:
        store.close()


async def test_legacy_reserved_tool_is_quarantined_but_stays_manageable(tmp_path):
    store, coordinator, _, manager = setup(tmp_path)
    try:
        await coordinator.commit(lambda: manager.install(package()))
        manifest = json.loads(store.rows('SELECT manifest FROM skill_plugins')[0]['manifest'])
        manifest['tools'][0]['name'] = 'qq_operation'
        def legacy_row():
            with store.transaction() as db:
                db.execute('UPDATE skill_plugins SET manifest=?,enabled=1', (json.dumps(manifest),))
        await coordinator.commit(legacy_row)
        tools = ToolRegistry()
        reloaded = SkillPlugins(store, tools, tmp_path / 'skills')
        listed = reloaded.list()
        assert listed[0]['error']
        assert 'qq_operation' not in tools.handlers
        await coordinator.commit(lambda: reloaded.install(package(id='other', tools=[{
            'name': 'other_read', 'description': 'other', 'input': {'type': 'object'}}])))
        await coordinator.commit(lambda: reloaded.uninstall('sample', expected_revision=1))
        assert len(reloaded.list()) == 1
    finally:
        store.close()
    store.close()


async def test_subprocess_host_library_matches_current_runtime_checkout(tmp_path):
    from pathlib import Path

    import buwubot.learning
    store, coordinator, tools, manager = setup(tmp_path)
    script = '''import buwubot.learning,json
print(json.dumps({'state':'succeeded','data':{'reader':buwubot.learning.__file__}}))
'''
    item = await coordinator.commit(lambda:manager.install(package(script)))
    await coordinator.commit(lambda:manager.set_enabled('sample',True,expected_revision=item['revision']))
    result = await tools.invoke('sample_read',{})
    assert Path(result['data']['reader']).resolve() == Path(buwubot.learning.__file__).resolve()
    store.close()


@pytest.mark.parametrize('path', ['../escape.py','C:/escape.py',r'..\escape.py','/escape.py'])
async def test_install_rejects_archive_paths_and_does_not_execute(tmp_path,path):
    store, coordinator, _tools, manager = setup(tmp_path)
    buf = io.BytesIO(package())
    with zipfile.ZipFile(buf,'a') as archive:
        archive.writestr(path,'raise SystemExit()')
    raw = buf.getvalue()
    if chr(92) in path:
        # Windows ZipInfo normalizes backslashes on write; forge actual ZIP names after writing.
        raw = raw.replace(path.replace(chr(92),'/').encode(),path.encode())
    with pytest.raises(ValueError):
        await coordinator.commit(lambda:manager.install(raw))
    assert manager.list() == [] and not (tmp_path/'escape.py').exists()
    store.close()


async def test_installer_rejects_links_bombs_and_tool_conflicts(tmp_path):
    store, coordinator, tools, manager = setup(tmp_path)
    buf = io.BytesIO(package())
    with zipfile.ZipFile(buf,'a') as archive:
        info = zipfile.ZipInfo('link')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info,'../../outside')
    with pytest.raises(ValueError):
        await coordinator.commit(lambda:manager.install(buf.getvalue()))
    buf = io.BytesIO(package())
    with zipfile.ZipFile(buf,'a',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('large.txt','x' * (17 * 1024 * 1024))
    with pytest.raises(ValueError):
        await coordinator.commit(lambda:manager.install(buf.getvalue()))
    await coordinator.commit(lambda:manager.install(package()))
    with pytest.raises(ValueError):
        await coordinator.commit(lambda:manager.install(package(id='another')))
    assert len(manager.list()) == 1 and len(tools.definitions()) == 3
    store.close()


async def test_bad_protocol_or_timeout_does_not_block_other_skills(tmp_path):
    store, coordinator, tools, manager = setup(tmp_path)
    await coordinator.commit(lambda:manager.install(package('print("not JSON")')))
    await coordinator.commit(lambda:manager.set_enabled('sample',True,expected_revision=1))
    with pytest.raises(ValueError):
        await tools.invoke('sample_read',{})
    await coordinator.commit(lambda:manager.uninstall('sample',expected_revision=2))
    await coordinator.commit(lambda:manager.install(package('import time; time.sleep(30)')))
    await coordinator.commit(lambda:manager.set_enabled('sample',True,expected_revision=1))
    manager.call_timeout = .1
    with pytest.raises(TimeoutError):
        await tools.invoke('sample_read',{})
    assert not manager.active_calls
    await coordinator.commit(lambda:manager.uninstall('sample',expected_revision=2))
    await coordinator.commit(lambda:manager.install(package()))
    await coordinator.commit(lambda:manager.set_enabled('sample',True,expected_revision=1))
    manager.call_timeout = 5
    assert (await tools.invoke('sample_read',{}))['state'] == 'succeeded'
    store.close()


async def test_formal_plugin_changes_require_coordinator_and_config_validation(tmp_path):
    store, coordinator, _tools, manager = setup(tmp_path)
    with pytest.raises(RuntimeError,match='coordinator'):
        manager.install(package())
    await coordinator.commit(lambda:manager.install(package()))
    for changes in ({'prefix':False},{'unknown':'value'}):
        with pytest.raises(ValueError):
            await coordinator.commit(lambda changes=changes:manager.configure('sample',changes,expected_revision=1))
    assert manager.list()[0]['revision'] == 1
    store.close()


async def test_enable_resumes_only_this_skills_unavailable_tasks(tmp_path):
    store, coordinator, _tools, manager = setup(tmp_path)
    await coordinator.commit(lambda:manager.install(package()))
    queue = coordinator.queue
    jobs = []
    for name,reason in [('sample_read','工具处理器未加载，请检查配置'),
                        ('another_tool','工具处理器未加载，请检查配置'),
                        ('sample_read','结果或输入格式需检查')]:
        jid = queue.enqueue('tool',{'tool_name':name},dedupe_key=str(len(jobs)),priority=1,source_time=1)
        queue.defer(jid,reason,now=1,needs_attention=True)
        jobs.append(jid)
    await coordinator.commit(lambda:manager.set_enabled('sample',True,expected_revision=1))
    assert queue.get(jobs[0])['state'] == 'pending'
    assert queue.get(jobs[0])['next_attempt'] == 0
    assert all(queue.get(jid)['state'] == 'needs_attention' for jid in jobs[1:])
    store.close()


async def test_uninstall_reinstall_while_call_runs_preserves_reinstalled_files(tmp_path):
    import asyncio

    script = SCRIPT.replace("request=json.load(sys.stdin)",
        "request=json.load(sys.stdin)\nimport time\npathlib.Path('started').write_text('yes')\ntime.sleep(.3)")
    raw = package(script)
    store, coordinator, tools, manager = setup(tmp_path)
    await coordinator.commit(lambda:manager.install(raw))
    await coordinator.commit(lambda:manager.set_enabled('sample',True,expected_revision=1))
    task = asyncio.create_task(tools.invoke('sample_read',{}))
    marker = tmp_path/'skills'/'data'/'sample'/'started'
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(.01)
    await coordinator.commit(lambda:manager.uninstall('sample',expected_revision=2))
    assert (await tools.invoke('sample_read',{}))['state'] == 'unavailable'
    await coordinator.commit(lambda:manager.install(raw))
    assert (await task)['state'] == 'succeeded'
    assert list((tmp_path/'skills'/'objects').glob('*/main.py'))
    assert not manager.active_calls
    store.close()


async def test_oversized_stdout_is_bounded_and_missing_package_is_reported_on_reload(tmp_path):
    store, coordinator, tools, manager = setup(tmp_path)
    await coordinator.commit(lambda:manager.install(package("print('x' * 150000)")))
    await coordinator.commit(lambda:manager.set_enabled('sample',True,expected_revision=1))
    with pytest.raises(ValueError,match='output'):
        await tools.invoke('sample_read',{})
    assert not manager.active_calls
    entry = next((tmp_path/'skills'/'objects').glob('*/main.py'))
    entry.unlink()
    tools = ToolRegistry()
    reloaded = SkillPlugins(store,tools,tmp_path/'skills')
    assert reloaded.list()[0]['error']
    assert not any(t['name'] == 'sample_read' for t in tools.definitions())
    with pytest.raises(ValueError,match='conflict'):
        await coordinator.commit(lambda:reloaded.install(package(id='another')))
    store.close()


def test_local_management_installs_disabled_package_and_keeps_qq_persona_unchanged(tmp_path):
    from fastapi.testclient import TestClient

    from buwubot.app import create_app

    app = create_app(tmp_path,run_bot=False)
    base = 'http://127.0.0.1:8765'
    with TestClient(app,base_url=base) as client:
        before = client.get('/api/config').json()
        response = client.post('/api/skill-plugins/install',content=package(),
            headers={'Content-Type':'application/zip','Origin':base})
        assert response.status_code == 200
        assert not response.json()['enabled']
        assert client.get('/api/config').json() == before
        response = client.patch('/api/skill-plugins/sample/settings',
            json={'revision':1,'changes':{'token':'secret-value'}})
        assert response.status_code == 200 and 'secret-value' not in response.text
        assert client.patch('/api/skill-plugins/sample/enabled',
            json={'revision':1,'enabled':True}).status_code == 409
        assert client.patch('/api/skill-plugins/sample/enabled',
            json={'revision':2,'enabled':True}).status_code == 200
        assert client.get('/api/plugins').json()['items'][0]['kind'] == 'activity'
        assert client.get('/api/skill-plugins').json()['items'][0]['id'] == 'sample'
        assert client.post('/api/skill-plugins/install',content=package(id='bad'),
            headers={'Content-Type':'application/zip','Origin':'https://bad.invalid'}).status_code == 403
    with TestClient(create_app(tmp_path,run_bot=False),base_url=base) as client:
        assert client.get('/api/skill-plugins').json()['items'][0]['enabled']
        assert client.request('DELETE','/api/skill-plugins/sample?revision=2',json={}).status_code == 409
        assert client.request('DELETE','/api/skill-plugins/sample?revision=3',json={}).status_code == 200
        assert client.get('/api/skill-plugins').json()['items'] == []
