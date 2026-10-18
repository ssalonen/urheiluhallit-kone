"""A local, restartable publication/availability monitor. No booking operations."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from .models import Course


class WatchStateError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Toistuva avain: {key}")
        result[key] = value
    return result


def _validate_state(state: object) -> None:
    if (
        not isinstance(state, dict)
        or type(state.get("version")) is not int
        or state["version"] != 1
        or not isinstance(state.get("courses"), dict)
        or set(state) - {"version", "courses", "checked_at"}
    ):
        raise ValueError("Tuntematon tiedostomuoto")
    if "checked_at" in state:
        timestamp = state["checked_at"]
        if not isinstance(timestamp, str):
            raise ValueError("Virheellinen tarkistusaika")
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Tarkistusajasta puuttuu aikavyöhyke")
    for key, entry in state["courses"].items():
        if (
            not isinstance(key, str)
            or not key.isascii()
            or not key.isdecimal()
            or str(int(key)) != key
            or not isinstance(entry, dict)
            or set(entry) != {"available"}
            or type(entry["available"]) is not bool
        ):
            raise ValueError("Virheellinen kurssitila")


def load_state(path: Path) -> dict:
    try:
        if not path.exists():
            return {"version": 1, "courses": {}}
        state = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        _validate_state(state)
        return state
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise WatchStateError(f"Vahdin tilatiedostoa ei voi lukea: {path}: {exc}") from exc


def detect_changes(courses: list[Course], state: dict, *, baseline: bool = False) -> tuple[list[tuple[str, Course]], dict]:
    """Preserve unseen entries so temporary empty responses do not cause repeats."""
    previous = state.get("courses", {})
    entries = dict(previous)
    events = []
    for course in courses:
        key = str(course.id)
        old = previous.get(key)
        if not baseline:
            if old is None:
                events.append(("uusi", course))
            elif course.available and not old["available"]:
                events.append(("vapautui", course))
        entries[key] = {"available": course.available}
    return events, {"version": 1, "checked_at": datetime.now(timezone.utc).isoformat(), "courses": entries}


def save_state(path: Path, state: dict) -> None:
    file_name = None
    try:
        _validate_state(state)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".vahti-", delete=False) as file:
                file_name = file.name
                json.dump(state, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(file_name, path)
        finally:
            if file_name is not None:
                try:
                    os.unlink(file_name)
                except FileNotFoundError:
                    pass
    except (OSError, ValueError, TypeError) as exc:
        raise WatchStateError(f"Vahdin tilatiedostoa ei voi tallentaa: {path}: {exc}") from exc
