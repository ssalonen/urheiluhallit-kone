"""Non-personal preferences, deliberately separate from the browser profile."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import tomllib

from .models import SearchFilters


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    filters: SearchFilters = field(default_factory=lambda: SearchFilters(
        locations=("Mäkelänrinne", "Kallio"),
        weekday_earliest_time="17:30", weekend_earliest_time="09:30",
    ))
    interval: int = 300
    queue_fallback: bool = True


def state_directory() -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    return (Path(root).expanduser() if root else Path.home() / ".local" / "state") / "uhkone"


def load_settings(path: Path | None = None) -> Settings:
    if path is None:
        candidate = Path("asetukset.toml")
        if not candidate.is_file():
            return Settings()
        path = candidate
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Asetustiedostoa ei voi lukea: {path}: {exc}") from exc
    if set(data) - {"haku", "vahti", "varaus"}:
        raise ConfigError("Tuntematon asetusosio. Sallitut osiot: haku, vahti, varaus.")
    search = data.get("haku", {})
    watch = data.get("vahti", {})
    booking = data.get("varaus", {})
    if not all(isinstance(value, dict) for value in (search, watch, booking)):
        raise ConfigError("haku, vahti ja varaus ovat TOML-taulukoita.")
    allowed = {
        "kurssi", "hallit", "viikonpaivat", "aikaisintaan", "viimeistaan",
        "arki_aikaisintaan", "viikonloppu_aikaisintaan", "alkaen", "asti", "enintaan_euroa",
    }
    if set(search) - allowed or set(watch) - {"vali_sekuntia"} or set(booking) - {"jono_varavaihtoehtona"}:
        raise ConfigError("Tuntematon asetus. Tarkista asetukset.toml-esimerkki.")
    try:
        target = search.get("kurssi", "Leikki-ikäisten jatkouimakoulu")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("kurssi ei saa olla tyhjä")
        halls = search.get("hallit", ["Mäkelänrinne", "Kallio"])
        if not isinstance(halls, list) or any(not isinstance(h, str) or not h.strip() for h in halls):
            raise ValueError("hallit on lista hallien nimistä")
        days = search.get("viikonpaivat", [])
        if not isinstance(days, list) or any(type(d) is not int or not 1 <= d <= 7 for d in days):
            raise ValueError("viikonpaivat on lista numeroista 1–7")
        earliest = search.get("aikaisintaan")
        latest = search.get("viimeistaan")
        weekday_earliest = search.get("arki_aikaisintaan", "17:30")
        weekend_earliest = search.get("viikonloppu_aikaisintaan", "09:30")
        for value in (earliest, latest, weekday_earliest, weekend_earliest):
            if value is not None:
                if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
                    raise ValueError("kellonaika annetaan muodossa HH:MM")
                time.fromisoformat(value)
        if earliest and latest and earliest > latest:
            raise ValueError("aikaisintaan ei voi olla viimeistaan-arvoa myöhemmin")
        def parse_date(key: str) -> date | None:
            value = search.get(key)
            if value is None:
                return None
            if type(value) is date:
                return value
            return date.fromisoformat(value)
        start, end = parse_date("alkaen"), parse_date("asti")
        if start and end and start > end:
            raise ValueError("alkaen on asti-päivän jälkeen")
        budget = Decimal(str(search["enintaan_euroa"])) if "enintaan_euroa" in search else None
        if budget is not None and (not budget.is_finite() or budget < 0):
            raise ValueError("enintaan_euroa on vähintään nolla")
        interval = watch.get("vali_sekuntia", 300)
        if type(interval) is not int or interval < 60:
            raise ValueError("vali_sekuntia on kokonaisluku, vähintään 60")
        queue_fallback = booking.get("jono_varavaihtoehtona", True)
        if type(queue_fallback) is not bool:
            raise ValueError("jono_varavaihtoehtona on true tai false")
        return Settings(SearchFilters(target=target, locations=tuple(halls), weekdays=tuple(days),
                                     earliest_time=earliest, latest_time=latest,
                                     weekday_earliest_time=weekday_earliest,
                                     weekend_earliest_time=weekend_earliest, date_from=start,
                                     date_to=end, max_price=budget), interval, queue_fallback)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ConfigError(f"Virheellinen asetus: {exc}") from exc
