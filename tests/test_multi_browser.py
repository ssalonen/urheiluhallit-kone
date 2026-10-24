"""Exclusive browser write routes and shared evidence for candidate courses."""
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from tests.test_booking_browser import cart_row, course_row, reservation, spec
from uhkone.booking import BookingError, Observation, TransientBookingError
from uhkone.client import BASE_URL
from uhkone.multi_browser import BookingPool, _candidate_rows, _PoolRouting


def candidates():
    first = spec("fastpay")
    row = course_row() | {"reservation_event_group_id": 124}
    second_course = replace(first.course, id=124, url=first.course.url.rsplit("/", 1)[0] + "/124", raw=row)
    return first, replace(first, course=second_course)


class Page:
    def __init__(self, url=BASE_URL + "/webshop/reservations2/", opener=None):
        self.url, self.parent = url, opener
        self.jobs = []
        self.values = None
        self.focus_count = 0

    def bring_to_front(self):
        self.focus_count += 1

    def opener(self):
        return self.parent

    def is_closed(self):
        return False

    def evaluate(self, script, argument):
        if isinstance(argument, str):
            self.url = argument
            return
        self.jobs.append(argument)
        if self.values is not None:
            return self.values
        return [{"id": job["id"], "status": 200,
                 "value": course_row() | {"reservation_event_group_id": job["id"]}}
                for job in reversed(argument)]

    def wait_for_url(self, url, **options):
        self.url = url

    def wait_for_function(self, script, **options):
        return None


def request(page, course_id, *, method="POST"):
    body = {"reservation_event_group_id": course_id, "fare_product_id": 4321,
            "quantity": 1, "price": 7600, "account": {"account_id": 20, "name": "Aino Esimerkki"}}
    return SimpleNamespace(url=BASE_URL + "/webshop/reservations2/apicart", method=method,
                           post_data=json.dumps(body), frame=SimpleNamespace(page=page))


def route_for(req, calls):
    return SimpleNamespace(request=req, continue_=lambda: calls.append("sent"),
                           abort=lambda reason: calls.append("blocked"))


class ExclusiveTabRoutingTests(unittest.TestCase):
    def setUp(self):
        first, second = candidates()
        third = replace(second, course=replace(second.course, id=125,
                        url=second.course.url.rsplit("/", 1)[0] + "/125",
                        raw=second.course.raw | {"reservation_event_group_id": 125}))
        self.specs = (first, second, third)
        self.routing = _PoolRouting(None, self.specs, base_url=BASE_URL)
        self.pages = [Page(), Page(), Page()]
        self.pool = BookingPool(self.specs, None, self.pages, self.routing)
        for backend in self.pool.backends.values():
            backend.guard.child_id, backend.guard.product_id = 20, 4321
            backend.guard.price = backend.spec.course.price

    def test_inactive_tab_cannot_send_even_payload_matching_active_course(self):
        selected = self.pool.activate(123)
        selected.guard.arm("add")
        calls = []
        self.routing.route(route_for(request(self.pages[1], 123), calls))
        self.assertEqual(calls, ["blocked"])
        self.assertFalse(selected.guard.sent)
        self.routing.route(route_for(request(self.pages[0], 123), calls))
        self.assertEqual(calls, ["blocked", "sent"])
        self.assertTrue(selected.guard.sent)

    def test_switching_is_allowed_before_send_and_pinned_after_any_possible_send(self):
        self.pool.activate(123)
        chosen = self.pool.activate(124)
        chosen.guard.arm("add")
        calls = []
        self.routing.route(route_for(request(self.pages[1], 124), calls))
        self.assertEqual(calls, ["sent"])
        chosen.guard.sent = False  # The backend starts a later phase; pin persists.
        with self.assertRaises(BookingError):
            self.pool.activate(123)
        self.assertIs(self.pool.activate(124), chosen)

    def test_only_selected_tabs_popup_inherits_its_route(self):
        self.pool.activate(123).guard.arm("add")
        calls = []
        self.routing.route(route_for(request(Page(opener=self.pages[1]), 123), calls))
        self.routing.route(route_for(request(Page(opener=self.pages[0]), 123), calls))
        self.assertEqual(calls, ["blocked", "sent"])

    def test_prepared_tabs_are_read_only_before_activation(self):
        calls = []
        self.pool.backends[123].guard.arm("add")
        self.routing.route(route_for(request(self.pages[0], 123), calls))
        self.assertEqual(calls, ["blocked"])
        self.assertIsNone(self.routing.committed_id)

    def test_activation_foregrounds_only_the_winner_before_granting_its_route(self):
        states = []
        self.pages[1].bring_to_front = Mock(side_effect=lambda: states.append(self.routing.active_id))
        chosen = self.pool.activate(124)
        self.assertEqual(states, [None])
        self.assertEqual(self.routing.active_id, 124)
        self.assertIs(chosen.page, self.pages[1])
        self.assertEqual(self.pages[0].focus_count, 0)

    def test_queue_does_not_pin_an_actual_candidate_or_allow_wrong_tab_writes(self):
        backend = self.pool.activate_queue(123)
        backend.guard.arm("queue")
        req = request(self.pages[0], 123)
        req.url = BASE_URL + "/webshop/reservations2/apiusereventgroups"
        req.post_data = json.dumps({"reservation_event_group_id": 123, "reservation_status_id": 1,
                                   "account": {"account_id": 20, "name": "Aino Esimerkki"}})
        calls = []
        wrong_page = SimpleNamespace(**vars(req))
        wrong_page.frame = SimpleNamespace(page=self.pages[1])
        self.routing.route(route_for(wrong_page, calls))
        self.assertFalse(backend.guard.sent)
        self.routing.route(route_for(req, calls))
        self.assertEqual(calls, ["blocked", "sent"])
        self.assertTrue(backend.guard.queue_sent)
        self.assertFalse(backend.guard.actual_sent)
        self.assertIsNone(self.routing.actual_committed_id)
        self.assertIs(self.pool.activate_queue(124), self.pool.backends[124])

    def test_two_queues_then_another_actual_course_are_permitted(self):
        calls = []
        for index, course_id in enumerate((123, 124)):
            backend = self.pool.activate_queue(course_id)
            backend.guard.arm("queue")
            req = request(self.pages[index], course_id)
            req.url = BASE_URL + "/webshop/reservations2/apiusereventgroups"
            req.post_data = json.dumps({"reservation_event_group_id": course_id, "reservation_status_id": 1,
                                       "account": {"account_id": 20, "name": "Aino Esimerkki"}})
            self.routing.route(route_for(req, calls))
            backend.guard.disarm()
            read = request(self.pages[index], course_id, method="GET")
            self.routing.route(route_for(read, calls))
            self.assertIsNone(self.routing.actual_committed_id)
            backend.guard.arm("queue")
            self.routing.route(route_for(req, calls))
            backend.guard.disarm()
        self.assertEqual(calls, ["sent", "sent", "blocked"] * 2)
        chosen = self.pool.activate(125)
        chosen.guard.arm("add")
        self.routing.route(route_for(request(self.pages[2], 125), calls))
        self.assertTrue(chosen.guard.actual_sent)
        self.assertEqual(self.routing.actual_committed_id, 125)
        for course_id in (123, 124, 125):
            with self.assertRaises(BookingError):
                self.pool.activate_queue(course_id)
        with self.assertRaises(BookingError):
            self.pool.activate(124)

    def test_ambiguous_actual_transmission_pins_even_if_continue_raises(self):
        self.pool.activate(123).guard.arm("add")
        def transmission_error():
            raise RuntimeError("connection lost after request dispatch")
        route = SimpleNamespace(request=request(self.pages[0], 123), continue_=transmission_error,
                                abort=lambda reason: self.fail("valid payload must be dispatched"))
        with self.assertRaises(RuntimeError):
            self.routing.route(route)
        self.assertEqual(self.routing.actual_committed_id, 123)
        with self.assertRaises(BookingError):
            self.pool.activate(124)
        with self.assertRaises(BookingError):
            self.pool.activate_queue(123)


class SharedCandidateEvidenceTests(unittest.TestCase):
    def make_pool(self, *, cart=None, reservations=None, locked=False):
        specs = candidates()
        pages = [Page(), Page()]
        routing = _PoolRouting(None, specs, base_url=BASE_URL)
        pool = BookingPool(specs, None, pages, routing)
        calls = []
        reader = pool._reader()
        def user():
            calls.append("user")
            return {"account_id": 10, "linked_accounts": [{"account_id": 20, "name": "Aino Esimerkki"}]}
        reader._user = Mock(side_effect=user)
        values = {"/webshop/reservations2/apicart": cart or [],
                  "/webshop/reservations2/apiusereventgroups": reservations or [],
                  "cart/getreceiptinfo": None, "cart/islocked": {"isLocked": locked}}
        def read(path, **options):
            calls.append(path)
            return values[path]
        reader._read = Mock(side_effect=read)
        return pool, calls

    def test_shared_account_state_is_read_once_and_candidate_urls_batched(self):
        pool, calls = self.make_pool()
        values = pool.observations()
        self.assertEqual(set(values), {123, 124})
        self.assertTrue(all(isinstance(value, Observation) for value in values.values()))
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(pool._reader().page.jobs), 1)
        self.assertEqual({job["id"] for job in pool._reader().page.jobs[0]}, {123, 124})
        self.assertTrue(pool.selection_clear)
        self.assertEqual(pool.identity, BASE_URL + "|10:20")

    def test_one_course_503_does_not_hide_other_available_candidate(self):
        pool, _ = self.make_pool()
        pool._reader().page.values = [
            {"id": 123, "status": 503, "retry_after": "7"},
            {"id": 124, "status": 200, "value": course_row() | {"reservation_event_group_id": 124}},
        ]
        values = pool.observations()
        self.assertIsInstance(values[123], TransientBookingError)
        self.assertEqual(values[123].retry_after, 7)
        self.assertTrue(values[124].available)
        self.assertTrue(pool.selection_clear)

    def test_candidate_hold_is_shared_evidence_and_blocks_selection_clear(self):
        held = cart_row()
        held["reservation_params"]["reservation_event_group_id"] = 124
        pool, _ = self.make_pool(cart=[held], reservations=[reservation() | {"reservation_event_group_id": 124}])
        values = pool.observations()
        self.assertTrue(values[124].held)
        self.assertTrue(values[123].cart_has_others)
        self.assertFalse(pool.selection_clear)

    def test_existing_hold_is_not_mistaken_for_foreign_cart_when_its_course_get_fails(self):
        held = cart_row()
        held["reservation_params"]["reservation_event_group_id"] = 124
        pool, _ = self.make_pool(cart=[held], reservations=[reservation() | {"reservation_event_group_id": 124}])
        pool._reader().page.values = [
            {"id": 124, "status": 503}, {"id": 123, "status": 200, "value": course_row()},
        ]
        pool.initialize()
        self.assertFalse(pool.selection_clear)
        self.assertIsNone(pool._reader().payment_type_id)

    def test_confirmed_reservation_in_failed_candidate_prevents_fallback(self):
        pool, _ = self.make_pool(reservations=[reservation(4, 1) | {"reservation_event_group_id": 124}])
        pool._reader().page.values = [
            {"id": 124, "transport_error": True}, {"id": 123, "status": 200, "value": course_row()},
        ]
        values = pool.observations()
        self.assertTrue(values[123].available)
        self.assertFalse(pool.selection_clear)

    def test_canceled_reservation_does_not_prevent_a_new_selection(self):
        pool, _ = self.make_pool(reservations=[reservation(6, 0)])
        pool.observations()
        self.assertTrue(pool.selection_clear)

    def test_existing_queue_does_not_block_an_actual_place(self):
        pool, _ = self.make_pool(reservations=[reservation(1, 0) | {"queue_number": 2}])
        observations = pool.observations()
        self.assertTrue(pool.selection_clear)
        self.assertTrue(observations[123].queued)
        self.assertIsNone(observations[123].reservation_id)
        self.assertTrue(observations[123].available)

    def test_unknown_batch_shapes_fail_closed(self):
        with self.assertRaises(BookingError):
            _candidate_rows([{"id": 123, "status": 200, "value": course_row()}], {123, 124})
        with self.assertRaises(BookingError):
            _candidate_rows([{"id": 123, "status": 200, "value": course_row()}] * 2, {123, 124})


if __name__ == "__main__":
    unittest.main()
