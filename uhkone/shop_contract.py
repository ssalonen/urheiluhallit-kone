"""Read-only checks of the public Urheiluhallit shop's published contract."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .browser import BrowserError, _validate_course_url
from .client import BASE_URL, ClientError, _integer, parse_course
from .models import _meeting_start_times
from .seed import SOURCE_URL

SHOP_PAGE = BASE_URL + "/webshop/reservations2/"
SCRIPT_DIRECTORY = "/webshop/javascript/jsmodule/reservations2/"
_MAX_BYTES = 8 * 1024 * 1024
_COURSE_KEYS = frozenset({
    "reservation_event_group_id", "name", "location", "service_id", "location_id", "reservation_group_id",
    "start", "end", "public_reservation_start", "is_reservable", "free", "free_queue", "weekdays", "fare_products",
})
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                        "source", "track", "wbr"})


class ContractError(Exception):
    """A public source is unavailable or differs from the expected contract."""


def check_catalogue(data: object) -> int:
    if not isinstance(data, list) or not data:
        raise ContractError("Kurssirajapinnan vastaus on tyhjä tai ei ole kurssiluettelo.")
    ids: set[int] = set()
    supported = 0
    for row in data:
        if not isinstance(row, dict):
            raise ContractError("Kurssirajapinnan kurssi ei ole tietue.")
        missing = _COURSE_KEYS - row.keys()
        if missing:
            raise ContractError("Kurssirajapinnasta puuttuu kenttä: " + ", ".join(sorted(missing)))
        try:
            course = parse_course(row)
        except ClientError as exc:
            raise ContractError(str(exc)) from None
        if any((_integer(row[key]) or 0) <= 0 for key in ("service_id", "location_id", "reservation_group_id")):
            raise ContractError("Kurssin reittitunnus puuttuu tai on virheellinen.")
        try:
            _validate_course_url(course)
        except BrowserError as exc:
            raise ContractError(str(exc)) from None
        if course.id in ids:
            raise ContractError("Kurssirajapinta palautti saman kurssitunnuksen useita kertoja.")
        ids.add(course.id)
        if course.start is None or course.end is None or course.registration_start is None:
            raise ContractError("Kurssirajapinnan alkamis- tai päättymisaikaa ei voitu tulkita.")
        free, queue = _integer(row["free"]), _integer(row["free_queue"])
        if (free is None or queue is None
                or type(row["is_reservable"]) not in {bool, int} or row["is_reservable"] not in (0, 1)):
            raise ContractError("Kurssirajapinnan paikka-, jono- tai varaustilaa ei voitu tulkita.")
        if not row["weekdays"] or _meeting_start_times(course) is None:
            raise ContractError("Kurssirajapinnan viikkoaikataulun (weekdays) rakenne muuttui.")
        products = row["fare_products"]
        if not isinstance(products, list) or not products or any(
            not isinstance(product, dict) or (_integer(product.get("fare_product_id")) or 0) <= 0
            or _integer(product.get("price")) is None or int(product["price"]) < 0
            or (product.get("bundle_price") is not None and (
                _integer(product["bundle_price"]) is None or int(product["bundle_price"]) < 0))
            for product in products
        ) or course.price is None:
            raise ContractError("Kurssin myyntituotteen fare_product_id tai price ei kelpaa varaukseen.")
        if free >= 0 and queue >= 0:
            supported += 1
    if not supported:
        raise ContractError("Yhdelläkään kurssilla ei ole varaukseen tulkittavaa paikka- ja jonotietoa.")
    return supported


class _PageScripts(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.sources: list[str] = []
        self.templates: dict[str, str] = {}
        self._template_id: str | None = None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        values = dict(attrs)
        if (src := values.get("src")):
            self.sources.append(src)
        elif values.get("type") == "text/html" and (name := values.get("id")):
            if name in self.templates:
                raise ContractError("Kaupan HTML:ssä on samannimisiä käyttöliittymämalleja.")
            self._template_id = name
            self.templates[name] = ""

    def handle_data(self, data: str) -> None:
        if self._template_id is not None:
            self.templates[self._template_id] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._template_id = None


_Attributes = dict[str, str | None]


class _Template(HTMLParser):
    """Parse the static element structure of a template without rendering it."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, _Attributes, tuple[_Attributes, ...]]] = []
        self._stack: list[tuple[str, _Attributes]] = []
        # Preserve dynamic attribute values as nonempty placeholders. Remove
        # control statements so they do not interrupt HTML attribute parsing.
        self.feed(re.sub(r"<%([=~\-]?)[\s\S]*?%>", lambda m: "TEMPLATE_VALUE" if m[1] else "", text))
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.elements.append((tag, values, tuple(parent for _, parent in self._stack)))
        if tag not in _VOID_TAGS:
            self._stack.append((tag, values))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def has(self, tag: str, *, class_name: str = "", within_id: str = "", within_class: str = "",
            **attributes: str | None) -> bool:
        for name, values, parents in self.elements:
            if name != tag or (class_name and class_name not in (values.get("class") or "").split()):
                continue
            if within_id and not any(parent.get("id") == within_id for parent in parents):
                continue
            if within_class and not any(within_class in (parent.get("class") or "").split() for parent in parents):
                continue
            if all(values.get(key) == value if value is not None else bool(values.get(key))
                   for key, value in attributes.items()):
                return True
        return False


def check_shop_html(text: str) -> str:
    page = _PageScripts(text)
    candidates: set[str] = set()
    for source in page.sources:
        try:
            candidate = urlsplit(urljoin(SHOP_PAGE, source))
        except ValueError:
            raise ContractError("Kaupan HTML:n JavaScript-osoite on virheellinen.") from None
        if not candidate.path.startswith(SCRIPT_DIRECTORY):
            continue
        if (candidate.scheme, candidate.netloc) != ("https", urlsplit(BASE_URL).netloc):
            raise ContractError("Kaupan varausmoduulin JavaScriptin alkuperä muuttui.")
        version = candidate.path.removeprefix(SCRIPT_DIRECTORY)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", version) or candidate.query or candidate.fragment:
            raise ContractError("Kaupan varausmoduulin julkinen JavaScript-osoite muuttui.")
        candidates.add(candidate.geturl())
    if len(candidates) != 1:
        raise ContractError("Kaupan HTML:stä ei löytynyt yksiselitteistä varausmoduulin JavaScript-osoitetta.")

    card = _Template(page.templates.get("template_eventgroup", ""))
    details = _Template(page.templates.get("template_eventgroup_modal", ""))
    participants = _Template(page.templates.get("template__participant_select", ""))
    selection = _Template(page.templates.get("template__eventgroup_buy", ""))
    cart = _Template(page.templates.get("template_cart", ""))
    checks = {
        "kurssin otsikko": card.has("h4", class_name="title") and details.has("h3", within_class="modal-header"),
        "kurssin varauspainike (button.buy)": card.has("button", class_name="buy")
        and details.has("button", class_name="buy"),
        "kurssin jonopainike (button.queue)": card.has("button", class_name="queue")
        and details.has("button", class_name="queue"),
        "osallistujan valinta (#linked-accounts)": participants.has("input", id="linked-accounts", type="radio"),
        "lapsitilin valinta (#linkedAccount)": participants.has(
            "input", within_id="linkedAccount", name="participant", type="radio", value=None, **{"data-name": None},
        ),
        "korilisäyksen vahvistus (.modal-footer .ok)": selection.has("a", class_name="ok", within_class="modal-footer"),
        "ostosehtojen valinta (#usageTermsCheckbox)": cart.has("input", id="usageTermsCheckbox", type="checkbox"),
        "saldon valinta (#useBalanceCheckbox)": cart.has("input", id="useBalanceCheckbox", type="checkbox"),
        "saldomaksun toiminto (.buyWithBalance)": cart.has("button", class_name="buyWithBalance", **{"payment-type-id": None}),
        "maksutavan toiminto (.buyCart)": any(cart.has(tag, class_name="buyCart", **{"payment-type-id": None})
                                              for tag in ("button", "a")),
        "maksutapavalikko (.dropdown-toggle)": cart.has(
            "button", class_name="dropdown-toggle", within_class="btn-group-dropdown-top-left",
        ),
    }
    if missing := [label for label, seen in checks.items() if not seen]:
        raise ContractError("Kaupan HTML:stä puuttuu odotettu rakenne: " + ", ".join(missing))
    return next(iter(candidates))


def check_shop_javascript(text: str) -> None:
    checks = {
        **{f"R2.{name}": rf"\bR2\s*\.\s*{name}\s*=\s*new\s+R2\s*\.\s*{model}\s*\("
           for name, model in {"session": "Session", "user": "User", "eventgroups": "EventGroups", "cart": "Cart"}.items()},
        "isAuthenticated": r"\bisAuthenticated\s*:\s*function\s*\(",
        **{event: rf"\bR2\s*\.\s*vent\s*\.\s*on\s*\(\s*['\"]{event}['\"]\s*,"
           for event in ("login:open", "cart:open")},
        **{endpoint: rf"\bEnkora\s*\.\s*getApiUrl\s*\(\s*['\"]reservations2/{endpoint}['\"]\s*\)"
           for endpoint in ("apieventgroups", "apicart", "apiusereventgroups")},
        "externalpaymentstart": r"\bEnkora\s*\.\s*ajax\s*\(\s*['\"]call/cart/externalpaymentstart['\"]\s*,",
    }
    if missing := [label for label, pattern in checks.items() if not re.search(pattern, text)]:
        raise ContractError("Kaupan JavaScriptistä puuttuu odotettu sopimusmerkki: " + ", ".join(missing))


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get_public(url: str, label: str) -> bytes:
    # No cookies or auth handlers, and no redirects to other origins or paths.
    opener = build_opener(_NoRedirects())
    request = Request(url, headers={"User-Agent": "uhkone (public contract check)"}, method="GET")
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read(_MAX_BYTES + 1)
    except HTTPError as exc:
        raise ContractError(f"{label} vastasi virheellä HTTP {exc.code}.") from None
    except (URLError, OSError, ValueError):
        raise ContractError(f"{label} ei ollut luettavissa.") from None
    if len(body) > _MAX_BYTES:
        raise ContractError(f"{label} ylitti julkisen tarkistuksen kokorajan.")
    return body


def run_public_contract() -> dict[str, object]:
    """Fetch three public sources and return only a summary and provenance."""
    try:
        html = _get_public(SHOP_PAGE, "Kaupan HTML")
        script_url = check_shop_html(html.decode("utf-8"))
        javascript = _get_public(script_url, "Kaupan JavaScript")
        check_shop_javascript(javascript.decode("utf-8"))
        catalogue = _get_public(SOURCE_URL, "Kurssirajapinta")
        rows = json.loads(catalogue)
        supported = check_catalogue(rows)
        count = len(rows)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ContractError("Julkisen lähteen merkistökoodaus tai JSON-rakenne muuttui.") from None
    return {
        "alkupera": BASE_URL,
        "tarkistettu_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "lahteet": [{"url": url, "sha256": sha256(content).hexdigest()}
                    for url, content in ((SHOP_PAGE, html), (script_url, javascript), (SOURCE_URL, catalogue))],
        "tarkistukset": {"kurssirajapinta": "läpäisty", "kurssit": count,
                         "ei_negatiiviset_paikkatiedot": supported, "negatiiviset_paikkatiedot": count - supported,
                         "html_mallien_rakenne": "läpäisty", "javascript_sopimusmerkit": "läpäisty"},
        "todentamatta": ["kirjautuminen", "tilin lapsivalinta", "ostoskori", "varaus", "jonotus",
                         "maksupalvelu", "FastPay", "maksu ja kuitti"],
    }
