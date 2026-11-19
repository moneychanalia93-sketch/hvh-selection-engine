import threading
import unittest

from hvh.applications import IdempotencyConflict, get_application, new_application_id, submit, submit_many
from hvh.db import connect
from tests.helpers import CATALOG, DbTestCase, sample_body


class IdempotencyTests(DbTestCase):
    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def test_retry_returns_same_application(self) -> None:
        first = submit(self.conn, CATALOG, "client-a", "key-1", sample_body())
        again = submit(self.conn, CATALOG, "client-a", "key-1", sample_body())
        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual(first.application_id, again.application_id)
        self.assertEqual(self.count("applications"), 1)
        self.assertEqual(self.count("queue"), 1)  # no duplicate job either
        self.assertEqual(self.count("events"), 1)

    def test_same_key_different_body_conflicts(self) -> None:
        submit(self.conn, CATALOG, "client-a", "key-1", sample_body())
        with self.assertRaises(IdempotencyConflict):
            submit(self.conn, CATALOG, "client-a", "key-1", sample_body(years_experience=9))
        self.assertEqual(self.count("applications"), 1)

    def test_key_is_scoped_per_client(self) -> None:
        a = submit(self.conn, CATALOG, "client-a", "key-1", sample_body())
        b = submit(self.conn, CATALOG, "client-b", "key-1", sample_body())
        self.assertNotEqual(a.application_id, b.application_id)

    def test_key_order_in_json_does_not_matter(self) -> None:
        body = sample_body()
        reordered = {"candidate": body["candidate"], "self_identification": body["self_identification"],
                     "job_id": body["job_id"]}
        submit(self.conn, CATALOG, "c", "k", body)
        self.assertFalse(submit(self.conn, CATALOG, "c", "k", reordered).created)

    def test_concurrent_retries_create_exactly_one(self) -> None:
        results = []
        lock = threading.Lock()

        def send() -> None:
            conn = connect(self.db_path)
            try:
                result = submit(conn, CATALOG, "client-a", "race-key", sample_body())
                with lock:
                    results.append(result)
            finally:
                conn.close()

        threads = [threading.Thread(target=send) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 16)
        self.assertEqual(len({r.application_id for r in results}), 1)
        self.assertEqual(sum(r.created for r in results), 1)
        self.assertEqual(self.count("applications"), 1)

    def test_bulk_load_is_idempotent(self) -> None:
        items = [(f"k{i}", sample_body(years_experience=i)) for i in range(10)]
        self.assertEqual(submit_many(self.conn, CATALOG, "seed", items, batch_size=3), (10, 0))
        self.assertEqual(submit_many(self.conn, CATALOG, "seed", items, batch_size=3), (0, 10))
        self.assertEqual(self.count("applications"), 10)

    def test_ids_are_unique_hex_and_time_ordered(self) -> None:
        ids = [new_application_id() for _ in range(10_000)]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(len(i) == 32 and int(i, 16) >= 0 for i in ids))
        prefixes = [i[:16] for i in ids]
        self.assertEqual(prefixes, sorted(prefixes))  # creation-time prefix never goes backwards

    def test_self_identification_stored_apart_and_not_returned(self) -> None:
        result = submit(self.conn, CATALOG, "c", "k", sample_body())
        app = get_application(self.conn, result.application_id)
        self.assertNotIn("self_identification", app)
        self.assertEqual(self.count("self_identification"), 1)


if __name__ == "__main__":
    unittest.main()
