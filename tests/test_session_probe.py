"""Read-only session diagnostics never infer isolation from cookie profiles."""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from uhkone.browser import BrowserError, _PreparationGuard
from uhkone.client import BASE_URL
from uhkone.session_probe import (
    _compare_snapshots,
    _read_json,
    _snapshot,
    _write_report,
)


def cart_row(item_id=55, course_id=100569):
    return {"item_id": item_id, "name": "Private child name",
            "reservation_params": {"reservation_event_group_id": course_id,
                                   "reservation_account_id": 999}}


def snapshot(account=10, rows=None):
    return _snapshot({"account_id": account, "name": "Private parent name"}, rows or [])


class CartComparisonTests(unittest.TestCase):
    def compare(self, first, second):
        return _compare_snapshots(first, second)

    def test_empty_carts_never_prove_isolation(self):
        value = self.compare(snapshot(), snapshot())
        self.assertTrue(value["same_account"])
        self.assertEqual(value["cart_relationship"], "inconclusive_empty")

    def test_exact_nonempty_server_lines_are_evidence_of_shared_cart(self):
        value = self.compare(snapshot(rows=[cart_row()]), snapshot(rows=[cart_row()]))
        self.assertEqual(value["cart_relationship"], "shared_cart_observed")

    def test_different_accounts_cannot_establish_same_account_cart_isolation(self):
        value = self.compare(snapshot(10, [cart_row()]), snapshot(20, [cart_row()]))
        self.assertFalse(value["same_account"])
        self.assertEqual(value["cart_relationship"], "different_accounts")

    def test_missing_login_does_not_count_as_another_account(self):
        for account in (None, 0, False, ""):
            with self.subTest(account=account):
                value = self.compare(snapshot(account), snapshot())
                self.assertIsNone(value["same_account"])
                self.assertEqual(value["cart_relationship"], "login_missing")

    def test_different_content_is_only_an_observation(self):
        for rows in ([], [cart_row(item_id=56)], [cart_row(course_id=100570)]):
            with self.subTest(rows=rows):
                value = self.compare(snapshot(rows=[cart_row()]), snapshot(rows=rows))
                self.assertEqual(value["cart_relationship"], "different_carts_observed")
                self.assertIn("ei vielä todista", value["conclusion"])

    def test_similar_public_courses_without_line_ids_are_inconclusive(self):
        row = cart_row()
        del row["item_id"]
        value = self.compare(snapshot(rows=[row]), snapshot(rows=[row]))
        self.assertEqual(value["cart_relationship"], "inconclusive_identifiers")

    def test_duplicate_line_ids_are_not_accepted_as_shared_cart_evidence(self):
        value = self.compare(snapshot(rows=[cart_row(), cart_row()]),
                             snapshot(rows=[cart_row(), cart_row()]))
        self.assertEqual(value["cart_relationship"], "inconclusive_identifiers")

    def test_report_contains_only_counts_public_courses_and_login_result(self):
        value = snapshot(888, [cart_row()])
        report = value.public(Path("/tmp/profile-a")) | self.compare(value, value)
        serialized = json.dumps(report)
        self.assertNotIn("Private", serialized)
        self.assertNotIn("888", serialized)
        self.assertNotIn("999", serialized)
        self.assertNotIn('"item_ids"', serialized)
        self.assertNotIn('"account_id"', serialized)
        self.assertEqual(report["course_ids"], [100569])
        self.assertEqual(report["cart_item_count"], 1)

    def test_schema_errors_fail_without_interpreting_empty_content(self):
        for user, cart in (([], []), ({}, {}), ({}, ["unexpected"])):
            with self.subTest(user=user, cart=cart), self.assertRaises(BrowserError):
                _snapshot(user, cart)


class ReadOnlyScopeTests(unittest.TestCase):
    def context(self, result, *, status=200):
        response = SimpleNamespace(status=status, json=lambda: result, dispose=Mock())
        request = SimpleNamespace(get=Mock(return_value=response), post=Mock(return_value=response))
        return SimpleNamespace(request=request), response

    def test_post_is_only_a_hardcoded_getcurrentuser_read(self):
        context, response = self.context({"result": {"account_id": 10}})
        self.assertEqual(_read_json(context, "user/getcurrentuser"), {"account_id": 10})
        context.request.post.assert_called_once_with(
            BASE_URL + "/webshop/call/user/getcurrentuser",
            form={"input_format": "json", "output_format": "json", "content": "[true,true]"},
            timeout=15_000, max_redirects=0)
        context.request.get.assert_not_called()
        response.dispose.assert_called_once()

    def test_cart_request_is_get_with_redirects_disabled(self):
        context, _response = self.context([cart_row()])
        self.assertEqual(_read_json(context, "/webshop/reservations2/apicart"), [cart_row()])
        context.request.get.assert_called_once_with(
            BASE_URL + "/webshop/reservations2/apicart", timeout=15_000, max_redirects=0)
        context.request.post.assert_not_called()

    def test_unknown_endpoints_never_reach_the_api_request_context(self):
        for path in ("cart/resetcart", "cart/externalpaymentstart", "common/customerlogin",
                     "/webshop/reservations2/apicart/55", "/webshop/call/user/getcurrentuser"):
            context, _ = self.context({})
            with self.subTest(path=path), self.assertRaises(BrowserError):
                _read_json(context, path)
            context.request.post.assert_not_called()
            context.request.get.assert_not_called()

    def test_redirect_is_not_a_successful_read(self):
        context, response = self.context({}, status=302)
        with self.assertRaises(BrowserError):
            _read_json(context, "/webshop/reservations2/apicart")
        response.dispose.assert_called_once()

    def test_browser_guard_also_blocks_manual_cart_and_payment_buttons(self):
        guard = _PreparationGuard(MagicMock())
        calls = []
        for path, verb in (("/webshop/reservations2/apicart", "POST"),
                           ("/webshop/reservations2/apicart/55", "DELETE"),
                           ("/webshop/call/cart/externalpaymentstart", "POST")):
            request = SimpleNamespace(url=BASE_URL + path, method=verb)
            guard.route(SimpleNamespace(request=request,
                                        continue_=lambda: calls.append("sent"),
                                        abort=lambda _: calls.append("blocked")))
        self.assertEqual(calls, ["blocked"] * 3)

    def test_report_replaces_existing_file_with_private_permissions(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text("old", encoding="utf-8")
            path.chmod(0o644)
            _write_report(path, {"read_only": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"read_only": True})
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
