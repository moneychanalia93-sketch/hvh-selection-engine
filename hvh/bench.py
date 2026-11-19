"""Benchmark: ingestion, HTTP, scoring at 1/2/4/8 workers, and end to end.

Every number this prints is measured on the machine it runs on. Results are
written to ``<out>/results.json`` and ``<out>/results.md``.

Phases:

1. **Ingestion (in-process).** ``applications.submit`` once per application,
   one transaction each: the exact code path the API uses, minus HTTP.
2. **Ingestion (HTTP).** The API runs in a separate process; client threads
   POST over keep-alive connections on 127.0.0.1.
3. **Scoring sweep.** The queue from phase 1 is copied, then drained by a
   worker pool of each size.
4. **End to end.** Ingestion and scoring run at the same time; latency is
   ``scored_at - created_at`` for every application.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from hvh.applications import submit
from hvh.db import connection, init_db
from hvh.jobs import JobCatalog
from hvh.loadgen import generate
from hvh.metrics import Metrics, summarize
from hvh.sysinfo import machine_info
from hvh.work_queue import WorkQueue
from hvh.worker import WorkerPool

Body = Tuple[str, Dict[str, Any]]


def _ms(stats: Dict[str, float]) -> Dict[str, float]:
    return {k: (v if k == "count" else round(v * 1000, 3)) for k, v in stats.items()}


def _checkpoint(db: Path) -> None:
    with connection(db) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def bench_ingest(db: Path, catalog: JobCatalog, bodies: Sequence[Body]) -> Dict[str, Any]:
    init_db(db)
    latencies: List[float] = []
    with connection(db) as conn:
        started = time.perf_counter()
        for key, body in bodies:
            t0 = time.perf_counter()
            submit(conn, catalog, "bench", key, body)
            latencies.append(time.perf_counter() - t0)
        elapsed = time.perf_counter() - started
    _checkpoint(db)
    return {"applications": len(bodies), "seconds": round(elapsed, 2),
            "per_sec": round(len(bodies) / elapsed), "latency_ms": _ms(summarize(latencies))}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def bench_http(db: Path, bodies: Sequence[Body], clients: int) -> Dict[str, Any]:
    init_db(db)
    port = _free_port()
    package_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=package_root)
    cmd = [sys.executable, "-m", "hvh", "--db", str(db), "serve", "--port", str(port),
           "--workers", "0", "--rate", "1000000", "--burst", "1000000"]
    proc = subprocess.Popen(cmd, cwd=package_root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        while True:
            try:
                probe = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                probe.request("GET", "/health")
                if probe.getresponse().status == 200:
                    probe.close()
                    break
            except OSError:
                if time.time() > deadline:
                    raise RuntimeError("API server did not start")
                time.sleep(0.2)

        latencies: List[float] = []
        statuses: Dict[int, int] = {}
        lock = threading.Lock()

        def client(index: int) -> None:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            mine: List[float] = []
            codes: Dict[int, int] = {}
            for key, body in bodies[index::clients]:
                payload = json.dumps(body)
                t0 = time.perf_counter()
                conn.request("POST", "/applications", body=payload, headers={
                    "Content-Type": "application/json", "Idempotency-Key": key, "X-Client-Id": f"bench-{index}"})
                response = conn.getresponse()
                response.read()
                mine.append(time.perf_counter() - t0)
                codes[response.status] = codes.get(response.status, 0) + 1
            conn.close()
            with lock:
                latencies.extend(mine)
                for code, count in codes.items():
                    statuses[code] = statuses.get(code, 0) + count

        threads = [threading.Thread(target=client, args=(i,)) for i in range(clients)]
        started = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - started
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    return {"requests": len(bodies), "clients": clients, "seconds": round(elapsed, 2),
            "per_sec": round(len(bodies) / elapsed), "status_counts": {str(k): v for k, v in statuses.items()},
            "latency_ms": _ms(summarize(latencies))}


def _verify(db: Path, expected: int) -> Dict[str, int]:
    with connection(db) as conn:
        counts = WorkQueue.counts(conn)
        scores = conn.execute("SELECT COUNT(*) AS n FROM scores").fetchone()["n"]
        screened = conn.execute("SELECT COUNT(*) AS n FROM events WHERE to_state = 'screened'").fetchone()["n"]
    if counts["done"] != expected or scores != expected or screened != expected:
        raise AssertionError(f"lost or duplicate work: {counts}, scores={scores}, screened events={screened}")
    return {"done": counts["done"], "scores": scores, "dead": counts["dead"]}


def bench_scoring(template: Path, work_dir: Path, catalog: JobCatalog, workers: int, n: int) -> Dict[str, Any]:
    db = work_dir / f"score_w{workers}.db"
    shutil.copyfile(template, db)
    metrics = Metrics(window=n)
    pool = WorkerPool(db, catalog, workers=workers, batch_size=32, metrics=metrics)
    started = time.perf_counter()
    pool.run_until_drained()
    elapsed = time.perf_counter() - started
    check = _verify(db, n)
    latency = metrics.snapshot()["latency_ms"]["score_batch_item"]  # type: ignore[index]
    return {"workers": workers, "seconds": round(elapsed, 2), "per_sec": round(n / elapsed),
            "batch_latency_ms": latency, "verified": check}


def bench_end_to_end(db: Path, catalog: JobCatalog, bodies: Sequence[Body], workers: int) -> Dict[str, Any]:
    init_db(db)
    pool = WorkerPool(db, catalog, workers=workers, batch_size=32)
    pool.start()
    started = time.perf_counter()
    with connection(db) as conn:
        for key, body in bodies:
            submit(conn, catalog, "bench", key, body)
        while WorkQueue.pending(conn):
            time.sleep(0.05)
    elapsed = time.perf_counter() - started
    pool.stop()
    check = _verify(db, len(bodies))
    with connection(db) as conn:
        rows = conn.execute(
            "SELECT s.scored_at - a.created_at AS lag FROM scores s JOIN applications a ON a.id = s.application_id"
        ).fetchall()
    return {"applications": len(bodies), "workers": workers, "seconds": round(elapsed, 2),
            "per_sec": round(len(bodies) / elapsed), "latency_ms": _ms(summarize(r["lag"] for r in rows)),
            "verified": check}


def _markdown(results: Dict[str, Any]) -> str:
    m = results["machine"]
    lines = [
        "# Benchmark results",
        "",
        f"Measured {results['measured_at']} on: {m['cpu']}, {m['logical_cpus']} logical CPUs, "
        f"{m['ram_gb']} GB RAM, {m['os']}, Python {m['python']}, SQLite {m['sqlite']}.",
        f"Synthetic applications: {results['applications']:,} (seed {results['seed']}).",
        "",
        "| Phase | Workers | Items | Seconds | Items/sec | p50 ms | p95 ms | p99 ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    ing = results["ingest_in_process"]
    lat = ing["latency_ms"]
    lines.append(f"| Ingestion, in-process (1 thread, 1 txn per app) | - | {ing['applications']:,} | {ing['seconds']} "
                 f"| {ing['per_sec']:,} | {lat['p50']} | {lat['p95']} | {lat['p99']} |")
    http_ = results["ingest_http"]
    lat = http_["latency_ms"]
    lines.append(f"| Ingestion, HTTP ({http_['clients']} keep-alive clients) | - | {http_['requests']:,} | "
                 f"{http_['seconds']} | {http_['per_sec']:,} | {lat['p50']} | {lat['p95']} | {lat['p99']} |")
    for run in results["scoring"]:
        lat = run["batch_latency_ms"]
        lines.append(f"| Scoring (batch of 32, claim to commit) | {run['workers']} | {results['applications']:,} | "
                     f"{run['seconds']} | {run['per_sec']:,} | {lat['p50']} | {lat['p95']} | {lat['p99']} |")
    e2e = results["end_to_end"]
    lat = e2e["latency_ms"]
    lines.append(f"| End to end (ingest + score concurrently, created to scored) | {e2e['workers']} | "
                 f"{e2e['applications']:,} | {e2e['seconds']} | {e2e['per_sec']:,} | {lat['p50']} | {lat['p95']} | "
                 f"{lat['p99']} |")
    lines += ["", f"HTTP status counts: {http_['status_counts']}.",
              "Every scoring and end-to-end run was verified: done == scores == screened events == applications."]
    return "\n".join(lines) + "\n"


def run_benchmark(n: int, worker_counts: Sequence[int], http_requests: int, http_clients: int, out: Path,
                  seed: int = 7) -> Dict[str, Any]:
    catalog = JobCatalog.load()
    work_dir = Path(tempfile.mkdtemp(prefix="hvh-bench-"))
    try:
        print(f"generating {n:,} synthetic applications (seed {seed}) ...", flush=True)
        bodies = list(generate(n, seed, catalog.ids()))
        results: Dict[str, Any] = {"measured_at": time.strftime("%Y-%m-%d %H:%M:%S %z"), "machine": machine_info(),
                                   "applications": n, "seed": seed}
        print("phase 1: in-process ingestion ...", flush=True)
        template = work_dir / "template.db"
        results["ingest_in_process"] = bench_ingest(template, catalog, bodies)
        print(f"  {results['ingest_in_process']['per_sec']:,} apps/sec", flush=True)
        print(f"phase 2: HTTP ingestion ({http_requests:,} requests) ...", flush=True)
        results["ingest_http"] = bench_http(work_dir / "http.db", bodies[:http_requests], http_clients)
        print(f"  {results['ingest_http']['per_sec']:,} req/sec", flush=True)
        results["scoring"] = []
        for workers in worker_counts:
            print(f"phase 3: scoring with {workers} worker(s) ...", flush=True)
            run = bench_scoring(template, work_dir, catalog, workers, n)
            results["scoring"].append(run)
            print(f"  {run['per_sec']:,} apps/sec", flush=True)
        best = max(results["scoring"], key=lambda r: r["per_sec"])["workers"]
        print(f"phase 4: end to end with {best} workers (fastest in phase 3) ...", flush=True)
        results["end_to_end"] = bench_end_to_end(work_dir / "e2e.db", catalog, bodies, best)
        print(f"  {results['end_to_end']['per_sec']:,} apps/sec", flush=True)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    markdown = _markdown(results)
    (out / "results.md").write_text(markdown, encoding="utf-8")
    print()
    print(markdown)
    return results
