"""A browser booking backend with one-shot, course-and-child-scoped writes.

Reads use the same local browser cookie jar. UI actions are the shop's own
actions; a route guard validates their outgoing payload before allowing a write.
This module never clears a cart or creates/updates a child account.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from html import unescape
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from .booking import (ActionNotSent, ActionUncertain, BookingError, BookingSpec,
                      Observation, TransientBookingError)
from .browser import _browser, _ensure_login, _prepare_page, _request_allowed, _validate_course_url, BrowserError
from .client import BASE_URL, ClientError, parse_course
from .models import normalize_location, normalize_text
from .origin import is_local_test, same_origin, validate_base_url


_READ_RPC = frozenset({
    "cart/getreceiptinfo", "cart/islocked", "cart/getonlinepaymenttypes",
    "cart/checkcurrentuserbalance",
})


def _payment_label(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())


def _eligible_payment(row: dict, mode: str) -> bool:
    if _flag(row.get("is_disabled")) is not False or _flag(row.get("is_pay_later")) is not False:
        return False
    if _flag(row.get("is_balance")) is not (mode == "saldo"):
        return False
    if mode == "fastpay":
        # Match the branded merchant option, including Fast Pay and trailing
        # card/label details. Do not infer FastPay from a generic card button.
        name = normalize_text(_payment_label(row.get("name")))
        return re.match(r"^(?:enkora )?fast ?pay(?: |$)", name) is not None
    return mode in {"saldo", "verkkomaksu"}


def _integer(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def _flag(value: Any) -> bool | None:
    if value is True or value == "1" or type(value) is int and value == 1:
        return True
    if value is False or value == "0" or type(value) is int and value == 0:
        return False
    return None


def _money(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise BookingError("Kaupan hintatiedon muoto muuttui.")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BookingError("Kaupan hintatiedon muoto muuttui.") from exc
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        raise BookingError("Kauppa ei palauttanut hintaa kokonaisina sentteinä.")
    return amount / 100


def _payload(body: str | None) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        try:
            fields = parse_qs(body, strict_parsing=True)
            if set(fields) - {"content", "input_format", "output_format"} or len(fields.get("content", [])) != 1:
                return None
            return json.loads(fields["content"][0])
        except (ValueError, TypeError):
            return None


def _retry_preflight(action, wait, deadline: datetime, *, now=None):
    """Retry only positively read-only, transient initialization failures."""
    clock = now or (lambda: datetime.now(timezone.utc))
    delay = 1.0
    while True:
        if clock() >= deadline:
            raise BookingError("Varaamisen määräaika päättyi ennen selaimen valmistumista.")
        try:
            return action()
        except TransientBookingError as exc:
            remaining = (deadline - clock()).total_seconds()
            if remaining <= 0:
                raise BookingError("Varaamisen määräaika päättyi kaupan ruuhkan aikana.") from exc
            wait(min(remaining, max(delay, exc.retry_after)))
            delay = min(15.0, delay * 2)


def _preflight_page(action):
    """Map only browser transport/load failures to read-only retries."""
    from playwright.sync_api import Error, TimeoutError
    try:
        return action()
    except TimeoutError as exc:
        raise TransientBookingError("Kaupan sivu ei valmistunut ruuhkan aikana.") from exc
    except Error as exc:
        transient = ("ERR_TIMED_OUT", "ERR_CONNECTION_", "ERR_NETWORK_CHANGED", "ERR_EMPTY_RESPONSE",
                     "ERR_HTTP_RESPONSE_CODE_FAILURE", "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED")
        if any(marker in str(exc) for marker in transient):
            raise TransientBookingError("Yhteys kaupan sivuun katkesi valmistelun aikana.") from exc
        raise BookingError("Kaupan selainta ei voitu valmistella. Tarkista selainikkuna ja verkkoyhteys.") from exc


def _fresh_course(spec: BookingSpec, row: Any, *, base_url: str = BASE_URL):
    try:
        course = parse_course(row, base_url=base_url)
    except ClientError as exc:
        raise BookingError("Kurssin tietomuoto muuttui.") from exc
    expected = spec.course
    if (course.id != expected.id or normalize_text(course.name) != normalize_text(expected.name)
            or normalize_location(course.location) != normalize_location(expected.location)
            or course.start != expected.start or course.end != expected.end):
        raise BookingError("Valitun kurssin nimi, halli tai ajankohta muuttui. Varausta ei jatketa automaattisesti.")
    if course.price is None:
        raise BookingError("Valitun kurssin hintaa ei voitu vahvistaa.")
    if course.price > spec.max_price:
        raise BookingError("Kurssin hinta ylittää annetun enimmäishinnan.")
    return course


def _cart_match(row: dict, course_id: int, child_id: int) -> bool:
    params = row.get("reservation_params")
    return (isinstance(params, dict)
            and _integer(params.get("reservation_event_group_id")) == course_id
            and _integer(params.get("reservation_account_id")) == child_id
            and _integer(row.get("quantity")) == 1)


def _observation(spec: BookingSpec, child_id: int, course_row: Any,
                 cart: Any, reservations: Any, receipt: Any, locked: Any,
                 *, base_url: str = BASE_URL) -> Observation:
    """Parse independent server evidence; UI headings never establish success."""
    course = _fresh_course(spec, course_row, base_url=base_url)
    if (not isinstance(cart, list) or any(not isinstance(row, dict) for row in cart)
            or not isinstance(reservations, list) or any(not isinstance(row, dict) for row in reservations)):
        raise BookingError("Ostoskorin tai omien varausten tietomuoto muuttui.")
    matching_cart = [row for row in cart if _cart_match(row, course.id, child_id)]
    held = len(cart) == 1 and len(matching_cart) == 1
    others = bool(cart) and not held
    price = course.price
    if held:
        item = matching_cart[0]
        price = _money(item.get("bundle_price") if item.get("bundle_price") is not None else item.get("price"))
        if price > spec.max_price:
            raise BookingError("Ostoskorin hinta ylittää annetun enimmäishinnan.")
    matching = [row for row in reservations
                if _integer(row.get("reservation_event_group_id")) == course.id
                and _integer(row.get("reservation_account_id")) == child_id
                and _integer(row.get("reservation_status_id")) in {2, 4, 5, 7}]
    if len(matching) > 1:
        raise BookingError("Samalle lapselle löytyi useita tämän kurssin varauksia. Automaattinen ostaminen pysähtyi.")
    reservation_id = None
    confirmed = False
    if matching:
        reservation = matching[0]
        rid = _integer(reservation.get("reservation_id"))
        if rid is None or rid <= 0:
            raise BookingError("Varaustunnuksen tietomuoto muuttui.")
        reservation_id = str(rid)
        confirmed = (_integer(reservation.get("reservation_status_id")) in {2, 4, 5}
                     and ("is_paid" not in reservation or _flag(reservation.get("is_paid")) is True))
    queue_entries = [row for row in reservations
                     if _integer(row.get("reservation_event_group_id")) == course.id
                     and _integer(row.get("reservation_account_id")) == child_id
                     and _integer(row.get("reservation_status_id")) in {1, 3}]
    if len(queue_entries) > 1:
        raise BookingError("Samalle lapselle löytyi useita tämän kurssin jonotietoja.")
    queue_reservation_id, queue_position = None, None
    if queue_entries:
        entry = queue_entries[0]
        queue_id = _integer(entry.get("reservation_id"))
        if queue_id is None or queue_id <= 0:
            raise BookingError("Jonotiedon tunnus puuttuu tai sen tietomuoto muuttui.")
        queue_reservation_id = str(queue_id)
        position = _integer(entry.get("queue_number", entry.get("queue_position")))
        queue_position = position if position is not None and position > 0 else None
    receipt_id, receipt_total, receipt_closed = None, None, False
    if receipt is not None and receipt is not False:
        if not isinstance(receipt, dict):
            raise BookingError("Kuitin tietomuoto muuttui.")
        if receipt.get("receipt_id") is not None:
            rid = _integer(receipt["receipt_id"])
            if rid is None or rid <= 0:
                raise BookingError("Kuitin tunnuksen tietomuoto muuttui.")
            receipt_id = str(rid)
            receipt_total = _money(receipt.get("total_amount"))
            receipt_closed = (_flag(receipt.get("is_closed")) is True
                              and _flag(receipt.get("is_pay_later")) is False)
    if not isinstance(locked, dict) or _flag(locked.get("isLocked")) is None:
        raise BookingError("Maksulukituksen tietomuoto muuttui.")
    return Observation(
        available=course.available, current_price=price, held=held,
        reservation_id=reservation_id, reservation_confirmed=confirmed,
        receipt_id=receipt_id, receipt_closed=receipt_closed, receipt_total=receipt_total,
        cart_has_others=others, cart_empty=not cart, payment_locked=_flag(locked["isLocked"]) is True,
        queue_available=course.queue_available, queued=queue_reservation_id is not None,
        queue_reservation_id=queue_reservation_id, queue_position=queue_position,
    )


class _FormAction(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action: str | None = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form" and values.get("id") == "payment_form":
            self.action = values.get("action")


class _BookingGuard:
    """Only one verified mutation can pass during an explicitly armed action."""
    def __init__(self, context, spec: BookingSpec, *, base_url: str = BASE_URL):
        self.context, self.spec = context, spec
        self.base_url = validate_base_url(base_url)
        self.allow_login = True
        self.child_id: int | None = None
        self.payer_id: int | None = None
        self.product_id: int | None = None
        self.price: Decimal | None = None
        self.payment_type_id: int | None = None
        self.balance_cents: int | None = None
        self.service_group_id: int | None = None
        self.armed: str | None = None
        self.sent = False
        self.actual_sent = False
        self.blocked_write = False
        self.provider_origins: set[str] = set()
        self.external_action: str | None = None
        self.checkout_sent = False
        self.queue_sent = False
        self.return_paths: set[tuple[str, str]] = set()

    def arm(self, action: str) -> None:
        if action not in {"add", "checkout", "queue"} or self.armed is not None:
            raise BookingError("Varaustoiminnon tila ei ole yksiselitteinen.")
        self.armed, self.sent, self.blocked_write = action, False, False

    def disarm(self) -> None:
        self.armed = None

    def _add_matches(self, data: Any) -> bool:
        if not isinstance(data, dict) or self.child_id is None or self.product_id is None or self.price is None:
            return False
        # Unknown extra products, agreements or subscription parameters cannot
        # sneak through the course-cart operation.
        if set(data) - {"fare_product_id", "reservation_event_group_id", "quantity", "price", "title", "account", "tags", "campaign_code"}:
            return False
        account = data.get("account")
        if not isinstance(account, dict) or set(account) - {"account_id", "name"}:
            return False
        try:
            price = _money(data.get("price"))
        except BookingError:
            return False
        return (_integer(data.get("reservation_event_group_id")) == self.spec.course.id
                and _integer(account.get("account_id")) == self.child_id
                and (not account.get("name") or " ".join(str(account["name"]).casefold().split())
                     == " ".join(self.spec.child_name.casefold().split()))
                and _integer(data.get("fare_product_id")) == self.product_id
                and _integer(data.get("quantity")) == 1
                and price == self.price and price <= self.spec.max_price
                and not data.get("campaign_code"))

    def _checkout_matches(self, data: Any) -> bool:
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            return False
        row = data[0]
        if set(row) - {"return_url", "cancel_url", "balance_amount", "payment_type_id", "service_group_id"}:
            return False
        if (_integer(row.get("payment_type_id")) != self.payment_type_id
                or self.payment_type_id is None or self.price is None
                or self.price > self.spec.max_price):
            return False
        balance = row.get("balance_amount")
        if balance is None:
            balance = 0  # JSON.stringify(NaN) in the shop's ordinary no-balance UI.
        if _integer(balance) != self.balance_cents:
            return False
        if self.service_group_id is not None and _integer(row.get("service_group_id")) != self.service_group_id:
            return False
        for key, state in (("return_url", "success"), ("cancel_url", "cancel")):
            parsed = urlsplit(str(row.get(key, "")))
            if (not same_origin(str(row.get(key, "")), self.base_url)
                    or not parsed.path.startswith("/webshop/reservations2")
                    or parse_qs(parsed.query).get("doPayment") != [state]):
                return False
        return True

    def _queue_matches(self, data: Any) -> bool:
        if (not isinstance(data, dict) or self.child_id is None
                or set(data) - {"reservation_event_group_id", "reservation_status_id", "account", "tags"}):
            return False
        account = data.get("account")
        if not isinstance(account, dict) or set(account) - {"account_id", "name"}:
            return False
        return (_integer(data.get("reservation_event_group_id")) == self.spec.course.id
                and _integer(data.get("reservation_status_id")) == 1
                and _integer(account.get("account_id")) == self.child_id
                and (not account.get("name") or " ".join(str(account["name"]).casefold().split())
                     == " ".join(self.spec.child_name.casefold().split())))

    def permits_write(self, url: str, method: str, body: str | None) -> bool:
        if self.sent or method.upper() != "POST":
            return False
        if not same_origin(url, self.base_url):
            return False
        parsed = urlsplit(url)
        if parsed.query or parsed.fragment:
            return False
        data = _payload(body)
        return ((self.armed == "add" and parsed.path == "/webshop/reservations2/apicart" and self._add_matches(data))
                or (self.armed == "queue" and not self.queue_sent and not self.actual_sent
                    and parsed.path == "/webshop/reservations2/apiusereventgroups" and self._queue_matches(data))
                or (self.armed == "checkout" and not self.checkout_sent
                    and parsed.path == "/webshop/call/cart/externalpaymentstart" and self._checkout_matches(data)))

    def _provider_request(self, request) -> bool:
        if is_local_test(self.base_url) or not self.provider_origins:
            return False
        parsed = urlsplit(request.url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return False
        origin = f"https://{parsed.netloc}"
        # Payment services redirect to the bank chosen by the user. Follow only
        # documents initiated by an already trusted payment page/redirect.
        previous = request.redirected_from
        previous_origin = None
        if previous:
            prior = urlsplit(previous.url)
            previous_origin = f"{prior.scheme}://{prior.netloc}"
        try:
            source = urlsplit(request.frame.url)
            source_origin = f"{source.scheme}://{source.netloc}"
        except Exception:
            source_origin = None
        try:
            top = urlsplit(request.frame.page.main_frame.url)
            top_origin = f"{top.scheme}://{top.netloc}"
        except Exception:
            top_origin = None
        trusted_source = (source_origin in self.provider_origins
                          or top_origin in self.provider_origins
                          or previous_origin in self.provider_origins)
        if same_origin(request.url, self.base_url):
            states = parse_qs(parsed.query).get("doPayment", [])
            return (request.resource_type == "document"
                    and request.method.upper() in {"GET", "HEAD", "POST"}
                    and trusted_source and len(states) == 1
                    and (parsed.path, states[0]) in self.return_paths)
        if origin in self.provider_origins:
            return request.method.upper() in {"GET", "HEAD", "POST"}
        if trusted_source and request.method.upper() in {"GET", "HEAD", "POST"}:
            # 3-D Secure frequently posts from an about:blank iframe. The
            # trusted payment page enclosing that iframe establishes its origin.
            if request.resource_type == "document":
                self.provider_origins.add(origin)
            return True
        return False

    def route(self, route) -> None:
        request = route.request
        parsed = urlsplit(request.url)
        path = unquote(parsed.path)
        read_rpc = (same_origin(request.url, self.base_url)
                    and path.startswith("/webshop/call/")
                    and path[len("/webshop/call/"):].rstrip("/") in _READ_RPC
                    and request.method.upper() in {"GET", "POST"})
        if _request_allowed(request.url, request.method, allow_login=self.allow_login, base_url=self.base_url) or read_rpc:
            route.continue_()
        elif self.permits_write(request.url, request.method, request.post_data):
            self.sent = True  # Conservatively mark before attempting transmission.
            if self.armed in {"add", "checkout"}:
                self.actual_sent = True
            if self.armed == "queue":
                self.queue_sent = True
            if self.armed == "checkout":
                self.checkout_sent = True
                options = _payload(request.post_data)[0]
                for key in ("return_url", "cancel_url"):
                    return_url = urlsplit(options[key])
                    state = parse_qs(return_url.query)["doPayment"][0]
                    self.return_paths.add((return_url.path, state))
            route.continue_()
        elif self._provider_request(request):
            route.continue_()
        else:
            if same_origin(request.url, self.base_url) and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                self.blocked_write = True
            route.abort("blockedbyclient")

    def response(self, response) -> None:
        if (is_local_test(self.base_url) or not self.checkout_sent or self.spec.payment_mode not in {"verkkomaksu", "fastpay"}
                or response.url != self.base_url + "/webshop/call/cart/externalpaymentstart"):
            return
        try:
            data = response.json()
            form = data.get("result", {}).get("form")
            if not isinstance(form, str):
                return
            parser = _FormAction()
            parser.feed(form)
            action = urljoin(self.base_url, parser.action or "")
            target = urlsplit(action)
            if (target.scheme == "https" and not same_origin(action, self.base_url)
                    and not target.username and not target.password):
                self.external_action = action
                self.provider_origins.add(f"https://{target.netloc}")
        except Exception:
            # A malformed payment response never grants provider write access.
            return


class BookingBrowser:
    def __init__(self, spec: BookingSpec, context, page, guard: _BookingGuard, *, base_url: str = BASE_URL):
        self.spec, self.context, self.page, self.guard = spec, context, page, guard
        self.base_url = validate_base_url(base_url)
        if guard.base_url != self.base_url:
            raise BookingError("Selaimen ja varaussuojan kauppaosoitteet eivät vastaa toisiaan.")
        self.child_id = 0
        self.payer_id = 0
        self.identity = ""
        self.payment_type_id: int | None = spec.payment_type_id
        self.payment_type_name: str | None = None
        self._course = spec.course
        self._cart: list[dict] = []
        self._last_observation: Observation | None = None
        self._observed_at = 0.0

    def _read(self, path: str, *, rpc: bool = False, content: Any = None) -> Any:
        # Hardcoded read endpoints only: APIRequestContext is not covered by
        # page routing and must never be used to send a booking/payment write.
        if rpc:
            if path not in _READ_RPC and path != "user/getcurrentuser":
                raise BookingError("Tuntematon lukurajapinta.")
            url = self.base_url + "/webshop/call/" + path
        else:
            allowed = {"/webshop/reservations2/apicart", "/webshop/reservations2/apiusereventgroups",
                       f"/webshop/reservations2/apieventgroups/{self.spec.course.id}"}
            if path not in allowed:
                raise BookingError("Tuntematon lukurajapinta.")
            url = self.base_url + path
        try:
            options = {"timeout": 15_000, "max_redirects": 0}
            if rpc:
                response = self.context.request.post(url, form={
                    "input_format": "json", "output_format": "json",
                    "content": json.dumps(content, ensure_ascii=False),
                }, **options)
            else:
                response = self.context.request.get(url, **options)
        except Exception as exc:
            raise TransientBookingError("Kaupan lukupyyntö katkesi; tilanne tarkistetaan uudelleen.") from exc
        try:
            status = response.status
            if status == 429 or status >= 500:
                retry = 0.0
                value = response.headers.get("retry-after", "")
                try:
                    retry = float(value)
                except ValueError:
                    try:
                        retry = max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        pass
                raise TransientBookingError(f"Kauppa on ruuhkautunut (HTTP {status}).", retry_after=retry)
            if status in {401, 403} or 300 <= status < 400:
                raise BookingError("Kirjautuminen ei ole enää voimassa. Varausta ei jatketa automaattisesti.")
            if status < 200 or status >= 300:
                raise BookingError(f"Kaupan lukurajapinta vastasi HTTP {status}.")
            try:
                data = response.json()
            except Exception as exc:
                raise BookingError("Kauppa ei palauttanut odotettua JSON-tietoa.") from exc
        finally:
            response.dispose()
        if rpc:
            if not isinstance(data, dict) or "result" not in data:
                raise BookingError("Kaupan lukurajapinnan tietomuoto muuttui.")
            if data.get("errors") or data.get("user_errors"):
                raise BookingError("Kaupan lukurajapinta palautti virheen.")
            return data["result"]
        return data

    def _user(self) -> dict:
        user = self._read("user/getcurrentuser", rpc=True, content=[True, True])
        if not isinstance(user, dict) or _integer(user.get("account_id")) is None:
            raise BookingError("Huoltajan kirjautumista ei voitu vahvistaa.")
        return user

    def _bind_user(self, user: dict) -> None:
        """Bind one existing child without inspecting a course-specific cart."""
        self.payer_id = _integer(user["account_id"])
        children = user.get("linked_accounts")
        if not isinstance(children, (list, dict)):
            raise BookingError("Tililtä ei löytynyt olemassa olevia lapsitilejä.")
        candidates = list(children.values()) if isinstance(children, dict) else children
        wanted = " ".join(self.spec.child_name.casefold().split())
        matches = [child for child in candidates if isinstance(child, dict)
                   and " ".join(str(child.get("name", "")).casefold().split()) == wanted]
        if len(matches) != 1 or _integer(matches[0].get("account_id")) in {None, self.payer_id}:
            raise BookingError("Lapsen nimeä ei löytynyt yksiselitteisesti olemassa olevista lapsitileistä.")
        self.child_id = _integer(matches[0]["account_id"])
        self.identity = f"{self.payer_id}:{self.child_id}:{self.spec.course.id}"
        self.guard.child_id, self.guard.payer_id = self.child_id, self.payer_id

    def initialize(self) -> None:
        self._bind_user(self._user())
        initial = self.snapshot()
        if initial.cart_has_others or (not initial.cart_empty and not initial.held):
            raise BookingError("Ostoskorissa on muita tuotteita. Työkalu ei poista eikä osta niitä.")
        if initial.payment_locked:
            print("Ostoskorissa on keskeneräinen maksu. Työkalu tarkistaa sen tilaa lähettämättä uutta maksua.")
        if not (initial.payment_locked or initial.held or initial.reservation_id is not None or initial.reservation_confirmed):
            self._select_payment(initial.current_price)
        # Resuming a previous purchase must not require the balance that was
        # already spent, or the availability of a payment method already used.
        self.page.goto(_validate_course_url(self.spec.course, base_url=self.base_url), wait_until="domcontentloaded")

    def _select_payment(self, price: Decimal) -> None:
        types = self._payment_types()
        eligible = [row for row in types if _eligible_payment(row, self.spec.payment_mode)]
        if self.payment_type_id is not None:
            eligible = [row for row in eligible if _integer(row.get("payment_type_id")) == self.payment_type_id]
        if not eligible:
            if self.spec.payment_mode == "fastpay":
                raise BookingError("Kaupan käytettävissä olevista maksutavoista ei löytynyt FastPay-vaihtoehtoa annetuilla ehdoilla. Tarkista FastPay ja maksutapatunnus ostoskorin maksuosiosta.")
            raise BookingError("Valittu maksutapa ei ole käytettävissä. Tarkista maksutapatunnus ennen ilmoittautumisen alkua.")
        if len(eligible) > 1:
            if not sys.stdin.isatty():
                raise BookingError("Verkkomaksutapoja on useita. Anna --maksutunnus ennen ajastettua varausta.")
            print("Valitse verkkomaksutapa ennen ilmoittautumisen alkua:")
            for row in eligible:
                print(f"  {_integer(row['payment_type_id'])}: {row.get('name', 'Maksutapa')}")
            try:
                chosen = int(input("Maksutavan tunnus: "))
            except (EOFError, ValueError) as exc:
                raise BookingError("Maksutavan valinta puuttuu.") from exc
            eligible = [row for row in eligible if _integer(row.get("payment_type_id")) == chosen]
            if len(eligible) != 1:
                raise BookingError("Annettu maksutapa ei kuulu käytettävissä oleviin vaihtoehtoihin.")
        self.payment_type_id = _integer(eligible[0]["payment_type_id"])
        self.payment_type_name = _payment_label(eligible[0].get("name")) or "Maksutapa"
        if self.spec.payment_mode == "saldo":
            balance = self._read("cart/checkcurrentuserbalance", rpc=True, content=[])
            if not isinstance(balance, dict) or _money(balance.get("balance")) < price:
                raise BookingError("Asiakastilin saldo ei riitä valitun kurssin maksamiseen.")
        print(f"Valittu maksutapa: {self.payment_type_name} (tunnus {self.payment_type_id}).")

    def _payment_types(self) -> list[dict]:
        rows = self._read("cart/getonlinepaymenttypes", rpc=True, content=[])
        if (not isinstance(rows, list) or any(not isinstance(row, dict)
                or _integer(row.get("payment_type_id")) is None
                or _flag(row.get("is_disabled")) is None
                or _flag(row.get("is_balance")) is None
                or _flag(row.get("is_pay_later")) is None for row in rows)):
            raise BookingError("Maksutapaluettelon tietomuoto muuttui.")
        return rows

    def snapshot(self) -> Observation:
        if self.payer_id and _integer(self._user().get("account_id")) != self.payer_id:
            raise BookingError("Kirjautunut huoltajatili vaihtui. Varaus keskeytettiin.")
        row = self._read(f"/webshop/reservations2/apieventgroups/{self.spec.course.id}")
        cart = self._read("/webshop/reservations2/apicart")
        reservations = self._read("/webshop/reservations2/apiusereventgroups")
        receipt = self._read("cart/getreceiptinfo", rpc=True)
        locked = self._read("cart/islocked", rpc=True)
        observation = _observation(self.spec, self.child_id, row, cart, reservations, receipt, locked, base_url=self.base_url)
        self._course, self._cart = _fresh_course(self.spec, row, base_url=self.base_url), cart
        self._last_observation, self._observed_at = observation, time.monotonic()
        return observation

    def _recent_observation(self) -> Observation:
        # The controller has normally just reconciled server state. Reuse only
        # that immediately preceding observation, never a preopening snapshot.
        if self._last_observation is not None and time.monotonic() - self._observed_at <= 2.0:
            return self._last_observation
        return self.snapshot()

    def _finish_action(self, failure: Exception | None) -> None:
        sent = self.guard.sent
        blocked = self.guard.blocked_write
        self.guard.disarm()
        if failure is not None or blocked or not sent:
            if sent:
                raise ActionUncertain("Kauppa on voinut vastaanottaa toiminnon. Tila tarkistetaan ennen jatkamista.") from failure
            raise ActionNotSent("Varaus- tai maksupyyntöä ei lähetetty. Kaupan näkymä tarkistetaan uudelleen.") from failure

    def _participant_action(self, *, queue: bool = False):
        same_page = (same_origin(self.page.url, self.base_url)
                     and urlsplit(self.page.url).path.rstrip("/") == urlsplit(self._course.url).path.rstrip("/"))
        if same_page:
            # Only an authoritative server snapshot updates the loaded UI.
            same_page = self.page.evaluate("""row => {
                const model = window.R2 && R2.eventgroups && R2.eventgroups.get(row.reservation_event_group_id);
                if (!model) return false;
                model.set(row);
                return true;
            }""", self._course.raw)
        _prepare_page(self.page, self._course, self.spec.child_name, navigate=not same_page,
                      base_url=self.base_url, queue=queue)
        selected = self.page.locator('#modal #linkedAccount input[name="participant"]:checked')
        if selected.count() != 1 or _integer(selected.get_attribute("value")) != self.child_id:
            raise BookingError("Valittu osallistuja ei ole ennalta sovittu lapsitili.")
        if self.page.evaluate("() => Number(R2.user.get('account_id'))") != self.payer_id:
            raise BookingError("Huoltajatili vaihtui ennen varausta.")
        button = self.page.locator("#modal .modal-footer .ok")
        if (button.count() != 1 or not button.is_visible()
                or button.evaluate("e => e.hasAttribute('disabled') || e.classList.contains('disabled')")
                or self.page.locator("#selected-child-label, #child-account-dob").count()):
            raise BookingError("Lapsen ikä- tai henkilötiedot vaativat tarkistusta ennen varaamista.")
        return button

    def add_selected_course(self) -> None:
        self.guard.sent, self.guard.blocked_write = False, False
        state = self._recent_observation()
        if state.held or state.reservation_confirmed:
            return
        if state.cart_has_others or not state.cart_empty or state.payment_locked:
            raise ActionNotSent("Ostoskorin tila ei salli uuden varauksen lähettämistä.")
        if not state.available:
            raise ActionNotSent("Kurssin ilmoittautuminen ei ole vielä auki tai paikkoja ei ole.")
        self.guard.price = state.current_price
        products = self._course.raw.get("fare_products")
        if not isinstance(products, list) or not products or not isinstance(products[0], dict):
            raise BookingError("Kurssin myyntituotetta ei voitu vahvistaa.")
        self.guard.product_id = _integer(products[0].get("fare_product_id"))
        product_price = products[0].get("bundle_price") if products[0].get("bundle_price") is not None else products[0].get("price")
        if self.guard.product_id is None or _money(product_price) != state.current_price:
            raise BookingError("Kurssin myyntituotteen hinta ei vastaa vahvistettua hintaa.")
        failure = None
        try:
            button = self._participant_action()
            self.guard.arm("add")
            self._last_observation = None
            with self.page.expect_response(lambda response: response.url == self.base_url + "/webshop/reservations2/apicart"
                                           and response.request.method == "POST", timeout=20_000):
                button.click()
        except Exception as exc:
            failure = exc
        self._finish_action(failure)

    def join_queue(self) -> None:
        """Join one identified child's waitlist without cart or payment writes."""
        if self.guard.actual_sent:
            raise ActionNotSent("Varsinainen varaus tai maksu on jo voitu lähettää. Uutta jonopyyntöä ei lähetetä.")
        if self.guard.queue_sent:
            raise ActionUncertain("Jonopyyntö on jo voitu vastaanottaa. Sen tilaa tarkistetaan lähettämättä uutta pyyntöä.")
        self.guard.sent, self.guard.blocked_write = False, False
        try:
            state = self.snapshot()
        except (TransientBookingError, TimeoutError, OSError) as exc:
            raise ActionNotSent("Jonopyyntöä ei lähetetty: alustava tilan lukeminen epäonnistui.") from exc
        if state.queued:
            return
        if (state.held or state.reservation_id is not None or state.reservation_confirmed
                or state.cart_has_others or not state.cart_empty or state.payment_locked
                or self.guard.checkout_sent):
            raise ActionNotSent("Kurssivaraus tai maksu on jo olemassa tai sen tila on kesken. Jonopyyntöä ei lähetetty.")
        if state.available or not state.queue_available:
            raise ActionNotSent("Kurssi ei ole nyt avoinna jonoon liittymiselle.")
        failure = None
        try:
            button = self._participant_action(queue=True)
            self.guard.arm("queue")
            self._last_observation = None
            with self.page.expect_response(lambda response: response.url == self.base_url + "/webshop/reservations2/apiusereventgroups"
                                           and response.request.method == "POST", timeout=20_000):
                button.click()
        except Exception as exc:
            failure = exc
        self._finish_action(failure)

    def checkout(self) -> None:
        self.guard.sent, self.guard.blocked_write = False, False
        state = self._recent_observation()
        if (not state.held or state.cart_has_others or state.cart_empty or state.payment_locked
                or self.guard.checkout_sent):
            raise ActionNotSent("Täsmällistä maksukelpoista kurssivarausta ei voitu vahvistaa tai maksu on jo aloitettu.")
        if self.payment_type_id is None:
            self._select_payment(state.current_price)
        types = [row for row in self._payment_types()
                 if _integer(row.get("payment_type_id")) == self.payment_type_id
                 and _eligible_payment(row, self.spec.payment_mode)]
        if len(types) != 1:
            raise BookingError("Ennen ilmoittautumista valittu maksutapa ei käy tämän kurssin maksamiseen.")
        balance_cents = 0
        if self.spec.payment_mode == "saldo":
            balance = self._read("cart/checkcurrentuserbalance", rpc=True, content=[])
            if not isinstance(balance, dict) or _money(balance.get("balance")) < state.current_price:
                raise BookingError("Saldo ei riitä kurssin maksamiseen. Varausta ei makseta toisella maksutavalla.")
            balance_cents = int(state.current_price * 100)
        failure = None
        try:
            if not same_origin(self.page.url, self.base_url):
                raise BookingError("Selain on jo ulkoisessa maksupalvelussa. Uutta maksua ei aloiteta.")
            self.page.evaluate("() => R2.cart.fetch({reset:true}).then(() => R2.vent.trigger('cart:open'))")
            self.page.locator("#modal #usageTermsCheckbox").wait_for(state="visible")
            # Re-rendered UI data must still represent the same exact one line.
            ui_cart = self.page.evaluate("() => R2.cart.toJSON()")
            if (not isinstance(ui_cart, list) or len(ui_cart) != 1
                    or not _cart_match(ui_cart[0], self.spec.course.id, self.child_id)
                    or _money(ui_cart[0].get("bundle_price") if ui_cart[0].get("bundle_price") is not None else ui_cart[0].get("price")) != state.current_price):
                raise BookingError("Selaimen ostoskori ei vastaa vahvistettua lapsen kurssivarausta.")
            self.page.locator("#modal #usageTermsCheckbox").check()
            if self.spec.payment_mode == "saldo":
                self.page.locator("#modal #useBalanceCheckbox").check()
                selector = f'#modal .buyWithBalance[payment-type-id="{self.payment_type_id}"]'
            else:
                balance_box = self.page.locator("#modal #useBalanceCheckbox")
                if balance_box.count() and balance_box.is_checked():
                    balance_box.uncheck()
                selector = f'#modal .buyCart[payment-type-id="{self.payment_type_id}"]'
            action = self.page.locator(selector)
            if action.count() != 1:
                raise BookingError("Valitun maksutavan yksiselitteistä maksupainiketta ei löytynyt.")
            if not action.is_visible():
                dropdown = self.page.locator("#modal .btn-group-dropdown-top-left .dropdown-toggle")
                if dropdown.count() == 1:
                    dropdown.click()
            if (not action.is_visible() or not action.is_enabled()
                    or action.evaluate("e => e.hasAttribute('disabled') || e.classList.contains('disabled')")):
                raise BookingError("Valitun maksutavan painike ei ole käytettävissä.")
            self.guard.price, self.guard.payment_type_id = state.current_price, self.payment_type_id
            self.guard.balance_cents = balance_cents
            self.guard.service_group_id = _integer(self.page.evaluate("() => R2.service_group_id"))
            self.guard.arm("checkout")
            self._last_observation = None
            with self.page.expect_response(lambda response: response.url == self.base_url + "/webshop/call/cart/externalpaymentstart"
                                           and response.request.method == "POST", timeout=20_000):
                action.click()
            if self.spec.payment_mode in {"verkkomaksu", "fastpay"}:
                print("Maksu aloitettu. Tee maksupalvelun mahdollinen lisätunnistautuminen avatussa selaimessa; työkalu tarkistaa vahvistuksen automaattisesti.")
        except Exception as exc:
            failure = exc
        self._finish_action(failure)

    def wait(self, seconds: float) -> None:
        remaining = max(0, seconds)
        while remaining > 0:
            active = next((page for page in self.context.pages if not page.is_closed()), None)
            if active is None:
                raise BookingError("Varausselain suljettiin. Tarkista varauksen tila samalla profiililla ennen uutta yritystä.")
            duration = min(remaining, 1.0)
            try:
                active.wait_for_timeout(duration * 1000)
            except Exception as exc:
                raise TransientBookingError("Selaimen tapahtumien odotus katkesi.") from exc
            remaining -= duration


@contextmanager
def open_booking_browser(spec: BookingSpec, profile_dir: Path,
                         executable_path: str | None = None, *, base_url: str = BASE_URL,
                         headless: bool = False) -> Iterator[BookingBrowser]:
    base_url = validate_base_url(base_url)
    _validate_course_url(spec.course, base_url=base_url)
    try:
        with _browser(profile_dir, executable_path, headless=headless) as context:
            guard = _BookingGuard(context, spec, base_url=base_url)
            context.route("**/*", guard.route)
            context.on("response", guard.response)
            page = context.pages[0] if context.pages else context.new_page()
            backend = BookingBrowser(spec, context, page, guard, base_url=base_url)
            def login_phase():
                response = page.goto(base_url + "/webshop/reservations2/", wait_until="domcontentloaded")
                if response is not None and (response.status == 429 or response.status >= 500):
                    value = response.headers.get("retry-after", "")
                    try:
                        retry = float(value)
                    except ValueError:
                        retry = 0
                    raise TransientBookingError(f"Kaupan kirjautumissivu on ruuhkautunut (HTTP {response.status}).", retry_after=retry)
                if response is not None and response.status in {401, 403}:
                    raise BookingError("Kaupan kirjautumissivu ei ole käytettävissä tällä yhteydellä.")
                remaining_ms = max(1, (spec.deadline - datetime.now(timezone.utc)).total_seconds() * 1000)
                _ensure_login(page, guard, timeout_ms=min(2_000_000_000, remaining_ms))
            _retry_preflight(lambda: _preflight_page(login_phase), backend.wait, spec.deadline)
            _retry_preflight(lambda: _preflight_page(backend.initialize), backend.wait, spec.deadline)
            try:
                yield backend
            finally:
                # Keep protections installed through browser shutdown. There is
                # no uncontrolled write handoff in autonomous booking mode.
                guard.disarm()
    except BrowserError as exc:
        raise BookingError(str(exc)) from exc
