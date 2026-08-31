#!/usr/bin/env python3
"""Cron entry point for the reviewed, restart-safe daily email batch."""
from __future__ import annotations

import random
import sys
import time
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
from send_mails import assert_unique_organizations
DAILY_LIMIT = 450
NEW_CONTACT_DAILY_LIMIT = DAILY_LIMIT
SEND_TIMEZONE = ZoneInfo("Europe/Istanbul")
SEND_START_HOUR = 9
SEND_END_HOUR = 18


def within_send_window(now: datetime | None = None) -> bool:
    """09:00-18:00 arasi, Turkiye saati. Hafta sonu dahil her gun calisir
    (8 Agu 2026 kullanici karari: bot durmasin)."""
    local_now = now or datetime.now(SEND_TIMEZONE)
    return SEND_START_HOUR <= local_now.hour < SEND_END_HOUR


def main() -> None:
    requested_limit = int(sys.argv[1]) if len(sys.argv) > 1 else DAILY_LIMIT
    if requested_limit < 1:
        raise ValueError("gunluk limit pozitif olmali")
    if not within_send_window():
        print("gonderim penceresi disinda (09:00-18:00 Europe/Istanbul); sonraki calismayi bekliyorum")
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
                todo = renderable[:remaining_new]
                # Ayni sirkete bu partide iki basvuru gitmesin.
                assert_unique_organizations(todo)

            if not todo:
                pending = [row for row in rows if normalize_email(row["email"]) not in attempted]
                if pending:
                    print(f"bugunku {requested_limit} mail tavani doldu, yarin devam")
                else:
                    print("gonderilecek yeni firma kalmadi; kampanya tamamlandi")
                return

            print(
                f"{time.strftime('%Y-%m-%d %H:%M')} - "
                f"{len(todo)} yeni mail gonderilecek; follow-up sistemi kapali",
                flush=True,
            )
            for index, row in enumerate(todo, 1):
                if not within_send_window():
                    print("18:00 Europe/Istanbul oldu; kalan mailler sonraki is gunune birakildi", flush=True)
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
