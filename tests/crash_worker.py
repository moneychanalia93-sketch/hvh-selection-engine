"""Helper process for test_crash_recovery: claim jobs, then hang until killed.

Usage: python tests/crash_worker.py <db_path> <how_many>
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hvh.db import connect  # noqa: E402
from hvh.work_queue import QueueSettings, WorkQueue  # noqa: E402

if __name__ == "__main__":
    conn = connect(sys.argv[1])
    jobs = WorkQueue(QueueSettings(lease_seconds=1.0)).claim(conn, "doomed-process", int(sys.argv[2]))
    print(f"CLAIMED {len(jobs)}", flush=True)
    time.sleep(120)  # the test kills us long before this ends
