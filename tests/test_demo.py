"""The standalone control-pane example ends at the selected payment boundary."""

from pathlib import Path
import shutil
import unittest


class DemoTests(unittest.TestCase):
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
                headless=True, **({"executable_path": executable} if executable else {}))
        except Exception as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_unpaid_demo_stops_with_one_cart_item_and_zero_payments(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.goto((Path(__file__).resolve().parents[1] / "docs" / "esimerkkiajo.html").as_uri())
        page.locator("#goal").select_option("cart")
        for _ in range(20):
            if not page.locator("#next").is_enabled():
                break
            page.locator("#next").click()
        self.assertFalse(page.locator("#next").is_enabled())
        self.assertEqual(page.locator("#cart-count").inner_text(), "1 tuote")
        self.assertEqual(page.locator("#payment-count").inner_text(), "0")
        self.assertEqual(page.locator("#actual-count").inner_text(), "0")
        self.assertIn("Maksamaton", page.locator("#cart-label").inner_text())
        self.assertNotIn("MAKSA", page.locator("#terminal-lines .log-tag").all_text_contents())
