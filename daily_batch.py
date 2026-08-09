#!/usr/bin/env python3
"""Cron entry point for the reviewed, restart-safe daily email batch."""
from __future__ import annotations

import random
import subprocess
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
    normalize_email,
    preflight,
    send_row,
)

DAILY_LIMIT = 200
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
            rows = load_rows()
            preflight(rows)
            today = time.strftime("%Y-%m-%d")
            remaining_today = max(0, requested_limit - state.claimed_on(today))
            attempted = state.attempted_emails()
            todo = [row for row in rows if normalize_email(row["email"]) not in attempted][:remaining_today]

            if not todo:
                pending = [row for row in rows if normalize_email(row["email"]) not in attempted]
                if pending:
                    print(f"bugunku {requested_limit} mail tavani doldu, yarin devam")
                else:
                    print("gonderilecek yeni firma kalmadi; kampanya tamamlandi")
                    subprocess.run("crontab -l | grep -v daily_batch.py | crontab -", shell=True, check=False)
                return

            print(f"{time.strftime('%Y-%m-%d %H:%M')} - bugun {len(todo)} firmaya gonderilecek", flush=True)
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
