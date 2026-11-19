"""Synthetic, reproducible application data.

Everything here is made up. The same ``seed`` always produces exactly the same
applications, so benchmark and audit results can be reproduced.

``skew`` lets a demo simulate a biased *upstream* signal: it shifts the
assessment score for one demographic group. The scorer never sees the group,
yet the shortlist can still end up unequal. The fairness audit is what
catches it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

SKILL_POOL: Tuple[str, ...] = (
    "python", "sql", "java", "aws", "distributed systems", "testing", "concurrency",
    "etl", "spark", "data modeling", "airflow", "linux", "networking", "communication",
    "javascript", "go", "docker", "kubernetes", "c++", "excel",
)
# Probability that a candidate lists each skill.
SKILL_PROBABILITY: Dict[str, float] = {
    "python": 0.75, "sql": 0.7, "linux": 0.6, "etl": 0.35, "communication": 0.5,
}
DEFAULT_SKILL_PROBABILITY = 0.25

GENDERS = (("female", 0.45), ("male", 0.45), ("nonbinary", 0.05), ("not_disclosed", 0.05))
AGE_BANDS = (("under_40", 0.7), ("40_and_over", 0.25), ("not_disclosed", 0.05))
ETHNICITIES = (("group_a", 0.4), ("group_b", 0.25), ("group_c", 0.2), ("group_d", 0.1), ("not_disclosed", 0.05))


@dataclass(frozen=True)
class Skew:
    """Shift the assessment score of one group, e.g. ``age_band=40_and_over:-20``."""

    attribute: str
    group: str
    assessment_delta: float

    @classmethod
    def parse(cls, text: str) -> "Skew":
        try:
            left, delta = text.rsplit(":", 1)
            attribute, group = left.split("=", 1)
            return cls(attribute.strip(), group.strip(), float(delta))
        except ValueError as exc:
            raise ValueError(f"skew must look like attribute=group:delta, got {text!r}") from exc


def _pick(rng: random.Random, options: Sequence[Tuple[str, float]]) -> str:
    return rng.choices([o for o, _ in options], weights=[w for _, w in options])[0]


def generate(
    n: int, seed: int = 42, job_ids: Sequence[str] = ("sde-l4-hvh",), skew: Optional[Skew] = None
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield ``(idempotency_key, body)`` pairs shaped like real API requests."""
    rng = random.Random(seed)
    for i in range(n):
        skills = [s for s in SKILL_POOL if rng.random() < SKILL_PROBABILITY.get(s, DEFAULT_SKILL_PROBABILITY)]
        years = round(min(40.0, rng.expovariate(1 / 4)), 1)
        assessment: Optional[float] = None
        if rng.random() > 0.1:  # 10% have no assessment yet
            assessment = max(0.0, min(100.0, rng.gauss(65, 15)))
        self_id = {
            "gender": _pick(rng, GENDERS),
            "age_band": _pick(rng, AGE_BANDS),
            "ethnicity": _pick(rng, ETHNICITIES),
        }
        if skew and assessment is not None and self_id.get(skew.attribute) == skew.group:
            assessment = max(0.0, min(100.0, assessment + skew.assessment_delta))
        body = {
            "job_id": job_ids[i % len(job_ids)],
            "candidate": {
                "name": f"Synthetic Candidate {seed}-{i:06d}",
                "email": f"candidate.{seed}.{i}@example.com",
                "skills": skills,
                "years_experience": years,
                "assessment_score": None if assessment is None else round(assessment, 1),
            },
            "self_identification": {k: v for k, v in self_id.items() if v != "not_disclosed"},
        }
        yield f"seed-{seed}-{i}", body
