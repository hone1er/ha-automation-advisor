"""Compact event log (SQLite) kept by Automation Advisor.

Why not just read the recorder? The recorder's history API drops the *context* of a state change,
and the context is the only way to tell "you pressed the wall switch" from "an automation did it".
The recorder also keeps only ~10 days by default. So the integration logs the few domains it
cares about itself, with the cause already worked out, and backfills once from the recorder.

All methods are blocking; call them from an executor.
"""

from __future__ import annotations

from collections.abc import Iterable
import sqlite3
import threading

from .analysis import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL,
    entity_id TEXT NOT NULL,
    old TEXT,
    new TEXT NOT NULL,
    cause TEXT NOT NULL,
    cause_ref TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE UNIQUE INDEX IF NOT EXISTS events_uniq ON events (entity_id, ts, new);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Archive:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        with self._lock:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def add(self, events: Iterable[Event]) -> int:
        rows = [(e.ts, e.entity_id, e.old, e.new, e.cause, e.cause_ref) for e in events]
        if not rows:
            return 0
        with self._lock:
            assert self._conn is not None
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?)", rows
            )
            self._conn.commit()
            return cur.rowcount

    def since(self, ts: float) -> list[Event]:
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(
                "SELECT ts, entity_id, old, new, cause, cause_ref FROM events "
                "WHERE ts >= ? ORDER BY ts",
                (ts,),
            )
            return [Event(*row) for row in cur.fetchall()]

    def purge(self, before: float) -> int:
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute("DELETE FROM events WHERE ts < ?", (before,))
            self._conn.commit()
            return cur.rowcount

    def stats(self) -> dict:
        with self._lock:
            assert self._conn is not None
            n, first, last = self._conn.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM events"
            ).fetchone()
            return {"events": n, "first": first, "last": last}

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            assert self._conn is not None
            self._conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            self._conn.commit()
