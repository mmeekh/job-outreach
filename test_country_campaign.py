import tempfile
import unittest
import csv
import io
from collections import Counter
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from country_campaign import (COUNTRIES, DAILY_PER_COUNTRY, MARKER, WEIGHTS,
                              accepted_counts, balanced_batch, claimed_counts,
                              daily_share, old_first_batch, in_cohort)
from send_mails import DeliveryState, route_for


def rows(counts):
    return [{"oncelik": f"{country}-EN", "email": f"job{i}@{country.lower()}{i}.test",
             "firma": f"Firm {country}{i}", "dil_notu": "", "site": f"{country}{i}.test"}
            for country, count in counts.items() for i in range(count)]


def distribution(batch):
    return Counter(route_for(row)[0] for row in batch)


class OrganisationDedupeTests(unittest.TestCase):
    def test_second_application_to_a_sent_organisation_is_skipped(self):
        import daily_batch
        queue = rows({"PL": 1, "MT": 1, "IE": 1})
        queue[0]["email"] = "kariera@hsbc.com"
        queue[1]["email"] = "malta_hrrecruitment@hsbc.com"
        kept, skipped = daily_batch.dedupe_organizations(queue, {"dublinreception@hsbc.com"})
        self.assertEqual([r["email"] for r in kept], [queue[2]["email"]])
        self.assertEqual([reason for reason, _ in skipped],
                         ["daha once bu kuruma gonderildi", "daha once bu kuruma gonderildi"])

    def test_duplicate_within_the_list_keeps_the_first_only(self):
        import daily_batch
        queue = rows({"PL": 1, "MT": 1})
        queue[0]["email"] = "kariera@hsbc.com"
        queue[1]["email"] = "malta_hrrecruitment@hsbc.com"
        kept, skipped = daily_batch.dedupe_organizations(queue, set())
        self.assertEqual([r["email"] for r in kept], ["kariera@hsbc.com"])
        self.assertEqual(skipped[0][0], "ayni kurum bu listede")
        daily_batch.assert_unique_organizations(kept)

    def test_shared_mail_providers_are_never_treated_as_one_organisation(self):
        import daily_batch
        queue = rows({"IE": 2})
        queue[0]["email"] = "a@gmail.com"
        queue[1]["email"] = "b@gmail.com"
        kept, skipped = daily_batch.dedupe_organizations(queue, {"c@gmail.com"})
        self.assertEqual(len(kept), 2)
        self.assertEqual(skipped, [])


class TierTests(unittest.TestCase):
    def test_posting_firms_first_small_offices_last_order_kept_within_tier(self):
        from country_campaign import prioritise_rows, tier_of, TIER_POSTING, TIER_SMALL, TIER_OTHER
        queue = rows({"IE": 4})
        queue[0]["firma"] = "Murphy & Co Chartered Accountants"     # kucuk buro
        queue[1]["firma"] = "Lendable Ltd"                           # diger
        queue[2]["firma"] = "Bradan Accountants"                     # kucuk buro
        queue[3]["firma"] = "Moniepoint"                             # ilanli
        ordered = prioritise_rows(queue, {queue[3]["email"]})
        self.assertEqual([r["firma"] for r in ordered],
                         ["Moniepoint", "Lendable Ltd", "Murphy & Co Chartered Accountants", "Bradan Accountants"])
        self.assertEqual(tier_of(queue[0], False), TIER_SMALL)
        self.assertEqual(tier_of(queue[1], False), TIER_OTHER)
        self.assertEqual(tier_of(queue[0], True), TIER_POSTING)   # ilan varsa buro olsa da one gecer

    def test_sender_survives_missing_lead_database(self):
        import daily_batch
        with patch.object(daily_batch, "LEADS_DB", Path("/nonexistent/leads.sqlite3")):
            self.assertEqual(daily_batch.posting_emails(rows({"IE": 2})), set())


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

    def test_full_day_follows_the_approved_weights(self):
        batch = balanced_batch(rows(dict.fromkeys(COUNTRIES, 500)), {}, 450)
        self.assertEqual(len(batch), 450)
        share = distribution(batch)
        for country, weight in WEIGHTS.items():
            self.assertAlmostEqual(share[country] / 450, weight / sum(WEIGHTS.values()),
                                   delta=0.005, msg=country)

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
        available = dict.fromkeys(COUNTRIES, 500)
        available["PT"] = 3
        batch = balanced_batch(rows(available), {}, 450)
        self.assertEqual(len(batch), 450)
        share = distribution(batch)
        self.assertEqual(share["PT"], 3)
        # Agirligi buyuk ulkeler PT'nin hizina inmez, kendi paylarini alir.
        self.assertGreaterEqual(share["GB"], daily_share("GB", 450))

    def test_restart_catches_up_from_the_least_served_country(self):
        previous = {"IE": 42, "PL": 41, "NL": 41}
        batch = balanced_batch(rows(dict.fromkeys(COUNTRIES, 100)), previous, 30)
        # Hic hizmet almamis ulkeler once gelir, sonra en geride kalan PL/NL.
        self.assertEqual(len(batch), 30)
        self.assertNotIn("IE", distribution(batch))

    def test_weightless_country_is_not_stranded(self):
        # LU/CH/AT/BE kapatildi ama kuyrukta yayinlanmis alicilari kalabilir;
        # ikinci tur onlari da almali, yoksa sonsuza kadar mahsur kalirlar.
        batch = balanced_batch(rows({"LU": 40, "GB": 10, "IE": 10}), {}, 450)
        self.assertEqual(distribution(batch)["LU"], 40)

    def test_other_countries_remain_queued_but_are_not_selected(self):
        batch = balanced_batch(rows({**dict.fromkeys(COUNTRIES, 2), "CA": 10}), {}, 450)
        self.assertEqual(len(batch), 2 * len(COUNTRIES))
        self.assertNotIn("CA", distribution(batch))

    def test_total_budget_and_duplicates(self):
        candidates = rows(dict.fromkeys(COUNTRIES, 100))
        batch = balanced_batch(candidates + candidates, {}, 17)
        self.assertEqual(len(batch), 17)
        self.assertEqual(len({r["email"] for r in batch}), 17)
        # Kucuk partide de en agir ulke en cok payi alir.
        self.assertEqual(max(distribution(batch), key=distribution(batch).get), "GB")

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
