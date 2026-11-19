import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from hvh.cli import main


class CliTests(unittest.TestCase):
    def test_seed_shortlist_audit_round_trip(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = str(Path(tmp) / "cli.db")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--db", db, "seed", "--apps", "600", "--jobs", "sde-l4-hvh", "--workers", "2"]), 0)
                self.assertEqual(main(["--db", db, "seed", "--apps", "600", "--jobs", "sde-l4-hvh", "--workers", "2"]), 0)
            self.assertIn("ingested 600 new applications", out.getvalue())
            self.assertIn("0 new applications (600 idempotent replays)", out.getvalue())

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["--db", db, "shortlist", "--job", "sde-l4-hvh", "--top", "5", "--json"])
            entries = json.loads(out.getvalue())
            self.assertEqual([e["rank"] for e in entries], [1, 2, 3, 4, 5])
            self.assertEqual(sorted((e["score"] for e in entries), reverse=True), [e["score"] for e in entries])

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--db", db, "audit", "--job", "sde-l4-hvh", "--stage", "screening", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["stage"], "screening")


if __name__ == "__main__":
    unittest.main()
