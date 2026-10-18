"""The selected unpublished-for-booking course is a valid booking target."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import io
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from uhkone import cli
from uhkone.config import Settings
from uhkone.models import Course


class ReservationCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.url = "https://kauppa.urheiluhallit.fi/webshop/reservations2/reservations/9/2/6/-/123"
        self.course = Course(123, "Leikki-ikäisten jatkouimakoulu", "Mäkelänrinne",
                             start=datetime(2099, 2, 1, 17, tzinfo=cli.HELSINKI),
                             price=Decimal("76"), free_places=8, is_reservable=False,
                             registration_start=datetime(2099, 1, 12, 7, tzinfo=cli.HELSINKI),
                             url=self.url)
        self.client = MagicMock()
        self.client.__enter__.return_value = self.client
        self.client.get_course.return_value = self.course
        client_module = ModuleType("uhkone.client")
        client_module.ShopClient = Mock(return_value=self.client)
        client_module.ClientError = type("ClientError", (Exception,), {})
        booking = ModuleType("uhkone.booking")
        booking.BookingSpec = lambda **kwargs: SimpleNamespace(**kwargs)
        multi = ModuleType("uhkone.multi")
        self.run_booking = multi.run_race = Mock(return_value=SimpleNamespace(
            status="confirmed", course_id=123, reservation_id="r123", receipt_id="receipt123"))
        browser = ModuleType("uhkone.booking_browser")
        self.open_browser = browser.open_booking_browser = MagicMock()
        self.backend = self.open_browser.return_value.__enter__.return_value
        self.backend.identity = "1:2:123"
        pool_module = ModuleType("uhkone.multi_browser")
        self.open_pool = pool_module.open_booking_pool = MagicMock()
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {"uhkone.client": client_module,
                                "uhkone.booking": booking,
                                "uhkone.multi": multi,
                                "uhkone.multi_browser": pool_module,
                                "uhkone.booking_browser": browser}).start()
        patch("uhkone.cli.load_settings", return_value=Settings()).start()
        patch("uhkone.cli.state_directory", return_value=Path(self.tmp.name)).start()
        self.args = ["varaa", self.url, "--lapsi", "Testilapsi", "--enintaan", "76,00", "--maksutapa", "saldo"]

    def invoke(self, extra=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(self.args + list(extra))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_published_course_can_be_armed_before_registration_opens(self):
        code, stdout, stderr = self.invoke(["--vapaat"])
        self.assertEqual(code, 0, stderr)
        self.client.get_course.assert_called_once_with(123)
        specs, pool, state = self.run_booking.call_args.args
        spec = specs[0]
        self.assertEqual(spec.course, self.course)
        self.assertFalse(spec.course.available)
        self.assertEqual(spec.opens_at, self.course.registration_start)
        self.assertEqual(spec.max_price, Decimal("76.00"))
        self.assertGreater(spec.deadline, spec.opens_at)
        self.assertEqual(pool.activate(123), self.backend)
        self.assertEqual(pool.identity, "https://kauppa.urheiluhallit.fi|1:2")
        self.assertNotIn("Testilapsi", str(state))
        self.assertIn("VARAUS VAHVISTETTU", stdout)

    def test_maximum_price_blocks_booking_before_browser(self):
        self.client.get_course.return_value = replace(self.course, price=Decimal("76.01"))
        code, _, stderr = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("enimmäissumman", stderr)
        self.open_browser.assert_not_called()
        self.run_booking.assert_not_called()

    def test_fastpay_is_passed_as_distinct_payment_mode(self):
        self.args[-1] = "fastpay"
        code, stdout, stderr = self.invoke(["--maksutunnus", "5"])
        self.assertEqual(code, 0, stderr)
        spec = self.run_booking.call_args.args[0][0]
        self.assertEqual(spec.payment_mode, "fastpay")
        self.assertEqual(spec.payment_type_id, 5)
        self.assertIn("fastpay", stdout)

    def test_requested_queue_fallback_is_default_and_can_be_disabled(self):
        self.invoke()
        self.assertTrue(self.run_booking.call_args.kwargs["allow_queue"])
        self.assertEqual(self.run_booking.call_args.kwargs["queue_policy"], "eager")
        self.run_booking.reset_mock()
        self.invoke(["--ei-jono"])
        self.assertFalse(self.run_booking.call_args.kwargs["allow_queue"])

    def test_step_mode_needs_a_terminal_before_opening_a_browser(self):
        with patch("sys.stdin.isatty", return_value=False):
            code, _, stderr = self.invoke(["--kysy-toiminnot"])
        self.assertEqual(code, 1)
        self.assertIn("päätteen", stderr)
        self.open_browser.assert_not_called()
        self.run_booking.assert_not_called()

    def test_automatic_mode_exposes_main_thread_controls_without_extra_approval(self):
        code, _, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        callbacks = self.run_booking.call_args.kwargs
        self.assertTrue(callable(callbacks["checkpoint"]))
        self.assertTrue(callable(callbacks["progress"]))
        self.assertIsNone(callbacks["before_action"])

    def test_step_mode_passes_action_approval_to_the_controller(self):
        with patch("sys.stdin.isatty", return_value=True), patch("uhkone.console.TerminalSession") as console:
            console.return_value.__enter__.return_value.ask_before_actions = True
            code, _, stderr = self.invoke(["--kysy-toiminnot"])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(callable(self.run_booking.call_args.kwargs["before_action"]))
        console.assert_called_once_with(ask_before_actions=True)

    def test_all_queues_are_reported_separately_from_a_paid_winner(self):
        self.run_booking.return_value = SimpleNamespace(status="confirmed", course_id=123,
            reservation_id="r123", receipt_id="receipt123",
            queue_reservations=((124, "q124", None), (125, "q125", 1)), queue_pending_ids=(126,))
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        self.assertIn("VARAUS VAHVISTETTU", stdout)
        self.assertIn("JONOPAIKKA VAHVISTETTU 2 kurssille: 124, 125", stdout)
        self.assertIn("126", stderr)
        self.assertNotIn("Testilapsi", stdout + stderr)

    def test_confirmed_queues_and_unknown_queue_returns_incomplete_outcome(self):
        self.run_booking.return_value = SimpleNamespace(status="queued", course_id=123,
            reservation_id=None, receipt_id=None, queue_reservations=((123, "q123", 1),),
            queue_pending_ids=(124,))
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("JONOPAIKKA VAHVISTETTU", stdout)
        self.assertIn("124", stderr)
        self.assertNotIn("VARAUS VAHVISTETTU", stdout)

    def test_queue_result_never_claims_a_paid_course_place(self):
        self.run_booking.return_value = SimpleNamespace(status="queued", course_id=123,
            reservation_id=None, receipt_id=None, queue_reservation_id="q123", queue_position=2)
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        self.assertIn("JONOPAIKKA VAHVISTETTU", stdout)
        self.assertIn("Oma jonosija: 2", stdout)
        self.assertNotIn("VARAUS VAHVISTETTU", stdout)
        self.assertNotIn("Kuitti:", stdout)

    def test_uncertain_queue_is_not_reported_as_a_confirmed_queue_place(self):
        self.run_booking.return_value = SimpleNamespace(status="queue_pending", course_id=123,
            reservation_id=None, receipt_id=None, queue_reservation_id=None, queue_position=None)
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 1)
        self.assertNotIn("JONOPAIKKA VAHVISTETTU", stdout)
        self.assertIn("voinut mennä perille", stderr)

    def test_earlier_override_does_not_override_published_opening(self):
        code, _, stderr = self.invoke(["--avautuu", "2099-01-11T07:00:00+02:00"])
        self.assertEqual(code, 1)
        self.assertIn("aikaisempi", stderr)
        self.open_browser.assert_not_called()

    def test_multiple_targets_are_ranked_and_share_one_coordinator(self):
        kallio = replace(self.course, id=124, location="Kallio")
        self.client.get_course.side_effect = [kallio, self.course]
        self.args = ["varaa-yksi", "124", "123", "124", "--lapsi", "Testilapsi",
                     "--enintaan", "76", "--maksutapa", "fastpay"]
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        self.assertEqual([call.args[0] for call in self.client.get_course.call_args_list], [124, 123])
        specs, pool, _ = self.run_booking.call_args.args
        self.assertEqual([spec.course.id for spec in specs], [123, 124])
        self.assertEqual(pool, self.open_pool.return_value.__enter__.return_value)
        self.open_browser.assert_not_called()
        self.assertIn("yksi kurssi 2", stdout)

    def test_weekday_too_early_rejected_before_any_browser(self):
        self.client.get_course.return_value = replace(self.course, start=datetime(2099, 2, 2, 17, tzinfo=cli.HELSINKI),
                                                     weekdays=(1,), time_start="17:29")
        code, _, stderr = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("kellonajat", stderr)
        self.open_browser.assert_not_called()
        self.open_pool.assert_not_called()

    def test_unconfirmed_result_is_not_reported_as_success(self):
        self.run_booking.return_value = SimpleNamespace(status="deadline", course_id=None, reservation_id=None, receipt_id=None)
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 1)
        self.assertNotIn("VARAUS VAHVISTETTU", stdout)
        self.assertIn("ei ole vahvistettu", stderr)


if __name__ == "__main__":
    unittest.main()
