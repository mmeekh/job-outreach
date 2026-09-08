#!/usr/bin/env python3
"""Gece taramasinin sabah raporu ve alarmi.

7 Eyl 2026: arastirma servisi bir kod hatasi yuzunden gece boyunca 398 kez
cokup yeniden basladi; "active" gorundugu icin kimse fark etmedi ve sabah
sifir lead ile karsilastik. Bu betik her sabah gecenin gercek ciktisini
olcer ve e-postayla bildirir. Cikti sifirsa, servis cokmusse ya da kuyruk
2 gunun altina dusmusse konu satiri [ALARM] ile baslar.

Modlar:
  (varsayilan)  gece penceresini olc, raporu her zaman gonder
  --preflight   tarama baslamadan selfcheck calistir, YALNIZCA sorun varsa
                alarm gonder (sessiz basari)
  --dry-run     e-posta gonderme, raporu ekrana yaz
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import json

from send_mails import EMAIL, DeliveryState, connect_smtp, load_rows, normalize_email
from country_campaign import CAMPAIGN, RESEARCH_COUNTRIES, WEIGHTS

BASE = Path(__file__).resolve().parent
SCRAPER = BASE.parent / "lead-scraper"
LEADS_DB = SCRAPER / "leads.sqlite3"
ORCHESTRATOR = SCRAPER / "run-qualified-contact-targets.py"
# "Yeni lead" sayimi icin son raporda gorulen en buyuk rowid. 8 Eyl 2026: eski
# olcum checked_at'e bakiyordu; kesif kaynaklari o alani doldurmadigi icin
# gece 3.000+ firma eklenmisken rapor "2 yeni lead" yazdi.
STATE_PATH = BASE / "runtime" / "research-report-state.json"
SERVICE = "personal-job-qualified-contact-targets.service"
DAILY_SEND = 450
NIGHT_HOURS = 10          # 20:00-05:00 UTC penceresi + pay
MIN_RUNWAY_DAYS = 2.0     # kuyruk bunun altina dusunce alarm


def systemctl_show(*props: str) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", SERVICE, *(f"-p{p}" for p in props)],
        text=True, capture_output=True, timeout=30,
    )
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def journal_tracebacks(since: datetime) -> int:
    result = subprocess.run(
        ["journalctl", "-u", SERVICE, "--since", since.strftime("%Y-%m-%d %H:%M:%S"),
         "--no-pager", "-o", "cat"],
        text=True, capture_output=True, timeout=60,
    )
    return sum(1 for line in result.stdout.splitlines() if line.startswith("Traceback"))


def _last_rowid() -> int:
    try:
        return int(json.loads(STATE_PATH.read_text()).get("max_rowid", 0))
    except (OSError, ValueError):
        return 0


def _remember_rowid(value: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"max_rowid": value, "written_at": datetime.now(timezone.utc).isoformat()}))


def night_output(since: datetime) -> tuple[dict[str, tuple[int, int]], int]:
    """Ulke -> (islenen, qualified) ve son rapordan beri eklenen yeni lead sayisi."""
    conn = sqlite3.connect(f"file:{LEADS_DB}?mode=ro", uri=True, timeout=30)
    try:
        stamp = since.strftime("%Y-%m-%d %H:%M:%S")
        per_country: dict[str, tuple[int, int]] = {}
        for country, processed, qualified in conn.execute(
            """SELECT country, COUNT(*),
                      SUM(CASE WHEN profile_status='qualified' THEN 1 ELSE 0 END)
                 FROM leads WHERE profile_checked_at >= ? GROUP BY country""",
            (stamp,),
        ):
            per_country[country or "?"] = (processed, qualified or 0)
        last = _last_rowid()
        new_leads, max_rowid = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid), ?) FROM leads WHERE rowid > ?", (last, last),
        ).fetchone()
        night_output.max_rowid = int(max_rowid or 0)  # type: ignore[attr-defined]
        return per_country, new_leads
    finally:
        conn.close()


def queue_runway() -> tuple[int, float]:
    rows = load_rows()
    with DeliveryState() as state:
        attempted = state.attempted_emails()
        today = time.strftime("%Y-%m-%d")
        sent_today = state.claimed_on(today)
    unsent = sum(1 for row in rows if normalize_email(row["email"]) not in attempted)
    return unsent, unsent / DAILY_SEND, sent_today  # type: ignore[return-value]


def selfcheck_problems() -> list[str]:
    result = subprocess.run(
        [sys.executable, str(ORCHESTRATOR), "--selfcheck"],
        cwd=SCRAPER, text=True, capture_output=True, timeout=120,
    )
    return [line[len("SORUN: "):] for line in result.stdout.splitlines()
            if line.startswith("SORUN: ")] + (
        [f"selfcheck cikis kodu {result.returncode}: {result.stderr.strip()[:200]}"]
        if result.returncode and "SORUN:" not in result.stdout else [])


def build_report(now: datetime) -> tuple[bool, str, str]:
    since = now - timedelta(hours=NIGHT_HOURS)
    alarms: list[str] = []

    per_country, new_leads = night_output(since)
    processed = sum(p for p, _ in per_country.values())
    qualified = sum(q for _, q in per_country.values())
    if processed == 0:
        alarms.append("gece boyunca HIC firma islenmedi")

    svc = systemctl_show("NRestarts", "ActiveState", "Result", "ExecMainStartTimestamp")
    restarts = int(svc.get("NRestarts") or 0)
    if restarts:
        alarms.append(f"servis {restarts} kez yeniden basladi")
    if svc.get("Result") not in ("", "success"):
        alarms.append(f"servis sonucu: {svc.get('Result')}")
    tracebacks = journal_tracebacks(since)
    if tracebacks:
        alarms.append(f"journal'da {tracebacks} Traceback")

    problems = selfcheck_problems()
    if problems:
        alarms.append("selfcheck basarisiz: " + "; ".join(problems[:3]))

    unsent, runway, sent_today = queue_runway()
    if runway < MIN_RUNWAY_DAYS:
        alarms.append(f"kuyruk {runway:.1f} gunluk kaldi (esik {MIN_RUNWAY_DAYS:g})")

    lines = [
        f"Gece penceresi: {since:%d %b %H:%M} - {now:%d %b %H:%M} UTC",
        "",
        f"Islenen firma : {processed}",
        f"Uygun (qualified): {qualified}"
        + (f"  (%{100 * qualified / processed:.0f})" if processed else ""),
        f"Yeni lead (son rapordan beri, tum kaynaklar): {new_leads}",
        "",
        "Ulke bazinda (islenen / uygun / agirlik):",
    ]
    for country in RESEARCH_COUNTRIES:
        p, q = per_country.get(country, (0, 0))
        lines.append(f"  {country}: {p:>5} / {q:>4} / %{WEIGHTS[country]}")
    extra = {c: v for c, v in per_country.items() if c not in RESEARCH_COUNTRIES}
    if extra:
        lines.append("  diger: " + ", ".join(f"{c} {p}/{q}" for c, (p, q) in extra.items()))
    lines += [
        "",
        f"Servis: {svc.get('ActiveState', '?')} / sonuc={svc.get('Result') or '-'} "
        f"/ yeniden baslatma={restarts} / traceback={tracebacks}",
        f"Selfcheck: {'OK' if not problems else 'SORUN'}",
        *(f"  - {p}" for p in problems),
        "",
        f"Kuyruk: {unsent} denenmemis alici = {runway:.1f} gun ({DAILY_SEND}/gun)",
        f"Bugun gonderilen: {sent_today}",
    ]
    if alarms:
        lines = ["ALARM:", *(f"  ! {a}" for a in alarms), ""] + lines
    tag = "[ALARM]" if alarms else "[arastirma]"
    subject = (f"{tag} gece taramasi: {qualified} uygun / {processed} islenen, "
               f"kuyruk {runway:.1f} gun")
    return bool(alarms), subject, "\n".join(lines)


def send(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = EMAIL
    msg["To"] = EMAIL
    msg["Subject"] = subject
    msg.set_content(body)
    server = connect_smtp()
    try:
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)

    if args.preflight:
        problems = selfcheck_problems()
        if not problems:
            print("preflight OK", flush=True)
            return
        subject = f"[ALARM] tarama baslayamayacak: {len(problems)} sorun"
        body = "Selfcheck basarisiz, gece taramasi baslamayacak:\n\n" + "\n".join(
            f"  - {p}" for p in problems)
        print(body, flush=True)
        if not args.dry_run:
            send(subject, body)
        raise SystemExit(1)

    alarm, subject, body = build_report(now)
    print(subject, flush=True)
    print(body, flush=True)
    if not args.dry_run:
        send(subject, body)
        # Deneme calismasi sayaci ilerletmez; gercek rapor bir sonraki gece
        # icin baslangic noktasini kaydeder.
        _remember_rowid(getattr(night_output, "max_rowid", 0))
    raise SystemExit(1 if alarm else 0)


if __name__ == "__main__":
    main()
