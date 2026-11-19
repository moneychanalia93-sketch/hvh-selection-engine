import json
import unittest

from hvh import fairness, shortlist
from hvh.applications import submit_many
from hvh.fairness import NOT_DISCLOSED, four_fifths
from hvh.loadgen import Skew, generate
from hvh.worker import WorkerPool
from tests.helpers import CATALOG, DbTestCase


class FourFifthsMathTests(unittest.TestCase):
    def test_rates_and_impact_ratios(self) -> None:
        report = four_fifths({"a": (50, 100), "b": (30, 100)})
        groups = {g.group: g for g in report.groups}
        self.assertEqual(report.reference_group, "a")
        self.assertAlmostEqual(groups["a"].selection_rate, 0.5)
        self.assertAlmostEqual(groups["b"].impact_ratio, 0.6)
        self.assertTrue(groups["b"].flagged)
        self.assertFalse(groups["a"].flagged)
        self.assertEqual([g.group for g in report.violations], ["b"])

    def test_exactly_eighty_percent_is_not_flagged(self) -> None:
        report = four_fifths({"a": (50, 100), "b": (40, 100)})
        self.assertFalse(report.violations)
        self.assertTrue(four_fifths({"a": (50, 100), "b": (39, 100)}).violations)

    def test_reference_is_highest_rate_not_largest_group(self) -> None:
        report = four_fifths({"big": (100, 1000), "small": (20, 100)})
        self.assertEqual(report.reference_group, "small")
        self.assertEqual([g.group for g in report.violations], ["big"])

    def test_nobody_selected(self) -> None:
        report = four_fifths({"a": (0, 10), "b": (0, 10)})
        self.assertIsNone(report.reference_group)
        self.assertFalse(report.violations)
        self.assertIn("No one was selected", report.explain())

    def test_not_disclosed_is_never_reference_or_flagged(self) -> None:
        report = four_fifths({"a": (10, 100), NOT_DISCLOSED: (90, 100), "b": (1, 100)})
        self.assertEqual(report.reference_group, "a")
        flagged = {g.group for g in report.violations}
        self.assertEqual(flagged, {"b"})

    def test_small_groups_marked_low_confidence(self) -> None:
        report = four_fifths({"a": (5, 10), "b": (50, 100)}, min_group_size=30)
        self.assertTrue(next(g for g in report.groups if g.group == "a").low_confidence)
        self.assertIn("small group", report.explain())

    def test_plain_english_explanation(self) -> None:
        text = four_fifths({"a": (50, 100), "b": (30, 100)}, attribute="gender").explain()
        self.assertIn("'b' was selected at 30.0% (30 of 100), which is 60% of the top rate", text)
        self.assertIn("1 group(s) flagged", text)
        self.assertIn("not legal advice", text)
        json.dumps(four_fifths({"a": (1, 2)}).to_dict())  # serializable


class AuditFromDatabaseTests(DbTestCase):
    def run_pipeline(self, skew: object) -> None:
        submit_many(self.conn, CATALOG, "t", generate(1500, seed=21, job_ids=["sde-l4-hvh"], skew=skew))
        WorkerPool(self.db_path, CATALOG, workers=2).run_until_drained(timeout=60)
        shortlist.commit(self.conn, "sde-l4-hvh", 150)

    def test_biased_upstream_signal_is_flagged(self) -> None:
        self.run_pipeline(Skew("age_band", "40_and_over", -25))
        report = fairness.audit(self.conn, "sde-l4-hvh", "age_band", "shortlist")
        self.assertIn("40_and_over", [g.group for g in report.violations])

    def test_counts_cover_every_applicant(self) -> None:
        self.run_pipeline(None)
        counts = fairness.counts_from_db(self.conn, "sde-l4-hvh", "gender", "shortlist")
        self.assertEqual(sum(n for _, n in counts.values()), 1500)
        self.assertEqual(sum(s for s, _ in counts.values()), 150)
        with self.assertRaises(ValueError):
            fairness.counts_from_db(self.conn, "sde-l4-hvh", "gender", "interview")


if __name__ == "__main__":
    unittest.main()
