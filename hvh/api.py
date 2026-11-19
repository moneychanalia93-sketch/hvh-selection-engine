"""HTTP ingestion API built on ``http.server``.

Routes:

* ``POST /applications``      submit (needs an ``Idempotency-Key`` header)
* ``GET  /applications/{id}`` state, score breakdown and history
* ``GET  /metrics``           counters, latency percentiles, queue depth
* ``GET  /health``            liveness plus a database check

``ThreadingHTTPServer`` runs one thread per connection. Each connection opens
its own SQLite connection in :meth:`Handler.handle` and closes it when done.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from hvh import applications
from hvh.applications import IdempotencyConflict
from hvh.db import connect
from hvh.jobs import JobCatalog
from hvh.metrics import Metrics
from hvh.ratelimit import RateLimiter
from hvh.validation import ValidationError
from hvh.work_queue import WorkQueue

MAX_BODY_BYTES = 64 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_APP_PATH = re.compile(r"^/applications/([0-9a-f]{32})$")


@dataclass
class AppContext:
    """Shared, thread-safe state handed to every request handler."""

    db_path: Union[str, Path]
    catalog: JobCatalog
    limiter: RateLimiter
    metrics: Metrics


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: clients can reuse a connection
    server: "ApiServer"
    conn: sqlite3.Connection

    def handle(self) -> None:
        self.conn = connect(self.server.ctx.db_path)
        try:
            super().handle()
        finally:
            self.conn.close()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        """Silence per-request logging; /metrics is the observability surface."""

    # ---- helpers -------------------------------------------------------
    def _send(self, status: int, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.server.ctx.metrics.incr(f"http_{status}")

    def _error(self, status: int, message: str, **extra: Any) -> None:
        self._send(status, {"error": message, **extra})

    def _client_id(self) -> str:
        # shortcut: trusts the X-Client-Id header; a real service would take the
        # caller's identity from authentication (e.g. a signed token) instead.
        header = self.headers.get("X-Client-Id", "")
        return header if _SAFE_ID.match(header) else f"ip:{self.client_address[0]}"

    def _read_json(self) -> Tuple[bool, Any]:
        """Return ``(ok, body)``; on failure an error response was already sent."""
        length_header = self.headers.get("Content-Length")
        if length_header is None or not length_header.isdigit():
            self.close_connection = True
            self._error(HTTPStatus.LENGTH_REQUIRED, "Content-Length header is required")
            return False, None
        length = int(length_header)
        if length > MAX_BODY_BYTES:
            self.close_connection = True  # do not read a huge body we will reject
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"body must be at most {MAX_BODY_BYTES} bytes")
            return False, None
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
            return False, None
        try:
            return True, json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"body is not valid JSON: {exc}")
            return False, None

    # ---- routes --------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        started = time.perf_counter()
        ctx = self.server.ctx
        if self.path != "/applications":
            self.close_connection = True
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        client_id = self._client_id()
        if not ctx.limiter.allow(client_id):
            self.close_connection = True
            retry = max(1, round(ctx.limiter.retry_after(client_id) + 0.5))
            ctx.metrics.incr("rate_limited")
            self._send(HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate limit exceeded"}, {"Retry-After": str(retry)})
            return
        ok, body = self._read_json()
        if not ok:
            return
        key = self.headers.get("Idempotency-Key", "")
        if not _SAFE_ID.match(key):
            self._error(HTTPStatus.BAD_REQUEST, "Idempotency-Key header is required (1-128 chars of [A-Za-z0-9._:-])")
            return
        try:
            result = applications.submit(self.conn, ctx.catalog, client_id, key, body)
        except ValidationError as exc:
            self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation failed", details=exc.errors)
            return
        except IdempotencyConflict as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
            return
        if result.created:
            ctx.metrics.incr("applications_created")
            self._send(HTTPStatus.CREATED, {"id": result.application_id, "state": "applied"},
                       {"Location": f"/applications/{result.application_id}"})
        else:
            ctx.metrics.incr("idempotent_replays")
            self._send(HTTPStatus.OK, {"id": result.application_id, "replayed": True}, {"Idempotent-Replayed": "true"})
        ctx.metrics.observe("ingest", time.perf_counter() - started)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        ctx = self.server.ctx
        if self.path == "/health":
            self.conn.execute("SELECT 1").fetchone()
            self._send(HTTPStatus.OK, {"status": "ok", "jobs": list(ctx.catalog.ids())})
            return
        if self.path == "/metrics":
            snapshot = ctx.metrics.snapshot()
            snapshot["queue"] = WorkQueue.counts(self.conn)
            self._send(HTTPStatus.OK, snapshot)
            return
        match = _APP_PATH.match(self.path)
        if match:
            app = applications.get_application(self.conn, match.group(1))
            if app is None:
                self._error(HTTPStatus.NOT_FOUND, "application not found")
            else:
                self._send(HTTPStatus.OK, app)
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR lets a second process bind a port that is already
    # in use, so requests could silently go to someone else's server. Keep it
    # on elsewhere, where it only allows a quick restart after TIME_WAIT.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, ctx: AppContext, port: int = 8080) -> None:
        # Always bind to loopback: this is a local demo service.
        super().__init__(("127.0.0.1", port), Handler)
        self.ctx = ctx

    @property
    def port(self) -> int:
        return int(self.server_address[1])
