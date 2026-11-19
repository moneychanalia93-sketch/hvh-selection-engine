import unittest

from hvh.loadgen import Skew, generate
from hvh.metrics import Metrics, percentile, summarize
from hvh.validation import validate_application
from tests.helpers import CATALOG


class LoadgenTests(unittest.TestCase):
    def test_same_seed_same_data_different_seed_different_data(self) -> None:
        self.assertEqual(list(generate(200, seed=1)), list(generate(200, seed=1)))
        self.assertNotEqual(list(generate(200, seed=1)), list(generate(200, seed=2)))

    def test_every_generated_body_is_valid(self) -> None:
        for _, body in generate(2000, seed=3, job_ids=CATALOG.ids()):
            validate_application(body, CATALOG)

    def test_skew_only_moves_the_target_group(self) -> None:
        skew = Skew.parse("gender=female:-10")
        plain = list(generate(500, seed=4))
        skewed = list(generate(500, seed=4, skew=skew))
        for (_, a), (_, b) in zip(plain, skewed):
            before, after = a["candidate"]["assessment_score"], b["candidate"]["assessment_score"]
            if a["self_identification"].get("gender") == "female" and before is not None:
                self.assertAlmostEqual(after, max(0.0, before - 10), delta=0.11)
            else:
                self.assertEqual(before, after)
        with self.assertRaises(ValueError):
            Skew.parse("nonsense")


class MetricsTests(unittest.TestCase):
    def test_nearest_rank_percentile(self) -> None:
        data = list(range(1, 101))  # 1..100
        self.assertEqual(percentile(data, 50), 50)
        self.assertEqual(percentile(data, 95), 95)
        self.assertEqual(percentile(data, 99), 99)
        self.assertEqual(percentile(data, 100), 100)
        self.assertEqual(percentile([7.0], 99), 7.0)
        self.assertEqual(summarize([])["count"], 0)

    def test_window_is_bounded(self) -> None:
        metrics = Metrics(window=10)
        for i in range(100):
            metrics.observe("op", i / 1000)
        metrics.incr("hits", 3)
        snap = metrics.snapshot()
        self.assertEqual(snap["latency_ms"]["op"]["count"], 10)
        self.assertEqual(snap["latency_ms"]["op"]["max"], 99.0)
        self.assertEqual(snap["counters"]["hits"], 3)


if __name__ == "__main__":
    unittest.main()
