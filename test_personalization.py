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
        self.assertIn("Power BI", body)
        self.assertIn("Excel/VBA and Python", body)
        self.assertIn("one long-term, full-time role", body)
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


if __name__ == "__main__":
    unittest.main()
