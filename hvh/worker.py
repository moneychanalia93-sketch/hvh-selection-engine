"""Worker pool that drains the queue and scores applications.

Why no application is lost or scored twice, even when workers crash:

1. **Not lost.** A job leaves the queue only when ``mark_done`` commits. A
   worker that dies before that leaves the job ``leased``; when the lease runs
   out, another worker claims it again.
2. **Not scored twice.** The ack (``mark_done``), the score row and the state
   changes are written in ONE transaction. ``mark_done`` only succeeds for the
   holder of the current lease token, and ``scores.application_id`` is a
   primary key. So a retried or zombie attempt either commits everything once,
   or commits nothing.

Scoring is pure and fast, so it runs before the write transaction; the write
lock is held only for the short commit.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

from hvh import state_machine
from hvh.db import connect, transaction
from hvh.jobs import JobCatalog
from hvh.metrics import Metrics
from hvh.models import Candidate
from hvh.scoring import SCORER_VERSION, ScoreResult, score_candidate
from hvh.state_machine import State
from hvh.work_queue import QueueJob, WorkQueue

log = logging.getLogger(__name__)

# fault_hook(stage, job): tests use it to inject failures. Stages are
# "score" (once per job, before scoring) and "before_commit" (job is None).
FaultHook = Callable[[str, Optional[QueueJob]], None]


class SimulatedCrash(Exception):
    """Raised by a fault hook to mimic a worker dying mid-job (no ack, no fail)."""


@dataclass
class BatchOutcome:
    done: int = 0
    failed: int = 0
    lost: int = 0  # lease was taken over by another worker; result discarded


def _load(conn: sqlite3.Connection, ids: List[str]) -> Dict[str, sqlite3.Row]:
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, job_id, candidate_json FROM applications WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {row["id"]: row for row in rows}


def process_batch(
    conn: sqlite3.Connection,
    queue: WorkQueue,
    catalog: JobCatalog,
    worker_id: str,
    jobs: List[QueueJob],
    fault_hook: Optional[FaultHook] = None,
) -> BatchOutcome:
    """Score a claimed batch and commit results, acks and state changes together."""
    outcome = BatchOutcome()
    rows = _load(conn, [job.application_id for job in jobs])
    scored: List[Tuple[QueueJob, str, ScoreResult]] = []
    failures: List[Tuple[QueueJob, str]] = []
    for job in jobs:
        try:
            if fault_hook:
                fault_hook("score", job)
            row = rows[job.application_id]
            requirements = catalog.get(row["job_id"])
            if requirements is None:
                raise LookupError(f"no requirements loaded for job {row['job_id']!r}")
            result = score_candidate(Candidate.from_dict(json.loads(row["candidate_json"])), requirements)
            scored.append((job, row["job_id"], result))
        except SimulatedCrash:
            raise
        except Exception as exc:  # one bad job must not sink the batch
            failures.append((job, f"{type(exc).__name__}: {exc}"))

    if fault_hook:
        fault_hook("before_commit", None)

    now = queue.clock()
    with transaction(conn):
        for job, job_id, result in scored:
            if not queue.mark_done(conn, job):
                outcome.lost += 1
                continue
            inserted = conn.execute(
                "INSERT INTO scores (application_id, job_id, score, knocked_out, breakdown_json,"
                " scorer_version, worker_id, scored_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (application_id) DO NOTHING",
                (job.application_id, job_id, result.total, int(result.knocked_out),
                 json.dumps(result.to_dict()), SCORER_VERSION, worker_id, now),
            ).rowcount
            if inserted == 0:
                continue  # already scored by an earlier attempt: nothing more to do
            state_machine.transition(conn, job.application_id, State.SCREENED, "screened for required skills", now)
            if result.knocked_out:
                state_machine.transition(conn, job.application_id, State.REJECTED, result.knockout_reason or "", now)
            else:
                state_machine.transition(conn, job.application_id, State.ASSESSED, f"scored {result.total}", now)
            outcome.done += 1

    for job, error in failures:
        queue.fail(conn, job, error)
        outcome.failed += 1
    return outcome


class WorkerPool:
    """N threads, each with its own SQLite connection, draining one queue."""

    def __init__(
        self,
        db_path: Union[str, Path],
        catalog: JobCatalog,
        workers: int = 4,
        batch_size: int = 32,
        queue: Optional[WorkQueue] = None,
        poll_interval: float = 0.05,
        metrics: Optional[Metrics] = None,
        fault_hook: Optional[FaultHook] = None,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self.db_path = db_path
        self.catalog = catalog
        self.workers = workers
        self.batch_size = batch_size
        self.queue = queue or WorkQueue()
        self.poll_interval = poll_interval
        self.metrics = metrics or Metrics()
        self.fault_hook = fault_hook
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._drain = False

    def start(self, drain: bool = False) -> None:
        """Start the threads. With ``drain=True`` they exit once nothing is pending."""
        self._drain = drain
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._run, args=(i,), name=f"hvh-worker-{i}", daemon=True)
            for i in range(self.workers)
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: Optional[float] = 10.0) -> None:
        self._stop.set()
        self.join(timeout)

    def join(self, timeout: Optional[float] = None) -> None:
        for thread in self._threads:
            thread.join(timeout)

    def run_until_drained(self, timeout: Optional[float] = None) -> None:
        """Start, wait for the queue to empty, then return."""
        self.start(drain=True)
        self.join(timeout)
        self._stop.set()

    def _run(self, index: int) -> None:
        worker_id = f"pid{os.getpid()}-w{index}"
        conn = connect(self.db_path)
        try:
            while not self._stop.is_set():
                try:
                    jobs = self.queue.claim(conn, worker_id, self.batch_size)
                    if not jobs:
                        if self._drain and WorkQueue.pending(conn) == 0:
                            return
                        self._stop.wait(self.poll_interval)
                        continue
                    started = time.perf_counter()
                    outcome = process_batch(conn, self.queue, self.catalog, worker_id, jobs, self.fault_hook)
                    elapsed = time.perf_counter() - started
                    for _ in range(outcome.done):
                        self.metrics.observe("score_batch_item", elapsed)
                    self.metrics.incr("scored", outcome.done)
                    self.metrics.incr("score_failures", outcome.failed)
                    self.metrics.incr("lost_leases", outcome.lost)
                except SimulatedCrash:
                    # The "process" died: its leased jobs are abandoned and will
                    # be reclaimed when their leases expire.
                    self.metrics.incr("simulated_crashes")
                except Exception:
                    # Keep the thread alive. The batch rolled back, so its jobs
                    # come back after the lease and dead-letter if this repeats.
                    log.exception("worker %s batch error; retrying", worker_id)
                    self.metrics.incr("worker_errors")
                    self._stop.wait(self.poll_interval)
        finally:
            conn.close()
