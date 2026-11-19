"""Top-K selection with a bounded min-heap.

Complexity for ``n`` items and ``k`` slots:

* Time ``O(n log k)``: each item costs one comparison with the heap's smallest
  entry, and at most one ``heapreplace`` (``O(log k)``).
* Memory ``O(k)``: only the current best ``k`` are kept, so the input can be a
  database cursor streaming a million rows.

Sorting everything would be ``O(n log n)`` time and ``O(n)`` memory. For
k = 20 and n = 100,000 that is about log2(20) ~ 4.3 versus log2(100000) ~ 16.6
per item.

Ties: equal keys keep input order (earlier wins), exactly like Python's stable
``sorted(..., reverse=True)``. The heap stores ``(key, -index, item)``, so
among equal keys a later index is "smaller" and gets evicted first, and
``item`` itself is never compared.
"""

from __future__ import annotations

import heapq
from typing import Any, Callable, Iterable, List, Tuple, TypeVar

T = TypeVar("T")


def top_k(items: Iterable[T], k: int, key: Callable[[T], Any]) -> List[T]:
    """The ``k`` items with the largest ``key``, best first."""
    if k <= 0:
        return []
    heap: List[Tuple[Any, int, T]] = []
    for index, item in enumerate(items):
        entry = (key(item), -index, item)
        if len(heap) < k:
            heapq.heappush(heap, entry)
        elif entry[:2] > heap[0][:2]:
            heapq.heapreplace(heap, entry)
    heap.sort(key=lambda e: e[:2], reverse=True)
    return [item for _, _, item in heap]
