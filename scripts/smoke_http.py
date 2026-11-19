"""Manual smoke test: start ``python -m hvh serve`` as a real process and hit every endpoint.

Usage (from the repo root):
    python scripts/smoke_http.py [--port 8781]

It uses a throwaway database in a temp folder and stops the server at the end.
"""

from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8781)
    port = parser.parse_args().port
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = str(Path(tmp.name) / "smoke.db")
    proc = subprocess.Popen(
        [sys.executable, "-m", "hvh", "--db", db, "serve", "--port", str(port), "--workers", "2",
         "--rate", "5", "--burst", "8"],
        cwd=REPO, stdout=subprocess.PIPE, text=True)
    print("server:", proc.stdout.readline().strip())  # type: ignore[union-attr]

    def call(method: str, path: str, body: Any = None,
             headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], str]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path, body=None if body is None else json.dumps(body),
                     headers={"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        data = response.read().decode()
        conn.close()
        return response.status, dict(response.getheaders()), data

    try:
        body = {"job_id": "sde-l4-hvh",
                "candidate": {"name": "Synthetic Person", "email": "synthetic.person@example.com",
                              "skills": ["Python", "SQL", "AWS", "Testing"], "years_experience": 3,
                              "assessment_score": 82},
                "self_identification": {"gender": "female"}}
        key = {"Idempotency-Key": "manual-1", "X-Client-Id": "manual"}
        status, _, data = call("POST", "/applications", body, key)
        print("POST /applications       ->", status, data)
        app_id = json.loads(data)["id"]
        status, headers, data = call("POST", "/applications", body, key)
        print("POST retry, same key     ->", status, data, "| Idempotent-Replayed:", headers.get("Idempotent-Replayed"))
        status, _, data = call("POST", "/applications", {"job_id": "x"},
                               {"Idempotency-Key": "bad-1", "X-Client-Id": "manual"})
        print("POST invalid body        ->", status, data)
        status, _, data = call("POST", "/applications", body)
        print("POST without key         ->", status, data)
        time.sleep(1)  # let a worker score it
        status, _, data = call("GET", f"/applications/{app_id}")
        print("GET /applications/{id}   ->", status, data)
        status, _, data = call("GET", "/applications/" + "f" * 32)
        print("GET unknown id           ->", status, data)
        codes = [call("POST", "/applications", body, {"Idempotency-Key": f"burst-{i}", "X-Client-Id": "burst"})[0]
                 for i in range(12)]
        print("12 rapid POSTs (burst 8) ->", codes)
        status, _, data = call("GET", "/health")
        print("GET /health              ->", status, data)
        status, _, data = call("GET", "/metrics")
        print("GET /metrics             ->", status, data)
    finally:
        proc.terminate()
        proc.wait(10)
        proc.stdout.close()  # type: ignore[union-attr]
        print("server stopped")
        tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
