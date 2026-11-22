"""Offline checks for irreversible-operation boundaries and server evidence."""
import io
import json
import unittest
from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlencode

from uhkone.booking import (
    ActionNotSent,
    ActionUncertain,
    BookingError,
    BookingSpec,
    Observation,
    TransientBookingError,
    run_booking,
)
from uhkone.booking_browser import (
    BookingBrowser,
    _BookingGuard,
    _eligible_payment,
    _observation,
    _retry_preflight,
)
from uhkone.client import BASE_URL, parse_course


def course_row():
    return {
        "reservation_event_group_id": 123, "service_id": 9,
        "name": "Leikki-ikäisten jatkouimakoulu", "location": "Mäkelänrinne",
        "location_id": 2, "reservation_group_id": 6,
        "start": "2027-01-10 10:00:00", "end": "2027-02-10 10:30:00",
        "free": 3, "is_reservable": 1,
        "fare_products": [{"fare_product_id": 4321, "price": 7600}],
    }


def spec(mode="saldo"):
    opening = datetime.now(UTC)
    return BookingSpec(parse_course(course_row()), "Aino Esimerkki", Decimal(80),
                       opening, opening + timedelta(minutes=30), mode)


def cart_row(child=20, price=7600):
    return {"item_id": 11, "fare_product_id": 4321, "quantity": 1, "price": price,
            "reservation_params": {"reservation_event_group_id": 123, "reservation_account_id": child}}


def reservation(status=7, paid=0, child=20):
    return {"reservation_id": 456, "reservation_event_group_id": 123,
            "reservation_account_id": child, "reservation_status_id": status, "is_paid": paid}


def observed_hold():
    """Fictional values in the authenticated pending course and cart JSON shape."""
    cart = {
        "item_id": 0, "fare_product_id": 4321, "fare_product_type_id": 2,
        "title": "Leikki-ikäisten jatkouimakoulu", "price": 7600, "bundle_price": None,
        "bundle_vat_amount": None,
        "reservation_params": {"reservation_event_group_id": 123, "reservation_account_id": 20, "tags": None},
        "sale_event_id": 601, "payment_event_id": None, "loading_event_id": 701,
        "organization_id": 1, "bundle_event_id": None, "is_bundle": None,
        "forward_account_number": None, "reservation_event_group_id": 123,
        "related_reservation_event_group_id": None, "max_age": None, "min_age": None,
        "quantity": 1, "vat_percentage": 13.5, "discount": 0, "type": 1, "capacity": 1,
        "amount_for_second_vat": None, "second_vat_percentage": None,
        "is_visible_bundle_item": False, "variation": None, "bundle_rows": None, "name_customer": None,
    }
    own = {
        "reservation_event_group_id": 123, "name": "Leikki-ikäisten jatkouimakoulu",
        "start": "2027-01-10 10:00:00", "end": "2027-02-10 10:30:00",
        "is_course": 1, "instructors": "Esimerkkiohjaaja", "event_number": 3,
        "reservation_status_id": 7, "reservation_id": 456, "location_id": 2,
        "location": "Mäkelänrinne", "reservation_account_id": 20,
        "reservation_account_name": "Aino Esimerkki", "payment_label": None, "payment_label_class": None,
    }
    return cart, own


class ScopeGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = _BookingGuard(None, spec())
        self.guard.child_id, self.guard.payer_id = 20, 10
        self.guard.product_id, self.guard.price = 4321, Decimal(76)
        self.add = {"reservation_event_group_id": 123, "fare_product_id": 4321,
                    "quantity": 1, "price": 7600, "title": "Kurssi",
                    "account": {"account_id": "20", "name": "Aino Esimerkki"},
                    "tags": {}, "campaign_code": None}
        self.add_url = BASE_URL + "/webshop/reservations2/apicart"

    def test_readiness_never_allows_an_unarmed_cart_write(self):
        self.assertFalse(self.guard.permits_write(self.add_url, "POST", json.dumps(self.add)))

    def test_unpaid_mode_cannot_arm_checkout(self):
        guard = _BookingGuard(None, replace(spec(), payment_mode="none", without_payment=True))
        with self.assertRaises(BookingError):
            guard.arm("checkout")
        self.assertFalse(guard.permits_write(BASE_URL + "/webshop/call/cart/externalpaymentstart", "POST", ""))

    def test_exact_course_child_quantity_and_price_are_required(self):
        self.guard.arm("add")
        self.assertTrue(self.guard.permits_write(self.add_url, "POST", json.dumps(self.add)))
        changes = [
            {"reservation_event_group_id": 124}, {"fare_product_id": 999},
            {"account": {"account_id": "10", "name": "Aino Esimerkki"}},
            {"account": {"account_id": "20", "name": "Eino Esimerkki"}},
            {"account": {"account_id": "20", "agreement_id": 1}},
            {"quantity": 2}, {"price": 8001}, {"price": 7500},
            {"related_reservation_event_group_id": 124},
            {"campaign_code": "unknown"},
        ]
        for change in changes:
            payload = deepcopy(self.add)
            payload.update(change)
            with self.subTest(change=change):
                self.assertFalse(self.guard.permits_write(self.add_url, "POST", json.dumps(payload)))

    def test_one_valid_outgoing_request_consumes_the_slot(self):
        self.guard.arm("add")
        calls = []
        request = SimpleNamespace(url=self.add_url, method="POST", post_data=json.dumps(self.add))
        route = SimpleNamespace(request=request,
                                continue_=lambda: calls.append("sent"),
                                abort=lambda reason: calls.append("blocked"))
        self.guard.route(route)
        self.guard.route(route)
        self.assertEqual(calls, ["sent", "blocked"])
        self.assertTrue(self.guard.sent)
        self.assertTrue(self.guard.actual_sent)

    def test_checkout_requires_selected_method_and_exact_balance(self):
        self.guard.payment_type_id, self.guard.balance_cents = 4, 7600
        self.guard.service_group_id = 9
        self.guard.arm("checkout")
        url = BASE_URL + "/webshop/call/cart/externalpaymentstart"
        options = {"return_url": BASE_URL + "/webshop/reservations2/?doPayment=success",
                   "cancel_url": BASE_URL + "/webshop/reservations2/?doPayment=cancel",
                   "balance_amount": 7600, "payment_type_id": "4", "service_group_id": 9}
        body = urlencode({"content": json.dumps([options]), "input_format": "json", "output_format": "json"})
        self.assertTrue(self.guard.permits_write(url, "POST", body))
        for change in ({"payment_type_id": 5}, {"balance_amount": 7601},
                       {"service_group_id": 8}, {"create_invoice": True},
                       {"return_url": "https://example.org/?doPayment=success"},
                       {"return_url": "//example.org/webshop/reservations2/?doPayment=success"},
                       {"return_url": "/webshop/reservations2/?doPayment=success&extra=1"},
                       {"return_url": BASE_URL + "/webshop/reservations2-neighbor?doPayment=success"},
                       {"return_url": BASE_URL + "/webshop/reservations2/reservations/9/2/6/-/124?doPayment=success"}):
            payload = options | change
            with self.subTest(change=change):
                self.assertFalse(self.guard.permits_write(url, "POST", urlencode({"content": json.dumps([payload])})))
        self.guard.checkout_sent = True
        self.assertFalse(self.guard.permits_write(url, "POST", body))

    def test_other_mutations_never_fit_the_cart_scope(self):
        self.guard.arm("add")
        for path in ("/webshop/call/account/linknewaccount", "/webshop/call/cart/resetcart",
                     "/webshop/reservations2/apiusereventgroups", "/webshop/call/cart/paylater"):
            self.assertFalse(self.guard.permits_write(BASE_URL + path, "POST", json.dumps(self.add)))

    def test_provider_posts_require_actual_merchant_payment_form(self):
        guard = _BookingGuard(None, spec("verkkomaksu"))
        guard.price, guard.payment_type_id, guard.balance_cents = Decimal(76), 4, 0
        top = SimpleNamespace(main_frame=SimpleNamespace(url="https://payment.example/start"))
        frame = SimpleNamespace(url="about:blank", page=top)
        bank_post = SimpleNamespace(url="https://bank.example/authenticate", method="POST",
                                    redirected_from=None, frame=frame, resource_type="document")
        response = SimpleNamespace(url=BASE_URL + "/webshop/call/cart/externalpaymentstart",
                                   json=lambda: {"result": {"form": '<form id="payment_form" action="https://payment.example/start"></form>'}})
        guard.response(response)
        self.assertFalse(guard._provider_request(bank_post))
        options = {"return_url": "/webshop/reservations2/reservations/9/2/6/-/123?doPayment=success",
                   "cancel_url": "/webshop/reservations2/reservations/9/2/6/-/123?doPayment=cancel",
                   "balance_amount": None, "payment_type_id": 4}
        request = SimpleNamespace(url=response.url, method="POST",
                                  post_data=urlencode({"content": json.dumps([options])}))
        sent = []
        guard.arm("checkout")
        guard.route(SimpleNamespace(request=request, continue_=lambda: sent.append(True),
                                    abort=lambda reason: sent.append(False)))
        self.assertEqual(sent, [True])
        self.assertFalse(guard._provider_request(bank_post))
        guard.response(response)
        self.assertTrue(guard._provider_request(bank_post))
        merchant_return = SimpleNamespace(url=BASE_URL + options["return_url"] + "&receipt_id=77", method="POST",
                                          redirected_from=None, frame=frame, resource_type="document")
        self.assertTrue(guard._provider_request(merchant_return))
        merchant_return.url = BASE_URL + "/webshop/call/cart/resetcart"
        self.assertFalse(guard._provider_request(merchant_return))
        other = SimpleNamespace(url="https://unrelated.example/write", method="POST", redirected_from=None,
                                frame=SimpleNamespace(url=BASE_URL, page=SimpleNamespace(main_frame=SimpleNamespace(url=BASE_URL))),
                                resource_type="xhr")
        self.assertFalse(guard._provider_request(other))

    def test_armed_requests_are_blocked_at_the_deadline(self):
        past = datetime.now(UTC) - timedelta(seconds=1)
        guard = _BookingGuard(None, replace(spec(), opens_at=past - timedelta(minutes=1), deadline=past))
        guard.child_id, guard.product_id, guard.price = 20, 4321, Decimal(76)
        guard.arm("add")
        calls = []
        request = SimpleNamespace(url=self.add_url, method="POST", post_data=json.dumps(self.add))
        guard.route(SimpleNamespace(request=request, continue_=lambda: calls.append("sent"),
                                    abort=lambda _reason: calls.append("blocked")))
        self.assertEqual(calls, ["blocked"])
        self.assertFalse(guard.actual_sent)

    def test_queue_scope_allows_only_the_exact_child_course_and_status_one(self):
        self.guard.arm("queue")
        url = BASE_URL + "/webshop/reservations2/apiusereventgroups"
        data = {"reservation_event_group_id": 123, "reservation_status_id": 1,
                "account": {"account_id": 20, "name": "Aino Esimerkki"}, "tags": {}}
        self.assertTrue(self.guard.permits_write(url, "POST", json.dumps(data)))
        for change in ({"reservation_status_id": 2}, {"reservation_status_id": 3},
                       {"reservation_status_id": 7}, {"reservation_event_group_id": 124},
                       {"account": {"account_id": 10}}, {"account": {"account_id": 20, "name": "Eino Esimerkki"}},
                       {"quantity": 1}, {"price": 7600}, {"payment_type_id": 5}):
            with self.subTest(change=change):
                self.assertFalse(self.guard.permits_write(url, "POST", json.dumps(data | change)))
        self.assertFalse(self.guard.permits_write(self.add_url, "POST", json.dumps(self.add)))
        calls = []
        request = SimpleNamespace(url=url, method="POST", post_data=json.dumps(data))
        self.guard.route(SimpleNamespace(request=request, continue_=lambda: calls.append("sent"),
                                         abort=lambda reason: calls.append("blocked")))
        self.guard.disarm()
        self.guard.arm("queue")
        self.assertTrue(self.guard.queue_sent)
        self.assertFalse(self.guard.actual_sent)
        self.assertFalse(self.guard.permits_write(url, "POST", json.dumps(data)))
        self.assertEqual(calls, ["sent"])


class ServerEvidenceTests(unittest.TestCase):
    def observe(self, *, cart=None, reservations=None, receipt=None, row=None):
        return _observation(spec(), 20, row or course_row(), cart or [],
                            reservations or [], receipt, {"isLocked": False})

    def test_cart_hold_is_not_a_confirmed_reservation(self):
        value = self.observe(cart=[cart_row()], reservations=[reservation()])
        self.assertTrue(value.held)
        self.assertFalse(value.reservation_confirmed)
        self.assertFalse(value.cart_empty)
        self.assertEqual(value.reservation_id, "456")

    def test_observed_pending_course_cart_does_not_infer_reservation_payment(self):
        cart, own = observed_hold()
        unpaid = replace(spec(), payment_mode="none", without_payment=True)
        value = _observation(unpaid, 20, course_row(), [cart], [own], False, {"isLocked": False})
        self.assertEqual((value.reservation_id, value.current_price), ("456", Decimal(76)))
        self.assertIsNone(value.reservation_paid)
        self.assertFalse(value.unpaid_hold)
        self.assertTrue(value.unpaid_hold_without_checkout)
        paid = self.observe(cart=[cart], reservations=[own])
        self.assertFalse(paid.unpaid_hold)
        self.assertFalse(paid.unpaid_hold_without_checkout)

    def test_cart_payment_event_contradicts_legacy_paid_false(self):
        cart, own = observed_hold()
        for bad in (222, 0, False, "", [], {}):
            with self.subTest(value=bad):
                value = self.observe(cart=[cart | {"payment_event_id": bad}],
                                     reservations=[own | {"is_paid": False}])
                self.assertFalse(value.unpaid_hold)

    def test_observed_pending_course_cart_requires_nonconflicting_evidence(self):
        cart, own = observed_hold()
        unpaid = replace(spec(), payment_mode="none", without_payment=True)
        cases = (
            ("missing payment event", [{key: value for key, value in cart.items() if key != "payment_event_id"}], [own], False),
            ("payment event exists", [cart | {"payment_event_id": 223}], [own], False),
            ("malformed payment event", [cart | {"payment_event_id": []}], [own], False),
            ("wrong product", [cart | {"fare_product_id": 999}], [own], False),
            ("contradictory cart course", [cart | {"reservation_event_group_id": 999}], [own], False),
            ("wrong account", [cart], [own | {"reservation_account_id": 10}], False),
            ("multiple cart lines", [cart, cart], [own], False),
            ("no reservation", [cart], [], False),
            ("different status", [cart], [own | {"reservation_status_id": 4}], False),
            ("explicit paid", [cart], [own | {"is_paid": True}], False),
            ("malformed paid field", [cart], [own | {"is_paid": None}], False),
            ("conflicting linked receipt", [cart], [own | {"receipt_event_id": 800}], False),
            ("locked cart", [cart], [own], True),
        )
        for label, cart_rows, own_rows, locked in cases:
            with self.subTest(label=label):
                value = _observation(unpaid, 20, course_row(), cart_rows, own_rows, False, {"isLocked": locked})
                self.assertFalse(value.unpaid_hold_without_checkout)
        for own_rows in ([own, own | {"reservation_id": 457}], [own | {"reservation_id": 0}]):
            with self.assertRaises(BookingError):
                _observation(unpaid, 20, course_row(), [cart], own_rows, False, {"isLocked": False})

    def test_selected_reservation_id_is_unique_across_course_response(self):
        cart, own = observed_hold()
        unpaid = replace(spec(), payment_mode="none", without_payment=True)
        for conflicting in ({"reservation_account_id": 21}, {"reservation_event_group_id": 124},
                            {"reservation_status_id": 1}, {"reservation_status_id": None}):
            with self.subTest(conflicting=conflicting), self.assertRaises(BookingError):
                _observation(unpaid, 20, course_row(), [cart], [own, own | conflicting],
                             False, {"isLocked": False})

    def test_queue_and_unpaid_reservations_are_not_complete(self):
        for status, paid in ((1, 1), (3, 1), (7, 1), (6, 1), (2, 0), (4, 0), (5, 0)):
            with self.subTest(status=status, paid=paid):
                self.assertFalse(self.observe(reservations=[reservation(status, paid)]).reservation_confirmed)
        for status in (2, 4, 5):
            self.assertTrue(self.observe(reservations=[reservation(status, 1)]).reservation_confirmed)

    def test_only_exact_childs_cart_is_held(self):
        value = self.observe(cart=[cart_row(child=10)])
        self.assertFalse(value.held)
        self.assertTrue(value.cart_has_others)
        self.assertFalse(self.observe(reservations=[reservation(4, 1, child=10)]).reservation_confirmed)

    def test_final_group_status_without_paid_field_does_not_confirm_payment(self):
        own_reservation = reservation(4, 1)
        del own_reservation["is_paid"]
        receipt = {"receipt_id": 78, "total_amount": 7600, "is_closed": 1, "is_pay_later": 0}
        value = self.observe(reservations=[own_reservation], receipt=receipt)
        self.assertFalse(value.reservation_confirmed)
        self.assertTrue(value.receipt_closed)
        self.assertEqual(value.receipt_id, "78")

    def test_reservation_and_session_receipt_identifiers_are_independent(self):
        own = reservation(4, 1) | {"receipt_event_id": "79"}
        receipt = {"receipt_id": 78, "total_amount": 7600, "is_closed": 1, "is_pay_later": 0}
        value = _observation(spec(), 20, course_row(), [], [own], receipt, {"isLocked": False},
                             base_url="http://127.0.0.1:43210")
        self.assertEqual(value.reservation_receipt_id, "79")
        self.assertEqual(value.receipt_id, "78")
        for invalid in (None, 0, -1, True, "unknown"):
            with self.subTest(invalid=invalid):
                value = _observation(spec(), 20, course_row(), [], [own | {"receipt_event_id": invalid}],
                                     receipt, {"isLocked": False}, base_url="http://127.0.0.1:43210")
                self.assertIsNone(value.reservation_receipt_id)

    def test_foreign_product_for_same_course_and_child_is_not_a_hold(self):
        value = self.observe(cart=[cart_row() | {"fare_product_id": 999}], reservations=[reservation()])
        self.assertFalse(value.held)
        self.assertTrue(value.cart_has_others)

    def test_held_price_over_cap_stops_reconciliation(self):
        with self.assertRaises(BookingError):
            self.observe(cart=[cart_row(price=8100)], reservations=[reservation()])

    def test_changed_weekly_schedule_is_not_the_approved_course(self):
        row = course_row() | {"weekdays": [{"weekdays": [0], "times": "11:30 - 12:00"},
                                          {"weekdays": [2], "times": "13:00 - 13:30"}]}
        approved = replace(spec(), course=parse_course(row))
        changed = row | {"weekdays": [{"weekdays": [2], "times": "11:30 - 12:00"},
                                      {"weekdays": [0], "times": "13:00 - 13:30"}]}
        with self.assertRaises(BookingError):
            _observation(approved, 20, changed, [], [], None, {"isLocked": False})

    def test_second_line_or_quantity_two_blocks_the_purchase(self):
        for cart in ([cart_row(), cart_row()], [dict(cart_row(), quantity=2)]):
            value = self.observe(cart=cart)
            self.assertFalse(value.held)
            self.assertTrue(value.cart_has_others)

    def test_held_price_comes_from_server_cart_not_catalogue(self):
        value = self.observe(cart=[cart_row(price=7000)])
        self.assertEqual(value.current_price, Decimal(70))

    def test_only_closed_paid_receipt_is_closed(self):
        for closed, pay_later in ((0, 0), (1, 1), (None, 0), (1, None)):
            receipt = {"receipt_id": 77, "total_amount": 7600, "is_closed": closed, "is_pay_later": pay_later}
            with self.subTest(closed=closed, pay_later=pay_later):
                self.assertFalse(self.observe(receipt=receipt).receipt_closed)
        receipt = {"receipt_id": 77, "total_amount": 7600, "is_closed": 1, "is_pay_later": 0}
        value = self.observe(receipt=receipt)
        self.assertTrue(value.receipt_closed)
        self.assertEqual(value.receipt_id, "77")
        self.assertEqual(value.receipt_total, Decimal(76))

    def test_changed_target_and_unknown_shapes_fail_closed(self):
        for change in ({"reservation_event_group_id": 124}, {"location": "Kallio"},
                       {"start": "2027-01-11 10:00:00"}, {"name": "Alkeisuimakoulu"}):
            with self.subTest(change=change), self.assertRaises(BookingError):
                self.observe(row=course_row() | change)
        with self.assertRaises(BookingError):
            _observation(spec(), 20, course_row(), {}, [], None, {"isLocked": False})
        with self.assertRaises(BookingError):
            self.observe(receipt={"receipt_id": 77, "total_amount": "unknown"})

    def test_own_queue_is_separate_from_actual_place_and_payment(self):
        for status in (1, 3):
            entry = reservation(status, 0) | {"queue_number": 3}
            value = self.observe(row=course_row() | {"free": 0, "free_queue": 2}, reservations=[entry])
            self.assertTrue(value.queued)
            self.assertTrue(value.queue_available)
            self.assertEqual(value.queue_reservation_id, "456")
            self.assertEqual(value.queue_position, 3)
            self.assertIsNone(value.reservation_id)
            self.assertFalse(value.reservation_confirmed)
            self.assertFalse(value.available)
            self.assertFalse(value.held)
            self.assertTrue(value.cart_empty)

    def test_unknown_queue_position_stays_unknown_and_wrong_child_is_ignored(self):
        for position in (None, 0, "unknown"):
            value = self.observe(reservations=[reservation(1, 0) | {"queue_number": position}])
            self.assertTrue(value.queued)
            self.assertIsNone(value.queue_position)
        value = self.observe(reservations=[reservation(1, 0, child=10) | {"queue_number": 1}])
        self.assertFalse(value.queued)
        self.assertIsNone(value.queue_reservation_id)


class QueueMutationPreconditionsTests(unittest.TestCase):
    def backend(self, state):
        backend = BookingBrowser(spec(), None, None, _BookingGuard(None, spec()))
        backend.snapshot = Mock(return_value=state)
        return backend

    def test_existing_queue_requires_no_new_request(self):
        backend = self.backend(Observation(queued=True, queue_reservation_id="456", queue_position=2))
        backend.join_queue()
        self.assertFalse(backend.guard.queue_sent)

    def test_real_place_or_incomplete_purchase_blocks_queue_mutation(self):
        for state in (Observation(available=True, queue_available=True),
                      Observation(held=True, queue_available=True, cart_empty=False),
                      Observation(reservation_id="456", queue_available=True),
                      Observation(payment_locked=True, queue_available=True),
                      Observation(queue_available=False)):
            with self.subTest(state=state):
                backend = self.backend(state)
                with self.assertRaises(ActionNotSent):
                    backend.join_queue()
                self.assertFalse(backend.guard.queue_sent)

    def test_possibly_sent_queue_request_is_never_rearmed(self):
        backend = self.backend(Observation(queue_available=True))
        backend.guard.queue_sent = True
        with self.assertRaises(ActionUncertain):
            backend.join_queue()

    def test_actual_request_already_sent_blocks_new_queue_action(self):
        backend = self.backend(Observation(queue_available=True))
        backend.guard.actual_sent = True
        with self.assertRaises(ActionNotSent):
            backend.join_queue()
        self.assertFalse(backend.guard.queue_sent)

    def test_transient_read_before_queue_action_is_proven_unsent(self):
        backend = self.backend(Observation(queue_available=True))
        def interrupted_read():
            raise TransientBookingError("HTTP 503")
        backend.snapshot = interrupted_read
        with self.assertRaises(ActionNotSent):
            backend.join_queue()
        self.assertFalse(backend.guard.sent)
        self.assertFalse(backend.guard.queue_sent)


class ReadOnlyPreflightTests(unittest.TestCase):
    def test_preflight_conflicted_reservation_cannot_be_rebound_to_replacement(self):
        selected = spec()
        page = SimpleNamespace(goto=lambda *args, **kwargs: None)
        backend = BookingBrowser(selected, None, page, _BookingGuard(None, selected))
        cart, own = observed_hold()
        values = {
            "/webshop/reservations2/apieventgroups/123": course_row(),
            "/webshop/reservations2/apicart": [cart | {"payment_event_id": 78}],
            "/webshop/reservations2/apiusereventgroups": [own | {"is_paid": False}],
            "cart/getreceiptinfo": False, "cart/islocked": {"isLocked": False},
        }
        backend._read = Mock(side_effect=lambda path, **kwargs: values[path])
        backend._user = Mock(return_value={"account_id": 10, "linked_accounts": [
            {"account_id": 20, "name": "Aino Esimerkki"},
        ]})
        backend.initialize()
        values["/webshop/reservations2/apicart"] = []
        values["/webshop/reservations2/apiusereventgroups"] = [reservation(4, 1) | {"reservation_id": 457}]
        current = selected.opens_at

        def wait(seconds: float) -> None:
            nonlocal current
            current += timedelta(seconds=seconds)

        backend.wait = Mock(side_effect=wait)
        bounded = replace(selected, deadline=current + timedelta(seconds=4))
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "booking.json"
            result = run_booking(bounded, backend, state_path, now=lambda: current, log=lambda _: None)
            self.assertEqual((result.status, result.reservation_id, result.receipt_id), ("deadline", "456", None))
            self.assertEqual(json.loads(state_path.read_text())["reservation_id"], "456")
        self.assertFalse(backend.guard.checkout_sent)

    def test_snapshot_keeps_preflight_payment_conflict_for_the_controller(self):
        selected = spec()
        cart, own = observed_hold()
        backend = BookingBrowser(selected, None, None, _BookingGuard(None, selected))
        backend.child_id = 20
        values = {
            "/webshop/reservations2/apieventgroups/123": course_row(),
            "/webshop/reservations2/apicart": [cart | {"payment_event_id": 78}],
            "/webshop/reservations2/apiusereventgroups": [own | {"is_paid": False}],
            "cart/getreceiptinfo": False, "cart/islocked": {"isLocked": False},
        }
        backend._read = Mock(side_effect=lambda path, **kwargs: values[path])
        self.assertTrue(backend.snapshot().hold_payment_conflict)
        values["/webshop/reservations2/apicart"] = [cart]
        self.assertTrue(backend.snapshot().hold_payment_conflict)
        self.assertFalse(backend.snapshot().unpaid_hold)

    def test_loopback_payment_proof_rejects_public_paid_fields_and_missing_receipt_key(self):
        _, own = observed_hold()
        loopback = "http://127.0.0.1:43210"
        backend = BookingBrowser(spec(), None, None, _BookingGuard(None, spec(), base_url=loopback),
                                 base_url=loopback)
        normal = {"reservation_id": 456, "reservation_status_id": 7,
                  "is_paid": 0, "receipt_event_id": None}
        public_claim = own | {"is_paid": True, "receipt_event_id": 78}
        for rows, proof in (([public_claim], [normal]),
                            ([public_claim], [normal | {"reservation_id": 457}]),
                            ([own], [{key: value for key, value in normal.items() if key != "receipt_event_id"}])):
            with self.subTest(rows=rows, proof=proof):
                backend._read = Mock(side_effect=[rows, {"testshop": True, "reservations": proof}])
                with self.assertRaises(BookingError):
                    backend._reservations({123})

    def test_simulated_proof_is_read_only_on_loopback_and_bound_to_observed_status(self):
        production = BASE_URL
        loopback = "http://127.0.0.1:43210"
        proof_path = "/_test/reservation-proof"
        _, own = observed_hold()
        calls = []
        proof_status = 7

        def get(url, **options):
            calls.append(url)
            payload = [own]
            if url.endswith(proof_path):
                payload = {"testshop": True, "reservations": [{
                    "reservation_id": 456, "reservation_status_id": proof_status,
                    "is_paid": 0, "receipt_event_id": None,
                }]}
            return SimpleNamespace(status=200, headers={}, json=lambda: payload, dispose=lambda: None)

        context = SimpleNamespace(request=SimpleNamespace(get=get))
        live = BookingBrowser(spec(), context, None, _BookingGuard(context, spec()))
        with self.assertRaises(BookingError):
            live._read(proof_path)
        self.assertEqual(calls, [])
        local = BookingBrowser(spec(), context, None, _BookingGuard(context, spec(), base_url=loopback),
                               base_url=loopback)
        self.assertEqual(local._read(proof_path)["reservations"][0]["reservation_id"], 456)
        self.assertEqual(local._reservations({123})[0]["is_paid"], 0)
        self.assertEqual(calls, [
            loopback + proof_path, loopback + "/webshop/reservations2/apiusereventgroups", loopback + proof_path,
        ])
        calls.clear()
        self.assertEqual(live._reservations({123}), [own])
        self.assertEqual(local._reservations(set()), [own])
        self.assertEqual(local._reservations({124}), [own])
        self.assertEqual(calls, [production + "/webshop/reservations2/apiusereventgroups",
                                 loopback + "/webshop/reservations2/apiusereventgroups",
                                 loopback + "/webshop/reservations2/apiusereventgroups"])
        proof_status = 4
        self.assertEqual(local._reservations({123}), [own])

    def test_rpc_no_arguments_follow_public_serializer(self):
        calls = []
        def post(url, *, form, **options):
            calls.append((url.split("/call/")[1], form["content"]))
            return SimpleNamespace(status=200, headers={}, json=lambda: {"result": None}, dispose=lambda: None)
        context = SimpleNamespace(request=SimpleNamespace(post=post))
        backend = BookingBrowser(spec(), context, None, _BookingGuard(context, spec()))
        backend._read("cart/getreceiptinfo", rpc=True)
        backend._read("cart/islocked", rpc=True)
        backend._read("user/getcurrentuser", rpc=True, content=[True, True])
        self.assertEqual(calls, [("cart/getreceiptinfo", '""'), ("cart/islocked", '""'),
                                 ("user/getcurrentuser", "[true, true]")])

    def test_transient_add_refresh_remains_retryable_without_a_send(self):
        backend = BookingBrowser(spec(), None, None, _BookingGuard(None, spec()))
        backend.snapshot = Mock(side_effect=TransientBookingError("HTTP 503", retry_after=7))
        with self.assertRaises(ActionNotSent) as error:
            backend.add_selected_course()
        self.assertEqual(error.exception.retry_after, 7)
        self.assertFalse(backend.guard.actual_sent)

    def test_transient_payment_type_refresh_remains_retryable_without_a_send(self):
        selected = replace(spec("fastpay"), payment_type_id=5)
        backend = BookingBrowser(selected, None, None, _BookingGuard(None, selected))
        backend.snapshot = Mock(return_value=Observation(held=True, cart_empty=False, reservation_id="456", reservation_paid=False,
                                                          current_price=Decimal(76)))
        backend._payment_types = Mock(side_effect=TransientBookingError("HTTP 503", retry_after=8))
        with self.assertRaises(ActionNotSent) as error:
            backend.checkout("456", Decimal(76))
        self.assertEqual(error.exception.retry_after, 8)
        self.assertFalse(backend.guard.actual_sent)

    def test_fresh_own_reservation_blocks_add_even_without_cart(self):
        backend = BookingBrowser(spec(), None, None, _BookingGuard(None, spec()))
        backend.snapshot = Mock(return_value=Observation(available=True, current_price=Decimal(76),
                                                          reservation_id="456"))
        backend._participant_action = Mock(side_effect=AssertionError("must stop before entering the UI action"))
        with self.assertRaises(ActionNotSent):
            backend.add_selected_course()
        backend._participant_action.assert_not_called()

    def test_reservation_appearing_while_preparing_participant_blocks_submit(self):
        backend = BookingBrowser(spec(), None, None, _BookingGuard(None, spec()))
        available = Observation(available=True, current_price=Decimal(76))
        backend.snapshot = Mock(side_effect=[available, replace(available, reservation_id="456")])
        button = SimpleNamespace(click=Mock())
        backend._participant_action = Mock(return_value=button)
        backend.page = SimpleNamespace(expect_response=lambda *args, **kwargs: nullcontext())
        with self.assertRaises(ActionNotSent):
            backend.add_selected_course()
        button.click.assert_not_called()

    def test_a_paid_reservation_with_a_cart_does_not_start_checkout(self):
        backend = BookingBrowser(spec(), None, None, _BookingGuard(None, spec()))
        backend.snapshot = Mock(return_value=Observation(held=True, cart_empty=False, reservation_id="456",
                               reservation_confirmed=True, current_price=Decimal(76)))
        backend._payment_types = Mock(side_effect=AssertionError("must stop before fetching payment methods"))
        with self.assertRaises(ActionNotSent):
            backend.checkout("456", Decimal(76))
        backend._payment_types.assert_not_called()
        self.assertFalse(backend.guard.actual_sent)

    def test_http_503_is_retried_once_with_retry_after_before_ready(self):
        values = [
            SimpleNamespace(status=503, headers={"retry-after": "3"}, dispose=lambda: None),
            SimpleNamespace(status=200, headers={}, json=course_row, dispose=lambda: None),
        ]
        urls = []
        def get(url, **options):
            urls.append(url)
            return values.pop(0)
        context = SimpleNamespace(request=SimpleNamespace(get=get))
        backend = BookingBrowser(spec(), context, None, _BookingGuard(context, spec()))
        clock = [datetime(2027, 1, 1, tzinfo=UTC)]
        waits = []
        def wait(seconds):
            waits.append(seconds)
            clock[0] += timedelta(seconds=seconds)
        row = _retry_preflight(lambda: backend._read("/webshop/reservations2/apieventgroups/123"),
                               wait, clock[0] + timedelta(minutes=1), now=lambda: clock[0])
        self.assertEqual(row["reservation_event_group_id"], 123)
        self.assertEqual(waits, [3])
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(url.endswith("/apieventgroups/123") for url in urls))

    def test_resuming_existing_purchase_never_requires_spent_balance(self):
        for state in (
            Observation(reservation_confirmed=True, reservation_id="456", current_price=Decimal(76)),
            Observation(held=True, cart_empty=False, reservation_id="456", reservation_paid=False, current_price=Decimal(76)),
            Observation(payment_locked=True, current_price=Decimal(76)),
            Observation(reservation_id="456", current_price=Decimal(76)),
        ):
            with self.subTest(state=state):
                page = SimpleNamespace(goto=lambda *args, **kwargs: None)
                backend = BookingBrowser(spec(), None, page, _BookingGuard(None, spec()))
                backend._user = Mock(return_value={"account_id": 10, "linked_accounts": [{"account_id": 20, "name": "Aino Esimerkki"}]})
                backend.snapshot = Mock(return_value=state)
                def no_new_payment():
                    self.fail("Existing purchase reconciliation must not require any new payment method or balance")
                backend._payment_types = Mock(side_effect=no_new_payment)
                backend.initialize()
                self.assertEqual(backend.identity, "10:20:123")


class FastPaySelectionTests(unittest.TestCase):
    @staticmethod
    def payment(payment_id, name, **extra):
        return {"payment_type_id": payment_id, "name": name, "is_balance": False,
                "is_pay_later": False, "is_disabled": False} | extra

    def backend(self, methods, payment_id=None):
        selected = replace(spec("fastpay"), payment_type_id=payment_id)
        backend = BookingBrowser(selected, None, None, _BookingGuard(None, selected))
        backend._payment_types = Mock(return_value=methods)
        return backend

    def test_only_branded_fastpay_is_chosen_among_external_methods(self):
        backend = self.backend([
            self.payment(3, "Verkkopankki"),
            self.payment(5, "<strong>FAST</strong>&nbsp;Pay (Visa •••• 1234)"),
            self.payment(8, "Korttimaksu"),
        ])
        output = io.StringIO()
        with redirect_stdout(output):
            backend._select_payment(Decimal(76))
        self.assertEqual(backend.payment_type_id, 5)
        self.assertEqual(backend.payment_type_name, "FAST Pay (Visa •••• 1234)")
        self.assertIn("tunnus 5", output.getvalue())

    def test_explicit_unrelated_id_cannot_override_fastpay_mode(self):
        backend = self.backend([self.payment(3, "Korttimaksu"), self.payment(5, "FastPay")], payment_id=3)
        with self.assertRaisesRegex(BookingError, "FastPay"):
            backend._select_payment(Decimal(76))

    def test_disabled_balance_paylater_or_similar_names_do_not_match(self):
        for method in (
            self.payment(5, "FastPay", is_disabled=True),
            self.payment(5, "FastPay", is_balance=True),
            self.payment(5, "FastPay", is_pay_later=True),
            self.payment(5, "FastPayPal"), self.payment(5, "Ei FastPay"),
            self.payment(5, '<span title="FastPay">Korttimaksu</span>'),
        ):
            with self.subTest(method=method):
                self.assertFalse(_eligible_payment(method, "fastpay"))
                with self.assertRaises(BookingError):
                    self.backend([method])._select_payment(Decimal(76))

    def test_checkout_revalidates_brand_even_if_method_id_is_unchanged(self):
        backend = self.backend([self.payment(5, "Tavallinen korttimaksu")], payment_id=5)
        backend._recent_observation = lambda: Observation(held=True, cart_empty=False, reservation_id="456", reservation_paid=False, current_price=Decimal(76))
        with self.assertRaises(BookingError):
            backend.checkout("456", Decimal(76))
        self.assertFalse(backend.guard.checkout_sent)

    def test_fastpay_card_authentication_uses_authorized_provider_flow(self):
        guard = _BookingGuard(None, spec("fastpay"))
        guard.checkout_sent = True
        guard.response(SimpleNamespace(
            url=BASE_URL + "/webshop/call/cart/externalpaymentstart",
            json=lambda: {"result": {"form": '<form id="payment_form" action="https://payment.example/authenticate"></form>'}},
        ))
        self.assertEqual(guard.provider_origins, {"https://payment.example"})


    def test_rendered_cart_payment_event_blocks_checkout_and_preserves_the_conflict(self):
        selected = replace(spec("fastpay"), payment_type_id=5)
        backend = BookingBrowser(selected, None, None, _BookingGuard(None, selected))
        backend.child_id = 20
        state = Observation(held=True, cart_empty=False, reservation_id="456",
                            reservation_paid=False, current_price=Decimal(76))
        backend.snapshot = Mock(return_value=state)
        backend._payment_types = Mock(return_value=[{
            "payment_type_id": 5, "name": "FastPay", "is_disabled": False,
            "is_balance": False, "is_pay_later": False,
        }])
        action = Mock()
        action.count.return_value = 1
        action.is_checked.return_value = False
        action.is_visible.return_value = action.is_enabled.return_value = True
        action.evaluate.return_value = False

        def simulated_click():
            backend.guard.sent = True
            backend.guard.checkout_sent = True

        action.click.side_effect = simulated_click
        page = Mock(url=BASE_URL + "/webshop/reservations2/")
        page.locator.return_value = action
        page.evaluate.side_effect = [None, [cart_row() | {"payment_event_id": 78}], 9]
        page.expect_response.return_value = nullcontext()
        backend.page = page

        with self.assertRaises(ActionNotSent) as failure:
            backend.checkout("456", Decimal(76))
        self.assertEqual(failure.exception.observation, replace(state, hold_payment_conflict=True))
        self.assertFalse(backend.guard.checkout_sent)
        action.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
