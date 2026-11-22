"""Local dashboard tests; never contact the production shop."""
import unittest
from types import SimpleNamespace

from uhkone.panel import PanelError, PanelState


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.panel = PanelState()

    def test_initial_defaults_are_ordered_and_private(self):
        state = self.panel.public()
        self.assertEqual(state["ids"], [110980, 110962, 110970])
        self.assertEqual(state["max_price"], "76")
        self.assertFalse(state["has_child"])
        self.assertNotIn("child", state)

    def test_settings_are_reordered_explicitly_without_search(self):
        self.panel.configure({"ids": [110970, 110980, 110962],
                              "max_price": "76,00", "child": "Testiosallistuja"})
        state = self.panel.public()
        self.assertEqual(state["ids"], [110970, 110980, 110962])
        self.assertEqual(state["max_price"], "76.00")
        self.assertTrue(state["has_child"])
        self.assertNotIn("Testiosallistuja", repr(state))

    def test_clearing_child_name_never_reuses_previous_selection(self):
        self.panel.configure({"ids": [110980], "max_price": "76", "child": "Vanha testilapsi"})
        self.panel.configure({"ids": [110962], "max_price": "76", "child": ""})
        self.assertFalse(self.panel.public()["has_child"])
        self.assertEqual(self.panel.child, "")

    def test_rejects_bad_or_duplicated_course_ids(self):
        for ids in ([], [1, 1], [0], [-1], ["1"], [True], list(range(13))):
            with self.subTest(ids=ids), self.assertRaises(PanelError):
                self.panel.configure({"ids": ids, "max_price": "76", "child": "Testi"})

    def test_rejects_bad_prices(self):
        for price in ("nan", "inf", "-1", "76.001", "10001", "", 76):
            with self.subTest(price=price), self.assertRaises(PanelError):
                self.panel.configure({"ids": [42], "max_price": price, "child": "Testi"})

    def test_no_settings_changes_during_booking(self):
        self.panel.running = True
        with self.assertRaises(PanelError):
            self.panel.configure({"ids": [42], "max_price": "76", "child": "Testi"})
        self.assertEqual(self.panel.ids, [110980, 110962, 110970])

    def test_course_progress_only_exposes_public_metadata(self):
        self.panel.course_info([SimpleNamespace(
            id=110980, location="Mäkelänrinne", time_start="17:50",
            registration_start=None)])
        self.panel.progress("waiting", {"course_id": 110980,
                                         "opens_at": "2026-11-24T07:00:00+02:00"})
        self.panel.progress("candidate", {"course_id": 110980, "available": True,
                                           "private_child_name": "DO NOT LEAK"})
        public = self.panel.public()
        self.assertTrue(public["details"]["110980"]["available"])
        self.assertNotIn("DO NOT LEAK", repr(public))


    def test_linked_accounts_names_validate_payer_and_unique_child(self):
        from uhkone.browser import BrowserError
        from uhkone.linked_accounts import linked_child_names
        valid = {"account_id": 10, "linked_accounts": [
            {"account_id": 20, "name": "Testilapsi"},
            {"account_id": 30, "name": "Toinen testilapsi"},
        ]}
        self.assertEqual(linked_child_names(valid), ["Testilapsi", "Toinen testilapsi"])
        string_ids = {"account_id": "10", "linked_accounts": [
            {"account_id": "20", "name": "Testilapsi"},
        ]}
        self.assertEqual(linked_child_names(string_ids), ["Testilapsi"])
        for bad in (
            {**valid, "account_id": None},
            {**valid, "linked_accounts": [{"account_id": 10, "name": "Oma tili"}]},
            {**valid, "linked_accounts": [{"account_id": 20, "name": "Sama"},
                                          {"account_id": 30, "name": "sama"}]},
            {**valid, "linked_accounts": [{"account_id": 20, "name": "Yksi"},
                                          {"account_id": 20, "name": "Kaksi"}]},
            {**valid, "linked_accounts": [{"account_id": 20, "name": ""}]},
        ):
            with self.subTest(bad=bad), self.assertRaises(BrowserError):
                linked_child_names(bad)

    def test_health_states_distinguish_rush_and_pending_reservation(self):
        state = self.panel.public()
        self.assertEqual(state["health"], "valmis")
        self.panel.progress("candidate", {"course_id": 110980, "available": False})
        normal = self.panel.public()
        self.assertEqual(normal["health"], "seurataan")
        self.assertIsNotNone(normal["last_checked"])
        self.panel.progress("retry", {"retry_after": 7, "private_info": "SECRET"})
        overloaded = self.panel.public()
        self.assertEqual(overloaded["health"], "ruuhka")
        self.assertNotIn("SECRET", repr(overloaded))
        self.panel.progress("reservation_pending", {"course_id": 110980})
        self.assertEqual(self.panel.public()["health"], "varaus_epavarma")
        self.panel.progress("held", {"course_id": 110980})
        self.assertEqual(self.panel.public()["health"], "maksamaton_kori")

    def test_sensitive_exception_is_replaced_with_safe_action(self):
        from uhkone.booking import BookingError
        self.panel.record_failure(BookingError("Kirjautuminen ei ole enää voimassa. salainen data"))
        state = self.panel.public()
        self.assertEqual(state["health"], "kirjaudu_uudelleen")
        self.assertNotIn("salainen", repr(state))

    def test_manual_fallback_exposes_only_verified_public_course_url(self):
        self.panel.course_info([SimpleNamespace(
            id=110980, location="Mäkelänrinne", time_start="17:50",
            registration_start=None,
            url="https://kauppa.urheiluhallit.fi/webshop/reservations2/reservations/9/6/2/-/110980",
        )])
        detail = self.panel.public()["details"]["110980"]
        self.assertEqual(detail["manual_url"],
                         "https://kauppa.urheiluhallit.fi/webshop/reservations2/reservations/9/6/2/-/110980")
        self.assertNotIn("child", repr(detail))
        # An injected URL must never become a clickable navigation target.
        self.panel.course_info([SimpleNamespace(
            id=110962, location="Kallio", time_start="18:10",
            registration_start=None, url="https://evil.example/steal?child=Testilapsi",
        )])
        self.assertNotIn("manual_url", self.panel.public()["details"]["110962"])

    def test_preflight_rejects_existing_cart_and_duplicate_child_bookings(self):
        from uhkone.preflight import validate_preflight_snapshot

        course = {"reservation_event_group_id": 110980, "public_reservation_start": "2026-11-24 07:00:00",
                  "price": 7600, "free": 10, "is_reservable": False}
        user = {"account_id": 10, "linked_accounts": [{"account_id": 20, "name": "Testilapsi"}]}
        result = validate_preflight_snapshot(user=user, cart=[], reservations=[],
                                             courses=[course], ids=[110980], child="Testilapsi",
                                             max_price="76")
        self.assertTrue(result["ready"])
        self.assertEqual(result["courses"][0]["id"], 110980)
        self.assertEqual(result["courses"][0]["opens_at"], "2026-11-24T07:00:00+02:00")
        self.assertNotIn("Testilapsi", repr(result))
        self.assertFalse(validate_preflight_snapshot(
            user=user, cart=[{"item_id": 1}], reservations=[],
            courses=[course], ids=[110980], child="Testilapsi", max_price="76")["ready"])
        self.assertFalse(validate_preflight_snapshot(
            user=user, cart=[], reservations=[{"reservation_account_id": 20,
                    "reservation_event_group_id": 110980, "reservation_status_id": 7}],
            courses=[course], ids=[110980], child="Testilapsi", max_price="76")["ready"])

    def test_preflight_fails_closed_on_unknown_or_wrong_course(self):
        from uhkone.preflight import validate_preflight_snapshot

        with self.assertRaises(ValueError):
            validate_preflight_snapshot(
                user={"account_id": 10, "linked_accounts": [{"account_id": 20, "name": "Testilapsi"}]},
                cart=[], reservations=[],
                courses=[{"reservation_event_group_id": 999, "price": 7600}],
                ids=[110980], child="Testilapsi", max_price="76")

    def test_stop_checkpoint_refuses_to_restart_actions(self):
        from uhkone.console import StopRequested
        self.panel.stop_requested = True
        with self.assertRaises(StopRequested):
            self.panel.checkpoint(lambda _: None)


if __name__ == "__main__":
    unittest.main()
