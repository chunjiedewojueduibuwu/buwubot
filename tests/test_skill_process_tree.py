import asyncio

import psutil
import pytest
from test_skill_plugins import package, setup


@pytest.mark.parametrize('cancel', [True, False])
async def test_skill_cancellation_and_success_cleanup_descendant_processes(tmp_path, cancel):
    store, coordinator, tools, manager = setup(tmp_path)
    script = '''import json,pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
pathlib.Path('child.pid').write_text(str(child.pid))
'''
    script += 'time.sleep(120)\n' if cancel else 'print(json.dumps({"state":"succeeded"}))\n'
    item = await coordinator.commit(lambda: manager.install(package(script)))
    await coordinator.commit(lambda: manager.set_enabled('sample',True,expected_revision=item['revision']))
    task = asyncio.create_task(tools.invoke('sample_read', {}))
    marker = tmp_path / 'skills/data/sample/child.pid'
    child = None
    try:
        async with asyncio.timeout(5):
            while not marker.is_file():
                await asyncio.sleep(.02)
        child_pid = int(marker.read_text())
        try:
            child = psutil.Process(child_pid)
        except psutil.NoSuchProcess:
            child = None  # A successful short-lived parent may already have closed its job.
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)['state'] == 'succeeded'
        if child:
            await asyncio.to_thread(child.wait, timeout=3)
        else:
            assert not psutil.pid_exists(child_pid)
    finally:
        if not task.done():
            task.cancel()
        if child and child.is_running():
            child.kill()
            await asyncio.to_thread(child.wait, timeout=3)
        store.close()
