"""Command line entry point: ``python -m hvh <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

from hvh import fairness, shortlist
from hvh.applications import submit_many
from hvh.db import connection, init_db
from hvh.jobs import JobCatalog
from hvh.loadgen import Skew, generate
from hvh.metrics import Metrics
from hvh.ratelimit import RateLimiter
from hvh.work_queue import WorkQueue
from hvh.worker import WorkerPool

SEED_CLIENT_ID = "seed-loader"


def _remove_db(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


def cmd_serve(args: argparse.Namespace) -> int:
    from hvh.api import ApiServer, AppContext

    init_db(args.db)
    catalog = JobCatalog.load(args.jobs_dir)
    metrics = Metrics()
    ctx = AppContext(args.db, catalog, RateLimiter(args.burst, args.rate), metrics)
    server = ApiServer(ctx, args.port)
    pool = None
    if args.workers > 0:
        pool = WorkerPool(args.db, catalog, workers=args.workers, metrics=metrics)
        pool.start()
    print(f"hvh API listening on http://127.0.0.1:{server.port} (workers={args.workers}); Ctrl+C to stop", flush=True)
    def _on_sigterm(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if pool:
            pool.stop()
    print("stopped", flush=True)
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    if args.fresh:
        _remove_db(args.db)
    init_db(args.db)
    catalog = JobCatalog.load(args.jobs_dir)
    job_ids = args.jobs.split(",") if args.jobs else list(catalog.ids())
    skew = Skew.parse(args.skew) if args.skew else None
    started = time.perf_counter()
    with connection(args.db) as conn:
        created, replayed = submit_many(conn, catalog, SEED_CLIENT_ID, generate(args.apps, args.seed, job_ids, skew))
    print(f"ingested {created} new applications ({replayed} idempotent replays) in {time.perf_counter() - started:.1f}s")
    if not args.no_score:
        return cmd_work(args)
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    init_db(args.db)
    catalog = JobCatalog.load(args.jobs_dir)
    started = time.perf_counter()
    pool = WorkerPool(args.db, catalog, workers=args.workers)
    pool.run_until_drained()
    with connection(args.db) as conn:
        counts = WorkQueue.counts(conn)
    print(f"queue drained in {time.perf_counter() - started:.1f}s with {args.workers} workers: {counts}")
    return 0


def cmd_shortlist(args: argparse.Namespace) -> int:
    with connection(args.db) as conn:
        entries = (shortlist.commit if args.commit else shortlist.preview)(conn, args.job, args.top)
        if args.json:
            print(json.dumps([e.__dict__ for e in entries], indent=2))
            return 0
        verb = "Shortlisted" if args.commit else "Top"
        print(f"{verb} {len(entries)} for job {args.job}:")
        print(f"{'rank':>4}  {'score':>6}  application_id")
        for e in entries:
            print(f"{e.rank:>4}  {e.score:>6.2f}  {e.application_id}")
        if args.commit:
            print("Committed: top entries -> shortlisted, remaining assessed -> rejected.")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    with connection(args.db) as conn:
        report = fairness.audit(conn, args.job, args.attribute, args.stage)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"{'group':<15} {'applicants':>10} {'selected':>9} {'rate':>7} {'ratio':>6}  flag")
        for g in report.groups:
            ratio = "-" if g.impact_ratio is None else f"{g.impact_ratio:.2f}"
            flag = "FLAG" if g.flagged else ("low-n" if g.low_confidence else "")
            print(f"{g.group:<15} {g.applicants:>10} {g.selected:>9} {g.selection_rate:>7.1%} {ratio:>6}  {flag}")
        print()
        print(report.explain())
    return 1 if (args.fail_on_violation and report.violations) else 0


def cmd_bench(args: argparse.Namespace) -> int:
    from hvh.bench import run_benchmark

    workers = [int(w) for w in args.workers.split(",")]
    run_benchmark(args.apps, workers, args.http_requests, args.http_clients, Path(args.out), args.seed)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from hvh.demo import run_demo

    return run_demo()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hvh", description="High-volume candidate selection pipeline")
    parser.add_argument("--db", default="hvh.db", help="SQLite database file (default: hvh.db)")
    parser.add_argument("--jobs-dir", default=None, help="folder of job requirement JSON files")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the HTTP API and a worker pool")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--workers", type=int, default=4, help="scoring threads (0 = API only)")
    p.add_argument("--rate", type=float, default=50.0, help="requests per second per client")
    p.add_argument("--burst", type=float, default=100.0, help="token bucket size per client")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("seed", help="load synthetic applications, then score them")
    p.add_argument("--apps", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--jobs", default="", help="comma-separated job ids (default: all)")
    p.add_argument("--skew", default="", help="bias one group's assessment, e.g. age_band=40_and_over:-20")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--fresh", action="store_true", help="delete the database first")
    p.add_argument("--no-score", action="store_true", help="only ingest, leave jobs queued")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("work", help="drain the queue with a worker pool, then exit")
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(func=cmd_work)

    p = sub.add_parser("shortlist", help="top-K shortlist for a job")
    p.add_argument("--job", required=True)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--commit", action="store_true", help="apply: shortlist top K, reject the rest")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_shortlist)

    p = sub.add_parser("audit", help="four-fifths adverse-impact audit")
    p.add_argument("--job", required=True)
    p.add_argument("--attribute", default="gender", help="gender, age_band or ethnicity")
    p.add_argument("--stage", choices=fairness.STAGES, default="shortlist")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fail-on-violation", action="store_true", help="exit code 1 if any group is flagged")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("bench", help="measure throughput and latency")
    p.add_argument("--apps", type=int, default=100_000)
    p.add_argument("--workers", default="1,2,4,8")
    p.add_argument("--http-requests", type=int, default=5_000)
    p.add_argument("--http-clients", type=int, default=8)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", default="docs/benchmarks")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("demo", help="run the whole story end to end (< 1 minute)")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
