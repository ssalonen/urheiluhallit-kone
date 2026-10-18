"""Small public catalogue snapshot for the local simulator; no customer data."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .client import BASE_URL, CATALOGUE_PATH, ShopClient, parse_course


DEFAULT_SEED = Path(__file__).parent / "data" / "courses.json"
SOURCE_URL = BASE_URL + CATALOGUE_PATH + "?service_id=9"
COURSE_FIELDS = frozenset({
    "reservation_event_group_id", "name", "is_course", "service_id", "location", "location_id",
    "location_address", "reservation_group_id", "reservation_group_name", "start", "end",
    "capacity", "free", "queue_capacity", "queue", "free_queue", "is_reservable",
    "public_reservation_start", "weekdays", "event_number",
    "fare_products", "price", "bundle_price",
})
PRODUCT_FIELDS = frozenset({"fare_product_id", "name", "price", "bundle_price", "original_price"})
WEEKDAY_FIELDS = frozenset({"days", "times", "weekdays"})


def sanitize_courses(rows: list[dict]) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("Julkinen kurssidata puuttuu.")
    output, seen = [], set()
    for row in rows:
        course = parse_course(row)
        if course.id in seen:
            raise ValueError("Kurssidatassa on sama tunnus kahdesti.")
        seen.add(course.id)
        sanitized = {key: row[key] for key in COURSE_FIELDS if key in row}
        products = row.get("fare_products")
        if isinstance(products, list):
            sanitized["fare_products"] = [
                {key: product[key] for key in PRODUCT_FIELDS if key in product}
                for product in products if isinstance(product, dict)
            ]
        if isinstance(row.get("weekdays"), list):
            sanitized["weekdays"] = [
                {key: entry[key] for key in WEEKDAY_FIELDS if key in entry}
                for entry in row["weekdays"] if isinstance(entry, dict)
            ]
        # Round-trip removes arbitrary Python objects and normalizes JSON-only data.
        output.append(json.loads(json.dumps(sanitized, ensure_ascii=False)))
    return output


def write_seed(path: Path, rows: list[dict], *, fetched_at: datetime | None = None) -> dict:
    if fetched_at is not None and (fetched_at.tzinfo is None or fetched_at.utcoffset() is None):
        raise ValueError("Testidatan tallennusajassa on oltava aikavyöhyke.")
    rows = sanitize_courses(rows)
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    document = {
        "version": 1, "source_url": SOURCE_URL,
        "fetched_at": (fetched_at or datetime.now(timezone.utc)).isoformat(),
        "data_sha256": hashlib.sha256(encoded).hexdigest(), "courses": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return document


def load_seed(path: Path | None = None) -> dict:
    path = path or DEFAULT_SEED
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(document, dict) or type(document.get("version")) is not int
                or document["version"] != 1 or document.get("source_url") != SOURCE_URL):
            raise ValueError("Tuntematon testidatan muoto tai lähde.")
        captured = datetime.fromisoformat(document["fetched_at"])
        if captured.tzinfo is None:
            raise ValueError("Testidatan ajankohta on aikavyöhykkeetön.")
        sanitized = sanitize_courses(document["courses"])
        if sanitized != document["courses"]:
            raise ValueError("Testidata sisältää odottamattomia kenttiä.")
        encoded = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(encoded).hexdigest() != document.get("data_sha256"):
            raise ValueError("Testidatan tarkistussumma ei täsmää.")
        return document
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Testidataa ei voi lukea: {path}: {exc}") from exc


def update_seed(path: Path = DEFAULT_SEED) -> dict:
    # Fixed public GET endpoint through the read-only adapter. No login or cart.
    with ShopClient() as shop:
        rows = [course.raw for course in shop.courses()]
    return write_seed(path, rows)
