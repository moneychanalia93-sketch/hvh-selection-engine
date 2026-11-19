"""Build a top-K shortlist for one job from scored applications."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, List

from hvh import state_machine
from hvh.db import transaction
from hvh.ranking import top_k
from hvh.state_machine import State


@dataclass(frozen=True)
class ShortlistEntry:
    rank: int
    application_id: str
    score: float


def preview(conn: sqlite3.Connection, job_id: str, k: int) -> List[ShortlistEntry]:
    """Rank ``assessed`` applications for a job. Read-only.

    Rows stream from the cursor in arrival order (``seq``), so ties go to the
    earlier application and memory stays O(k).
    """
    cursor = conn.execute(
        "SELECT a.id, s.score FROM applications a JOIN scores s ON s.application_id = a.id"
        " WHERE a.job_id = ? AND a.state = ? ORDER BY a.seq",
        (job_id, State.ASSESSED.value),
    )
    best = top_k(cursor, k, key=lambda row: row["score"])
    return [ShortlistEntry(i + 1, row["id"], row["score"]) for i, row in enumerate(best)]


def commit(
    conn: sqlite3.Connection, job_id: str, k: int, clock: Callable[[], float] = time.time
) -> List[ShortlistEntry]:
    """Shortlist the top ``k`` and reject the rest of the ``assessed`` pool, in one transaction."""
    with transaction(conn):
        entries = preview(conn, job_id, k)
        chosen = {e.application_id for e in entries}
        now = clock()
        for entry in entries:
            state_machine.transition(conn, entry.application_id, State.SHORTLISTED, f"rank {entry.rank} of top {k}", now)
        rest = conn.execute(
            "SELECT id FROM applications WHERE job_id = ? AND state = ?", (job_id, State.ASSESSED.value)
        ).fetchall()
        for row in rest:
            if row["id"] not in chosen:
                state_machine.transition(conn, row["id"], State.REJECTED, f"not in top {k}", now)
    return entries
