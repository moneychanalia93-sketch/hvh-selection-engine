# Completion report

Verification run on 2026-11-18 (America/New_York) from the repo root, on:
AMD Ryzen 5 PRO 8540U (12 logical CPUs), 31.3 GB RAM, Windows 11 (10.0.26200), Python 3.13.5, SQLite 3.49.1.
`python` was not on PATH, so every command used the full interpreter path:

```powershell
$py = "C:\Users\chanmone\AppData\Local\Programs\Python\Python313\python.exe"
```

Temporary databases were written to a scratch folder, not the repo. Every server process started was stopped,
and no project process was left running (checked with `Get-CimInstance Win32_Process`).

## 1. Unit tests

```powershell
& $py -m unittest discover -v
```

Result: `Ran 89 tests in 7.112s` / `OK`. An extra run with `& $py -X dev -m unittest discover` (dev mode shows
resource warnings) also passed with no warnings.

| Test module | Tests |
|---|---|
| test_api | 13 |
| test_cli | 1 |
| test_crash_recovery | 4 |
| test_fairness | 9 |
| test_idempotency | 8 |
| test_loadgen_and_metrics | 5 |
| test_ranking | 7 |
| test_ratelimit | 7 |
| test_scoring | 12 |
| test_state_machine | 6 |
| test_validation | 9 |
| test_work_queue | 8 |
| **Total** | **89** |

## 2. Seed, shortlist, audit (100,000 applications)

```powershell
& $py -m hvh --db $db seed --apps 100000 --fresh --workers 4
& $py -m hvh --db $db shortlist --job sde-l4-hvh --top 20
& $py -m hvh --db $db audit --job sde-l4-hvh --attribute gender --stage screening
& $py -m hvh --db $db shortlist --job sde-l4-hvh --top 3000 --commit
& $py -m hvh --db $db audit --job sde-l4-hvh --attribute ethnicity --stage shortlist
```

Output (trimmed to the key lines):

```
ingested 100000 new applications (0 idempotent replays) in 8.4s
queue drained in 18.4s with 4 workers: {'ready': 0, 'leased': 0, 'done': 100000, 'dead': 0}
Top 20 for job sde-l4-hvh:  rank 1 = 98.77 ... rank 20 = 91.52
gender, screening stage: female 52.2%, male 52.6%, nonbinary 55.2% (reference), not_disclosed 51.9%
  Result: no group falls below the four-fifths line.
Committed: top entries -> shortlisted, remaining assessed -> rejected.
ethnicity, shortlist stage: group_a 9.0% (reference), group_b 9.0%, group_c 8.8%, group_d 8.9%
  Result: no group falls below the four-fifths line.
```

## 3. Demo

```powershell
& $py -m hvh demo
```

Exit code 0, `Demo finished in 4.5s`. Key lines:

```
POST /applications              -> 201
POST again, same key (a retry)  -> 200 ... 'replayed': True  (same id, no duplicate)
POST same key, different body   -> 409
POST invalid body               -> 422 (5 errors listed)
15 fast POSTs from one client (bucket of 10) -> 10 x 201, 5 x 429
10 simulated crashes, 168 jobs needed more than one attempt
applications=1000 scores=1000 scored-twice=0  -> nothing lost, nothing doubled
refused: ... cannot go from rejected to shortlisted
refused: editing history -> events table is append-only
unbiased data:  '40_and_over' selected at 9.0% (87 of 962), 87% of the top rate -> not flagged
biased upstream assessment (-20): '40_and_over' selected at 3.1% (30 of 962), 26% of the top rate -> flagged
```

## 4. Live server over HTTP

```powershell
& $py scripts\smoke_http.py
```

This starts `python -m hvh serve --port 8781 --workers 2 --rate 5 --burst 8` as a separate process and calls every
endpoint. Exit code 0. Results:

```
POST /applications       -> 201 {"id": "18e9431d0100a8bcde3b1616baa5f8df", "state": "applied"}
POST retry, same key     -> 200 {"id": "18e9431d0100a8bcde3b1616baa5f8df", "replayed": true} | Idempotent-Replayed: true
POST invalid body        -> 422 {"error": "validation failed", "details": ["unknown job_id 'x'", "candidate must be an object"]}
POST without key         -> 400
GET /applications/{id}   -> 200 state "assessed", total 78.85, four factors with explanations, history applied -> screened -> assessed
GET unknown id           -> 404
12 rapid POSTs (burst 8) -> [201 x 8, 429 x 4]
GET /health              -> 200 {"status": "ok", "jobs": ["data-engineer-l4", "sde-l4-hvh", "support-engineer-l3"]}
GET /metrics             -> 200 counters, ingest latency percentiles, queue {"ready": 0, "leased": 0, "done": 9, "dead": 0}
server stopped
```

Found and fixed during this step: on Windows, Python's `SO_REUSEADDR` let the server bind port 8765 while
another program on this machine was already listening there, so requests reached the other program.
`ApiServer` now disables it on Windows; binding a taken port now fails with `WinError 10048`, and
`tests/test_api.py::PortTests` covers it.

## 5. Benchmark (100,000 applications)

```powershell
& $py -m hvh bench --apps 100000 --workers 1,2,4,8 --http-requests 5000 --http-clients 8 --out docs/benchmarks
```

Measured 2026-11-18 16:58:34 -0500. Written to `docs/benchmarks/results.md` and `results.json`.

| Phase | Workers | Items | Seconds | Items/sec | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| Ingestion, in-process (1 thread, 1 txn per app) | - | 100,000 | 105.79 | 945 | 0.638 | 1.34 | 10.846 |
| Ingestion, HTTP (8 keep-alive clients) | - | 5,000 | 8.11 | 616 | 1.16 | 17.372 | 137.553 |
| Scoring (batch of 32, claim to commit) | 1 | 100,000 | 29.18 | 3,427 | 5.196 | 10.712 | 59.997 |
| Scoring | 2 | 100,000 | 26.77 | 3,735 | 5.772 | 39.371 | 110.544 |
| Scoring | 4 | 100,000 | 25.93 | 3,857 | 5.609 | 69.485 | 276.595 |
| Scoring | 8 | 100,000 | 30.92 | 3,234 | 7.061 | 117.248 | 1042.02 |
| End to end (ingest + score concurrently) | 4 | 100,000 | 130.97 | 764 | 64.182 | 315.606 | 482.154 |

HTTP status counts: 5,000 x 201. Every scoring and end-to-end run verified done == scores == screened events ==
100,000.

An earlier full run of the same command, before switching to time-ordered ids and a 64 MiB page cache, measured
592/s ingestion and 1,566/s (1 worker) / 2,105/s (4 workers) scoring.

## 6. Not verified here

* CI on Python 3.11 and 3.12: the workflow is configured but was not run (no git or GitHub CLI on this machine).
  Locally only Python 3.13.5 was available.
* Linux and macOS: not run locally. `sysinfo.py` has Linux code paths that were not exercised.
* Benchmark numbers come from one laptop run and vary from run to run with power state and background load.
