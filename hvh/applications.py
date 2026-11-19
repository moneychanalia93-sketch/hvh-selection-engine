"""Store applications idempotently and read them back.

Idempotency: the client sends an ``Idempotency-Key`` with each request. The
pair ``(client_id, idempotency_key)`` is UNIQUE in the database, so a retried
request finds the row the first attempt created and returns it instead of
making a duplicate. If the same key arrives with a *different* body, that is a
client bug and we refuse it (:class:`IdempotencyConflict`).

Transactional outbox: the application row, its first history event and its
queue job are written in ONE transaction. Either all three exist or none do,
so an accepted application can never be missing from the queue.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from hvh import state_machine
from hvh.db import transaction
from hvh.jobs import JobCatalog
from hvh.models import ValidatedApplication
from hvh.state_machine import State
from hvh.validation import validate_application
from hvh.work_queue import enqueue


class IdempotencyConflict(ValueError):
    """The idempotency key was already used with a different request body."""


@dataclass(frozen=True)
class SubmitResult:
    application_id: str
    created: bool  # False means this was a replay of an earlier request


def request_hash(body: Any) -> str:
    """Stable fingerprint of a body: same JSON content gives the same hash."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_application_id() -> str:
    """A 32-hex-char id that sorts by creation time (like UUIDv7).

    First 16 hex chars: nanoseconds since the epoch. Last 16: random bits.
    Random UUID4 keys land all over the B-tree index, so every insert touches
    a different page; time-ordered keys append at the end and stay in cache.
    On 60,000 inserts this kept ingestion flat at about 850/s instead of
    sliding from 900/s to 570/s.
    """
    return f"{time.time_ns():016x}{secrets.randbits(64):016x}"


def _insert(
    conn: sqlite3.Connection,
    client_id: str,
    idempotency_key: str,
    body_hash: str,
    app: ValidatedApplication,
    now: float,
) -> SubmitResult:
    """Insert one application. Must run inside a write transaction."""
    application_id = new_application_id()
    cursor = conn.execute(
        "INSERT INTO applications (id, client_id, idempotency_key, request_hash, job_id, candidate_json,"
        " state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (client_id, idempotency_key) DO NOTHING",
        (
            application_id, client_id, idempotency_key, body_hash, app.job_id,
            json.dumps(app.candidate.to_dict()), State.APPLIED.value, now, now,
        ),
    )
    if cursor.rowcount == 0:
        existing = conn.execute(
            "SELECT id, request_hash FROM applications WHERE client_id = ? AND idempotency_key = ?",
            (client_id, idempotency_key),
        ).fetchone()
        if existing["request_hash"] != body_hash:
            raise IdempotencyConflict(f"idempotency key {idempotency_key!r} was used with a different body")
        return SubmitResult(existing["id"], created=False)
    if app.self_identification:
        conn.execute(
            "INSERT INTO self_identification (application_id, data_json) VALUES (?, ?)",
            (application_id, json.dumps(app.self_identification, sort_keys=True)),
        )
    state_machine.record_initial(conn, application_id, now)
    enqueue(conn, application_id, now)
    return SubmitResult(application_id, created=True)


def submit(
    conn: sqlite3.Connection,
    catalog: JobCatalog,
    client_id: str,
    idempotency_key: str,
    body: Any,
    clock: Callable[[], float] = time.time,
) -> SubmitResult:
    """Validate and store one application (the API's code path).

    Raises :class:`hvh.validation.ValidationError` or :class:`IdempotencyConflict`.
    """
    app = validate_application(body, catalog)
    body_hash = request_hash(body)
    with transaction(conn):
        return _insert(conn, client_id, idempotency_key, body_hash, app, clock())


def submit_many(
    conn: sqlite3.Connection,
    catalog: JobCatalog,
    client_id: str,
    items: Iterable[Tuple[str, Any]],
    batch_size: int = 1000,
    clock: Callable[[], float] = time.time,
) -> Tuple[int, int]:
    """Bulk load ``(idempotency_key, body)`` pairs in batched transactions.

    Same validation and idempotency rules as :func:`submit`; batching just
    commits many rows at once, which is far faster for seeding.
    Returns ``(created, replayed)``.
    """
    created = replayed = 0
    batch = []

    def flush() -> None:
        nonlocal created, replayed
        with transaction(conn):
            for key, body_hash, app in batch:
                if _insert(conn, client_id, key, body_hash, app, clock()).created:
                    created += 1
                else:
                    replayed += 1
        batch.clear()

    for key, body in items:
        batch.append((key, request_hash(body), validate_application(body, catalog)))
        if len(batch) >= batch_size:
            flush()
    if batch:
        flush()
    return created, replayed


def get_application(conn: sqlite3.Connection, application_id: str) -> Optional[Dict[str, Any]]:
    """Everything about one application: state, score breakdown and history."""
    row = conn.execute(
        "SELECT id, job_id, candidate_json, state, created_at, updated_at FROM applications WHERE id = ?",
        (application_id,),
    ).fetchone()
    if row is None:
        return None
    score_row = conn.execute(
        "SELECT score, knocked_out, breakdown_json, scored_at FROM scores WHERE application_id = ?",
        (application_id,),
    ).fetchone()
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "state": row["state"],
        "candidate": json.loads(row["candidate_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "score": None if score_row is None else json.loads(score_row["breakdown_json"]),
        "history": state_machine.history(conn, application_id),
    }
