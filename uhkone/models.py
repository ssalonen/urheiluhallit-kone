"""Course data and conservative, Finnish-friendly search filters.

Missing values never count as evidence that a restrictive filter matches.
The catalogue adapter owns parsing source data; this module has no network
access and does not reserve places.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

DEFAULT_TARGET = "Leikki-ikäisten jatkouimakoulu"


def normalize_text(value: str) -> str:
    """Fold Finnish accents, case, punctuation and repeated whitespace."""
    folded = unicodedata.normalize("NFKD", value.casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w]+", " ", folded).split())


def normalize_location(value: str) -> str:
    """Normalize an exact hall name, accepting common Mäkelänrinne aliases."""
    normalized = normalize_text(value)
    aliases = {
        "makela": "makelanrinne",
        "makelan": "makelanrinne",
        "makelanrinteen uimahalli": "makelanrinne",
        "makelanrinne uimahalli": "makelanrinne",
        "kallion uimahalli": "kallio",
        "kallio uimahalli": "kallio",
    }
    return aliases.get(normalized, normalized)


def _is_continuation_course(name: str) -> bool:
    normalized = normalize_text(name)
    compact = normalized.replace(" ", "")
    preschool = "leikkiikai" in compact
    continuation = any(word.startswith("jatko") for word in normalized.split())
    excluded = any(
        word.startswith(("aikuis", "alkeis", "nuorten"))
        for word in normalized.split()
    )
    return preschool and continuation and not excluded


def _is_default_target(target: str) -> bool:
    # All these phrasings describe the requested course family. Match the
    # family, since the shop can vary word order and append age/level labels.
    return _is_continuation_course(target)


def _parse_time(value: str | None) -> time | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Course:
    id: int
    name: str
    location: str
    start: datetime | None = None
    end: datetime | None = None
    weekdays: tuple[int, ...] = ()
    time_start: str = ""
    time_end: str = ""
    price: Decimal | None = None
    free_places: int | None = None
    is_reservable: bool = False
    registration_start: datetime | None = None
    url: str = ""
    description: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    queue_places: int | None = None
    is_queueable: bool = False

    @property
    def available(self) -> bool:
        """True only when the shop confirms booking and a positive place count."""
        return (
            self.is_reservable is True
            and self.free_places is not None
            and self.free_places > 0
        )

    @property
    def queue_available(self) -> bool:
        """True for an open waiting list with a known positive slot count.

        Waiting-list capacity never establishes an available course place.
        The catalogue adapter maps the shop's registration gate separately
        so callers cannot accidentally treat a queue entry as a reservation.
        """
        return (
            self.is_queueable is True
            and type(self.queue_places) is int
            and self.queue_places > 0
        )


def _meeting_start_times(course: Course) -> tuple[tuple[int, time], ...] | None:
    """Read every declared meeting; incomplete schedules cannot prove a match.

    The catalogue's weekday numbers are zero based, whereas Course uses ISO
    weekdays. Keep per-entry times because a course may meet on weekdays and
    weekends at different times.
    """
    schedules = course.raw.get("weekdays")
    if schedules is not None and schedules != []:
        if not isinstance(schedules, list):
            return None
        meetings: list[tuple[int, time]] = []
        for entry in schedules:
            if not isinstance(entry, dict):
                return None
            days = entry.get("weekdays")
            if not isinstance(days, list) or not days or any(
                type(day) is not int or day not in range(7) for day in days
            ):
                return None
            times = entry.get("times")
            match = re.fullmatch(
                r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})", times
            ) if isinstance(times, str) else None
            if match is None:
                return None
            start_time, end_time = _parse_time(match[1]), _parse_time(match[2])
            if start_time is None or end_time is None:
                return None
            meetings.extend((day + 1, start_time) for day in days)
        # Disagreement between the normalized and original schedules leaves
        # at least one meeting unverified.
        if course.weekdays and set(course.weekdays) != {day for day, _ in meetings}:
            return None
        return tuple(meetings) or None

    days = course.weekdays
    if not days and course.start is not None:
        days = (course.start.isoweekday(),)
    if not days or any(type(day) is not int or day not in range(1, 8) for day in days):
        return None
    start_time = _parse_time(course.time_start)
    if start_time is None:
        if course.time_start or course.start is None:
            return None
        start_time = course.start.time().replace(tzinfo=None)
    return tuple((day, start_time) for day in days)


@dataclass(frozen=True)
class SearchFilters:
    target: str = DEFAULT_TARGET
    locations: tuple[str, ...] = ()
    weekdays: tuple[int, ...] = ()
    earliest_time: str | None = None
    latest_time: str | None = None
    weekday_earliest_time: str | None = None
    weekend_earliest_time: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    available_only: bool = False
    max_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not normalize_text(self.target):
            raise ValueError("Kurssin hakunimi ei saa olla tyhjä.")
        if any(type(day) is not int or day not in range(1, 8) for day in self.weekdays):
            raise ValueError("Viikonpäivien on oltava kokonaislukuja 1–7 (ma–su).")
        for label, value in (
            ("Aikaisin aloitusaika", self.earliest_time),
            ("Myöhäisin aloitusaika", self.latest_time),
            ("Arkipäivän aikaisin aloitusaika", self.weekday_earliest_time),
            ("Viikonlopun aikaisin aloitusaika", self.weekend_earliest_time),
        ):
            if value is not None and _parse_time(value) is None:
                raise ValueError(f"{label}: käytä kellonaikaa HH:MM.")
        if (
            self.earliest_time is not None
            and self.latest_time is not None
            and self.earliest_time > self.latest_time
        ):
            raise ValueError("Aikaisin aloitusaika ei saa olla myöhäisimmän jälkeen.")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("Alkupäivä ei saa olla loppupäivän jälkeen.")
        if self.max_price is not None and (
            not self.max_price.is_finite() or self.max_price < 0
        ):
            raise ValueError("Enimmäishinnan on oltava vähintään nolla.")

    def matches(self, course: Course) -> bool:
        """Return whether a course satisfies every requested restriction.

        Weekdays are allowed attendance days: every known meeting day must
        fit. Date limits apply to the course's first date, and time limits
        apply to its start time, inclusively.
        """
        if _is_default_target(self.target):
            if not _is_continuation_course(course.name):
                return False
        elif normalize_text(self.target) not in normalize_text(course.name):
            return False

        if self.locations and normalize_location(course.location) not in {
            normalize_location(location) for location in self.locations
        }:
            return False

        if self.weekdays:
            meeting_days = course.weekdays
            if not meeting_days and course.start is not None:
                meeting_days = (course.start.isoweekday(),)
            if not meeting_days or not set(meeting_days).issubset(self.weekdays):
                return False

        if self.date_from is not None or self.date_to is not None:
            if course.start is None:
                return False
            first_date = course.start.date()
            if self.date_from is not None and first_date < self.date_from:
                return False
            if self.date_to is not None and first_date > self.date_to:
                return False

        if self.earliest_time is not None or self.latest_time is not None:
            start_time = _parse_time(course.time_start)
            if start_time is None:
                return False
            earliest_time = _parse_time(self.earliest_time)
            latest_time = _parse_time(self.latest_time)
            if earliest_time is not None and start_time < earliest_time:
                return False
            if latest_time is not None and start_time > latest_time:
                return False

        if self.weekday_earliest_time is not None or self.weekend_earliest_time is not None:
            meetings = _meeting_start_times(course)
            if meetings is None:
                return False
            for day, start_time in meetings:
                earliest = self.weekday_earliest_time if day <= 5 else self.weekend_earliest_time
                earliest_time = _parse_time(earliest)
                if earliest_time is not None and start_time < earliest_time:
                    return False

        if self.available_only and not course.available:
            return False
        return not (self.max_price is not None and (
            course.price is None
            or not course.price.is_finite()
            or course.price > self.max_price
        ))


def filter_courses(courses: Iterable[Course], filters: SearchFilters) -> list[Course]:
    """Keep catalogue order; hall preference ranking is the caller's job."""
    return [course for course in courses if filters.matches(course)]
