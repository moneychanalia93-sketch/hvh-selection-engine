"""Four-fifths (80%) adverse-impact audit.

The rule of thumb from the US Uniform Guidelines on Employee Selection
Procedures: if a group's selection rate is less than 80% of the rate of the
group selected most often, that is a sign of possible adverse impact worth
investigating.

Important limits (also stated in the README):

* This is a screening heuristic, not legal advice and not a statistical test.
  Small groups give noisy rates, so groups below ``min_group_size`` are
  marked as low-confidence.
* All demographic data in this project is synthetic.
* Removing protected fields from the scorer does not guarantee fair outcomes:
  a job-relevant input (for example an assessment) can itself differ between
  groups. That is why outcomes are audited, not just inputs.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

FOUR_FIFTHS = 0.8
NOT_DISCLOSED = "not_disclosed"
STAGES = ("shortlist", "screening")


@dataclass(frozen=True)
class GroupResult:
    group: str
    applicants: int
    selected: int
    selection_rate: float
    impact_ratio: Optional[float]  # rate / highest rate; None when nobody was selected
    flagged: bool
    low_confidence: bool


@dataclass(frozen=True)
class AuditReport:
    attribute: str
    stage: str
    threshold: float
    reference_group: Optional[str]
    groups: Tuple[GroupResult, ...]

    @property
    def violations(self) -> List[GroupResult]:
        return [g for g in self.groups if g.flagged]

    def explain(self) -> str:
        """The result in plain English."""
        lines = [f"Adverse-impact check on '{self.attribute}' at the {self.stage} stage."]
        if self.reference_group is None:
            lines.append("No one was selected, so selection rates cannot be compared.")
            return "\n".join(lines)
        ref = next(g for g in self.groups if g.group == self.reference_group)
        lines.append(
            f"The highest selection rate is '{ref.group}': {ref.selection_rate:.1%} "
            f"({ref.selected} of {ref.applicants}). Every group is compared to it."
        )
        for g in self.groups:
            if g.group == ref.group or g.impact_ratio is None:
                continue
            verdict = (
                "not a demographic group, so never flagged"
                if g.group == NOT_DISCLOSED
                else f"BELOW the {self.threshold:.0%} line: possible adverse impact, investigate"
                if g.flagged
                else f"at or above the {self.threshold:.0%} line"
            )
            note = " (small group: treat with caution)" if g.low_confidence else ""
            lines.append(
                f"'{g.group}' was selected at {g.selection_rate:.1%} ({g.selected} of {g.applicants}), "
                f"which is {g.impact_ratio:.0%} of the top rate: {verdict}{note}."
            )
        lines.append(
            f"Result: {len(self.violations)} group(s) flagged."
            if self.violations
            else "Result: no group falls below the four-fifths line."
        )
        lines.append("This is a screening heuristic on synthetic data, not legal advice.")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, object]:
        return {
            "attribute": self.attribute,
            "stage": self.stage,
            "threshold": self.threshold,
            "reference_group": self.reference_group,
            "groups": [g.__dict__ for g in self.groups],
            "violations": [g.group for g in self.violations],
        }


def four_fifths(
    counts: Mapping[str, Tuple[int, int]],
    attribute: str = "group",
    stage: str = "shortlist",
    threshold: float = FOUR_FIFTHS,
    min_group_size: int = 30,
) -> AuditReport:
    """Apply the rule to ``{group: (selected, applicants)}``.

    The ``not_disclosed`` group is reported but never used as the reference
    and never flagged: it is not a demographic group.
    """
    rates = {g: (s / n if n else 0.0) for g, (s, n) in counts.items()}
    comparable = {g: r for g, r in rates.items() if g != NOT_DISCLOSED and counts[g][1] > 0}
    reference = max(comparable, key=lambda g: (comparable[g], g)) if comparable else None
    top_rate = comparable[reference] if reference else 0.0
    if top_rate == 0:
        reference = None
    groups = []
    for g in sorted(counts):
        selected, applicants = counts[g]
        ratio = rates[g] / top_rate if top_rate > 0 else None
        groups.append(
            GroupResult(
                group=g,
                applicants=applicants,
                selected=selected,
                selection_rate=rates[g],
                impact_ratio=ratio,
                # A tiny epsilon so a ratio of exactly 0.8 is not flagged by float error.
                flagged=g != NOT_DISCLOSED and ratio is not None and ratio < threshold - 1e-12,
                low_confidence=applicants < min_group_size,
            )
        )
    return AuditReport(attribute, stage, threshold, reference, tuple(groups))


def counts_from_db(conn: sqlite3.Connection, job_id: str, attribute: str, stage: str) -> Dict[str, Tuple[int, int]]:
    """Selected and total applicants per group for one job.

    ``shortlist``: selected means the application ended up ``shortlisted``.
    ``screening``: selected means it passed the required-skills knockout.
    """
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    rows = conn.execute(
        "SELECT a.state, s.knocked_out, i.data_json FROM applications a"
        " LEFT JOIN scores s ON s.application_id = a.id"
        " LEFT JOIN self_identification i ON i.application_id = a.id"
        " WHERE a.job_id = ?",
        (job_id,),
    )
    tallies: Dict[str, List[int]] = {}
    for row in rows:
        data = json.loads(row["data_json"]) if row["data_json"] else {}
        group = data.get(attribute) or NOT_DISCLOSED
        if stage == "shortlist":
            selected = row["state"] == "shortlisted"
        else:
            selected = row["knocked_out"] == 0
        tally = tallies.setdefault(group, [0, 0])
        tally[0] += int(selected)
        tally[1] += 1
    return {g: (s, n) for g, (s, n) in tallies.items()}


def audit(conn: sqlite3.Connection, job_id: str, attribute: str, stage: str = "shortlist") -> AuditReport:
    return four_fifths(counts_from_db(conn, job_id, attribute, stage), attribute=attribute, stage=stage)
