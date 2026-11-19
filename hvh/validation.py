"""Validate an incoming application body at the trust boundary.

The rules are strict on purpose: unknown fields are rejected, numbers must be
real numbers (``true`` is not ``1``), and every list and string has a size cap
so one request cannot exhaust memory.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

from hvh.jobs import JobCatalog, normalize_skill
from hvh.models import Candidate, ValidatedApplication

MAX_NAME_LENGTH = 200
MAX_EMAIL_LENGTH = 254
MAX_SKILLS = 50
MAX_SKILL_LENGTH = 50
MAX_YEARS = 60.0
MAX_SELF_ID_FIELDS = 10
MAX_SELF_ID_VALUE_LENGTH = 50

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TOP_LEVEL_KEYS = {"job_id", "candidate", "self_identification"}
_CANDIDATE_KEYS = {"name", "email", "skills", "years_experience", "assessment_score"}


class ValidationError(ValueError):
    """The body is not a valid application. ``errors`` lists every problem."""

    def __init__(self, errors: List[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_candidate(raw: Any, errors: List[str]) -> Optional[Candidate]:
    if not isinstance(raw, dict):
        errors.append("candidate must be an object")
        return None
    unknown = set(raw) - _CANDIDATE_KEYS
    if unknown:
        errors.append(f"candidate has unknown fields: {sorted(unknown)}")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LENGTH:
        errors.append(f"candidate.name must be a non-empty string of at most {MAX_NAME_LENGTH} chars")

    email = raw.get("email")
    if not isinstance(email, str) or len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(email):
        errors.append("candidate.email must be a valid email address")

    skills = raw.get("skills")
    clean_skills: List[str] = []
    if not isinstance(skills, list) or len(skills) > MAX_SKILLS:
        errors.append(f"candidate.skills must be a list of at most {MAX_SKILLS} strings")
    else:
        for skill in skills:
            if not isinstance(skill, str) or not skill.strip() or len(skill) > MAX_SKILL_LENGTH:
                errors.append(f"each skill must be a non-empty string of at most {MAX_SKILL_LENGTH} chars")
                break
            clean_skills.append(normalize_skill(skill))

    years = raw.get("years_experience")
    if not _is_number(years) or not 0 <= years <= MAX_YEARS:
        errors.append(f"candidate.years_experience must be a number from 0 to {MAX_YEARS:g}")

    assessment = raw.get("assessment_score")
    if assessment is not None and (not _is_number(assessment) or not 0 <= assessment <= 100):
        errors.append("candidate.assessment_score must be null or a number from 0 to 100")

    if errors:
        return None
    # dict.fromkeys keeps order and drops duplicates.
    return Candidate(
        name=name.strip(),  # type: ignore[union-attr]
        email=email,  # type: ignore[arg-type]
        skills=tuple(dict.fromkeys(clean_skills)),
        years_experience=float(years),  # type: ignore[arg-type]
        assessment_score=None if assessment is None else float(assessment),
    )


def _check_self_id(raw: Any, errors: List[str]) -> Dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_SELF_ID_FIELDS:
        errors.append(f"self_identification must be an object with at most {MAX_SELF_ID_FIELDS} fields")
        return {}
    clean: Dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or len(value) > MAX_SELF_ID_VALUE_LENGTH or len(key) > MAX_SELF_ID_VALUE_LENGTH:
            errors.append(f"self_identification values must be strings of at most {MAX_SELF_ID_VALUE_LENGTH} chars")
            return {}
        clean[key] = value
    return clean


def validate_application(body: Any, catalog: JobCatalog) -> ValidatedApplication:
    """Return a clean application or raise :class:`ValidationError`."""
    if not isinstance(body, dict):
        raise ValidationError(["body must be a JSON object"])
    errors: List[str] = []
    unknown = set(body) - _TOP_LEVEL_KEYS
    if unknown:
        errors.append(f"unknown fields: {sorted(unknown)}")

    job_id = body.get("job_id")
    if not isinstance(job_id, str):
        errors.append("job_id must be a string")
    elif job_id not in catalog:
        errors.append(f"unknown job_id {job_id!r}")

    candidate = _check_candidate(body.get("candidate"), errors)
    self_id = _check_self_id(body.get("self_identification"), errors)
    if errors or candidate is None:
        raise ValidationError(errors)
    return ValidatedApplication(job_id=job_id, candidate=candidate, self_identification=self_id)  # type: ignore[arg-type]
