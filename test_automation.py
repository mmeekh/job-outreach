from __future__ import annotations

import csv
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import send_mails


class FakeSMTP:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.messages = []
        self.closed = False

    def send_message(self, message):
        self.messages.append(message)
        if self.error:
            raise self.error
        return {}

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


def sample_row(tag: str = "DE-EN", email: str = "candidate@example.test"):
    return {
        "oncelik": tag,
        "firma": "Example Finance",
        "sehir": "Berlin (Almanya)",
        "email": email,
        "site": "example.test",
        "dil_notu": "",
    }


class RoutingAndDataTests(unittest.TestCase):
    def test_every_active_row_renders_and_has_https_link(self):
        rows = send_mails.load_rows()
        self.assertGreater(len(rows), 0)
        for row in rows:
            subject, body, country, language = send_mails.render_for(row)
            self.assertTrue(subject)
            self.assertIn(send_mails.LINKEDIN_URL, body)
            self.assertIn(send_mails.GITHUB_URL, body)
            if country != "NL":
                self.assertNotEqual(language, "nl")
                # Hollandaca kontrolu SABLONDA yapilir: alici firmanin adinda
                # "boekhouding" gecmesi mesajin Hollandaca oldugunu gostermez.
                sablon_konu, sablon_govde = send_mails.load_template(language)
                sablon = f"{sablon_konu}\n{sablon_govde}".casefold()
                for marker in send_mails.NON_NL_FORBIDDEN:
                    self.assertNotIn(marker, sablon, row["email"])

    def test_every_active_row_is_routed_to_an_allowed_language(self):
        """Avrupa/Korfez kollari Ingilizce; Turkiye ve kruvaziyer kollari kendi
        sablonlarini kullanir. Baska hicbir kombinasyona izin verilmez."""
        allowed = {"TR": "tr-yerel", "CRUISE": "en-cruise"}
        rows = send_mails.load_rows()
        for row in rows:
            country, language = send_mails.route_for(row)
            self.assertTrue(country)
            self.assertEqual(language, allowed.get(country, "en"), row["email"])

    def test_unknown_route_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "bilinmeyen"):
            send_mails.route_for(sample_row("DE-NL"))

    def test_preflight_validates_every_active_template_and_pdf(self):
        send_mails.preflight(send_mails.load_rows(), require_password=False)

    def test_sent_and_active_lists_have_no_duplicate_addresses_or_orgs(self):
        active = send_mails.load_rows()
        emails = [send_mails.normalize_email(row["email"]) for row in active]
        organizations = [send_mails.organization_key(row) for row in active]
        self.assertEqual(len(emails), len(set(emails)))
        self.assertEqual(len(organizations), len(set(organizations)))

        with send_mails.LOG_PATH.open(encoding="utf-8", newline="") as f:
            sent = [row for row in csv.DictReader(f) if row.get("durum") == "OK"]
        sent_counts = Counter(send_mails.normalize_email(row["email"]) for row in sent)
        self.assertFalse([email for email, count in sent_counts.items() if count > 1])

    def test_message_id_is_stable_per_campaign_and_recipient(self):
        first, _country, _language = send_mails.build_msg(sample_row())
        second, _country, _language = send_mails.build_msg(sample_row())
        self.assertEqual(first["Message-ID"], second["Message-ID"])


class RestartAndDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.db_path = self.base / "state.sqlite3"
        self.no_legacy = self.base / "missing.csv"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_claim_survives_restart_and_cannot_be_claimed_twice(self):
        row = sample_row()
        with send_mails.DeliveryState(self.db_path, self.no_legacy) as state:
            self.assertTrue(state.claim(row, "DE", "en"))
        with send_mails.DeliveryState(self.db_path, self.no_legacy) as restarted:
            self.assertIn(send_mails.normalize_email(row["email"]), restarted.attempted_emails())
            self.assertFalse(restarted.claim(row, "DE", "en"))
            self.assertEqual(restarted.counts(), {"claimed": 1})

    def test_connection_failure_does_not_claim_recipient(self):
        row = sample_row()
        with send_mails.DeliveryState(self.db_path, self.no_legacy) as state:
            with mock.patch.object(
                send_mails, "connect_smtp", side_effect=ConnectionError("offline")
            ):
                with self.assertRaises(ConnectionError):
                    send_mails.send_row(row, state)
            self.assertEqual(state.attempted_emails(), set())

    def test_success_is_marked_sent_once(self):
        row = sample_row()
        server = FakeSMTP()
        with send_mails.DeliveryState(self.db_path, self.no_legacy) as state:
            with mock.patch.object(send_mails, "connect_smtp", return_value=server), mock.patch.object(
                send_mails, "log_result"
            ):
                send_mails.send_row(row, state)
            self.assertEqual(state.counts(), {"sent": 1})
            self.assertEqual(len(server.messages), 1)
            self.assertFalse(state.claim(row, "DE", "en"))

    def test_ambiguous_smtp_result_is_never_retried(self):
        row = sample_row()
        server = FakeSMTP(TimeoutError("connection lost after DATA"))
        with send_mails.DeliveryState(self.db_path, self.no_legacy) as state:
            with mock.patch.object(send_mails, "connect_smtp", return_value=server), mock.patch.object(
                send_mails, "log_result"
            ):
                with self.assertRaises(send_mails.DeliveryUncertainError):
                    send_mails.send_row(row, state)
            self.assertEqual(state.counts(), {"uncertain": 1})
        with send_mails.DeliveryState(self.db_path, self.no_legacy) as restarted:
            self.assertFalse(restarted.claim(row, "DE", "en"))

    def test_second_process_lock_is_rejected_and_lock_recovers(self):
        lock_path = self.base / "delivery.lock"
        with mock.patch.object(send_mails, "LOCK_PATH", lock_path):
            with send_mails.delivery_lock():
                with self.assertRaises(send_mails.AlreadyRunningError):
                    with send_mails.delivery_lock():
                        pass
            with send_mails.delivery_lock():
                pass


if __name__ == "__main__":
    unittest.main()
