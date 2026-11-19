"""Prove no application is lost or scored twice when workers crash."""

import random
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from hvh.applications import submit_many
from hvh.jobs import JobCatalog
from hvh.loadgen import generate
from hvh.work_queue import QueueSettings, WorkQueue
from hvh.worker import SimulatedCrash, WorkerPool, process_batch
from tests.helpers import CATALOG, DbTestCase


class CrashRecoveryTests(DbTestCase):
    def load(self, n: int, job_ids: Any = ("sde-l4-hvh",)) -> None:
        submit_many(self.conn, CATALOG, "test", generate(n, seed=3, job_ids=job_ids))

    def assertEachScoredExactlyOnce(self, n: int) -> None:
        q = WorkQueue.counts(self.conn)
        self.assertEqual(q["done"], n, q)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"], n)
        per_app = self.conn.execute(
            "SELECT a.id, (SELECT COUNT(*) FROM events e WHERE e.application_id = a.id AND e.to_state = 'screened') c"
            " FROM applications a").fetchall()
        self.assertEqual({row["c"] for row in per_app}, {1})  # every app screened once, none twice
        not_done = self.conn.execute("SELECT COUNT(*) n FROM applications WHERE state IN ('applied','screened')")
        self.assertEqual(not_done.fetchone()["n"], 0)

    def test_random_crashes_lose_nothing_and_double_nothing(self) -> None:
        n = 600
        self.load(n)
        rng = random.Random(42)
        lock = threading.Lock()

        def chaos(stage: str, job: Any) -> None:
            with lock:
                roll = rng.random()
            if (stage == "score" and roll < 0.02) or (stage == "before_commit" and roll < 0.25):
                raise SimulatedCrash()

        pool = WorkerPool(self.db_path, CATALOG, workers=4, batch_size=16, fault_hook=chaos,
                          queue=WorkQueue(QueueSettings(lease_seconds=0.2, max_attempts=100)))
        pool.run_until_drained(timeout=120)
        self.assertGreater(pool.metrics.snapshot()["counters"]["simulated_crashes"], 5)
        self.assertGreater(self.conn.execute("SELECT COUNT(*) n FROM queue WHERE attempts > 1").fetchone()["n"], 0)
        self.assertEachScoredExactlyOnce(n)

    def test_zombie_worker_cannot_commit_after_lease_taken_over(self) -> None:
        self.load(1)
        clock = [time.time() + 10]
        queue = WorkQueue(QueueSettings(lease_seconds=5), clock=lambda: clock[0])
        zombie_jobs = queue.claim(self.conn, "zombie", 1)
        clock[0] += 6  # zombie stalls (GC pause, network hiccup) past its lease
        fresh_jobs = queue.claim(self.conn, "fresh", 1)
        self.assertEqual(process_batch(self.conn, queue, CATALOG, "fresh", fresh_jobs).done, 1)
        late = process_batch(self.conn, queue, CATALOG, "zombie", zombie_jobs)  # zombie wakes up
        self.assertEqual((late.done, late.lost), (0, 1))
        self.assertEachScoredExactlyOnce(1)
        worker = self.conn.execute("SELECT worker_id FROM scores").fetchone()["worker_id"]
        self.assertEqual(worker, "fresh")

    def test_killed_process_jobs_are_recovered(self) -> None:
        n = 200
        self.load(n)
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        script = Path(__file__).with_name("crash_worker.py")
        proc = subprocess.Popen([sys.executable, str(script), str(self.db_path), "50"],
                                stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "CLAIMED 50")
        finally:
            proc.kill()  # hard kill: no cleanup code runs, like a crashed host
            proc.wait(10)
            proc.stdout.close()
        self.assertEqual(WorkQueue.counts(self.conn)["leased"], 50)
        WorkerPool(self.db_path, CATALOG, workers=3).run_until_drained(timeout=60)
        self.assertEachScoredExactlyOnce(n)
        reclaimed = self.conn.execute("SELECT COUNT(*) n FROM queue WHERE attempts = 2").fetchone()["n"]
        self.assertEqual(reclaimed, 50)

    def test_failing_job_dead_letters_without_blocking_others(self) -> None:
        self.load(20, job_ids=("sde-l4-hvh", "data-engineer-l4"))
        # A catalog that lost one job's requirements: those jobs fail every time.
        partial = JobCatalog([CATALOG.get("sde-l4-hvh")])
        queue = WorkQueue(QueueSettings(max_attempts=2, backoff_base_seconds=0.01, backoff_max_seconds=0.01))
        WorkerPool(self.db_path, partial, workers=2, queue=queue).run_until_drained(timeout=60)
        counts = WorkQueue.counts(self.conn)
        self.assertEqual((counts["done"], counts["dead"]), (10, 10))
        error = self.conn.execute("SELECT last_error FROM queue WHERE status='dead' LIMIT 1").fetchone()[0]
        self.assertIn("data-engineer-l4", error)


if __name__ == "__main__":
    unittest.main()
