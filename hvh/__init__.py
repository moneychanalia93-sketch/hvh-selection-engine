"""hvh: a high-volume automated candidate selection pipeline.

The package is pure Python standard library. The main pieces are:

* ``api``          - HTTP ingestion API (idempotent, rate limited)
* ``work_queue``   - durable SQLite work queue with leases and dead-lettering
* ``worker``       - thread pool that drains the queue and scores applications
* ``scoring``      - explainable, rule-based scoring against a job's requirements
* ``state_machine``- enforced candidate lifecycle with an append-only history
* ``ranking``      - heap-based top-K selection
* ``fairness``     - four-fifths (80%) adverse-impact audit
"""

__version__ = "1.0.0"
