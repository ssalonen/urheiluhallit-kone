#!/usr/bin/env python3
"""Real Chromium smoke test against the loopback simulator only.

Uses bundled public course facts and fictional participants/money. Two queue
entries are verified (including a lost response), then the third course opens.
Exactly one FastPay purchase must complete, and resuming may not repeat it.
No catalogue update, external merchant request, or real card operation is used.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

# Also support running this directly from an uninstalled source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uhkone.booking import BookingError, BookingSpec
from uhkone.cli import ranked
from uhkone.client import HELSINKI, ShopClient, parse_course
from uhkone.config import Settings
from uhkone.multi import run_race
from uhkone.multi_browser import open_booking_pool
from uhkone.origin import is_local_test
from uhkone.seed import load_seed
from uhkone.testshop import TestShop


def _course_rows() -> list[dict]:
    rows = load_seed()["courses"]
    today = datetime.now(HELSINKI).date()
    filters = replace(Settings().filters, date_from=today, available_only=False)
    eligible = ranked([parse_course(row) for row in rows], filters)[:12]
    if len(eligible) < 3:
        raise BookingError("Pakatusta julkisesta testidatasta ei löytynyt kolmea nykyisiin hakuehtoihin sopivaa tulevaa kurssia.")
    by_id = {row["reservation_event_group_id"]: row for row in rows}
    return [by_id[course.id] for course in eligible[:3]]


def _assert_counts(stats: dict, winner: int, expected_cents: int) -> None:
    expected = {
        "queue_posts": 2, "queued": 2, "cart_posts": 1, "checkout_posts": 1,
        "confirmed": 1, "receipts": 1, "fake_card_charges": 1,
        "balance_cents": 100000, "fastpay_total_cents": expected_cents, "cart_items": 0,
    }
    mismatches = {key: {"expected": value, "actual": stats.get(key)}
                  for key, value in expected.items() if stats.get(key) != value}
    paid_courses = [row["reservation_event_group_id"] for row in stats["reservation_rows"]
                    if row["is_paid"] == 1]
    if paid_courses != [winner]:
        mismatches["paid_courses"] = {"expected": [winner], "actual": paid_courses}
    if mismatches:
        raise AssertionError("Testin tapahtumamäärät eivät täsmää: " + json.dumps(mismatches, ensure_ascii=False))


def run_smoke(executable_path: str | None = None) -> dict:
    rows = _course_rows()
    course_ids = [row["reservation_event_group_id"] for row in rows]
    target = course_ids[2]
    verified_queues: set[int] = set()
    released = False
    with TestShop(rows, scenario="jono-vastaus-katoaa", opens_in=0) as shop, TemporaryDirectory(prefix="uhkone-smoke-") as tmp:
        if not is_local_test(shop.base_url):
            raise AssertionError("Smoke-testi saa käyttää vain paikallista testikauppaa.")
        with ShopClient(base_url=shop.base_url) as client:
            courses = [client.get_course(course_id) for course_id in course_ids]
        deadline = datetime.now(timezone.utc) + timedelta(seconds=90)
        specs = [BookingSpec(course=course, child_name="Testilapsi", max_price=course.price,
                             opens_at=shop.opens_at, deadline=deadline,
                             payment_mode="fastpay", payment_type_id=5) for course in courses]

        def progress(phase: str, metadata: dict) -> None:
            nonlocal released
            if phase != "queued":
                return
            verified_queues.add(metadata["course_id"])
            if len(verified_queues) == 2 and not released:
                if target in verified_queues:
                    raise AssertionError("Kolmas vaihtoehto jonotettiin ennen kahden ensimmäisen jonon vahvistamista.")
                # This changes simulated stock only. The real browser still has
                # to discover it through the same merchant read endpoints.
                with shop._lock:
                    shop._db.execute("UPDATE courses SET capacity=1 WHERE id=?", (target,))
                released = True

        state_dir = Path(tmp) / "tila"
        with open_booking_pool(specs, Path(tmp) / "selain", executable_path=executable_path,
                               base_url=shop.base_url, headless=True) as pool:
            first = run_race(specs, pool, state_dir, allow_queue=True, queue_policy="eager",
                             queue_timeout=20, progress=progress, log=lambda message: None)
            first_stats = shop.stats()
            if first.status != "confirmed" or first.course_id != target or not released:
                raise AssertionError("Jonojen jälkeen avattu kolmas kurssi ei vahvistunut: "
                                     + json.dumps({"status": first.status, "course_id": first.course_id,
                                                   "released": released, "server": first_stats}, ensure_ascii=False))
            expected_cents = int(courses[2].price * 100)
            _assert_counts(first_stats, target, expected_cents)
            resumed = run_race(specs, pool, state_dir, allow_queue=True, queue_policy="eager",
                               queue_timeout=20, progress=progress, log=lambda message: None)
            resumed_stats = shop.stats()
            _assert_counts(resumed_stats, target, expected_cents)
            if resumed.status != "confirmed" or resumed.course_id != target:
                raise AssertionError("Tallennettu varaus ei vahvistunut jatkettaessa.")
            if any(resumed_stats[key] != first_stats[key] for key in ("queue_posts", "cart_posts", "checkout_posts", "writes")):
                raise AssertionError("Jatkaminen lähetti ylimääräisen varaus-, jono- tai maksupyynnön.")
        return {"test_only": True, "real_payment": False, "outcome": first.status,
                "candidates": len(course_ids), "queued": resumed_stats["queued"],
                "queue_posts": resumed_stats["queue_posts"], "cart_posts": resumed_stats["cart_posts"],
                "checkout_posts": resumed_stats["checkout_posts"], "confirmed": resumed_stats["confirmed"],
                "receipts": resumed_stats["receipts"], "fake_card_charges": resumed_stats["fake_card_charges"],
                "balance_unchanged": resumed_stats["balance_cents"] == 100000,
                "winner_is_third_candidate": first.course_id == target,
                "resume_without_new_writes": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selain", help="Chromen tai Chromiumin ohjelmapolku")
    args = parser.parse_args(argv)
    executable = args.selain or shutil.which("chromium") or shutil.which("chromium-browser")
    try:
        result = run_smoke(executable)
    except (BookingError, OSError, AssertionError) as exc:
        print(f"Paikallinen Chromium-smoke epäonnistui: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
