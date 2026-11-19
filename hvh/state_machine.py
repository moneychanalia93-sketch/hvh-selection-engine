"""Candidate lifecycle with enforced transitions and an append-only history.

    applied -> screened -> assessed -> shortlisted
                   |           |
                   v           v
                rejected    rejected

* ``screened``: the application was read and checked for required skills.
* ``assessed``: it passed screening and has a full score.
* ``rejected`` from ``screened``: a required skill was missing (knockout).
* ``rejected`` from ``assessed``: it was scored but not picked for the shortlist.
"""

from __future__ import annotations

import sqlite3
from enum import Enum
from typing import Dict, FrozenSet, List


class State(str, Enum):
    APPLIED = "applied"
    SCREENED = "screened"
    ASSESSED = "assessed"
    SHORTLISTED = "shortlisted"
    REJECTED = "rejected"


TRANSITIONS: Dict[State, FrozenSet[State]] = {
    State.APPLIED: frozenset({State.SCREENED}),
    State.SCREENED: frozenset({State.ASSESSED, State.REJECTED}),
    State.ASSESSED: frozenset({State.SHORTLISTED, State.REJECTED}),
    State.SHORTLISTED: frozenset(),
    State.REJECTED: frozenset(),
}


class InvalidTransition(ValueError):
    """The requested state change is not allowed from the current state."""


class ApplicationNotFound(LookupError):
    """No application has this id."""


def can_transition(current: State, target: State) -> bool:
    return target in TRANSITIONS[current]


def record_initial(conn: sqlite3.Connection, application_id: str, now: float) -> None:
    """Write the first event (``None -> applied``). Call inside a transaction."""
    conn.execute(
        "INSERT INTO events (application_id, from_state, to_state, reason, created_at) VALUES (?, NULL, ?, ?, ?)",
        (application_id, State.APPLIED.value, "application received", now),
    )


def transition(conn: sqlite3.Connection, application_id: str, target: State, reason: str, now: float) -> State:
    """Move an application to ``target`` and append an event.

    Must run inside a write transaction. The ``UPDATE ... WHERE state = ?`` is
    a compare-and-set: if another writer changed the state in between, no row
    matches and the move is refused instead of silently overwriting.
    Returns the previous state.
    """
    row = conn.execute("SELECT state FROM applications WHERE id = ?", (application_id,)).fetchone()
    if row is None:
        raise ApplicationNotFound(application_id)
    current = State(row["state"])
    if not can_transition(current, target):
        raise InvalidTransition(f"{application_id}: cannot go from {current.value} to {target.value}")
    cursor = conn.execute(
        "UPDATE applications SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
        (target.value, now, application_id, current.value),
    )
    if cursor.rowcount != 1:
        raise InvalidTransition(f"{application_id}: state changed concurrently")
    conn.execute(
        "INSERT INTO events (application_id, from_state, to_state, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (application_id, current.value, target.value, reason, now),
    )
    return current


def history(conn: sqlite3.Connection, application_id: str) -> List[Dict[str, object]]:
    """All events for one application, oldest first."""
    rows = conn.execute(
        "SELECT from_state, to_state, reason, created_at FROM events WHERE application_id = ? ORDER BY id",
        (application_id,),
    ).fetchall()
    return [dict(r) for r in rows]
