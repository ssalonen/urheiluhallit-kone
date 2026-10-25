"""Policy regression tests and a local, offline child-selection UI fixture."""
import shutil
import unittest
from dataclasses import replace

from uhkone.browser import (
    BrowserError,
    _is_queue_confirmation,
    _prepare_page,
    _request_allowed,
    _select_child,
    _validate_course_url,
)
from uhkone.client import BASE_URL
from uhkone.models import Course


class PreparationPolicyTests(unittest.TestCase):
    def test_waitlist_confirmation_must_explicitly_name_the_queue(self):
        for text in ("Varaa paikka jonossa", "Varaa paikka jonoon", "Reserve a place in the queue", "Reserve place in queue"):
            self.assertTrue(_is_queue_confirmation(text))
        for text in ("Varaa", "Lisää ostoskoriin", "Add to cart", "Maksa", "Varaa paikka jonossa ja maksa"):
            self.assertFalse(_is_queue_confirmation(text))

    def test_catalogue_and_post_reads_are_allowed(self):
        for path, method in (
            ("/webshop/reservations2/apieventgroups?service_id=9", "GET"),
            ("/webshop/reservations2/apiusers/42", "GET"),
            ("/webshop/call/reservation/geteventgroupcapacity", "POST"),
            ("/webshop/call/user/getcurrentuser", "POST"),
            ("/webshop/call/common/tick", "POST"),
        ):
            with self.subTest(path=path):
                self.assertTrue(_request_allowed(BASE_URL + path, method))

    def test_login_only_allowed_in_login_phase(self):
        url = BASE_URL + "/webshop/call/common/customerlogin"
        self.assertFalse(_request_allowed(url, "POST"))
        self.assertTrue(_request_allowed(url, "POST", allow_login=True))
        self.assertFalse(_request_allowed(url, "GET", allow_login=True))

    def test_reservation_cart_and_account_writes_blocked(self):
        for path, method in (
            ("/webshop/reservations2/apicart", "POST"),
            ("/webshop/reservations2/apicart/1", "DELETE"),
            ("/webshop/reservations2/apiusereventgroups", "POST"),
            ("/webshop/reservations2/apiusers/42", "PUT"),
            ("/webshop/call/account/linknewaccount", "POST"),
            ("/webshop/call/common/modifyaccount", "POST"),
            ("/webshop/call/cart/checkout", "POST"),
            ("/webshop/call/new/readlookingoperation", "POST"),
            ("/webshop/call/reservation/create", "GET"),
        ):
            with self.subTest(path=path, method=method):
                self.assertFalse(_request_allowed(BASE_URL + path, method, allow_login=True))

    def test_unknown_hosts_and_encoded_traversal_blocked(self):
        for url in (
            BASE_URL.replace("https:", "http:") + "/webshop/",
            BASE_URL + ".example.com/webshop/",
            BASE_URL + "/webshop/resource/%2e%2e/call/account/linknewaccount",
            BASE_URL + "/webshop/resource/..%5ccall/account/linknewaccount",
            "https://payments.example/checkout",
        ):
            with self.subTest(url=url):
                self.assertFalse(_request_allowed(url, "GET", allow_login=True))

    def test_only_matching_exact_course_url_is_accepted(self):
        course = Course(123, "Kurssi", "Mäkelänrinne", url=BASE_URL + "/webshop/reservations2/reservations/9/2/6/-/123")
        self.assertEqual(_validate_course_url(course), course.url)
        for url in (
            course.url.replace("/-/123", "/-/124"),
            course.url + "?other=124",
            course.url + "#other",
            course.url.replace("https:", "http:"),
            course.url.replace("/9/", "/10/"),
        ):
            with self.subTest(url=url), self.assertRaises(BrowserError):
                _validate_course_url(replace(course, url=url))


class ChildSelectionFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright unavailable")
        cls.playwright = sync_playwright().start()
        try:
            executable = shutil.which("chromium") or shutil.which("chromium-browser")
            cls.browser = cls.playwright.chromium.launch(headless=True, executable_path=executable)
        except Exception:  # noqa: BLE001 - Skip browser integration when the browser cannot start.
            cls.playwright.stop()
            raise unittest.SkipTest("Chromium unavailable")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content('''
          <div id="modal">
            <input name="participant" type="radio" value="parent" checked>
            <input id="linked-accounts" type="radio">
            <div id="linkedAccount" style="display:none">
              <input name="participant" type="radio" data-name="Aino Esimerkki" value="child1">
              <input name="participant" type="radio" data-name="Eino Esimerkki" value="child2">
              <button class="addAccount">Lisää lapsi</button>
            </div>
            <input type="checkbox" id="terms">
            <button class="ok">Lisää ostoskoriin</button>
          </div>
          <script>
            window.writes = 0;
            document.querySelector('#linked-accounts').onclick = () => {
              document.querySelector('[value=parent]').checked = false;
              document.querySelector('#linkedAccount').style.display = 'block';
            };
            for (const item of document.querySelectorAll('.ok,.addAccount,#terms'))
              item.onclick = () => window.writes++;
          </script>
        ''')

    def tearDown(self):
        self.assertEqual(self.page.evaluate("window.writes"), 0)
        self.assertFalse(self.page.locator("#terms").is_checked())
        self.assertFalse(self.page.locator('[value="parent"]').is_checked())
        self.page.close()

    def test_exact_existing_child_selected_without_purchase(self):
        self.assertTrue(_select_child(self.page, "  aino   esimerkki "))
        self.assertTrue(self.page.locator('[value="child1"]').is_checked())
        self.assertFalse(self.page.locator('[value="child2"]').is_checked())

    def test_no_name_leaves_child_choice_manual(self):
        self.assertFalse(_select_child(self.page, None))
        self.assertEqual(self.page.locator('#linkedAccount input:checked').count(), 0)

    def test_partial_name_never_selects_or_creates_child(self):
        with self.assertRaises(BrowserError):
            _select_child(self.page, "Aino")
        self.assertEqual(self.page.locator('#linkedAccount input:checked').count(), 0)

    def test_duplicate_name_never_selects_ambiguously(self):
        self.page.locator('[value="child2"]').evaluate("e => e.dataset.name = 'Aino Esimerkki'")
        with self.assertRaises(BrowserError):
            _select_child(self.page, "Aino Esimerkki")
        self.assertEqual(self.page.locator('#linkedAccount input:checked').count(), 0)

    def test_bilingual_course_preparation_selects_child_without_cart_action(self):
        course = Course(123, "Leikki-ikäisten jatkouimakoulu Swimming School for Preschoolers, advanced",
                        "Mäkelänrinne", url=BASE_URL + "/webshop/reservations2/reservations/9/2/6/-/123")
        card = '''<script>window.R2={eventgroups:{length:1}};</script>
          <div class="eventgroup">
            <h4 class="title">Leikki-ikäisten jatkouimakoulu<br>Swimming School for Preschoolers, advanced</h4>
            <button class="buy" onclick="document.querySelector('#modal').style.display='block'">Osta</button>
          </div>'''
        fixture = self.page.content().replace("<body>", "<body>" + card).replace(
            '<div id="modal">', '<div id="modal" style="display:none">'
        )
        self.page.route("**/*", lambda route: route.fulfill(status=200,
                         content_type="text/html; charset=utf-8", body=fixture))
        self.page.set_default_timeout(5_000)
        _prepare_page(self.page, course, "Aino Esimerkki")
        self.assertEqual(self.page.url, course.url)
        self.assertTrue(self.page.locator('[value="child1"]').is_checked())


if __name__ == "__main__":
    unittest.main()
