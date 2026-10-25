import json
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx

from uhkone.client import ClientError, ShopClient
from uhkone.seed import load_seed, sanitize_courses, write_seed


class PublicSeedTests(unittest.TestCase):
    def test_bundled_snapshot_has_public_course_facts_and_source(self):
        document = load_seed()
        self.assertGreater(len(document["courses"]), 0)
        self.assertIn("kauppa.urheiluhallit.fi", document["source_url"])
        self.assertTrue(all(row["location"] for row in document["courses"]))
        for row in document["courses"]:
            self.assertNotIn("account", row)
            self.assertNotIn("instructors", row)
            self.assertNotIn("description", row)

    def test_customer_and_unexpected_nested_data_cannot_enter_seed(self):
        row = deepcopy(load_seed()["courses"][0])
        row["linked_accounts"] = [{"name": "EXCLUDE ME"}]
        row["fare_products"][0]["access_token"] = "EXCLUDE ME"
        row["weekdays"][0]["private"] = "EXCLUDE ME"
        sanitized = sanitize_courses([row])
        self.assertNotIn("EXCLUDE ME", json.dumps(sanitized))
        self.assertEqual(sanitized[0]["reservation_event_group_id"], row["reservation_event_group_id"])

    def test_public_waiting_list_counts_survive_sanitization_and_round_trip(self):
        row = deepcopy(load_seed()["courses"][0])
        row.update(free=0, queue_capacity=5, queue=4, free_queue=1)
        row["queue_accounts"] = [{"name": "EXCLUDE ME"}]
        sanitized = sanitize_courses([row])
        self.assertEqual(sanitized[0]["queue_capacity"], 5)
        self.assertEqual(sanitized[0]["queue"], 4)
        self.assertEqual(sanitized[0]["free_queue"], 1)
        self.assertNotIn("EXCLUDE ME", json.dumps(sanitized))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seed.json"
            write_seed(path, [row], fetched_at=datetime(2026, 10, 18, tzinfo=UTC))
            self.assertEqual(load_seed(path)["courses"], sanitized)

    def test_tampered_snapshot_is_not_silently_accepted(self):
        row = load_seed()["courses"][0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seed.json"
            document = write_seed(path, [row], fetched_at=datetime(2026, 10, 18, tzinfo=UTC))
            self.assertEqual(load_seed(path)["courses"], [row])
            document["courses"][0]["name"] += " changed"
            path.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                load_seed(path)

    def test_even_supplied_http_client_cannot_redirect_local_read_to_production(self):
        requests = []
        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(302, headers={"Location": "https://kauppa.urheiluhallit.fi/webshop/"})
        local = "http://127.0.0.1:18765"
        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http, ShopClient(base_url=local, client=http) as shop, self.assertRaises(ClientError):
            shop.get_course(123)
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].startswith(local))


if __name__ == "__main__":
    unittest.main()
