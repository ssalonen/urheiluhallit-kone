"""Account-wide first-winner coordination with fake clocks and no network."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from uhkone.booking import ActionNotSent, ActionUncertain, BookingError, BookingSpec, Observation, TransientBookingError
from uhkone.models import Course
from uhkone.multi import RaceBusyError, _account_lock, run_race


BASE = datetime(2030, 1, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.value = BASE

    def now(self):
        return self.value


class Backend:
    def __init__(self, pool, course_id):
        self.pool = pool
        self.course_id = course_id
        self.identity = f"payer:child:{course_id}"
        self.observation = Observation(available=True, current_price=Decimal("90"))
        self.on_snapshot = None
        self.on_add = None
        self.on_checkout = None
        self.on_queue = None
        self.adds = []
        self.checkouts = []
        self.queue_calls = []

    def snapshot(self):
        if self.on_snapshot:
            self.on_snapshot(self)
        return self.observation

    def add_selected_course(self):
        if self.pool.active != self.course_id:
            raise AssertionError("Inactive page attempted a mutation")
        self.adds.append(self.pool.clock.now())
        self.pool.writes.append((self.course_id, "add"))
        if self.on_add:
            self.on_add(self)
        else:
            self.hold()

    def hold(self):
        self.observation = replace(self.observation, held=True, cart_empty=False)

    def checkout(self):
        self.checkouts.append(self.pool.clock.now())
        self.pool.writes.append((self.course_id, "checkout"))
        if self.on_checkout:
            self.on_checkout(self)
        else:
            self.confirm()

    def confirm(self):
        self.observation = replace(self.observation, held=False, cart_empty=True,
                                   reservation_id=f"reservation-{self.course_id}",
                                   reservation_confirmed=True, receipt_id=f"receipt-{self.course_id}",
                                   receipt_closed=True, receipt_total=Decimal("90"))

    def join_queue(self):
        if self.pool.active != self.course_id:
            raise AssertionError("Inactive page attempted a queue mutation")
        self.queue_calls.append(self.pool.clock.now())
        self.pool.writes.append((self.course_id, "queue"))
        if self.on_queue:
            self.on_queue(self)
        else:
            self.queued()

    def queued(self):
        self.observation = replace(self.observation, queued=True,
                                   queue_reservation_id=f"queue-{self.course_id}", queue_position=2)

    def wait(self, seconds):
        if seconds <= 0:
            raise AssertionError("Wait must make progress")
        self.pool.clock.value += timedelta(seconds=seconds)


class Pool:
    base_url = "https://kauppa.urheiluhallit.fi"
    identity = f"{base_url}|payer:child"

    def __init__(self, clock, identifiers=(1, 2)):
        self.clock = clock
        self.backends = {course_id: Backend(self, course_id) for course_id in identifiers}
        self.writes = []
        self.active = None
        self.activations = []
        self.on_observations = None
        self.errors = {}
        self.polls = []

    def observations(self):
        self.polls.append(self.clock.now())
        if self.on_observations:
            self.on_observations(self)
        held = [course_id for course_id, backend in self.backends.items() if backend.observation.held]
        result = {}
        for course_id, backend in self.backends.items():
            observation = backend.observation
            if held and course_id not in held:
                observation = replace(observation, cart_has_others=True, cart_empty=False)
            result[course_id] = self.errors.get(course_id, observation)
        return result

    def activate(self, course_id):
        self.active = course_id
        self.activations.append(course_id)
        return self.backends[course_id]

    def activate_queue(self, course_id):
        return self.activate(course_id)


class MultiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "state"
        self.clock = Clock()
        self.pool = Pool(self.clock)
        self.specs = tuple(BookingSpec(
            course=Course(id=course_id, name="Leikki-ikäisten jatkouimakoulu",
                          location="Mäkelänrinne" if course_id == 1 else "Kallio"),
            child_name="Testi Lapsi", max_price=Decimal("100"), opens_at=BASE,
            deadline=BASE + timedelta(seconds=30), payment_mode="saldo",
        ) for course_id in (1, 2))

    def race(self, specs=None, pool=None, **kwargs):
        return run_race(specs or self.specs, pool or self.pool, self.root,
                        now=self.clock.now, log=lambda _: None, **kwargs)

    def queue_only(self):
        self.specs = tuple(replace(spec, deadline=BASE + timedelta(seconds=4)) for spec in self.specs)
        for backend in self.pool.backends.values():
            backend.observation = replace(backend.observation, available=False, queue_available=True)

    def lease(self):
        path = next(path for path in self.root.glob("yksi-*.json") if "-kurssi-" not in path.name)
        return json.loads(path.read_text())

    def test_simultaneously_available_courses_choose_first_and_only_one_payment(self):
        result = self.race()
        self.assertEqual((result.status, result.course_id), ("confirmed", 1))
        self.assertEqual(self.pool.writes, [(1, "add"), (1, "checkout")])
        self.assertEqual(self.pool.activations, [1])

    def test_kallio_wins_when_preferred_hall_has_no_place(self):
        self.pool.backends[1].observation = replace(self.pool.backends[1].observation, available=False)
        result = self.race()
        self.assertEqual((result.status, result.course_id), ("confirmed", 2))
        self.assertEqual(self.pool.writes, [(2, "add"), (2, "checkout")])

    def test_future_course_does_not_block_already_open_alternative(self):
        specs = (replace(self.specs[0], opens_at=BASE + timedelta(seconds=10)), self.specs[1])
        self.assertEqual(self.race(specs).course_id, 2)
        self.assertEqual(self.pool.backends[2].adds, [BASE])

    def test_no_mutation_before_opening(self):
        specs = tuple(replace(spec, opens_at=BASE + timedelta(seconds=8)) for spec in self.specs)
        result = self.race(specs)
        self.assertEqual(result.course_id, 1)
        self.assertEqual(self.pool.backends[1].adds, [BASE + timedelta(seconds=8)])

    def test_existing_hold_takes_priority_over_an_available_preferred_hall(self):
        self.pool.backends[2].hold()
        self.assertEqual(self.race().course_id, 2)
        self.assertEqual(self.pool.writes, [(2, "checkout")])

    def test_existing_confirmation_prevents_any_new_booking(self):
        self.pool.backends[2].confirm()
        result = self.race()
        self.assertEqual((result.status, result.course_id), ("already_confirmed", 2))
        self.assertEqual(self.pool.writes, [])

    def test_pending_own_reservation_blocks_other_course_and_duplicate_add(self):
        backend = self.pool.backends[2]
        backend.observation = replace(backend.observation, available=False,
                                      reservation_id="pending-reservation-2")
        result = self.race()
        self.assertEqual((result.status, result.course_id), ("deadline", 2))
        self.assertEqual(self.pool.writes, [])
        self.assertEqual(self.pool.activations, [2])
        self.assertEqual(self.lease()["pinned_id"], 2)

    def test_pending_own_reservation_with_available_flag_is_not_added_again(self):
        backend = self.pool.backends[2]
        backend.observation = replace(backend.observation, reservation_id="pending-reservation-2")
        result = self.race()
        self.assertEqual((result.status, result.course_id), ("deadline", 2))
        self.assertEqual(self.pool.writes, [])

    def test_pending_reservation_temporarily_missing_does_not_allow_an_add(self):
        backend = self.pool.backends[2]
        backend.observation = replace(backend.observation, reservation_id="pending-reservation-2")
        def disappears(value):
            if self.clock.now() > BASE:
                value.observation = replace(value.observation, reservation_id=None)
        backend.on_snapshot = disappears
        result = self.race()
        self.assertEqual((result.status, result.course_id), ("deadline", 2))
        self.assertEqual(self.pool.writes, [])

    def test_existing_claim_missing_in_first_selected_snapshot_never_switches_or_adds(self):
        backend = self.pool.backends[2]
        backend.observation = replace(backend.observation, available=False,
                                      reservation_id="pending-reservation-2")
        def disappears(value):
            value.observation = replace(value.observation, reservation_id=None)
        backend.on_snapshot = disappears
        result = self.race()
        self.assertEqual((result.status, result.course_id), ("deadline", 2))
        self.assertEqual(self.pool.activations, [2])
        self.assertEqual(self.pool.writes, [])
        self.assertTrue(self.lease()["existing_claim"])

    def test_existing_hold_missing_in_first_selected_snapshot_is_not_re_added(self):
        backend = self.pool.backends[2]
        backend.hold()
        def disappears(value):
            value.observation = replace(value.observation, held=False, cart_empty=True)
        backend.on_snapshot = disappears
        self.assertEqual(self.race().course_id, 2)
        self.assertEqual(self.pool.writes, [])
        self.clock.value = BASE
        self.assertEqual(self.race().course_id, 2)
        self.assertEqual(self.pool.writes, [])

    def test_multiple_existing_confirmations_stop_without_a_new_charge(self):
        for backend in self.pool.backends.values():
            backend.confirm()
        with self.assertRaises(BookingError):
            self.race()
        self.assertEqual(self.pool.writes, [])

    def test_uncertain_add_pins_one_course_and_never_writes_an_alternative(self):
        def uncertain(_):
            raise ActionUncertain("Request may have reached the shop")
        self.pool.backends[1].on_add = uncertain
        result = self.race()
        self.assertEqual((result.status, result.course_id), ("deadline", 1))
        self.assertEqual(self.pool.writes, [(1, "add")])
        self.assertEqual(self.lease()["pinned_id"], 1)

    def test_positive_unsent_add_releases_selection_for_an_alternative(self):
        def unsent(_):
            raise ActionNotSent("No request was dispatched")
        self.pool.backends[1].on_add = unsent
        result = self.race()
        self.assertEqual((result.status, result.course_id), ("confirmed", 2))
        self.assertEqual(self.pool.activations, [1, 2])
        self.assertEqual(self.pool.backends[1].checkouts, [])

    def test_no_retry_can_follow_an_earlier_uncertain_send(self):
        def response(backend):
            if len(backend.adds) == 1:
                raise ActionUncertain("First request may have reached the shop")
            raise ActionNotSent("This later retry was never sent")
        self.pool.backends[1].on_add = response
        result = self.race()
        self.assertEqual((result.status, result.course_id), ("deadline", 1))
        self.assertEqual(len(self.pool.backends[1].adds), 1)
        self.assertEqual(self.pool.activations, [1])
        self.assertEqual(self.pool.backends[2].adds, [])

    def test_full_between_selection_and_first_add_releases_without_a_mutation(self):
        def fills(backend):
            backend.observation = replace(backend.observation, available=False)
        self.pool.backends[1].on_snapshot = fills
        result = self.race()
        self.assertEqual(result.course_id, 2)
        self.assertEqual(self.pool.writes, [(2, "add"), (2, "checkout")])

    def test_completed_restart_verifies_same_winner_without_new_mutations(self):
        self.assertEqual(self.race().course_id, 1)
        self.assertEqual(self.race().course_id, 1)
        self.assertEqual(self.pool.writes, [(1, "add"), (1, "checkout")])

    def test_different_candidate_set_cannot_bypass_account_lease(self):
        self.race()
        replacement = Pool(self.clock, identifiers=(2,))
        with self.assertRaises(BookingError):
            self.race((self.specs[1],), replacement)
        self.assertEqual(replacement.writes, [])

    def test_deadline_can_be_extended_without_changing_the_durable_scope(self):
        self.race()
        extended = tuple(replace(spec, deadline=spec.deadline + timedelta(seconds=30)) for spec in self.specs)
        self.assertEqual(self.race(extended).course_id, 1)
        self.assertEqual(self.pool.writes, [(1, "add"), (1, "checkout")])

    def test_foreign_cart_stops_every_candidate(self):
        self.pool.backends[1].observation = replace(self.pool.backends[1].observation,
                                                   cart_has_others=True, cart_empty=False)
        with self.assertRaises(BookingError):
            self.race()
        self.assertEqual(self.pool.writes, [])

    def test_unknown_candidate_cannot_hide_an_existing_confirmation(self):
        self.pool.errors[1] = TransientBookingError("503")
        result = self.race()
        self.assertEqual(result.status, "deadline")
        self.assertEqual(self.pool.writes, [])

    def test_authoritative_empty_account_allows_healthy_path_despite_other_get_failure(self):
        self.pool.errors[1] = TransientBookingError("503")
        self.pool.selection_clear = True
        self.assertEqual(self.race().course_id, 2)
        self.assertEqual(self.pool.writes, [(2, "add"), (2, "checkout")])

    def test_budget_rejected_candidate_does_not_block_affordable_one(self):
        self.pool.backends[1].observation = replace(self.pool.backends[1].observation,
                                                   current_price=Decimal("101"))
        self.assertEqual(self.race().course_id, 2)

    def test_lock_is_held_while_reading_and_persisted_files_are_private(self):
        def competing(pool):
            other = Pool(self.clock)
            with self.assertRaises(RaceBusyError):
                self.race(pool=other)
            pool.on_observations = None
        self.pool.on_observations = competing
        self.race()
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in self.root.iterdir()))

    def test_lock_rejects_another_operating_system_process(self):
        path = self.root / "process.lock"
        code = (
            "import sys; from pathlib import Path; "
            "from uhkone.multi import _account_lock, RaceBusyError; "
            "lock = _account_lock(Path(sys.argv[1])); "
            "\ntry:\n lock.__enter__()\nexcept RaceBusyError:\n sys.exit(23)\n"
        )
        with _account_lock(path):
            result = subprocess.run([sys.executable, "-c", code, str(path)], check=False,
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr)
        # The file remains, but the kernel releases the process-bound lock.
        with _account_lock(path):
            pass

    def test_duplicate_candidates_and_different_children_fail_before_any_write(self):
        with self.assertRaises(BookingError):
            self.race((self.specs[0], self.specs[0]))
        with self.assertRaises(BookingError):
            self.race((self.specs[0], replace(self.specs[1], child_name="Muu Lapsi")))
        self.assertEqual(self.pool.writes, [])

    def test_queue_fallback_is_opt_in(self):
        self.queue_only()
        self.assertEqual(self.race().status, "deadline")
        self.assertEqual(self.pool.writes, [])

    def test_queue_waits_until_actual_window_ends_and_uses_preferred_hall(self):
        self.queue_only()
        result = self.race(allow_queue=True, queue_timeout=8)
        self.assertEqual((result.status, result.course_id), ("queued", 1))
        self.assertEqual(self.pool.writes, [(1, "queue")])
        self.assertEqual(self.pool.backends[1].queue_calls, [self.specs[0].deadline])
        self.assertEqual((result.queue_reservation_id, result.queue_position), ("queue-1", 2))
        self.assertIsNone(result.reservation_id)
        self.assertIsNone(result.receipt_id)
        self.assertEqual(self.lease()["pinned_id"], None)

    def test_actual_place_appearing_at_queue_fallback_wins_before_any_queue(self):
        self.queue_only()
        def opens(pool):
            if self.clock.now() >= self.specs[0].deadline:
                backend = pool.backends[2]
                backend.observation = replace(backend.observation, available=True)
        self.pool.on_observations = opens
        result = self.race(allow_queue=True, queue_timeout=10)
        self.assertEqual((result.status, result.course_id), ("confirmed", 2))
        self.assertEqual(self.pool.writes, [(2, "add"), (2, "checkout")])

    def test_possible_actual_add_never_falls_back_to_queue(self):
        self.queue_only()
        backend = self.pool.backends[1]
        backend.observation = replace(backend.observation, available=True)
        backend.on_add = lambda _: (_ for _ in ()).throw(ActionUncertain("unknown actual add"))
        result = self.race(allow_queue=True, queue_timeout=10)
        self.assertEqual((result.status, result.course_id), ("deadline", 1))
        self.assertEqual(self.pool.writes, [(1, "add")])
        self.assertIsNone(self.lease()["queue"])

    def test_uncertain_queue_is_not_resent_or_replaced_after_absence_or_restart(self):
        self.queue_only()
        self.pool.backends[1].on_queue = lambda _: (_ for _ in ()).throw(ActionUncertain("unknown queue"))
        result = self.race(allow_queue=True, queue_timeout=8)
        self.assertEqual((result.status, result.course_id), ("queue_pending", 1))
        self.assertEqual(self.race(allow_queue=True, queue_timeout=8).status, "queue_pending")
        self.assertEqual(self.pool.writes, [(1, "queue")])
        self.assertEqual(self.lease()["queue"]["sends"], 1)

    def test_late_queue_commit_is_reconciled_on_restart_without_another_send(self):
        self.queue_only()
        backend = self.pool.backends[1]
        backend.on_queue = lambda _: (_ for _ in ()).throw(ActionUncertain("queue response lost"))
        self.assertEqual(self.race(allow_queue=True, queue_timeout=6).status, "queue_pending")
        backend.queued()
        result = self.race(allow_queue=True, queue_timeout=6)
        self.assertEqual((result.status, result.queue_reservation_id), ("queued", "queue-1"))
        self.assertEqual(self.pool.writes, [(1, "queue")])

    def test_queue_lost_response_is_reconciled_as_queue_without_payment_claim(self):
        self.queue_only()
        def lost(backend):
            backend.queued()
            raise ActionUncertain("queue committed; response lost")
        self.pool.backends[1].on_queue = lost
        result = self.race(allow_queue=True, queue_timeout=8)
        self.assertEqual((result.status, result.queue_reservation_id), ("queued", "queue-1"))
        self.assertEqual(self.pool.writes, [(1, "queue")])
        self.assertIsNone(result.receipt_id)

    def test_unsent_queue_can_yield_to_another_acceptable_queue(self):
        self.queue_only()
        self.pool.backends[1].on_queue = lambda _: (_ for _ in ()).throw(ActionNotSent("never dispatched"))
        result = self.race(allow_queue=True, queue_timeout=8)
        self.assertEqual((result.status, result.course_id), ("queued", 2))
        self.assertEqual(self.pool.backends[2].queue_calls, [self.specs[0].deadline])
        self.assertEqual(self.lease()["queue"]["course_id"], 2)

    def test_existing_queue_does_not_block_actual_place_on_another_course(self):
        backend = self.pool.backends[1]
        backend.observation = replace(backend.observation, available=False)
        backend.queued()
        result = self.race(allow_queue=True, queue_timeout=8)
        self.assertEqual((result.status, result.course_id), ("confirmed", 2))
        self.assertEqual(self.pool.writes, [(2, "add"), (2, "checkout")])

    def test_single_queue_retries_a_proven_unsent_request_after_cooldown(self):
        self.specs = (self.specs[0],)
        self.pool = Pool(self.clock, identifiers=(1,))
        self.queue_only()
        def interrupted_once(backend):
            if len(backend.queue_calls) == 1:
                raise ActionNotSent("Alustava lukupyyntö katkesi, jonopyyntöä ei lähetetty")
            backend.queued()
        self.pool.backends[1].on_queue = interrupted_once
        result = self.race(allow_queue=True, queue_timeout=8)
        self.assertEqual(result.status, "queued")
        attempts = self.pool.backends[1].queue_calls
        self.assertEqual(len(attempts), 2)
        self.assertGreaterEqual((attempts[1] - attempts[0]).total_seconds(), 2)
        self.assertEqual(self.lease()["queue"]["sends"], 1)

    def test_existing_queue_is_reported_after_actual_window_without_a_new_queue(self):
        self.queue_only()
        self.pool.backends[2].queued()
        result = self.race(allow_queue=True, queue_timeout=8)
        self.assertEqual((result.status, result.course_id), ("queued", 2))
        self.assertEqual(self.pool.writes, [])
        self.assertEqual(self.lease()["queue"]["sends"], 0)

    def test_queue_intent_survives_interruption_before_response(self):
        self.queue_only()
        def interrupt(backend):
            self.assertEqual(self.lease()["queue"]["phase"], "sending")
            backend.queued()
            raise KeyboardInterrupt()
        self.pool.backends[1].on_queue = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.race(allow_queue=True, queue_timeout=8)
        result = self.race(allow_queue=True, queue_timeout=8)
        self.assertEqual((result.status, result.course_id), ("queued", 1))
        self.assertEqual(self.pool.writes, [(1, "queue")])

    def test_extended_actual_search_can_win_after_prior_queue_confirmation(self):
        self.queue_only()
        self.assertEqual(self.race(allow_queue=True, queue_timeout=8).status, "queued")
        extended = tuple(replace(spec, deadline=self.clock.now() + timedelta(seconds=10)) for spec in self.specs)
        self.pool.backends[2].observation = replace(self.pool.backends[2].observation, available=True)
        result = self.race(extended, allow_queue=True, queue_timeout=8)
        self.assertEqual((result.status, result.course_id), ("confirmed", 2))
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "add"), (2, "checkout")])
        self.assertEqual(self.lease()["queue"]["reservation_id"], "queue-1")

    def test_queue_unknown_candidate_cannot_hide_an_existing_queue(self):
        self.queue_only()
        self.pool.selection_clear = True
        self.pool.errors[1] = TransientBookingError("unknown course queue")
        result = self.race(allow_queue=True, queue_timeout=6)
        self.assertEqual(result.status, "deadline")
        self.assertEqual(self.pool.writes, [])

    def test_lease_without_queue_field_migrates_without_losing_actual_winner(self):
        self.race()
        path = next(path for path in self.root.glob("yksi-*.json") if "-kurssi-" not in path.name)
        state = json.loads(path.read_text())
        state.pop("queue")
        path.write_text(json.dumps(state))
        self.assertEqual(self.race().course_id, 1)
        self.assertIn("queue", self.lease())
        self.assertEqual(self.pool.writes, [(1, "add"), (1, "checkout")])

    def test_eager_queues_join_every_acceptable_queue_before_actual_deadline(self):
        self.queue_only()
        result = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual(result.status, "queued")
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "queue")])
        self.assertEqual(self.pool.backends[1].queue_calls, [BASE])
        self.assertEqual(self.pool.backends[2].queue_calls, [BASE])
        self.assertEqual(result.queue_reservations, ((1, "queue-1", 2), (2, "queue-2", 2)))
        self.assertEqual(result.queue_pending_ids, ())
        self.assertEqual(self.clock.now(), self.specs[0].deadline)
        self.assertIsNone(self.lease()["pinned_id"])

    def test_eager_queues_prioritize_currently_available_real_place(self):
        self.pool.backends[1].observation = replace(self.pool.backends[1].observation,
                                                  available=False, queue_available=True)
        result = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual((result.status, result.course_id), ("confirmed", 2))
        self.assertEqual(self.pool.writes, [(2, "add"), (2, "checkout")])

    def test_eager_queue_is_not_final_and_real_place_can_win_next_poll(self):
        self.queue_only()
        self.specs = tuple(replace(spec, deadline=BASE + timedelta(seconds=10)) for spec in self.specs)
        def free_place(pool):
            if pool.backends[1].queue_calls:
                pool.backends[2].observation = replace(pool.backends[2].observation, available=True)
        self.pool.on_observations = free_place
        result = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual((result.status, result.course_id), ("confirmed", 2))
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "add"), (2, "checkout")])
        self.assertEqual(result.queue_reservations, ((1, "queue-1", 2),))

    def test_uncertain_eager_queue_allows_other_queue_without_replaying_first(self):
        self.queue_only()
        self.pool.backends[1].on_queue = lambda _: (_ for _ in ()).throw(ActionUncertain("unknown queue reply"))
        result = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual(result.status, "queued")
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "queue")])
        self.assertEqual(result.queue_reservations, ((2, "queue-2", 2),))
        self.assertEqual(result.queue_pending_ids, (1,))
        extended = tuple(replace(spec, deadline=self.clock.now() + timedelta(seconds=4)) for spec in self.specs)
        resumed = self.race(extended, allow_queue=True, queue_policy="eager")
        self.assertEqual(resumed.queue_pending_ids, (1,))
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "queue")])

    def test_uncertain_eager_queue_does_not_block_real_place(self):
        self.queue_only()
        self.specs = tuple(replace(spec, deadline=BASE + timedelta(seconds=10)) for spec in self.specs)
        self.pool.backends[1].on_queue = lambda _: (_ for _ in ()).throw(ActionUncertain("unknown queue reply"))
        self.pool.on_observations = lambda pool: setattr(
            pool.backends[2], "observation", replace(pool.backends[2].observation, available=bool(pool.writes)))
        result = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual((result.status, result.course_id), ("confirmed", 2))
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "add"), (2, "checkout")])
        self.assertEqual(result.queue_pending_ids, (1,))

    def test_eager_queue_intent_is_durable_and_restart_never_resends(self):
        self.queue_only()
        def interrupt(backend):
            self.assertEqual(self.lease()["queues"]["1"]["phase"], "sending")
            backend.queued()
            raise KeyboardInterrupt()
        self.pool.backends[1].on_queue = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.race(allow_queue=True, queue_policy="eager")
        result = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual(result.queue_reservations, ((1, "queue-1", 2), (2, "queue-2", 2)))
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "queue")])

    def test_legacy_single_queue_migrates_to_eager_without_duplicate_join(self):
        self.queue_only()
        self.race(allow_queue=True)
        path = next(path for path in self.root.glob("yksi-*.json") if "-kurssi-" not in path.name)
        data = json.loads(path.read_text())
        data.pop("queues")
        path.write_text(json.dumps(data))
        extended = tuple(replace(spec, deadline=self.clock.now() + timedelta(seconds=4)) for spec in self.specs)
        result = self.race(extended, allow_queue=True, queue_policy="eager")
        self.assertEqual(len(result.queue_reservations), 2)
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "queue")])

    def test_eager_queue_prompt_refreshes_all_candidates_before_dispatch(self):
        self.queue_only()
        self.specs = tuple(replace(spec, deadline=BASE + timedelta(seconds=10)) for spec in self.specs)
        actions = []
        def choose(action, course_id):
            actions.append((action, course_id))
            if action == "queue":
                self.pool.backends[2].observation = replace(self.pool.backends[2].observation, available=True)
        result = self.race(allow_queue=True, queue_policy="eager", before_action=choose)
        self.assertEqual((result.status, result.course_id), ("confirmed", 2))
        self.assertEqual(self.pool.writes, [(2, "add"), (2, "checkout")])
        self.assertEqual(actions, [("queue", 1), ("add", 2), ("checkout", 2)])

    def test_stop_at_queue_prompt_does_not_record_or_send_queue(self):
        self.queue_only()
        def stop(action, course_id):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.race(allow_queue=True, queue_policy="eager", before_action=stop)
        self.assertEqual(self.pool.writes, [])
        self.assertEqual(self.lease()["queues"], {})
        self.assertIsNone(self.lease()["queue"])

    def test_live_controls_interrupt_long_race_wait_without_any_writes(self):
        specs = tuple(replace(spec, opens_at=BASE + timedelta(seconds=120),
                              deadline=BASE + timedelta(seconds=180)) for spec in self.specs)
        def stop():
            if self.clock.now() >= BASE + timedelta(seconds=3):
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.race(specs, checkpoint=stop)
        self.assertEqual(self.clock.now(), BASE + timedelta(seconds=3))
        self.assertEqual(self.pool.writes, [])

    def test_eager_progress_distinguishes_queue_and_actual_confirmation(self):
        self.queue_only()
        events = []
        result = self.race(allow_queue=True, queue_policy="eager",
                           progress=lambda phase, data: events.append((phase, data)))
        self.assertEqual(result.status, "queued")
        self.assertIn("queueing", [phase for phase, _ in events])
        self.assertEqual([data["course_id"] for phase, data in events if phase == "queued"], [1, 2])
        self.assertNotIn("confirmed", [phase for phase, _ in events])
        encoded = json.dumps(events)
        self.assertNotIn(self.specs[0].child_name, encoded)
        self.assertNotIn(self.pool.identity, encoded)

    def test_eager_actual_uncertainty_blocks_all_new_queue_writes(self):
        self.pool.backends[1].on_add = lambda _: (_ for _ in ()).throw(ActionUncertain("unknown actual add"))
        for backend in self.pool.backends.values():
            backend.observation = replace(backend.observation, queue_available=True)
        result = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual((result.status, result.course_id), ("deadline", 1))
        self.assertEqual(self.pool.writes, [(1, "add")])
        self.assertEqual(self.lease()["queues"], {})

    def test_eager_queue_pause_past_deadline_does_not_dispatch(self):
        self.queue_only()
        def pause(action, course_id):
            self.clock.value += timedelta(seconds=10)
        result = self.race(allow_queue=True, queue_policy="eager", before_action=pause)
        self.assertEqual(result.status, "deadline")
        self.assertEqual(self.pool.writes, [])
        self.assertEqual(self.lease()["queues"], {})

    def test_cancelled_eager_queue_is_unverified_and_never_rejoined_on_restart(self):
        self.queue_only()
        self.race(allow_queue=True, queue_policy="eager")
        for backend in self.pool.backends.values():
            backend.observation = replace(backend.observation, queued=False,
                                          queue_reservation_id=None, queue_position=None)
        extended = tuple(replace(spec, deadline=self.clock.now() + timedelta(seconds=4)) for spec in self.specs)
        result = self.race(extended, allow_queue=True, queue_policy="eager")
        self.assertEqual(result.status, "queue_pending")
        self.assertEqual(result.queue_reservations, ())
        self.assertEqual(result.queue_pending_ids, (1, 2))
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "queue")])

    def test_two_eager_queues_can_precede_one_real_winner_without_an_actual_pin(self):
        self.pool = Pool(self.clock, identifiers=(1, 2, 3))
        self.specs += (replace(self.specs[1], course=replace(self.specs[1].course, id=3)),)
        self.queue_only()
        self.specs = tuple(replace(spec, deadline=BASE + timedelta(seconds=12)) for spec in self.specs)
        def release_real_place(pool):
            if pool.backends[1].queue_calls and pool.backends[2].queue_calls:
                pool.backends[3].observation = replace(pool.backends[3].observation, available=True)
        self.pool.on_observations = release_real_place
        result = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual((result.status, result.course_id), ("confirmed", 3))
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "queue"), (3, "add"), (3, "checkout")])
        self.assertEqual(result.queue_reservations, ((1, "queue-1", 2), (2, "queue-2", 2)))
        self.assertEqual(self.lease()["pinned_id"], 3)

    def test_cancelled_confirmed_actual_winner_cannot_reopen_eager_queues(self):
        first = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual(first.status, "confirmed")
        for backend in self.pool.backends.values():
            backend.observation = replace(backend.observation, available=False, held=False,
                                          cart_empty=True, reservation_id=None,
                                          reservation_confirmed=False, queue_available=True)
        with self.assertRaises(BookingError):
            self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual(self.pool.writes, [(1, "add"), (1, "checkout")])
        self.assertEqual(self.lease()["queues"], {})

    def test_saved_queues_with_failed_course_reads_are_pending_on_restart(self):
        self.queue_only()
        self.race(allow_queue=True, queue_policy="eager")
        self.pool.selection_clear = True
        self.pool.errors = {course_id: TransientBookingError("course read unavailable")
                            for course_id in self.pool.backends}
        extended = tuple(replace(spec, deadline=self.clock.now() + timedelta(seconds=4)) for spec in self.specs)
        result = self.race(extended, allow_queue=True, queue_policy="eager")
        self.assertEqual(result.status, "queue_pending")
        self.assertEqual(result.queue_reservations, ())
        self.assertEqual(result.queue_pending_ids, (1, 2))
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "queue")])
        self.assertTrue(all(record["phase"] == "queued" for record in self.lease()["queues"].values()))

    def test_later_global_read_failure_removes_current_queue_confirmation_claim(self):
        self.queue_only()
        def fail_after_confirmation(pool):
            if pool.backends[1].queue_calls and pool.backends[2].queue_calls and self.clock.now() > BASE:
                raise TransientBookingError("account read unavailable")
        self.pool.on_observations = fail_after_confirmation
        result = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual(result.status, "queue_pending")
        self.assertEqual(result.queue_reservations, ())
        self.assertEqual(result.queue_pending_ids, (1, 2))
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "queue")])

    def test_queue_upgraded_to_same_actual_winner_is_not_reported_as_another_queue(self):
        self.queue_only()
        self.specs = tuple(replace(spec, deadline=BASE + timedelta(seconds=12)) for spec in self.specs)
        def free_queued_course(pool):
            if pool.backends[1].queue_calls and pool.backends[2].queue_calls:
                pool.backends[1].observation = replace(pool.backends[1].observation, available=True)
        self.pool.on_observations = free_queued_course
        result = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual((result.status, result.course_id), ("confirmed", 1))
        self.assertEqual(result.queue_reservations, ((2, "queue-2", 2),))
        self.assertEqual(result.queue_pending_ids, ())
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "queue"), (1, "add"), (1, "checkout")])

    def test_uncertain_actual_upgrade_marks_same_course_queue_pending(self):
        self.queue_only()
        self.specs = tuple(replace(spec, deadline=BASE + timedelta(seconds=8)) for spec in self.specs)
        self.pool.on_observations = lambda pool: setattr(
            pool.backends[1], "observation", replace(pool.backends[1].observation,
            available=bool(pool.backends[1].queue_calls and pool.backends[2].queue_calls)))
        self.pool.backends[1].on_add = lambda _: (_ for _ in ()).throw(ActionUncertain("unknown actual upgrade"))
        result = self.race(allow_queue=True, queue_policy="eager")
        self.assertEqual((result.status, result.course_id), ("deadline", 1))
        self.assertEqual(result.queue_reservations, ((2, "queue-2", 2),))
        self.assertEqual(result.queue_pending_ids, (1,))
        self.assertEqual(self.pool.writes, [(1, "queue"), (2, "queue"), (1, "add")])


if __name__ == "__main__":
    unittest.main()
