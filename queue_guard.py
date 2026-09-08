#!/usr/bin/env python3
"""Kuyruk derinligine gore taramayi baslat/durdur (systemd ExecCondition/oneshot).

8 Eyl 2026 kullanici karari: "ready to send" 450'ye dusunce tarama otomatik
baslasin, 900'e cikinca dursun. Esikler country_campaign.QUEUE_LOW/HIGH.

  --start-below N          kuyruk < N ise cikis 0 (gece baslangici: < QUEUE_HIGH)
  --start-at-or-below N    kuyruk <= N ise cikis 0 (gunduz acil doldurma: <= QUEUE_LOW)
  --stop-service-if-at-least N   kuyruk >= N ve tarama calisiyorsa servisi durdur
Cikis 1 = "bu sefer baslama"; hata degil.
"""
from __future__ import annotations

import argparse
import subprocess

from country_campaign import QUEUE_HIGH, QUEUE_LOW
from send_mails import DeliveryState, load_rows, normalize_email

SERVICE = "personal-job-qualified-contact-targets.service"


def unsent_count() -> int:
    rows = load_rows()
    with DeliveryState() as state:
        attempted = state.attempted_emails()
    return sum(1 for row in rows if normalize_email(row["email"]) not in attempted)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-below", type=int)
    parser.add_argument("--start-at-or-below", type=int)
    parser.add_argument("--stop-service-if-at-least", type=int)
    args = parser.parse_args()
    unsent = unsent_count()
    if args.stop_service_if_at_least is not None:
        active = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0
        if unsent >= args.stop_service_if_at_least and active:
            subprocess.run(["systemctl", "stop", SERVICE], check=False)
            print(f"kuyruk {unsent} >= {args.stop_service_if_at_least}; tarama durduruldu")
        else:
            print(f"kuyruk {unsent}; tarama {'calisiyor' if active else 'kapali'}, esik {args.stop_service_if_at_least}")
        return
    if args.start_below is not None:
        ok = unsent < args.start_below
        print(f"kuyruk {unsent} {'<' if ok else '>='} {args.start_below}; tarama {'basliyor' if ok else 'atlaniyor'}")
        raise SystemExit(0 if ok else 1)
    if args.start_at_or_below is not None:
        ok = unsent <= args.start_at_or_below
        print(f"kuyruk {unsent} {'<=' if ok else '>'} {args.start_at_or_below}; acil doldurma {'basliyor' if ok else 'gerekmiyor'}")
        raise SystemExit(0 if ok else 1)
    print(f"kuyruk {unsent} (esikler: dusuk {QUEUE_LOW}, yuksek {QUEUE_HIGH})")


if __name__ == "__main__":
    main()
