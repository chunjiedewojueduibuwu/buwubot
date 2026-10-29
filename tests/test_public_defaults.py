from pathlib import Path

from fastapi.testclient import TestClient

from buwubot.app import create_app
from buwubot.config import Settings
from buwubot.store import Store


def test_clean_install_has_no_identity_secrets_or_enabled_channels(tmp_path):
    cfg = Settings()
    assert cfg.owner_id == cfg.self_id == cfg.persona == ""
    assert not cfg.group_ids and not cfg.private_ids
    assert not cfg.runtime_enabled and not cfg.qq_enabled
    assert not cfg.owner_private_enabled and not cfg.learning_enabled
    assert cfg.persona_name == "AI助手" and cfg.persona_id == "assistant"
    assert not cfg.ready
    assert not any(cfg.storage()[k] for k in ("workbuddy_key", "qq_token", "qq_ws_token", "embedding_key", "snowluma_password"))
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        state = client.get("/api/status").json()
        assert state["mode"] == "setup" and state["pending_jobs"] == 0
        assert not state["qq_connected"] and not state["embedding_loaded"]


def test_existing_settings_survive_public_defaults_and_restart(tmp_path):
    path = tmp_path / "bot.db"
    store = Store(path)
    cfg, revision = store.settings()
    cfg = Settings.model_validate(cfg.storage() | {"owner_id": "20001", "persona_id": "saved-role",
        "persona_name": "Saved role", "aliases": ["Saved"], "persona": "Synthetic saved identity",
        "runtime_enabled": True, "qq_enabled": True, "workbuddy_key": "synthetic-key"})
    store.save_settings(cfg, revision)
    expected = cfg.storage()
    store.close()
    reopened = Store(path)
    try:
        actual, _ = reopened.settings()
        assert actual.storage() == expected
    finally:
        reopened.close()


def test_launcher_does_not_contain_machine_specific_gateway_path():
    import re
    script = (Path(__file__).parents[1] / "scripts/Manage-BuwuBot.ps1").read_text(encoding="utf-8-sig")
    assert not re.search(r"[A-Z]:[\\/]", script)
