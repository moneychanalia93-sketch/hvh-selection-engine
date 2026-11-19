import random
import unittest
from dataclasses import replace

from hvh.jobs import JobConfigError, JobRequirements
from hvh.loadgen import generate
from hvh.models import Candidate
from hvh.scoring import ScoringFeatures, extract_features, score, score_candidate
from hvh.validation import validate_application
from tests.helpers import CATALOG

JOB = CATALOG.get("sde-l4-hvh")


def candidate(**overrides: object) -> Candidate:
    base = Candidate(name="A", email="a@example.com", skills=("python", "sql"), years_experience=4.0,
                     assessment_score=100.0)
    return replace(base, **overrides)


class ScoringTests(unittest.TestCase):
    def test_breakdown_adds_up_to_total(self) -> None:
        result = score_candidate(candidate(skills=("python", "sql", "aws"), years_experience=2, assessment_score=70), JOB)
        self.assertAlmostEqual(sum(f.points for f in result.factors), result.total, delta=0.02)
        self.assertEqual([f.factor for f in result.factors],
                         ["required_skills", "preferred_skills", "experience", "assessment"])
        self.assertTrue(all(f.explanation for f in result.factors))

    def test_perfect_candidate_scores_100(self) -> None:
        perfect = candidate(skills=tuple(JOB.required_skills + JOB.preferred_skills), years_experience=10)
        self.assertEqual(score_candidate(perfect, JOB).total, 100.0)

    def test_hand_computed_example(self) -> None:
        # required 2/2 * 35 = 35; preferred 1/5 * 20 = 4; experience 2/4 * 15 = 7.5; assessment 0.7 * 30 = 21
        result = score_candidate(candidate(skills=("python", "sql", "aws"), years_experience=2, assessment_score=70), JOB)
        self.assertEqual(result.total, 67.5)

    def test_missing_required_skill_is_a_knockout_with_reason(self) -> None:
        result = score_candidate(candidate(skills=("python",)), JOB)
        self.assertTrue(result.knocked_out)
        self.assertIn("sql", result.knockout_reason)

    def test_no_knockout_when_job_disables_it(self) -> None:
        support = CATALOG.get("support-engineer-l3")
        self.assertFalse(score_candidate(candidate(skills=()), support).knocked_out)

    def test_missing_assessment_scores_zero_and_says_so(self) -> None:
        result = score_candidate(candidate(assessment_score=None), JOB)
        factor = next(f for f in result.factors if f.factor == "assessment")
        self.assertEqual(factor.points, 0)
        self.assertIn("no assessment", factor.explanation)

    def test_experience_is_capped_and_flags_below_minimum(self) -> None:
        senior = score_candidate(candidate(years_experience=40), JOB)
        exact = score_candidate(candidate(years_experience=4), JOB)
        self.assertEqual(senior.total, exact.total)
        junior = next(f for f in score_candidate(candidate(years_experience=0.5), JOB).factors if f.factor == "experience")
        self.assertIn("below", junior.explanation)

    def test_more_of_a_good_thing_never_lowers_the_score(self) -> None:
        rng = random.Random(5)
        for _ in range(200):
            c = candidate(years_experience=rng.uniform(0, 10), assessment_score=rng.uniform(0, 90))
            better = replace(c, assessment_score=c.assessment_score + 10, years_experience=c.years_experience + 1)
            self.assertGreaterEqual(score_candidate(better, JOB).total, score_candidate(c, JOB).total)

    def test_scorer_only_sees_whitelisted_features(self) -> None:
        self.assertEqual(set(ScoringFeatures.__dataclass_fields__), {"skills", "years_experience", "assessment_score"})

    def test_job_weights_are_validated(self) -> None:
        good = {"job_id": "x", "title": "t", "required_skills": [], "preferred_skills": [],
                "min_years_experience": 0, "target_years_experience": 1,
                "weights": {"required_skills": 0.25, "preferred_skills": 0.25, "experience": 0.25, "assessment": 0.25}}
        self.assertEqual(JobRequirements.from_dict(good).job_id, "x")
        for bad_weights in ({"required_skills": 1.0}, {**good["weights"], "assessment": 0.5},
                            {**good["weights"], "assessment": -0.25, "experience": 0.75}):
            with self.assertRaises(JobConfigError):
                JobRequirements.from_dict({**good, "weights": bad_weights})


class ProtectedAttributeInvarianceTests(unittest.TestCase):
    """Changing protected fields must never change a score."""

    def test_randomized_protected_fields_never_change_score(self) -> None:
        rng = random.Random(2024)
        values = ["female", "male", "nonbinary", "under_40", "40_and_over", "group_a", "group_b", ""]
        for _, body in generate(300, seed=8, job_ids=CATALOG.ids()):
            baseline = validate_application(body, CATALOG)
            expected = score_candidate(baseline.candidate, CATALOG.get(baseline.job_id)).to_dict()
            for _ in range(5):
                body["candidate"]["name"] = "".join(rng.choice("abcdefghij ") for _ in range(12)).strip() or "x"
                body["candidate"]["email"] = f"{rng.randint(0, 10**9)}@example.org"
                body["self_identification"] = {k: rng.choice(values) for k in ("gender", "age_band", "ethnicity")}
                changed = validate_application(body, CATALOG)
                self.assertEqual(score_candidate(changed.candidate, CATALOG.get(changed.job_id)).to_dict(), expected)

    def test_features_ignore_name_and_email(self) -> None:
        a = extract_features(candidate(name="Alice", email="alice@example.com"))
        b = extract_features(candidate(name="Mohammed", email="m@example.com"))
        self.assertEqual(a, b)
        self.assertEqual(score(a, JOB), score(b, JOB))


if __name__ == "__main__":
    unittest.main()
