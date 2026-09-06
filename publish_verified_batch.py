#!/usr/bin/env python3
"""Atomically publish evidence-verified scraper output to the mail queue.

This is the only automatic bridge from the lead scraper to ``firmalar.csv``.
It never guesses an address: every accepted row needs an HTTPS page that
contains the public contact address and an exactly matching personalized cover
email.  It rechecks the live queue, exclusion list, legacy log and SQLite
delivery ledger *while holding the sender lock*, so a restart or concurrent
sender cannot introduce a duplicate.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from send_mails import (
    BASE,
    CSV_PATH,
    EXCLUSIONS_PATH,
    GITHUB_URL,
    LINKEDIN_URL,
    LOG_PATH,
    PERSONALIZATIONS_PATH,
    DeliveryState,
    english_subject_for,
    enterprise_exclusion_reason,
    load_rows,
    normalize_email,
    organization_key,
    queue_lock,
    route_for,
)
from project_paths import AUDIT_DIR, BACKUP_DIR, dated_dir
from country_campaign import (COUNTRIES, MARKER, MIN_FIT_SCORE,
                              TARGET_PER_COUNTRY, accepted_counts)

AUDIT_FIELDS = {
    "oncelik", "firma", "sehir", "email", "site", "fit_score", "email_source_url",
}
PERSONALIZATION_FIELDS = {"email", "firma", "subject", "body", "email_source_url"}
QUEUE_FIELDS = ("oncelik", "firma", "sehir", "email", "site", "dil_notu")
EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+$", re.I)
LEGAL_SUFFIXES = re.compile(
    r"\b(gmbh|ug|ag|kg|ohg|gbr|bv|b\.?v\.?|nv|n\.?v\.?|ltd|limited|llc|inc|incorporated|"
    r"plc|corp|corporation|holding|group)\b", re.I,
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def host(value: str) -> str:
    value = (value or "").strip()
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").casefold().removeprefix("www.")


def company_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", LEGAL_SUFFIXES.sub("", value or "").casefold())


def check_headers(required: set[str], path: Path) -> None:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        headers = set(csv.DictReader(handle).fieldnames or ())
    missing = required - headers
    if missing:
        raise ValueError(f"{path.name}: gerekli kolonlar yok: {', '.join(sorted(missing))}")


def is_https(value: str) -> bool:
    parsed = urlparse((value or "").strip())
    return parsed.scheme == "https" and bool(parsed.hostname)


def queue_row(row: dict[str, str]) -> dict[str, str]:
    turkish_priority = "turkish_company:" in (row.get("fit_reasons") or "")
    priority_note = "; turkish-company-priority" if turkish_priority else ""
    return {
        "oncelik": row["oncelik"].strip().upper(),
        "firma": row["firma"].strip(),
        "sehir": row["sehir"].strip(),
        "email": row["email"].strip(),
        "site": host(row["site"]),
        # Missing personalization must stop preflight rather than falling back
        # to the generic template for a newly scraped contact.
        "dil_notu": f"personalized-required; fit-score={row.get('fit_score', '').strip()}; "
                    f"evidence={row['email_source_url'].strip()}{priority_note}; {MARKER}",
    }


def row_problem(audit: dict[str, str], personalized: dict[str, str] | None) -> str | None:
    try:
        if float(audit.get("fit_score", "0")) < MIN_FIT_SCORE:
            return "profile fit below threshold"
    except (TypeError, ValueError):
        return "invalid profile fit score"
    email = normalize_email(audit.get("email", ""))
    if not EMAIL_RE.fullmatch(email):
        return "invalid email"
    if not audit.get("firma", "").strip() or not host(audit.get("site", "")):
        return "missing company or website"
    if not is_https(audit.get("email_source_url", "")):
        return "email source is not HTTPS"
    tag = audit.get("oncelik", "").strip().upper()
    if not tag.endswith("-EN"):
        return "not an English campaign row"
    try:
        country, language = route_for({"oncelik": tag})
    except ValueError:
        return "unsupported country route"
    if language != "en" or not country:
        return "unsafe language route"
    if personalized is None:
        return "missing personalization"
    if personalized.get("firma", "").strip() != audit["firma"].strip():
        return "personalization company mismatch"
    if personalized.get("email_source_url", "").strip() != audit["email_source_url"].strip():
        return "personalization evidence mismatch"
    subject = personalized.get("subject", "").strip()
    body = personalized.get("body", "")
    if subject != english_subject_for(audit["firma"]):
        return "invalid personalization subject"
    if audit["firma"].strip() not in body or LINKEDIN_URL not in body or GITHUB_URL not in body:
        return "invalid personalization body"
    return None


def write_csv_atomic(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as handle:
        temp = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path, help="scraper evidence audit CSV")
    parser.add_argument("personalizations", type=Path, help="matching generated cover-email CSV")
    parser.add_argument("--apply", action="store_true", help="validated rows are atomically added to the queue")
    parser.add_argument(
        "--write-audit",
        action="store_true",
        help="write detailed accepted/rejected CSVs to the dated archive (off for polling workers)",
    )
    args = parser.parse_args()

    audit_rows = read_csv(args.audit)
    personalization_rows = read_csv(args.personalizations)
    check_headers(AUDIT_FIELDS, args.audit)
    check_headers(PERSONALIZATION_FIELDS, args.personalizations)

    personalization_by_email: dict[str, dict[str, str]] = {}
    duplicate_personalizations: set[str] = set()
    for row in personalization_rows:
        key = normalize_email(row.get("email", ""))
        if key in personalization_by_email:
            duplicate_personalizations.add(key)
        personalization_by_email[key] = row

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    accepted: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []

    # The queue lock protects the live-state checks and two file replacements.
    # It is intentionally separate from the SMTP lock: an active sender has an
    # immutable batch snapshot, while newly verified rows should not wait hours
    # to join the next batch.
    with queue_lock(), DeliveryState() as state:
        # Validate the reviewed, sendable view, but retain suppressed historical
        # rows in the physical CSV when publishing. The exclusion list remains
        # the authority; a publishing pass must never silently erase its audit
        # trail from firmalar.csv.
        # Some historical hand-edited CSV rows contain spare columns. Keep the
        # supported six-field record intact, but never feed an unnamed DictReader
        # ``None`` key back into DictWriter during an atomic rewrite.
        existing_all = [
            {field: row.get(field, "") for field in QUEUE_FIELDS}
            for row in read_csv(CSV_PATH)
        ]
        existing = load_rows()
        cohort_counts = accepted_counts(existing_all)
        sent = read_csv(LOG_PATH) if LOG_PATH.exists() else []
        exclusions = read_csv(EXCLUSIONS_PATH)
        blocked_emails = {
            normalize_email(row.get("email", ""))
            for row in [*existing_all, *sent, *exclusions]
            if normalize_email(row.get("email", ""))
        } | state.attempted_emails()
        blocked_orgs = {organization_key(row) for row in existing}
        blocked_companies = {company_key(row.get("firma", "")) for row in existing if company_key(row.get("firma", ""))}
        blocked_companies |= {company_key(row.get("firma", "")) for row in sent if company_key(row.get("firma", ""))}
        blocked_companies |= {company_key(firm) for (firm,) in state.db.execute("SELECT firm FROM deliveries") if company_key(firm)}
        existing_personalizations = (
            {normalize_email(row.get("email", "")): row for row in read_csv(PERSONALIZATIONS_PATH)}
            if PERSONALIZATIONS_PATH.exists() else set()
        )
        seen_emails: set[str] = set()
        seen_orgs: set[str] = set()
        seen_companies: set[str] = set()

        for audit in audit_rows:
            email = normalize_email(audit.get("email", ""))
            candidate = queue_row(audit) if audit.get("email") else {}
            problem = row_problem(audit, personalization_by_email.get(email))
            country = route_for(candidate)[0] if not problem else ""
            if not problem and country not in COUNTRIES:
                problem = "country outside active campaign"
            if not problem and cohort_counts[country] >= TARGET_PER_COUNTRY:
                problem = "country research target already published"
            if email in duplicate_personalizations:
                problem = problem or "duplicate personalization"
            if not problem and email in blocked_emails:
                problem = "email already in queue/history/exclusions/delivery ledger"
            if not problem and enterprise_exclusion_reason(candidate):
                problem = "enterprise employer excluded by queue policy"
            org = organization_key(candidate) if not problem else ""
            firm = company_key(audit.get("firma", ""))
            if not problem and org in blocked_orgs:
                problem = "company website already in queue"
            elif not problem and firm in blocked_companies:
                problem = "company already in queue/history"
            elif not problem and email in existing_personalizations:
                active = existing_personalizations[email]
                generated = personalization_by_email[email]
                if any(active.get(field, "") != generated.get(field, "")
                       for field in ("firma", "subject", "body", "email_source_url")):
                    problem = "active personalization mismatch"
            elif not problem and email in seen_emails:
                problem = "duplicate candidate email"
            elif not problem and org in seen_orgs:
                problem = "duplicate candidate website"
            elif not problem and firm in seen_companies:
                problem = "duplicate candidate company"

            if problem:
                rejected.append({"email": audit.get("email", ""), "firma": audit.get("firma", ""), "reason": problem})
                continue
            accepted.append(audit)
            cohort_counts[country] += 1
            seen_emails.add(email)
            seen_orgs.add(org)
            seen_companies.add(firm)

        if args.apply and accepted:
            backup_dir = dated_dir(BACKUP_DIR)
            # One pre-change restore point per hour is enough for a worker that
            # polls every two minutes and prevents backup-file explosions.
            backup = backup_dir / f"firmalar.csv.bak-{datetime.now():%Y%m%d-%H00}"
            if not backup.exists():
                shutil.copy2(CSV_PATH, backup)
            old_personalizations = read_csv(PERSONALIZATIONS_PATH) if PERSONALIZATIONS_PATH.exists() else []
            new_personalizations = [
                personalization_by_email[normalize_email(row["email"])]
                for row in accepted
                if normalize_email(row["email"]) not in existing_personalizations
            ]
            # Install the snapshots first. If an interruption happens before
            # the queue replacement, there is no recipient to send to; the
            # reverse order would risk a generic fallback.
            write_csv_atomic(
                PERSONALIZATIONS_PATH,
                tuple(personalization_rows[0].keys()),
                [*old_personalizations, *new_personalizations],
            )
            write_csv_atomic(CSV_PATH, QUEUE_FIELDS, [*existing_all, *(queue_row(row) for row in accepted)])
            load_rows()  # verifies the published queue before releasing the sender lock
            print(f"published={len(accepted)} backup={backup}")
        else:
            print(f"validated={len(accepted)} (dry run; use --apply to publish)")

    if args.write_audit:
        audit_dir = dated_dir(AUDIT_DIR / "publisher")
        accepted_path = audit_dir / f"accepted-{stamp}.csv"
        rejected_path = audit_dir / f"rejected-{stamp}.csv"
        with accepted_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=("email", "firma", "oncelik", "email_source_url"))
            writer.writeheader()
            writer.writerows({key: row.get(key, "") for key in writer.fieldnames} for row in accepted)
        with rejected_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=("email", "firma", "reason"))
            writer.writeheader()
            writer.writerows(rejected)
        print(f"rejected={len(rejected)} audit_dir={audit_dir}")
    else:
        print(f"rejected={len(rejected)} audit_files=disabled")


if __name__ == "__main__":
    main()
