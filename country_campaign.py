"""Shared personal campaign targets and restart-safe, balanced batch selection."""
from collections import Counter, deque

from send_mails import normalize_email, route_for

CAMPAIGN = "five-countries-20260905"
# 6 Eyl 2026: Almanya kapatildi (3.053 alici tamamlandi, kuyrukta DE kalmadi).
# Yerine, veritabaninda halihazirda derinligi olan Avrupa ulkeleri alindi.
# Kampanya etiketi bilerek degismedi: yayinlanmis 588 aliciyi kohorttan
# dusurup yeniden siraya sokmanin faydasi yok.
#
# MT ve LU kaynak olarak tukendi (sirasiyla 278 ve 1.053 lead). Yeni ARASTIRMA
# almazlar, ama COUNTRIES'te kalirlar: kuyrukta zaten dogrulanip yayinlanmis
# 51 Malta/Luksemburg alicisi var, onlari ulasilamaz hale getirmeyiz.
COUNTRIES = ("IE", "PL", "NL", "GB", "FI", "AT", "BE", "PT", "SE", "NO", "CH",
             "MT", "LU")
EXHAUSTED = ("MT", "LU")
RESEARCH_COUNTRIES = tuple(c for c in COUNTRIES if c not in EXHAUSTED)
# 6 Eyl 2026: ulke basina 900 tavani kaldirildi. NL tam 900'e dayanmisti ve
# yayina hazir 429 adayin 410'u "country research target already published"
# diye eleniyordu. Hedef artik 2 haftalik gonderim hacmi (14 x 450).
CAMPAIGN_TOTAL_TARGET = 6300
TARGET_PER_COUNTRY = CAMPAIGN_TOTAL_TARGET
# Onaylanan ulke dagilimi buraya yazilir; bos birakilan ulke ortak tavani alir.
TARGET_BY_COUNTRY: dict[str, int] = {}


def target_for(country: str) -> int:
    return TARGET_BY_COUNTRY.get(country, TARGET_PER_COUNTRY)
DAILY_PER_COUNTRY = 90
# Yayinci ve arastirmaci ayni uygunluk esigini kullanir; scraper tarafindaki
# profile_fit.QUALIFY_MIN_SCORE ile ayni degerde tutulmalidir.
MIN_FIT_SCORE = 25
DAILY_TOTAL = DAILY_PER_COUNTRY * len(COUNTRIES)
MARKER = f"campaign={CAMPAIGN}"


def in_cohort(row):
    return MARKER in [part.strip() for part in (row.get("dil_notu") or "").split(";")]


def accepted_counts(rows):
    """Count newly published cohort members, including already delivered rows."""
    counts = Counter()
    seen = set()
    for row in rows:
        if not in_cohort(row):
            continue
        email = normalize_email(row.get("email", ""))
        if email and email not in seen:
            country, _ = route_for(row)
            counts[country] += 1
            seen.add(email)
    return counts


def claimed_counts(state, day, rows, *, cohort_only=False):
    # Legacy log imports have an empty country_code. Resolve those against the
    # queue so restarting cannot silently restore their country allowance.
    routes = {normalize_email(row["email"]): route_for(row)[0] for row in rows}
    cohort = {normalize_email(row["email"]) for row in rows if in_cohort(row)}
    counts = Counter()
    for email, country in state.db.execute(
        "SELECT email_key,country_code FROM deliveries WHERE substr(claimed_at,1,10)=?",
        (day,),
    ):
        if cohort_only and email not in cohort:
            continue
        counts[country or routes.get(email, "")] += 1
    return counts


def balanced_batch(rows, claimed, limit):
    """Interleave countries least-served-first, in two passes.

    Claims (including uncertain SMTP results) consume quota permanently. A
    partial run catches the other countries up on restart before progressing.
    Equal *successful delivery* is not promised when a remote server fails.

    6 Eyl 2026 duzeltmesi: tavan `min(counts[c] + len(buckets[c]) for c in
    COUNTRIES)` ile hesaplaniyordu, yani en fakir ulke butun kampanyayi
    kilitliyordu. Malta'da 22 aday kalinca gunluk tavan 5x22=110'a dustu ve
    IE/PL/NL'deki yuzlerce hazir alici beklemede kaldi. Artik esitlik ilk
    turda korunur (ulke basina DAILY_PER_COUNTRY'ye kadar), tukenen ulkeler
    sadece kendi siralarini kaybeder; limit hala dolmadiysa ikinci tur
    kalan kotayi yine en az hizmet almis ulkeden baslayarak dagitir.
    """
    buckets = {country: deque() for country in COUNTRIES}
    seen = set()
    for row in rows:
        country, _ = route_for(row)
        email = normalize_email(row["email"])
        if country in buckets and email not in seen:
            buckets[country].append(row)
            seen.add(email)
    counts = Counter(claimed)
    selected = []
    for ceiling in (DAILY_PER_COUNTRY, None):
        while len(selected) < max(0, limit):
            available = [country for country in COUNTRIES
                         if buckets[country]
                         and (ceiling is None or counts[country] < ceiling)]
            if not available:
                break
            country = min(available, key=lambda c: counts[c])
            selected.append(buckets[country].popleft())
            counts[country] += 1
    return selected


def old_first_batch(rows, claimed, limit):
    """Drain reviewed historical rows before the new country experiment."""
    old = [row for row in rows if not in_cohort(row)]
    selected = old[:max(0, limit)]
    if len(selected) < limit:
        selected.extend(balanced_batch(
            [row for row in rows if in_cohort(row)], claimed, limit - len(selected)))
    return selected
