import pytest
from fastapi.testclient import TestClient

from buwubot.app import create_app
from buwubot.runtime import InstanceLock


def test_unconfigured_service_runs_without_loading_ai_or_sending(tmp_path):
    with TestClient(create_app(tmp_path, run_bot=False), base_url="http://127.0.0.1:8765") as client:
        response = client.get("/api/status")
        assert response.status_code == 200
        assert response.json()["mode"] == "setup"
        assert response.json()["embedding_loaded"] is False
        assert response.json()["pending_jobs"] == 0


def test_single_instance_lock_releases_after_exit(tmp_path):
    path = tmp_path / "bot.lock"
    with InstanceLock(path), pytest.raises(RuntimeError, match="already running"), InstanceLock(path):
        pass
    with InstanceLock(path):
        pass


def test_default_lifespan_starts_and_stops_the_engine_without_ai_configuration(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert app.state.bot is not None
        assert client.get("/api/status").json()["mode"] == "setup"
        assert app.state.bot.models.status == "unchecked"
    assert all(task.done() for task in app.state.bot._tasks + app.state.bot._workers)
