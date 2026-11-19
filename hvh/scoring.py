"""Explainable, rule-based scoring.

Fairness by construction: :func:`extract_features` copies ONLY job-relevant
fields (skills, years of experience, assessment result) into
:class:`ScoringFeatures`. :func:`score` takes that object, so it has no way to
read a name, an email or self-identification data. The test suite also checks
the end result: changing protected fields never changes a score.

Every score comes with a per-factor breakdown so a recruiter or candidate can
see exactly why it came out the way it did.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, FrozenSet, Optional, Tuple

from hvh.jobs import JobRequirements
from hvh.models import Candidate

SCORER_VERSION = "1.0.0"


@dataclass(frozen=True)
class ScoringFeatures:
    """The only inputs the scorer is allowed to see."""

    skills: FrozenSet[str]
    years_experience: float
    assessment_score: Optional[float]


@dataclass(frozen=True)
class FactorScore:
    factor: str
    weight: float
    raw: float  # 0.0 to 1.0: how well this factor is met
    points: float  # weight * raw * 100
    explanation: str


@dataclass(frozen=True)
class ScoreResult:
    total: float  # 0 to 100
    knocked_out: bool
    knockout_reason: Optional[str]
    factors: Tuple[FactorScore, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "total": self.total,
            "knocked_out": self.knocked_out,
            "knockout_reason": self.knockout_reason,
            "factors": [asdict(f) for f in self.factors],
            "scorer_version": SCORER_VERSION,
        }


def extract_features(candidate: Candidate) -> ScoringFeatures:
    """Whitelist the job-relevant fields. Everything else is left behind."""
    return ScoringFeatures(
        skills=frozenset(candidate.skills),
        years_experience=candidate.years_experience,
        assessment_score=candidate.assessment_score,
    )


def _fraction(matched: int, wanted: int) -> float:
    return 1.0 if wanted == 0 else matched / wanted


def score(features: ScoringFeatures, job: JobRequirements) -> ScoreResult:
    """Score one candidate against one job. Pure function: same input, same output."""
    required = set(job.required_skills)
    preferred = set(job.preferred_skills)
    have_required = sorted(required & features.skills)
    missing_required = sorted(required - features.skills)
    have_preferred = sorted(preferred & features.skills)

    raw_required = _fraction(len(have_required), len(required))
    raw_preferred = _fraction(len(have_preferred), len(preferred))
    raw_experience = min(features.years_experience / job.target_years_experience, 1.0)
    raw_assessment = 0.0 if features.assessment_score is None else features.assessment_score / 100.0

    experience_note = f"{features.years_experience:g} of {job.target_years_experience:g} target years"
    if features.years_experience < job.min_years_experience:
        experience_note += f" (below the {job.min_years_experience:g}-year minimum)"
    assessment_note = (
        "no assessment result on file"
        if features.assessment_score is None
        else f"assessment result {features.assessment_score:g}/100"
    )

    parts = [
        ("required_skills", raw_required,
         f"has {len(have_required)}/{len(required)} required skills"
         + (f"; missing {', '.join(missing_required)}" if missing_required else "")),
        ("preferred_skills", raw_preferred,
         f"has {len(have_preferred)}/{len(preferred)} preferred skills"
         + (f": {', '.join(have_preferred)}" if have_preferred else "")),
        ("experience", raw_experience, experience_note),
        ("assessment", raw_assessment, assessment_note),
    ]
    factors = tuple(
        FactorScore(
            factor=name,
            weight=job.weights[name],
            raw=round(raw, 4),
            points=round(job.weights[name] * raw * 100, 2),
            explanation=note,
        )
        for name, raw, note in parts
    )
    knocked_out = job.knockout_on_missing_required and bool(missing_required)
    return ScoreResult(
        total=round(sum(job.weights[name] * raw * 100 for name, raw, _ in parts), 2),
        knocked_out=knocked_out,
        knockout_reason=f"missing required skills: {', '.join(missing_required)}" if knocked_out else None,
        factors=factors,
    )


def score_candidate(candidate: Candidate, job: JobRequirements) -> ScoreResult:
    """Convenience wrapper: extract the allowed features, then score."""
    return score(extract_features(candidate), job)
