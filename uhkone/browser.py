"""Headed browser preparation, followed by an explicit manual handoff.

Only existing child accounts can be selected. During preparation the network
policy blocks cart, reservation, account-change and payment requests, including
the shop's POST-based RPC calls. The policy is removed before the user takes over.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import unquote, urlsplit

from .client import BASE_URL
from .models import Course, normalize_text
from .origin import same_origin, validate_base_url

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


class BrowserError(Exception):
    """Browser setup or a changed shop UI requires the user's attention."""


_READ_CALLS = frozenset({
    "user/getcurrentuser", "common/tick", "common/getgenders",
    "common/getfields", "common/getPublicFareProducts",
    "common/getmyreceipts", "common/getcurrentaccountreceipts",
    "common/getownparameter", "reservation/geteventgroupcapacity",
    "reservation/getcampaignproductprice", "webshop/getsuggestedaccounts",
})
_READ_APIS = frozenset({
    "apieventgroups", "apievents", "apiusers", "apiproducts", "apicart",
    "apireservations", "apiusereventgroups", "apireservationservices",
    "apireservationgroups", "apireservationlocations", "apiservicegroups",
    "apireservationinstructors", "apireservationresourcestags",
    "apireservationaccessareas", "apiresourceslots", "apisalegroups",
    "apiresourceseasons", "apireservationresources", "apireservationtags",
    "apicoursetags",
})


def _request_allowed(url: str, method: str, *, allow_login: bool = False, base_url: str = BASE_URL) -> bool:
    """Fail closed: POST is also used for reads by Enkora.ajax()."""
    base_url = validate_base_url(base_url)
    if not same_origin(url, base_url):
        return False
    parsed = urlsplit(url)
    path = unquote(parsed.path)
    if ".." in path.split("/") or "\\" in path:
        return False
    verb = method.upper()
    prefix = "/webshop/call/"
    if path.startswith(prefix):
        call = path[len(prefix):].rstrip("/")
        if call == "common/customerlogin":
            return allow_login and verb == "POST"
        return call in _READ_CALLS and verb in {"GET", "POST"}
    if verb not in {"GET", "HEAD"}:
        return False
    api = re.fullmatch(r"/webshop/reservations2/(api[a-z]+)(?:/[0-9]+)?/?", path)
    if api:
        return api.group(1) in _READ_APIS
    if path in {"/webshop", "/webshop/", "/webshop/reservations2", "/webshop/reservations2/"}:
        return True
    if re.fullmatch(r"/webshop/reservations2/(?:login|login_selection)/?", path):
        return True
    if re.fullmatch(r"/webshop/reservations2/reservations/[0-9]+/[0-9-]+/[0-9-]+/-/[0-9]+/?", path):
        return True
    return path.startswith(("/webshop/resource/", "/webshop/javascript/jsmodule/")) or path in {
        "/webshop/javascript/translations", "/webshop/web/getthemevariablescss",
        "/webshop/organizationlogo/1",
    }


def _validate_course_url(course: Course, *, base_url: str = BASE_URL) -> str:
    base_url = validate_base_url(base_url)
    parsed = urlsplit(course.url)
    if (not same_origin(course.url, base_url)
            or parsed.query or parsed.fragment
            or not re.fullmatch(
                rf"/webshop/reservations2/reservations/9/[0-9]+/[0-9]+/-/{course.id}",
                parsed.path,
            )):
        raise BrowserError("Kurssin osoite ei ole tunnistettu Urheiluhallien kurssiosoite.")
    return course.url


class _PreparationGuard:
    def __init__(self, context: BrowserContext, *, base_url: str = BASE_URL):
        self.context = context
        self.base_url = validate_base_url(base_url)
        self.allow_login = True
        self.blocked = False

    def route(self, route: Any) -> None:
        if _request_allowed(route.request.url, route.request.method, allow_login=self.allow_login, base_url=self.base_url):
            route.continue_()
        else:
            # Never print request bodies, participant names, session data or URLs.
            parsed = urlsplit(route.request.url)
            if same_origin(route.request.url, self.base_url) and (
                route.request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
                or unquote(parsed.path).startswith("/webshop/call/")
            ):
                self.blocked = True
            route.abort("blockedbyclient")

    def install(self) -> None:
        self.context.route("**/*", self.route)

    def remove(self) -> None:
        self.context.unroute("**/*", self.route)


@contextmanager
def _browser(profile_dir: Path, executable_path: str | None, *, headless: bool = False) -> Generator[BrowserContext]:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as exc:
        raise BrowserError("Asenna selainosat: pip install -e . ja python -m playwright install chromium") from exc
    if not headless and sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise BrowserError("Selain tarvitsee graafisen työpöydän. Aja komento omalla tietokoneellasi; palvelimella ei ole näyttöä.")
    profile_dir = profile_dir.expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        profile_dir.chmod(0o700)
    launch_options: dict[str, Any] = {
        "headless": headless, "locale": "fi-FI", "timezone_id": "Europe/Helsinki",
        "service_workers": "block", "viewport": {"width": 1280, "height": 900},
    }
    if executable_path:
        launch_options["executable_path"] = executable_path
    # Use installed Chrome/Chromium when available; Playwright's installed
    # Chromium remains the fallback on systems without a system browser.
    elif shutil.which("chromium") or shutil.which("chromium-browser"):
        launch_options["executable_path"] = shutil.which("chromium") or shutil.which("chromium-browser")
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(str(profile_dir), **launch_options)
        except Error as exc:
            raise BrowserError("Selaimen käynnistys epäonnistui. Sulje saman profiilin muut ikkunat tai asenna python -m playwright install chromium. Voit valita selaimen --selain-polulla.") from exc
        try:
            context.set_default_timeout(20_000)
            yield context
        except Error as exc:
            raise BrowserError("Selaimen käyttö keskeytyi tai kaupan näkymä muuttui. Käynnistä valmistelu uudelleen; automaatio ei vahvistanut varausta.") from exc
        finally:
            try:
                context.close()
            except Error:
                pass


class _LoginGuard(Protocol):
    allow_login: bool


def _ensure_login(page: Page, guard: _LoginGuard, *, timeout_ms: float = 0) -> None:
    page.wait_for_function("() => !!(window.R2 && R2.session)")
    if not page.evaluate("() => R2.session.isAuthenticated()"):
        print("Kirjaudu avautuvassa Urheiluhallien selaimessa. Salasanaa ei lueta eikä tallenneta työkalun asetuksiin.")
        # The shop opens its own login UI. We never fill or submit credentials.
        page.evaluate("() => R2.vent.trigger('login:open')")
        page.wait_for_function("() => !!(window.R2 && R2.session && R2.session.isAuthenticated())", timeout=timeout_ms)
    guard.allow_login = False


def _select_child(page: Page, child_name: str | None) -> bool:
    """Expand children and select one exact existing name, never the parent."""
    linked = page.locator("#modal #linked-accounts")
    if linked.count() != 1:
        raise BrowserError("Kaupan lapsivalintaa ei löytynyt. Tarkista osallistuja itse selaimessa.")
    linked.check()
    children = page.locator('#modal #linkedAccount input[name="participant"]')
    children.first.wait_for(state="attached", timeout=5_000) if children.count() else None
    if not child_name:
        print("Lapsivalinta avattu. Valitse oikea lapsi itse ennen varaamista.")
        return False
    wanted = " ".join(child_name.casefold().split())
    matches = []
    for index in range(children.count()):
        child = children.nth(index)
        if " ".join((child.get_attribute("data-name") or "").casefold().split()) == wanted:
            matches.append(child)
    if len(matches) != 1:
        raise BrowserError("Annettua lasta ei löytynyt yksiselitteisesti olemassa olevista lapsitileistä. Valitse lapsi itse selaimessa.")
    matches[0].check()
    if not matches[0].is_checked():
        raise BrowserError("Lapsivalinta ei pysynyt voimassa. Tarkista osallistuja itse selaimessa.")
    print("Olemassa oleva lapsi valittu. Tarkista nimi ja mahdolliset ikä- tai tietovaroitukset selaimessa.")
    return True


def _is_queue_confirmation(value: str) -> bool:
    return normalize_text(value) in {
        "varaa paikka jonossa", "varaa paikka jonoon",
        "reserve a place in the queue", "reserve a place in queue", "reserve place in queue",
    }


def _prepare_page(page: Page, course: Course, child_name: str | None, *, navigate: bool = True,
                  base_url: str = BASE_URL, queue: bool = False) -> None:
    url = _validate_course_url(course, base_url=base_url)
    if navigate:
        page.goto(url, wait_until="domcontentloaded")
    # The dedicated URL identifies the group; only the matching visible title's
    # own container may be used. Avoid clicking a similarly named list item.
    # textContent joins the two languages across <br> without a space, whereas
    # the catalogue adapter inserts one. Preserve exact letters and punctuation
    # while accepting those layout-only whitespace differences.
    name_pattern = re.compile(r"^\s*" + r"\s*".join(re.escape(word) for word in course.name.split()) + r"\s*$")
    title = page.locator(".eventgroup h4.title").filter(has_text=name_pattern)
    modal_title = page.locator("#modal .modal-header h3").filter(has_text=name_pattern)
    page.wait_for_function("() => !!(window.R2 && R2.eventgroups && R2.eventgroups.length)")
    title.or_(modal_title).first.wait_for(state="visible")
    if title.count() == 1 and title.is_visible():
        container = title.locator("xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' eventgroup ')][1]")
    elif modal_title.count() == 1 and modal_title.is_visible():
        container = page.locator("#modal")
    else:
        raise BrowserError("Kurssin yksiselitteistä näkymää ei löytynyt. Valmistelu pysäytettiin.")
    if (not same_origin(page.url, base_url)
            or urlsplit(page.url).path.rstrip("/") != urlsplit(course.url).path.rstrip("/")):
        raise BrowserError("Kauppa siirsi eri kurssisivulle. Tarkista kurssi itse selaimessa.")
    buy = container.locator("button.queue" if queue else "button.buy")
    if buy.count() != 1 or not buy.is_visible() or not buy.is_enabled():
        raise BrowserError("Kurssin osallistujavalinta ei ole nyt avattavissa. Paikka voi olla täynnä tai ilmoittautuminen ei ole alkanut.")
    # Observed EventGroupView.buy only constructs EventGroupModalView. The
    # later .ok button writes to the cart/reservation; it is never clicked.
    buy.click()
    page.locator("#modal #linked-accounts").wait_for(state="visible")
    _select_child(page, child_name)
    if queue:
        confirmation = page.locator("#modal .modal-footer .ok")
        if confirmation.count() != 1 or not _is_queue_confirmation(confirmation.inner_text()):
            raise BrowserError("Jonoon liittymisen vahvistuspainiketta ei voitu tunnistaa. Pyyntöä ei lähetetty.")


def _manual_handoff(context: BrowserContext, page: Page) -> None:
    print("Automaatio päättyi. Tarkista kurssi, lapsi, hinta ja ehdot selaimessa. Tee varaus ja maksu itse.")
    print("Sulje selainikkuna lopuksi. Selainprofiili säilyy vain tällä tietokoneella.")
    # Pump Playwright events without blocking stdin or closing the browser as
    # soon as the program has prepared the page. The user can open payment tabs.
    while context.pages:
        active = next((item for item in context.pages if not item.is_closed()), None)
        if active is None:
            break
        try:
            active.wait_for_timeout(250)
        except Exception:
            if not context.pages:
                break
            raise


def login(profile_dir: Path, executable_path: str | None = None) -> None:
    """Log in manually and persist the isolated local browser profile."""
    with _browser(profile_dir, executable_path) as context:
        guard = _PreparationGuard(context)
        guard.install()
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(BASE_URL + "/webshop/reservations2/", wait_until="domcontentloaded")
        _ensure_login(page, guard)
        print("Kirjautuminen onnistui. Selainistunto tallennettu paikalliseen profiiliin.")


def prepare_course(course: Course, profile_dir: Path, child_name: str | None = None,
                   executable_path: str | None = None) -> None:
    """Prepare an exact course and existing child; the user completes booking."""
    _validate_course_url(course)
    with _browser(profile_dir, executable_path) as context:
        guard = _PreparationGuard(context)
        guard.install()
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(BASE_URL + "/webshop/reservations2/", wait_until="domcontentloaded")
        _ensure_login(page, guard)
        try:
            _prepare_page(page, course, child_name)
        except BrowserError as exc:
            print(f"Automaattinen valmistelu pysähtyi: {exc}")
        finally:
            # This boundary explicitly ends automation. The user's subsequent
            # clicks can now reserve, accept terms and pay in the same window.
            guard.remove()
        if guard.blocked:
            print("Kaupan tunnistamaton toiminto estettiin valmistelun aikana. Tarkista osallistujavalinta itse selaimessa.")
        _manual_handoff(context, page)
