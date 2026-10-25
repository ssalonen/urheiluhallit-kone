"""Exercise command integration without network access or opening a browser."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from uhkone import cli
from uhkone.config import Settings
from uhkone.models import Course, SearchFilters


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.filters = SearchFilters(locations=("Mäkelänrinne", "Kallio"))
        self.settings = Settings(self.filters, interval=60)
        self.course = Course(
            id=123,
            name="Leikki-ikäisten jatkouimakoulu",
            location="Mäkelänrinne",
            start=datetime(2099, 1, 12, 16, 30, tzinfo=cli.HELSINKI),
            weekdays=(1,),
            time_start="16:30",
            time_end="17:00",
            price=Decimal("145.50"),
            is_reservable=True,
            free_places=2,
            url="https://urheiluhallit.actorsmartbook.fi/product/123",
            raw={"private": {"child_name": "DO NOT OUTPUT", "access_token": "SECRET TOKEN"}},
        )

        self.client = MagicMock()
        self.client.__enter__.return_value = self.client
        self.client.__exit__.return_value = False
        self.client.courses.return_value = [self.course]
        self.client.get_course.return_value = self.course
        client_module = SimpleNamespace(__name__="uhkone.client")
        client_module.ShopClient = Mock(return_value=self.client)
        client_module.ClientError = type("ClientError", (Exception,), {})
        self.browser = SimpleNamespace(__name__="uhkone.browser")
        self.browser.prepare_course = Mock()
        self.browser.login = Mock()
        self.browser.BrowserError = type("BrowserError", (Exception,), {})
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {"uhkone.client": client_module, "uhkone.browser": self.browser}).start()
        self.load_settings = patch("uhkone.cli.load_settings", return_value=self.settings).start()
        patch("uhkone.cli.state_directory", return_value=self.directory).start()

    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_hall_priority_precedes_date_and_each_hall_is_chronological(self):
        kallio = replace(self.course, id=124, location="Kallio", start=datetime(2099, 1, 1))
        earlier = replace(self.course, id=125, start=datetime(2099, 1, 11))
        unrelated = replace(self.course, id=126, name="Aikuisten jatkouimakoulu")
        outside_halls = replace(self.course, id=127, location="Vuosaari")
        courses = [kallio, self.course, unrelated, outside_halls, earlier]
        self.assertEqual([c.id for c in cli.ranked(courses, self.filters)], [125, 123, 124])
        self.assertEqual(courses[0], kallio)

    def test_duplicate_hall_aliases_preserve_first_preference(self):
        filters = replace(self.filters, locations=("Mäkelä", "Kallio", "Mäkelänrinne"))
        kallio = replace(self.course, id=124, location="Kallio")
        self.assertEqual([c.id for c in cli.ranked([kallio, self.course], filters)], [123, 124])

    def test_json_search_omits_raw_private_data_and_serializes_public_types(self):
        result, stdout, stderr = self.run_cli(["haku", "--json"])
        self.assertEqual(result, 0, stderr)
        document = json.loads(stdout)
        record = document["courses"][0]
        self.assertNotIn("raw", record)
        self.assertNotIn("DO NOT OUTPUT", stdout)
        self.assertNotIn("SECRET TOKEN", stdout)
        self.assertTrue(record["available"])
        self.assertEqual(record["price"], "145.50")
        assert self.course.start is not None
        self.assertEqual(record["start"], self.course.start.isoformat())
        self.browser.prepare_course.assert_not_called()

    def test_wrong_course_family_is_refused_before_browser(self):
        self.client.get_course.return_value = replace(self.course, name="Aikuisten jatkouimakoulu")
        result, _, stderr = self.run_cli(["ilmoittaudu", "123"])
        self.assertEqual(result, 1)
        self.assertIn("hakuehtoja", stderr)
        self.assertEqual(self.client.get_course.call_count, 1)
        self.browser.prepare_course.assert_not_called()

    def test_stale_availability_is_refetched_and_refused_before_browser(self):
        self.client.get_course.side_effect = [self.course, replace(self.course, free_places=0)]
        result, _, stderr = self.run_cli(["ilmoittaudu", "123"])
        self.assertEqual(result, 1)
        self.assertIn("ei ole nyt varattavissa", stderr)
        self.assertEqual(self.client.get_course.call_args_list, [((123,),), ((123,),)])
        self.browser.prepare_course.assert_not_called()

    def test_changed_course_family_on_refetch_is_also_refused(self):
        self.client.get_course.side_effect = [self.course, replace(self.course, name="Leikki-ikäisten alkeisuimakoulu")]
        result, _, stderr = self.run_cli(["ilmoittaudu", "123"])
        self.assertEqual(result, 1)
        self.assertIn("ei enää vastaa hakuehtoja", stderr)
        self.browser.prepare_course.assert_not_called()

    def test_selected_course_is_refetched_then_passed_to_browser_with_child_choice(self):
        refreshed = replace(self.course, free_places=1)
        self.client.get_course.side_effect = [self.course, refreshed]
        result, _, stderr = self.run_cli(["ilmoittaudu", "123", "--lapsi", "Testilapsi"])
        self.assertEqual(result, 0, stderr)
        self.assertEqual(self.client.get_course.call_count, 2)
        self.browser.prepare_course.assert_called_once_with(
            refreshed,
            self.directory / "selain",
            child_name="Testilapsi",
            executable_path=None,
        )

    def test_details_lookup_is_independent_of_expired_search_date_preferences(self):
        self.load_settings.return_value = Settings(
            replace(self.filters, date_from=date(2020, 1, 1), date_to=date(2020, 1, 31))
        )
        self.client.get_course.return_value = replace(self.course, start=datetime(2020, 1, 12))
        result, stdout, stderr = self.run_cli(["kurssi", "123"])
        self.assertEqual(result, 0, stderr)
        self.assertIn("12.01.2020", stdout)
        self.client.courses.assert_not_called()

    def test_watch_available_only_does_not_hide_intermediate_full_state(self):
        # Each invocation reloads persisted state, just as separate --kerran
        # runs would. Empty-response preservation must not hide a real change.
        self.client.courses.side_effect = [
            [self.course],
            [replace(self.course, free_places=0)],
            [self.course],
        ]
        args = ["vahti", "--kerran", "--vapaat"]
        result, first, stderr = self.run_cli(args)
        self.assertEqual(result, 0, stderr)
        self.assertIn("Lähtötilanne", first)
        result, _, stderr = self.run_cli(args)
        self.assertEqual(result, 0, stderr)
        result, reopened, stderr = self.run_cli(args)
        self.assertEqual(result, 0, stderr)
        self.assertIn("PAIKKA AVAUTUNUT", reopened)
        self.browser.prepare_course.assert_not_called()


if __name__ == "__main__":
    unittest.main()
