"""Exercise the public-template simulator as a standalone shop on loopback."""
import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error, Route, expect, sync_playwright

from tests.test_testshop import course_row
from uhkone.booking import BookingSpec, run_booking
from uhkone.booking_browser import BookingBrowser, _BookingGuard
from uhkone.client import parse_course
from uhkone.testshop import TestShop


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                headless=True, executable_path=shutil.which("chromium") or shutil.which("chromium-browser"),
            )
        except Error as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        with browser:
            yield browser


def test_child_cart_remove_and_reload_stay_on_loopback(chromium):
    row = course_row()
    with TestShop([row], opens_in=0) as shop, chromium.new_context(service_workers="block") as context:
        local = urlsplit(shop.base_url)
        requests = []
        foreign_origins = []
        errors = []
        popups = []

        def keep_local(route: Route) -> None:
            url = urlsplit(route.request.url)
            requests.append((route.request.method, url.path))
            if (url.scheme, url.netloc) != (local.scheme, local.netloc):
                foreign_origins.append((url.scheme, url.netloc))
                route.abort()
                return
            route.continue_()

        context.route("**/*", keep_local)
        # CSP may reject an external resource before Chromium emits a route.
        context.add_init_script("""addEventListener('securitypolicyviolation', event => {
            if (/^https?:/.test(event.blockedURI) && new URL(event.blockedURI).origin !== location.origin) {
                window.reportForeignResource(new URL(event.blockedURI).origin);
            }
        });""")
        context.expose_function("reportForeignResource", lambda origin: foreign_origins.append(origin))
        page = context.new_page()
        context.on("page", lambda popup: popups.append(popup))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(shop.base_url + "/webshop/reservations2/")
        course = page.locator(".eventgroup")
        name = "Leikki-ikäisten jatkouimakoulu Advanced"
        expect(course.locator(".title")).to_have_text(name)
        availability = course.locator('[data-slot="availability"]')
        expect(availability).to_have_text("8 / 8 paikkaa vapaana")
        widget = page.locator("#cart")
        expect(widget).to_be_hidden()
        buy = course.locator("button.buy")
        expect(buy).to_have_text("Osta")
        buy.click()

        modal = page.locator("#modal")
        expansion = modal.locator("#linked-accounts")
        assert expansion.get_attribute("name") is None
        expansion.check()
        child = modal.locator('#linkedAccount input[data-name="Testilapsi"]')
        expect(child).to_be_visible()
        expect(child).not_to_be_checked()
        submit = modal.locator(".modal-footer a.ok")
        expect(submit).to_have_text("Siirrä ostoskoriin")
        if submit.is_enabled():
            submit.click()
        expect(child).to_be_visible()
        child.check()
        assert shop.stats()["writes"] == 0
        submit.click()

        cart_row = modal.locator("#cartItems tbody tr")
        expect(cart_row).to_have_count(1)
        expect(cart_row).to_be_visible()
        expect(cart_row).to_contain_text(name)
        expect(cart_row).to_contain_text(re.compile(r"\b76,00\s*€"))
        expect(modal.locator("#usageTermsCheckbox")).not_to_be_checked()
        expect(widget).to_be_visible()
        modal.locator(".continueShopping").click()
        pending = course.get_by_role("row").filter(has_text="Testilapsi").filter(has_text="Ei vahvistettu")
        expect(pending).to_be_visible()
        expect(availability).to_have_text("7 / 8 paikkaa vapaana")
        assert shop.stats()["participant_ids"] == [20]

        widget.locator(".buy").click()
        cart_row.locator(".remove").click()
        expect(widget).to_be_hidden()
        page.reload()
        expect(course.locator(".title")).to_have_text(name)
        expect(availability).to_have_text("8 / 8 paikkaa vapaana")
        expect(widget).to_be_hidden()
        expect(pending).to_have_count(0)
        stats = shop.stats()
        assert stats["cart_items"] == stats["reservations"] == 0
        assert stats["cart_posts"] == 1
        assert stats["checkout_posts"] == stats["receipts"] == stats["fake_card_charges"] == 0
        assert requests.count(("POST", "/webshop/reservations2/apicart")) == 1
        assert ("GET", "/webshop/resource/testshop/templates.html") in requests
        assert not foreign_origins
        assert not popups
        assert not errors


def test_successful_cart_add_stays_visible_when_catalogue_refresh_fails(chromium):
    with TestShop([course_row()], opens_in=0) as shop, chromium.new_context(service_workers="block") as context:
        prefix = shop.base_url + "/webshop/reservations2/"
        cart_posts = []
        replaced_refreshes = []
        fail_next_refresh = False

        def deliver_one_refresh_error(route: Route) -> None:
            nonlocal fail_next_refresh
            request = route.request
            if not request.url.startswith(shop.base_url + "/"):
                route.abort()
                return
            if request.url == prefix + "apicart" and request.method == "POST":
                response = route.fetch(max_redirects=0)
                cart_posts.append(response.status)
                fail_next_refresh = cart_posts == [200]
                route.fulfill(response=response)
                response.dispose()
            elif request.url == prefix + "apieventgroups" and request.method == "GET" and fail_next_refresh:
                fail_next_refresh = False
                response = route.fetch(max_redirects=0)
                replaced_refreshes.append(response.status)
                response.dispose()
                route.fulfill(status=503, json={"errors": ["Tilapäinen testilukuvirhe"]})
            else:
                route.continue_()

        context.route("**/*", deliver_one_refresh_error)
        page = context.new_page()
        page.goto(prefix)
        course = page.locator(".eventgroup")
        course.locator("button.buy").click()
        modal = page.locator("#modal")
        modal.locator("#linked-accounts").check()
        modal.locator('#linkedAccount input[data-name="Testilapsi"]').check()
        with page.expect_response(lambda response: response.url == prefix + "apieventgroups" and response.status == 503):
            modal.locator(".modal-footer a.ok").click()

        item = modal.locator("#cartItems tbody tr")
        expect(item).to_have_count(1)
        expect(item).to_be_visible()
        expect(item).to_contain_text("Leikki-ikäisten jatkouimakoulu Advanced")
        expect(item).to_contain_text(re.compile(r"\b76,00\s*€"))
        expect(modal.locator("#usageTermsCheckbox")).not_to_be_checked()
        page.reload()
        expect(course.get_by_role("row").filter(has_text="Testilapsi")).to_contain_text("Ei vahvistettu")
        page.locator("#cart .buy").click()
        expect(item).to_have_count(1)
        expect(item).to_be_visible()
        expect(item).to_contain_text("Leikki-ikäisten jatkouimakoulu Advanced")
        assert replaced_refreshes == [200] and cart_posts == [200]
        stats = shop.stats()
        assert stats["cart_posts"] == stats["cart_items"] == stats["reservations"] == 1
        assert stats["writes"] == 1
        assert stats["checkout_posts"] == stats["receipts"] == stats["fake_card_charges"] == 0


def test_initial_cart_read_failure_preserves_courses_and_recovers_existing_cart(chromium):
    row = course_row()
    with TestShop([row], opens_in=0) as shop, chromium.new_context(service_workers="block") as context:
        prefix = shop.base_url + "/webshop/reservations2/"
        cart_url = prefix + "apicart"
        seeded = context.request.post(cart_url, max_redirects=0, data={
            "account": {"account_id": 20, "name": "Testilapsi"}, "reservation_event_group_id": 123,
            "fare_product_id": 4321, "quantity": 1, "price": 7600, "title": row["name"],
        })
        assert seeded.status == 200 and len(seeded.json()) == 1
        seeded.dispose()
        cart_reads = []
        held_retries = []
        allow_retry = False

        def fail_first_cart_read(route: Route) -> None:
            if not route.request.url.startswith(shop.base_url + "/"):
                route.abort()
                return
            if route.request.url != cart_url or route.request.method != "GET":
                route.continue_()
                return
            cart_reads.append(route.request)
            if len(cart_reads) == 1:
                original = route.fetch(max_redirects=0)
                assert original.status == 200
                original.dispose()
                route.fulfill(status=503, json={"errors": ["Testikorin luku epäonnistui"]})
            elif allow_retry:
                route.continue_()
            else:
                # Keep the cart unreadable while checking the independently loaded course.
                held_retries.append(route)

        context.route("**/*", fail_first_cart_read)
        page = context.new_page()
        with page.expect_response(lambda response: response.url == cart_url and response.status == 503):
            page.goto(prefix)
        course = page.locator(".eventgroup")
        name = "Leikki-ikäisten jatkouimakoulu Advanced"
        expect(course.locator(".title")).to_have_text(name)
        expect(course.get_by_role("row").filter(has_text="Testilapsi")).to_contain_text("Ei vahvistettu")
        allow_retry = True
        for route in held_retries:
            route.continue_()
        widget = page.locator("#cart")
        expect(widget).to_be_visible(timeout=10_000)
        widget.locator(".buy").click()
        item = page.locator("#modal #cartItems tbody tr")
        expect(item).to_have_count(1)
        expect(item).to_contain_text(name)
        expect(item).to_contain_text(re.compile(r"\b76,00\s*€"))
        assert len(cart_reads) >= 2
        stats = shop.stats()
        assert stats["writes"] == stats["cart_posts"] == stats["cart_items"] == stats["reservations"] == 1
        assert stats["checkout_posts"] == stats["receipts"] == stats["fake_card_charges"] == 0


def test_checkout_from_another_course_page_pays_only_the_cart_course(chromium):
    first = course_row() | {"name": "Ensimmäinen testikurssi"}
    second = first | {"reservation_event_group_id": 124, "name": "Toinen testikurssi",
                      "fare_products": [{"fare_product_id": 4322, "price": 8400}]}
    with TestShop([first, second], opens_in=0) as shop, chromium.new_context(service_workers="block") as context:
        def local_only(route: Route) -> None:
            if route.request.url.startswith(shop.base_url + "/"):
                route.continue_()
            else:
                route.abort()

        context.route("**/*", local_only)
        page = context.new_page()
        page.goto(shop.base_url + "/")
        cards = page.locator(".eventgroup")
        cards.filter(has_text=first["name"]).locator("button.buy").click()
        modal = page.locator("#modal")
        modal.locator("#linked-accounts").check()
        modal.locator('#linkedAccount input[data-name="Testilapsi"]').check()
        modal.locator(".modal-footer a.ok").click()
        item = modal.locator("#cartItems tbody tr")
        expect(item).to_contain_text(first["name"])
        modal.locator(".continueShopping").click()

        cards.filter(has_text=second["name"]).get_by_role("link", name="Avaa kurssi").click()
        expect(page).to_have_url(parse_course(second, base_url=shop.base_url).url)
        expect(cards.locator(".title")).to_have_text(second["name"])
        page.locator("#cart .buy").click()
        expect(item).to_have_count(1)
        expect(item).to_contain_text(first["name"])
        expect(item).to_contain_text(re.compile(r"\b76,00\s*€"))
        page.wait_for_function("""() => {
            const view = R2.app.modal.currentView;
            return view?.kind === 'cart' && Number.isFinite(view.balance) && Array.isArray(view.types);
        }""")
        modal.locator("#usageTermsCheckbox").check()
        checkout_url = shop.base_url + "/webshop/call/cart/externalpaymentstart"
        with page.expect_response(lambda response: response.url == checkout_url and response.request.method == "POST") as checkout:
            modal.locator('.buyCart[payment-type-id="5"]').click()
        assert checkout.value.status == 200
        stats = shop.stats()
        assert stats["cart_posts"] == stats["checkout_posts"] == stats["receipts"] == stats["fake_card_charges"] == 1
        assert stats["fastpay_total_cents"] == 7600
        assert stats["cart_items"] == 0
        assert [(r["reservation_event_group_id"], r["reservation_account_id"], r["is_paid"])
                for r in stats["reservation_rows"]] == [(123, 20, 1)]


# Let both real HTTP requests start. Deliver the second response to the
# simulator only once the first result has actually rendered in that view.
_ORDER_CART_RESPONSES = """first => {
    const paths = new Map([
        ['/webshop/call/cart/getonlinepaymenttypes', 'methods'],
        ['/webshop/call/cart/checkcurrentuserbalance', 'balance'],
    ]);
    const deliveredAfterRender = new WeakSet();
    const ready = (view, kind) => kind === 'balance'
        ? Number.isFinite(view.balance) : Array.isArray(view.types);
    const currentCart = () => window.R2?.app?.modal?.currentView;
    const fetchResponse = window.fetch.bind(window);
    window.fetch = async (resource, options) => {
        const view = currentCart();
        const url = new URL(resource instanceof Request ? resource.url : resource, location.href);
        const kind = paths.get(url.pathname);
        const response = await fetchResponse(resource, options);
        if (view?.kind !== 'cart' || !kind || kind === first) return response;
        await new Promise(resolve => {
            const observer = new MutationObserver(check);
            function check() {
                if (currentCart() === view && !ready(view, first)) return;
                if (currentCart() === view && !ready(view, kind)) deliveredAfterRender.add(view);
                observer.disconnect();
                resolve();
            }
            observer.observe(document.querySelector('#modal'), {subtree: true, childList: true});
            check();
        });
        return response;
    };
    window.testCheckoutClicks = [];
    document.addEventListener('click', event => {
        const action = event.target.closest('.buyCart, .buyWithBalance');
        if (!action) return;
        const view = currentCart();
        window.testCheckoutClicks.push({
            sawFirstRender: deliveredAfterRender.has(view),
            ready: ready(view, 'methods') && ready(view, 'balance'),
            terms: document.querySelector('#modal #usageTermsCheckbox').checked,
            method: action.getAttribute('payment-type-id'),
        });
    }, true);
}"""


@pytest.mark.parametrize(("first_response", "mode", "method"), [
    ("methods", "fastpay", 5),
    ("balance", "saldo", 4),
])
def test_real_checkout_waits_for_both_cart_renders(chromium, tmp_path, first_response, mode, method):
    row = course_row()
    with TestShop([row], opens_in=0) as shop, chromium.new_context(service_workers="block") as context:
        course = parse_course(row, base_url=shop.base_url)
        spec = BookingSpec(course, "Testilapsi", Decimal(76), shop.opens_at,
                           datetime.now(UTC) + timedelta(seconds=30), mode, payment_type_id=method)
        guard = _BookingGuard(context, spec, base_url=shop.base_url)
        guard.allow_login = False
        context.route("**/*", guard.route)
        context.on("response", guard.response)
        context.add_init_script(f"({_ORDER_CART_RESPONSES})({json.dumps(first_response)})")
        page = context.new_page()
        page.set_default_timeout(5_000)
        backend = BookingBrowser(spec, context, page, guard, base_url=shop.base_url)
        backend.initialize()
        page.wait_for_function("() => Boolean(window.R2?.cart)")
        journal = tmp_path / "booking.json"
        result = run_booking(spec, backend, journal, log=lambda _: None)
        assert page.evaluate("window.testCheckoutClicks") == [
            {"sawFirstRender": True, "ready": True, "terms": True, "method": str(method)},
        ]
        assert result.status == "confirmed" and result.receipt_id
        before = shop.stats()
        assert before["cart_posts"] == before["checkout_posts"] == before["receipts"] == before["confirmed"] == 1
        assert before["participant_ids"] == [20]
        assert before["payment_type_ids"] == [method]
        assert before["receipt_rows"][0]["total_amount"] == 7600
        assert before["fake_card_charges"] == (1 if mode == "fastpay" else 0)
        assert before["balance_cents"] == (100000 if mode == "fastpay" else 92400)
        assert run_booking(spec, backend, journal, log=lambda _: None) == result
        assert shop.stats()["writes"] == before["writes"]
