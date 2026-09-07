#!/usr/bin/env python3
"""Gece taramasi baslamadan once kuyruk derinligini kontrol et.

7 Eyl 2026 kullanici karari: "2 haftalik yeterli sirket oldugu zaman
scrape'i durdurabiliriz". Kuyrukta denenmemis alici sayisi 14 gunluk
gonderimi (14 x 450) karsiliyorsa tarama o gece BASLAMAZ; VPS bos kalir.
systemd ExecCondition olarak calisir: cikis 0 = basla, 1 = bu gece atla.
"""
from __future__ import annotations

import argparse

from send_mails import DeliveryState, load_rows, normalize_email

DAILY_SEND = 450


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-days", type=float, default=14.0)
    args = parser.parse_args()
    rows = load_rows()
    with DeliveryState() as state:
        attempted = state.attempted_emails()
    unsent = sum(1 for row in rows if normalize_email(row["email"]) not in attempted)
    days = unsent / DAILY_SEND
    if days >= args.min_days:
        print(f"kuyruk {unsent} alici = {days:.1f} gun >= {args.min_days:g}; tarama bu gece atlaniyor")
        raise SystemExit(1)
    print(f"kuyruk {unsent} alici = {days:.1f} gun < {args.min_days:g}; tarama basliyor")


if __name__ == "__main__":
    main()
