"""Black-box tests over real HTTP on 127.0.0.1."""

import http.client
import json
import socket
import threading
import time
import unittest
from typing import Any, Dict, Optional, Tuple

from hvh.api import MAX_BODY_BYTES, ApiServer, AppContext
from hvh.metrics import Metrics
from hvh.ratelimit import RateLimiter
from hvh.worker import WorkerPool
from tests.helpers import CATALOG, DbTestCase, sample_body


class ApiTestCase(DbTestCase):
    capacity = 1000.0

    def setUp(self) -> None:
        super().setUp()
        self.metrics = Metrics()
        ctx = AppContext(self.db_path, CATALOG, RateLimiter(self.capacity, 0.001), self.metrics)
        self.server = ApiServer(ctx, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        super().tearDown()

    def request(self, method: str, path: str, body: Any = None, headers: Optional[Dict[str, str]] = None,
                raw: Optional[bytes] = None) -> Tuple[int, Dict[str, Any], http.client.HTTPResponse]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data, response

    def post(self, body: Any, key: str = "key-1", client: str = "tester", **kwargs: Any):
        return self.request("POST", "/applications", body, {"Idempotency-Key": key, "X-Client-Id": client}, **kwargs)


class ApiTests(ApiTestCase):
    def test_health(self) -> None:
        status, data, _ = self.request("GET", "/health")
        self.assertEqual((status, data["status"]), (200, "ok"))
        self.assertIn("sde-l4-hvh", data["jobs"])

    def test_create_then_replay(self) -> None:
        status, created, response = self.post(sample_body())
        self.assertEqual(status, 201)
        self.assertEqual(response.getheader("Location"), f"/applications/{created['id']}")
        status, replay, response = self.post(sample_body())
        self.assertEqual(status, 200)
        self.assertEqual(replay["id"], created["id"])
        self.assertEqual(response.getheader("Idempotent-Replayed"), "true")

    def test_conflicting_reuse_of_key(self) -> None:
        self.post(sample_body())
        status, data, _ = self.post(sample_body(years_experience=12))
        self.assertEqual(status, 409)
        self.assertIn("different body", data["error"])

    def test_missing_or_bad_idempotency_key(self) -> None:
        status, _, _ = self.request("POST", "/applications", sample_body(), {"X-Client-Id": "t"})
        self.assertEqual(status, 400)
        status, _, _ = self.post(sample_body(), key="has spaces")
        self.assertEqual(status, 400)

    def test_invalid_json_and_content_type(self) -> None:
        self.assertEqual(self.post(None, raw=b"{not json")[0], 400)
        status, _, _ = self.request("POST", "/applications", raw=b"{}",
                                    headers={"Content-Type": "text/plain", "Idempotency-Key": "k"})
        self.assertEqual(status, 415)

    def test_validation_errors_are_listed(self) -> None:
        status, data, _ = self.post(sample_body(years_experience=-3, email="nope"))
        self.assertEqual(status, 422)
        self.assertEqual(len(data["details"]), 2)

    def test_body_too_large(self) -> None:
        status, _, _ = self.post(None, raw=b"x" * (MAX_BODY_BYTES + 1))
        self.assertEqual(status, 413)

    def test_get_unknown_and_bad_paths(self) -> None:
        self.assertEqual(self.request("GET", "/applications/" + "0" * 32)[0], 404)
        self.assertEqual(self.request("GET", "/applications/../etc")[0], 404)
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("POST", "/nope", {})[0], 404)

    def test_get_application_after_scoring_shows_breakdown(self) -> None:
        _, created, _ = self.post(sample_body())
        WorkerPool(self.db_path, CATALOG, workers=1).run_until_drained(timeout=30)
        status, app, _ = self.request("GET", f"/applications/{created['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(app["state"], "assessed")
        self.assertEqual(len(app["score"]["factors"]), 4)
        self.assertEqual([e["to_state"] for e in app["history"]], ["applied", "screened", "assessed"])

    def test_metrics_report_counters_and_queue(self) -> None:
        self.post(sample_body())
        self.post(sample_body())
        status, data, _ = self.request("GET", "/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(data["counters"]["applications_created"], 1)
        self.assertEqual(data["counters"]["idempotent_replays"], 1)
        self.assertEqual(data["queue"]["ready"], 1)
        self.assertEqual(data["latency_ms"]["ingest"]["count"], 2)

    def test_concurrent_clients(self) -> None:
        statuses = []
        lock = threading.Lock()

        def client(i: int) -> None:
            for j in range(10):
                status = self.post(sample_body(years_experience=j), key=f"c{i}-{j}", client=f"client-{i}")[0]
                with lock:
                    statuses.append(status)

        threads = [threading.Thread(target=client, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(statuses.count(201), 80)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 80)


class PortTests(unittest.TestCase):
    def test_refuses_a_port_another_process_is_listening_on(self) -> None:
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen()
            ctx = AppContext("unused.db", CATALOG, RateLimiter(1, 1), Metrics())
            with self.assertRaises(OSError):
                ApiServer(ctx, port=other.getsockname()[1])


class RateLimitApiTests(ApiTestCase):
    capacity = 3.0

    def test_429_with_retry_after_and_per_client_buckets(self) -> None:
        codes = [self.post(sample_body(years_experience=i), key=f"k{i}")[0] for i in range(5)]
        self.assertEqual(codes, [201, 201, 201, 429, 429])
        status, data, response = self.post(sample_body(), key="k9")
        self.assertEqual(status, 429)
        self.assertGreaterEqual(int(response.getheader("Retry-After")), 1)
        self.assertEqual(self.post(sample_body(), key="other", client="someone-else")[0], 201)


if __name__ == "__main__":
    unittest.main()
