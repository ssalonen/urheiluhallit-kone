"""Offline checks for irreversible-operation boundaries and server evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal
import io
import json
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from urllib.parse import urlencode

from uhkone.booking import ActionNotSent, ActionUncertain, BookingError, BookingSpec, Observation, TransientBookingError
from uhkone.booking_browser import BookingBrowser, _BookingGuard, _observation, _retry_preflight, _eligible_payment
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
    opening = datetime(2027, 1, 1, tzinfo=timezone.utc)
    return BookingSpec(parse_course(course_row()), "Aino Esimerkki", Decimal("80"),
                       opening, opening + timedelta(minutes=30), mode)


def cart_row(child=20, price=7600):
    return {"item_id": 11, "fare_product_id": 4321, "quantity": 1, "price": price,
            "reservation_params": {"reservation_event_group_id": 123, "reservation_account_id": child}}


def reservation(status=7, paid=0, child=20):
    return {"reservation_id": 456, "reservation_event_group_id": 123,
            "reservation_account_id": child, "reservation_status_id": status, "is_paid": paid}


class ScopeGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = _BookingGuard(None, spec())
        self.guard.child_id, self.guard.payer_id = 20, 10
        self.guard.product_id, self.guard.price = 4321, Decimal("76")
        self.add = {"reservation_event_group_id": 123, "fare_product_id": 4321,
                    "quantity": 1, "price": 7600, "title": "Kurssi",
                    "account": {"account_id": "20", "name": "Aino Esimerkki"},
                    "tags": {}, "campaign_code": None}
        self.add_url = BASE_URL + "/webshop/reservations2/apicart"

    def test_readiness_never_allows_an_unarmed_cart_write(self):
        self.assertFalse(self.guard.permits_write(self.add_url, "POST", json.dumps(self.add)))

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
                       {"return_url": "https://example.org/?doPayment=success"}):
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
        guard.price, guard.payment_type_id, guard.balance_cents = Decimal("76"), 4, 0
        top = SimpleNamespace(main_frame=SimpleNamespace(url="https://payment.example/start"))
        frame = SimpleNamespace(url="about:blank", page=top)
        bank_post = SimpleNamespace(url="https://bank.example/authenticate", method="POST",
                                    redirected_from=None, frame=frame, resource_type="document")
        response = SimpleNamespace(url=BASE_URL + "/webshop/call/cart/externalpaymentstart",
                                   json=lambda: {"result": {"form": '<form id="payment_form" action="https://payment.example/start"></form>'}})
        guard.response(response)
        self.assertFalse(guard._provider_request(bank_post))
        options = {"return_url": BASE_URL + "/webshop/reservations2/?doPayment=success",
                   "cancel_url": BASE_URL + "/webshop/reservations2/?doPayment=cancel",
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
        merchant_return = SimpleNamespace(url=options["return_url"] + "&receipt_id=77", method="POST",
                                          redirected_from=None, frame=frame, resource_type="document")
        self.assertTrue(guard._provider_request(merchant_return))
        merchant_return.url = BASE_URL + "/webshop/call/cart/resetcart"
        self.assertFalse(guard._provider_request(merchant_return))
        other = SimpleNamespace(url="https://unrelated.example/write", method="POST", redirected_from=None,
                                frame=SimpleNamespace(url=BASE_URL, page=SimpleNamespace(main_frame=SimpleNamespace(url=BASE_URL))),
                                resource_type="xhr")
        self.assertFalse(guard._provider_request(other))

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

    def test_final_group_status_without_optional_paid_field_uses_receipt_evidence(self):
        own_reservation = reservation(4, 1)
        del own_reservation["is_paid"]
        receipt = {"receipt_id": 78, "total_amount": 7600, "is_closed": 1, "is_pay_later": 0}
        value = self.observe(reservations=[own_reservation], receipt=receipt)
        self.assertTrue(value.reservation_confirmed)
        self.assertTrue(value.receipt_closed)
        self.assertEqual(value.receipt_id, "78")

    def test_second_line_or_quantity_two_blocks_the_purchase(self):
        for cart in ([cart_row(), cart_row()], [dict(cart_row(), quantity=2)]):
            value = self.observe(cart=cart)
            self.assertFalse(value.held)
            self.assertTrue(value.cart_has_others)

    def test_held_price_comes_from_server_cart_not_catalogue(self):
        value = self.observe(cart=[cart_row(price=7000)])
        self.assertEqual(value.current_price, Decimal("70"))

    def test_only_closed_paid_receipt_is_closed(self):
        for closed, pay_later in ((0, 0), (1, 1), (None, 0), (1, None)):
            receipt = {"receipt_id": 77, "total_amount": 7600, "is_closed": closed, "is_pay_later": pay_later}
            with self.subTest(closed=closed, pay_later=pay_later):
                self.assertFalse(self.observe(receipt=receipt).receipt_closed)
        receipt = {"receipt_id": 77, "total_amount": 7600, "is_closed": 1, "is_pay_later": 0}
        value = self.observe(receipt=receipt)
        self.assertTrue(value.receipt_closed)
        self.assertEqual(value.receipt_id, "77")
        self.assertEqual(value.receipt_total, Decimal("76"))

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
        backend.snapshot = lambda: state
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
        clock = [datetime(2027, 1, 1, tzinfo=timezone.utc)]
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
            Observation(reservation_confirmed=True, reservation_id="456", current_price=Decimal("76")),
            Observation(held=True, cart_empty=False, current_price=Decimal("76")),
            Observation(payment_locked=True, current_price=Decimal("76")),
            Observation(reservation_id="456", current_price=Decimal("76")),
        ):
            with self.subTest(state=state):
                page = SimpleNamespace(goto=lambda *args, **kwargs: None)
                backend = BookingBrowser(spec(), None, page, _BookingGuard(None, spec()))
                backend._user = lambda: {"account_id": 10, "linked_accounts": [{"account_id": 20, "name": "Aino Esimerkki"}]}
                backend.snapshot = lambda: state
                def no_new_payment():
                    self.fail("Existing purchase reconciliation must not require any new payment method or balance")
                backend._payment_types = no_new_payment
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
        backend._payment_types = lambda: methods
        return backend

    def test_only_branded_fastpay_is_chosen_among_external_methods(self):
        backend = self.backend([
            self.payment(3, "Verkkopankki"),
            self.payment(5, "<strong>FAST</strong>&nbsp;Pay (Visa •••• 1234)"),
            self.payment(8, "Korttimaksu"),
        ])
        output = io.StringIO()
        with redirect_stdout(output):
            backend._select_payment(Decimal("76"))
        self.assertEqual(backend.payment_type_id, 5)
        self.assertEqual(backend.payment_type_name, "FAST Pay (Visa •••• 1234)")
        self.assertIn("tunnus 5", output.getvalue())

    def test_explicit_unrelated_id_cannot_override_fastpay_mode(self):
        backend = self.backend([self.payment(3, "Korttimaksu"), self.payment(5, "FastPay")], payment_id=3)
        with self.assertRaisesRegex(BookingError, "FastPay"):
            backend._select_payment(Decimal("76"))

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
                    self.backend([method])._select_payment(Decimal("76"))

    def test_checkout_revalidates_brand_even_if_method_id_is_unchanged(self):
        backend = self.backend([self.payment(5, "Tavallinen korttimaksu")], payment_id=5)
        backend._recent_observation = lambda: Observation(held=True, cart_empty=False, current_price=Decimal("76"))
        with self.assertRaises(BookingError):
            backend.checkout()
        self.assertFalse(backend.guard.checkout_sent)

    def test_fastpay_card_authentication_uses_authorized_provider_flow(self):
        guard = _BookingGuard(None, spec("fastpay"))
        guard.checkout_sent = True
        guard.response(SimpleNamespace(
            url=BASE_URL + "/webshop/call/cart/externalpaymentstart",
            json=lambda: {"result": {"form": '<form id="payment_form" action="https://payment.example/authenticate"></form>'}},
        ))
        self.assertEqual(guard.provider_origins, {"https://payment.example"})


if __name__ == "__main__":
    unittest.main()
