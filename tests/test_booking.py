"""Fake-clock transaction tests: no network, payment or browser operations."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from uhkone.booking import (
    ActionNotSent, ActionUncertain, BookingError, BookingSpec, Observation,
    TransientBookingError, run_booking,
)
from uhkone.models import Course


BASE = datetime(2030, 1, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.value = BASE

    def now(self):
        return self.value


class Backend:
    """A shop with controllable mutation outcomes and authoritative snapshots."""
    identity = "payer:child:123"

    def __init__(self, clock):
        self.clock = clock
        self.observation = Observation(
            available=True, current_price=Decimal("90"),
            receipt_id="old-receipt", receipt_closed=True, receipt_total=Decimal("90"),
        )
        self.waits = []
        self.snapshots = []
        self.adds = []
        self.checkouts = []
        self.errors = []
        self.on_snapshot = None
        self.on_add = None
        self.on_checkout = None

    def snapshot(self):
        if self.errors:
            raise self.errors.pop(0)
        if self.on_snapshot:
            self.on_snapshot(self)
        self.snapshots.append((self.clock.now(), self.observation))
        return self.observation

    def add_selected_course(self):
        self.adds.append(self.clock.now())
        if self.on_add:
            self.on_add(self)
        else:
            self.hold()

    def hold(self):
        self.observation = replace(self.observation, held=True, cart_empty=False)

    def checkout(self):
        self.checkouts.append(self.clock.now())
        if self.on_checkout:
            self.on_checkout(self)
        else:
            self.confirm()

    def confirm(self):
        self.observation = replace(
            self.observation, held=False, cart_empty=True, reservation_id="reservation-123",
            reservation_confirmed=True, receipt_id="new-receipt", receipt_closed=True,
            receipt_total=Decimal("90"),
        )

    def wait(self, seconds):
        if seconds <= 0:
            raise AssertionError("Busy loop: wait must be positive")
        self.waits.append(seconds)
        self.clock.value += timedelta(seconds=seconds)


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "booking.json"
        self.clock = Clock()
        self.backend = Backend(self.clock)
        self.spec = BookingSpec(
            course=Course(id=123, name="Leikki-ikäisten jatkouimakoulu", location="Mäkelänrinne"),
            child_name="Testi Lapsi", max_price=Decimal("100"), opens_at=BASE,
            deadline=BASE + timedelta(seconds=90), payment_mode="saldo",
        )
        self.logs = []

    def run_booking(self, spec=None, backend=None, path=None, **kwargs):
        return run_booking(
            spec or self.spec, backend or self.backend, path or self.path,
            now=self.clock.now, log=self.logs.append, **kwargs,
        )

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_future_opening_has_preflight_but_no_early_write_and_bounded_wait_chunks(self):
        spec = replace(self.spec, opens_at=BASE + timedelta(seconds=120), deadline=BASE + timedelta(seconds=180))
        result = self.run_booking(spec)
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(self.backend.snapshots[0][0], BASE)
        self.assertEqual(self.backend.adds, [spec.opens_at])
        self.assertGreaterEqual(self.backend.checkouts[0], spec.opens_at)
        self.assertLessEqual(max(self.backend.waits), 30)
        self.assertEqual([time for time, _ in self.backend.snapshots if time < BASE + timedelta(seconds=60)], [BASE])

    def test_transient_reads_back_off_and_honor_retry_after(self):
        self.backend.errors = [TransientBookingError("503"), TransientBookingError("429", retry_after=20)]
        result = self.run_booking()
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(self.backend.waits[:2], [2, 20])
        self.assertEqual(len(self.backend.adds), 1)
        self.assertEqual(len(self.backend.checkouts), 1)

    def test_uncertain_fastpay_is_reconciled_without_second_card_charge(self):
        spec = replace(self.spec, payment_mode="fastpay", payment_type_id=5)
        def lost_response(backend):
            backend.confirm()
            raise ActionUncertain("Kortti veloitettiin mutta vastaus katkesi")
        self.backend.on_checkout = lost_response
        result = self.run_booking(spec)
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(len(self.backend.checkouts), 1)
        self.assertEqual(self.state()["spec"]["payment_mode"], "fastpay")

    def test_phase_is_durable_before_each_mutation_and_state_is_private(self):
        def add(backend):
            self.assertEqual(self.state()["phase"], "adding")
            self.assertEqual(self.state()["attempts"]["add"], 1)
            backend.hold()

        def checkout(backend):
            saved = self.state()
            self.assertEqual(saved["phase"], "paying")
            self.assertEqual(saved["baseline_receipt_id"], "old-receipt")
            self.assertEqual(saved["expected_price"], "90")
            backend.confirm()

        self.backend.on_add, self.backend.on_checkout = add, checkout
        self.assertEqual(self.run_booking().status, "confirmed")
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_add_timeout_that_succeeded_is_observed_without_duplicate_add(self):
        def add(backend):
            backend.hold()
            raise TimeoutError("response lost after commit")

        self.backend.on_add = add
        result = self.run_booking()
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(len(self.backend.adds), 1)
        self.assertEqual(len(self.backend.checkouts), 1)

    def test_mutation_retry_after_delays_reconciliation_without_replaying_the_write(self):
        def add(backend):
            backend.hold()
            raise TransientBookingError("503 response after add", retry_after=25)

        self.backend.on_add = add
        self.assertEqual(self.run_booking().status, "confirmed")
        self.assertEqual(self.backend.waits[0], 25)
        self.assertEqual((len(self.backend.adds), len(self.backend.checkouts)), (1, 1))

    def test_late_commit_after_many_absent_reads_does_not_send_a_second_add(self):
        def add(backend):
            raise ActionUncertain("No response; commit remains pending")

        def snapshot(backend):
            if backend.adds and self.clock.now() >= BASE + timedelta(seconds=30):
                if not backend.checkouts:
                    backend.hold()

        self.backend.on_add = add
        self.backend.on_snapshot = snapshot
        self.assertEqual(self.run_booking().status, "confirmed")
        self.assertEqual(len(self.backend.adds), 1)
        self.assertEqual(len(self.backend.checkouts), 1)
        absent_times = [
            when for when, observed in self.backend.snapshots
            if BASE + timedelta(seconds=15) <= when < BASE + timedelta(seconds=30) and not observed.held
        ]
        self.assertGreaterEqual(len(absent_times), 2)

    def test_uncertain_add_is_never_resent_despite_long_absence_or_restart(self):
        self.backend.on_add = lambda backend: (_ for _ in ()).throw(TimeoutError("unknown add"))
        result = self.run_booking(replace(self.spec, deadline=BASE + timedelta(seconds=65)))
        self.assertEqual(result.status, "deadline")
        self.assertEqual(len(self.backend.adds), 1)
        self.assertEqual(self.backend.checkouts, [])
        self.assertEqual(self.state()["add_sends"], 1)
        result = self.run_booking(replace(self.spec, deadline=BASE + timedelta(seconds=120)))
        self.assertEqual(result.status, "deadline")
        self.assertEqual(len(self.backend.adds), 1)
        self.assertEqual(self.backend.checkouts, [])

    def test_legacy_journal_with_multiple_possible_sends_is_read_but_never_resent(self):
        self.backend.on_add = lambda backend: (_ for _ in ()).throw(TimeoutError("unknown add"))
        self.run_booking(replace(self.spec, deadline=BASE + timedelta(seconds=2)))
        for count in (2, 3):
            state = self.state()
            state["phase"] = "ready"
            state["attempts"]["add"] = state["add_sends"] = count
            self.path.write_text(json.dumps(state), encoding="utf-8")
            result = self.run_booking(replace(self.spec, deadline=self.clock.now() + timedelta(seconds=4)))
            self.assertEqual(result.status, "deadline")
            self.assertEqual(len(self.backend.adds), 1)
            self.assertEqual(self.backend.checkouts, [])

    def test_checkout_timeout_is_polled_until_confirmation_and_never_resent(self):
        self.backend.on_checkout = lambda backend: (_ for _ in ()).throw(ActionUncertain("payment response lost"))

        def snapshot(backend):
            if backend.checkouts and self.clock.now() >= BASE + timedelta(seconds=12):
                backend.confirm()

        self.backend.on_snapshot = snapshot
        result = self.run_booking()
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.reservation_id, "reservation-123")
        self.assertEqual(result.receipt_id, "new-receipt")
        self.assertEqual(len(self.backend.checkouts), 1)

    def test_restart_paying_preserves_old_receipt_and_never_repeats_checkout(self):
        def interrupt(backend):
            raise KeyboardInterrupt()

        self.backend.on_checkout = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_booking()
        self.assertEqual(self.state()["phase"], "paying")
        self.assertEqual(self.state()["baseline_receipt_id"], "old-receipt")
        # An exact confirmed booking plus the OLD closed receipt is insufficient.
        self.backend.observation = replace(
            self.backend.observation, held=False, cart_empty=True,
            reservation_confirmed=True, reservation_id="reservation-123",
        )

        def snapshot(backend):
            if self.clock.now() >= BASE + timedelta(seconds=12):
                backend.confirm()

        self.backend.on_snapshot = snapshot
        result = self.run_booking(replace(self.spec, deadline=BASE + timedelta(seconds=120)))
        self.assertEqual(result.status, "confirmed")
        self.assertGreaterEqual(self.clock.now(), BASE + timedelta(seconds=12))
        self.assertEqual(len(self.backend.checkouts), 1)

    def test_known_not_sent_can_retry_add_and_checkout(self):
        def add(backend):
            if len(backend.adds) == 1:
                raise ActionNotSent("not sent")
            backend.hold()

        def checkout(backend):
            if len(backend.checkouts) == 1:
                raise ActionNotSent("not sent")
            backend.confirm()

        self.backend.on_add, self.backend.on_checkout = add, checkout
        self.assertEqual(self.run_booking().status, "confirmed")
        self.assertEqual((len(self.backend.adds), len(self.backend.checkouts)), (2, 2))
        self.assertEqual(self.state()["attempts"], {"add": 2, "checkout": 2})
        self.assertEqual((self.state()["add_sends"], self.state()["checkout_sends"]), (1, 1))

    def test_wrong_price_unknown_price_or_unrelated_cart_blocks_all_writes(self):
        cases = (
            {"current_price": None}, {"current_price": Decimal("100.01")},
            {"current_price": Decimal("NaN")}, {"cart_has_others": True},
            {"cart_empty": False, "held": False},
        )
        for index, changes in enumerate(cases):
            backend = Backend(self.clock)
            backend.observation = replace(backend.observation, **changes)
            with self.subTest(changes=changes), self.assertRaises(BookingError):
                self.run_booking(backend=backend, path=self.path.parent / f"case-{index}.json")
            self.assertEqual((backend.adds, backend.checkouts), ([], []))

    def test_held_price_increase_blocks_checkout(self):
        def add(backend):
            backend.hold()
            backend.observation = replace(backend.observation, current_price=Decimal("101"))

        self.backend.on_add = add
        with self.assertRaises(BookingError):
            self.run_booking()
        self.assertEqual(len(self.backend.adds), 1)
        self.assertEqual(self.backend.checkouts, [])

    def test_preexisting_exact_confirmed_reservation_has_separate_status_without_payment_claim(self):
        self.backend.observation = replace(
            self.backend.observation, reservation_confirmed=True, reservation_id="existing",
            current_price=None,
        )
        result = self.run_booking()
        self.assertEqual((result.status, result.reservation_id, result.receipt_id), ("already_confirmed", "existing", None))
        self.assertEqual((self.backend.adds, self.backend.checkouts), ([], []))
        self.assertEqual(self.run_booking(), result)

    def test_completed_job_resume_rechecks_reservation_and_does_not_rebook_cancellation(self):
        self.assertEqual(self.run_booking().status, "confirmed")
        self.backend.observation = replace(self.backend.observation, reservation_confirmed=False)
        with self.assertRaises(BookingError):
            self.run_booking()
        self.assertEqual((len(self.backend.adds), len(self.backend.checkouts)), (1, 1))

    def test_old_receipt_wrong_amount_or_fake_success_does_not_prove_payment(self):
        cases = (
            {"receipt_id": "old-receipt"},
            {"receipt_total": Decimal("89.99")},
            {"receipt_closed": False},
            {"reservation_confirmed": False},
            {"cart_empty": False, "held": True},
        )
        for index, changes in enumerate(cases):
            clock, backend = Clock(), None
            backend = Backend(clock)

            def checkout(target, changes=changes):
                target.confirm()
                target.observation = replace(target.observation, **changes)

            backend.on_checkout = checkout
            result = run_booking(
                replace(self.spec, deadline=BASE + timedelta(seconds=10)), backend,
                self.path.parent / f"receipt-{index}.json", now=clock.now, log=lambda message: None,
            )
            with self.subTest(changes=changes):
                self.assertEqual(result.status, "deadline")
                self.assertIsNone(result.receipt_id)
                self.assertEqual(len(backend.checkouts), 1)

    def test_state_rejects_changed_child_course_spend_payment_or_backend_identity(self):
        self.backend.observation = replace(self.backend.observation, available=False)
        self.run_booking(replace(self.spec, deadline=BASE + timedelta(seconds=2)))
        changes = (
            {"child_name": "Another Child"},
            {"course": replace(self.spec.course, id=124)},
            {"max_price": Decimal("101")},
            {"payment_mode": "verkkomaksu"},
            {"payment_type_id": 7},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(BookingError):
                self.run_booking(replace(self.spec, **change))
        self.backend.identity = "another-payer:child:123"
        with self.assertRaises(BookingError):
            self.run_booking()
        self.assertEqual((self.backend.adds, self.backend.checkouts), ([], []))

    def test_identity_changed_by_snapshot_is_rejected_before_mutation(self):
        self.backend.on_snapshot = lambda backend: setattr(backend, "identity", "wrong:child:course")
        with self.assertRaises(BookingError):
            self.run_booking()
        self.assertEqual((self.backend.adds, self.backend.checkouts), ([], []))

    def test_payment_lock_prevents_new_writes_and_waits_are_deadline_bounded(self):
        self.backend.observation = replace(self.backend.observation, payment_locked=True)
        result = self.run_booking(replace(self.spec, deadline=BASE + timedelta(seconds=5)))
        self.assertEqual(result.status, "deadline")
        self.assertEqual(self.clock.now(), BASE + timedelta(seconds=5))
        self.assertEqual((self.backend.adds, self.backend.checkouts), ([], []))

    def test_state_write_failure_preserves_old_state_and_prevents_actions(self):
        self.backend.observation = replace(self.backend.observation, available=False)
        self.run_booking(replace(self.spec, deadline=BASE + timedelta(seconds=2)))
        original = self.path.read_bytes()
        self.backend.observation = replace(self.backend.observation, available=True)
        with patch("uhkone.booking.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(BookingError):
                self.run_booking()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["booking.json"])
        self.assertEqual((self.backend.adds, self.backend.checkouts), ([], []))

    def test_corrupt_state_is_not_silently_reset(self):
        self.path.parent.mkdir()
        for raw in ('{}', '[]', 'null', 'broken', '{"version": 1, "version": 1}'):
            self.path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(BookingError):
                self.run_booking()
            self.assertEqual(self.path.read_text(encoding="utf-8"), raw)
        self.assertEqual((self.backend.adds, self.backend.checkouts), ([], []))

    def test_invalid_authorization_is_rejected(self):
        for change in (
            {"opens_at": datetime(2030, 1, 1)},
            {"deadline": BASE},
            {"max_price": Decimal("NaN")},
            {"max_price": Decimal("-1")},
            {"child_name": " "},
            {"payment_mode": "unknown"},
            {"payment_type_id": True},
        ):
            with self.subTest(change=change), self.assertRaises(BookingError):
                replace(self.spec, **change)

    def test_action_controls_run_before_intent_and_refresh_before_dispatch(self):
        actions = []
        def approve(action, course_id):
            actions.append((action, course_id, len(self.backend.snapshots)))
            state = self.state()
            self.assertEqual(state["add_sends"], 0 if action == "add" else 1)
            self.assertEqual(state["checkout_sends"], 0)
        self.assertEqual(self.run_booking(before_action=approve).status, "confirmed")
        self.assertEqual([action for action, _, _ in actions], ["add", "checkout"])
        self.assertEqual([course_id for _, course_id, _ in actions], [123, 123])
        self.assertGreaterEqual(len(self.backend.snapshots), 5)
        self.assertEqual((len(self.backend.adds), len(self.backend.checkouts)), (1, 1))

    def test_stop_at_action_prompt_does_not_create_possible_send(self):
        def stop(action, course_id):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_booking(before_action=stop)
        self.assertEqual((self.backend.adds, self.backend.checkouts), ([], []))
        self.assertEqual((self.state()["phase"], self.state()["add_sends"]), ("ready", 0))
        self.assertEqual(self.run_booking().status, "confirmed")

    def test_pause_at_add_prompt_refreshes_price_and_rejects_changed_budget(self):
        def pause(action, course_id):
            self.backend.wait(5)
            self.backend.observation = replace(self.backend.observation, current_price=Decimal("101"))
        with self.assertRaises(BookingError):
            self.run_booking(before_action=pause)
        self.assertEqual((self.backend.adds, self.backend.checkouts), ([], []))
        self.assertEqual(self.state()["add_sends"], 0)

    def test_pause_at_checkout_prompt_refreshes_cart_before_payment(self):
        def pause(action, course_id):
            if action == "checkout":
                self.backend.wait(5)
                self.backend.observation = replace(self.backend.observation, cart_has_others=True)
        with self.assertRaises(BookingError):
            self.run_booking(before_action=pause)
        self.assertEqual((len(self.backend.adds), len(self.backend.checkouts)), (1, 0))
        self.assertEqual(self.state()["checkout_sends"], 0)

    def test_pause_past_deadline_prevents_authorized_add(self):
        def pause(action, course_id):
            self.backend.wait(100)
        self.assertEqual(self.run_booking(before_action=pause).status, "deadline")
        self.assertEqual(self.backend.adds, [])
        self.assertEqual(self.state()["add_sends"], 0)

    def test_live_controls_split_long_waits_and_can_stop_before_opening(self):
        def checkpoint():
            if self.clock.now() >= BASE + timedelta(seconds=3):
                raise KeyboardInterrupt()
        spec = replace(self.spec, opens_at=BASE + timedelta(seconds=120),
                       deadline=BASE + timedelta(seconds=180))
        with self.assertRaises(KeyboardInterrupt):
            self.run_booking(spec, checkpoint=checkpoint)
        self.assertEqual(self.clock.now(), BASE + timedelta(seconds=3))
        self.assertEqual(self.backend.waits, [1, 1, 1])
        self.assertEqual((self.backend.adds, self.backend.checkouts), ([], []))

    def test_progress_is_structured_without_customer_or_receipt_fields(self):
        events = []
        self.assertEqual(self.run_booking(progress=lambda phase, data: events.append((phase, data))).status,
                         "confirmed")
        phases = [phase for phase, _ in events]
        for phase in ("waiting", "candidate", "adding", "held", "paying", "heartbeat", "confirmed"):
            self.assertIn(phase, phases)
        encoded = json.dumps(events)
        for private in (self.spec.child_name, self.backend.identity, "reservation-123", "new-receipt"):
            self.assertNotIn(private, encoded)

    def test_stopping_after_uncertain_payment_preserves_restart_reconciliation(self):
        def uncertain(backend):
            backend.confirm()
            raise ActionUncertain("lost reply")
        self.backend.on_checkout = uncertain
        def checkpoint():
            if self.backend.checkouts:
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_booking(checkpoint=checkpoint)
        self.assertEqual(self.state()["checkout_sends"], 1)
        self.assertEqual(self.run_booking().status, "confirmed")
        self.assertEqual(len(self.backend.checkouts), 1)


if __name__ == "__main__":
    unittest.main()
