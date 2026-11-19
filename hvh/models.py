"""Plain data types shared across the package."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# Fields that describe who a person is rather than what they can do. The
# scorer must never see them. ``name`` and ``email`` are here too: a name can
# reveal gender or ethnicity, so it is treated as a protected proxy.
PROTECTED_CANDIDATE_FIELDS: Tuple[str, ...] = ("name", "email")


@dataclass(frozen=True)
class Candidate:
    """What a candidate submitted, after validation."""

    name: str
    email: str
    skills: Tuple[str, ...]
    years_experience: float
    assessment_score: Optional[float]

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "email": self.email,
            "skills": list(self.skills),
            "years_experience": self.years_experience,
            "assessment_score": self.assessment_score,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "Candidate":
        assessment = data.get("assessment_score")
        return cls(
            name=str(data["name"]),
            email=str(data["email"]),
            skills=tuple(str(s) for s in data["skills"]),  # type: ignore[union-attr]
            years_experience=float(data["years_experience"]),  # type: ignore[arg-type]
            assessment_score=None if assessment is None else float(assessment),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class ValidatedApplication:
    """A request body that passed validation."""

    job_id: str
    candidate: Candidate
    self_identification: Dict[str, str] = field(default_factory=dict)
