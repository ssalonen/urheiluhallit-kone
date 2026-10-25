"""Offline Chromium integration of controller, merchant UI and scoped guard."""
import json
import shutil
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from uhkone.booking import BookingSpec, run_booking
from uhkone.booking_browser import BookingBrowser, _BookingGuard
from uhkone.client import parse_course

FIXTURE = '''<!doctype html><meta charset="utf-8">
<div class="eventgroup"><h4 class="title">Leikki-ikäisten jatkouimakoulu</h4>
<button class="buy" onclick="participants()">Osta</button></div><div id="modal" hidden></div>
<script>
window.modelRefreshes = 0; window.selectedParticipants = []; window.cartRows = [];
const modal = document.querySelector('#modal');
function participants() {
  modal.hidden = false;
  modal.innerHTML = `<div class="modal-header"><h3>Leikki-ikäisten jatkouimakoulu</h3></div>
    <input type="radio" name="participant" value="10" checked>
    <input type="radio" name="participant" id="linked-accounts" value="children">
    <div id="linkedAccount" style="display:none">
      <input type="radio" name="participant" value="20" data-name="Aino Esimerkki">
      <input type="radio" name="participant" value="21" data-name="Eino Esimerkki"></div>
    <div class="modal-footer"><a href="#" class="ok">Lisää ostoskoriin</a></div>`;
  document.querySelector('#linked-accounts').onclick = () =>
    document.querySelector('#linkedAccount').style.display = 'block';
  document.querySelector('.ok').onclick = async event => {
    event.preventDefault();
    const child = document.querySelector('input[name="participant"]:checked');
    window.selectedParticipants.push(Number(child.value));
    const payload = {reservation_event_group_id:123, fare_product_id:4321,
      quantity:1, price:7600, title:'Leikki-ikäisten jatkouimakoulu',
      account:{account_id:child.value, name:child.dataset.name || 'Huoltaja'}, campaign_code:null};
    await fetch('/webshop/reservations2/apicart', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    modal.hidden = true;
  };
}
function cartModal() {
  modal.hidden = false;
  modal.innerHTML = `<input type="checkbox" id="usageTermsCheckbox">
    <input type="checkbox" id="useBalanceCheckbox">
    <a href="#" class="buyWithBalance" payment-type-id="4">Maksa saldolla</a>`;
  document.querySelector('.buyWithBalance').onclick = async event => {
    event.preventDefault();
    if (!document.querySelector('#usageTermsCheckbox').checked ||
        !document.querySelector('#useBalanceCheckbox').checked) throw Error('Terms or balance missing');
    const options = {return_url:location.origin+'/webshop/reservations2/?doPayment=success',
      cancel_url:location.origin+'/webshop/reservations2/?doPayment=cancel',
      balance_amount:7600, payment_type_id:'4', service_group_id:9};
    const form = new URLSearchParams({input_format:'json', output_format:'json', content:JSON.stringify([options])});
    await fetch('/webshop/call/cart/externalpaymentstart', {method:'POST', body:form});
    modal.hidden = true;
  };
}
window.R2 = {
  session:{isAuthenticated:()=>true}, user:{get:key=>key==='account_id'?10:null}, service_group_id:9,
  eventgroups:{length:1, get:id=>Number(id)===123?{set:row=>window.modelRefreshes++}:null},
  cart:{fetch:async()=>{window.cartRows=await fetch('/webshop/reservations2/apicart').then(r=>r.json());},
        toJSON:()=>window.cartRows},
  vent:{trigger:name=>{if(name==='cart:open')cartModal();}}
};
</script>'''


class BookingFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright unavailable")
        cls.playwright = sync_playwright().start()
        try:
            executable = shutil.which("chromium") or shutil.which("chromium-browser")
            cls.browser = cls.playwright.chromium.launch(
                headless=True, executable_path=executable)
        except Exception as exc:  # noqa: BLE001 - Skip browser integration when the browser cannot start.
            cls.playwright.stop()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_scheduled_exact_child_cart_and_balance_checkout_reconcile_real_ui(self):
        row = {"reservation_event_group_id": 123, "service_id": 9, "location_id": 2,
               "reservation_group_id": 6, "name": "Leikki-ikäisten jatkouimakoulu",
               "location": "Mäkelänrinne", "start": "2030-01-10 10:00:00",
               "end": "2030-02-10 10:30:00", "free": 3, "is_reservable": 1,
               "fare_products": [{"fare_product_id": 4321, "price": 7600}]}
        moment = datetime(2030, 1, 1, tzinfo=UTC)
        opening = moment + timedelta(seconds=6)
        spec = BookingSpec(parse_course(row), "Aino Esimerkki", Decimal(80), opening,
                           opening + timedelta(seconds=40), "saldo")
        server: dict[str, Any] = {"cart": [], "own": [], "receipt": {
            "receipt_id": 77, "total_amount": 7600, "is_closed": 1, "is_pay_later": 0}}
        writes = []
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(3000)
        guard = _BookingGuard(context, spec)
        guard.allow_login = False

        def fulfill(route):
            request, path = route.request, urlsplit(route.request.url).path
            if request.method == "POST":
                self.assertGreaterEqual(moment, opening)
                writes.append((path, request.post_data))
                if path == "/webshop/reservations2/apicart":
                    payload = json.loads(request.post_data)
                    self.assertEqual(int(payload["account"]["account_id"]), 20)
                    server["cart"] = [{"item_id": 11, "fare_product_id": 4321, "quantity": 1,
                        "price": 7600, "reservation_params": {"reservation_event_group_id": 123,
                                                              "reservation_account_id": 20}}]
                    response = server["cart"]
                else:
                    self.assertEqual(path, "/webshop/call/cart/externalpaymentstart")
                    options = json.loads(parse_qs(request.post_data)["content"][0])[0]
                    self.assertEqual((int(options["payment_type_id"]), options["balance_amount"]), (4, 7600))
                    self.assertEqual(server["cart"][0]["reservation_params"]["reservation_account_id"], 20)
                    server["cart"] = []
                    server["own"] = [{"reservation_id": 456, "reservation_event_group_id": 123,
                        "reservation_account_id": 20, "reservation_status_id": 4, "is_paid": 1}]
                    server["receipt"] = server["receipt"] | {"receipt_id": 78}
                    response = {"result": {"payment_succeeded": True}}
                route.fulfill(status=200, content_type="application/json", body=json.dumps(response))
            elif path == "/webshop/reservations2/apicart":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(server["cart"]))
            else:
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE)

        # Exercise the actual guard decision, replacing only its allowed network
        # transmission with an in-memory merchant. No request reaches a host.
        def routed(route):
            guard.route(SimpleNamespace(request=route.request,
                continue_=lambda: fulfill(route), abort=route.abort))

        context.route("**/*", routed)
        context.on("response", guard.response)
        backend = BookingBrowser(spec, context, page, guard)

        def read(path, *, rpc=False, content=None):
            if path == "user/getcurrentuser":
                return {"account_id": 10, "linked_accounts": [{"account_id": 20, "name": spec.child_name}]}
            values = {f"/webshop/reservations2/apieventgroups/{spec.course.id}": row,
                "/webshop/reservations2/apicart": server["cart"],
                "/webshop/reservations2/apiusereventgroups": server["own"],
                "cart/getreceiptinfo": server["receipt"], "cart/islocked": {"isLocked": False},
                "cart/checkcurrentuserbalance": {"balance": 10000},
                "cart/getonlinepaymenttypes": [{"payment_type_id": 4, "is_disabled": 0,
                                               "is_pay_later": 0, "is_balance": 1}]}
            return deepcopy(values[path])

        def wait(seconds):
            nonlocal moment
            moment += timedelta(seconds=seconds)
            page.wait_for_timeout(1)

        with tempfile.TemporaryDirectory() as directory, patch.object(backend, "_read", side_effect=read):
            backend.wait = Mock(side_effect=wait)
            backend.initialize()
            result = run_booking(spec, backend, Path(directory) / "state.json",
                                 now=lambda: moment, log=lambda message: None)
        self.assertEqual((result.status, result.reservation_id, result.receipt_id), ("confirmed", "456", "78"))
        self.assertEqual([path for path, _ in writes], ["/webshop/reservations2/apicart",
                                                      "/webshop/call/cart/externalpaymentstart"])
        self.assertEqual(page.evaluate("window.selectedParticipants"), [20])
        self.assertGreater(page.evaluate("window.modelRefreshes"), 0)
        self.assertTrue(guard.checkout_sent)
        self.assertFalse(guard.blocked_write)
        self.assertEqual(server["cart"], [])


if __name__ == "__main__":
    unittest.main()
