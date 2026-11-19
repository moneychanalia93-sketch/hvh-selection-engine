import random
import unittest

from hvh.applications import submit
from hvh.work_queue import QueueSettings, WorkQueue, compute_backoff
from tests.helpers import CATALOG, DbTestCase, sample_body


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class WorkQueueTests(DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = FakeClock()
        self.queue = WorkQueue(QueueSettings(lease_seconds=10, max_attempts=3, backoff_base_seconds=1,
                                             backoff_max_seconds=60), clock=self.clock)
        # submit() stamps rows with real time; move the fake clock past it.
        self.ids = [submit(self.conn, CATALOG, "c", f"k{i}", sample_body(years_experience=i)).application_id
                    for i in range(3)]
        self.clock.now = 10_000_000_000.0

    def test_claim_is_fifo_and_hides_leased_jobs(self) -> None:
        first = self.queue.claim(self.conn, "w1", limit=2)
        self.assertEqual([j.application_id for j in first], self.ids[:2])
        second = self.queue.claim(self.conn, "w2", limit=5)
        self.assertEqual([j.application_id for j in second], self.ids[2:])
        self.assertEqual(self.queue.claim(self.conn, "w3", limit=5), [])

    def test_ack_with_current_token(self) -> None:
        job = self.queue.claim(self.conn, "w1")[0]
        self.assertTrue(self.queue.mark_done(self.conn, job))
        self.assertFalse(self.queue.mark_done(self.conn, job))  # second ack is a no-op
        self.assertEqual(WorkQueue.counts(self.conn)["done"], 1)

    def test_expired_lease_is_reclaimed_and_old_token_rejected(self) -> None:
        stale = self.queue.claim(self.conn, "w1", limit=1)[0]
        self.clock.now += 11  # lease ran out: w1 is presumed dead
        fresh = self.queue.claim(self.conn, "w2", limit=1)[0]
        self.assertEqual(fresh.application_id, stale.application_id)
        self.assertEqual(fresh.attempts, 2)
        self.assertFalse(self.queue.mark_done(self.conn, stale))  # zombie cannot ack
        self.assertEqual(self.queue.fail(self.conn, stale, "late"), "lost")
        self.assertTrue(self.queue.mark_done(self.conn, fresh))

    def test_unexpired_lease_is_not_reclaimed(self) -> None:
        self.queue.claim(self.conn, "w1", limit=3)
        self.clock.now += 9
        self.assertEqual(self.queue.claim(self.conn, "w2", limit=3), [])

    def test_fail_schedules_retry_with_backoff(self) -> None:
        job = self.queue.claim(self.conn, "w1", limit=1)[0]
        self.assertEqual(self.queue.fail(self.conn, job, "boom"), "ready")
        remaining = [j.application_id for j in self.queue.claim(self.conn, "w1", limit=3)]
        self.assertNotIn(job.application_id, remaining)  # still backing off (1 s)
        self.clock.now += 1.01
        retry = self.queue.claim(self.conn, "w1", limit=1)[0]
        self.assertEqual(retry.application_id, job.application_id)
        self.assertEqual(retry.attempts, 2)

    def test_dead_letter_after_max_attempts(self) -> None:
        target = self.ids[0]
        self.conn.execute("UPDATE queue SET status='done' WHERE application_id != ?", (target,))
        statuses = []
        for attempt in range(1, 4):
            job = self.queue.claim(self.conn, "w", limit=1)[0]
            statuses.append(self.queue.fail(self.conn, job, f"error {attempt}"))
            self.clock.now += 1000  # past any backoff
        self.assertEqual(statuses, ["ready", "ready", "dead"])
        row = self.conn.execute("SELECT status, attempts, last_error FROM queue WHERE application_id=?",
                                (target,)).fetchone()
        self.assertEqual((row["status"], row["attempts"], row["last_error"]), ("dead", 3, "error 3"))
        self.assertEqual(self.queue.claim(self.conn, "w", limit=3), [])

    def test_poison_message_that_crashes_workers_is_dead_lettered(self) -> None:
        self.conn.execute("UPDATE queue SET status='done' WHERE application_id != ?", (self.ids[0],))
        for _ in range(3):  # claim, then "crash" by never acking
            self.assertEqual(len(self.queue.claim(self.conn, "w", limit=1)), 1)
            self.clock.now += 11
        self.assertEqual(self.queue.claim(self.conn, "w", limit=1), [])
        self.assertEqual(WorkQueue.counts(self.conn)["dead"], 1)

    def test_backoff_is_exponential_capped_and_jittered(self) -> None:
        self.assertEqual([compute_backoff(a, 0.5, 4) for a in range(1, 6)], [0.5, 1, 2, 4, 4])
        rng = random.Random(3)
        for attempts in range(1, 10):
            self.assertTrue(0 <= compute_backoff(attempts, 0.5, 4, rng) <= min(4, 0.5 * 2 ** (attempts - 1)))


if __name__ == "__main__":
    unittest.main()
