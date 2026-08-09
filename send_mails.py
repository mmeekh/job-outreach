#!/usr/bin/env python3
"""Safe multi-country job-application email sender.

Safety properties:
- exclusions.csv is mandatory and is applied before queue construction;
- recipient email and organization are unique in the active queue;
- a SQLite claim is committed before SMTP DATA, so restarts cannot duplicate mail;
- ambiguous SMTP outcomes become ``uncertain`` and are never retried automatically;
- all sender processes share one OS-level lock;
- every outreach message is rendered from the English template.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import os
import random
import re
import smtplib
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
# Kimlik ve sir yolu ortam degiskeniyle degistirilebilir; verilmezse mevcut
# kurulumun degerleri kullanilir (calisan cron bozulmasin diye).
EMAIL = os.environ.get("OUTREACH_EMAIL", "muhammeteminkilic012@gmail.com")
DISPLAY_NAME = os.environ.get("OUTREACH_NAME", "Emin Kilic")
CAMPAIGN_ID = "emin-job-outreach-2026-08"

CV_PATH = BASE / "001-Emin-Kilic-CV.pdf"
CSV_PATH = BASE / "firmalar.csv"
EXCLUSIONS_PATH = BASE / "exclusions.csv"
LOG_PATH = BASE / "sent-log.csv"
STATE_PATH = BASE / "delivery-state.sqlite3"
LOCK_PATH = BASE / ".delivery.lock"
PASSWORD_PATH = Path(os.environ.get(
    "OUTREACH_PASSWORD_FILE", "/root/secrets/gmail-emin-app-password.txt"))

LINKEDIN_URL = "https://www.linkedin.com/in/emin-kilic-dd58gr9cd"
GITHUB_URL = "https://github.com/mmeekh"
# 400 mail 9 saatlik pencereye sigmali: ortalama gecikme <= 81sn olmali
MIN_DELAY, MAX_DELAY = 40, 110

NON_NL_FORBIDDEN = (
    "open sollicitatie",
    "met vriendelijke groet",
    "beste team van",
    "naar nederland",
    "boekhouding",
    "maandafsluitingen",
)


class AlreadyRunningError(RuntimeError):
    pass


class DeliveryUncertainError(RuntimeError):
    pass


def normalize_email(value: str) -> str:
    return (value or "").strip().casefold()


def route_for(row: dict[str, str]) -> tuple[str, str]:
    tag = (row.get("oncelik") or "").strip().upper()
    # Campaign policy: every recipient gets English, regardless of legacy tag.
    if tag in {"1-TURK", "1-TURK-RISKLI", "2-EN", "3-NL"}:
        return "NL", "en"
    if tag in {"DE-EN", "DE-TURK"}:
        return "DE", "en"
    if tag == "PL-EN":
        return "PL", "en"
    if tag == "IE-EN":
        return "IE", "en"
    if tag == "SE-EN":
        return "SE", "en"
    if tag == "DK-EN":
        return "DK", "en"
    if tag == "LU-EN":
        return "LU", "en"
    if tag == "MT-EN":
        return "MT", "en"
    if tag == "NO-EN":
        return "NO", "en"
    # scraper ile eklenen ulkeler: "<ULKE>-EN" kalibi
    generic = {"BG", "RO", "CZ", "SK", "HU", "HR", "SI", "EE", "LV", "LT",
               "PT", "ES", "IT", "GR", "CY", "FI", "AT", "BE", "CH", "TH", "VN",
               "NL", "DE", "PL", "IE", "SE", "DK", "LU", "MT", "NO", "EU"}
    if tag.endswith("-EN") and tag[:-3] in generic:
        return tag[:-3], "en"
    if tag in {"GULF-EN", "GULF-TURK"}:
        city = (row.get("sehir") or "").casefold()
        return ("QA" if "qatar" in city else "AE"), "en"
    # Turkiye ici basvurular: tasinma yok, mesaj Turkce
    if tag in {"TR-YARD", "TR-INSAAT"}:
        return "TR", "tr-yerel"
    # kruvaziyer: gemide finans pozisyonu, ayri Ingilizce mesaj
    if tag == "CRUISE-EN":
        return "CRUISE", "en-cruise"
    raise ValueError(f"bilinmeyen veya guvensiz rota: {tag!r}")


def _site_host(row: dict[str, str]) -> str:
    site = (row.get("site") or "").strip().casefold()
    if site in {"", "-", "n/a", "none"}:
        return f"email:{normalize_email(row.get('email', ''))}"
    candidate = site if "://" in site else f"https://{site}"
    host = (urlparse(candidate).hostname or "").casefold()
    host = host.removeprefix("www.")
    return host or f"email:{normalize_email(row.get('email', ''))}"


def organization_key(row: dict[str, str]) -> str:
    country, _language = route_for(row)
    return f"{country}:{_site_host(row)}"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_rows() -> list[dict[str, str]]:
    """Return the reviewed active queue; fail closed on inconsistent data."""
    if not CSV_PATH.exists() or not EXCLUSIONS_PATH.exists():
        raise FileNotFoundError("firmalar.csv ve exclusions.csv birlikte bulunmali")
    excluded = {
        normalize_email(row.get("email", ""))
        for row in _read_csv(EXCLUSIONS_PATH)
        if normalize_email(row.get("email", ""))
    }
    rows = [
        row for row in _read_csv(CSV_PATH)
        if normalize_email(row.get("email", "")) not in excluded
    ]
    emails = [normalize_email(row.get("email", "")) for row in rows]
    if not all(emails):
        raise ValueError("aktif listede bos e-posta adresi var")
    duplicate_emails = [key for key, count in Counter(emails).items() if count > 1]
    if duplicate_emails:
        raise ValueError(f"aktif listede duplicate e-posta var: {duplicate_emails}")
    orgs = [organization_key(row) for row in rows]
    duplicate_orgs = [key for key, count in Counter(orgs).items() if count > 1]
    if duplicate_orgs:
        raise ValueError(f"aktif listede duplicate kurum var: {duplicate_orgs}")
    return rows


def load_template(language: str) -> tuple[str, str]:
    path = BASE / {
        "en": "template_en.txt",
        "tr": "template_tr.txt",
        "nl": "template_nl.txt",
        "tr-yerel": "template_tr_yerel.txt",
        "en-cruise": "template_cruise.txt",
    }[language]
    raw = path.read_text(encoding="utf-8")
    first, separator, body = raw.partition("\n")
    if not separator or not first.startswith("SUBJECT:"):
        raise ValueError(f"gecersiz sablon: {path.name}")
    return first.removeprefix("SUBJECT:").strip(), body.strip() + "\n"


def _country_target(country: str, language: str) -> str:
    values = {
        ("NL", "en"): "the Netherlands",
        ("NL", "nl"): "Nederland",
        ("NL", "tr"): "Hollanda'ya",
        ("DE", "en"): "Germany",
        ("DE", "tr"): "Almanya'ya",
        ("PL", "en"): "Poland",
        ("IE", "en"): "Ireland",
        ("SE", "en"): "Sweden",
        ("DK", "en"): "Denmark",
        ("LU", "en"): "Luxembourg",
        ("MT", "en"): "Malta",
        ("NO", "en"): "Norway",
        ("BG", "en"): "Bulgaria",
        ("RO", "en"): "Romania",
        ("CZ", "en"): "Czechia",
        ("SK", "en"): "Slovakia",
        ("HU", "en"): "Hungary",
        ("HR", "en"): "Croatia",
        ("SI", "en"): "Slovenia",
        ("EE", "en"): "Estonia",
        ("LV", "en"): "Latvia",
        ("LT", "en"): "Lithuania",
        ("PT", "en"): "Portugal",
        ("ES", "en"): "Spain",
        ("IT", "en"): "Italy",
        ("GR", "en"): "Greece",
        ("CY", "en"): "Cyprus",
        ("FI", "en"): "Finland",
        ("AT", "en"): "Austria",
        ("BE", "en"): "Belgium",
        ("CH", "en"): "Switzerland",
        ("TH", "en"): "Thailand",
        ("VN", "en"): "Vietnam",
        ("EU", "en"): "Europe",  # uluslararasi ajanslar: tek ulke yerine bolge
        ("AE", "en"): "the UAE",
        ("AE", "tr"): "Birleşik Arap Emirlikleri'ne",
        ("QA", "en"): "Qatar",
        ("QA", "tr"): "Katar'a",
        ("TR", "tr-yerel"): "Türkiye",
        ("CRUISE", "en-cruise"): "your fleet",
    }
    try:
        return values[(country, language)]
    except KeyError as exc:
        raise ValueError(f"desteklenmeyen ulke/dil rotasi: {country}/{language}") from exc


def _clean_city(value: str, country_target: str) -> str:
    city = re.sub(r"\s*\([^)]*\)\s*$", "", value or "")
    city = city.split("/")[0].split(",")[0].strip()
    placeholders = {"", "multiple", "nationwide", "remote", "europe", "netherlands", "germany", "poland", "ireland", "uae", "qatar"}
    return country_target if city.casefold() in placeholders else city


def _tr_locative(city: str) -> str:
    translations = {"Cologne": "Köln", "Munich": "Münih", "Nuremberg": "Nürnberg", "The Hague": "Lahey"}
    city = translations.get(city, city)
    low = city.casefold()
    vowel = next((char for char in reversed(low) if char in "aeıioöuü"), "a")
    suffix = "da" if vowel in "aıou" else "de"
    if low and low[-1] in "fstkçşhp":
        suffix = "t" + suffix[1:]
    return f"{city}'{suffix}"


def _sector_line(row: dict[str, str]) -> str:
    """Turkce yerel sablondaki sektore ozel cumle."""
    tag = (row.get("oncelik") or "").strip().upper()
    if tag == "TR-YARD":
        return ("Tersane ve denizcilik tarafında ihracat, döviz bazlı maliyet takibi ve "
                "yabancı müşteriyle yazışma öne çıkıyor; İngilizcem C1 ve raporlama "
                "tarafında rahatım, bu yüzden özellikle sizin sektörünüze başvuruyorum.")
    if tag == "TR-INSAAT":
        return ("Daha önce BL Harbert International'da uluslararası bir inşaat projesinin "
                "muhasebesinde çalıştım: cari hesaplar, döviz hesaplamaları ve GAAP'e uygun "
                "ay sonu kapanışları. Orada asıl fark yarattığım nokta şuydu: saha ile merkez "
                "arasındaki veri birleştirme işini VBA ile otomatikleştirip haftada yaklaşık "
                "9 saatlik tekrar eden işi ortadan kaldırdım. Yurt dışı proje muhasebesinde "
                "raporlamanın nerede tıkandığını biliyorum ve o tıkanıklığı açan taraf benim "
                "güçlü olduğum yer; yurt dışı görevlendirmeye de açığım.")
    return ("Şirketinizin finans süreçlerine hem günlük muhasebe işinde hem de "
            "raporlama tarafında katkı verebileceğimi düşünüyorum.")


def render_for(row: dict[str, str]) -> tuple[str, str, str, str]:
    country, language = route_for(row)
    subject, body = load_template(language)
    target = _country_target(country, language)
    city = _clean_city(row.get("sehir", ""), target)
    replacements = {
        "{firm}": row.get("firma", "").strip(),
        "{city}": city,
        "{city_loc}": _tr_locative(city),
        "{country_target}": target,
        "{sector_line}": _sector_line(row),
    }
    for marker, value in replacements.items():
        subject = subject.replace(marker, value)
        body = body.replace(marker, value)
    leftovers = re.findall(r"\{[a-z_]+\}", f"{subject}\n{body}")
    if leftovers:
        raise ValueError(f"sablonda doldurulmamis alan kaldi: {leftovers}")
    if LINKEDIN_URL not in body or GITHUB_URL not in body:
        raise ValueError("LinkedIn/GitHub HTTPS linkleri sablonda eksik")
    # Avrupa/Korfez kampanyasi tamamen Ingilizce; Turkiye ve kruvaziyer kollari
    # kendi sablonlarini kullanir (TR yerel Turkce, kruvaziyer ayri Ingilizce).
    if country not in {"TR", "CRUISE"} and language != "en":
        raise ValueError("kampanya politikasi geregi tum mesajlar Ingilizce olmali")
    if country not in {"NL", "TR"}:
        lowered = f"{subject}\n{body}".casefold()
        found = [marker for marker in NON_NL_FORBIDDEN if marker in lowered]
        if found:
            raise ValueError(f"Hollanda disi mesajda Hollandaca ifade bulundu: {found}")
    return subject, body, country, language


def build_msg(row: dict[str, str]) -> tuple[EmailMessage, str, str]:
    subject, body, country, language = render_for(row)
    message = EmailMessage()
    message["From"] = formataddr((DISPLAY_NAME, EMAIL))
    message["To"] = row["email"].strip()
    message["Reply-To"] = EMAIL
    message["Subject"] = subject
    digest = hashlib.sha256(f"{CAMPAIGN_ID}|{normalize_email(row['email'])}".encode()).hexdigest()[:32]
    message["Message-ID"] = f"<{digest}@job-outreach.local>"
    message.set_content(body)
    message.add_attachment(
        CV_PATH.read_bytes(), maintype="application", subtype="pdf", filename="Emin-Kilic-CV.pdf"
    )
    return message, country, language


def preflight(rows: list[dict[str, str]], require_password: bool = True) -> None:
    if not CV_PATH.is_file() or not CV_PATH.read_bytes().startswith(b"%PDF"):
        raise ValueError("CV PDF bulunamadi veya gecerli PDF degil")
    if require_password and (not PASSWORD_PATH.is_file() or not PASSWORD_PATH.read_text().strip()):
        raise ValueError("Gmail uygulama sifresi bulunamadi")
    if not LINKEDIN_URL.startswith("https://") or not GITHUB_URL.startswith("https://"):
        raise ValueError("profil linkleri HTTPS olmali")
    for row in rows:
        render_for(row)


def log_result(firm: str, email_addr: str, status: str) -> None:
    new_file = not LOG_PATH.exists()
    with LOG_PATH.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(["zaman", "firma", "email", "durum"])
        writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), firm, email_addr, status])


class DeliveryState:
    def __init__(self, path: Path = STATE_PATH, legacy_log: Path = LOG_PATH):
        self.path = path
        self.legacy_log = legacy_log
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> "DeliveryState":
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS deliveries (
                email_key TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                firm TEXT NOT NULL,
                country_code TEXT NOT NULL,
                language TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('claimed', 'sent', 'uncertain')),
                claimed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            )"""
        )
        self._sync_legacy_log()
        return self

    def __exit__(self, *_args) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("DeliveryState context disinda kullanildi")
        return self.connection

    def _sync_legacy_log(self) -> None:
        if not self.legacy_log.exists():
            return
        grouped: dict[str, dict[str, str]] = {}
        for row in _read_csv(self.legacy_log):
            key = normalize_email(row.get("email", ""))
            if not key:
                continue
            current = grouped.setdefault(key, row.copy())
            if row.get("durum") == "OK":
                current.update(row)
        for key, row in grouped.items():
            status = "sent" if row.get("durum") == "OK" else "uncertain"
            timestamp = row.get("zaman") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.db.execute(
                """INSERT OR IGNORE INTO deliveries
                   (email_key,email,firm,country_code,language,status,claimed_at,updated_at,detail)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (key, row.get("email", key), row.get("firma", ""), "", "", status, timestamp, timestamp,
                 f"sent-log.csv import: {row.get('durum', '')}"),
            )
            if status == "sent":
                self.db.execute(
                    "UPDATE deliveries SET status='sent', updated_at=? WHERE email_key=? AND status!='sent'",
                    (timestamp, key),
                )
        self.db.commit()

    def attempted_emails(self) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT email_key FROM deliveries")}

    def counts(self) -> dict[str, int]:
        return {status: count for status, count in self.db.execute("SELECT status, COUNT(*) FROM deliveries GROUP BY status")}

    def claimed_on(self, day: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM deliveries WHERE substr(claimed_at,1,10)=?", (day,)
        ).fetchone()[0]

    def claim(self, row: dict[str, str], country: str, language: str) -> bool:
        key = normalize_email(row["email"])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """INSERT INTO deliveries
                   (email_key,email,firm,country_code,language,status,claimed_at,updated_at,detail)
                   VALUES (?,?,?,?,?,'claimed',?,?, '')""",
                (key, row["email"].strip(), row["firma"].strip(), country, language, now, now),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False

    def mark_sent(self, email_addr: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.db.execute(
            "UPDATE deliveries SET status='sent', updated_at=?, detail='' WHERE email_key=?",
            (now, normalize_email(email_addr)),
        )
        self.db.commit()

    def mark_uncertain(self, email_addr: str, detail: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.db.execute(
            "UPDATE deliveries SET status='uncertain', updated_at=?, detail=? WHERE email_key=?",
            (now, detail[:1000], normalize_email(email_addr)),
        )
        self.db.commit()


@contextmanager
def delivery_lock():
    handle = LOCK_PATH.open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunningError("baska bir gonderim sureci zaten calisiyor") from exc
        yield handle
    finally:
        handle.close()


def connect_smtp(attempts: int = 3):
    last_error: Exception | None = None
    for attempt in range(attempts):
        server = None
        try:
            server = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
            server.starttls()
            server.login(EMAIL, PASSWORD_PATH.read_text().strip())
            return server
        except Exception as exc:
            last_error = exc
            if server is not None:
                try:
                    server.close()
                except Exception:
                    pass
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    assert last_error is not None
    raise last_error


def send_row(row: dict[str, str], state: DeliveryState) -> bool:
    message, country, language = build_msg(row)
    server = connect_smtp()  # Connection failures happen before the persistent claim.
    if not state.claim(row, country, language):
        try:
            server.quit()
        except Exception:
            server.close()
        return False
    try:
        server.send_message(message)
    except Exception as exc:
        state.mark_uncertain(row["email"], f"{type(exc).__name__}: {exc}")
        log_result(row["firma"], row["email"], f"BELIRSIZ: {type(exc).__name__}: {exc}")
        try:
            server.close()
        except Exception:
            pass
        raise DeliveryUncertainError(str(exc)) from exc
    state.mark_sent(row["email"])
    log_result(row["firma"], row["email"], "OK")
    try:
        server.quit()
    except Exception:
        pass
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--test", action="store_true")
    group.add_argument("--send", action="store_true")
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    rows = load_rows()
    filters = [value.strip().upper() for value in args.only.split(",") if value.strip()]
    if filters:
        rows = [row for row in rows if any(row["oncelik"].upper().startswith(value) for value in filters)]
    preflight(rows, require_password=not args.dry_run)

    with DeliveryState() as state:
        attempted = state.attempted_emails()
        todo = [row for row in rows if normalize_email(row["email"]) not in attempted]
        print(f"aktif {len(rows)}, daha once denenmis {len(rows)-len(todo)}, sirada {len(todo)}")
        if args.dry_run:
            for index, row in enumerate(todo, 1):
                country, language = route_for(row)
                print(f"DRY [{index}/{len(todo)}] {country}/{language} {row['firma']} <{row['email']}>")
            return
        if args.test:
            sample = dict(rows[0], email=EMAIL, firma="Test Kantoor")
            message, _country, _language = build_msg(sample)
            server = connect_smtp()
            server.send_message(message)
            server.quit()
            print("test maili kendi adresine gonderildi")
            return

    try:
        with delivery_lock(), DeliveryState() as state:
            todo = [row for row in rows if normalize_email(row["email"]) not in state.attempted_emails()]
            for index, row in enumerate(todo, 1):
                try:
                    sent = send_row(row, state)
                    result = "GONDERILDI" if sent else "ATLANDI"
                except DeliveryUncertainError:
                    result = "BELIRSIZ-TEKRARLANMAYACAK"
                except Exception as exc:
                    result = f"BAGLANTI-HATASI: {type(exc).__name__}"
                print(f"[{index}/{len(todo)}] {result}: {row['firma']} <{row['email']}>", flush=True)
                if index < len(todo):
                    time.sleep(random.randint(MIN_DELAY, MAX_DELAY))
    except AlreadyRunningError as exc:
        print(exc)


if __name__ == "__main__":
    main()
