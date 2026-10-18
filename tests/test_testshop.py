"""Actual loopback HTTP tests; no merchant account, browser or outside network."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import httpx
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from uhkone.testshop import TestShop


def course_row():
    return {"reservation_event_group_id": 123, "service_id": 9, "location_id": 2,
        "reservation_group_id": 6, "name": "Leikki-ikäisten jatkouimakoulu<br>Advanced",
        "location": "Mäkelänrinne", "start": "2027-01-10 10:00:00", "end": "2027-02-10 10:30:00",
        "weekdays": [{"weekdays": [6], "times": "10:00 - 10:30"}], "capacity": 8,
        "free": 0, "is_reservable": 0, "public_reservation_start": "2026-12-01 08:00:00",
        "fare_products": [{"fare_product_id": 4321, "price": 7600}]}


class TestShopHTTPTests(unittest.TestCase):
    def setUp(self):
        self._clients = {}

    def tearDown(self):
        for client in self._clients.values():
            client.close()

    def request(self, shop, path, payload=None, *, rpc=False, client=None):
        headers, body = {}, None
        if payload is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded" if rpc else "application/json"
            body = urlencode({"input_format": "json", "output_format": "json", "content": json.dumps(payload)}).encode() if rpc else json.dumps(payload).encode()
        elif rpc:
            body = urlencode({"content": "null"}).encode()
        if client is None:
            if shop not in self._clients:
                self._clients[shop] = httpx.Client(trust_env=False, timeout=5)
            client = self._clients[shop]
        response = client.request("POST" if body is not None else "GET", shop.base_url + path, content=body, headers=headers)
        return response.status_code, response.headers, response.json() if "json" in response.headers.get("Content-Type", "") else response.text

    def add(self, shop, *, client=None, **changes):
        payload = {"account": {"account_id": "20", "name": "Testilapsi"}, "fare_product_id": 4321,
                   "reservation_event_group_id": 123, "quantity": 1, "price": 7600, "title": "Testi", "campaign_code": None}
        return self.request(shop, "/webshop/reservations2/apicart", payload | changes, client=client)

    def checkout(self, shop, *, method=4, balance=7600, client=None):
        options = {"return_url": shop.base_url + "/webshop/reservations2/?doPayment=success",
            "cancel_url": shop.base_url + "/webshop/reservations2/?doPayment=cancel",
            "balance_amount": balance, "payment_type_id": str(method), "service_group_id": 9}
        return self.request(shop, "/webshop/call/cart/externalpaymentstart", [options], rpc=True, client=client)

    def queue(self, shop, *, client=None, **changes):
        payload = {"account": {"account_id": "20", "name": "Testilapsi"},
                   "reservation_event_group_id": 123, "reservation_status_id": 1}
        return self.request(shop, "/webshop/reservations2/apiusereventgroups", payload | changes, client=client)

    def test_waitlist_reservation_is_shared_by_account_without_cart_payment_or_receipt(self):
        with TestShop([course_row()], opens_in=0, scenario="jono") as shop, httpx.Client(trust_env=False) as other:
            row = self.request(shop, "/webshop/reservations2/apieventgroups/123")[2]
            self.assertTrue(row["is_reservable"])
            self.assertEqual((row["free"], row["queue_capacity"], row["queue"], row["free_queue"]), (0, 1, 0, 1))
            self.assertEqual(self.queue(shop)[0], 200)
            own = self.request(shop, "/webshop/reservations2/apiusereventgroups", client=other)[2]
            self.assertEqual(len(own), 1)
            self.assertEqual((own[0]["reservation_status_id"], own[0]["queue_number"], own[0]["is_paid"], own[0]["receipt_event_id"]), (1, 1, 0, None))
            row = self.request(shop, "/webshop/reservations2/apieventgroups/123")[2]
            self.assertEqual((row["free"], row["queue"], row["free_queue"]), (0, 1, 0))
            self.assertEqual(self.request(shop, "/webshop/reservations2/apicart")[2], [])
            self.assertIsNone(self.request(shop, "/webshop/call/cart/getreceiptinfo", rpc=True)[2]["result"])
            stats = shop.stats()
            self.assertEqual((stats["queue_posts"], stats["queued"], stats["queue_capacity"], stats["writes"]), (1, 1, 1, 1))
            self.assertEqual((stats["cart_posts"], stats["checkout_posts"], stats["confirmed"], stats["receipts"], stats["fake_card_charges"], stats["balance_cents"]), (0, 0, 0, 0, 0, 100000))

    def test_lost_waitlist_response_retains_readable_evidence_and_does_not_duplicate(self):
        with TestShop([course_row()], opens_in=0, scenario="jono-vastaus-katoaa") as shop:
            code, headers, _ = self.queue(shop)
            self.assertEqual((code, headers["Retry-After"]), (503, "1"))
            own = self.request(shop, "/webshop/reservations2/apiusereventgroups")[2]
            self.assertEqual([(row["reservation_status_id"], row["queue_number"]) for row in own], [(1, 1)])
            self.assertEqual(self.queue(shop)[0], 409)
            self.assertEqual(self.request(shop, "/webshop/reservations2/apiusereventgroups")[2], own)
            stats = shop.stats()
            self.assertEqual((stats["reservations"], stats["queued"], stats["cart_items"], stats["checkout_posts"], stats["receipts"]), (1, 1, 0, 0, 0))

    def test_waitlist_rejects_closed_full_wrong_participant_and_nonqueue_requests(self):
        for scenario, opens_in in (("jono", 60), ("jono-taynna", 0), ("normaali", 0)):
            with self.subTest(scenario=scenario), TestShop([course_row()], opens_in=opens_in, scenario=scenario) as shop:
                self.assertEqual(self.queue(shop)[0], 409)
                self.assertEqual((shop.stats()["queued"], shop.stats()["reservations"]), (0, 0))
        with TestShop([course_row()], opens_in=0, scenario="jono") as shop:
            for changes in ({"account": {"account_id": 10, "name": "Testihuoltaja"}},
                            {"reservation_status_id": 4}, {"quantity": 1}, {"price": 7600}):
                with self.subTest(changes=changes):
                    self.assertEqual(self.queue(shop, **changes)[0], 409)
            self.assertEqual((shop.stats()["queued"], shop.stats()["cart_items"], shop.stats()["receipts"]), (0, 0, 0))

    def test_waitlist_can_upgrade_to_an_actual_place_without_duplicate_reservation(self):
        with TestShop([course_row()], opens_in=0, scenario="jono") as shop:
            self.assertEqual(self.queue(shop)[0], 200)
            queued_id = self.request(shop, "/webshop/reservations2/apiusereventgroups")[2][0]["reservation_id"]
            with shop._lock:
                shop._db.execute("UPDATE courses SET capacity=1 WHERE id=123")
            row = self.request(shop, "/webshop/reservations2/apieventgroups/123")[2]
            self.assertEqual((row["free"], row["queue"], row["free_queue"]), (1, 1, 0))
            self.assertEqual(self.add(shop)[0], 200)
            own = self.request(shop, "/webshop/reservations2/apiusereventgroups")[2]
            self.assertEqual([(row["reservation_id"], row["reservation_status_id"], row["queue_number"]) for row in own], [(queued_id, 7, None)])
            self.assertEqual(self.checkout(shop, method=5, balance=0)[0], 200)
            stats = shop.stats()
            self.assertEqual((stats["reservations"], stats["queued"], stats["confirmed"], stats["receipts"], stats["fake_card_charges"]), (1, 0, 1, 1, 1))

    def test_waitlist_capacity_is_shared_across_browser_sessions(self):
        with TestShop([course_row()], opens_in=0, scenario="jono") as shop, httpx.Client(trust_env=False) as first, httpx.Client(trust_env=False) as second:
            with ThreadPoolExecutor(max_workers=2) as pool:
                codes = list(pool.map(lambda client: self.queue(shop, client=client)[0], (first, second)))
            self.assertEqual(sorted(codes), [200, 409])
            stats = shop.stats()
            self.assertEqual((stats["reservations"], stats["queued"], stats["queue_posts"], stats["cart_items"], stats["receipts"]), (1, 1, 2, 0, 0))

    def test_separate_browser_carts_share_account_reservations_but_checkout_only_one_cart(self):
        other = course_row() | {"reservation_event_group_id": 124,
            "name": "Leikki-ikäisten uintikerho", "fare_products": [{"fare_product_id": 4322, "price": 8400}]}
        with TestShop([course_row(), other], opens_in=0) as shop, httpx.Client(trust_env=False) as first, httpx.Client(trust_env=False) as second:
            for client in (first, second):
                user = self.request(shop, "/webshop/call/user/getcurrentuser", [True, True], rpc=True, client=client)[2]["result"]
                self.assertEqual(user["account_id"], 10)
            self.assertEqual(self.add(shop, client=first)[0], 200)
            self.assertEqual(self.add(shop, client=second, reservation_event_group_id=124, fare_product_id=4322, price=8400)[0], 200)
            for client, course_id in ((first, 123), (second, 124)):
                cart = self.request(shop, "/webshop/reservations2/apicart", client=client)[2]
                self.assertEqual([row["reservation_params"]["reservation_event_group_id"] for row in cart], [course_id])
                own = self.request(shop, "/webshop/reservations2/apiusereventgroups", client=client)[2]
                self.assertEqual([(row["reservation_event_group_id"], row["reservation_status_id"]) for row in own], [(123, 7), (124, 7)])
            self.assertEqual(self.checkout(shop, method=5, balance=0, client=first)[0], 200)
            self.assertEqual(self.request(shop, "/webshop/reservations2/apicart", client=first)[2], [])
            second_cart = self.request(shop, "/webshop/reservations2/apicart", client=second)[2]
            self.assertEqual([row["reservation_params"]["reservation_event_group_id"] for row in second_cart], [124])
            own = self.request(shop, "/webshop/reservations2/apiusereventgroups", client=second)[2]
            self.assertEqual([(row["reservation_event_group_id"], row["reservation_status_id"]) for row in own], [(123, 4), (124, 7)])
            first_receipt = self.request(shop, "/webshop/call/cart/getreceiptinfo", rpc=True, client=first)[2]["result"]
            self.assertEqual(first_receipt["total_amount"], 7600)
            self.assertIsNone(self.request(shop, "/webshop/call/cart/getreceiptinfo", rpc=True, client=second)[2]["result"])
            self.assertEqual(self.checkout(shop, method=5, balance=0, client=first)[0], 409)
            stats = shop.stats()
            self.assertEqual((stats["reservations"], stats["confirmed"], stats["cart_items"], stats["receipts"], stats["fake_card_charges"]), (2, 1, 1, 1, 1))
            self.assertEqual((stats["fastpay_total_cents"], stats["balance_cents"]), (7600, 100000))

    def test_session_cookie_is_private_and_receipt_does_not_change_when_other_browser_pays(self):
        other = course_row() | {"reservation_event_group_id": 124, "fare_products": [{"fare_product_id": 4322, "price": 8400}]}
        with TestShop([course_row(), other], opens_in=0) as shop, httpx.Client(trust_env=False) as first, httpx.Client(trust_env=False) as second:
            _, headers, _ = self.request(shop, "/", client=first)
            cookie = headers["Set-Cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)
            self.assertIn("Path=/", cookie)
            self.assertEqual(self.add(shop, client=first)[0], 200)
            self.assertEqual(self.checkout(shop, client=first)[0], 200)
            receipt = self.request(shop, "/webshop/call/cart/getreceiptinfo", rpc=True, client=first)[2]["result"]
            self.assertEqual(self.add(shop, client=second, reservation_event_group_id=124, fare_product_id=4322, price=8400)[0], 200)
            self.assertEqual(self.checkout(shop, balance=8400, client=second)[0], 200)
            other_receipt = self.request(shop, "/webshop/call/cart/getreceiptinfo", rpc=True, client=second)[2]["result"]
            self.assertNotEqual(receipt["receipt_id"], other_receipt["receipt_id"])
            self.assertEqual(other_receipt["total_amount"], 8400)
            self.assertEqual(self.request(shop, "/webshop/call/cart/getreceiptinfo", rpc=True, client=first)[2]["result"], receipt)
            stats = shop.stats()
            self.assertNotIn("session_id", json.dumps(stats))
            self.assertEqual((stats["confirmed"], stats["receipts"], stats["balance_cents"]), (2, 2, 84000))

    def test_existing_global_cart_schema_migrates_without_losing_old_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "legacy.sqlite")
            with sqlite3.connect(database) as stored:
                stored.executescript("""
                    CREATE TABLE cart(item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        reservation_id INTEGER NOT NULL UNIQUE REFERENCES reservations(id));
                    CREATE TABLE receipts(id INTEGER PRIMARY KEY AUTOINCREMENT, total_amount INTEGER NOT NULL);
                    INSERT INTO receipts(id,total_amount) VALUES(44,7600);
                """)
            with TestShop([course_row()], opens_in=0, db_path=database) as shop:
                self.assertEqual(shop.stats()["receipt_rows"], [{"id": 44, "total_amount": 7600, "payment_type_id": 4}])
                self.assertIsNone(self.request(shop, "/webshop/call/cart/getreceiptinfo", rpc=True)[2]["result"])
                self.assertEqual(self.add(shop)[0], 200)
                self.assertEqual(self.checkout(shop)[0], 200)
                receipt = self.request(shop, "/webshop/call/cart/getreceiptinfo", rpc=True)[2]["result"]
                self.assertEqual(receipt["receipt_id"], 45)
                self.assertEqual(shop.stats()["receipts"], 2)

    def test_public_seed_facts_are_preserved_and_simulation_is_explicit(self):
        source = course_row()
        original = deepcopy(source)
        with TestShop([source], opens_in=0) as shop:
            status, _, row = self.request(shop, "/webshop/reservations2/apieventgroups/123")
            self.assertEqual(status, 200)
            for key in ("name", "location", "start", "end", "weekdays", "fare_products"):
                self.assertEqual(row[key], original[key])
            self.assertTrue(row["is_reservable"])
            self.assertEqual(row["_test"]["original_availability"]["free"], 0)
            self.assertEqual(source, original)
            self.assertIn("TESTIKAUPPA", self.request(shop, "/webshop/reservations2/")[2])
            self.assertIn("SIMULOITU MAKSU", self.request(shop, "/webshop/reservations2/reservations/9/2/6/-/123")[2])

    def test_real_http_add_and_checkout_are_atomic_and_only_confirm_exact_child(self):
        with TestShop([course_row()], opens_in=0) as shop:
            user = self.request(shop, "/webshop/call/user/getcurrentuser", [True, True], rpc=True)[2]["result"]
            self.assertEqual((user["account_id"], user["linked_accounts"][0]["account_id"]), (10, 20))
            self.assertEqual(self.add(shop)[0], 200)
            held = self.request(shop, "/webshop/reservations2/apiusereventgroups")[2][0]
            self.assertEqual((held["reservation_status_id"], held["is_paid"]), (7, 0))
            self.assertEqual(self.checkout(shop)[0], 200)
            status = self.request(shop, "/_test/status")[2]
            self.assertEqual((status["cart_posts"], status["checkout_posts"], status["receipts"], status["confirmed"]), (1, 1, 1, 1))
            self.assertEqual((status["participant_ids"], status["cart_items"], status["balance_cents"]), ([20], 0, 92400))
            receipt = self.request(shop, "/webshop/call/cart/getreceiptinfo", rpc=True)[2]["result"]
            self.assertEqual((receipt["total_amount"], receipt["is_closed"], receipt["is_pay_later"]), (7600, 1, 0))
            self.assertEqual(self.checkout(shop)[0], 409)
            self.assertEqual(shop.stats()["receipts"], 1)

    def test_not_open_wrong_parent_price_quantity_and_full_course_never_hold_stock(self):
        with TestShop([course_row()], opens_in=60) as shop:
            self.assertEqual(self.add(shop)[0], 409)
            self.assertEqual(shop.stats()["reservations"], 0)
        with TestShop([course_row()], opens_in=0) as shop:
            for changes in ({"account": {"account_id": 10}}, {"quantity": 2}, {"price": 7500}, {"fare_product_id": 999}):
                with self.subTest(changes=changes):
                    self.assertEqual(self.add(shop, **changes)[0], 409)
            self.assertEqual(shop.stats()["reservations"], 0)
        with TestShop([course_row()], opens_in=0, scenario="taynna") as shop:
            self.assertEqual(self.add(shop)[0], 409)
            self.assertEqual(shop.stats()["reservations"], 0)

    def test_last_place_concurrent_requests_can_only_commit_one_hold(self):
        with TestShop([course_row()], opens_in=0, scenario="viimeinen-paikka") as shop, httpx.Client(trust_env=False) as first, httpx.Client(trust_env=False) as second:
            with ThreadPoolExecutor(max_workers=2) as pool:
                codes = list(pool.map(lambda client: self.add(shop, client=client)[0], (first, second)))
            self.assertEqual(sorted(codes), [200, 409])
            self.assertEqual((shop.stats()["reservations"], shop.stats()["cart_items"]), (1, 1))

    def test_congestion_retries_and_lost_responses_preserve_committed_evidence(self):
        with TestShop([course_row()], opens_in=0, scenario="ruuhka") as shop:
            for _ in range(2):
                code, headers, _ = self.request(shop, "/webshop/reservations2/apieventgroups/123")
                self.assertEqual((code, headers["Retry-After"]), (503, "1"))
            self.assertEqual(self.request(shop, "/webshop/reservations2/apieventgroups/123")[0], 200)
        for scenario in ("varaus-vastaus-katoaa", "maksu-vastaus-katoaa"):
            with self.subTest(scenario=scenario), TestShop([course_row()], opens_in=0, scenario=scenario) as shop:
                self.assertEqual(self.add(shop)[0], 503 if scenario.startswith("varaus") else 200)
                self.assertEqual(shop.stats()["cart_items"], 1)
                self.assertEqual(self.checkout(shop)[0], 503 if scenario.startswith("maksu") else 200)
                self.assertEqual((shop.stats()["receipts"], shop.stats()["confirmed"]), (1, 1))

    def test_sqlite_restart_preserves_payment_and_prevents_duplicate_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "test.sqlite")
            with TestShop([course_row()], opens_in=0, db_path=database) as shop:
                self.assertEqual(self.add(shop)[0], 200)
                self.assertEqual(self.checkout(shop)[0], 200)
            with TestShop([course_row()], opens_in=0, db_path=database) as shop:
                self.assertEqual(shop.stats()["receipts"], 1)
                self.assertEqual(self.add(shop)[0], 409)
                self.assertEqual((shop.stats()["reservations"], shop.stats()["balance_cents"]), (1, 92400))

    def test_fastpay_and_lost_payment_response_commit_once_without_spending_balance(self):
        for scenario, balance in (("normaali", None), ("maksu-vastaus-katoaa", 0)):
            with self.subTest(scenario=scenario), TestShop([course_row()], opens_in=0, scenario=scenario) as shop:
                types = self.request(shop, "/webshop/call/cart/getonlinepaymenttypes", [], rpc=True)[2]["result"]
                fastpay = next(row for row in types if row["payment_type_id"] == 5)
                self.assertIn("FastPay", fastpay["name"])
                self.assertEqual((fastpay["is_balance"], fastpay["is_pay_later"], fastpay["is_disabled"]), (False, False, False))
                self.assertEqual(self.add(shop)[0], 200)
                self.assertEqual(self.checkout(shop, method=5, balance=balance)[0], 503 if scenario.startswith("maksu") else 200)
                stats = shop.stats()
                self.assertEqual((stats["checkout_posts"], stats["fake_card_charges"], stats["receipts"], stats["confirmed"]), (1, 1, 1, 1))
                self.assertEqual((stats["balance_cents"], stats["fastpay_total_cents"], stats["payment_type_ids"]), (100000, 7600, [5]))
                receipt = self.request(shop, "/webshop/call/cart/getreceiptinfo", rpc=True)[2]["result"]
                self.assertEqual((receipt["payment_type_id"], receipt["is_closed"], receipt["total_amount"]), (5, 1, 7600))
                self.assertEqual(self.checkout(shop, method=5, balance=0)[0], 409)
                self.assertEqual((shop.stats()["fake_card_charges"], shop.stats()["receipts"]), (1, 1))

    def test_missing_or_declined_fastpay_card_does_not_charge_or_confirm(self):
        for scenario, expected in (("fastpay-kortti-puuttuu", 409), ("fastpay-hylatty", 402)):
            with self.subTest(scenario=scenario), TestShop([course_row()], opens_in=0, scenario=scenario) as shop:
                types = self.request(shop, "/webshop/call/cart/getonlinepaymenttypes", [], rpc=True)[2]["result"]
                fastpay = next(row for row in types if row["payment_type_id"] == 5)
                self.assertEqual(fastpay["is_disabled"], scenario == "fastpay-kortti-puuttuu")
                self.assertEqual(self.add(shop)[0], 200)
                self.assertEqual(self.checkout(shop, method=5, balance=None)[0], expected)
                stats = shop.stats()
                self.assertEqual((stats["checkout_posts"], stats["fake_card_charges"], stats["receipts"], stats["confirmed"]), (1, 0, 0, 0))
                self.assertEqual((stats["balance_cents"], stats["cart_items"]), (100000, 1))

    def test_fastpay_never_combines_card_with_account_balance(self):
        with TestShop([course_row()], opens_in=0) as shop:
            self.assertEqual(self.add(shop)[0], 200)
            self.assertEqual(self.checkout(shop, method=5, balance=7600)[0], 409)
            self.assertEqual((shop.stats()["receipts"], shop.stats()["balance_cents"]), (0, 100000))

    def test_cross_origin_mutation_is_rejected(self):
        with TestShop([course_row()], opens_in=0) as shop:
            request = Request(shop.base_url + "/webshop/reservations2/apicart", data=b"{}",
                              headers={"Origin": "https://unrelated.example"})
            with self.assertRaises(HTTPError) as error:
                build_opener(ProxyHandler({})).open(request, timeout=5)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
            self.assertEqual(shop.stats()["writes"], 0)


if __name__ == "__main__":
    unittest.main()
