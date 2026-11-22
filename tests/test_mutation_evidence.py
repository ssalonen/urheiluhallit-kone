"""Adversarial transitions between a controller observation and browser dispatch."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tests.test_booking import BASE, Backend, Clock
from tests.test_booking_browser import cart_row, course_row, reservation, spec
from tests.test_multi import Pool
from uhkone.booking import (
    ActionNotSent,
    BookingError,
    Observation,
    _save_state,
    run_booking,
)
from uhkone.booking_browser import BookingBrowser, _BookingGuard, _observation
from uhkone.multi import run_race


def observed(selected, own, *, cart=True):
    return _observation(selected, 20, course_row(), [cart_row()] if cart else [], own, None, {"isLocked": False})


def browser(selected):
    page = MagicMock()
    page.url = selected.course.url
    page.locator.return_value.count.return_value = 1
    page.locator.return_value.evaluate.return_value = False
    page.evaluate.side_effect = lambda script: [cart_row()] if "toJSON" in script else 9
    backend = BookingBrowser(selected, None, page, _BookingGuard(None, selected))
    backend.child_id = 20
    backend._course = selected.course
    backend._payment_types = MagicMock(return_value=[{"payment_type_id": 5, "name": "FastPay", "is_disabled": 0,
                                       "is_pay_later": 0, "is_balance": 0}])
    return backend


@pytest.mark.parametrize("change", [
    {"reservation_id": "999"}, {"reservation_id": None}, {"current_price": Decimal(75)},
])
@pytest.mark.parametrize("late_stage", ["before_prepare", "after_prepare"])
def test_checkout_dispatch_bound_to_authorized_hold(change, late_stage):
    selected = replace(spec("fastpay"), payment_type_id=5)
    backend = browser(selected)
    original = observed(selected, [reservation()])
    fresh = replace(original, **change)
    backend._recent_observation = lambda: fresh if late_stage == "before_prepare" else original
    backend.snapshot = lambda: fresh
    backend.page.evaluate.side_effect = lambda script: (
        [cart_row(price=int(fresh.current_price * 100))] if "toJSON" in script else 9)
    with pytest.raises(ActionNotSent):
        backend.checkout("456", Decimal(76), deadline=selected.deadline)
    backend.page.locator.return_value.click.assert_not_called()
    assert not backend.guard.actual_sent


@pytest.mark.parametrize("row", [
    {k: v for k, v in reservation(4, 1).items() if k != "is_paid"},
    reservation(4, "unknown"), reservation(7, 1), reservation(4, 0),
])
@pytest.mark.parametrize("unpaid", [False, True])
def test_inconsistent_or_unknown_payment_status_cannot_pay_or_handoff(tmp_path, row, unpaid):
    selected = replace(spec(), opens_at=BASE, deadline=BASE + timedelta(seconds=5),
                       payment_mode="none" if unpaid else "saldo", without_payment=unpaid)
    clock = Clock()
    backend = Backend(clock)
    backend.observation = observed(selected, [row])
    result = run_booking(selected, backend, tmp_path / "state", now=clock.now, log=lambda _: None)
    assert result.status == "deadline"
    assert backend.checkouts == []
    if unpaid:
        live = browser(selected)
        live.snapshot = lambda: backend.observation
        with pytest.raises(BookingError):
            live.show_unpaid_cart("456")


def test_crash_after_intent_before_checkout_is_not_this_runs_payment(tmp_path):
    selected = replace(spec(), opens_at=BASE, deadline=BASE + timedelta(seconds=10), max_price=Decimal(100))
    clock = Clock()
    backend = Backend(clock)
    backend.hold()
    path = tmp_path / "state"
    def crash_after_intent(path, state):
        _save_state(path, state)
        if state["phase"] == "paying":
            raise KeyboardInterrupt
    with patch("uhkone.booking._save_state", side_effect=crash_after_intent), pytest.raises(KeyboardInterrupt):
        run_booking(selected, backend, path, now=clock.now, log=lambda _: None)
    backend.confirm()
    result = run_booking(selected, backend, path, now=clock.now, log=lambda _: None)
    assert (result.status, result.receipt_id) == ("already_confirmed", None)
    assert backend.checkouts == []


@pytest.mark.parametrize("policy", ["fallback", "eager"])
def test_queue_unsent_preserves_server_backoff(tmp_path, policy):
    clock = Clock()
    pool = Pool(clock, identifiers=(123,))
    selected = replace(spec(), opens_at=BASE, deadline=BASE + timedelta(seconds=15))
    target = pool.backends[123]
    target.observation = Observation(queue_available=True, current_price=Decimal(76))
    def join(backend):
        if len(backend.queue_calls) == 1:
            raise ActionNotSent("Too many requests", retry_after=7)
        backend.queued()
    target.on_queue = join
    result = run_race((selected,), pool, tmp_path, now=clock.now, log=lambda _: None,
                      allow_queue=True, queue_policy=policy, queue_timeout=15)
    assert result.status == "queued"
    assert target.queue_calls[1] - target.queue_calls[0] >= timedelta(seconds=7)


def test_expired_fallback_never_reopens_a_new_write_window(tmp_path):
    clock = Clock()
    pool = Pool(clock, identifiers=(123,))
    selected = replace(spec(), opens_at=BASE, deadline=BASE + timedelta(seconds=4))
    target = pool.backends[123]
    target.observation = Observation(queue_available=True, current_price=Decimal(76))
    clock.value = BASE + timedelta(seconds=30)
    result = run_race((selected,), pool, tmp_path, now=clock.now, log=lambda _: None,
                      allow_queue=True, queue_policy="fallback", queue_timeout=10)
    assert result.status == "deadline"
    assert pool.writes == []


def test_late_queue_claim_before_arm_is_returned_for_reconciliation():
    selected = spec()
    backend = browser(selected)
    ready = Observation(queue_available=True, current_price=Decimal(76))
    late = observed(selected, [reservation()], cart=False)
    backend.snapshot = MagicMock(side_effect=[ready, late])
    button = MagicMock()
    backend._participant_action = lambda **kwargs: button
    with pytest.raises(ActionNotSent) as refused:
        backend.join_queue()
    button.click.assert_not_called()
    assert refused.value.observation == late


def test_live_receipt_event_numeric_equality_is_not_a_verified_paid_course_link():
    selected = spec()
    own = [reservation(4, 1) | {"receipt_event_id": 78}]
    receipt = {"receipt_id": 78, "total_amount": 7600, "is_closed": 1, "is_pay_later": 0}
    evidence = _observation(selected, 20, course_row(), [], own, receipt, {"isLocked": False})
    assert evidence.receipt_id == "78"
    assert evidence.reservation_receipt_id is None


@pytest.mark.parametrize("late_stage", ["before_prepare", "after_prepare"])
def test_late_paid_cart_evidence_blocks_checkout_on_older_read_after_restart(tmp_path, late_stage):
    selected = replace(spec("fastpay"), payment_type_id=5, opens_at=BASE, deadline=BASE + timedelta(seconds=2))
    clock = Clock()
    backend = Backend(clock)
    fresh = observed(selected, [reservation()])
    backend.observation = fresh
    live = browser(selected)
    paid = observed(selected, [reservation(4, 1)])
    live._recent_observation = lambda: paid if late_stage == "before_prepare" else fresh
    live.snapshot = lambda: paid
    backend.checkout = live.checkout
    state_path = tmp_path / "journal"
    result = run_booking(selected, backend, state_path, now=clock.now, log=lambda _: None)
    assert result.status == "deadline"
    live._recent_observation = lambda: fresh
    live.snapshot = lambda: fresh
    second = run_booking(replace(selected, deadline=BASE + timedelta(seconds=8)), backend, state_path,
                         now=clock.now, log=lambda _: None)
    assert second.status == "deadline"
    live.page.locator.return_value.click.assert_not_called()


@pytest.mark.parametrize("late_stage", ["before_prepare", "after_prepare"])
def test_pending_claim_seen_by_add_blocks_other_course_even_after_restart(tmp_path, late_stage):
    selected = replace(spec(), opens_at=BASE, deadline=BASE + timedelta(seconds=4))
    alternative = replace(selected, course=replace(selected.course, id=124))
    clock = Clock()
    pool = Pool(clock, identifiers=(123, 124))
    for target in pool.backends.values():
        target.observation = Observation(available=True, current_price=Decimal(76))
    clear = pool.backends[123].observation
    live = browser(selected)
    claim = observed(selected, [reservation()], cart=False)
    live._recent_observation = lambda: claim if late_stage == "before_prepare" else clear
    live.snapshot = lambda: claim
    live._participant_action = MagicMock()
    pool.backends[123].add_selected_course = live.add_selected_course
    for extra in (0, 4):
        specs = tuple(replace(s, deadline=selected.deadline + timedelta(seconds=extra)) for s in (selected, alternative))
        result = run_race(specs, pool, tmp_path, now=clock.now, log=lambda _: None)
        assert (result.status, result.course_id, result.reservation_id) == ("deadline", 123, "456")
    assert pool.writes == []
    live._participant_action.return_value.click.assert_not_called()


@pytest.mark.parametrize("policy", ["fallback", "eager"])
def test_queue_final_read_can_pin_a_late_actual_claim(tmp_path, policy):
    selected = replace(spec(), opens_at=BASE, deadline=BASE + timedelta(seconds=4))
    other = replace(selected, course=replace(selected.course, id=124))
    clock = Clock()
    pool = Pool(clock, identifiers=(123, 124))
    ready = Observation(queue_available=True, current_price=Decimal(76))
    for target in pool.backends.values():
        target.observation = ready
    live = browser(selected)
    live._participant_action = MagicMock()
    claim = observed(selected, [reservation()], cart=False)
    live.snapshot = MagicMock(side_effect=[ready, claim])
    pool.backends[123].join_queue = live.join_queue
    result = run_race((selected, other), pool, tmp_path, now=clock.now, log=lambda _: None,
                      allow_queue=True, queue_policy=policy, queue_timeout=8)
    assert result.status == "deadline"
    assert pool.activations.count(124) == 0
    assert pool.writes == []


def test_queue_price_above_cap_after_participant_modal_blocks_submit():
    selected = spec()
    live = browser(selected)
    ready = Observation(queue_available=True, current_price=Decimal(76))
    live.snapshot = MagicMock(side_effect=[ready, replace(ready, current_price=Decimal(81))])
    live._participant_action = MagicMock()
    with pytest.raises(ActionNotSent):
        live.join_queue()
    live._participant_action.return_value.click.assert_not_called()


def test_a_larger_retry_timeout_does_not_extend_completed_fallback(tmp_path):
    selected = replace(spec(), opens_at=BASE, deadline=BASE + timedelta(seconds=4))
    clock = Clock()
    pool = Pool(clock, identifiers=(123,))
    target = pool.backends[123]
    target.observation = Observation(current_price=Decimal(76))
    def fallback(timeout):
        return run_race((selected,), pool, tmp_path, now=clock.now, log=lambda _: None,
                        allow_queue=True, queue_policy="fallback", queue_timeout=timeout)
    assert fallback(4).status == "deadline"
    target.observation = replace(target.observation, queue_available=True)
    assert fallback(30).status == "deadline"
    assert pool.writes == []


@pytest.mark.parametrize(("when", "expected"), [(2, 1), (8, 0)])
def test_actual_queue_guard_uses_effective_fallback_cutoff(tmp_path, when, expected):
    from json import dumps
    from types import SimpleNamespace

    selected = replace(spec(), opens_at=BASE, deadline=BASE + timedelta(seconds=4))
    clock = Clock()
    pool = Pool(clock, identifiers=(123,))
    target = pool.backends[123]
    ready = Observation(queue_available=True, current_price=Decimal(76))
    target.observation = ready
    live = browser(selected)
    live.snapshot = lambda: ready
    live.guard.child_id = 20
    req = SimpleNamespace(url=live.base_url + "/webshop/reservations2/apiusereventgroups", method="POST",
                          post_data=dumps({"account": {"account_id": 20}, "reservation_event_group_id": 123,
                                          "reservation_status_id": 1}))
    writes = []
    def dispatch():
        clock.value = selected.deadline + timedelta(seconds=when)
        live.guard.route(SimpleNamespace(request=req, continue_=lambda: (writes.append(clock.now()), target.queued()),
                                         abort=lambda _: None))
    button = MagicMock()
    button.click.side_effect = dispatch
    live._participant_action = lambda **kwargs: button
    target.join_queue = live.join_queue
    with patch("uhkone.booking_browser.datetime") as time:
        time.now.side_effect = lambda zone: clock.now()
        run_race((selected,), pool, tmp_path, now=clock.now, log=lambda _: None,
                 allow_queue=True, queue_policy="fallback", queue_timeout=8)
    assert len(writes) == expected


def test_unresolved_final_reservation_cannot_regress_to_a_payable_hold_on_restart(tmp_path):
    selected = replace(spec(), opens_at=BASE, deadline=BASE + timedelta(seconds=2))
    clock = Clock()
    backend = Backend(clock)
    row = reservation(4, 1)
    row.pop("is_paid")
    backend.observation = observed(selected, [row])
    path = tmp_path / "state"
    assert run_booking(selected, backend, path, now=clock.now, log=lambda _: None).status == "deadline"
    backend.observation = observed(selected, [reservation()])
    result = run_booking(replace(selected, deadline=BASE + timedelta(seconds=8)), backend, path,
                         now=clock.now, log=lambda _: None)
    assert result.status == "deadline"
    assert not backend.checkouts


def test_unidentified_held_cart_survives_restart_without_authorizing_an_add(tmp_path):
    selected = replace(spec(), opens_at=BASE, deadline=BASE + timedelta(seconds=2))
    clock = Clock()
    backend = Backend(clock)
    path = tmp_path / "state"
    backend.observation = observed(selected, [])
    assert run_booking(selected, backend, path, now=clock.now, log=lambda _: None).status == "deadline"
    backend.observation = observed(selected, [], cart=False)
    result = run_booking(replace(selected, deadline=BASE + timedelta(seconds=8)), backend, path,
                         now=clock.now, log=lambda _: None)
    assert result.status == "deadline"
    assert not backend.adds and not backend.checkouts


def test_observed_dispatch_cannot_be_rolled_back_by_a_contradictory_unsent_error(tmp_path):
    selected = replace(spec(), opens_at=BASE, deadline=BASE + timedelta(seconds=12))
    clock = Clock()
    backend = Backend(clock)
    backend.observation = observed(selected, [reservation()])
    forwarded = []
    def checkout(reservation_id, price, *, deadline, on_dispatched):
        on_dispatched()
        forwarded.append(reservation_id)
        raise ActionNotSent("Contradictory transport error")
    backend.checkout = MagicMock(side_effect=checkout)
    result = run_booking(selected, backend, tmp_path / "state", now=clock.now, log=lambda _: None)
    assert result.status == "deadline"
    assert forwarded == ["456"]


def test_checkout_rechecks_after_interacting_with_terms_and_dropdown():
    selected = replace(spec("fastpay"), payment_type_id=5)
    live = browser(selected)
    initial = observed(selected, [reservation()])
    state = [initial]
    live._recent_observation = lambda: initial
    live.snapshot = lambda: state[0]
    action = live.page.locator.return_value
    def replace_reservation():
        state[0] = replace(initial, reservation_id="different")
    action.check.side_effect = replace_reservation
    with pytest.raises(ActionNotSent):
        live.checkout("456", Decimal(76), deadline=selected.deadline)
    action.click.assert_not_called()
