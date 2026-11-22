"""Actual Chromium UI and booking coordinator tests against localhost only.

Browser UI is exercised over HTTP. The injected booking runner talks only to
the local TestShop simulator and must never open a production merchant URL.
"""
from __future__ import annotations

import json
import queue
import shutil
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error, expect, sync_playwright

from tests.test_testshop import course_row
from uhkone.booking import BookingSpec
from uhkone.client import ShopClient
from uhkone.multi import run_race
from uhkone.multi_browser import open_booking_pool
from uhkone.panel import serve_panel
from uhkone.testshop import TestShop


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=shutil.which("chromium") or shutil.which("chromium-browser"),
            )
        except Error as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        with browser:
            yield browser


class PanelServer:
    def __init__(self, directory: Path, runner=None, port=0, child_loader=None):
        self.directory, self.runner, self.port = directory, runner, port
        self.child_loader = child_loader
        self.stop = threading.Event()
        self.url: str = ""
        self.thread: threading.Thread | None = None
        self.failures: queue.Queue[BaseException] = queue.Queue()

    def __enter__(self):
        addresses: queue.Queue[str] = queue.Queue()

        def serving():
            try:
                serve_panel(self.directory, port=self.port, open_browser=False,
                            runner=self.runner, child_loader=self.child_loader, stop_event=self.stop,
                            on_listening=addresses.put)
            except BaseException as exc:  # noqa: BLE001 - Propagate worker failures to tests.
                self.failures.put(exc)

        self.thread = threading.Thread(target=serving, name="panel-ui-test", daemon=True)
        self.thread.start()
        try:
            self.url = addresses.get(timeout=10)
        except queue.Empty:
            if not self.failures.empty():
                raise self.failures.get()
            raise AssertionError("Testipaneeli ei käynnistynyt") from None
        assert urlsplit(self.url).hostname == "127.0.0.1"
        return self

    def __exit__(self, *_):
        self.stop.set()
        assert self.thread is not None
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()
        if not self.failures.empty():
            raise self.failures.get()


def test_panel_ui_localstorage_survives_reload_and_server_restart(chromium, tmp_path):
    with chromium.new_context() as context:
        page = context.new_page()
        with PanelServer(tmp_path) as host:
            page.goto(host.url)
            expect(page.locator("#ids")).to_have_value("110980 110962 110970")
            page.locator("#ids").fill("110970 110980 110962")
            page.locator("#price").fill("85,00")
            page.locator("#child").fill("Testilapsi")
            page.locator("#save").click()
            expect(page.locator("#message")).to_contain_text("tallennettu")
            assert json.loads(page.evaluate(
                "() => localStorage.getItem('uhkone.panel.preferences.v1')"
            )) == {"ids": [110970, 110980, 110962],
                    "max_price": "85.00", "child": "Testilapsi"}
            state = page.request.get(host.url + "api/status").json()
            assert state["has_child"]
            assert "Testilapsi" not in json.dumps(state, ensure_ascii=False)
            page.reload()
            expect(page.locator("#ids")).to_have_value("110970 110980 110962")
            expect(page.locator("#child")).to_have_value("Testilapsi")
            port = urlsplit(host.url).port
        assert port is not None

        with PanelServer(tmp_path, port=port) as host:
            page.goto(host.url)
            expect(page.locator("#ids")).to_have_value("110970 110980 110962")
            expect(page.locator("#price")).to_have_value("85.00")
            expect(page.locator("#child")).to_have_value("Testilapsi")
            expect(page.locator("#cards .card")).to_have_count(3)
            assert page.request.get(host.url + "api/status").json()["has_child"]


def test_panel_button_runs_simulated_unpaid_booking_without_checkout(chromium, tmp_path):
    with TestShop([course_row()], opens_in=0) as shop:
        assert urlsplit(shop.base_url).hostname == "127.0.0.1"
        outcomes: queue.Queue[str] = queue.Queue()
        with ShopClient(base_url=shop.base_url) as client:
            course = client.get_course(shop.course_id)
        assert course.price is not None

        def run_simulator(state, _directory, _executable):
            try:
                state.course_info([course])
                spec = BookingSpec(
                    course=course, child_name="Testilapsi", max_price=Decimal(76),
                    opens_at=shop.opens_at,
                    deadline=datetime.now(UTC) + timedelta(seconds=55),
                    payment_mode="none", without_payment=True,
                )
                with open_booking_pool(
                    [spec], tmp_path / "simulated-profile",
                    base_url=shop.base_url, headless=True,
                ) as pool:
                    result = run_race(
                        [spec], pool, tmp_path / "simulated-journal",
                        allow_queue=False, log=lambda _: None,
                        progress=state.progress,
                        checkpoint=lambda: state.checkpoint(pool.wait),
                    )
                    outcomes.put(result.status)
                    with state.lock:
                        state.phase = result.status
                        state.note = "Maksamaton ostoskori; mitään ei veloitettu."
            finally:
                with state.lock:
                    state.running = False

        with PanelServer(tmp_path, runner=run_simulator) as host, chromium.new_context() as context:
            page = context.new_page()
            page.goto(host.url)
            page.locator("#ids").fill(str(course.id))
            page.locator("#price").fill("76")
            page.locator("#child").fill("Testilapsi")
            page.locator("#save").click()
            expect(page.locator("#message")).to_contain_text("tallennettu")
            page.locator("#start").click()
            expect(page.locator("#phase")).to_have_text("HELD_UNPAID", timeout=55000)
            assert outcomes.get(timeout=3) == "held_unpaid"
            row = page.request.get(host.url + "api/status").json()
            assert row["ids"] == [course.id]
            assert row["details"][str(course.id)]["location"] == course.location
            assert row["details"][str(course.id)]["opens_at"] is not None
            assert "Testilapsi" not in json.dumps(row, ensure_ascii=False)
            stats = shop.stats()
            assert stats["cart_posts"] == 1
            assert stats["cart_items"] == 1
            assert stats["reservations"] == 1
            assert stats["checkout_posts"] == 0
            assert stats["receipts"] == 0
            assert stats["fake_card_charges"] == 0

def test_panel_child_picker_uses_linked_account_names_not_ids(chromium, tmp_path):
    calls = []

    def listed(directory, executable):
        calls.append((directory, executable))
        return ["Testilapsi", "Toinen testilapsi"]

    with PanelServer(tmp_path, child_loader=listed) as host, chromium.new_context() as context:
        page = context.new_page()
        page.goto(host.url)
        page.locator("#children").click()
        expect(page.locator("#child-select option")).to_have_count(3)
        page.locator("#child-select").select_option(label="Toinen testilapsi")
        expect(page.locator("#child")).to_have_value("Toinen testilapsi")
        page.locator("#save").click()
        expect(page.locator("#message")).to_contain_text("tallennettu")
        state = page.request.get(host.url + "api/status").json()
        assert state["has_child"]
        assert "Toinen testilapsi" not in json.dumps(state, ensure_ascii=False)
        assert "account_id" not in page.locator("#child-select").inner_html()
        assert len(calls) == 1
        page.reload()
        expect(page.locator("#child")).to_have_value("Toinen testilapsi")


def test_logged_in_simulator_children_are_read_only(chromium, tmp_path):
    from uhkone.linked_accounts import load_linked_children

    with TestShop([course_row()], opens_in=0) as shop:
        assert urlsplit(shop.base_url).hostname == "127.0.0.1"
        before = shop.stats()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            names = executor.submit(load_linked_children, tmp_path / "test-child-profile",
                                    base_url=shop.base_url, headless=True).result(timeout=40)
        after = shop.stats()
        assert names == ["Testilapsi"]
        assert before["writes"] == after["writes"]
        assert after["cart_posts"] == 0
        assert after["checkout_posts"] == 0
        assert after["queue_posts"] == 0


def test_child_lookup_blocks_competing_booking_start(chromium, tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def slow_lookup(_directory, _executable):
        entered.set()
        if not release.wait(timeout=10):
            raise AssertionError("Alitilin testihaku aikakatkaistiin")
        return ["Testilapsi"]

    with PanelServer(tmp_path, child_loader=slow_lookup) as host, chromium.new_context() as context:
        page = context.new_page()
        page.goto(host.url)
        page.locator("#child").fill("Testilapsi")
        page.locator("#save").click()
        expect(page.locator("#message")).to_contain_text("tallennettu")
        try:
            page.locator("#children").click()
            assert entered.wait(timeout=5)
            status = page.request.get(host.url + "api/status").json()
            assert status["children_loading"] is True
            denied = page.evaluate("""async () => {
                try { await req('/api/start', {}); return false; }
                catch (_) { return true; }
            }""")
            assert denied
            assert not page.request.get(host.url + "api/status").json()["running"]
        finally:
            release.set()
        expect(page.locator("#child-select option")).to_have_count(2)
        assert not page.request.get(host.url + "api/status").json()["children_loading"]
