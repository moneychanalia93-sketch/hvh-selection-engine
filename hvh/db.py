"""SQLite connection handling and schema.

Every thread opens its own connection: a ``sqlite3.Connection`` must not be
shared between threads. WAL (write-ahead logging) mode lets many readers run
while one writer commits, which is what a queue with several workers needs.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

PathLike = Union[str, Path]

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT    NOT NULL UNIQUE,
    client_id       TEXT    NOT NULL,
    idempotency_key TEXT    NOT NULL,
    request_hash    TEXT    NOT NULL,
    job_id          TEXT    NOT NULL,
    candidate_json  TEXT    NOT NULL,
    state           TEXT    NOT NULL,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    UNIQUE (client_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS applications_job_state ON applications (job_id, state);

-- Voluntary self-identification lives in its own table. The scorer never reads
-- it; only the fairness audit does.
CREATE TABLE IF NOT EXISTS self_identification (
    application_id TEXT PRIMARY KEY REFERENCES applications (id),
    data_json      TEXT NOT NULL
);

-- One score per application, enforced by the primary key. This is what makes
-- at-least-once delivery safe: a second attempt cannot create a second score.
CREATE TABLE IF NOT EXISTS scores (
    application_id TEXT PRIMARY KEY REFERENCES applications (id),
    job_id         TEXT    NOT NULL,
    score          REAL    NOT NULL,
    knocked_out    INTEGER NOT NULL,
    breakdown_json TEXT    NOT NULL,
    scorer_version TEXT    NOT NULL,
    worker_id      TEXT    NOT NULL,
    scored_at      REAL    NOT NULL
);

-- Append-only history of every state change.
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL REFERENCES applications (id),
    from_state     TEXT,
    to_state       TEXT NOT NULL,
    reason         TEXT NOT NULL,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_application ON events (application_id, id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only');
END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only');
END;

-- Durable work queue: one row per application waiting to be scored.
CREATE TABLE IF NOT EXISTS queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id   TEXT    NOT NULL UNIQUE REFERENCES applications (id),
    status           TEXT    NOT NULL CHECK (status IN ('ready', 'leased', 'done', 'dead')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    available_at     REAL    NOT NULL,
    lease_token      TEXT,
    lease_owner      TEXT,
    lease_expires_at REAL,
    last_error       TEXT,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS queue_ready ON queue (status, available_at);
CREATE INDEX IF NOT EXISTS queue_lease ON queue (status, lease_expires_at);
"""


def connect(path: PathLike) -> sqlite3.Connection:
    """Open a connection configured for this app.

    ``isolation_level=None`` turns off Python's implicit transactions, so the
    code controls transactions itself with :func:`transaction`.
    """
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL is safe in WAL mode: a power cut can lose the last commits but
    # never corrupts the file. FULL would fsync on every commit.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    # 64 MiB page cache (default is about 2 MiB). With 100k+ rows the indexes
    # outgrow the default and every insert starts paying for disk reads.
    conn.execute("PRAGMA cache_size=-65536")
    return conn


def init_db(path: PathLike) -> None:
    """Create the schema if it does not exist yet."""
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside ``BEGIN IMMEDIATE`` ... ``COMMIT``.

    ``IMMEDIATE`` takes the write lock up front. Two workers can then never
    both read "this job is free" and both claim it.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def connection(path: PathLike) -> Iterator[sqlite3.Connection]:
    """Open a connection and always close it."""
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()
