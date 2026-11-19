"""A durable work queue stored in SQLite.

Life of a queue row::

    ready --claim--> leased --mark_done--> done
      ^                |
      |   fail (attempts < max)            fail or expired lease (attempts >= max)
      +----------------+                      --> dead

* **Lease (visibility timeout).** A claim hides the row for ``lease_seconds``.
  If the worker dies, the lease runs out and another worker can claim it.
* **Lease token.** Each claim writes a fresh random token. Only the holder of
  the current token can finish or fail the job, so a slow "zombie" worker
  whose lease was taken over cannot commit stale work.
* **At-least-once.** A job can be handed out more than once (after a crash).
  The consumer makes this safe by being idempotent; see ``worker.py``.
"""

from __future__ import annotations

import random
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from hvh.db import transaction


@dataclass(frozen=True)
class QueueSettings:
    lease_seconds: float = 30.0
    max_attempts: int = 5
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 30.0


@dataclass(frozen=True)
class QueueJob:
    """A job a worker holds a lease on."""

    queue_id: int
    application_id: str
    attempts: int
    lease_token: str


def compute_backoff(attempts: int, base: float, cap: float, rng: Optional[random.Random] = None) -> float:
    """Exponential backoff: ``base * 2**(attempts-1)``, capped at ``cap``.

    With ``rng`` it uses "full jitter" (a random delay from 0 to that value),
    which spreads retries out so failed jobs do not all retry at once.
    """
    delay = min(cap, base * (2 ** max(0, attempts - 1)))
    return rng.uniform(0, delay) if rng else delay


def enqueue(conn: sqlite3.Connection, application_id: str, now: float) -> None:
    """Add a job. Call inside the same transaction that stores the application."""
    conn.execute(
        "INSERT INTO queue (application_id, status, attempts, available_at, created_at, updated_at)"
        " VALUES (?, 'ready', 0, ?, ?, ?)",
        (application_id, now, now, now),
    )


class WorkQueue:
    """Claim, finish and fail jobs. One instance can be shared; connections cannot."""

    def __init__(
        self,
        settings: QueueSettings = QueueSettings(),
        clock: Callable[[], float] = time.time,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.rng = rng

    def claim(self, conn: sqlite3.Connection, worker_id: str, limit: int = 1) -> List[QueueJob]:
        """Lease up to ``limit`` jobs: expired leases first, then ready jobs.

        An expired lease whose job already used every attempt goes to ``dead``
        instead: a job that keeps crashing workers is a poison message.
        """
        now = self.clock()
        expires = now + self.settings.lease_seconds
        jobs: List[QueueJob] = []
        with transaction(conn):
            expired = conn.execute(
                "SELECT id, application_id, attempts FROM queue"
                " WHERE status = 'leased' AND lease_expires_at <= ? ORDER BY lease_expires_at LIMIT ?",
                (now, limit),
            ).fetchall()
            candidates = []
            for row in expired:
                if row["attempts"] >= self.settings.max_attempts:
                    conn.execute(
                        "UPDATE queue SET status = 'dead', lease_token = NULL, last_error = ?, updated_at = ?"
                        " WHERE id = ?",
                        ("lease expired on final attempt (worker crashed or timed out)", now, row["id"]),
                    )
                else:
                    candidates.append(row)
            remaining = limit - len(candidates)
            if remaining > 0:
                candidates.extend(
                    conn.execute(
                        "SELECT id, application_id, attempts FROM queue"
                        " WHERE status = 'ready' AND available_at <= ? ORDER BY available_at, id LIMIT ?",
                        (now, remaining),
                    ).fetchall()
                )
            for row in candidates:
                token = uuid.uuid4().hex
                conn.execute(
                    "UPDATE queue SET status = 'leased', attempts = attempts + 1, lease_token = ?,"
                    " lease_owner = ?, lease_expires_at = ?, updated_at = ? WHERE id = ?",
                    (token, worker_id, expires, now, row["id"]),
                )
                jobs.append(QueueJob(row["id"], row["application_id"], row["attempts"] + 1, token))
        return jobs

    def mark_done(self, conn: sqlite3.Connection, job: QueueJob) -> bool:
        """Finish a job. Returns False if this worker no longer holds the lease.

        Call inside the transaction that writes the job's result, so the
        result and the ack commit together or not at all.
        """
        cursor = conn.execute(
            "UPDATE queue SET status = 'done', lease_token = NULL, updated_at = ?"
            " WHERE id = ? AND status = 'leased' AND lease_token = ?",
            (self.clock(), job.queue_id, job.lease_token),
        )
        return cursor.rowcount == 1

    def fail(self, conn: sqlite3.Connection, job: QueueJob, error: str) -> str:
        """Record a failed attempt. Returns the new status: ``ready``, ``dead`` or ``lost``."""
        now = self.clock()
        if job.attempts >= self.settings.max_attempts:
            status, available_at = "dead", now
        else:
            status = "ready"
            available_at = now + compute_backoff(
                job.attempts, self.settings.backoff_base_seconds, self.settings.backoff_max_seconds, self.rng
            )
        with transaction(conn):
            cursor = conn.execute(
                "UPDATE queue SET status = ?, available_at = ?, lease_token = NULL, last_error = ?, updated_at = ?"
                " WHERE id = ? AND status = 'leased' AND lease_token = ?",
                (status, available_at, error[:1000], now, job.queue_id, job.lease_token),
            )
        return status if cursor.rowcount == 1 else "lost"

    @staticmethod
    def counts(conn: sqlite3.Connection) -> Dict[str, int]:
        """Number of jobs per status, always with all four keys."""
        result = {"ready": 0, "leased": 0, "done": 0, "dead": 0}
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM queue GROUP BY status"):
            result[row["status"]] = row["n"]
        return result

    @staticmethod
    def pending(conn: sqlite3.Connection) -> int:
        """Jobs that still need work (ready or leased)."""
        row = conn.execute("SELECT COUNT(*) AS n FROM queue WHERE status IN ('ready', 'leased')").fetchone()
        return int(row["n"])
