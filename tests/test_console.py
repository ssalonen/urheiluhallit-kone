"""Terminal controls exercise main-thread waits without a real browser/stdin."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
import unittest

from uhkone.console import StopRequested, TerminalSession
from uhkone.models import Course


PUBLIC_COURSE = Course(
    id=42, name="Leikki-ikäisten jatkouimakoulu", location="Mäkelänrinne",
    start=datetime(2030, 1, 5, 10, tzinfo=timezone.utc), time_start="10:00",
    price=Decimal("76"), description="PRIVATE_DESCRIPTION",
    raw={"account_name": "PRIVATE_ACCOUNT", "card": "PRIVATE_CARD"},
)


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def wait(self, seconds):
        self.value += seconds


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.output = StringIO()
        self.clock = Clock()

    def session(self, **options):
        return TerminalSession(
            stream=self.output, input_stream=StringIO(), start_reader=False,
            clock=self.clock.now,
            wall_clock=lambda: datetime(2030, 1, 1, 12, 34, 56, tzinfo=timezone.utc),
            **options,
        )

    def test_status_contains_timestamp_and_only_public_course_fields(self):
        session = self.session()
        session.event("candidate", PUBLIC_COURSE)
        output = self.output.getvalue()
        self.assertTrue(output.startswith("[12:34:56]"))
        for expected in ("Kurssi #42", PUBLIC_COURSE.name, "Mäkelänrinne", "10:00", "76.00 €"):
            self.assertIn(expected, output)
        for private in ("PRIVATE_DESCRIPTION", "PRIVATE_ACCOUNT", "PRIVATE_CARD"):
            self.assertNotIn(private, output)

    def test_status_repeats_only_after_heartbeat_or_change(self):
        session = self.session()
        session.event("waiting", PUBLIC_COURSE)
        self.clock.value = 9.9
        session.event("waiting", PUBLIC_COURSE)
        session.checkpoint(self.clock.wait)
        self.assertEqual(len(self.output.getvalue().splitlines()), 1)
        self.clock.value = 10.0
        session.event("heartbeat")
        self.assertEqual(len(self.output.getvalue().splitlines()), 2)
        session.event("held", PUBLIC_COURSE)
        self.assertEqual(len(self.output.getvalue().splitlines()), 3)

    def test_changed_course_outputs_status_immediately(self):
        session = self.session()
        session.event("candidate", PUBLIC_COURSE)
        session.event("candidate", replace(PUBLIC_COURSE, id=43))
        self.assertEqual(len(self.output.getvalue().splitlines()), 2)

    def test_pending_request_messages_do_not_claim_confirmed_reservations(self):
        session = self.session()
        session.event("reservation_pending", PUBLIC_COURSE)
        session.event("queue_phase", PUBLIC_COURSE)
        session.event("queue_pending", PUBLIC_COURSE)
        output = self.output.getvalue()
        self.assertIn("Selvitän varauspyynnön tulosta", output)
        self.assertIn("Tarkistan sopivat jonot", output)
        self.assertIn("muiden kurssien haku jatkuu", output)
        self.assertNotIn("varaus löytyi", output)
        self.assertNotIn("paikkaa ei saatu", output)

    def test_unknown_phase_and_command_are_never_echoed(self):
        session = self.session()
        session.event("PRIVATE_ACCOUNT")
        session.submit_command("PRIVATE_CARD")
        session.checkpoint(self.clock.wait)
        output = self.output.getvalue()
        self.assertNotIn("PRIVATE_ACCOUNT", output)
        self.assertNotIn("PRIVATE_CARD", output)
        self.assertIn("Tuntematon komento", output)

    def test_course_text_cannot_inject_terminal_control_or_extra_lines(self):
        session = self.session()
        session.event("candidate", replace(PUBLIC_COURSE, name="nimi\n\x1b[31m\rtesti"))
        self.assertEqual(len(self.output.getvalue().splitlines()), 1)
        self.assertNotIn("\x1b", self.output.getvalue())

    def test_noninteractive_session_never_starts_reader_or_prompts(self):
        session = self.session()
        with session:
            session.before_action("checkout", PUBLIC_COURSE, wait=self.clock.wait)
            self.assertIsNone(session._reader)
        self.assertNotIn("Hyväksytkö", self.output.getvalue())
        self.assertNotIn("Ohjaus + Enter", self.output.getvalue())

    def test_asking_without_terminal_fails_before_any_action(self):
        with self.assertRaises(ValueError):
            self.session(ask_before_actions=True)

    def test_pause_pumps_browser_events_until_resume(self):
        session = self.session(interactive=True)
        calls = []
        session.submit_command("tauko")

        def wait(seconds):
            calls.append(seconds)
            self.clock.wait(seconds)
            session.submit_command("jatka")

        session.checkpoint(wait)
        self.assertEqual(calls, [0.2])
        self.assertFalse(session.paused)
        self.assertIn("Tauolla", self.output.getvalue())

    def test_pause_heartbeats_continue_while_waiting(self):
        session = self.session(interactive=True, heartbeat_seconds=1)
        session.submit_command("p")

        def wait(seconds):
            self.clock.wait(seconds)
            if self.clock.value > 2.5:
                session.submit_command("j")

        session.checkpoint(wait)
        self.assertGreaterEqual(self.output.getvalue().count("Tauolla"), 3)

    def test_stop_during_pause_prevents_action_and_stays_stopped(self):
        session = self.session(interactive=True)
        session.submit_command("p")

        def wait(seconds):
            session.submit_command("q")

        with self.assertRaises(StopRequested):
            session.before_action("add", PUBLIC_COURSE, wait=wait)
        with self.assertRaises(StopRequested):
            session.checkpoint(self.clock.wait)
        self.assertNotIn("Yritän varata kurssipaikan", self.output.getvalue())

    def test_explicit_status_and_help_are_available_during_run(self):
        session = self.session(interactive=True)
        session.event("payment_pending", PUBLIC_COURSE)
        session.submit_command("t")
        session.submit_command("?")
        session.checkpoint(self.clock.wait)
        output = self.output.getvalue()
        self.assertEqual(output.count("Maksua ei lähetetä uudelleen"), 2)
        self.assertIn("Ohjaus + Enter", output)

    def test_approval_is_read_from_queue_while_main_thread_pumps_browser(self):
        session = self.session(interactive=True, ask_before_actions=True)
        calls = []

        def wait(seconds):
            calls.append(seconds)
            session.submit_command("kyllä")

        session.before_action("checkout", PUBLIC_COURSE, wait=wait)
        self.assertEqual(calls, [0.2])
        self.assertIn("jatka varauksen maksuun", self.output.getvalue())
        self.assertIn("Toiminto hyväksytty", self.output.getvalue())

    def test_declining_an_action_stops_without_authorizing_it(self):
        session = self.session(interactive=True, ask_before_actions=True)

        def wait(seconds):
            session.submit_command("ei")

        with self.assertRaises(StopRequested):
            session.before_action("queue", PUBLIC_COURSE, wait=wait)
        self.assertIn("Toimintoa ei hyväksytty", self.output.getvalue())
        self.assertNotIn("Toiminto hyväksytty", self.output.getvalue())

    def test_approval_given_while_paused_cannot_authorize_write(self):
        session = self.session(interactive=True, ask_before_actions=True)
        responses = iter(("tauko\nkyllä", "jatka", "kyllä"))
        calls = []

        def wait(seconds):
            calls.append(seconds)
            for line in next(responses).splitlines():
                session.submit_command(line)

        session.before_action("add", PUBLIC_COURSE, wait=wait)
        self.assertEqual(calls, [0.2, 0.2, 0.2])
        self.assertIn("Kirjoita ensin jatka", self.output.getvalue())

    def test_eof_disables_control_once_without_spinning_during_automatic_run(self):
        session = self.session(interactive=True)
        session.finish_input()
        session.finish_input()
        session.checkpoint(self.clock.wait)
        session.checkpoint(self.clock.wait)
        self.assertFalse(session.input_open)
        self.assertEqual(self.clock.value, 0)
        self.assertEqual(self.output.getvalue().count("Päätteen syöte sulkeutui"), 1)

    def test_eof_on_pause_stops_without_infinite_wait(self):
        session = self.session(interactive=True)
        session.submit_command("p")
        session.finish_input()
        with self.assertRaises(StopRequested):
            session.checkpoint(self.clock.wait)
        self.assertEqual(self.clock.value, 0)

    def test_eof_at_approval_never_authorizes_action(self):
        session = self.session(interactive=True, ask_before_actions=True)

        def wait(seconds):
            session.finish_input()

        with self.assertRaises(StopRequested):
            session.before_action("checkout", PUBLIC_COURSE, wait=wait)
        self.assertNotIn("Toiminto hyväksytty", self.output.getvalue())

    def test_real_reader_stops_at_eof_and_does_not_close_input(self):
        source = StringIO("tila\n")
        session = TerminalSession(stream=self.output, input_stream=source, interactive=True)
        with session:
            session._reader.join(timeout=1)
            self.assertFalse(session._reader.is_alive())
            session.checkpoint(self.clock.wait)
            self.assertFalse(session.input_open)
        self.assertFalse(source.closed)

    def test_interval_must_be_finite_and_positive(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.session(heartbeat_seconds=value)


if __name__ == "__main__":
    unittest.main()
