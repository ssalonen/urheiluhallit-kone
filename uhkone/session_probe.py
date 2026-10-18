"""Read-only observation of carts in two independent, manually logged-in profiles.

Profile isolation proves cookie isolation, not server-side cart isolation. This
probe never adds a product or starts a payment, so empty carts are inconclusive.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

from .browser import BrowserError, _PreparationGuard, _ensure_login
from .client import BASE_URL


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0:
        return int(value)
    return None


@dataclass(frozen=True)
class _Snapshot:
    # Account and cart-line identifiers stay in memory and never enter reports.
    account_id: int | None
    item_ids: tuple[int, ...] | None
    item_count: int
    course_ids: tuple[int, ...]
    observed_at: str

    @property
    def authenticated(self) -> bool:
        return self.account_id is not None

    def public(self, profile_path: Path) -> dict:
        return {"profile_path": str(profile_path), "authenticated": self.authenticated,
                "cart_item_count": self.item_count, "course_ids": list(self.course_ids),
                "observed_at": self.observed_at}


def _snapshot(user: Any, cart: Any) -> _Snapshot:
    if not isinstance(user, dict):
        raise BrowserError("Kirjautumisen lukurajapinnan tietomuoto muuttui.")
    if not isinstance(cart, list) or any(not isinstance(row, dict) for row in cart):
        raise BrowserError("Ostoskorin lukurajapinnan tietomuoto muuttui.")
    item_ids = [_positive_integer(row.get("item_id")) for row in cart]
    ids_known = all(value is not None for value in item_ids) and len(set(item_ids)) == len(item_ids)
    course_ids = set()
    for row in cart:
        params = row.get("reservation_params")
        if isinstance(params, dict):
            course_id = _positive_integer(params.get("reservation_event_group_id"))
            if course_id is not None:
                course_ids.add(course_id)
    return _Snapshot(_positive_integer(user.get("account_id")),
                     tuple(sorted(item_ids)) if ids_known else None,
                     len(cart), tuple(sorted(course_ids)), datetime.now(timezone.utc).isoformat())


def _compare_snapshots(first: _Snapshot, second: _Snapshot) -> dict:
    if not first.authenticated or not second.authenticated:
        return {"same_account": None, "cart_relationship": "login_missing",
                "conclusion": "Molempien profiilien kirjautumista ei voitu vahvistaa. Koria ei voi verrata luotettavasti."}
    if first.account_id != second.account_id:
        return {"same_account": False, "cart_relationship": "different_accounts",
                "conclusion": "Profiilit ovat kirjautuneet eri tileille. Tulos ei kerro saman tilin korien erillisyydestä."}
    if first.item_count == second.item_count == 0:
        return {"same_account": True, "cart_relationship": "inconclusive_empty",
                "conclusion": "Molemmat korit ovat tyhjiä. Tästä ei voi päätellä, ovatko korit palvelimella yhteisiä vai erillisiä."}
    if (first.item_ids is not None and second.item_ids is not None
            and first.item_ids == second.item_ids and first.item_count > 0
            and first.course_ids == second.course_ids):
        return {"same_account": True, "cart_relationship": "shared_cart_observed",
                "conclusion": "Molemmissa profiileissa näkyivät samat palvelimen koririvit. Havainto tukee yhteistä ostoskoria; erillisiin profiileihin ei pidä luottaa korien erottamisessa."}
    if (first.item_count != second.item_count or first.course_ids != second.course_ids
            or (first.item_ids is not None and second.item_ids is not None
                and first.item_ids != second.item_ids)):
        return {"same_account": True, "cart_relationship": "different_carts_observed",
                "conclusion": "Korien sisältö erosi havaintohetkellä. Tämä ei vielä todista pysyvää erillisyyttä: palvelin voi yhdistää korit tai sisältö on voinut muuttua lukujen välillä."}
    return {"same_account": True, "cart_relationship": "inconclusive_identifiers",
            "conclusion": "Korien julkinen sisältö vastaa toisiaan, mutta palvelimen koririvitunnukset puuttuvat. Korien yhteisyyttä tai erillisyyttä ei voitu vahvistaa."}


def _read_json(context: Any, path: str) -> Any:
    """APIRequestContext bypasses page routes: permit exactly these two reads."""
    options = {"timeout": 15_000, "max_redirects": 0}
    if path == "user/getcurrentuser":
        response = context.request.post(BASE_URL + "/webshop/call/" + path, form={
            "input_format": "json", "output_format": "json", "content": "[true,true]",
        }, **options)
        rpc = True
    elif path == "/webshop/reservations2/apicart":
        response = context.request.get(BASE_URL + path, **options)
        rpc = False
    else:
        raise BrowserError("Istuntotesti sallii vain kirjautumisen ja ostoskorin lukemisen.")
    try:
        if not 200 <= response.status < 300:
            raise BrowserError(f"Istuntotestin lukurajapinta vastasi HTTP {response.status}. Korien yhteisyyttä ei vahvistettu.")
        try:
            data = response.json()
        except Exception as exc:
            raise BrowserError("Istuntotesti ei saanut odotettua JSON-vastausta.") from exc
    finally:
        response.dispose()
    if rpc:
        if (not isinstance(data, dict) or "result" not in data
                or data.get("errors") or data.get("user_errors")):
            raise BrowserError("Istuntotestin kirjautumisen lukurajapinta palautti virheen.")
        return data["result"]
    return data


def _observe(context: Any) -> _Snapshot:
    return _snapshot(_read_json(context, "user/getcurrentuser"),
                     _read_json(context, "/webshop/reservations2/apicart"))


def _write_report(path: Path, report: dict) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".istuntotesti-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inspect_sessions(profile_root: Path, executable_path: str | None = None, *,
                     report_path: Path | None = None, login_timeout_seconds: int = 600) -> dict:
    """Open two visible profiles, allow manual login, then compare only reads.

    Existing cart items are observed without changing them. Login is necessary
    in each profile for a same-account comparison. No report contains account
    IDs, names, raw responses, credentials, cookies or payment information.
    """
    if isinstance(login_timeout_seconds, bool) or not isinstance(login_timeout_seconds, int) or login_timeout_seconds <= 0:
        raise BrowserError("Kirjautumisen odotusajan tulee olla positiivinen kokonaisluku sekunteina.")
    try:
        from playwright.sync_api import sync_playwright, Error
    except ImportError as exc:
        raise BrowserError("Asenna selainosat: pip install -e . ja python -m playwright install chromium") from exc
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise BrowserError("Istuntotesti tarvitsee näkyvän selaimen. Aja komento omalla tietokoneellasi graafisella työpöydällä.")
    profile_root = Path(profile_root).expanduser().resolve()
    profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        profile_root.chmod(0o700)
    paths = [profile_root / "profiili-a", profile_root / "profiili-b"]
    options = {"headless": False, "locale": "fi-FI", "timezone_id": "Europe/Helsinki",
               "service_workers": "block", "viewport": {"width": 1000, "height": 900}}
    browser_executable = executable_path or shutil.which("chromium") or shutil.which("chromium-browser")
    if browser_executable:
        options["executable_path"] = browser_executable
    print("Avataan kaksi erillistä selainprofiilia. Kirjaudu molemmissa samalle huoltajan tilille.")
    print("Testi lukee korit. Tuotteiden lisäys, poisto, varaus ja maksu on estetty myös käsin painettaessa.")
    try:
        with sync_playwright() as playwright, ExitStack() as stack:
            contexts = []
            guards = []
            pages = []
            for path in paths:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                if os.name == "posix":
                    path.chmod(0o700)
                context = playwright.chromium.launch_persistent_context(str(path), **options)
                stack.callback(context.close)
                context.set_default_timeout(20_000)
                guard = _PreparationGuard(context)
                guard.install()
                contexts.append(context)
                guards.append(guard)
                page = context.pages[0] if context.pages else context.new_page()
                pages.append(page)
                page.goto(BASE_URL + "/webshop/reservations2/", wait_until="domcontentloaded")
            for index, (page, guard) in enumerate(zip(pages, guards), start=1):
                print(f"Vahvistetaan profiilin {index} kirjautuminen.")
                _ensure_login(page, guard, timeout_ms=login_timeout_seconds * 1000)
            snapshots = [_observe(context) for context in contexts]
            report = {"version": 1, "shop_url": BASE_URL, "read_only": True,
                      "profiles": [snapshot.public(path) for snapshot, path in zip(snapshots, paths)],
                      **_compare_snapshots(*snapshots)}
            if report_path is not None:
                _write_report(Path(report_path), report)
            print(report["conclusion"])
            return report
    except Error as exc:
        raise BrowserError("Istuntotestin selain tai lukuyhteys keskeytyi. Korien yhteisyyttä ei vahvistettu; testi ei tehnyt varausta tai maksua.") from exc
