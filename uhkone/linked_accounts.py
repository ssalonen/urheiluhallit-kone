"""Read linked child account names from the signed-in shop, without writes.

The account IDs are checked and kept internal to this read. The booking adapter
independently resolves the chosen name to an account ID before sending a write.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .booking_browser import _integer
from .browser import BrowserError, _PreparationGuard, _browser, _ensure_login
from .client import BASE_URL
from .origin import validate_base_url


def linked_child_names(user: Any) -> list[str]:
    """Only unambiguous real child accounts; never return payer IDs or raw JSON."""
    if not isinstance(user, dict):
        raise BrowserError("Kirjautuneen tilin tietomuoto ei vastaa odotettua.")
    payer = _integer(user.get("account_id"))
    if payer is None or payer <= 0:
        raise BrowserError("Huoltajan tilitunnusta ei voitu vahvistaa.")
    linked = user.get("linked_accounts")
    if not isinstance(linked, (dict, list)):
        raise BrowserError("Kirjautuneella tilillä ei ole luettavia alitilejä.")
    entries = linked.values() if isinstance(linked, dict) else linked
    names: list[str] = []
    identifiers: set[int] = set()
    seen_names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise BrowserError("Alitilin tiedot eivät vastaa odotettua.")
        child_id, name = _integer(entry.get("account_id")), entry.get("name")
        if (child_id is None or child_id <= 0 or child_id == payer
                or not isinstance(name, str) or not name.strip() or len(name) > 160):
            raise BrowserError("Alitilillä ei ole yksilöitävissä olevaa nimeä tai tunnistetta.")
        label = " ".join(name.split())
        folded = label.casefold()
        if child_id in identifiers or folded in seen_names:
            raise BrowserError("Alitileillä on toistuva nimi tai tunniste. Valinta on tehtävä yksiselitteiseksi.")
        identifiers.add(child_id)
        seen_names.add(folded)
        names.append(label)
    return names


def load_linked_children(profile_dir: Path, executable: str | None = None, *,
                         base_url: str = BASE_URL, headless: bool = False) -> list[str]:
    """Use the normal persistent browser login and *one* allowlisted read RPC."""
    base_url = validate_base_url(base_url)
    with _browser(profile_dir, executable, headless=headless) as context:
        guard = _PreparationGuard(context, base_url=base_url)
        guard.install()
        page = context.pages[0] if context.pages else context.new_page()
        response = page.goto(base_url + "/webshop/reservations2/", wait_until="domcontentloaded")
        if response is None or response.status != 200:
            raise BrowserError("Kirjautumissivun lukeminen epäonnistui.")
        _ensure_login(page, guard, timeout_ms=90_000)
        # A POST is used as a read by Enkora; never use the booking/cart RPC.
        result = context.request.post(base_url + "/webshop/call/user/getcurrentuser",
            form={"input_format": "json", "output_format": "json", "content": "[true,true]"},
            timeout=15_000, max_redirects=0)
        try:
            if result.status != 200:
                raise BrowserError("Alitilien lukupyyntö epäonnistui.")
            try:
                data = result.json()
            except ValueError as exc:
                raise BrowserError("Alitilien rajapinta ei palauttanut JSON-tietoa.") from exc
        finally:
            result.dispose()
        if (not isinstance(data, dict) or "result" not in data or data.get("errors")
                or data.get("user_errors")):
            raise BrowserError("Alitilien rajapinta palautti virheen.")
        return linked_child_names(data["result"])
