import threading
import unittest

from hvh.ratelimit import RateLimiter, TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TokenBucketTests(unittest.TestCase):
    def test_allows_burst_then_blocks(self) -> None:
        bucket = TokenBucket(capacity=5, refill_per_second=1, clock=FakeClock())
        self.assertEqual([bucket.try_acquire() for _ in range(7)], [True] * 5 + [False] * 2)

    def test_refills_over_time_up_to_capacity(self) -> None:
        clock = FakeClock()
        bucket = TokenBucket(capacity=2, refill_per_second=2, clock=clock)
        bucket.try_acquire()
        bucket.try_acquire()
        self.assertFalse(bucket.try_acquire())
        clock.now += 0.5  # 0.5 s * 2 tokens/s = 1 token
        self.assertTrue(bucket.try_acquire())
        self.assertFalse(bucket.try_acquire())
        clock.now += 100  # long idle: capped at capacity, not 200 tokens
        self.assertEqual(sum(bucket.try_acquire() for _ in range(5)), 2)

    def test_retry_after(self) -> None:
        clock = FakeClock()
        bucket = TokenBucket(capacity=1, refill_per_second=4, clock=clock)
        self.assertEqual(bucket.retry_after(), 0.0)
        bucket.try_acquire()
        self.assertAlmostEqual(bucket.retry_after(), 0.25)

    def test_rejects_bad_config(self) -> None:
        with self.assertRaises(ValueError):
            TokenBucket(capacity=0, refill_per_second=1)

    def test_thread_safe_never_over_grants(self) -> None:
        bucket = TokenBucket(capacity=100, refill_per_second=1e-9, clock=FakeClock())
        granted = []
        lock = threading.Lock()

        def hammer() -> None:
            mine = sum(bucket.try_acquire() for _ in range(100))
            with lock:
                granted.append(mine)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(granted), 100)


class RateLimiterTests(unittest.TestCase):
    def test_clients_are_isolated(self) -> None:
        limiter = RateLimiter(capacity=2, refill_per_second=1, clock=FakeClock())
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))

    def test_bucket_count_is_bounded(self) -> None:
        limiter = RateLimiter(capacity=1, refill_per_second=1, max_clients=3, clock=FakeClock())
        for client in ["a", "b", "c", "d", "e"]:
            limiter.allow(client)
        self.assertEqual(len(limiter), 3)


if __name__ == "__main__":
    unittest.main()
