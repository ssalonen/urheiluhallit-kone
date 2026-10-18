"""Explicit test origins cannot grant access to the real shop or another port."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest

from uhkone.booking import BookingSpec
from uhkone.booking_browser import BookingBrowser, _BookingGuard, _fresh_course
from uhkone.browser import BrowserError, _PreparationGuard, _request_allowed, _validate_course_url
from uhkone.client import BASE_URL, parse_course
from uhkone.origin import validate_base_url


LOCAL = "http://127.0.0.1:18765"
OTHER_LOCAL = "http://127.0.0.1:18766"
COURSE_PATH = "/webshop/reservations2/reservations/9/2/6/-/123"


def row():
    return {"reservation_event_group_id": 123, "service_id": 9,
            "location_id": 2, "reservation_group_id": 6,
            "name": "Leikki-ikäisten jatkouimakoulu", "location": "Mäkelänrinne",
            "free": 1, "is_reservable": 1,
            "fare_products": [{"fare_product_id": 4321, "price": 7600}]}


def local_spec(mode="saldo"):
    opening = datetime(2027, 1, 1, tzinfo=timezone.utc)
    return BookingSpec(parse_course(row(), base_url=LOCAL), "Testilapsi", Decimal("80"),
                       opening, opening + timedelta(minutes=30), mode)


class ExplicitOriginPolicyTests(unittest.TestCase):
    def test_default_policy_never_accepts_local_http(self):
        self.assertFalse(_request_allowed(LOCAL + "/webshop/reservations2/apicart", "GET"))
        with self.assertRaises(BrowserError):
            _validate_course_url(local_spec().course)

    def test_local_read_policy_accepts_only_the_explicit_port(self):
        path = "/webshop/reservations2/apicart"
        self.assertTrue(_request_allowed(LOCAL + path, "GET", base_url=LOCAL))
        for origin in (BASE_URL, OTHER_LOCAL, "http://localhost:18765", "https://127.0.0.1:18765"):
            with self.subTest(origin=origin):
                self.assertFalse(_request_allowed(origin + path, "GET", base_url=LOCAL))

    def test_test_course_url_requires_explicit_matching_origin_and_group(self):
        course = local_spec().course
        self.assertEqual(_validate_course_url(course, base_url=LOCAL), LOCAL + COURSE_PATH)
        self.assertEqual(_fresh_course(local_spec(), row(), base_url=LOCAL).url, LOCAL + COURSE_PATH)
        for url in (BASE_URL + COURSE_PATH, OTHER_LOCAL + COURSE_PATH, LOCAL + COURSE_PATH + "0"):
            with self.subTest(url=url), self.assertRaises(BrowserError):
                _validate_course_url(replace(course, url=url), base_url=LOCAL)

    def test_arbitrary_origins_and_plain_http_production_are_rejected(self):
        for origin in ("http://kauppa.urheiluhallit.fi", "https://example.com", "http://127.0.0.1",
                       "http://localhost:18765", "http://127.0.0.1:18765/path", "https://kauppa.urheiluhallit.fi:8443"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                validate_base_url(origin)

    def test_scoped_local_write_cannot_reach_production_or_another_port(self):
        guard = _BookingGuard(None, local_spec(), base_url=LOCAL)
        guard.child_id, guard.product_id, guard.price = 20, 4321, Decimal("76")
        guard.arm("add")
        body = json.dumps({"reservation_event_group_id": 123, "fare_product_id": 4321,
                           "quantity": 1, "price": 7600, "account": {"account_id": 20, "name": "Testilapsi"}})
        path = "/webshop/reservations2/apicart"
        self.assertTrue(guard.permits_write(LOCAL + path, "POST", body))
        self.assertFalse(guard.permits_write(BASE_URL + path, "POST", body))
        self.assertFalse(guard.permits_write(OTHER_LOCAL + path, "POST", body))

    def test_preparation_guard_keeps_production_blocked_in_test_mode(self):
        guard = _PreparationGuard(None, base_url=LOCAL)
        calls = []
        for origin in (LOCAL, BASE_URL):
            request = SimpleNamespace(url=origin + "/webshop/reservations2/apieventgroups/123", method="GET")
            guard.route(SimpleNamespace(request=request, continue_=lambda: calls.append("allowed"),
                                         abort=lambda reason: calls.append("blocked")))
        self.assertEqual(calls, ["allowed", "blocked"])

    def test_local_mode_cannot_enable_external_payment_navigation(self):
        guard = _BookingGuard(None, local_spec("verkkomaksu"), base_url=LOCAL)
        guard.checkout_sent = True
        response = SimpleNamespace(url=LOCAL + "/webshop/call/cart/externalpaymentstart",
                                   json=lambda: {"result": {"form": '<form id="payment_form" action="https://payments.example/start"></form>'}})
        guard.response(response)
        self.assertEqual(guard.provider_origins, set())
        guard.provider_origins.add(BASE_URL)
        request = SimpleNamespace(url=BASE_URL + "/webshop/reservations2/apicart", method="POST")
        self.assertFalse(guard._provider_request(request))

    def test_browser_api_reads_use_explicit_origin_without_global_patch(self):
        urls = []
        def get(url, **options):
            urls.append(url)
            return SimpleNamespace(status=200, headers={}, json=row, dispose=lambda: None)
        context = SimpleNamespace(request=SimpleNamespace(get=get))
        guard = _BookingGuard(context, local_spec(), base_url=LOCAL)
        backend = BookingBrowser(local_spec(), context, None, guard, base_url=LOCAL)
        self.assertEqual(backend._read("/webshop/reservations2/apieventgroups/123")["reservation_event_group_id"], 123)
        self.assertEqual(urls, [LOCAL + "/webshop/reservations2/apieventgroups/123"])
        self.assertEqual(BASE_URL, "https://kauppa.urheiluhallit.fi")

    def test_mixed_guard_and_api_origin_is_rejected(self):
        guard = _BookingGuard(None, local_spec())
        with self.assertRaises(ValueError):
            BookingBrowser(local_spec(), None, None, guard, base_url=LOCAL)


if __name__ == "__main__":
    unittest.main()
