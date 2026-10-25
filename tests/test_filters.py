"""Search correctness matters before any course can be selected for booking."""

import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from uhkone.models import Course, SearchFilters, filter_courses, normalize_text


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.course = Course(
            id=123,
            name="Leikki-ikäisten jatkouimakoulu (5–6 v)",
            location="Mäkelänrinne",
            start=datetime(2027, 1, 11, 16, 30),
            end=datetime(2027, 3, 15, 17),
            weekdays=(1,),
            time_start="16:30",
            time_end="17:00",
            price=Decimal("145.00"),
            free_places=2,
            is_reservable=True,
        )

    def test_intended_family_and_finnish_title_variants(self):
        for title in (
            "Leikki-ikäisten jatkouimakoulu",
            "LEIKKI-IKÄISTEN JATKOUIMAKOULU 5–6 v",
            "Uimakoulu leikki-ikäisille, jatkokurssi",
            "Leikkiikäisten uimakoulu – jatko",
        ):
            with self.subTest(title=title):
                self.assertTrue(SearchFilters().matches(replace(self.course, name=title)))

    def test_other_course_families_do_not_match(self):
        for title in (
            "Aikuisten jatkouimakoulu",
            "Leikki-ikäisten alkeisuimakoulu",
            "Leikki-ikäisten alkeistekniikka",
            "Kouluikäisten jatkouimakoulu",
            "Leikki-ikäisten alkeistekniikka ja jatko",
        ):
            with self.subTest(title=title):
                self.assertFalse(SearchFilters().matches(replace(self.course, name=title)))

    def test_halls_are_exact_after_normalization_and_aliases(self):
        for hall in ("MÄKELÄNRINNE", "makelanrinne", " Mäkelän ", "Mäkelä"):
            with self.subTest(hall=hall):
                self.assertTrue(SearchFilters(locations=(hall,)).matches(self.course))
        self.assertFalse(SearchFilters(locations=("Kallio",)).matches(self.course))
        self.assertFalse(SearchFilters(locations=("Mäkelänrinne 2",)).matches(self.course))

    def test_time_and_date_boundaries_are_inclusive(self):
        filters = SearchFilters(
            date_from=date(2027, 1, 11),
            date_to=date(2027, 1, 11),
            earliest_time="16:30",
            latest_time="16:30",
        )
        self.assertTrue(filters.matches(self.course))
        self.assertFalse(SearchFilters(earliest_time="16:31").matches(self.course))
        self.assertFalse(SearchFilters(latest_time="16:29").matches(self.course))
        self.assertFalse(SearchFilters(date_from=date(2027, 1, 12)).matches(self.course))
        self.assertFalse(SearchFilters(date_to=date(2027, 1, 10)).matches(self.course))

    def test_unknown_restricted_fields_do_not_match(self):
        unknown = replace(self.course, start=None, weekdays=(), time_start="", price=None)
        self.assertTrue(SearchFilters().matches(unknown))
        for filters in (
            SearchFilters(date_from=date(2027, 1, 1)),
            SearchFilters(date_to=date(2027, 3, 1)),
            SearchFilters(weekdays=(1,)),
            SearchFilters(earliest_time="00:00"),
            SearchFilters(latest_time="23:59"),
            SearchFilters(max_price=Decimal(200)),
        ):
            with self.subTest(filters=filters):
                self.assertFalse(filters.matches(unknown))
        for value in ("banana", "25:00", "16:30:00"):
            self.assertFalse(
                SearchFilters(earliest_time="00:00").matches(
                    replace(self.course, time_start=value)
                )
            )

    def test_allowed_weekdays_cover_all_meetings(self):
        self.assertTrue(SearchFilters(weekdays=(1, 3)).matches(self.course))
        self.assertFalse(SearchFilters(weekdays=(2, 3)).matches(self.course))
        two_days = replace(self.course, weekdays=(1, 4))
        self.assertFalse(SearchFilters(weekdays=(1, 3)).matches(two_days))
        self.assertTrue(SearchFilters(weekdays=(1, 4)).matches(two_days))
        self.assertTrue(SearchFilters(weekdays=(1,)).matches(replace(self.course, weekdays=())))

    def test_generic_filter_does_not_impose_user_schedule(self):
        filters = SearchFilters()
        self.assertIsNone(filters.weekday_earliest_time)
        self.assertIsNone(filters.weekend_earliest_time)
        self.assertTrue(filters.matches(self.course))

    def test_weekday_and_weekend_start_boundaries_are_inclusive(self):
        filters = SearchFilters(weekday_earliest_time="17:30", weekend_earliest_time="09:30")
        for day in range(1, 8):
            boundary, too_early = ("17:30", "17:29") if day <= 5 else ("09:30", "09:29")
            with self.subTest(day=day):
                self.assertTrue(filters.matches(replace(self.course, weekdays=(day,), time_start=boundary)))
                self.assertFalse(filters.matches(replace(self.course, weekdays=(day,), time_start=too_early)))

    def test_day_specific_limits_apply_to_every_meeting(self):
        filters = SearchFilters(weekday_earliest_time="17:30", weekend_earliest_time="09:30")
        mixed = replace(self.course, weekdays=(1, 6), time_start="09:30")
        self.assertFalse(filters.matches(mixed))
        self.assertTrue(filters.matches(replace(mixed, time_start="17:30")))
        schedule = [
            {"weekdays": [0, 2], "times": "17:30-18:00"},
            {"weekdays": [5, 6], "times": "09:30-10:00"},
        ]
        varied = replace(self.course, weekdays=(1, 3, 6, 7), time_start="", raw={"weekdays": schedule})
        self.assertTrue(filters.matches(varied))
        for index, too_early in ((0, "17:29-18:00"), (1, "09:29-10:00")):
            bad_schedule = [dict(entry) for entry in schedule]
            bad_schedule[index]["times"] = too_early
            with self.subTest(index=index):
                self.assertFalse(filters.matches(replace(varied, raw={"weekdays": bad_schedule})))

    def test_day_specific_limits_can_use_known_first_start_as_fallback(self):
        filters = SearchFilters(weekday_earliest_time="17:30", weekend_earliest_time="09:30")
        monday = replace(self.course, weekdays=(), start=datetime(2027, 1, 11, 17, 30), time_start="")
        self.assertTrue(filters.matches(monday))
        self.assertFalse(filters.matches(replace(monday, start=datetime(2027, 1, 11, 17, 29))))
        saturday = replace(monday, start=datetime(2027, 1, 9, 9, 30))
        self.assertTrue(filters.matches(saturday))
        self.assertFalse(filters.matches(replace(saturday, start=datetime(2027, 1, 9, 9, 29))))

    def test_day_specific_limits_reject_unknown_or_inconsistent_schedule(self):
        filters = SearchFilters(weekday_earliest_time="17:30", weekend_earliest_time="09:30")
        unknown = replace(self.course, start=None, weekdays=(), time_start="")
        for course in (
            unknown,
            replace(unknown, weekdays=(1,)),
            replace(unknown, time_start="17:30"),
            replace(self.course, time_start="bad"),
            replace(self.course, weekdays=(0,), time_start="17:30"),
            replace(self.course, weekdays=(True,), time_start="17:30"),
        ):
            with self.subTest(course=course):
                self.assertFalse(filters.matches(course))
        good = {"weekdays": [0], "times": "17:30-18:00"}
        for schedule in (
            "unknown",
            [good, None],
            [good, {"weekdays": [5]}],
            [{"weekdays": [], "times": "17:30-18:00"}],
            [{"weekdays": [True], "times": "17:30-18:00"}],
            [{"weekdays": [7], "times": "17:30-18:00"}],
            [{"weekdays": [0], "times": "17:30-25:00"}],
            [{"weekdays": [5], "times": "09:30-10:00"}],
        ):
            with self.subTest(schedule=schedule):
                self.assertFalse(filters.matches(replace(self.course, raw={"weekdays": schedule})))

    def test_availability_requires_confirmed_booking_and_known_positive_places(self):
        self.assertTrue(self.course.available)
        for values in (
            {"free_places": None},
            {"free_places": 0},
            {"free_places": -1},
            {"is_reservable": False},
        ):
            unavailable = replace(self.course, **values)
            with self.subTest(values=values):
                self.assertFalse(unavailable.available)
                self.assertFalse(SearchFilters(available_only=True).matches(unavailable))
                self.assertTrue(SearchFilters().matches(unavailable))

    def test_queue_slots_do_not_satisfy_available_only_filter(self):
        queued = replace(self.course, free_places=0, queue_places=1, is_queueable=True)
        self.assertTrue(queued.queue_available)
        self.assertFalse(queued.available)
        self.assertFalse(SearchFilters(available_only=True).matches(queued))
        self.assertTrue(SearchFilters().matches(queued))

    def test_queue_availability_defaults_and_malformed_values_are_conservative(self):
        self.assertIsNone(self.course.queue_places)
        self.assertFalse(self.course.queue_available)
        for values in (
            {"queue_places": None, "is_queueable": True},
            {"queue_places": 0, "is_queueable": True},
            {"queue_places": -1, "is_queueable": True},
            {"queue_places": True, "is_queueable": True},
            {"queue_places": "1", "is_queueable": True},
            {"queue_places": 1.0, "is_queueable": True},
            {"queue_places": 1, "is_queueable": False},
            {"queue_places": 1, "is_queueable": "true"},
        ):
            with self.subTest(values=values):
                self.assertFalse(replace(self.course, **values).queue_available)

    def test_price_cap_and_order_preservation(self):
        self.assertTrue(SearchFilters(max_price=Decimal(145)).matches(self.course))
        self.assertFalse(SearchFilters(max_price=Decimal("144.99")).matches(self.course))
        self.assertFalse(
            SearchFilters(max_price=Decimal(145)).matches(
                replace(self.course, price=Decimal("NaN"))
            )
        )
        other = replace(self.course, id=124, location="Kallio")
        self.assertEqual(filter_courses([other, self.course], SearchFilters()), [other, self.course])

    def test_invalid_filters_raise(self):
        # Include deliberately mistyped values to exercise runtime validation.
        invalid_filters: tuple[dict[str, Any], ...] = (
            {"target": " "},
            {"weekdays": (0,)},
            {"weekdays": (8,)},
            {"weekdays": (True,)},
            {"earliest_time": "9:00"},
            {"latest_time": "24:00"},
            {"weekday_earliest_time": "17:3"},
            {"weekday_earliest_time": 1730},
            {"weekend_earliest_time": "9:30"},
            {"weekend_earliest_time": "24:00"},
            {"earliest_time": "17:00", "latest_time": "16:00"},
            {"date_from": date(2027, 2, 1), "date_to": date(2027, 1, 1)},
            {"max_price": Decimal(-1)},
            {"max_price": Decimal("NaN")},
        )
        for kwargs in invalid_filters:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SearchFilters(**kwargs)

    def test_custom_target_uses_normalized_phrase(self):
        course = replace(self.course, name="Aikuisten uimakoulu – alkeet")
        self.assertTrue(SearchFilters(target="AIKUISTEN uimakoulu").matches(course))
        self.assertEqual(normalize_text("  MÄKELÄNRINNE—KALLIO  "), "makelanrinne kallio")


if __name__ == "__main__":
    unittest.main()
