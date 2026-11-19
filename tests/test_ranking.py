import math
import random
import unittest
from typing import List, Tuple

from hvh import shortlist
from hvh.applications import submit_many
from hvh.loadgen import generate
from hvh.ranking import top_k
from hvh.worker import WorkerPool
from tests.helpers import CATALOG, DbTestCase


class CountingKey:
    """A key that counts how often it is compared."""

    comparisons = 0

    def __init__(self, value: float) -> None:
        self.value = value

    def __lt__(self, other: "CountingKey") -> bool:
        CountingKey.comparisons += 1
        return self.value < other.value

    def __gt__(self, other: "CountingKey") -> bool:
        CountingKey.comparisons += 1
        return self.value > other.value

    def __eq__(self, other: object) -> bool:
        CountingKey.comparisons += 1
        return isinstance(other, CountingKey) and self.value == other.value


class TopKTests(unittest.TestCase):
    def test_differential_against_sorted(self) -> None:
        """Randomized: heap top-K must equal the brute-force stable sort, ties included."""
        rng = random.Random(1234)
        for trial in range(500):
            n = rng.randint(0, 300)
            k = rng.randint(0, 40)
            # Few distinct scores on purpose, so ties are common.
            items: List[Tuple[int, float]] = [(i, float(rng.randint(0, 20))) for i in range(n)]
            expected = sorted(items, key=lambda x: x[1], reverse=True)[:k]
            self.assertEqual(top_k(items, k, key=lambda x: x[1]), expected, f"trial {trial}")

    def test_k_larger_than_n_and_k_zero(self) -> None:
        self.assertEqual(top_k([3, 1, 2], 10, key=lambda x: x), [3, 2, 1])
        self.assertEqual(top_k([3, 1, 2], 0, key=lambda x: x), [])
        self.assertEqual(top_k([], 5, key=lambda x: x), [])

    def test_ties_go_to_earlier_item(self) -> None:
        items = [("first", 5), ("second", 5), ("third", 5)]
        self.assertEqual([name for name, _ in top_k(items, 2, key=lambda x: x[1])], ["first", "second"])

    def test_works_on_a_one_pass_iterator(self) -> None:
        self.assertEqual(top_k(iter(range(1000)), 3, key=lambda x: x), [999, 998, 997])

    def test_comparisons_grow_like_n_log_k_not_n_log_n(self) -> None:
        rng = random.Random(9)
        n, k = 20_000, 10
        values = [CountingKey(rng.random()) for _ in range(n)]
        CountingKey.comparisons = 0
        top_k(values, k, key=lambda v: v)
        heap_cost = CountingKey.comparisons
        CountingKey.comparisons = 0
        sorted(values, reverse=True)
        sort_cost = CountingKey.comparisons
        # One compare per item against the heap root, plus O(log k) for the rare replacements.
        self.assertLess(heap_cost, n * (1 + 2 * math.log2(k)))
        self.assertLess(heap_cost, sort_cost / 3)

    def test_custom_key_on_lazy_generator(self) -> None:
        def gen():
            yield from range(10_000)

        # Closest to 5000 first; ties (4999 vs 5001) go to the earlier item.
        result = top_k(gen(), 5, key=lambda x: -abs(x - 5000))
        self.assertEqual(result, [5000, 4999, 5001, 4998, 5002])


class ShortlistTests(DbTestCase):
    def test_shortlist_matches_sql_order_by_and_commits_states(self) -> None:
        submit_many(self.conn, CATALOG, "t", generate(300, seed=4, job_ids=["sde-l4-hvh"]))
        WorkerPool(self.db_path, CATALOG, workers=2).run_until_drained(timeout=60)
        preview = shortlist.preview(self.conn, "sde-l4-hvh", 15)
        brute = self.conn.execute(
            "SELECT a.id FROM applications a JOIN scores s ON s.application_id = a.id"
            " WHERE a.job_id='sde-l4-hvh' AND a.state='assessed' ORDER BY s.score DESC, a.seq ASC LIMIT 15").fetchall()
        self.assertEqual([e.application_id for e in preview], [r["id"] for r in brute])
        self.assertEqual([e.rank for e in preview], list(range(1, 16)))

        committed = shortlist.commit(self.conn, "sde-l4-hvh", 15)
        self.assertEqual(committed, preview)
        states = dict(self.conn.execute("SELECT state, COUNT(*) FROM applications GROUP BY state").fetchall())
        self.assertEqual(states.get("shortlisted"), 15)
        self.assertNotIn("assessed", states)


if __name__ == "__main__":
    unittest.main()
