"""Job requirements: what a role needs and how much each factor counts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple, Union

DEFAULT_JOBS_DIR = Path(__file__).parent / "data" / "jobs"

FACTORS: Tuple[str, ...] = ("required_skills", "preferred_skills", "experience", "assessment")


class JobConfigError(ValueError):
    """A job requirements file is malformed."""


def normalize_skill(skill: str) -> str:
    """Compare skills case- and space-insensitively: ' Python ' == 'python'."""
    return " ".join(skill.strip().lower().split())


@dataclass(frozen=True)
class JobRequirements:
    """A structured job requirements file, loaded and checked."""

    job_id: str
    title: str
    required_skills: Tuple[str, ...]
    preferred_skills: Tuple[str, ...]
    min_years_experience: float
    target_years_experience: float
    weights: Mapping[str, float]
    knockout_on_missing_required: bool

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "JobRequirements":
        try:
            weights = {str(k): float(v) for k, v in dict(data["weights"]).items()}  # type: ignore[call-overload]
            job = cls(
                job_id=str(data["job_id"]),
                title=str(data["title"]),
                required_skills=tuple(normalize_skill(s) for s in data["required_skills"]),  # type: ignore[union-attr]
                preferred_skills=tuple(normalize_skill(s) for s in data["preferred_skills"]),  # type: ignore[union-attr]
                min_years_experience=float(data["min_years_experience"]),  # type: ignore[arg-type]
                target_years_experience=float(data["target_years_experience"]),  # type: ignore[arg-type]
                weights=weights,
                knockout_on_missing_required=bool(data.get("knockout_on_missing_required", True)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise JobConfigError(f"invalid job requirements: {exc}") from exc
        job._check()
        return job

    def _check(self) -> None:
        if set(self.weights) != set(FACTORS):
            raise JobConfigError(f"{self.job_id}: weights must have exactly {FACTORS}")
        if any(w < 0 for w in self.weights.values()):
            raise JobConfigError(f"{self.job_id}: weights must be >= 0")
        if abs(sum(self.weights.values()) - 1.0) > 1e-6:
            raise JobConfigError(f"{self.job_id}: weights must sum to 1.0")
        if self.target_years_experience <= 0:
            raise JobConfigError(f"{self.job_id}: target_years_experience must be > 0")
        if self.min_years_experience < 0:
            raise JobConfigError(f"{self.job_id}: min_years_experience must be >= 0")


class JobCatalog:
    """All known jobs, looked up by id."""

    def __init__(self, jobs: Iterable[JobRequirements]) -> None:
        self._jobs: Dict[str, JobRequirements] = {}
        for job in jobs:
            if job.job_id in self._jobs:
                raise JobConfigError(f"duplicate job_id {job.job_id}")
            self._jobs[job.job_id] = job

    @classmethod
    def load(cls, directory: Optional[Union[str, Path]] = None) -> "JobCatalog":
        """Load every ``*.json`` file in ``directory`` (default: bundled jobs)."""
        folder = Path(directory) if directory else DEFAULT_JOBS_DIR
        jobs = []
        for path in sorted(folder.glob("*.json")):
            with path.open(encoding="utf-8") as handle:
                jobs.append(JobRequirements.from_dict(json.load(handle)))
        if not jobs:
            raise JobConfigError(f"no job files found in {folder}")
        return cls(jobs)

    def get(self, job_id: str) -> Optional[JobRequirements]:
        return self._jobs.get(job_id)

    def __contains__(self, job_id: object) -> bool:
        return job_id in self._jobs

    def ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._jobs))
