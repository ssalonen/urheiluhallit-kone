"""Read-only preflight for a specifically selected child and course IDs.

All network operations run through the existing authenticated persistent
Chromium profile; no booking, payment, queue or account mutation is permitted.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .browser import BrowserError, _browser, _ensure_login, _PreparationGuard
from .client import BASE_URL, HELSINKI, ClientError, parse_course
from .linked_accounts import _integer, linked_child_names
from .origin import validate_base_url


def validate_preflight_snapshot(*, user: Any, cart: Any, reservations: Any,
                                courses: Any, ids: list[int], child: str,
                                max_price: str, base_url: str = BASE_URL) -> dict:
    """Fail closed, return only public data and non-sensitive status codes."""
    if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
            or any(type(cid) is not int or cid <= 0 for cid in ids)):
        raise ValueError("Kurssitunnukset eivät ole yksiselitteisiä.")
    if not isinstance(child, str) or not child.strip():
        raise ValueError("Valittu lapsi puuttuu.")
    if not isinstance(cart, list) or any(not isinstance(row, dict) for row in cart):
        raise ValueError("Ostoskorin lukutieto on virheellinen.")
    if not isinstance(reservations, list) or any(not isinstance(row, dict) for row in reservations):
        raise ValueError("Omien varausten lukutieto on virheellinen.")
    if not isinstance(courses, list) or len(courses) != len(ids):
        raise ValueError("Valittujen kurssien tiedot ovat puutteelliset.")
    if not isinstance(user, dict):
        raise ValueError("Kirjautunut huoltajatili ei ole vahvistettavissa.")  # noqa: TRY004
    names = linked_child_names(user)
    wanted = " ".join(child.casefold().split())
    if sum(" ".join(name.casefold().split()) == wanted for name in names) != 1:
        raise ValueError("Valittu lapsi ei vastaa olemassa olevaa yksiselitteistä alitiliä.")
    accounts = user.get("linked_accounts")
    entries = accounts.values() if isinstance(accounts, dict) else accounts
    selected_id = next(_integer(a.get("account_id")) for a in entries
                       if isinstance(a, dict) and " ".join(str(a.get("name", "")).casefold().split()) == wanted)
    try:
        ceiling = Decimal(max_price.replace(",", "."))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("Enimmäishinta ei ole kelvollinen.") from exc
    if not ceiling.is_finite() or ceiling < 0:
        raise ValueError("Enimmäishinta ei ole kelvollinen.")
    # Every response must match the supplied order, not just belong to its set.
    verified = []
    issues = []
    for expected, raw in zip(ids, courses, strict=True):
        try:
            course = parse_course(raw, base_url=base_url)
        except (ClientError, ValueError) as exc:
            raise ValueError("Valitun kurssin JSON-vastausta ei voida vahvistaa.") from exc
        if course.id != expected:
            raise ValueError("Kurssipalvelin palautti väärän kurssitunnuksen.")
        ready_data = course.registration_start is not None and course.price is not None
        if not ready_data:
            issues.append("kurssitieto_puutteellinen")
        elif course.price > ceiling:
            issues.append("hinta_ylittaa_rajan")
        verified.append({
            "id": course.id,
            "location": course.location, "time": course.time_start,
            "opens_at": course.registration_start.isoformat() if course.registration_start else None,
            "price": str(course.price) if course.price is not None else None,
            "free": course.free_places,
            "reservable": course.is_reservable,
        })
    if cart:
        issues.append("ostoskori_ei_tyhja")
    for row in reservations:
        account_id = _integer(row.get("reservation_account_id"))
        course_id = _integer(row.get("reservation_event_group_id"))
        status = _integer(row.get("reservation_status_id"))
        if account_id is None or course_id is None or status is None:
            raise ValueError("Omien varausten tunnistetiedot eivät ole yksiselitteiset.")
        if account_id == selected_id and course_id in ids and status != 6:
            issues.append("aiempi_varaus_loytyi")
            break
    return {
        "ready": not issues,
        "issues": list(dict.fromkeys(issues)),
        "courses": verified,
        "cart_empty": not cart,
        "checked_at": datetime.now(HELSINKI).isoformat(timespec="seconds"),
    }


def run_readonly_preflight(profile_dir: Path, executable: str | None, *,
                           ids: list[int], child: str, max_price: str,
                           base_url: str = BASE_URL, headless: bool = False) -> dict:
    """Only allowlisted merchant reads; no POST other than user/getcurrentuser."""
    base_url = validate_base_url(base_url)
    with _browser(profile_dir, executable, headless=headless) as context:
        guard = _PreparationGuard(context, base_url=base_url)
        guard.install()
        page = context.pages[0] if context.pages else context.new_page()
        response = page.goto(base_url + "/webshop/reservations2/", wait_until="domcontentloaded")
        if response is None or response.status != 200:
            raise BrowserError("Verkkokaupan kirjautumissivu ei ollut luettavissa.")
        _ensure_login(page, guard, timeout_ms=90_000)

        def get_json(path: str, *, user_rpc: bool = False) -> Any:
            path = path.lstrip("/")
            allowed = ({"webshop/reservations2/apicart", "webshop/reservations2/apiusereventgroups"}
                       | {f"webshop/reservations2/apieventgroups/{course_id}" for course_id in ids})
            if not (user_rpc and path == "webshop/call/user/getcurrentuser" or
                    not user_rpc and path in allowed):
                raise BrowserError("Esitarkistus ei salli tätä rajapintaa.")
            if user_rpc:
                answer = context.request.post(base_url + "/" + path,
                    form={"input_format": "json", "output_format": "json", "content": "[true,true]"},
                    timeout=15_000, max_redirects=0)
            else:
                answer = context.request.get(base_url + "/" + path,
                    timeout=15_000, max_redirects=0)
            try:
                if answer.status != 200:
                    raise BrowserError("Esitarkistuksen lukupyyntö epäonnistui.")
                try:
                    data = answer.json()
                except ValueError as exc:
                    raise BrowserError("Verkkokauppa ei palauttanut JSONia.") from exc
            finally:
                answer.dispose()
            if user_rpc:
                if (not isinstance(data, dict) or "result" not in data or
                        data.get("errors") or data.get("user_errors")):
                    raise BrowserError("Huoltajatilin lukutieto on epäselvä.")
                return data["result"]
            return data

        user = get_json("webshop/call/user/getcurrentuser", user_rpc=True)
        cart = get_json("webshop/reservations2/apicart")
        reservations = get_json("webshop/reservations2/apiusereventgroups")
        courses = [get_json(f"webshop/reservations2/apieventgroups/{cid}") for cid in ids]
        return validate_preflight_snapshot(user=user, cart=cart, reservations=reservations,
                                           courses=courses, ids=ids, child=child,
                                           max_price=max_price, base_url=base_url)
