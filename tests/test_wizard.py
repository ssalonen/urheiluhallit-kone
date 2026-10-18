"""Scripted conversations never open a real shop, browser, or payment flow."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
import unittest

from uhkone.cli import parser
from uhkone.config import ConfigError, Settings
from uhkone.models import Course
from uhkone.wizard import run_wizard


BASE_COURSE = Course(
    id=42, name="Leikki-ikäisten jatkouimakoulu", location="Mäkelänrinne",
    start=datetime(2030, 1, 7, 18, tzinfo=timezone.utc),
    end=datetime(2030, 3, 7, 18, 30, tzinfo=timezone.utc), weekdays=(1,),
    time_start="18:00", time_end="18:30", price=Decimal("76"), free_places=0,
    registration_start=datetime(2029, 12, 12, 7, tzinfo=timezone.utc),
    url="https://kauppa.urheiluhallit.fi/webshop/reservations2/reservations/9/1,2/6/-/42#",
)


class Client:
    def __init__(self, courses):
        self.courses_by_id = {course.id: course for course in courses}
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def courses(self):
        self.calls.append("courses")
        return list(self.courses_by_id.values())

    def get_course(self, course_id):
        self.calls.append(course_id)
        return self.courses_by_id[course_id]


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.args = parser().parse_args(["ohjattu"])
        self.settings = Settings()
        self.directory = Path("/tmp/uhkone-wizard-test")
        self.output = StringIO()
        self.client = Client([BASE_COURSE])
        self.runs = []
        self.clients_opened = 0

    def run_conversation(self, answers, **options):
        answers = iter(answers)

        def read():
            try:
                return next(answers)
            except StopIteration as exc:
                raise EOFError from exc

        def client_factory():
            self.clients_opened += 1
            return self.client

        def reservation(parsed, settings, directory):
            self.assertTrue(self.client.closed)
            self.runs.append(("reservation", parsed, settings, directory))
            return 7

        def demo(parsed, settings, directory):
            self.runs.append(("demo", parsed, settings, directory))
            return 8

        def login(parsed, directory):
            self.runs.append(("login", parsed, directory))

        return run_wizard(
            self.args, self.settings, self.directory,
            parse_args=parser().parse_args, run_reservation=reservation,
            run_test_shop=demo, run_login=login, client_factory=client_factory,
            input_fn=read, output=self.output, interactive=options.pop("interactive", True),
            **options,
        )

    def booking_answers(self, *, selection="42", final="aloita", queue="", mode=""):
        # Menu, approved targets, child, budget, payment, duration, queue, mode,
        # final explicit start. Published registration time needs no question.
        return ["2", selection, "Testiosallistuja", "", "", "", queue, mode, final]

    def test_noninteractive_mode_does_not_read_or_open_anything(self):
        self.assertEqual(self.run_conversation([], interactive=False), 2)
        self.assertEqual(self.clients_opened, 0)
        self.assertFalse(self.runs)
        self.assertIn("vuorovaikutteisen päätteen", self.output.getvalue())

    def test_blank_main_menu_or_exit_starts_nothing(self):
        for answers in ([""], ["0"], []):
            with self.subTest(answers=answers):
                self.assertEqual(self.run_conversation(answers), 0)
        self.assertFalse(self.runs)
        self.assertEqual(self.clients_opened, 0)

    def test_reviewed_booking_uses_fastpay_and_step_approvals_by_default(self):
        self.assertEqual(self.run_conversation(self.booking_answers()), 7)
        kind, parsed, settings, directory = self.runs[0]
        self.assertEqual(kind, "reservation")
        self.assertEqual(parsed.command, "varaa-yksi")
        self.assertEqual(parsed.kohteet, [42])
        self.assertEqual(parsed.lapsi, "Testiosallistuja")
        self.assertEqual(parsed.enintaan, Decimal("76"))
        self.assertEqual(parsed.maksutapa, "fastpay")
        self.assertTrue(parsed.jono)
        self.assertTrue(parsed.keskustele)
        self.assertTrue(parsed.kysy_toiminnot)
        self.assertEqual(parsed.kesto, 30)
        self.assertIs(settings, self.settings)
        self.assertEqual(directory, self.directory)
        self.assertNotIn("Testiosallistuja", self.output.getvalue())
        self.assertIn("Tavoite: yksi varsinainen", self.output.getvalue())
        self.assertIn("FastPay voi käyttää", self.output.getvalue())
        self.assertIn("varmista maksutapa näkyvässä selaimessa", self.output.getvalue())
        self.assertIn("Jonopaikat: kaikille hyväksymillesi sopiville kursseille heti jonojen auetessa", self.output.getvalue())
        self.assertIn("varsinaisen paikan haku jatkuu", self.output.getvalue())
        self.assertNotIn("Jonopaikka: yksi", self.output.getvalue())

    def test_blank_or_unrecognized_final_confirmation_never_starts_booking(self):
        for final in ("", "kyllä", "joo", "start"):
            with self.subTest(final=final):
                self.assertEqual(self.run_conversation(self.booking_answers(final=final)), 0)
        self.assertFalse(self.runs)

    def test_eof_at_participant_question_cancels(self):
        self.assertEqual(self.run_conversation(["2", "42"]), 0)
        self.assertFalse(self.runs)
        self.assertIn("Peruttu", self.output.getvalue())

    def test_pasted_shop_url_with_comma_in_hall_selector_is_one_target(self):
        self.assertEqual(self.run_conversation(self.booking_answers(selection=BASE_COURSE.url)), 7)
        self.assertEqual(self.client.calls, [42])
        self.assertEqual(self.runs[0][1].kohteet, [42])

    def test_wrong_course_level_is_rejected_before_child_or_payment(self):
        self.client = Client([replace(BASE_COURSE, name="Leikki-ikäisten alkeisuimakoulu")])
        with self.assertRaises(ConfigError):
            self.run_conversation(["2", "42"])
        self.assertFalse(self.runs)
        self.assertNotIn("Lapsen tarkka nimi", self.output.getvalue())

    def test_too_early_weekday_course_is_rejected(self):
        self.client = Client([replace(BASE_COURSE, time_start="17:00")])
        with self.assertRaises(ConfigError):
            self.run_conversation(["2", "42"])
        self.assertFalse(self.runs)

    def test_search_ranks_halls_and_requires_approved_course_identifiers(self):
        self.client = Client([replace(BASE_COURSE, id=43, location="Kallio"), BASE_COURSE])
        answers = ["1", "999", "kaikki", "Testiosallistuja", "", "", "", "e", "1", "aloita"]
        self.assertEqual(self.run_conversation(answers), 7)
        self.assertEqual(self.client.calls, ["courses"])
        parsed = self.runs[0][1]
        self.assertEqual(parsed.kohteet, [42, 43])
        self.assertFalse(parsed.jono)
        self.assertFalse(parsed.kysy_toiminnot)
        self.assertIn("Valitse 1–12 yllä näkyvää", self.output.getvalue())

    def test_search_without_matching_courses_never_asks_personal_questions(self):
        self.client = Client([])
        self.assertEqual(self.run_conversation(["1"]), 0)
        self.assertFalse(self.runs)
        self.assertNotIn("Lapsen tarkka nimi", self.output.getvalue())

    def test_missing_price_is_rejected_before_run(self):
        self.client = Client([replace(BASE_COURSE, price=None)])
        with self.assertRaises(ConfigError):
            self.run_conversation(["2", "42", "Testiosallistuja"])
        self.assertFalse(self.runs)

    def test_invalid_or_insufficient_budget_reprompts_then_accepts_exact_limit(self):
        answers = ["2", "42", "Testiosallistuja", "xyz", "70", "76,00", "", "", "", "", "aloita"]
        self.assertEqual(self.run_conversation(answers), 7)
        self.assertEqual(self.runs[0][1].enintaan, Decimal("76.00"))
        self.assertIn("Raja alittaa", self.output.getvalue())

    def test_missing_opening_requires_explicit_time_and_preserves_timezone(self):
        self.client = Client([replace(BASE_COURSE, registration_start=None)])
        answers = ["2", "42", "Testiosallistuja", "", "", "2029-12-12T09:00", "", "", "", "aloita"]
        self.assertEqual(self.run_conversation(answers), 7)
        opening = self.runs[0][1].avautuu
        self.assertEqual(opening.hour, 9)
        self.assertIsNotNone(opening.utcoffset())

    def test_blank_missing_opening_cancels(self):
        self.client = Client([replace(BASE_COURSE, registration_start=None)])
        self.assertEqual(self.run_conversation(["2", "42", "Testiosallistuja", "", "", ""]), 0)
        self.assertFalse(self.runs)

    def test_bad_target_is_not_fetched_and_can_be_corrected(self):
        answers = self.booking_answers()
        answers.insert(1, "https://other.example/42")
        self.assertEqual(self.run_conversation(answers), 7)
        self.assertEqual(self.client.calls, [42])

    def test_local_demo_has_fake_fastpay_and_visible_browser(self):
        self.assertEqual(self.run_conversation(["3", ""]), 8)
        kind, parsed, *_ = self.runs[0]
        self.assertEqual(kind, "demo")
        self.assertEqual(parsed.command, "koeajo")
        self.assertEqual(parsed.monta, 3)
        self.assertEqual(parsed.maksutapa, "fastpay")
        self.assertEqual(parsed.skenaario, "normaali")
        self.assertTrue(parsed.keskustele)
        self.assertFalse(parsed.headless)
        self.assertEqual(self.clients_opened, 0)

    def test_lost_queue_demo_enables_queue_without_any_real_client(self):
        self.assertEqual(self.run_conversation(["3", "3"]), 8)
        self.assertEqual(self.runs[0][1].skenaario, "jono-vastaus-katoaa")
        self.assertTrue(self.runs[0][1].jono)
        self.assertEqual(self.clients_opened, 0)

    def test_login_only_opens_existing_browser_profile(self):
        self.args.selain = "/usr/bin/chromium"
        self.assertEqual(self.run_conversation(["4"]), 0)
        kind, parsed, directory = self.runs[0]
        self.assertEqual(kind, "login")
        self.assertEqual(parsed.command, "kirjaudu")
        self.assertEqual(parsed.selain, "/usr/bin/chromium")
        self.assertEqual(directory, self.directory)
        self.assertEqual(self.clients_opened, 0)

    def test_config_browser_and_search_overrides_survive_argument_assembly(self):
        self.args = parser().parse_args([
            "ohjattu", "--asetukset", "preferences.toml", "--selain", "/usr/bin/chromium",
            "--halli", "Mäkelänrinne", "--arki-aikaisintaan", "18:00",
        ])
        self.assertEqual(self.run_conversation(self.booking_answers()), 7)
        parsed = self.runs[0][1]
        self.assertEqual(parsed.asetukset, Path("preferences.toml"))
        self.assertEqual(parsed.selain, "/usr/bin/chromium")
        self.assertEqual(parsed.halli, ["Mäkelänrinne"])
        self.assertEqual(parsed.arki_aikaisintaan, "18:00")
        self.assertEqual(parsed.tilahakemisto, self.directory)


if __name__ == "__main__":
    unittest.main()
