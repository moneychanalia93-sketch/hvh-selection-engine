"""``python -m hvh demo``: the whole story, end to end, in under a minute.

Uses a throwaway database in a temp folder, so it never touches ``hvh.db``.
"""

from __future__ import annotations

import http.client
import json
import random
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from hvh import fairness, shortlist, state_machine
from hvh.api import ApiServer, AppContext
from hvh.applications import submit_many
from hvh.db import connection, init_db, transaction
from hvh.jobs import JobCatalog
from hvh.loadgen import Skew, generate
from hvh.metrics import Metrics
from hvh.ratelimit import RateLimiter
from hvh.state_machine import InvalidTransition, State
from hvh.work_queue import QueueSettings, WorkQueue
from hvh.worker import SimulatedCrash, WorkerPool

JOB = "sde-l4-hvh"


def _step(title: str) -> None:
    print(f"\n=== {title} ===")


def _request(port: int, method: str, path: str, body: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body) if body is not None else None
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, body=payload, headers=all_headers)
    response = conn.getresponse()
    data = json.loads(response.read() or b"{}")
    conn.close()
    return response.status, data


def _api_tour(db: Path, catalog: JobCatalog) -> None:
    _step("1. HTTP API: idempotency, validation, rate limiting")
    metrics = Metrics()
    server = ApiServer(AppContext(db, catalog, RateLimiter(capacity=10, refill_per_second=2), metrics), port=0)
    pool = WorkerPool(db, catalog, workers=2, metrics=metrics)
    pool.start()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    port = server.port
    try:
        _, body = next(iter(generate(1, seed=2024, job_ids=[JOB])))
        headers = {"Idempotency-Key": "demo-key-1", "X-Client-Id": "demo-client"}
        status, first = _request(port, "POST", "/applications", body, headers)
        print(f"POST /applications              -> {status} {first}")
        status, again = _request(port, "POST", "/applications", body, headers)
        print(f"POST again, same key (a retry)  -> {status} {again}  (same id, no duplicate)")
        changed = json.loads(json.dumps(body))
        changed["candidate"]["years_experience"] = 30
        status, conflict = _request(port, "POST", "/applications", changed, headers)
        print(f"POST same key, different body   -> {status} {conflict['error']}")
        bad = {"job_id": "nope", "candidate": {"name": "", "skills": "python"}}
        status, invalid = _request(port, "POST", "/applications", bad, {"Idempotency-Key": "demo-bad", "X-Client-Id": "demo-client"})
        print(f"POST invalid body               -> {status} {invalid['details']}")

        app_id = first["id"]
        app: Dict[str, Any] = {}
        for _ in range(50):
            _, app = _request(port, "GET", f"/applications/{app_id}")
            if app["state"] != "applied":
                break
            time.sleep(0.1)
        print(f"GET /applications/{app_id[:8]}... -> state={app['state']}, score={app['score']['total']}")
        for factor in app["score"]["factors"]:
            print(f"    {factor['factor']:<17} {factor['points']:>6.2f} pts  ({factor['explanation']})")
        print("    history: " + " -> ".join(e["to_state"] for e in app["history"]))

        codes = [
            _request(port, "POST", "/applications", b, {"Idempotency-Key": k, "X-Client-Id": "demo-burst"})[0]
            for k, b in generate(15, seed=99, job_ids=[JOB])
        ]
        print(f"15 fast POSTs from one client (bucket of 10) -> {codes.count(201)} x 201, {codes.count(429)} x 429")
        status, health = _request(port, "GET", "/health")
        print(f"GET /health  -> {status} {health['status']}")
        status, snap = _request(port, "GET", "/metrics")
        print(f"GET /metrics -> {status} counters={snap['counters']} queue={snap['queue']}")
    finally:
        server.shutdown()
        server.server_close()
        pool.stop()


def _crash_recovery(db: Path, catalog: JobCatalog) -> None:
    _step("2. Crash recovery: 20% of batches crash mid-job")
    with connection(db) as conn:
        submit_many(conn, catalog, "demo-crash", generate(1000, seed=5, job_ids=["data-engineer-l4"]))
    rng = random.Random(1)
    lock = threading.Lock()

    def crash_sometimes(stage: str, job: Any) -> None:
        with lock:
            roll = rng.random()
        if stage == "before_commit" and roll < 0.2:
            raise SimulatedCrash()

    metrics = Metrics()
    queue = WorkQueue(QueueSettings(lease_seconds=0.3, max_attempts=10))
    pool = WorkerPool(db, catalog, workers=4, queue=queue, metrics=metrics, fault_hook=crash_sometimes)
    started = time.perf_counter()
    pool.run_until_drained()
    with connection(db) as conn:
        apps = conn.execute("SELECT COUNT(*) n FROM applications WHERE job_id='data-engineer-l4'").fetchone()["n"]
        scores = conn.execute("SELECT COUNT(*) n FROM scores WHERE job_id='data-engineer-l4'").fetchone()["n"]
        dupes = conn.execute(
            "SELECT COUNT(*) n FROM (SELECT application_id FROM events WHERE to_state='screened'"
            " GROUP BY application_id HAVING COUNT(*) > 1)").fetchone()["n"]
        retried = conn.execute("SELECT COUNT(*) n FROM queue WHERE attempts > 1").fetchone()["n"]
    crashes = metrics.snapshot()["counters"].get("simulated_crashes", 0)  # type: ignore[union-attr]
    print(f"{crashes} simulated crashes, {retried} jobs needed more than one attempt "
          f"({time.perf_counter() - started:.1f}s)")
    print(f"applications={apps} scores={scores} scored-twice={dupes}  -> nothing lost, nothing doubled")


def _pipeline(db: Path, catalog: JobCatalog, skew: Optional[Skew], top: int) -> None:
    with connection(db) as conn:
        submit_many(conn, catalog, "demo-seed", generate(4000, seed=11, job_ids=[JOB], skew=skew))
    started = time.perf_counter()
    WorkerPool(db, catalog, workers=4).run_until_drained()
    with connection(db) as conn:
        counts = WorkQueue.counts(conn)
        print(f"4,000 applications scored in {time.perf_counter() - started:.1f}s; queue={counts}")
        entries = shortlist.commit(conn, JOB, top)
        print(f"shortlisted top {len(entries)} (best score {entries[0].score}, cut-off {entries[-1].score})")


def run_demo() -> int:
    total = time.perf_counter()
    catalog = JobCatalog.load()
    work = Path(tempfile.mkdtemp(prefix="hvh-demo-"))
    try:
        db = work / "demo.db"
        init_db(db)
        _api_tour(db, catalog)
        _crash_recovery(db, catalog)

        _step("3. Scoring and top-K shortlist (heap, O(n log k))")
        fair_db = work / "fair.db"
        init_db(fair_db)
        _pipeline(fair_db, catalog, skew=None, top=400)

        _step("4. State machine refuses illegal moves")
        with connection(fair_db) as conn:
            rejected = conn.execute("SELECT id FROM applications WHERE state='rejected' LIMIT 1").fetchone()["id"]
            try:
                with transaction(conn):
                    state_machine.transition(conn, rejected, State.SHORTLISTED, "trying to sneak in", time.time())
            except InvalidTransition as exc:
                print(f"refused: {exc}")
            try:
                conn.execute("UPDATE events SET reason='edited' WHERE application_id=?", (rejected,))
            except Exception as exc:  # sqlite3.IntegrityError from the trigger
                print(f"refused: editing history -> {exc}")

        _step("5. Fairness audit, unbiased synthetic data")
        with connection(fair_db) as conn:
            print(fairness.audit(conn, JOB, "age_band", "shortlist").explain())

        _step("6. Fairness audit, upstream assessment biased against age_band=40_and_over by -20 points")
        skew_db = work / "skew.db"
        init_db(skew_db)
        _pipeline(skew_db, catalog, skew=Skew("age_band", "40_and_over", -20), top=400)
        with connection(skew_db) as conn:
            print(fairness.audit(conn, JOB, "age_band", "shortlist").explain())
        print("The scorer never saw age. The bias came in through the assessment score,")
        print("and the outcome audit caught it. Blind inputs are not enough; audit outcomes.")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\nDemo finished in {time.perf_counter() - total:.1f}s")
    return 0
