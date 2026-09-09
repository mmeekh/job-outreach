#!/usr/bin/env python3
"""Cron entry point for the reviewed, restart-safe daily email batch."""
from __future__ import annotations

import random
from collections import Counter
import sqlite3
import sys
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from send_mails import (
    AlreadyRunningError,
    DeliveryState,
    DeliveryUncertainError,
    MAX_DELAY,
    MIN_DELAY,
    delivery_lock,
    load_rows,
    has_turkish_company_priority,
    normalize_email,
    preflight,
    queue_lock,
    send_row,
)
from send_mails import SHARED_MAIL_PROVIDERS, assert_unique_organizations
from campaign_pause import pause_status
from country_campaign import (DAILY_TOTAL, TIER_LABELS, old_first_batch, claimed_counts,
                              in_cohort, prioritise_rows, tier_of)
DAILY_LIMIT = 450
NEW_CONTACT_DAILY_LIMIT = DAILY_LIMIT
SEND_TIMEZONE = ZoneInfo("Europe/Istanbul")
SEND_START_HOUR = 9
SEND_END_HOUR = 20  # 31 Agu 2026: gunluk 400 hedefi 9 saatlik pencereye sigmiyordu


LEADS_DB = Path("/root/projects/otomasyon-paneli/apps/personal-job-outreach/lead-scraper/leads.sqlite3")


def posting_emails(rows: list[dict]) -> set[str]:
    """Adresleri, lead veritabaninda profile uygun canli ilani olan firmalara esle.

    Kuyruk dosyasi ilan bilgisi tasimaz; okunur modda lead veritabanina bakilir.
    Veritabani yoksa ya da okunamazsa bos kume doner ve parti eski sirayla gider:
    onceliklendirme gonderimi asla durdurmamali.
    """
    try:
        from personalize_outreach import relevant_role
    except ImportError:
        return set()
    emails = [normalize_email(row.get("email", "")) for row in rows]
    found: set[str] = set()
    try:
        conn = sqlite3.connect(f"file:{LEADS_DB}?mode=ro", uri=True, timeout=10)
        try:
            for start in range(0, len(emails), 400):
                chunk = [e for e in emails[start:start + 400] if e]
                if not chunk:
                    continue
                marks = ",".join("?" for _ in chunk)
                for email, titles in conn.execute(
                    f"SELECT lower(email), job_titles FROM leads WHERE lower(email) IN ({marks})", chunk
                ):
                    if any(relevant_role(t.strip()) for t in (titles or "").split("|") if t.strip()):
                        found.add(email)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"ilan onceligi atlandi (lead DB okunamadi: {exc})", flush=True)
    return found


def corporate_domain(email: str) -> str:
    """Posta alan adi; paylasimli saglayicilar (gmail vb.) kurum sayilmaz."""
    domain = normalize_email(email).split("@")[-1]
    return "" if not domain or domain in SHARED_MAIL_PROVIDERS else domain


def dedupe_organizations(rows: list[dict], sent_emails: set[str]) -> tuple[list[dict], list[tuple[str, dict]]]:
    """Ayni kuruma ikinci basvuruyu, gunu iptal etmeden ayikla.

    8 Eyl 2026: partide iki HSBC adresi cikinca assert_unique_organizations
    butun turu cokertti ve cron her saat ayni yerde patladi; gun boyunca sifir
    mail gitti. Kural degismedi (ayni kurumsal alan adina tek basvuru), ama
    artik iki sekilde uygulanir: daha once mail gitmis bir alan adi atlanir
    (koruma gecmise bakmiyordu, HSBC'ye 5 Eyl'de zaten yazilmisti) ve ayni
    listede tekrar eden alan adinin yalnizca ilki kalir. Atlananlar loglanir.
    """
    seen = {corporate_domain(email) for email in sent_emails}
    seen.discard("")
    kept: list[dict] = []
    skipped: list[tuple[str, dict]] = []
    for row in rows:
        domain = corporate_domain(row.get("email", ""))
        if domain and domain in seen:
            skipped.append(("daha once bu kuruma gonderildi" if domain not in
                            {corporate_domain(r["email"]) for r in kept} else "ayni kurum bu listede", row))
            continue
        if domain:
            seen.add(domain)
        kept.append(row)
    return kept, skipped


def within_send_window(now: datetime | None = None) -> bool:
    """09:00-20:00 arasi, Turkiye saati. Hafta sonu dahil her gun calisir
    (8 Agu 2026 kullanici karari: bot durmasin).

    31 Agu 2026'da 18:00'den 20:00'ye uzatildi: olculen gercek hiz mail basina
    86 sn (uyku ortalamasi 77 + SMTP baglanti yuku ~9) ve 9 saatlik pencereye
    ancak ~377 mail sigiyordu. 11 saat 460 maillik yer aciyor, gunluk 400 hedefi
    araligi kisaltmadan tutuyor. 20:00 Istanbul = 19:00 Hollanda."""
    local_now = now or datetime.now(SEND_TIMEZONE)
    return SEND_START_HOUR <= local_now.hour < SEND_END_HOUR


def main() -> None:
    requested_limit = min(int(sys.argv[1]) if len(sys.argv) > 1 else DAILY_LIMIT, DAILY_TOTAL)
    if requested_limit < 1:
        raise ValueError("gunluk limit pozitif olmali")
    paused, reason = pause_status()
    if paused:
        print(reason)
        return
    if not within_send_window():
        print("gonderim penceresi disinda (09:00-20:00 Europe/Istanbul); sonraki calismayi bekliyorum")
        return
    try:
        with delivery_lock(), DeliveryState() as state:
            # The sender owns delivery_lock for exactly-once SMTP claims, but
            # only holds queue_lock while taking its immutable batch snapshot.
            # This lets the incremental research worker publish later records
            # without changing the rows this sender is already processing.
            with queue_lock():
                rows = load_rows()
                today = time.strftime("%Y-%m-%d")
                sent_today = state.claimed_on(today)
                remaining_total = max(0, requested_limit - sent_today)
                remaining_new = min(
                    max(0, NEW_CONTACT_DAILY_LIMIT - sent_today),
                    remaining_total,
                )
                attempted = state.attempted_emails()
                eligible = [row for row in rows if normalize_email(row["email"]) not in attempted]
                # Stable ordering: evidence-tagged Turkish companies move to
                # the front; every other recipient keeps the reviewed CSV order.
                eligible.sort(key=lambda row: not has_turkish_company_priority(row))
                # Retired local-language templates must not block the active
                # DE/NL campaign.  Only a candidate that can render is allowed
                # into this immutable send snapshot.
                renderable = []
                for row in eligible:
                    try:
                        preflight([row])
                    except (FileNotFoundError, ValueError) as exc:
                        print(f"ATLANDI-GECERSIZ-SABLON: {row['firma']} ({type(exc).__name__})")
                        continue
                    renderable.append(row)
                renderable, skipped = dedupe_organizations(renderable, attempted)
                for reason, row in skipped:
                    print(f"ATLANDI-AYNI-KURUM: {row['firma']} <{row['email']}> ({reason})")
                # Canli ilani olan firmalar one, kucuk burolar sona; Turk-oncelik
                # sirasi katman icinde korunur (sorted kararlidir).
                with_posting = posting_emails(renderable)
                renderable = prioritise_rows(renderable, with_posting)
                country_claims = claimed_counts(state, today, rows, cohort_only=True)
                todo = old_first_batch(renderable, country_claims, remaining_new)
                tiers = Counter(TIER_LABELS[tier_of(row, normalize_email(row["email"]) in with_posting)] for row in todo)
                print(f"PARTI KATMANLARI: {dict(tiers)} (kuyrukta ilanli {len(with_posting)})", flush=True)
                old_pending = {normalize_email(row["email"]) for row in renderable if not in_cohort(row)}
                # Ayikladiktan sonra bu bir degismez: hala cakisma varsa gercek bir hata.
                assert_unique_organizations(todo)

            if not todo:
                pending = [row for row in rows if normalize_email(row["email"]) not in attempted]
                if pending:
                    print(f"gonderim yok: gunluk/ulke kotasi veya esit ulke dagilimi icin aday bekleniyor; tavan={requested_limit}")
                else:
                    print("gonderilecek yeni firma kalmadi; kampanya tamamlandi")
                return

            print(
                f"{time.strftime('%Y-%m-%d %H:%M')} - "
                f"{len(todo)} yeni mail gonderilecek; follow-up sistemi kapali",
                flush=True,
            )
            for index, row in enumerate(todo, 1):
                # A connection failure before a durable claim leaves the old
                # recipient pending. Do not jump into the experiment until a
                # later automatic run has handled that historical backlog.
                if in_cohort(row) and old_pending - state.attempted_emails():
                    print("eski kuyrukta denenmemis alici kaldi; yeni test sonraki otomatik turu bekliyor", flush=True)
                    break
                paused, reason = pause_status()
                if paused:
                    print(reason, flush=True)
                    break
                if not within_send_window():
                    print(f"{SEND_END_HOUR}:00 Europe/Istanbul oldu; kalan mailler yarina birakildi", flush=True)
                    break
                try:
                    sent = send_row(row, state)
                    result = "GONDERILDI" if sent else "ATLANDI-DUPLICATE"
                except DeliveryUncertainError:
                    result = "BELIRSIZ-TEKRARLANMAYACAK"
                except Exception as exc:
                    result = f"BAGLANTI-HATASI: {type(exc).__name__}: {exc}"
                print(
                    f"[{index}/{len(todo)}] {result}: {row['oncelik']:12s} "
                    f"{row['firma']} <{row['email']}>",
                    flush=True,
                )
                if index < len(todo):
                    time.sleep(random.randint(MIN_DELAY, MAX_DELAY))
            print(f"{time.strftime('%Y-%m-%d %H:%M')} - gunluk tur bitti", flush=True)
    except AlreadyRunningError:
        print("baska bir gonderim sureci zaten calisiyor, bu turu atliyorum")


if __name__ == "__main__":
    main()
