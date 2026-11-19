"""Thread-safe counters and latency percentiles."""

from __future__ import annotations

import math
import threading
from collections import deque
from typing import Deque, Dict, Iterable, List, Sequence


def percentile(sorted_values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile of an already sorted list (``p`` from 0 to 100).

    Nearest-rank always returns a value that was really observed, which is the
    honest choice for latency reporting.
    """
    if not sorted_values:
        return 0.0
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def summarize(values: Iterable[float]) -> Dict[str, float]:
    """count, mean, p50, p95, p99 and max of some values."""
    data: List[float] = sorted(values)
    if not data:
        return {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "count": len(data),
        "mean": sum(data) / len(data),
        "p50": percentile(data, 50),
        "p95": percentile(data, 95),
        "p99": percentile(data, 99),
        "max": data[-1],
    }


class Metrics:
    """Counters plus a bounded window of recent latencies per operation.

    The window is a ``deque(maxlen=...)`` so a long-running server keeps a
    fixed amount of memory no matter how many requests it serves.
    """

    def __init__(self, window: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._window = window
        self._counters: Dict[str, int] = {}
        self._latencies: Dict[str, Deque[float]] = {}

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            if name not in self._latencies:
                self._latencies[name] = deque(maxlen=self._window)
            self._latencies[name].append(seconds)

    def snapshot(self) -> Dict[str, object]:
        """Counters and latency summaries in milliseconds."""
        with self._lock:
            counters = dict(self._counters)
            latencies = {name: list(values) for name, values in self._latencies.items()}
        return {
            "counters": counters,
            "latency_ms": {
                name: {k: (v if k == "count" else round(v * 1000, 3)) for k, v in summarize(values).items()}
                for name, values in latencies.items()
            },
        }
