#!/usr/bin/env python3
"""Refresh only unsent personalized outreach snapshots from verified lead data.

Sent/claimed recipients are immutable.  For records still waiting in the queue,
this script regenerates the evidence-based body after a controlled template
change, then replaces ``personalizations.csv`` atomically.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

from personalize_outreach import evidence_hash, generate, validate
from publish_verified_batch import write_csv_atomic
from send_mails import DeliveryState, PERSONALIZATIONS_PATH, load_rows, normalize_email

SCRAPER_DB = Path("/root/projects/otomasyon-paneli/apps/personal-job-outreach/lead-scraper/leads.sqlite3")
REQUIRED = ("email", "firma", "subject", "body", "evidence_hash", "fit_score", "email_source_url")


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the refreshed snapshots")
    args = parser.parse_args()

    fields, existing = read_rows(PERSONALIZATIONS_PATH)
    missing = set(REQUIRED) - set(fields)
    if missing:
        raise ValueError(f"personalizations.csv missing fields: {sorted(missing)}")

    with DeliveryState() as state:
        attempted = state.attempted_emails()
    queue = [
        row for row in load_rows()
        if "personalized-required" in (row.get("dil_notu") or "").casefold()
        and normalize_email(row["email"]) not in attempted
    ]

    replacements: dict[str, dict[str, str]] = {}
    skipped: list[str] = []
    conn = sqlite3.connect(SCRAPER_DB)
    conn.row_factory = sqlite3.Row
    try:
        for queued in queue:
            email = normalize_email(queued["email"])
            lead = conn.execute(
                "SELECT * FROM leads WHERE lower(email)=? ORDER BY fit_score DESC LIMIT 1",
                (email,),
            ).fetchone()
            if not lead:
                skipped.append(f"{queued['firma']} <{email}>: lead not found")
                continue
            # CSV-driven personalizer receives strings; normalize SQLite's
            # numeric fields before passing the record to the shared helpers.
            audit = {key: "" if value is None else str(value) for key, value in dict(lead).items()}
            audit["firma"] = queued["firma"].strip()
            audit["email"] = email
            # 8 Eyl 2026: bir satirin dogrulamayi gecememesi (ornegin firma
            # kaynakli bir kelimenin yasakli kalibi tetiklemesi) butun yenilemeyi
            # iptal ediyordu. O satirin eski metni kalir, digerleri yenilenir.
            try:
                subject, body = generate(audit)
                validate(audit, subject, body)
            except (ValueError, FileNotFoundError) as exc:
                skipped.append(f"{queued['firma']} <{email}>: {exc}")
                continue
            replacements[email] = {
                "email": email,
                "firma": audit["firma"],
                "subject": subject,
                "body": body,
                "evidence_hash": evidence_hash(audit),
                "fit_score": str(audit.get("fit_score") or ""),
                "email_source_url": str(audit.get("email_source_url") or ""),
            }
    finally:
        conn.close()

    # Tek tuk satirin dogrulamayi gecememesi (firma kaynakli metin) yenilemeyi
    # durdurmamali: o satirlarin eski metni kalir. Cok sayida satir dusuyorsa
    # sablonun kendisi bozulmus demektir, o zaman hicbir sey yazilmaz.
    if skipped:
        print(f"skipped={len(skipped)}")
        for line in skipped[:12]:
            print("  -", line)
    if skipped and len(skipped) > max(25, len(queue) // 50):
        raise ValueError("cannot safely refresh: too many rows fail validation")

    refreshed = [
        {field: replacements.get(normalize_email(row.get("email", "")), row).get(field, "") for field in fields}
        for row in existing
    ]
    if args.apply:
        write_csv_atomic(PERSONALIZATIONS_PATH, tuple(fields), refreshed)
        print(f"refreshed={len(replacements)}")
    else:
        print(f"would_refresh={len(replacements)}")


if __name__ == "__main__":
    main()
