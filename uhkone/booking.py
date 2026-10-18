"""Durable, sequential booking controller; the backend owns browser/API access.

The shop exposes no idempotency key. A possibly sent add or checkout is never
automatically repeated: missing cart evidence cannot rule out a delayed commit.
Only positive proof that a request was not sent permits another attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Callable, Protocol

from .models import Course, normalize_text


class BookingError(ValueError):
    """A booking cannot safely continue with the supplied target or evidence."""


class TransientBookingError(BookingError):
    def __init__(self, message: str = "Tilapäinen palveluvirhe", retry_after: float = 0):
        super().__init__(message)
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = 0
        self.retry_after = delay if math.isfinite(delay) and delay > 0 else 0


class ActionNotSent(BookingError):
    """Positive evidence that the mutation request was never sent."""


class ActionUncertain(BookingError):
    """A mutation may have reached the shop; reconcile before proceeding."""


@dataclass(frozen=True)
class BookingSpec:
    course: Course
    child_name: str
    max_price: Decimal
    opens_at: datetime
    deadline: datetime
    payment_mode: str
    payment_type_id: int | None = None

    def __post_init__(self):
        if type(self.course.id) is not int or self.course.id <= 0:
            raise BookingError("Kurssitunnuksen on oltava positiivinen kokonaisluku.")
        if not isinstance(self.child_name, str) or not self.child_name.strip():
            raise BookingError("Lapsen tarkka nimi puuttuu.")
        if not isinstance(self.max_price, Decimal) or not self.max_price.is_finite() or self.max_price < 0:
            raise BookingError("Enimmäishinnan on oltava vähintään nolla.")
        for value in (self.opens_at, self.deadline):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise BookingError("Avaus- ja määräajoissa on oltava aikavyöhyke.")
        if self.deadline <= self.opens_at:
            raise BookingError("Määräajan on oltava avaamisajan jälkeen.")
        if self.payment_mode not in {"saldo", "fastpay", "verkkomaksu"}:
            raise BookingError("Maksutapa on saldo, fastpay tai verkkomaksu.")
        if self.payment_type_id is not None and (type(self.payment_type_id) is not int or self.payment_type_id <= 0):
            raise BookingError("Maksutunnuksen on oltava positiivinen kokonaisluku.")


@dataclass(frozen=True)
class Observation:
    available: bool = False
    current_price: Decimal | None = None
    held: bool = False
    reservation_id: str | None = None
    reservation_confirmed: bool = False
    receipt_id: str | None = None
    receipt_closed: bool = False
    receipt_total: Decimal | None = None
    cart_has_others: bool = False
    cart_empty: bool = True
    payment_locked: bool = False
    queue_available: bool = False
    queued: bool = False
    queue_reservation_id: str | None = None
    queue_position: int | None = None


@dataclass(frozen=True)
class BookingResult:
    status: str
    reservation_id: str | None = None
    receipt_id: str | None = None


class BookingBackend(Protocol):
    identity: str

    def snapshot(self) -> Observation: ...
    def add_selected_course(self) -> None: ...
    def checkout(self) -> None: ...
    def wait(self, seconds: float) -> None: ...


def _money(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BookingError("Kellon on palautettava aikavyöhykkeellinen aika.")
    return value.astimezone(timezone.utc)


def _binding(spec: BookingSpec) -> dict:
    return {
        "course_id": spec.course.id,
        "course_name": spec.course.name,
        "location": spec.course.location,
        "start": spec.course.start.isoformat() if spec.course.start else None,
        "end": spec.course.end.isoformat() if spec.course.end else None,
        "child_name": normalize_text(spec.child_name),
        "max_price": _money(spec.max_price),
        "opens_at": _aware(spec.opens_at).isoformat(),
        "payment_mode": spec.payment_mode,
        "payment_type_id": spec.payment_type_id,
    }


def _new_state(identity: str, spec: BookingSpec) -> dict:
    return {
        "version": 1, "identity": identity, "spec": _binding(spec), "phase": "ready",
        "attempts": {"add": 0, "checkout": 0}, "add_sends": 0, "checkout_sends": 0,
        "expected_price": None, "add_started": None, "absent_count": 0, "last_absent": None,
        "baseline_receipt_id": None, "reservation_id": None, "receipt_id": None,
        "result_status": None,
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BookingError("Varaustilassa on toistuva JSON-avain.")
        result[key] = value
    return result


def _load_state(path: Path, identity: str, spec: BookingSpec) -> dict:
    if path.is_symlink():
        raise BookingError("Varaustila ei saa olla symbolinen linkki.")
    if not path.exists():
        return _new_state(identity, spec)
    try:
        state = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        template = _new_state(identity, spec)
        if not isinstance(state, dict) or set(state) != set(template):
            raise ValueError("Tuntematon tilatiedoston rakenne")
        if type(state["version"]) is not int or state["version"] != 1:
            raise ValueError("Tuntematon tilatiedoston versio")
        if (state["identity"] != identity
                or json.dumps(state["spec"], sort_keys=True) != json.dumps(template["spec"], sort_keys=True)):
            raise BookingError("Tallennettu varaustila kuuluu eri lapselle, kurssille, maksutavalle tai hintarajalle.")
        if state["phase"] not in {"ready", "adding", "held", "paying", "confirmed"}:
            raise ValueError("Tuntematon varausvaihe")
        if not isinstance(state["attempts"], dict) or set(state["attempts"]) != {"add", "checkout"}:
            raise ValueError("Virheelliset yrityslaskurit")
        for value in [*state["attempts"].values(), state["add_sends"], state["checkout_sends"], state["absent_count"]]:
            if type(value) is not int or value < 0:
                raise ValueError("Virheellinen yrityslaskuri")
        if state["add_sends"] > 3 or state["checkout_sends"] > 1:
            raise ValueError("Liian monta lähetettyä toimintoa")
        if (state["add_sends"] > state["attempts"]["add"]
                or state["checkout_sends"] > state["attempts"]["checkout"]
                or (state["phase"] not in {"paying", "confirmed"} and state["checkout_sends"])):
            raise ValueError("Ristiriitaiset yrityslaskurit")
        for key in ("add_started", "last_absent"):
            if state[key] is not None:
                _aware(datetime.fromisoformat(state[key]))
        for key in ("baseline_receipt_id", "reservation_id", "receipt_id"):
            if state[key] is not None and (not isinstance(state[key], str) or not state[key]):
                raise ValueError("Virheellinen kuitti- tai varaustunnus")
        if state["expected_price"] is not None:
            if not isinstance(state["expected_price"], str):
                raise ValueError("Tallennetun hinnan muoto on virheellinen")
            price = Decimal(state["expected_price"])
            if not price.is_finite() or not 0 <= price <= spec.max_price:
                raise ValueError("Virheellinen tallennettu hinta")
        if state["phase"] == "adding" and state["add_started"] is None:
            raise ValueError("Lisäyksen ajankohta puuttuu")
        if state["phase"] in {"held", "paying", "confirmed"} and state["expected_price"] is None:
            # A preexisting confirmed booking needs no payment-price evidence.
            if not (state["phase"] == "confirmed" and state["result_status"] == "already_confirmed"):
                raise ValueError("Varausvaiheen hinta puuttuu")
        if state["phase"] == "confirmed":
            if state["result_status"] not in {"confirmed", "already_confirmed"} or not state["reservation_id"]:
                raise ValueError("Vahvistuksen tiedot puuttuvat")
            if state["result_status"] == "confirmed" and not state["receipt_id"]:
                raise ValueError("Maksuvahvistuksen kuitti puuttuu")
        elif state["result_status"] is not None:
            raise ValueError("Odottamaton lopputila")
        return state
    except (OSError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
        raise BookingError(f"Varaustilaa ei voi lukea: {exc}") from exc


def _save_state(path: Path, state: dict) -> None:
    temporary = None
    try:
        if path.is_symlink():
            raise BookingError("Varaustila ei saa olla symbolinen linkki.")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".varaus-", delete=False) as file:
            temporary = file.name
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        # Persist the directory entry as well as the temporary file contents.
        if hasattr(os, "O_DIRECTORY"):
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except OSError as exc:
        raise BookingError(f"Varaustilaa ei voi tallentaa: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _check_cart(observation: Observation) -> None:
    if observation.cart_has_others or (not observation.cart_empty and not observation.held):
        raise BookingError("Ostoskorissa on muu tai tunnistamaton tuote. Ostoskoria ei muuteta.")


def _check_observation(observation: Observation) -> None:
    if not isinstance(observation, Observation):
        raise BookingError("Varausrajapinta palautti tuntemattoman tilan.")
    for key in ("available", "held", "reservation_confirmed", "receipt_closed", "cart_has_others", "cart_empty", "payment_locked",
                "queue_available", "queued"):
        if type(getattr(observation, key)) is not bool:
            raise BookingError("Varausrajapinnan totuusarvo ei ole yksiselitteinen.")
    for value in (observation.reservation_id, observation.receipt_id, observation.queue_reservation_id):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise BookingError("Varausrajapinnan kuitti- tai varaustunnus on virheellinen.")
    if observation.queue_position is not None and (type(observation.queue_position) is not int or observation.queue_position < 1):
        raise BookingError("Jonosijan on oltava positiivinen kokonaisluku.")
    if observation.queued != (observation.queue_reservation_id is not None):
        raise BookingError("Jonovarauksen vahvistus ja tunnus eivät vastaa toisiaan.")
    if not observation.queued and observation.queue_position is not None:
        raise BookingError("Jonosija ei kuulu vahvistettuun jonovaraukseen.")
    for value in (observation.current_price, observation.receipt_total):
        if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
            raise BookingError("Varausrajapinnan hintatieto on virheellinen.")


def _write_price(observation: Observation, spec: BookingSpec) -> Decimal:
    price = observation.current_price
    if not isinstance(price, Decimal) or not price.is_finite() or price < 0:
        raise BookingError("Ajantasainen hinta puuttuu; varausta tai maksua ei lähetetä.")
    if price > spec.max_price:
        raise BookingError("Ajantasainen hinta ylittää annetun enimmäishinnan.")
    return price


def _observation_metadata(observation: Observation) -> dict:
    """Only public availability/state flags leave the transaction controller."""
    return {
        "available": observation.available,
        "held": observation.held,
        "confirmed": observation.reservation_confirmed,
        "payment_locked": observation.payment_locked,
        "queue_available": observation.queue_available,
        "queued": observation.queued,
    }


def run_booking(
    spec: BookingSpec, backend: BookingBackend, state_path: Path, *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    log: Callable[[str], None] = print,
    on_add_not_sent: Callable[[], None] | None = None,
    progress: Callable[[str, dict], None] | None = None,
    checkpoint: Callable[[], None] | None = None,
    before_action: Callable[[str, int], None] | None = None,
) -> BookingResult:
    """Book one course, with optional main-thread human controls.

    Controls run before durable mutation intents, never between an intent and
    its dispatch. After a human action prompt, another authoritative snapshot
    is required before a write; a pause cannot authorize a stale observation.
    """
    def emit(phase: str, **metadata) -> None:
        if progress is not None:
            progress(phase, {"course_id": spec.course.id, **metadata})

    if checkpoint is not None:
        checkpoint()
    identity = backend.identity
    if not isinstance(identity, str) or not identity:
        raise BookingError("Maksajan, lapsen ja kurssin varma tunniste puuttuu.")
    state = _load_state(state_path, identity, spec)
    if state["phase"] == "confirmed":
        # Durable proof records a past success. A later cancellation must not
        # be reported as a currently valid reservation or trigger a new charge.
        try:
            current = backend.snapshot()
        except (TransientBookingError, TimeoutError, OSError) as exc:
            raise BookingError(f"Aiemmin valmistuneen varauksen nykytilaa ei voi varmistaa: {exc}") from exc
        _check_observation(current)
        if (backend.identity != identity or not isinstance(current, Observation)
                or not current.reservation_confirmed
                or current.reservation_id != state["reservation_id"]):
            raise BookingError("Aiemmin valmistunut varaus ei ole enää vahvistettu samalla tunnuksella. Uutta maksua ei lähetetä.")
        emit("confirmed", status=state["result_status"])
        return BookingResult(state["result_status"], state["reservation_id"], state["receipt_id"])
    _save_state(state_path, state)
    log(f"Kurssi {spec.course.id}: avaus {spec.opens_at.isoformat()}, määräaika {spec.deadline.isoformat()}.")
    failures = 0
    add_failures = 0
    checkout_failures = 0
    preflight = False
    pending_without_cart_reported = False
    granted_action: str | None = None
    emit("waiting", opens_at=spec.opens_at.isoformat(), deadline=spec.deadline.isoformat())

    def wait(seconds: float) -> None:
        current = _aware(now())
        duration = min(seconds, (spec.deadline - current).total_seconds())
        if duration <= 0:
            return
        if checkpoint is None and progress is None:
            backend.wait(duration)
            return
        target = current + timedelta(seconds=duration)
        while _aware(now()) < min(target, spec.deadline):
            if checkpoint is not None:
                checkpoint()
            remaining = (min(target, spec.deadline) - _aware(now())).total_seconds()
            if remaining <= 0:
                break
            emit("heartbeat", state=state["phase"])
            backend.wait(min(1, remaining))

    def ensure_identity() -> None:
        if backend.identity != identity:
            raise BookingError("Maksaja, lapsi tai kurssi vaihtui kesken varauksen.")

    def retry_delay(exc: Exception, count: int) -> float:
        retry_after = getattr(exc, "retry_after", 0)
        return max(min(2 ** min(count, 4), 15), retry_after)

    def finish(status: str, observation: Observation) -> BookingResult:
        state.update(phase="confirmed", result_status=status, reservation_id=observation.reservation_id,
                     receipt_id=observation.receipt_id if status == "confirmed" else None)
        _save_state(state_path, state)
        log("Varaus ja maksu vahvistettu." if status == "confirmed" else "Täsmällinen varaus oli jo vahvistettu; maksusta ei tehdä uutta väitettä.")
        emit("confirmed", status=status)
        return BookingResult(status, state["reservation_id"], state["receipt_id"])

    while _aware(now()) < spec.deadline:
        if checkpoint is not None:
            checkpoint()
        if _aware(now()) >= spec.deadline:
            break
        # A prompt grants at most the next fresh observation, not a later
        # availability change or a recovery from an intervening read failure.
        permitted_action, granted_action = granted_action, None
        ensure_identity()
        until_open = (spec.opens_at - _aware(now())).total_seconds()
        if preflight and until_open > 60:
            wait(min(30, until_open - 60))
            continue
        try:
            observation = backend.snapshot()
        except (TransientBookingError, TimeoutError, OSError) as exc:
            failures += 1
            delay = retry_delay(exc, failures)
            log(f"Tilapäinen lukuvirhe; uusi tarkistus {delay:g} s kuluttua: {exc}")
            emit("retry", retry_after=delay)
            wait(delay)
            continue
        ensure_identity()
        _check_observation(observation)
        emit("candidate", **_observation_metadata(observation))
        failures = 0
        preflight = True
        if observation.reservation_confirmed and not observation.reservation_id:
            raise BookingError("Vahvistetun varauksen tunnus puuttuu.")
        if observation.reservation_id:
            state["reservation_id"] = observation.reservation_id
        if observation.reservation_confirmed and state["add_sends"] == state["checkout_sends"] == 0:
            return finish("already_confirmed", observation)
        _check_cart(observation)
        if (state["phase"] == "ready" and state["add_sends"] == state["checkout_sends"] == 0
                and state["reservation_id"] is not None
                and not observation.held and not observation.reservation_confirmed):
            # An own reservation may appear before its cart or final status.
            # It can also belong to another session's cart. Do not add again.
            _save_state(state_path, state)
            if not pending_without_cart_reported:
                log("Oma keskeneräinen varaus löytyi, mutta sitä ei näy tämän selainistunnon ostoskorissa. "
                    "Seurataan samaa varausta; muita kursseja ei varata. Jos varaus aloitettiin toisessa profiilissa, "
                    "maksua jatketaan alkuperäisessä selainprofiilissa.")
                pending_without_cart_reported = True
            emit("held", in_this_cart=False)
            wait(2)
            continue
        if state["phase"] == "paying":
            expected = Decimal(state["expected_price"])
            if (observation.reservation_confirmed and observation.cart_empty
                    and observation.receipt_closed and observation.receipt_id
                    and observation.receipt_id != state["baseline_receipt_id"]
                    and observation.receipt_total == expected):
                return finish("confirmed", observation)
            emit("payment_pending")
            wait(5)
            continue
        if _aware(now()) < spec.opens_at:
            emit("waiting", opens_at=spec.opens_at.isoformat())
            wait(min(2, (spec.opens_at - _aware(now())).total_seconds()))
            continue
        if _aware(now()) >= spec.deadline:
            break
        if observation.payment_locked:
            if state["phase"] == "adding":
                state.update(add_started=_aware(now()).isoformat(), absent_count=0, last_absent=None)
                _save_state(state_path, state)
            wait(2)
            continue
        if observation.held:
            price = _write_price(observation, spec)
            state.update(phase="held", expected_price=_money(price), absent_count=0, last_absent=None)
            _save_state(state_path, state)
            emit("held", in_this_cart=True)
        elif observation.reservation_confirmed:
            # A reservation appeared after a possible add. Do not add or pay
            # again while its payment evidence is still catching up.
            if state["expected_price"] is None:
                raise ActionUncertain("Varaus ilmestyi, mutta maksun hintatieto puuttuu.")
            state["phase"] = "paying"
            _save_state(state_path, state)
            emit("payment_pending")
            wait(2)
            continue
        elif state["phase"] in {"adding", "held"} or state["add_sends"] > 0:
            # A missing cart or reservation is not proof that an earlier add
            # failed. This also locks old journals that once allowed retries.
            if state["phase"] != "adding":
                state.update(phase="adding", add_started=_aware(now()).isoformat(), absent_count=0, last_absent=None)
                _save_state(state_path, state)
            emit("reservation_pending")
            wait(2)
            continue

        if state["phase"] == "held":
            _write_price(observation, spec)
            if before_action is not None and permitted_action != "checkout":
                before_action("checkout", spec.course.id)
                granted_action = "checkout"
                continue
            emit("paying")
            state.update(phase="paying", baseline_receipt_id=observation.receipt_id)
            state["attempts"]["checkout"] += 1
            state["checkout_sends"] += 1
            _save_state(state_path, state)
            ensure_identity()
            delay = 2
            try:
                backend.checkout()
            except ActionNotSent as exc:
                checkout_failures += 1
                delay = retry_delay(exc, checkout_failures)
                state.update(phase="held", baseline_receipt_id=None)
                state["checkout_sends"] -= 1
                _save_state(state_path, state)
                log(f"Maksupyyntöä ei lähetetty; uusi tarkistus ennen yritystä: {exc}")
                emit("retry", action="checkout", retry_after=delay)
            except Exception as exc:
                checkout_failures += 1
                delay = retry_delay(exc, checkout_failures)
                log(f"Maksun tulos on epävarma; maksupyyntöä ei lähetetä uudelleen: {exc}")
                emit("payment_pending")
            else:
                checkout_failures = 0
            wait(delay)
            continue

        if state["phase"] == "ready" and observation.available and state["add_sends"] == 0:
            price = _write_price(observation, spec)
            if before_action is not None and permitted_action != "add":
                before_action("add", spec.course.id)
                granted_action = "add"
                continue
            emit("adding")
            state.update(phase="adding", expected_price=_money(price), add_started=_aware(now()).isoformat(),
                         absent_count=0, last_absent=None, baseline_receipt_id=observation.receipt_id)
            state["attempts"]["add"] += 1
            state["add_sends"] += 1
            _save_state(state_path, state)
            ensure_identity()
            delay = 2
            try:
                backend.add_selected_course()
            except ActionNotSent as exc:
                add_failures += 1
                delay = retry_delay(exc, add_failures)
                state["phase"] = "ready"
                state["add_sends"] -= 1
                _save_state(state_path, state)
                log(f"Lisäyspyyntöä ei lähetetty; uusi tarkistus ennen yritystä: {exc}")
                emit("retry", action="add", retry_after=delay)
                if on_add_not_sent is not None:
                    on_add_not_sent()
            except Exception as exc:
                add_failures += 1
                delay = retry_delay(exc, add_failures)
                log(f"Lisäyksen tulos on epävarma; tarkistetaan palvelimen tila ilman uutta lisäyspyyntöä: {exc}")
                emit("reservation_pending")
            else:
                add_failures = 0
            wait(delay)
            continue
        wait(2)

    log(f"Määräaika saavutettu. Tallennettu vaihe: {state['phase']}; valmistumista ei vahvistettu.")
    emit("deadline", state=state["phase"])
    return BookingResult("deadline", state["reservation_id"], None)
