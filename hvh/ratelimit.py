"""Token-bucket rate limiting, one bucket per client.

A bucket holds up to ``capacity`` tokens and refills at ``refill_per_second``.
Each request spends one token. A full bucket allows a short burst; after that
the client is held to the steady refill rate.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable


class TokenBucket:
    """A single bucket. Thread-safe."""

    def __init__(self, capacity: float, refill_per_second: float, clock: Callable[[], float] = time.monotonic) -> None:
        if capacity <= 0 or refill_per_second <= 0:
            raise ValueError("capacity and refill_per_second must be > 0")
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._clock = clock
        self._tokens = capacity
        self._updated = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
        self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Spend ``tokens`` if available. Never blocks."""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def retry_after(self, tokens: float = 1.0) -> float:
        """Seconds until ``tokens`` will be available."""
        with self._lock:
            self._refill()
            missing = max(0.0, tokens - self._tokens)
            return missing / self.refill_per_second


class RateLimiter:
    """Buckets keyed by client id.

    At most ``max_clients`` buckets are kept; the least recently used one is
    dropped first, so a flood of fake client ids cannot grow memory forever.
    A dropped client simply starts again with a full bucket.
    """

    def __init__(
        self,
        capacity: float,
        refill_per_second: float,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.max_clients = max_clients
        self._clock = clock
        self._buckets: "OrderedDict[str, TokenBucket]" = OrderedDict()
        self._lock = threading.Lock()

    def _bucket(self, client_id: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = TokenBucket(self.capacity, self.refill_per_second, self._clock)
                self._buckets[client_id] = bucket
                if len(self._buckets) > self.max_clients:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(client_id)
            return bucket

    def allow(self, client_id: str) -> bool:
        return self._bucket(client_id).try_acquire()

    def retry_after(self, client_id: str) -> float:
        return self._bucket(client_id).retry_after()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)
