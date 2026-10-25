import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from uhkone.models import Course
from uhkone.watch import WatchStateError, detect_changes, load_state, save_state


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state" / "vahti.json"
        self.course = Course(
            id=123,
            name="Leikki-ikäisten jatkouimakoulu",
            location="Mäkelänrinne",
            is_reservable=False,
            free_places=0,
        )
        self.available = replace(self.course, is_reservable=True, free_places=2)

    def test_first_baseline_records_existing_courses_without_announcing_them(self):
        empty = load_state(self.path)
        self.assertEqual(empty, {"version": 1, "courses": {}})
        events, state = detect_changes([self.course, replace(self.available, id=124)], empty, baseline=True)
        self.assertEqual(events, [])
        self.assertEqual(state["courses"], {"123": {"available": False}, "124": {"available": True}})
        self.assertEqual(empty, {"version": 1, "courses": {}})

    def test_only_new_publications_and_unavailable_to_available_transitions_notify(self):
        empty = load_state(self.path)
        events, state = detect_changes([self.course], empty)
        self.assertEqual(events, [("uusi", self.course)])
        events, state = detect_changes([self.course], state)
        self.assertEqual(events, [])
        events, state = detect_changes([self.available], state)
        self.assertEqual(events, [("vapautui", self.available)])
        events, state = detect_changes([replace(self.available, free_places=1)], state)
        self.assertEqual(events, [])
        events, state = detect_changes([self.course], state)
        self.assertEqual(events, [])
        events, state = detect_changes([self.available], state)
        self.assertEqual(events, [("vapautui", self.available)])
        other = replace(self.course, id=124, location="Kallio")
        events, state = detect_changes([self.available, other], state)
        self.assertEqual(events, [("uusi", other)])

    def test_temporary_empty_catalogue_and_restart_do_not_repeat_notifications(self):
        _, state = detect_changes([self.available], load_state(self.path))
        events, state = detect_changes([], state)
        self.assertEqual(events, [])
        self.assertTrue(state["courses"]["123"]["available"])
        save_state(self.path, state)
        restarted = load_state(self.path)
        self.assertEqual(restarted, state)
        events, state = detect_changes([self.available], restarted)
        self.assertEqual(events, [])
        events, state = detect_changes([replace(self.course, id=124)], state)
        self.assertEqual([kind for kind, _ in events], ["uusi"])
        self.assertIn("123", state["courses"])

    def test_failed_atomic_replacement_keeps_previous_state_and_cleans_temporary_file(self):
        _, old = detect_changes([self.course], load_state(self.path))
        save_state(self.path, old)
        _, updated = detect_changes([self.available], old)
        with patch("uhkone.watch.os.replace", side_effect=OSError("simulated disk failure")), self.assertRaises(WatchStateError):
            save_state(self.path, updated)
        self.assertEqual(load_state(self.path), old)
        self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), ["vahti.json"])

    def test_invalid_state_cannot_overwrite_previous_state(self):
        _, old = detect_changes([self.course], load_state(self.path))
        save_state(self.path, old)
        with self.assertRaises(WatchStateError):
            save_state(self.path, {"version": True, "courses": {}})
        self.assertEqual(load_state(self.path), old)

    def test_corrupted_state_is_rejected_instead_of_silently_reset(self):
        malformed = (
            "not json",
            '[]',
            'null',
            '{}',
            '{"version": true, "courses": {}}',
            '{"version": 1.0, "courses": {}}',
            '{"version": 2, "courses": {}}',
            '{"version": 1, "courses": []}',
            '{"version": 1, "courses": {}, "ignored": {}}',
            '{"version": 1, "courses": {"abc": {"available": false}}}',
            '{"version": 1, "courses": {"001": {"available": false}}}',
            '{"version": 1, "courses": {"-1": {"available": false}}}',
            '{"version": 1, "courses": {"123": {}}}',
            '{"version": 1, "courses": {"123": null}}',
            '{"version": 1, "courses": {"123": {"available": 1}}}',
            '{"version": 1, "courses": {"123": {"available": "true"}}}',
            '{"version": 1, "courses": {"123": {"available": false, "ignored": 1}}}',
            '{"version": 1, "courses": {}, "checked_at": false}',
            '{"version": 1, "courses": {}, "checked_at": "yesterday"}',
            '{"version": 1, "courses": {}, "checked_at": "2027-01-01T12:00:00"}',
            '{"version": 1, "version": 1, "courses": {}}',
            '{"version": 1, "courses": {"123": {"available": false, "available": true}}}',
            '{"version": 1, "courses": {"123": {"available": false}, "123": {"available": true}}}',
        )
        self.path.parent.mkdir()
        for raw in malformed:
            self.path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(WatchStateError):
                load_state(self.path)
            self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    def test_unreadable_state_file_reports_watch_error(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"version": 1, "courses": {}}), encoding="utf-8")
        with patch("uhkone.watch.Path.read_text", side_effect=PermissionError("denied")), self.assertRaises(WatchStateError):
            load_state(self.path)


if __name__ == "__main__":
    unittest.main()
