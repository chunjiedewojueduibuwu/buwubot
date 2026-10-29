"""Exit evidence in isolated data directories; no live QQ or formal bot access."""

import faulthandler
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from buwubot import app


@pytest.mark.parametrize("outcome", ["returned", "management_stop", "exception", "interrupt", "system_exit"])
def test_main_logs_exit_reason_and_preserves_exceptions(tmp_path, monkeypatch, caplog, outcome):
    def run():
        if outcome == "management_stop":
            server.config.app.state.shutdown_requested = True
            server.config.app.state.shutdown()
            assert server.should_exit
        elif outcome == "exception":
            raise RuntimeError("isolated failure")
        elif outcome == "interrupt":
            raise KeyboardInterrupt
        elif outcome == "system_exit":
            raise SystemExit(3)

    server = SimpleNamespace(run=run, should_exit=False)
    def make_server(config):
        server.config = config
        return server

    monkeypatch.setattr("uvicorn.Server", make_server)
    monkeypatch.setattr(sys, "argv", ["buwubot", "--data-dir", str(tmp_path)])
    # Avoid creating logging handlers in the pytest process.
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: [h.close() for h in kwargs["handlers"]])
    enabled = faulthandler.is_enabled()
    if enabled:
        monkeypatch.setattr(faulthandler, "enable", lambda **kwargs: pytest.fail("existing handler replaced"))
    caplog.set_level(logging.INFO, logger="buwubot.app")
    expected = {"exception": RuntimeError, "interrupt": KeyboardInterrupt, "system_exit": SystemExit}
    if outcome in expected:
        with pytest.raises(expected[outcome]):
            app.main()
    else:
        app.main()
    assert faulthandler.is_enabled() == enabled
    if enabled:
        assert not (tmp_path / "logs/fault.log").exists()
    text = caplog.text
    assert "event=service_start" in text
    assert f"pid={os.getpid()}" in text
    reason = {"returned": "server_returned", "exception": "exception", "interrupt": "keyboard_interrupt"}.get(outcome, outcome)
    assert f"reason={reason}" in text
    if outcome == "exception":
        assert "Traceback (most recent call last)" in text
        assert "RuntimeError: isolated failure" in text


async def test_management_stop_endpoint_logs_only_accepted_requests(tmp_path, caplog):
    application = app.create_app(tmp_path, run_bot=False)
    caplog.set_level(logging.INFO, logger="buwubot.app")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://localhost") as client:
        assert (await client.post("/api/runtime/stop", json={})).status_code == 409
        assert "event=management_stop_requested" not in caplog.text
        application.state.shutdown = lambda: None
        assert (await client.post("/api/runtime/stop", json={})).status_code == 200
        assert "event=management_stop_requested" in caplog.text


def test_real_process_retains_traceback_and_fault_file_after_main_raises(tmp_path):
    code = """
import faulthandler, sys
from buwubot import app
sys.argv = ['buwubot', '--data-dir', sys.argv[1]]
def fail(*args, **kwargs):
    raise RuntimeError('isolated startup failure')
app.create_app = fail
try:
    app.main()
except RuntimeError:
    assert not app._fault_log.closed
    faulthandler.dump_traceback(file=app._fault_log)
    raise
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=False, capture_output=True,
                            timeout=10, env={**os.environ, "PYTHONFAULTHANDLER": "",
                                             "PYTHONPATH": str(Path(__file__).parents[1] / "src")})
    assert result.returncode != 0
    log = (tmp_path / "logs/service.log").read_text(encoding="utf-8")
    assert "reason=exception" in log
    assert "Traceback (most recent call last)" in log
    assert "RuntimeError: isolated startup failure" in log
    assert "Current thread" in (tmp_path / "logs/fault.log").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows managed launcher")
def test_launcher_records_startup_exception(tmp_path):
    root = Path(__file__).parents[1]
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-File", str(root / "scripts/Manage-BuwuBot.ps1"), "-Action", "Run",
                             "-DataDir", str(tmp_path), "-PythonPath", str(tmp_path / "missing.exe")],
                            check=False, capture_output=True, timeout=10)
    assert result.returncode != 0
    records = [json.loads(line) for line in (tmp_path / "logs/launcher.jsonl").read_text(encoding="utf-8-sig").splitlines()]
    assert records[-1]["event"] == "launcher_exception"
    assert records[-1]["error_type"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows managed launcher")
def test_launcher_records_already_running_without_starting_child(tmp_path):
    root = Path(__file__).parents[1]
    worker = tmp_path / "fake_runtime.py"
    worker.write_text("import sys\nassert '--status' in sys.argv\nsys.exit(0)\n", encoding="utf-8")
    executable = tmp_path / "fake-python.cmd"
    executable.write_text(f'@"{sys.executable}" "{worker}" %*\r\n', encoding="utf-8")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-File", str(root / "scripts/Manage-BuwuBot.ps1"), "-Action", "Run",
                             "-DataDir", str(tmp_path), "-PythonPath", str(executable)],
                            check=False, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    records = [json.loads(line) for line in (tmp_path / "logs/launcher.jsonl").read_text(encoding="utf-8-sig").splitlines()]
    assert records[-1]["event"] == "already_running"
    assert not any(r["event"] == "child_start" for r in records)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows managed launcher")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_launcher_records_child_exit_and_preserves_stop_flag_semantics(tmp_path, exit_code):
    root = Path(__file__).parents[1]
    data = tmp_path / "isolated-data"
    data.mkdir()
    # Run has always cleared an old administrative stop before its first launch.
    (data / "stop.request").write_text("old stop", encoding="utf-8")
    worker = tmp_path / "fake_runtime.py"
    worker.write_text(
        "import pathlib,sys\n"
        "data=pathlib.Path(sys.argv[sys.argv.index('--data-dir')+1])\n"
        "if '--status' in sys.argv: sys.exit(1)\n"
        "assert not (data/'stop.request').exists()\n"
        "(data/'stop.request').write_text('new stop')\n"
        f"sys.exit({exit_code})\n", encoding="utf-8")
    executable = tmp_path / "fake-python.cmd"
    executable.write_text(f'@"{sys.executable}" "{worker}" %*\r\n', encoding="utf-8")
    wrapper = tmp_path / "run.ps1"
    wrapper.write_text("function Start-Sleep {}\n& $args[0] -Action Run -DataDir $args[1] -PythonPath $args[2]\n", encoding="utf-8")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper), str(root / "scripts/Manage-BuwuBot.ps1"), str(data), str(executable)], check=False, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    records = [json.loads(line) for line in (data / "logs/launcher.jsonl").read_text(encoding="utf-8-sig").splitlines()]
    started = next(r for r in records if r["event"] == "child_start")
    exited = next(r for r in records if r["event"] == "child_exit")
    assert started["stop_requested"] is False
    assert exited["stop_requested"] is True
    assert exited["child_exit_code"] == exit_code
    assert exited["launcher_pid"] > 0
    assert datetime.fromisoformat(exited["timestamp"]).tzinfo is not None
    assert records[0]["event"] == "stale_stop_cleared"
    if exit_code:
        assert records[-1]["event"] == "stop_requested"
