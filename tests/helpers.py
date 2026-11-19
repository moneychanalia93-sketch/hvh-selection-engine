"""Shared test fixtures: a temp database and sample request bodies."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from hvh.db import connect, init_db
from hvh.jobs import JobCatalog

CATALOG = JobCatalog.load()

SAMPLE_BODY: Dict[str, Any] = {
    "job_id": "sde-l4-hvh",
    "candidate": {
        "name": "Test Candidate",
        "email": "test.candidate@example.com",
        "skills": ["Python", "SQL", "AWS"],
        "years_experience": 3,
        "assessment_score": 80,
    },
    "self_identification": {"gender": "female", "age_band": "under_40"},
}


def sample_body(**candidate_overrides: Any) -> Dict[str, Any]:
    body = copy.deepcopy(SAMPLE_BODY)
    body["candidate"].update(candidate_overrides)
    return body


class DbTestCase(unittest.TestCase):
    """Gives each test a fresh database file and an open connection."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()
