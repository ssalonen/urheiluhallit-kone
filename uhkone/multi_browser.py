"""Several prepared course tabs, with one exclusive route for purchases.

The sync Playwright API stays on one thread. Public course GET requests run in
the browser with at most four requests in flight; account state is read once per
cycle and shared by all candidate observations. Only the activated tab (and its
payment popup) can ever send a mutation. An actual reservation/payment pins the
choice; independent waitlist entries leave other actual course options open.
"""
from __future__ import annotations

import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .booking import BookingError, BookingSpec, Observation, TransientBookingError
from .booking_browser import (
    BookingBrowser,
    _BookingGuard,
    _cart_match,
    _flag,
    _fresh_course,
    _integer,
    _money,
    _observation,
    _preflight_page,
    _retry_preflight,
)
from .browser import BrowserError, _browser, _ensure_login, _validate_course_url
from .client import BASE_URL
from .origin import same_origin, validate_base_url

_COURSE_READS = """async jobs => {
  const results = [];
  let next = 0;
  async function worker() {
    while (next < jobs.length) {
      const job = jobs[next++];
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch(job.url, {
          method: 'GET', credentials: 'same-origin', redirect: 'error',
          headers: {'Accept': 'application/json'}, signal: controller.signal
        });
        const text = await response.text();
        let value = null, invalid_json = false;
        try { value = JSON.parse(text); } catch (_) { invalid_json = true; }
        results.push({id: job.id, status: response.status,
          retry_after: response.headers.get('Retry-After') || '', value, invalid_json});
      } catch (_) {
        results.push({id: job.id, transport_error: true});
      } finally { clearTimeout(timer); }
    }
  }
  await Promise.all(Array.from({length: Math.min(4, jobs.length)}, worker));
  return results;
}"""


def _retry_after(value: str) -> float:
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


def _candidate_rows(values: Any, expected_ids: set[int]) -> dict[int, dict | TransientBookingError]:
    """Validate browser GET results without accepting unknown/missing groups."""
    if not isinstance(values, list) or len(values) != len(expected_ids):
        raise BookingError("Rinnakkaisten kurssihakujen tietomuoto muuttui.")
    result = {}
    for item in values:
        if not isinstance(item, dict) or _integer(item.get("id")) not in expected_ids:
            raise BookingError("Rinnakkainen kurssihaku palautti tuntemattoman kurssin.")
        course_id = _integer(item["id"])
        assert course_id is not None  # Checked against expected_ids above.
        if course_id in result:
            raise BookingError("Rinnakkainen kurssihaku palautti saman kurssin kahdesti.")
        if item.get("transport_error") is True:
            result[course_id] = TransientBookingError("Yhden kurssisivun lukupyyntö katkesi ruuhkan aikana.")
            continue
        status = _integer(item.get("status"))
        if status is None:
            raise BookingError("Rinnakkaisen kurssihaun HTTP-tila puuttuu.")
        if status == 429 or status >= 500:
            result[course_id] = TransientBookingError(
                f"Yhden kurssin haku on ruuhkautunut (HTTP {status}).",
                retry_after=_retry_after(item.get("retry_after", "")),
            )
        elif status in {401, 403} or 300 <= status < 400:
            raise BookingError("Kirjautuminen ei ole enää voimassa kurssien rinnakkaishaussa.")
        elif status < 200 or status >= 300 or item.get("invalid_json") or not isinstance(item.get("value"), dict):
            raise BookingError("Kurssien rinnakkaishaku ei palauttanut odotettua JSON-tietoa.")
        else:
            result[course_id] = item["value"]
    return result


class _PoolRouting:
    def __init__(self, context, specs: Sequence[BookingSpec], *, base_url: str):
        self.read_guard = _BookingGuard(context, specs[0], base_url=base_url)
        self.backends: dict[int, BookingBrowser] = {}
        self.active_id: int | None = None
        self.actual_committed_id: int | None = None

    @property
    def committed_id(self) -> int | None:
        """Compatibility name for the actual-purchase pin."""
        return self.actual_committed_id

    def _active_page(self, request) -> bool:
        if self.active_id is None:
            return False
        try:
            page = request.frame.page
            target = self.backends[self.active_id].page
            for _ in range(8):
                if page is target:
                    return True
                page = page.opener()
                if page is None:
                    return False
        except Exception:  # noqa: BLE001 - An inaccessible request frame cannot authorize a write.
            return False
        return False

    def activate(self, course_id: int) -> BookingBrowser:
        if course_id not in self.backends:
            raise BookingError("Valittu kurssi ei kuulu valmisteltuihin vaihtoehtoihin.")
        if self.actual_committed_id is not None and self.actual_committed_id != course_id:
            raise BookingError("Yhdelle kurssille on jo voitu lähettää varaus. Toista kurssia ei avata ostettavaksi.")
        backend = self.backends[course_id]
        # Background tabs may throttle animation frames while their live course
        # list re-renders. Foreground the winner before Playwright checks that
        # the actual shop controls are stable and ready for interaction.
        backend.page.bring_to_front()
        self.active_id = course_id
        return backend

    def activate_queue(self, course_id: int) -> BookingBrowser:
        if self.actual_committed_id is not None:
            raise BookingError("Varsinainen varaus tai maksu on jo voitu lähettää. Uusia jonopyyntöjä ei sallita.")
        return self.activate(course_id)

    def route(self, route) -> None:
        if self._active_page(route.request) and self.active_id is not None:
            backend = self.backends[self.active_id]
            was_sent = backend.guard.sent
            try:
                backend.guard.route(route)
            finally:
                if backend.guard.actual_sent:
                    self.actual_committed_id = self.active_id
                if backend.guard.sent and not was_sent:
                    for candidate in self.backends.values():
                        candidate._last_observation = None
        else:
            # Even a payload matching the winner is blocked in any other tab.
            self.read_guard.route(route)

    def response(self, response) -> None:
        if self._active_page(response.request) and self.active_id is not None:
            self.backends[self.active_id].guard.response(response)


class BookingPool:
    def __init__(self, specs: Sequence[BookingSpec], context, pages, routing: _PoolRouting,
                 *, base_url: str = BASE_URL):
        self.specs = tuple(specs)
        self.base_url = validate_base_url(base_url)
        self.context, self.routing = context, routing
        self.identity = ""
        self.selection_clear = False
        self.preparation_errors: dict[int, TransientBookingError] = {}
        self._shared_cart: list[dict] = []
        self._candidate_activity = False
        self.backends = {
            spec.course.id: BookingBrowser(spec, context, page,
                _BookingGuard(context, spec, base_url=self.base_url), base_url=self.base_url)
            for spec, page in zip(self.specs, pages)
        }
        self.routing.backends = self.backends
        for backend in self.backends.values():
            backend.guard.allow_login = False

    def _reader(self) -> BookingBrowser:
        return next(iter(self.backends.values()))

    def _bind(self, user: dict) -> None:
        for backend in self.backends.values():
            backend._bind_user(user)
        reader = self._reader()
        identity = f"{self.base_url}|{reader.payer_id}:{reader.child_id}"
        if self.identity and identity != self.identity:
            raise BookingError("Huoltaja- tai lapsitili vaihtui rinnakkaisen varauksen aikana.")
        self.identity = identity

    def _shared(self):
        reader = self._reader()
        self._bind(reader._user())
        cart = reader._read("/webshop/reservations2/apicart")
        reservations = reader._read("/webshop/reservations2/apiusereventgroups")
        receipt = reader._read("cart/getreceiptinfo", rpc=True)
        locked = reader._read("cart/islocked", rpc=True)
        if (not isinstance(cart, list) or any(not isinstance(item, dict) for item in cart)
                or not isinstance(reservations, list) or any(not isinstance(item, dict) for item in reservations)
                or not isinstance(locked, dict) or _flag(locked.get("isLocked")) is None):
            raise BookingError("Yhteisen ostoskorin tai omien varausten tietomuoto muuttui.")
        for item in reservations:
            if any(_integer(item.get(key)) is None for key in (
                    "reservation_id", "reservation_account_id", "reservation_event_group_id", "reservation_status_id")):
                raise BookingError("Omien kurssivarausten tunnistetiedot puuttuvat.")
        if receipt is not None and receipt is not False:
            if not isinstance(receipt, dict):
                raise BookingError("Yhteisen kuitin tietomuoto muuttui.")
            if receipt.get("receipt_id") is not None:
                if not _integer(receipt["receipt_id"]):
                    raise BookingError("Yhteisen kuitin tunnus puuttuu.")
                _money(receipt.get("total_amount"))
        active = any(_integer(item["reservation_event_group_id"]) in self.backends
                     and _integer(item["reservation_account_id"]) == reader.child_id
                     and _integer(item["reservation_status_id"]) not in {1, 3, 6} for item in reservations)
        self._shared_cart, self._candidate_activity = cart, active
        self.selection_clear = not cart and _flag(locked["isLocked"]) is False and not active
        return cart, reservations, receipt, locked

    def _courses(self) -> dict[int, dict | TransientBookingError]:
        page = next((backend.page for backend in self.backends.values()
                     if not backend.page.is_closed() and same_origin(backend.page.url, self.base_url)), None)
        if page is None:
            raise TransientBookingError("Yhtään kurssihakua varten avattua kaupan välilehteä ei ole käytettävissä.")
        jobs = [{"id": course_id, "url": self.base_url + f"/webshop/reservations2/apieventgroups/{course_id}"}
                for course_id in self.backends]
        try:
            values = page.evaluate(_COURSE_READS, jobs)
        except Exception as exc:
            raise TransientBookingError("Rinnakkaiset kurssien lukupyynnöt keskeytyivät.") from exc
        return _candidate_rows(values, set(self.backends))

    def observations(self) -> dict[int, Observation | TransientBookingError]:
        self.selection_clear = False
        try:
            cart, reservations, receipt, locked = self._shared()
            rows = self._courses()
        except TransientBookingError as exc:
            self.selection_clear = False
            return {course_id: exc for course_id in self.backends}
        result = {}
        for course_id, row in rows.items():
            if isinstance(row, TransientBookingError):
                result[course_id] = row
                continue
            backend = self.backends[course_id]
            observation = _observation(backend.spec, backend.child_id, row, cart, reservations, receipt, locked,
                                       base_url=self.base_url)
            backend._course = _fresh_course(backend.spec, row, base_url=self.base_url)
            backend._cart, backend._last_observation = cart, observation
            backend._observed_at = time.monotonic()
            result[course_id] = observation
        return result

    def activate(self, course_id: int) -> BookingBrowser:
        return self.routing.activate(course_id)

    def activate_queue(self, course_id: int) -> BookingBrowser:
        """Use the same sole foreground tab; only its queue payload is armed."""
        return self.routing.activate_queue(course_id)

    def wait(self, seconds: float) -> None:
        self._reader().wait(seconds)

    def initialize(self) -> None:
        self._bind(self._reader()._user())
        initial = self.observations()
        successes = [value for value in initial.values() if isinstance(value, Observation)]
        if not successes:
            raise next(value for value in initial.values() if isinstance(value, TransientBookingError))
        candidate_cart = (len(self._shared_cart) == 1
                          and any(_cart_match(self._shared_cart[0], course_id, self._reader().child_id)
                                  for course_id in self.backends))
        if self._shared_cart and not candidate_cart:
            raise BookingError("Yhteisessä ostoskorissa on muu tuote. Vaihtoehtoja ei osteta automaattisesti.")
        existing = (candidate_cart or self._candidate_activity
                    or any(value.held or value.reservation_id or value.reservation_confirmed or value.payment_locked
                           for value in successes))
        selected_methods = {}
        for backend in self.backends.values():
            if existing:
                continue
            key = (backend.spec.payment_mode, backend.spec.payment_type_id)
            if key not in selected_methods:
                price = max(spec.course.price for spec in self.specs
                            if (spec.payment_mode, spec.payment_type_id) == key and spec.course.price is not None)
                backend._select_payment(price)
                selected_methods[key] = (backend.payment_type_id, backend.payment_type_name)
            backend.payment_type_id, backend.payment_type_name = selected_methods[key]
        # Start every navigation before waiting for any course UI to finish.
        # The browser can load the independent documents concurrently.
        for backend in self.backends.values():
            url = _validate_course_url(backend.spec.course, base_url=self.base_url)
            backend.page.evaluate("url => { setTimeout(() => { location.href = url; }, 0); }", url)
        for course_id, backend in self.backends.items():
            try:
                def ready(backend=backend):
                    backend.page.wait_for_url(backend.spec.course.url, wait_until="domcontentloaded", timeout=15_000)
                    backend.page.wait_for_function("() => !!(window.R2 && R2.session && R2.eventgroups)", timeout=15_000)
                _preflight_page(ready)
            except TransientBookingError as exc:
                self.preparation_errors[course_id] = exc


@contextmanager
def open_booking_pool(specs: Sequence[BookingSpec], profile_dir: Path,
                      executable_path: str | None = None, *, base_url: str = BASE_URL,
                      headless: bool = False) -> Generator[BookingPool]:
    specs = tuple(specs)
    base_url = validate_base_url(base_url)
    if not specs or len({spec.course.id for spec in specs}) != len(specs):
        raise BookingError("Anna vähintään yksi yksilöllinen kurssivaihtoehto.")
    names = {" ".join(spec.child_name.casefold().split()) for spec in specs}
    if len(names) != 1:
        raise BookingError("Kaikkien kurssivaihtoehtojen on koskettava samaa lasta.")
    if any(spec.course.price is None for spec in specs):
        raise BookingError("Kaikkien kurssivaihtoehtojen hinnan pitää olla tiedossa ennen valmistelua.")
    for spec in specs:
        _validate_course_url(spec.course, base_url=base_url)
    deadline = max(spec.deadline for spec in specs)
    try:
        with _browser(profile_dir, executable_path, headless=headless) as context:
            routing = _PoolRouting(context, specs, base_url=base_url)
            context.route("**/*", routing.route)
            context.on("response", routing.response)
            first = context.pages[0] if context.pages else context.new_page()
            pages = [first] + [context.new_page() for _ in specs[1:]]
            pool = BookingPool(specs, context, pages, routing, base_url=base_url)
            def login():
                response = first.goto(base_url + "/webshop/reservations2/", wait_until="domcontentloaded")
                if response is not None and (response.status == 429 or response.status >= 500):
                    raise TransientBookingError("Kaupan kirjautumissivu on ruuhkautunut.", retry_after=_retry_after(response.headers.get("retry-after", "")))
                remaining = max(1, (deadline - datetime.now(UTC)).total_seconds() * 1000)
                _ensure_login(first, routing.read_guard, timeout_ms=min(2_000_000_000, remaining))
            _retry_preflight(lambda: _preflight_page(login), pool.wait, deadline)
            _retry_preflight(lambda: _preflight_page(pool.initialize), pool.wait, deadline)
            try:
                yield pool
            finally:
                for backend in pool.backends.values():
                    backend.guard.disarm()
    except BrowserError as exc:
        raise BookingError(str(exc)) from exc
