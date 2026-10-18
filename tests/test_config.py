from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from uhkone.config import ConfigError, Settings, load_settings, state_directory


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "asetukset.toml"

    def load_text(self, text):
        self.path.write_text(text, encoding="utf-8")
        return load_settings(self.path)

    def test_defaults_preserve_user_hall_preferences_and_schedule(self):
        settings = self.load_text("")
        self.assertEqual(settings.filters.locations, ("Mäkelänrinne", "Kallio"))
        self.assertEqual(settings.filters.target, "Leikki-ikäisten jatkouimakoulu")
        self.assertEqual(settings.filters.weekdays, ())
        self.assertIsNone(settings.filters.earliest_time)
        self.assertIsNone(settings.filters.latest_time)
        self.assertEqual(settings.filters.weekday_earliest_time, "17:30")
        self.assertEqual(settings.filters.weekend_earliest_time, "09:30")
        self.assertEqual(Settings().filters.weekday_earliest_time, "17:30")
        self.assertEqual(Settings().filters.weekend_earliest_time, "09:30")
        self.assertIsNone(settings.filters.date_from)
        self.assertIsNone(settings.filters.date_to)
        self.assertEqual(settings.interval, 300)
        self.assertTrue(settings.queue_fallback)

    def test_queue_fallback_can_be_disabled_explicitly(self):
        self.assertFalse(self.load_text('[varaus]\njono_varavaihtoehtona = false\n').queue_fallback)

    def test_missing_optional_default_file_uses_defaults(self):
        with patch("uhkone.config.Path.is_file", return_value=False):
            self.assertEqual(load_settings().filters.locations, ("Mäkelänrinne", "Kallio"))

    def test_explicit_missing_file_is_an_error(self):
        with self.assertRaises(ConfigError):
            load_settings(self.path)

    def test_complete_preferences_with_both_toml_and_string_dates(self):
        settings = self.load_text('''
[haku]
kurssi = "Leikki-ikäisten jatkokurssi"
hallit = ["Kallio", "Mäkelänrinne"]
viikonpaivat = [1, 3, 7]
aikaisintaan = "15:30"
viimeistaan = "19:45"
arki_aikaisintaan = "16:45"
viikonloppu_aikaisintaan = "10:00"
alkaen = 2027-01-01
asti = "2027-04-30"
enintaan_euroa = 145.50
[vahti]
vali_sekuntia = 60
''')
        self.assertEqual(settings.filters.locations, ("Kallio", "Mäkelänrinne"))
        self.assertEqual(settings.filters.weekdays, (1, 3, 7))
        self.assertEqual(settings.filters.earliest_time, "15:30")
        self.assertEqual(settings.filters.latest_time, "19:45")
        self.assertEqual(settings.filters.weekday_earliest_time, "16:45")
        self.assertEqual(settings.filters.weekend_earliest_time, "10:00")
        self.assertEqual(settings.filters.date_from, date(2027, 1, 1))
        self.assertEqual(settings.filters.date_to, date(2027, 4, 30))
        self.assertEqual(settings.filters.max_price, Decimal("145.50"))
        self.assertEqual(settings.interval, 60)

    def test_empty_halls_and_days_allow_any_location_and_day_with_time_limits(self):
        settings = self.load_text('[haku]\nhallit = []\nviikonpaivat = []\nenintaan_euroa = 0\n')
        self.assertEqual(settings.filters.locations, ())
        self.assertEqual(settings.filters.weekdays, ())
        self.assertEqual(settings.filters.weekday_earliest_time, "17:30")
        self.assertEqual(settings.filters.weekend_earliest_time, "09:30")
        self.assertEqual(settings.filters.max_price, Decimal("0"))

    def test_invalid_preferences_report_config_errors(self):
        cases = (
            "[unknown]\nvalue = 1",
            "[haku]\nhallti = []",
            "[vahti]\ninterval = 60",
            'haku = "wrong"',
            "vahti = 1",
            "varaus = 1",
            '[varaus]\njono_varavaihtoehtona = "true"',
            '[varaus]\njono_varavaihtoehtona = 1',
            '[haku]\nkurssi = " "',
            '[haku]\nkurssi = "!!!"',
            '[haku]\nhallit = "Kallio"',
            '[haku]\nhallit = ["Kallio", 123]',
            '[haku]\nhallit = [""]',
            '[haku]\nviikonpaivat = [true]',
            '[haku]\nviikonpaivat = [0]',
            '[haku]\nviikonpaivat = [8]',
            '[haku]\naikaisintaan = "9:00"',
            '[haku]\naikaisintaan = "24:00"',
            '[haku]\nviimeistaan = "16:99"',
            '[haku]\narki_aikaisintaan = "7:30"',
            '[haku]\narki_aikaisintaan = 1730',
            '[haku]\narki_aikaisintaan = "17h30"',
            '[haku]\nviikonloppu_aikaisintaan = "09:3"',
            '[haku]\nviikonloppu_aikaisintaan = "25:00"',
            '[haku]\nviikonloppu_aikaisintaan = "09:60"',
            '[haku]\naikaisintaan = "17:00"\nviimeistaan = "16:00"',
            '[haku]\nalkaen = "2027-02-30"',
            '[haku]\nalkaen = 123',
            '[haku]\nalkaen = 2027-01-01T12:00:00Z',
            '[haku]\nalkaen = "2027-02-01"\nasti = "2027-01-01"',
            '[haku]\nenintaan_euroa = -1',
            '[haku]\nenintaan_euroa = nan',
            '[haku]\nenintaan_euroa = inf',
            '[haku]\nenintaan_euroa = true',
            '[vahti]\nvali_sekuntia = 59',
            '[vahti]\nvali_sekuntia = true',
            '[vahti]\nvali_sekuntia = 300.0',
            '[haku\n',
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.load_text(text)

    def test_state_directory_respects_xdg_without_reading_other_environment(self):
        with patch.dict("os.environ", {"XDG_STATE_HOME": self.temporary.name}):
            self.assertEqual(state_directory(), Path(self.temporary.name) / "uhkone")
        with patch.dict("os.environ", {"XDG_STATE_HOME": ""}), patch(
            "uhkone.config.Path.home", return_value=Path("/example/home")
        ):
            self.assertEqual(state_directory(), Path("/example/home/.local/state/uhkone"))


if __name__ == "__main__":
    unittest.main()
