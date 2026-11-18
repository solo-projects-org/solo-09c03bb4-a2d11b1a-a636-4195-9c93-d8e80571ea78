"""SQLite storage and schema for the turbine fault-code classification service.

Design notes
------------
All state that decides whether a daily publish is *current* lives in SQLite and is
changed only inside ``BEGIN IMMEDIATE`` transactions.  Nothing is cached in
process memory, so a restart cannot resurrect a stale publish: on boot the data
on disk is the whole truth.

The ``publishes`` table is append-only with respect to "currentness": a publish
is current when its ``status`` is ``CURRENT``.  Any event correction that touches
a published (unit, UTC day) or any mapping revision that changes the effective
category for that day revokes the publish by flipping it to ``REVOKED`` - it is
never deleted, so the audit trail shows exactly which revision-set used to be
live.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_no            TEXT    NOT NULL,
    revision            INTEGER NOT NULL,
    unit                TEXT    NOT NULL,
    firmware_version    TEXT    NOT NULL,
    fault_code          TEXT    NOT NULL,
    occurred_at         TEXT    NOT NULL,   -- canonical UTC ISO-8601, ...Z
    revision_created_at TEXT    NOT NULL,   -- when this revision was recorded
    UNIQUE (event_no, revision)
);

CREATE INDEX IF NOT EXISTS idx_events_unit_day
    ON events (unit, substr(occurred_at, 1, 10));

CREATE TABLE IF NOT EXISTS mappings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    firmware_version   TEXT    NOT NULL,
    fault_code         TEXT    NOT NULL,
    revision           INTEGER NOT NULL,
    category           TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,
    UNIQUE (firmware_version, fault_code, revision)
);

CREATE TABLE IF NOT EXISTS publishes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    unit               TEXT    NOT NULL,
    day                TEXT    NOT NULL,          -- YYYY-MM-DD (UTC)
    event_revisions    TEXT    NOT NULL,          -- e.g. "EVT-1@2,EVT-2@1"
    recipe             TEXT    NOT NULL,          -- event revs + mapping revs used
    event_count        INTEGER NOT NULL,
    status             TEXT    NOT NULL CHECK (status IN ('CURRENT', 'REVOKED')),
    created_at         TEXT    NOT NULL,
    revoked_at         TEXT,
    revoked_reason     TEXT
);

-- At most one CURRENT publish per (unit, day).  This partial unique index is the
-- last line of defence: even if two workers raced, the second COMMIT would fail
-- rather than silently shadow the first.
CREATE UNIQUE INDEX IF NOT EXISTS idx_publishes_one_current
    ON publishes (unit, day) WHERE status = 'CURRENT';

CREATE TABLE IF NOT EXISTS publish_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_id      INTEGER NOT NULL REFERENCES publishes(id),
    event_no        TEXT    NOT NULL,
    revision        INTEGER NOT NULL,
    unit            TEXT    NOT NULL,
    firmware_version TEXT   NOT NULL,
    fault_code      TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    occurred_at     TEXT    NOT NULL,
    map_revision    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_publish_items_publish
    ON publish_items (publish_id);

-- Key-set pagination for the streaming CSV export: each batch seeks straight to
-- the row after the previous batch instead of re-scanning all items of a publish.
CREATE INDEX IF NOT EXISTS idx_publish_items_stream
    ON publish_items (publish_id, occurred_at, event_no, id);

CREATE TABLE IF NOT EXISTS operation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    detail      TEXT    NOT NULL
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Store:
    """Thin connection wrapper.  One ``Store`` per request is cheap for SQLite."""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def init(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Serialised writer.  ``BEGIN IMMEDIATE`` takes the write lock up front so
        read-check-write sequences (e.g. "is this revision next?") are atomic."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()


def log(conn: sqlite3.Connection, action: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO operation_log (at, action, detail) VALUES (?, ?, ?)",
        (utc_now_iso(), action, detail),
    )
