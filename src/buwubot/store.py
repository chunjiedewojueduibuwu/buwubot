"""Canonical SQLite state; vector indexes are derived, never authoritative."""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings


class Conflict(ValueError):
    """An operation was based on an obsolete revision."""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(
                id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS personas(
                id TEXT PRIMARY KEY, value TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, priority INTEGER NOT NULL, source_time REAL NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_ready
                ON jobs(state,next_attempt,priority,source_time,created_at);
            CREATE TABLE IF NOT EXISTS index_sync(
                fingerprint TEXT NOT NULL, outbox_id INTEGER NOT NULL,
                PRIMARY KEY(fingerprint,outbox_id));
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                kind TEXT NOT NULL, summary TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}');
            PRAGMA user_version=1;
        """)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO settings(id,value) VALUES(1,?)",
                       (json.dumps(Settings().storage(), ensure_ascii=False),))
            saved = db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0]
            self._sync_persona(db, Settings.model_validate_json(saved))

    @contextmanager
    def transaction(self):
        with self._lock:
            savepoint = "nested_" + uuid.uuid4().hex if self.connection.in_transaction else None
            self.connection.execute(f"SAVEPOINT {savepoint}" if savepoint else "BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                if savepoint:
                    self.connection.execute(f"ROLLBACK TO {savepoint}")
                    self.connection.execute(f"RELEASE {savepoint}")
                else:
                    self.connection.rollback()
                raise
            else:
                if savepoint:
                    self.connection.execute(f"RELEASE {savepoint}")
                else:
                    self.connection.commit()

    def get_state(self, key: str, default=None):
        with self._lock:
            row = self.connection.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def rows(self, sql: str, parameters=()) -> list[sqlite3.Row]:
        """Materialize a read under the same lock as writes, including index-worker reads."""
        with self._lock:
            return self.connection.execute(sql, parameters).fetchall()

    def set_state(self, key: str, value):
        with self.transaction() as db:
            db.execute("INSERT INTO state(key,value) VALUES(?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value, ensure_ascii=False)))

    def settings(self) -> tuple[Settings, int]:
        with self._lock:
            row = self.connection.execute("SELECT value,revision FROM settings WHERE id=1").fetchone()
        return Settings.model_validate_json(row[0]), row[1]

    def save_settings(self, settings: Settings, expected_revision: int) -> int:
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE settings SET value=?,revision=revision+1 WHERE id=1 AND revision=?",
                (json.dumps(settings.storage(), ensure_ascii=False), expected_revision),
            )
            if changed.rowcount != 1:
                raise Conflict("settings have changed; reload before saving")
            self._sync_persona(db, settings)
        return expected_revision + 1

    @staticmethod
    def _sync_persona(db, settings):
        value = json.dumps({"name": settings.persona_name, "persona": settings.persona,
                            "aliases": settings.aliases}, ensure_ascii=False)
        db.execute("INSERT INTO personas(id,value) VALUES(?,?) ON CONFLICT(id) "
                   "DO UPDATE SET value=excluded.value,revision=personas.revision+1 "
                   "WHERE personas.value!=excluded.value", (settings.persona_id, value))

    def personas(self):
        return [dict(id=r["id"], revision=r["revision"], **json.loads(r["value"]))
                for r in self.rows("SELECT * FROM personas ORDER BY id")]

    def create_persona(self, pid, name, persona, aliases):
        with self.transaction() as db:
            if db.execute("SELECT id FROM personas WHERE id=?", (pid,)).fetchone():
                raise Conflict("persona already exists")
            db.execute("INSERT INTO personas(id,value) VALUES(?,?)", (pid, json.dumps(
                {"name": name, "persona": persona, "aliases": aliases}, ensure_ascii=False)))
        return next(p for p in self.personas() if p["id"] == pid)

    def switch_persona(self, pid, expected_revision, persona_revision):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM personas WHERE id=?", (pid,)).fetchone()
            if not row or row["revision"] != persona_revision:
                raise Conflict("persona changed; reload before switching")
            current = db.execute("SELECT * FROM settings WHERE id=1").fetchone()
            if current["revision"] != expected_revision:
                raise Conflict("settings changed; reload before switching")
            profile = json.loads(row["value"])
            cfg = Settings.model_validate(json.loads(current["value"]) | {
                "persona_id": pid, "persona_name": profile["name"],
                "persona": profile["persona"], "aliases": profile["aliases"]})
            db.execute("UPDATE settings SET value=?,revision=revision+1 WHERE id=1",
                       (json.dumps(cfg.storage(), ensure_ascii=False),))
        return cfg, expected_revision + 1

    def event(self, kind: str, summary: str, details: dict | None = None):
        with self.transaction() as db:
            db.execute("INSERT INTO events(time,kind,summary,details) VALUES(?,?,?,?)",
                       (datetime.now(UTC).timestamp(), kind, summary,
                        json.dumps(details or {}, ensure_ascii=False)))

    def pending_count(self) -> int:
        with self._lock:
            return self.connection.execute(
                "SELECT count(*) FROM jobs WHERE state IN "
                "('pending','running','retry_wait','needs_attention')"
            ).fetchone()[0]

    def backup(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"bot-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}.db"
        with self._lock, sqlite3.connect(destination) as target:
            self.connection.backup(target)
        for old in sorted(directory.glob("bot-*.db"), reverse=True)[3:]:
            old.unlink()
        return destination

    def close(self):
        with self._lock:
            self.connection.close()

    def compact_if_needed(self) -> bool:
        """Daily maintenance only: reclaim meaningful free space, never during a write transaction."""
        with self._lock:
            if self.connection.in_transaction:return False
            page=self.connection.execute('PRAGMA page_size').fetchone()[0]
            free=self.connection.execute('PRAGMA freelist_count').fetchone()[0]
            total=self.connection.execute('PRAGMA page_count').fetchone()[0]
            if free*page<1024*1024 or free<total*.25:return False
            self.connection.execute('PRAGMA wal_checkpoint(PASSIVE)')
            self.connection.execute('VACUUM')
            self.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            return True
