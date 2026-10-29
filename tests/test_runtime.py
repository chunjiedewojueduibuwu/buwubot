"""Real subprocess lifecycle checks; never target the user's running bot."""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from buwubot.runtime import inspect_instance, stop_instance


def test_stale_pid_identity_cannot_stop_an_unrelated_process(tmp_path):
    import psutil

    current = psutil.Process()
    (tmp_path / "bot.instance.json").write_text(json.dumps({"pid":current.pid,
        "created_at":current.create_time(),"executable":sys.executable,"port":8765}),encoding="utf-8")
    with pytest.raises(RuntimeError,match="identity"):
        stop_instance(tmp_path,force=True)
    assert current.is_running()


def test_stop_refuses_to_send_shutdown_to_a_reused_port(tmp_path,monkeypatch):
    from types import SimpleNamespace

    from buwubot import runtime

    seen=[]
    def handler(request):
        seen.append(request.method)
        return httpx.Response(200,json={"instance_pid":999})

    original=httpx.Client
    monkeypatch.setattr(runtime,"_managed_process",lambda directory:(SimpleNamespace(pid=111),{"port":8765}))
    monkeypatch.setattr(httpx,"Client",lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    with pytest.raises(RuntimeError,match="different process"):
        stop_instance(tmp_path,force=True)
    assert seen==["GET"]


def test_real_hidden_capable_service_start_graceful_stop_and_restart(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port = sock.getsockname()[1]
    command = [sys.executable,"-m","buwubot.app","--data-dir",str(tmp_path),"--port",str(port)]
    with httpx.Client(trust_env=False,timeout=1) as http:
        for _ in range(2):
            process = subprocess.Popen(command,cwd=Path(__file__).parents[1],
                                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic()+12
                while time.monotonic()<deadline:
                    if process.poll() is not None:
                        pytest.fail("service exited before it became ready")
                    try:
                        response = http.get(f"http://127.0.0.1:{port}/api/status")
                        if response.status_code==200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.05)
                else:
                    pytest.fail("service startup timed out")
                assert response.json()["mode"]=="setup"
                assert inspect_instance(tmp_path)["running"]
                stop_instance(tmp_path)
                process.wait(timeout=5)
                assert not inspect_instance(tmp_path)["running"]
                assert not (tmp_path/"bot.instance.json").exists()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows managed launcher')
def test_managed_windows_launcher_reuses_service_and_stops_supervisor(tmp_path):
    import psutil

    root = Path(__file__).parents[1]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
               'Bypass', '-File', str(root / 'scripts/Manage-BuwuBot.ps1'),
               '-DataDir', str(tmp_path), '-Port', str(port), '-SkipDependencies',
               '-PythonPath', sys.executable]
    supervisor = None
    try:
        first = subprocess.run(command + ['-Action', 'Start'], cwd=root,
                               capture_output=True, timeout=30, check=False)
        assert first.returncode == 0, first.stderr.decode(errors='replace')
        instance = inspect_instance(tmp_path)
        assert instance['running']
        # Inspect this isolated instance only; the formal bot is never targeted.
        info = json.loads((tmp_path / 'bot.instance.json').read_text(encoding='utf-8'))
        child = psutil.Process(info['pid'])
        supervisor = next(p for p in child.parents()
                          if p.name().lower() == 'powershell.exe'
                          and 'Run' in p.cmdline()
                          and str(tmp_path) in p.cmdline())
        second = subprocess.run(command + ['-Action', 'Start'], cwd=root,
                                capture_output=True, timeout=30, check=False)
        assert second.returncode == 0, second.stderr.decode(errors='replace')
        assert json.loads((tmp_path / 'bot.instance.json').read_text())['pid'] == child.pid
        stop_instance(tmp_path)
        supervisor.wait(timeout=10)
        assert not inspect_instance(tmp_path)['running']
    finally:
        if inspect_instance(tmp_path)['running']:
            stop_instance(tmp_path)
        if supervisor is not None and supervisor.is_running():
            supervisor.wait(timeout=10)
