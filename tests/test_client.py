"""Validate the observed Enkora contract without any live requests or writes."""
from decimal import Decimal
import unittest

import httpx

from uhkone.client import ClientError, ShopClient, parse_course


# Public fields from the observed preschool advanced course, trimmed to avoid
# depending on unrelated shop metadata or any participant/account information.
COURSE = {
    "reservation_event_group_id": 100679,
    "name": "Leikki-ikäisten jatkouimakoulu<br>Swimming School for Preschoolers, advanced",
    "location": "Vuosaari", "service_id": 9, "location_id": 9,
    "reservation_group_id": 6, "start": "2026-10-24 08:30:00",
    "end": "2026-12-12 09:00:00", "free": 6, "is_reservable": 1,
    "public_reservation_start": "2026-08-26 07:00:00",
    "weekdays": [{"days": "Sat", "times": "08:30-09:00", "weekdays": [5]}],
    "fare_products": [{"fare_product_id": 4406, "price": 7600}],
    "description": "<p>Vähintään 5 metriä vatsallaan &amp; selällään.</p>",
}


class ClientTests(unittest.TestCase):
    def make_client(self, payload=None, status=200):
        self.requests = []

        def respond(request):
            self.requests.append(request)
            return httpx.Response(status, json=payload)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        return ShopClient(client=client)

    def test_observed_schema_maps_cents_local_time_weekday_and_html(self):
        course = parse_course(COURSE)
        self.assertEqual(course.price, Decimal("76"))
        self.assertEqual(course.weekdays, (6,))
        self.assertEqual(course.time_start, "08:30")
        self.assertEqual(course.time_end, "09:00")
        self.assertEqual(course.start.isoformat(), "2026-10-24T08:30:00+03:00")
        self.assertEqual(course.end.isoformat(), "2026-12-12T09:00:00+02:00")
        self.assertNotIn("<br>", course.name)
        self.assertEqual(course.description, "Vähintään 5 metriä vatsallaan & selällään.")
        self.assertTrue(course.available)
        self.assertEqual(course.url, "https://kauppa.urheiluhallit.fi/webshop/reservations2/reservations/9/9/6/-/100679")

    def test_list_and_detail_use_get_and_revalidate_identity(self):
        with self.make_client([COURSE]) as client:
            self.assertEqual(client.courses()[0].id, COURSE["reservation_event_group_id"])
        self.assertEqual(self.requests[0].method, "GET")
        self.assertEqual(self.requests[0].url.params["service_id"], "9")
        with self.make_client(COURSE) as client:
            self.assertEqual(client.get_course(100679).id, 100679)
        self.assertEqual(self.requests[0].method, "GET")
        self.assertEqual(self.requests[0].url.path, "/webshop/reservations2/apieventgroups/100679")
        with self.make_client(COURSE) as client:
            with self.assertRaisesRegex(ClientError, "eri kurssin"):
                client.get_course(100680)

    def test_invalid_catalogue_and_required_identity_are_not_empty_results(self):
        for payload in ({"error": "login"}, [None], [{"name": "course"}], [COURSE, COURSE]):
            with self.subTest(payload=payload):
                with self.make_client(payload) as client:
                    with self.assertRaises(ClientError):
                        client.courses()
        self.assertEqual(self.make_client([]).courses(), [])

    def test_missing_and_ambiguous_prices_are_unknown(self):
        for products in ([], None, [{"price": None}], [{"price": "NaN"}], [{"price": 7600}, {"price": 8200}]):
            row = dict(COURSE, fare_products=products)
            self.assertIsNone(parse_course(row).price)
        self.assertEqual(parse_course(dict(COURSE, bundle_price=5700)).price, Decimal("57"))

    def test_unknown_availability_is_never_open(self):
        for value in (None, "true", "1", 2):
            self.assertFalse(parse_course(dict(COURSE, is_reservable=value)).available)
        for free in (None, -1, True, "unknown"):
            self.assertFalse(parse_course(dict(COURSE, free=free)).available)

    def test_waiting_list_slots_are_separate_from_course_places(self):
        course = parse_course(dict(COURSE, free=0, free_queue=1))
        self.assertEqual(course.free_places, 0)
        self.assertEqual(course.queue_places, 1)
        self.assertTrue(course.is_queueable)
        self.assertTrue(course.queue_available)
        self.assertFalse(course.available)
        both = parse_course(dict(COURSE, free=2, free_queue=3))
        self.assertTrue(both.available)
        self.assertTrue(both.queue_available)
        self.assertEqual(both.queue_places, 3)

    def test_queue_availability_requires_known_registration_gate_and_capacity(self):
        self.assertEqual(parse_course(dict(COURSE, free_queue="2")).queue_places, 2)
        self.assertTrue(parse_course(dict(COURSE, free_queue="2")).queue_available)
        for value in (None, False, 0, "true", "1", 2, 1.0):
            with self.subTest(registration_gate=value):
                course = parse_course(dict(COURSE, is_reservable=value, free_queue=1))
                self.assertFalse(course.queue_available)
        for value in (None, -1, True, "unknown", 1.0, "1.0", "-1", [], {}):
            with self.subTest(queue_capacity=value):
                course = parse_course(dict(COURSE, free_queue=value))
                self.assertIsNone(course.queue_places)
                self.assertFalse(course.queue_available)
        self.assertEqual(parse_course(dict(COURSE, free_queue=0)).queue_places, 0)
        self.assertFalse(parse_course(dict(COURSE, free_queue=0)).queue_available)
        self.assertFalse(parse_course(COURSE).queue_available)

    def test_http_and_non_json_errors_are_finnish(self):
        with self.make_client({"error": "not found"}, status=404) as client:
            with self.assertRaisesRegex(ClientError, "HTTP 404"):
                client.get_course(100679)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>login</html>"))
        with httpx.Client(transport=transport) as raw:
            with self.assertRaisesRegex(ClientError, "JSON"):
                ShopClient(client=raw).courses()


if __name__ == "__main__":
    unittest.main()
