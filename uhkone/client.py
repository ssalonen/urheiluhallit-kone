"""Read-only adapter for Urheiluhallit's public Enkora course catalogue."""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Any, Self
from zoneinfo import ZoneInfo

import httpx

from .models import Course
from .origin import PRODUCTION_URL, is_local_test, validate_base_url

BASE_URL = PRODUCTION_URL
CATALOGUE_PATH = "/webshop/reservations2/apieventgroups"
HELSINKI = ZoneInfo("Europe/Helsinki")


class ClientError(Exception):
    """An unavailable or unrecognized public shop response."""


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<\s*(?:br\s*/?|/p|/div)\s*>", "\n", value, flags=re.IGNORECASE)
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())


def _integer(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def _datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=HELSINKI) if parsed.tzinfo is None else parsed.astimezone(HELSINKI)


def _price(row: dict[str, Any]) -> Decimal | None:
    products = row.get("fare_products")
    # The frontend prioritizes bundle_price. For ordinary courses, product
    # prices are integer cents. Several alternatives do not establish one price.
    if row.get("bundle_price") is not None:
        values = [row["bundle_price"]]
    elif row.get("price") is not None:
        values = [row["price"]]
    elif isinstance(products, list) and products:
        if any(not isinstance(product, dict) for product in products):
            return None
        values = [product.get("price") for product in products]
    else:
        return None
    prices: set[Decimal] = set()
    for value in values:
        if isinstance(value, bool) or value is None:
            return None
        try:
            price = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not price.is_finite() or price < 0:
            return None
        prices.add(price / Decimal(100))
    return next(iter(prices)) if len(prices) == 1 else None


def _schedule(row: dict[str, Any], start: datetime | None, end: datetime | None) -> tuple[tuple[int, ...], str, str]:
    days: set[int] = set()
    times: set[tuple[str, str]] = set()
    schedules = row.get("weekdays")
    if isinstance(schedules, list):
        for entry in schedules:
            if not isinstance(entry, dict):
                continue
            raw_days = entry.get("weekdays")
            if isinstance(raw_days, list):
                for day in raw_days:
                    if type(day) is int and 0 <= day <= 6:
                        days.add(day + 1)
            raw_time = entry.get("times")
            if isinstance(raw_time, str):
                match = re.fullmatch(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})", raw_time)
                if match:
                    try:
                        datetime.strptime(match[1], "%H:%M")
                        datetime.strptime(match[2], "%H:%M")
                        times.add((match[1], match[2]))
                    except ValueError:
                        pass
    if not days and start:
        days.add(start.isoweekday())
    if len(times) == 1:
        time_start, time_end = next(iter(times))
    elif not times:
        time_start = start.strftime("%H:%M") if start else ""
        time_end = end.strftime("%H:%M") if end else ""
    else:
        # A single time filter cannot safely describe a varying schedule.
        time_start = time_end = ""
    return tuple(sorted(days)), time_start, time_end


def parse_course(row: Any, *, base_url: str = BASE_URL) -> Course:
    base_url = validate_base_url(base_url)
    if not isinstance(row, dict):
        raise ClientError("Kurssirajapinnan tietomuoto muuttui: kurssi ei ole tietue.")
    course_id = _integer(row.get("reservation_event_group_id"))
    name, location = _text(row.get("name")), _text(row.get("location"))
    if course_id is None or course_id <= 0 or not name or not location:
        raise ClientError("Kurssirajapinnan tietomuoto muuttui: kurssin tunnus, nimi tai halli puuttuu.")
    start, end = _datetime(row.get("start")), _datetime(row.get("end"))
    weekdays, time_start, time_end = _schedule(row, start, end)
    free = _integer(row.get("free"))
    free_queue = _integer(row.get("free_queue"))
    reservable = row.get("is_reservable") is True or (
        type(row.get("is_reservable")) is int and row["is_reservable"] == 1
    )
    location_id = _integer(row.get("location_id"))
    group_id = _integer(row.get("reservation_group_id"))
    service_id = _integer(row.get("service_id")) or 9
    url = (
        f"{base_url}/webshop/reservations2/reservations/{service_id}/"
        f"{location_id or '-'}/{group_id or '-'}/-/{course_id}"
    )
    return Course(
        id=course_id, name=name, location=location,
        start=start, end=end, weekdays=weekdays,
        time_start=time_start, time_end=time_end,
        price=_price(row), free_places=free if free is not None and free >= 0 else None,
        is_reservable=reservable,
        registration_start=_datetime(row.get("public_reservation_start")),
        url=url, description=_text(row.get("description")), raw=dict(row),
        queue_places=free_queue if free_queue is not None and free_queue >= 0 else None,
        # R2.EventGroup.isQueuable() uses the same is_reservable gate as
        # ordinary booking, followed by free_queue > 0. No separate queue
        # enable flag is present in the observed course API.
        is_queueable=reservable,
    )


class ShopClient:
    """Fetch public data using GET requests only, without a login or cart."""

    def __init__(self, *, client: httpx.Client | None = None, timeout: float = 25.0, base_url: str = BASE_URL):
        self.base_url = validate_base_url(base_url)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url, timeout=timeout, follow_redirects=not is_local_test(self.base_url),
            trust_env=not is_local_test(self.base_url),
            headers={"Accept": "application/json", "User-Agent": "uhkone/0.3 (course catalogue)"},
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _get(self, path: str, **params: Any) -> Any:
        try:
            response = self._client.get(
                f"{self.base_url}{path}", params=params or None,
                follow_redirects=False if is_local_test(self.base_url) else httpx.USE_CLIENT_DEFAULT,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ClientError(f"Kurssikauppa vastasi virheellä HTTP {exc.response.status_code}.") from exc
        except httpx.RequestError as exc:
            raise ClientError("Kurssikauppaan ei saada yhteyttä. Tarkista verkkoyhteys ja yritä uudelleen.") from exc
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ClientError("Kurssikauppa ei palauttanut odotettua JSON-tietoa.") from exc

    def courses(self) -> list[Course]:
        data = self._get(CATALOGUE_PATH, service_id=9)
        if not isinstance(data, list):
            raise ClientError("Kurssirajapinnan tietomuoto muuttui: odotettiin kurssiluetteloa.")
        courses = [parse_course(row, base_url=self.base_url) for row in data]
        if len({course.id for course in courses}) != len(courses):
            raise ClientError("Kurssirajapinta palautti saman kurssitunnuksen useita kertoja.")
        return courses

    def get_course(self, course_id: int) -> Course:
        if type(course_id) is not int or course_id <= 0:
            raise ClientError("Kurssitunnuksen on oltava positiivinen kokonaisluku.")
        course = parse_course(self._get(f"{CATALOGUE_PATH}/{course_id}"), base_url=self.base_url)
        if course.id != course_id:
            raise ClientError("Kurssikauppa palautti eri kurssin kuin pyydettiin. Ilmoittautuminen keskeytettiin.")
        return course
