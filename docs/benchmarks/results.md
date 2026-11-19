# Benchmark results

Measured 2026-11-18 16:58:34 -0500 on: AMD Ryzen 5 PRO 8540U w/ Radeon 740M Graphics, 12 logical CPUs, 31.3 GB RAM, Windows-11-10.0.26200-SP0, Python 3.13.5, SQLite 3.49.1.
Synthetic applications: 100,000 (seed 7).

| Phase | Workers | Items | Seconds | Items/sec | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| Ingestion, in-process (1 thread, 1 txn per app) | - | 100,000 | 105.79 | 945 | 0.638 | 1.34 | 10.846 |
| Ingestion, HTTP (8 keep-alive clients) | - | 5,000 | 8.11 | 616 | 1.16 | 17.372 | 137.553 |
| Scoring (batch of 32, claim to commit) | 1 | 100,000 | 29.18 | 3,427 | 5.196 | 10.712 | 59.997 |
| Scoring (batch of 32, claim to commit) | 2 | 100,000 | 26.77 | 3,735 | 5.772 | 39.371 | 110.544 |
| Scoring (batch of 32, claim to commit) | 4 | 100,000 | 25.93 | 3,857 | 5.609 | 69.485 | 276.595 |
| Scoring (batch of 32, claim to commit) | 8 | 100,000 | 30.92 | 3,234 | 7.061 | 117.248 | 1042.02 |
| End to end (ingest + score concurrently, created to scored) | 4 | 100,000 | 130.97 | 764 | 64.182 | 315.606 | 482.154 |

HTTP status counts: {'201': 5000}.
Every scoring and end-to-end run was verified: done == scores == screened events == applications.
