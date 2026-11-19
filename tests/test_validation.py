import unittest

from hvh.validation import ValidationError, validate_application
from tests.helpers import CATALOG, sample_body


class ValidationTests(unittest.TestCase):
    def assertInvalid(self, body: object, fragment: str) -> None:
        with self.assertRaises(ValidationError) as ctx:
            validate_application(body, CATALOG)
        self.assertTrue(any(fragment in e for e in ctx.exception.errors), ctx.exception.errors)

    def test_valid_body_is_normalized(self) -> None:
        app = validate_application(sample_body(skills=[" Python ", "python", "SQL"]), CATALOG)
        self.assertEqual(app.job_id, "sde-l4-hvh")
        self.assertEqual(app.candidate.skills, ("python", "sql"))  # trimmed, lower-cased, de-duplicated
        self.assertEqual(app.self_identification["gender"], "female")

    def test_body_must_be_object(self) -> None:
        self.assertInvalid(["not", "an", "object"], "JSON object")

    def test_unknown_job_rejected(self) -> None:
        body = sample_body()
        body["job_id"] = "no-such-job"
        self.assertInvalid(body, "unknown job_id")

    def test_unknown_fields_rejected(self) -> None:
        body = sample_body()
        body["salary"] = 1
        self.assertInvalid(body, "unknown fields")
        self.assertInvalid(sample_body(age=40), "candidate has unknown fields")

    def test_years_out_of_range_or_wrong_type(self) -> None:
        self.assertInvalid(sample_body(years_experience=-1), "years_experience")
        self.assertInvalid(sample_body(years_experience=61), "years_experience")
        self.assertInvalid(sample_body(years_experience="5"), "years_experience")
        self.assertInvalid(sample_body(years_experience=True), "years_experience")  # bool is not a number here
        self.assertInvalid(sample_body(years_experience=float("nan")), "years_experience")

    def test_assessment_range_and_null(self) -> None:
        self.assertIsNone(validate_application(sample_body(assessment_score=None), CATALOG).candidate.assessment_score)
        self.assertInvalid(sample_body(assessment_score=101), "assessment_score")

    def test_skill_limits(self) -> None:
        self.assertInvalid(sample_body(skills=[f"s{i}" for i in range(51)]), "skills")
        self.assertInvalid(sample_body(skills=["x" * 51]), "skill")
        self.assertInvalid(sample_body(skills=[1, 2]), "skill")

    def test_email_and_name(self) -> None:
        self.assertInvalid(sample_body(email="not-an-email"), "email")
        self.assertInvalid(sample_body(name="   "), "name")

    def test_reports_all_errors_at_once(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            validate_application(sample_body(email="bad", years_experience=-5, assessment_score=500), CATALOG)
        self.assertEqual(len(ctx.exception.errors), 3)


if __name__ == "__main__":
    unittest.main()
