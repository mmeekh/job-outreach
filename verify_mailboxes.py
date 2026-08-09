#!/usr/bin/env python3
"""Adreslerin gercekten var olup olmadigini MAIL GONDERMEDEN kontrol eder.

Iki asama:
  1. MX kaydi: alan adinin mail sunucusu var mi? Yoksa adres kesinlikle olu.
  2. SMTP sorgusu: sunucuya baglanip "bu kutu var mi" diye sorar (RCPT TO),
     sonra QUIT ile kapatir. Mesaj GONDERILMEZ (DATA komutu hic calismaz).

Sinirlar (durust olmak gerekirse):
  - "catch-all" sunucular her adrese evet der; bu adresler 'belirsiz' kalir.
  - Bazi sunucular yabanci IP'den gelen sorguyu geciktirir/reddeder; onlar da
    'belirsiz' sayilir ve listeden ATILMAZ (yanlislikla iyi adresi silmemek icin).
  - Sadece kesin 'boyle bir kutu yok' cevabi alanlar olu kabul edilir.

Kullanim:
  python3 verify_mailboxes.py            # kuyruktaki gonderilmemis adresleri tara
  python3 verify_mailboxes.py --apply    # olu bulunanlari exclusions.csv'ye ekle
"""
from __future__ import annotations

import argparse
import csv
import smtplib
import socket
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import dns.resolver

from send_mails import (
    DeliveryState, EMAIL, EXCLUSIONS_PATH, load_rows, normalize_email,
)

TIMEOUT = 12
_mx_cache: dict[str, list[str]] = {}


def mx_hosts(domain: str) -> list[str]:
    if domain in _mx_cache:
        return _mx_cache[domain]
    hosts: list[str] = []
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=TIMEOUT)
        hosts = [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda a: a.preference)]
    except Exception:
        try:  # MX yoksa A kaydi da mail kabul edebilir
            dns.resolver.resolve(domain, "A", lifetime=TIMEOUT)
            hosts = [domain]
        except Exception:
            hosts = []
    _mx_cache[domain] = hosts
    return hosts


def probe(address: str) -> tuple[str, str, str]:
    """(adres, durum, aciklama). durum: gecerli | OLU | belirsiz"""
    domain = address.split("@")[-1]
    hosts = mx_hosts(domain)
    if not hosts:
        return address, "OLU", "alan adinin mail sunucusu yok"

    for host in hosts[:2]:
        try:
            server = smtplib.SMTP(timeout=TIMEOUT)
            server.connect(host, 25)
            server.helo("gmail.com")
            server.mail(EMAIL)
            code, message = server.rcpt(address)
            # catch-all testi: rastgele bir kutu da kabul ediliyor mu?
            catch_code, _ = server.rcpt(f"zz-no-such-user-9174@{domain}")
            server.quit()
            text = message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message)
            if code in (250, 251):
                if catch_code in (250, 251):
                    return address, "belirsiz", "sunucu her adrese evet diyor (catch-all)"
                return address, "gecerli", "kutu dogrulandi"
            if code in (550, 551, 553, 554):
                return address, "OLU", f"{code}: {text[:60]}"
            return address, "belirsiz", f"{code}: {text[:60]}"
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                socket.timeout, socket.error, OSError) as exc:
            last = f"{type(exc).__name__}"
            continue
        except Exception as exc:
            last = f"{type(exc).__name__}"
            continue
    return address, "belirsiz", f"sunucuya sorulamadi ({last})"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="olu bulunanlari exclusions.csv'ye ekle")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    rows = load_rows()
    with DeliveryState() as state:
        attempted = state.attempted_emails()
    queue = [r for r in rows if normalize_email(r["email"]) not in attempted]
    addresses = [r["email"].strip() for r in queue]
    if args.limit:
        addresses = addresses[:args.limit]
    print(f"kuyrukta {len(addresses)} adres taranacak", flush=True)

    results: list[tuple[str, str, str]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(probe, a) for a in addresses]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            done += 1
            if result[1] == "OLU":
                print(f"  OLU  {result[0]:42s} {result[2]}", flush=True)
            elif done % 50 == 0:
                print(f"  ...{done}/{len(addresses)}", flush=True)

    stats = Counter(status for _, status, _ in results)
    print("\nSONUC:", dict(stats))
    dead = [a for a, s, _ in results if s == "OLU"]

    if dead and args.apply:
        existing = {normalize_email(r.get("email", ""))
                    for r in csv.DictReader(open(EXCLUSIONS_PATH, encoding="utf-8"))}
        new = [a for a in dead if normalize_email(a) not in existing]
        with open(EXCLUSIONS_PATH, "a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            for address in new:
                writer.writerow([address, "Kutu dogrulanamadi (SMTP kontrolu)", ""])
        print(f"{len(new)} olu adres exclusions.csv'ye eklendi")
    elif dead:
        print(f"{len(dead)} olu adres bulundu; eklemek icin --apply ile calistir")


if __name__ == "__main__":
    main()
