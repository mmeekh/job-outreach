import tempfile
import unittest
import csv
import io
from collections import Counter
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from country_campaign import (COUNTRIES, DAILY_PER_COUNTRY, MARKER, accepted_counts,
                              balanced_batch, claimed_counts, old_first_batch,
                              in_cohort)
from send_mails import DeliveryState, route_for


def rows(counts):
    return [{"oncelik": f"{country}-EN", "email": f"job{i}@{country.lower()}{i}.test",
             "firma": f"Firm {country}{i}", "dil_notu": "", "site": f"{country}{i}.test"}
            for country, count in counts.items() for i in range(count)]


def distribution(batch):
    return Counter(route_for(row)[0] for row in batch)


class CountryCampaignTests(unittest.TestCase):
    def test_sender_does_not_cross_failed_old_row_and_resumes_automatically(self):
        import daily_batch
        old = rows({"CA": 1})[0]
        new = rows(dict.fromkeys(COUNTRIES, 1))
        for row in new:
            row["dil_notu"] = MARKER
        candidates = new + [old]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            calls = []

            def fail_before_claim(row, state):
                calls.append(row)
                raise ConnectionError("test-only connection failure")

            def fake_send(row, state):
                calls.append(row)
                return state.claim(row, *route_for(row))

            with patch.object(daily_batch, "DeliveryState", lambda: DeliveryState(path / "state.sqlite3", path / "missing.csv")), \
                 patch.object(daily_batch, "delivery_lock", nullcontext), \
                 patch.object(daily_batch, "queue_lock", nullcontext), \
                 patch.object(daily_batch, "load_rows", lambda: candidates), \
                 patch.object(daily_batch, "preflight"), \
                 patch.object(daily_batch, "pause_status", return_value=(False, "")), \
                 patch.object(daily_batch, "within_send_window", return_value=True), \
                 patch.object(daily_batch.time, "sleep"), \
                 patch("sys.argv", ["daily_batch.py", "450"]), \
                 redirect_stdout(io.StringIO()):
                with patch.object(daily_batch, "send_row", fail_before_claim):
                    daily_batch.main()
                self.assertEqual(calls, [old])
                calls.clear()
                with patch.object(daily_batch, "send_row", fake_send):
                    daily_batch.main()
                    self.assertEqual(calls, [old] + new)
                    calls.clear()
                    daily_batch.main()
                    self.assertEqual(calls, [])

    def test_old_queue_precedes_new_with_shared_limit(self):
        old = rows({"CA": 32, "IE": 162, "NL": 74})
        new = rows(dict.fromkeys(COUNTRIES, 90))
        for row in new:
            row["dil_notu"] = MARKER
            row["email"] = "new" + row["email"]
        selected = old_first_batch(new + old, {}, 450)
        self.assertEqual(selected[:268], old)
        self.assertEqual(len(selected), 450)
        self.assertTrue(all(in_cohort(row) for row in selected[268:]))
        self.assertEqual(old_first_batch(new + old, {}, 100), old[:100])
        self.assertEqual(old_first_batch(old, {}, 450), old)

    def test_restart_after_old_queue_uses_new_country_allowances(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            old = rows({"IE": 1})[0]
            new = rows({"PL": 1})[0]
            new["dil_notu"] = MARKER
            with DeliveryState(path / "state.sqlite3", path / "missing.csv") as state:
                state.claim(old, "IE", "en")
                state.claim(new, "PL", "en")
                state.db.execute("UPDATE deliveries SET claimed_at='2026-09-07 07:00:00'")
                state.db.commit()
                self.assertEqual(state.claimed_on("2026-09-07"), 2)
                self.assertEqual(claimed_counts(state, "2026-09-07", [old, new], cohort_only=True), {"PL": 1})

    def test_full_day_fills_the_limit_and_stays_balanced(self):
        batch = balanced_batch(rows(dict.fromkeys(COUNTRIES, 150)), {}, 450)
        self.assertEqual(len(batch), 450)
        share = distribution(batch)
        self.assertLessEqual(max(share.values()) - min(share.values()), 1)
        self.assertLessEqual(max(share.values()), DAILY_PER_COUNTRY)
        self.assertEqual([route_for(r)[0] for r in batch[:len(COUNTRIES)]], list(COUNTRIES))

    def test_empty_country_does_not_block_the_rest(self):
        # 6 Eyl 2026 regresyon testi: tavan `min(... for c in COUNTRIES)` ile
        # hesaplandigi surece bos bir ulke butun gunu sifirliyordu. Malta'da
        # aday bitince gunluk gonderim 110'a dusmus, sonra 0'a inecekti.
        available = dict.fromkeys(COUNTRIES, 200)
        available["MT"] = 0
        batch = balanced_batch(rows(available), {}, 450)
        self.assertEqual(len(batch), 450)
        self.assertNotIn("MT", distribution(batch))

    def test_short_country_gives_up_its_turn_only(self):
        available = dict.fromkeys(COUNTRIES, 200)
        available["LU"] = 7
        batch = balanced_batch(rows(available), {}, 450)
        self.assertEqual(len(batch), 450)
        share = distribution(batch)
        self.assertEqual(share["LU"], 7)
        # Kalan kota tukenmeyen ulkelere esit dagilir, LU'nun hizina inmez.
        rest = [v for country, v in share.items() if country != "LU"]
        self.assertLessEqual(max(rest) - min(rest), 1)
        self.assertGreater(min(rest), 7)

    def test_restart_catches_up_from_the_least_served_country(self):
        previous = {"IE": 42, "PL": 41, "NL": 41}
        batch = balanced_batch(rows(dict.fromkeys(COUNTRIES, 100)), previous, 30)
        # Hic hizmet almamis ulkeler once gelir, sonra en geride kalan PL/NL.
        self.assertEqual(len(batch), 30)
        self.assertNotIn("IE", distribution(batch))

    def test_daily_cap_holds_while_stock_lasts(self):
        available = dict.fromkeys(COUNTRIES, 200)
        batch = balanced_batch(rows(available), {}, DAILY_PER_COUNTRY * len(COUNTRIES))
        self.assertEqual(distribution(batch), dict.fromkeys(COUNTRIES, DAILY_PER_COUNTRY))

    def test_other_countries_remain_queued_but_are_not_selected(self):
        batch = balanced_batch(rows({**dict.fromkeys(COUNTRIES, 2), "CA": 10}), {}, 450)
        self.assertEqual(len(batch), 2 * len(COUNTRIES))
        self.assertNotIn("CA", distribution(batch))

    def test_total_budget_and_duplicates(self):
        candidates = rows(dict.fromkeys(COUNTRIES, 100))
        batch = balanced_batch(candidates + candidates, {}, 17)
        self.assertEqual(len(batch), 17)
        self.assertEqual(len({r["email"] for r in batch}), 17)
        self.assertLessEqual(max(distribution(batch).values()) - min(distribution(batch).values()), 1)

    def test_cohort_excludes_old_queue_and_duplicate_rows(self):
        candidates = rows({"IE": 3})
        candidates[0]["dil_notu"] = f"personalized-required; {MARKER}"
        candidates[1]["dil_notu"] = f"{MARKER}-old"
        self.assertEqual(accepted_counts(candidates + candidates), {"IE": 1})

    def test_uncertain_and_legacy_claims_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            candidate = rows({"IE": 1})[0]
            with DeliveryState(path / "state.sqlite3", path / "absent.csv") as state:
                state.claim(candidate, "IE", "en")
                state.db.execute("UPDATE deliveries SET claimed_at='2026-09-07 07:00:00',status='uncertain',country_code=''")
                state.db.commit()
            with DeliveryState(path / "state.sqlite3", path / "absent.csv") as state:
                self.assertEqual(claimed_counts(state, "2026-09-07", [candidate]), {"IE": 1})
                self.assertEqual(claimed_counts(state, "2026-09-08", [candidate]), {})
                self.assertFalse(state.claim(candidate, "IE", "en"))


class PublisherCampaignTests(unittest.TestCase):
    def test_cap_restart_preserves_history_and_rejects_duplicates(self):
        import publish_verified_batch as publisher

        def write(path, fields, data):
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(data)

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            queue, exclusions, legacy, personal, audit, generated = [
                base / name for name in ("queue.csv", "exclusions.csv", "sent.csv",
                                         "personal.csv", "audit.csv", "generated.csv")]
            old = rows({"IE": 1})[0]
            old["sehir"] = "Dublin"
            write(queue, publisher.QUEUE_FIELDS, [old])
            write(exclusions, ["email"], [])
            write(legacy, ["email", "firma", "zaman", "durum"], [])
            candidates = rows({"IE": 4, "PL": 3, "DE": 1})
            audits, personalized = [], []
            for row in candidates:
                row = {k: row[k] for k in ("oncelik", "firma", "email", "site")}
                row.update(sehir="City", fit_score="60", email_source_url=f"https://{row['site']}/contact")
                audits.append(row)
                personalized.append({
                    "email": row["email"], "firma": row["firma"],
                    "subject": publisher.english_subject_for(row["firma"]),
                    "body": f"Dear {row['firma']} team, {publisher.LINKEDIN_URL} {publisher.GITHUB_URL}",
                    "email_source_url": row["email_source_url"],
                })
            write(audit, sorted(publisher.AUDIT_FIELDS), audits)
            write(generated, sorted(publisher.PERSONALIZATION_FIELDS), personalized)
            with patch.multiple(publisher, CSV_PATH=queue, EXCLUSIONS_PATH=exclusions,
                                LOG_PATH=legacy, PERSONALIZATIONS_PATH=personal,
                                BACKUP_DIR=base / "backups"), \
                 patch.object(publisher, "target_for", lambda country: 2), \
                 patch.object(publisher, "queue_lock", nullcontext), \
                 patch.object(publisher, "load_rows", lambda: publisher.read_csv(queue)), \
                 patch.object(publisher, "enterprise_exclusion_reason", lambda r: None), \
                 patch.object(publisher, "DeliveryState", lambda: DeliveryState(base / "state.sqlite3", legacy)), \
                 patch("sys.argv", ["publish", str(audit), str(generated), "--apply"]), \
                 redirect_stdout(io.StringIO()):
                publisher.main()
                after = publisher.read_csv(queue)
                self.assertEqual(len(after), 5)
                self.assertEqual(after[0], old)
                self.assertEqual(accepted_counts(after), {"IE": 2, "PL": 2})
                publisher.main()
                self.assertEqual(publisher.read_csv(queue), after)


if __name__ == "__main__":
    unittest.main()
