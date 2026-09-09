"""Shared personal campaign targets and restart-safe, balanced batch selection."""
import math
from collections import Counter, deque

from send_mails import normalize_email, route_for

# 6 Eyl 2026: yeni etiket bilerek verildi. Kuyrukta duran 2.785 alici eski
# etiketi tasidigi icin `in_cohort` onlara False doner; `old_first_batch`
# once onlari bosaltir (gunde 450, CSV sirasinda) ve ancak kuyruk bittiginde
# asagidaki yuzdeli dagitim devreye girer. Kullanici karari: "once siradaki
# ilanlar bitsin, sonra bu yuzdelerle gonderelim".
CAMPAIGN = "europe-english-20260906"
# Almanya 6 Eyl 2026'da kapatildi: 3.053 alici tamamlandi, kuyrukta DE kalmadi.
# Hedef, 2 haftalik gonderim hacmi (14 gun x 450).
CAMPAIGN_TOTAL_TARGET = 6300
COUNTRIES = ("GB", "IE", "PL", "NL", "PT", "MT", "FI", "SE", "NO",
             "LU", "CH", "AT", "BE")
# Yeni arastirma yalnizca agirligi olan ulkelere gider. LU/CH/AT/BE
# COUNTRIES'te kalir cunku kuyrukta yayinlanmis alicilari var; yenisi aranmaz.
# CH: AB disi vatandaslar icin kota cok sikci. AT/BE: yerel firmalarda
# Almanca/Fransizca sart, havuz da 71 ve 61 lead. MT/LU OSM'de tukendi ama
# MT'nin kalitesi yuksek (Ingilizce resmi dil), Wikidata'dan beslenebilir.
#
# Agirliklar 6 Eyl 2026 kullanici onayi. Olcut "Ingilizce konusan firma"
# degil, "beni ise alabilecek firma": Turk vatandasi olarak her AB ulkesinde
# calisma izni gerekiyor, bu Ingilizceden daha sert bir filtre.
#   GB 35 - ana dil + 12.696 sektore uygun lisansli sponsorun 11.100'u hic
#           kullanilmamis; sponsor lisansi zaten "AB disindan alabilirim" demek
#   IE 20 - ana dil, AB ici, 1.338 uygun leade karsilik sadece 323 mail gitmis
#   PL 15 - Krakow/Varsova servis merkezlerinde calisma dili Ingilizce,
#           izin esigi dusuk, 6.722 leade karsilik 151 mail
#   NL 15 - en yuksek Ingilizce yeterliligi ama 1.711 mail ile en doygun
#           ikinci ulke; bilerek sinirlandi, yalnizca taninmis sponsorlar
#   PT  6 - Lizbon/Porto servis merkezi buyumesi, calisma dili Ingilizce
#   MT  4 - Ingilizce resmi dil, finans/denetim agirlikli
#   FI/SE/NO 5 - cok yuksek Ingilizce, temiz havuzlar; derinlige gore bolundu
WEIGHTS = {"GB": 35, "IE": 20, "PL": 15, "NL": 15, "PT": 6, "MT": 4,
           "FI": 3, "SE": 1, "NO": 1}
# Agirligi buyuk olan once arastirilir. Ayri bir sira listesi TUTULMAZ:
# 6-7 Eyl gecesi orkestratordeki RESEARCH_ORDER bu kumeyle uyusmuyordu
# (MT yoktu, kapatilan CH/AT/BE vardi) ve `.index()` ValueError firlatip
# servisi 398 kez cokerterek butun geceyi bosa harcadi.
RESEARCH_COUNTRIES = tuple(sorted(WEIGHTS, key=lambda c: (-WEIGHTS[c], c)))
TARGET_PER_COUNTRY = CAMPAIGN_TOTAL_TARGET
TOTAL_WEIGHT = sum(WEIGHTS.values())
# 2 haftalik arastirma hedefi ayni yuzdelerden turer; agirligi olmayan ulke
# yeni aday almaz (kuyruktaki eski alicilari etkilenmez).
TARGET_BY_COUNTRY = {country: round(CAMPAIGN_TOTAL_TARGET * weight / TOTAL_WEIGHT)
                     for country, weight in WEIGHTS.items()}


def target_for(country: str) -> int:
    return TARGET_BY_COUNTRY.get(country, 0)


def daily_share(country: str, limit: int) -> int:
    """Bir gunluk partide bu ulkeye dusen ust sinir."""
    return math.ceil(limit * WEIGHTS.get(country, 0) / TOTAL_WEIGHT)
DAILY_PER_COUNTRY = 90
# Yayinci ve arastirmaci ayni uygunluk esigini kullanir; scraper tarafindaki
# profile_fit.QUALIFY_MIN_SCORE ile ayni degerde tutulmalidir.
MIN_FIT_SCORE = 25
# 8 Eyl 2026 kullanici karari: "ready to send" QUEUE_LOW'a dusunce tarama
# kendiliginden baslar ve kuyruk QUEUE_HIGH'a ulasinca durur. Gece penceresi
# QUEUE_HIGH'in altindaysa zaten calisir; gunduz yalnizca QUEUE_LOW'a inince
# devreye girer (VPS'i gereksiz yormamak icin).
QUEUE_LOW = 450
QUEUE_HIGH = 900
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


# 9 Eyl 2026 kullanici karari: gunluk parti once canli ve profile uygun ilani
# olan firmalara, sonra digerlerine gitsin; kucuk buro/danismanlik en sona.
# Olculen sebep: ATS kaynakli (ilanli) firmalarda sicak cevap %2,0, kucuk
# burolarda %0,2 (8.093 mail). Siralama ulke agirliklarini bozmaz: her ulkenin
# kuyrugu kendi icinde katmanlanir.
SMALL_OFFICE_TERMS = (
    "accountant", "accounting", "boekhoud", "administratie", "advies", "adviseur",
    "belasting", "steuer", "tax", "bookkeep", "chartered", "& co", "and co", "partners",
    "llp", "consultancy", "consulting", "kantoor", "fiscal", "fiscaal", "salaris",
    "payroll", "bureau",
)
TIER_POSTING, TIER_OTHER, TIER_SMALL = 0, 1, 2
TIER_LABELS = {TIER_POSTING: "ilanli", TIER_OTHER: "diger", TIER_SMALL: "kucuk buro"}


def is_small_office(firm: str) -> bool:
    low = (firm or "").casefold()
    return any(term in low for term in SMALL_OFFICE_TERMS)


def tier_of(row, has_posting: bool) -> int:
    if has_posting:
        return TIER_POSTING
    return TIER_SMALL if is_small_office(row.get("firma", "")) else TIER_OTHER


def prioritise_rows(rows, posting_emails: set[str]):
    """Stable sort: posting firms first, small offices last; order within a tier kept."""
    return sorted(rows, key=lambda row: tier_of(row, normalize_email(row.get("email", "")) in posting_emails))


def balanced_batch(rows, claimed, limit):
    """Ulkeleri onaylanan yuzdelere gore, iki turda dagit.

    Claims (including uncertain SMTP results) consume quota permanently. A
    partial run catches the other countries up on restart before progressing.
    Equal *successful delivery* is not promised when a remote server fails.

    6 Eyl 2026 duzeltmesi: tavan `min(counts[c] + len(buckets[c]) for c in
    COUNTRIES)` ile hesaplaniyordu, yani en fakir ulke butun kampanyayi
    kilitliyordu. Malta'da 22 aday kalinca gunluk tavan 5x22=110'a dustu ve
    IE/PL/NL'deki yuzlerce hazir alici beklemede kaldi.

    Ayni tarihte esit dagitim yerine WEIGHTS yuzdeleri geldi. Ilk turda her
    ulke gunluk payina kadar alir ve sira, payina gore en geride kalan ulkeye
    gider (counts/weight en kucuk). Adayi biten ulke yalnizca kendi sirasini
    kaybeder. Limit hala dolmadiysa ikinci tur pay tavanini kaldirir ve
    agirligi olmayan ulkeleri de dahil eder; boylece kuyrukta kalmis bir alici
    (ornegin kapatilan LU/CH/AT/BE) mahsur kalmaz.
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
    for weighted in (True, False):
        while len(selected) < max(0, limit):
            available = [country for country in COUNTRIES
                         if buckets[country]
                         and (not weighted
                              or (WEIGHTS.get(country, 0)
                                  and counts[country] < daily_share(country, limit)))]
            if not available:
                break
            if weighted:
                country = min(available,
                              key=lambda c: (counts[c] / WEIGHTS[c], COUNTRIES.index(c)))
            else:
                country = min(available, key=lambda c: (counts[c], COUNTRIES.index(c)))
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
