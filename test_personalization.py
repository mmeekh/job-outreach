from __future__ import annotations

import unittest

import personalize_outreach as p


def sample_audit():
    return {
        "oncelik": "IE-EN",
        "firma": "Acme Finance",
        "sehir": "Dublin",
        "email": "jobs@acme.example",
        "site": "acme.example",
        "fit_score": "82",
        "fit_tracks": "finance_ops,automation_ai,backend_platform,data_bi",
        "fit_reasons": "finance_ops:20 | automation_ai:20",
        "fit_keywords": "accounting,automation,python,power bi",
        "career_url": "https://acme.example/careers",
        "email_source_url": "https://acme.example/contact",
        "job_titles": "Junior Automation Engineer | Senior Finance Manager",
        "english_signal": "1",
        "source": "test",
    }


class PersonalizationTests(unittest.TestCase):
    def test_generation_is_deterministic_and_evidence_based(self):
        row = sample_audit()
        first = p.generate(row)
        second = p.generate(row)
        self.assertEqual(first, second)
        subject, body = first
        p.validate(row, subject, body)
        self.assertEqual(subject, p.english_subject_for("Acme Finance"))
        self.assertIn("I am writing to apply for a full-time position", body)
        for ifade in ("at no cost", "proof of concept", "free of charge"):
            self.assertNotIn(ifade, body.casefold())
        self.assertIn("Junior Automation Engineer", body)
        self.assertIn("around three years of experience in accounting and reporting", body)
        self.assertNotIn("Clemta", body)
        self.assertNotIn("Acun Media", body)
        self.assertIn("Excel-based reporting", body)
        self.assertIn("Excel/VBA and Python", body)
        self.assertIn("one long-term, full-time role", body)
        self.assertIn("I consent to you keeping my CV on file", body)
        self.assertIn("I would be happy to have a short introductory call", body)
        self.assertNotIn("proof of concept", body.casefold())
        self.assertEqual(body.count("I've also attached my CV for context."), 1)

    def test_missing_https_evidence_is_rejected(self):
        row = sample_audit()
        row["email_source_url"] = ""
        subject, body = p.generate(row)
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            p.validate(row, subject, body)

    def test_job_titles_are_used_only_when_the_scraper_found_one(self):
        row = sample_audit()
        row["job_titles"] = "Senior Finance Manager | Data Analyst"
        _subject, body = p.generate(row)
        self.assertIn("Data Analyst", body)
        self.assertNotIn("Senior Finance Manager", body)

    def test_unrelated_openings_are_never_cited(self):
        # 8 Eyl 2026: Rentman'a "Senior Frontend Developer", Alphatron'a
        # "IT Solutions Engineer" ilanini gordugumuz yazilmisti.
        row = sample_audit()
        row["job_titles"] = "Senior Frontend Developer (Customer Lifecycle) | IT Solutions Engineer"
        _subject, body = p.generate(row)
        self.assertNotIn("Frontend", body)
        self.assertNotIn("IT Solutions", body)
        self.assertNotIn("I noticed", body)
        self.assertIn("caught my attention", body)

    def test_product_page_blurbs_are_not_mistaken_for_openings(self):
        # Descartes, 8 Eyl 2026: "compliance" gecen bir urun sayfasi basligi
        # anildi ve icindeki "leverage" dogrulayiciya takildi.
        row = sample_audit()
        row["job_titles"] = ("Trade Compliance Content for Business Systems Leverage industry-leading "
                             "content for SAP | Product Classification Integrate classification with your business")
        _subject, body = p.generate(row)
        self.assertNotIn("I noticed", body)
        p.validate(row, _subject, body)

    def test_relevant_opening_is_still_cited(self):
        row = sample_audit()
        row["job_titles"] = "Backend Engineer | Junior Accountant | Accounts Payable Specialist"
        _subject, body = p.generate(row)
        self.assertIn("Junior Accountant", body)
        self.assertNotIn("Backend", body)


if __name__ == "__main__":
    unittest.main()
