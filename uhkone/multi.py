"""Read several acceptable courses; authorize mutations for one course at a time.

An account/child lease survives restarts and is shared by every candidate set.
Independent sessions may share a cart or pending reservations on the account;
their checkout operations must never race.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Callable, Mapping, Protocol, Sequence

from .booking import (
    ActionNotSent, BookingBackend, BookingError, BookingSpec, Observation, TransientBookingError,
    _aware, _binding, _check_observation, _load_state, _observation_metadata, _save_state, _unique_object,
    run_booking,
)
from .models import normalize_text
from .origin import validate_base_url


class RacePool(Protocol):
    identity: str
    base_url: str
    backends: Mapping[int, BookingBackend]

    def observations(self) -> Mapping[int, Observation | TransientBookingError]: ...
    def activate(self, course_id: int) -> BookingBackend: ...
    def activate_queue(self, course_id: int) -> BookingBackend: ...


@dataclass(frozen=True)
class RaceResult:
    status: str
    course_id: int | None = None
    reservation_id: str | None = None
    receipt_id: str | None = None
    queue_reservation_id: str | None = None
    queue_position: int | None = None
    queue_reservations: tuple[tuple[int, str, int | None], ...] = ()
    queue_pending_ids: tuple[int, ...] = ()


class RaceBusyError(BookingError):
    """Another process already owns the same account/child booking scope."""


class _YieldSelection(Exception):
    """Local evidence proves that no mutation has been sent for this choice."""


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise BookingError("Varauskansio ei saa olla symbolinen linkki.")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise BookingError(f"Varauskansiota ei voi avata: {exc}") from exc


@contextmanager
def _account_lock(path: Path):
    """Fail quickly, without breaking another process's lifetime-bound lock."""
    _private_directory(path.parent)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    locked = False
    try:
        if path.is_symlink():
            raise BookingError("Varauslukko ei saa olla symbolinen linkki.")
        descriptor = os.open(path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BookingError("Varauslukon on oltava tavallinen tiedosto.")
        os.chmod(path, 0o600)
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RaceBusyError("Saman maksajan ja lapsen varausajo on jo käynnissä.") from exc
        else:
            import fcntl
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RaceBusyError("Saman maksajan ja lapsen varausajo on jo käynnissä.") from exc
        locked = True
        yield
    except OSError as exc:
        raise BookingError(f"Varauslukkoa ei voi käyttää: {exc}") from exc
    finally:
        if descriptor is not None:
            try:
                if locked:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _lease_template(identity: str, origin: str, specs: Sequence[BookingSpec]) -> dict:
    return {
        "version": 1,
        "identity": identity,
        "base_url": origin,
        "candidates": sorted((_binding(spec) for spec in specs), key=lambda item: item["course_id"]),
        "pinned_id": None,
        "existing_claim": False,
        "queue": None,
        "queues": {},
    }


def _load_lease(path: Path, identity: str, origin: str, specs: Sequence[BookingSpec]) -> dict:
    expected = _lease_template(identity, origin, specs)
    if path.is_symlink():
        raise BookingError("Yhteinen varaustila ei saa olla symbolinen linkki.")
    if not path.exists():
        return expected
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        # Queue support was added to version 1 without changing actual-course
        # bindings or the meaning of a previously pinned purchase.
        if isinstance(value, dict) and "queue" not in value:
            value["queue"] = None
        if isinstance(value, dict) and "queues" not in value:
            queue = value.get("queue")
            value["queues"] = {str(queue["course_id"]): queue} if isinstance(queue, dict) else {}
        if not isinstance(value, dict) or set(value) != set(expected) or type(value["version"]) is not int:
            raise ValueError("Tuntematon yhteisen varaustilan rakenne")
        pinned = value["pinned_id"]
        if type(value["existing_claim"]) is not bool or (value["existing_claim"] and pinned is None):
            raise ValueError("Virheellinen aiemmin nähdyn varauksen tieto")
        if pinned is not None and (type(pinned) is not int or pinned not in {spec.course.id for spec in specs}):
            raise ValueError("Tallennettu voittajakurssi ei kuulu tähän ehdokasjoukkoon")
        queues = value["queues"]
        if not isinstance(queues, dict):
            raise ValueError("Virheellinen tallennettujen jonovarausten rakenne")
        queue = value["queue"]
        for key, queue_item in queues.items():
            if (not isinstance(key, str) or not isinstance(queue_item, dict)
                    or str(queue_item.get("course_id")) != key):
                raise ValueError("Jonovarauksen kurssitunnus ei vastaa avainta")
        for queue in [value["queue"], *queues.values()]:
            if queue is None:
                continue
            if (not isinstance(queue, dict)
                    or set(queue) != {"course_id", "phase", "sends", "reservation_id", "position"}
                    or type(queue["course_id"]) is not int
                    or queue["course_id"] not in {spec.course.id for spec in specs}
                    or queue["phase"] not in {"sending", "sent", "queued", "missing"}
                    or type(queue["sends"]) is not int or queue["sends"] not in {0, 1}
                    or (queue["phase"] in {"sending", "sent"} and queue["sends"] != 1)
                    or (queue["reservation_id"] is not None
                        and (not isinstance(queue["reservation_id"], str) or not queue["reservation_id"].strip()))
                    or (queue["position"] is not None
                        and (type(queue["position"]) is not int or queue["position"] < 1))
                    or (queue["phase"] == "queued" and not queue["reservation_id"])):
                raise ValueError("Virheellinen tallennettu jonovaraus")
        if value["queue"] is not None:
            key = str(value["queue"]["course_id"])
            if key in queues and queues[key] != value["queue"]:
                raise ValueError("Jonovarauksen vanha ja uusi tallenne ovat ristiriidassa")
            queues[key] = value["queue"]
        comparison = dict(value, pinned_id=None, existing_claim=False, queue=None, queues={})
        if comparison != expected:
            raise BookingError(
                "Saman maksajan ja lapsen tallennettu varausajo käyttää eri kurssijoukkoa tai asetuksia. "
                "Uutta rinnakkaista varausta ei käynnistetä."
            )
        return value
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, BookingError):
            raise
        raise BookingError(f"Yhteistä varaustilaa ei voi lukea: {exc}") from exc


def _can_release(path: Path, backend: BookingBackend, spec: BookingSpec) -> bool:
    """Only a journal proving zero possible sends permits a different course."""
    state = _load_state(path, backend.identity, spec)
    return (
        state["phase"] == "ready"
        and state["add_sends"] == state["checkout_sends"] == 0
        and state["reservation_id"] is None
    )


class _ArbitratedBackend:
    def __init__(self, backend: BookingBackend, spec: BookingSpec, journal: Path, *, existing_claim: bool):
        self.backend = backend
        self.spec = spec
        self.journal = journal
        self.existing_claim = existing_claim

    @property
    def identity(self):
        return self.backend.identity

    def snapshot(self):
        value = self.backend.snapshot()
        _check_observation(value)
        if (self.existing_claim and not value.held and not value.reservation_confirmed
                and value.reservation_id is None and value.cart_empty and not value.cart_has_others):
            # Shared account reads may expose a claim before the selected
            # course page does. Losing that evidence cannot authorize an add.
            raise TransientBookingError("Aiemmin nähty oma varaus ei vielä näy kurssipolussa.")
        if (not self.existing_claim and not value.available and not value.held and not value.reservation_confirmed
                and value.reservation_id is None
                and not value.payment_locked and not value.cart_has_others and value.cart_empty
                and _can_release(self.journal, self.backend, self.spec)):
            raise _YieldSelection()
        return value

    def add_selected_course(self):
        self.backend.add_selected_course()

    def checkout(self):
        self.backend.checkout()

    def wait(self, seconds):
        self.backend.wait(seconds)

    def on_add_not_sent(self):
        if not self.existing_claim and _can_release(self.journal, self.backend, self.spec):
            raise _YieldSelection()


def run_race(
    specs: Sequence[BookingSpec], pool: RacePool, state_dir: Path, *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    log: Callable[[str], None] = print,
    allow_queue: bool = False,
    queue_timeout: float = 60,
    queue_policy: str = "fallback",
    progress: Callable[[str, dict], None] | None = None,
    checkpoint: Callable[[], None] | None = None,
    before_action: Callable[[str, int], None] | None = None,
) -> RaceResult:
    """Poll accepted courses in priority order and commit at most one winner.

    Input order breaks ties in a poll. A Kallio course may win immediately when
    no preferred course is available. Only positive proof of an unsent add lets
    arbitration resume; an uncertain result permanently pins the same course.
    """
    specs = tuple(specs)
    if queue_policy not in {"fallback", "eager"}:
        raise BookingError("Jonopolitiikka on fallback tai eager.")
    if type(allow_queue) is not bool:
        raise BookingError("Jonovarausvalinnan on oltava totuusarvo.")
    if isinstance(queue_timeout, bool) or not isinstance(queue_timeout, (int, float)) or not 0 < queue_timeout <= 3600:
        raise BookingError("Jonovarauksen tarkistusajan on oltava 0–3600 sekuntia.")
    if not specs or any(not isinstance(spec, BookingSpec) for spec in specs):
        raise BookingError("Vähintään yksi hyväksytty kurssi tarvitaan.")
    identifiers = [spec.course.id for spec in specs]
    if len(set(identifiers)) != len(identifiers):
        raise BookingError("Samaa kurssia ei voi lisätä ehdokkaaksi kahdesti.")
    if len({normalize_text(spec.child_name) for spec in specs}) != 1:
        raise BookingError("Kaikkien ehdokkaiden on kuuluttava samalle lapselle.")
    identity = pool.identity
    if not isinstance(identity, str) or not identity:
        raise BookingError("Maksajan ja lapsen yhteinen tunniste puuttuu.")
    origin = validate_base_url(pool.base_url)
    if set(pool.backends) != set(identifiers):
        raise BookingError("Selainpolut eivät vastaa hyväksyttyjä kursseja.")
    # The scope deliberately excludes the candidate IDs: changing candidates
    # cannot create another lock or bypass an uncertain earlier purchase.
    scope = hashlib.sha256(f"{origin}\n{identity}".encode("utf-8")).hexdigest()
    root = Path(state_dir)
    lease_path = root / f"yksi-{scope}.json"
    lock_path = root / f"yksi-{scope}.lock"
    by_id = {spec.course.id: spec for spec in specs}
    end = max(spec.deadline for spec in specs)
    clock = lambda: _aware(now())
    failures = 0
    cooldowns: dict[int, datetime] = {}
    # Durable queue records prevent duplicate sends; only current successful
    # reads support a claim that an entry is still confirmed in this run.
    verified_queue_ids: set[int] = set()

    def emit(phase: str, **metadata):
        if progress is not None:
            progress(phase, metadata)

    def save_lease(lease: dict):
        if lease["queue"] is not None:
            lease["queues"][str(lease["queue"]["course_id"])] = dict(lease["queue"])
        _save_state(lease_path, lease)

    def with_queues(result: RaceResult, lease: dict) -> RaceResult:
        records = lease["queues"]
        # A queue upgraded into the confirmed actual winner is no longer an
        # independent waitlist entry. No additional post-payment reads are
        # needed to establish that distinction.
        actual_winner = result.course_id if result.status in {"confirmed", "already_confirmed"} else None
        confirmed = tuple((spec.course.id, records[str(spec.course.id)]["reservation_id"],
                           records[str(spec.course.id)]["position"])
                          for spec in specs if str(spec.course.id) in records
                          and spec.course.id != actual_winner
                          and spec.course.id in verified_queue_ids
                          and records[str(spec.course.id)]["phase"] == "queued")
        pending = tuple(spec.course.id for spec in specs if str(spec.course.id) in records
                        and spec.course.id != actual_winner
                        and (records[str(spec.course.id)]["phase"] != "queued"
                             or spec.course.id not in verified_queue_ids))
        return replace(result, queue_reservations=confirmed, queue_pending_ids=pending)

    def ensure_identity():
        if pool.identity != identity or validate_base_url(pool.base_url) != origin:
            raise BookingError("Maksaja, lapsi tai kauppa vaihtui kesken rinnakkaisen haun.")

    def wait(seconds: float, *, until: datetime | None = None):
        current = clock()
        duration = min(seconds, ((until or end) - current).total_seconds())
        if duration <= 0:
            return
        backend = pool.backends[identifiers[0]]
        if checkpoint is None and progress is None:
            backend.wait(duration)
            return
        target = current + timedelta(seconds=duration)
        while clock() < min(target, until or end):
            if checkpoint is not None:
                checkpoint()
            remaining = (min(target, until or end) - clock()).total_seconds()
            if remaining <= 0:
                break
            emit("heartbeat")
            backend.wait(min(1, remaining))

    def selected(lease: dict, course_id: int, *, existing_claim: bool = False,
                 grace_deadline: datetime | None = None) -> RaceResult | None:
        spec = by_id[course_id]
        if grace_deadline is not None and grace_deadline > spec.deadline:
            spec = replace(spec, deadline=grace_deadline)
        journal = root / f"yksi-{scope}-kurssi-{course_id}.json"
        # The actual-place path may upgrade this same course's queue. Until
        # the actual outcome is known, its older queue proof is insufficient.
        verified_queue_ids.discard(course_id)
        if checkpoint is not None:
            checkpoint()
        # The intent is durable before activating a writable page.
        lease["pinned_id"] = course_id
        lease["existing_claim"] = lease["existing_claim"] or existing_claim
        save_lease(lease)
        ensure_identity()
        try:
            backend = pool.activate(course_id)
        except Exception:
            # Activation does not authorize a different candidate without
            # positive evidence of no send, including after a process restart.
            raise
        if backend is not pool.backends[course_id]:
            raise BookingError("Aktivoitu selain ei vastaa valittua kurssia.")
        wrapper = _ArbitratedBackend(backend, spec, journal, existing_claim=lease["existing_claim"])
        try:
            result = run_booking(spec, wrapper, journal, now=now, log=log,
                                 on_add_not_sent=wrapper.on_add_not_sent, progress=progress,
                                 checkpoint=checkpoint, before_action=before_action)
        except _YieldSelection:
            if lease["existing_claim"] or not _can_release(journal, backend, spec):
                raise BookingError("Kurssia ei voi vaihtaa mahdollisesti lähetetyn varauksen jälkeen.")
            lease["pinned_id"] = None
            save_lease(lease)
            # Give an alternative path a chance after an unsent request.
            cooldowns[course_id] = clock() + timedelta(seconds=2)
            log(f"Kurssi {course_id}: pyyntöä ei lähetetty; muut hyväksytyt polut jatkavat.")
            return None
        return with_queues(RaceResult(result.status, course_id, result.reservation_id, result.receipt_id), lease)

    def actual_history() -> bool:
        for spec in specs:
            journal = root / f"yksi-{scope}-kurssi-{spec.course.id}.json"
            if journal.exists() or journal.is_symlink():
                state = _load_state(journal, pool.backends[spec.course.id].identity, spec)
                if (state["add_sends"] or state["checkout_sends"] or state["reservation_id"] is not None
                        or state["phase"] != "ready"):
                    return True
        return False

    def finish_queue(lease: dict, course_id: int, observation: Observation) -> RaceResult:
        if not observation.queued or not observation.queue_reservation_id:
            raise BookingError("Jonovarauksen vahvistettu tunnus puuttuu.")
        previous = lease["queue"]
        sends = previous["sends"] if previous is not None and previous["course_id"] == course_id else 0
        lease["queue"] = {"course_id": course_id, "phase": "queued", "sends": sends,
                          "reservation_id": observation.queue_reservation_id, "position": observation.queue_position}
        save_lease(lease)
        verified_queue_ids.add(course_id)
        log(f"Jonovaraus vahvistettu kurssille {course_id}; varsinaista kurssipaikkaa tai maksua ei vahvistettu.")
        emit("queued", course_id=course_id, queue_position=observation.queue_position)
        return with_queues(RaceResult("queued", course_id, queue_reservation_id=observation.queue_reservation_id,
                          queue_position=observation.queue_position), lease)

    def queue_fallback(lease: dict) -> RaceResult:
        if lease["pinned_id"] is not None or lease["existing_claim"] or actual_history():
            return RaceResult("deadline", lease["pinned_id"])
        grace_end = max(end, clock()) + timedelta(seconds=queue_timeout)
        queue_dispatched_here = False
        queue_cooldowns: dict[int, datetime] = {}
        failures = 0
        granted_queue: int | None = None
        log("Varsinaisen paikan hakuaika päättyi; tarkistetaan vielä vapaat paikat ennen jonovarausta.")
        emit("queue_phase")
        while clock() < grace_end:
            if checkpoint is not None:
                checkpoint()
            if clock() >= grace_end:
                break
            permitted_queue, granted_queue = granted_queue, None
            ensure_identity()
            try:
                observations = pool.observations()
            except (TransientBookingError, TimeoutError, OSError) as exc:
                failures += 1
                verified_queue_ids.clear()
                emit("retry")
                wait(max(min(2 ** min(failures, 4), 15), getattr(exc, "retry_after", 0)), until=grace_end)
                continue
            ensure_identity()
            if not isinstance(observations, Mapping) or set(observations) != set(identifiers):
                raise BookingError("Jonopolkujen havaintojoukko on virheellinen.")
            errors = [value for value in observations.values() if isinstance(value, TransientBookingError)]
            if errors:
                # selection_clear covers actual bookings, not existing queues.
                # Every candidate's queue state must be known before fallback.
                verified_queue_ids.clear()
                wait(max(2, *(error.retry_after for error in errors)), until=grace_end)
                continue
            observations = {course_id: value for course_id, value in observations.items()
                            if not isinstance(value, TransientBookingError)}
            for course_id, observation in observations.items():
                _check_observation(observation)
                emit("candidate", course_id=course_id, **_observation_metadata(observation))
            failures = 0
            existing = [course_id for course_id, value in observations.items()
                        if value.held or value.reservation_confirmed or value.reservation_id is not None]
            if len(existing) > 1:
                raise BookingError("Usealla hyväksytyllä kurssilla on jo varaus. Jonovarausta ei lähetetä.")
            if existing:
                if queue_dispatched_here:
                    # A real reservation appearing after queue dispatch needs
                    # explicit reconciliation on the next invocation.
                    return RaceResult("deadline", existing[0])
                result = selected(lease, existing[0], existing_claim=True, grace_deadline=grace_end)
                if result is not None:
                    return result
                continue
            if any(value.cart_has_others or not value.cart_empty for value in observations.values()):
                raise BookingError("Ostoskorissa on muu tai tunnistamaton tuote. Jonovarausta ei lähetetä.")
            if any(value.payment_locked for value in observations.values()):
                wait(2, until=grace_end)
                continue
            if not queue_dispatched_here:
                actual_visible = [spec for spec in specs if spec.course.id in observations
                          and spec.opens_at <= clock()
                          and observations[spec.course.id].available
                          and observations[spec.course.id].current_price is not None
                          and observations[spec.course.id].current_price <= spec.max_price]
                actual = [spec for spec in actual_visible
                          if clock() >= cooldowns.get(spec.course.id, spec.opens_at)]
                if actual:
                    result = selected(lease, actual[0].course.id, grace_deadline=grace_end)
                    if result is not None:
                        return result
                    continue
                if actual_visible:
                    wait(2, until=grace_end)
                    continue
            queue = lease["queue"]
            if queue is not None:
                observation = observations.get(queue["course_id"])
                if observation is not None and observation.queued:
                    return finish_queue(lease, queue["course_id"], observation)
                # Neither absence nor a returned error authorizes another
                # queue mutation after an earlier request may have been sent.
                wait(2, until=grace_end)
                continue
            queued = [spec.course.id for spec in specs
                      if spec.course.id in observations and observations[spec.course.id].queued]
            if queued:
                return finish_queue(lease, queued[0], observations[queued[0]])
            choices = [spec.course.id for spec in specs
                       if clock() >= queue_cooldowns.get(spec.course.id, spec.opens_at) and spec.opens_at <= clock()
                       and spec.course.id in observations and observations[spec.course.id].queue_available]
            if not choices:
                wait(2, until=grace_end)
                continue
            course_id = choices[0]
            if before_action is not None and permitted_queue != course_id:
                before_action("queue", course_id)
                granted_queue = course_id
                continue
            emit("queueing", course_id=course_id)
            lease["queue"] = {"course_id": course_id, "phase": "sending", "sends": 1,
                              "reservation_id": None, "position": None}
            save_lease(lease)
            ensure_identity()
            try:
                backend = pool.activate_queue(course_id)
                if backend is not pool.backends[course_id]:
                    raise BookingError("Aktivoitu selain ei vastaa valittua jonokurssia.")
                backend.join_queue()
            except ActionNotSent as exc:
                lease["queue"] = None
                lease["queues"].pop(str(course_id), None)
                save_lease(lease)
                queue_cooldowns[course_id] = clock() + timedelta(seconds=2)
                log(f"Jonopyyntöä ei lähetetty; toinen hyväksytty jono voidaan tarkistaa: {exc}")
                continue
            except Exception as exc:
                queue_dispatched_here = True
                log(f"Jonopyynnön tulos on epävarma; pyyntöä ei lähetetä uudelleen: {exc}")
            else:
                queue_dispatched_here = True
                lease["queue"]["phase"] = "sent"
                save_lease(lease)
            wait(2, until=grace_end)
        queue = lease["queue"]
        if queue is not None:
            log("Jonopyynnön nykyistä vahvistusta ei saatu; uutta jonopyyntöä ei lähetetty.")
            return with_queues(RaceResult("queue_pending", queue["course_id"]), lease)
        return with_queues(RaceResult("deadline"), lease)

    eager_cooldowns: dict[int, datetime] = {}

    def reconcile_queues(lease: dict, observations: Mapping[int, Observation]):
        verified_queue_ids.clear()
        records = lease["queues"]
        changed = False
        for spec in specs:
            course_id = spec.course.id
            observation = observations.get(course_id)
            if observation is None:
                continue
            previous = records.get(str(course_id))
            if not observation.queued:
                if previous is not None and previous["phase"] == "queued":
                    record = dict(previous, phase="missing")
                    records[str(course_id)] = record
                    if lease["queue"] is not None and lease["queue"]["course_id"] == course_id:
                        lease["queue"] = dict(record)
                    changed = True
                    emit("queue_pending", course_id=course_id)
                continue
            verified_queue_ids.add(course_id)
            record = {"course_id": course_id, "phase": "queued",
                      "sends": previous["sends"] if previous is not None else 0,
                      "reservation_id": observation.queue_reservation_id,
                      "position": observation.queue_position}
            if previous != record:
                records[str(course_id)] = record
                if lease["queue"] is not None and lease["queue"]["course_id"] == course_id:
                    lease["queue"] = dict(record)
                changed = True
                emit("queued", course_id=course_id, queue_position=observation.queue_position)
        if changed:
            save_lease(lease)

    def eager_queues(lease: dict, observations: Mapping[int, Observation],
                     permitted_queue: int | None) -> tuple[bool, int | None]:
        """Dispatch at most one accepted queue, independently of real places.

        Queues neither own the actual-place lease nor authorize a checkout.
        A possible queue send is recorded per course and never replayed. Its
        uncertainty does not hide a different acceptable queue or real place.
        """
        if lease["pinned_id"] is not None or lease["existing_claim"] or actual_history():
            return False, None
        records = lease["queues"]
        current = clock()
        choices = [spec.course.id for spec in specs
                   if spec.opens_at <= current < spec.deadline
                   and current >= eager_cooldowns.get(spec.course.id, spec.opens_at)
                   and str(spec.course.id) not in records
                   and spec.course.id in observations
                   and observations[spec.course.id].queue_available]
        if not choices:
            return False, None
        course_id = choices[0]
        if before_action is not None and permitted_queue != course_id:
            before_action("queue", course_id)
            return True, course_id
        emit("queueing", course_id=course_id)
        record = {"course_id": course_id, "phase": "sending", "sends": 1,
                  "reservation_id": None, "position": None}
        records[str(course_id)] = record
        lease["queue"] = dict(record)
        save_lease(lease)
        ensure_identity()
        try:
            backend = pool.activate_queue(course_id)
            if backend is not pool.backends[course_id]:
                raise BookingError("Aktivoitu selain ei vastaa valittua jonokurssia.")
            backend.join_queue()
        except ActionNotSent as exc:
            records.pop(str(course_id), None)
            lease["queue"] = None
            save_lease(lease)
            eager_cooldowns[course_id] = clock() + timedelta(seconds=2)
            log(f"Jonopyyntöä ei lähetetty kurssille {course_id}; muut hyväksytyt polut jatkavat: {exc}")
            emit("retry", course_id=course_id, action="queue", retry_after=2)
        except Exception as exc:
            log(f"Kurssin {course_id} jonopyynnön tulos on epävarma; tätä pyyntöä ei toisteta. "
                f"Muiden kurssien haku jatkuu: {exc}")
            emit("queue_pending", course_id=course_id)
        else:
            record["phase"] = "sent"
            lease["queue"] = dict(record)
            save_lease(lease)
        # A fresh all-candidate read follows before another queue mutation.
        return True, None

    def eager_result(lease: dict) -> RaceResult:
        result = with_queues(RaceResult("deadline"), lease)
        if result.queue_reservations:
            course_id, reservation_id, position = result.queue_reservations[0]
            return replace(result, status="queued", course_id=course_id,
                           queue_reservation_id=reservation_id, queue_position=position)
        if result.queue_pending_ids:
            return replace(result, status="queue_pending", course_id=result.queue_pending_ids[0])
        return result

    with _account_lock(lock_path):
        if checkpoint is not None:
            checkpoint()
        lease = _load_lease(lease_path, identity, origin, specs)
        save_lease(lease)
        for spec in specs:
            emit("waiting", course_id=spec.course.id, opens_at=spec.opens_at.isoformat(),
                 deadline=spec.deadline.isoformat())
        if lease["pinned_id"] is not None:
            log(f"Jatketaan aiemmin valittua kurssia {lease['pinned_id']}; muut polut pysyvät lukittuina.")
            result = selected(lease, lease["pinned_id"])
            if result is not None:
                return result
        granted_queue: int | None = None
        while clock() < end:
            if checkpoint is not None:
                checkpoint()
            if clock() >= end:
                break
            permitted_queue, granted_queue = granted_queue, None
            ensure_identity()
            try:
                observations = pool.observations()
            except (TransientBookingError, TimeoutError, OSError) as exc:
                failures += 1
                verified_queue_ids.clear()
                delay = max(min(2 ** min(failures, 4), 15), getattr(exc, "retry_after", 0))
                log(f"Kurssipolkujen tilaa ei saatu; tarkistetaan {delay:g} s kuluttua: {exc}")
                emit("retry", retry_after=delay)
                wait(delay)
                continue
            ensure_identity()
            if not isinstance(observations, Mapping) or set(observations) != set(identifiers):
                raise BookingError("Kurssipolkujen havaintojoukko on virheellinen.")
            errors = [value for value in observations.values() if isinstance(value, TransientBookingError)]
            if errors and getattr(pool, "selection_clear", False) is not True:
                # An unknown candidate may already have a confirmed booking.
                # Wait until every own-reservation observation is authoritative.
                failures += 1
                verified_queue_ids.clear()
                wait(max(min(2 ** min(failures, 4), 15), *(error.retry_after for error in errors)))
                continue
            observations = {course_id: value for course_id, value in observations.items()
                            if not isinstance(value, TransientBookingError)}
            for course_id, value in observations.items():
                _check_observation(value)
                emit("candidate", course_id=course_id, **_observation_metadata(value))
            if allow_queue and queue_policy == "eager":
                reconcile_queues(lease, observations)
            failures = 0
            existing = [course_id for course_id, value in observations.items()
                        if value.held or value.reservation_confirmed or value.reservation_id is not None]
            if len(existing) > 1:
                raise BookingError("Usealla hyväksytyllä kurssilla on jo varaus. Uutta maksua ei tehdä.")
            if existing:
                result = selected(lease, existing[0], existing_claim=True)
                if result is not None:
                    return result
                continue
            if any(value.cart_has_others or not value.cart_empty for value in observations.values()):
                raise BookingError("Ostoskorissa on muu tai tunnistamaton tuote. Uutta varausta ei tehdä.")
            if any(value.payment_locked for value in observations.values()):
                wait(2)
                continue
            current = clock()
            choices = [spec for spec in specs
                       if spec.opens_at <= current < spec.deadline
                       and current >= cooldowns.get(spec.course.id, spec.opens_at)
                       and spec.course.id in observations
                       and observations[spec.course.id].available
                       and observations[spec.course.id].current_price is not None
                       and observations[spec.course.id].current_price <= spec.max_price]
            if choices:
                winner = choices[0].course.id
                log(f"Kurssi {winner} saa yhteisen varausvuoron; muut polut jäävät seurantaan.")
                result = selected(lease, winner)
                if result is not None:
                    return result
                continue
            if allow_queue and queue_policy == "eager":
                advanced, granted_queue = eager_queues(lease, observations, permitted_queue)
                if advanced:
                    continue
            future = [(spec.opens_at - current).total_seconds() for spec in specs if spec.opens_at > current]
            # Keep the final minute responsive while limiting pre-opening load.
            wait(min(30, min(future) - 60) if future and min(future) > 60 else 2)
        log("Määräaika saavutettu; yhdellekään kurssille ei vahvistettu uutta varausta.")
        emit("deadline")
        if allow_queue and queue_policy == "eager":
            return eager_result(lease)
        if allow_queue:
            return queue_fallback(lease)
        return RaceResult("deadline")
