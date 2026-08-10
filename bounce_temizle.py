#!/usr/bin/env python3
"""Ulasilamayan (bounce) mailleri temizler ve olu adresleri engeller.

Saatlik cron'dan calisir. Iki is yapar:
  1. Gelen kutusundaki hata bildirimlerini Cop Kutusu'na tasir (kalici silmez,
     30 gun geri alinabilir).
  2. O bildirimlerin icindeki ulasilamayan adresleri exclusions.csv'ye ekler,
     boylece kampanya o adreslere bir daha denemez.

Sadece hata bildirimlerine dokunur; firma cevaplarina ve diger maillere ASLA.
"""
from __future__ import annotations

import csv
import email
import imaplib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_mails import EMAIL, EXCLUSIONS_PATH, PASSWORD_PATH, normalize_email

# hata bildirimlerini bulmak icin: gonderen ve konu kaliplari
ARAMALAR = [
    ("FROM", "mailer-daemon"),
    ("FROM", "postmaster"),
    ("SUBJECT", '"Delivery Status Notification"'),
    ("SUBJECT", "Undeliverable"),
    ("SUBJECT", '"Mail delivery failed"'),
    ("SUBJECT", '"Returned mail"'),
    ("SUBJECT", '"Delivery has failed"'),
]

# bounce metninde gecen ama firma adresi OLMAYAN seyler
YOKSAY = ("muhammeteminkilic", "google.com", "gmail.com", "job-outreach.local",
          "googlemail.com", "postmaster@", "mailer-daemon@")

ADRES_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def olu_adresler(msg) -> set[str]:
    bulunan: set[str] = set()
    for part in msg.walk():
        if part.get_content_type() not in ("message/delivery-status", "text/plain"):
            continue
        try:
            metin = part.get_payload(decode=True).decode("utf-8", "replace")
        except Exception:
            continue
        bulunan.update(ADRES_RE.findall(metin))
    temiz = set()
    for adres in bulunan:
        adres = adres.strip().strip("-<>").lower()
        if any(y in adres for y in YOKSAY):
            continue
        if ADRES_RE.fullmatch(adres):
            temiz.add(adres)
    return temiz


def main() -> None:
    password = PASSWORD_PATH.read_text().strip()
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(EMAIL, password)
    imap.select("INBOX")

    hatalar: set[bytes] = set()
    for alan, deger in ARAMALAR:
        typ, data = imap.search(None, alan, deger)
        if typ == "OK":
            hatalar.update(data[0].split())

    if not hatalar:
        print("temizlenecek hata maili yok")
        imap.logout()
        return

    yeni_olu: set[str] = set()
    for msg_id in sorted(hatalar):
        typ, raw = imap.fetch(msg_id, "(RFC822)")
        if typ != "OK" or not raw or not raw[0]:
            continue
        yeni_olu |= olu_adresler(email.message_from_bytes(raw[0][1]))

    imap.store(b",".join(sorted(hatalar)), "+X-GM-LABELS", "\\Trash")
    imap.expunge()
    imap.logout()

    mevcut = {normalize_email(r.get("email", ""))
              for r in csv.DictReader(EXCLUSIONS_PATH.open(encoding="utf-8"))}
    eklenecek = sorted(a for a in yeni_olu if normalize_email(a) not in mevcut)
    if eklenecek:
        with EXCLUSIONS_PATH.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            for adres in eklenecek:
                writer.writerow([adres, "Ulasilamayan adres (otomatik bounce temizligi)", ""])

    from datetime import datetime
    print(f"{datetime.now():%Y-%m-%d %H:%M} | {len(hatalar)} hata maili cope tasindi, "
          f"{len(eklenecek)} yeni olu adres engellendi")


if __name__ == "__main__":
    main()
