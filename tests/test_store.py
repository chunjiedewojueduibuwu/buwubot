import sqlite3

import pytest

from buwubot.config import Settings
from buwubot.store import Conflict, Store


def test_transaction_rolls_back_all_changes_and_reopens_durably(tmp_path):
    path = tmp_path / "bot.db"
    store = Store(path)
    with pytest.raises(RuntimeError), store.transaction() as db:
        db.execute("INSERT INTO state(key,value) VALUES ('probe','bad')")
        raise RuntimeError("simulated interruption")
    assert store.get_state("probe") is None
    store.set_state("probe", {"cursor": 42})
    store.close()
    reopened = Store(path)
    assert reopened.get_state("probe") == {"cursor": 42}
    assert reopened.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert reopened.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    reopened.close()


def test_config_revision_conflict_does_not_partially_save(tmp_path):
    store = Store(tmp_path / "bot.db")
    initial, revision = store.settings()
    assert not initial.ready
    changed = initial.model_copy(update={"group_id": "123", "persona": "群友"})
    next_revision = store.save_settings(changed, revision)
    with pytest.raises(Conflict):
        store.save_settings(initial.model_copy(update={"group_id": "999"}), revision)
    actual, actual_revision = store.settings()
    assert actual.group_id == "123"
    assert actual_revision == next_revision
    store.close()


def test_backup_is_consistent_and_readable(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.set_state("cursor", 9)
    backup = store.backup(tmp_path / "backups")
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()[0] == "9"
    store.close()


def test_settings_reject_invalid_values_and_keep_secrets_private():
    with pytest.raises(ValueError):
        Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], sticker_per_category=0)
    with pytest.raises(ValueError):
        Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="not-a-qq-number")
    with pytest.raises(ValueError):
        Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], reboot_wakeup="14:00")
    settings = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], workbuddy_key="example-private-key", qq_token="private-qq")
    assert "example-private-key" not in str(settings)
    assert "example-private-key" not in str(settings.public())
    assert settings.storage()["workbuddy_key"] == "example-private-key"
    assert settings.public()["workbuddy_key_set"] is True



def test_nested_canonical_operations_roll_back_with_outer_transaction(tmp_path):
    store=Store(tmp_path/"bot.db")
    cfg,revision=store.settings()
    try:
        with pytest.raises(ValueError,match="injected"), store.transaction():
            store.save_settings(cfg.model_copy(update={"persona":"must roll back"}),revision)
            store.event("test","must roll back")
            raise ValueError("injected")
        assert store.settings()[1]==revision
        assert store.settings()[0].persona==cfg.persona
        assert not store.rows("SELECT * FROM events")
    finally:store.close()
