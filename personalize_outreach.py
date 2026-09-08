#!/usr/bin/env python3
"""CV-uyum audit CSV'sinden kanita dayali, kisa cover email uretir.

Metinler deterministiktir ve yalnizca audit kaydindaki sirket/ilan/site
kanitlarini kullanir. Bu sayede binlerce kayitta maliyet, halusinasyon ve ayni
girdinin farkli metne donusmesi riski yoktur.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
import hashlib
import re
from pathlib import Path

from send_mails import english_subject_for

LINKEDIN = "https://www.linkedin.com/in/emin-kilic-dd58gr9cd"
GITHUB = "https://github.com/mmeekh"

COUNTRIES = {
    "IE": "Ireland", "SE": "Sweden", "DK": "Denmark", "NO": "Norway",
    "MT": "Malta", "NL": "the Netherlands", "DE": "Germany",
    "GB": "the United Kingdom", "CA": "Canada", "NZ": "New Zealand",
    "AU": "Australia", "SG": "Singapore", "AE": "the UAE", "QA": "Qatar",
    "CH": "Switzerland", "LU": "Luxembourg", "FI": "Finland",
    "PT": "Portugal",
}

AREA_LABELS = {
    "accounting": "accounting operations",
    "accounts payable": "accounts payable workflows",
    "bookkeeping": "digital bookkeeping",
    "financial reporting": "financial reporting",
    "finance operations": "finance operations",
    "tax": "tax operations",
    "payroll": "payroll workflows",
    "payments": "payment operations",
    "fintech": "financial technology",
    "financial services": "financial services",
    "automation": "workflow automation",
    "process automation": "process automation",
    "artificial intelligence": "AI-enabled workflows",
    "llm": "LLM-enabled automation",
    "digital transformation": "digital transformation",
    "python": "Python systems",
    "fastapi": "FastAPI services",
    "backend": "backend platforms",
    "rest api": "API integrations",
    "docker": "containerised services",
    "saas": "SaaS products",
    "data analytics": "data analytics",
    "business intelligence": "business intelligence",
    "power bi": "reporting automation",
    "data platform": "data platforms",
    "dashboard": "operational dashboards",
    "reporting automation": "reporting automation",
    "data pipeline": "data pipelines",
    "accounting software": "accounting software",
    "finance platform": "finance platforms",
    "payment platform": "payment platforms",
    "expense management": "expense management",
    "open banking": "open banking",
    "erp software": "ERP software",
    "financial automation": "financial automation",
}

SENIOR = ("senior", "staff", "principal", "lead ", "manager", "director", "head of", "vp ")
# 8 Eyl 2026: "I noticed X's <job> opening" cumlesi firmanin HERHANGI bir ilanini
# aliyordu; Rentman'a "Senior Frontend Developer", Alphatron'a "IT Solutions
# Engineer" ilanini gordugumuzu yazdik. Finans basvurusunda yazilim ilanina atif
# toplu mail izlenimi veriyor. Artik yalnizca profile uyan basliklar anilir;
# uymuyorsa cumle genel gozleme duser.
RELEVANT_ROLE_TERMS = (
    "financ", "account", "bookkeep", "controller", "controlling", "audit", "tax",
    "payroll", "treasury", "billing", "invoic", "credit control", "payable",
    "receivable", "administrat", "admin", "office manager", "back office",
    "reporting", "fp&a", "analyst", "compliance", "reconcil",
    # Metin Excel/VBA ve Python ile surec otomasyonunu profilin parcasi olarak
    # sunuyor; otomasyon/veri ilanlari bu yuzden anilabilir.
    "automation", "data ",
)


MARKETING_TERMS = ("leverage", "synergy", "best-in-class", "industry-leading", "seamless",
                   "integrate", "your business", "solutions for", "learn more")


def relevant_role(title: str) -> bool:
    """Only openings a finance/administration candidate can plausibly apply to.

    A real job title is short. The scraper sometimes captures a product page
    heading instead ("Trade Compliance Content for Business Systems Leverage
    industry-leading content..."); those are rejected by length and by
    marketing vocabulary before the relevance check, so a stray "compliance"
    cannot smuggle a sales blurb into the opening line.
    """
    low = title.casefold().strip()
    if len(low) > 60 or len(low.split()) > 7:
        return False
    if any(term in low for term in MARKETING_TERMS):
        return False
    return any(term in low for term in RELEVANT_ROLE_TERMS)


def _split(value: str, separator: str) -> list[str]:
    return [part.strip() for part in (value or "").split(separator) if part.strip()]


def _country(row: dict[str, str]) -> str:
    tag = (row.get("oncelik") or "").upper()
    code = tag[:-3] if tag.endswith("-EN") else tag
    return COUNTRIES.get(code, row.get("sehir") or "Europe")


def _best_job(row: dict[str, str]) -> str:
    titles = [title for title in _split(row.get("job_titles", ""), "|") if relevant_role(title)]
    non_senior = [title for title in titles if not any(word in title.casefold() for word in SENIOR)]
    choice = (non_senior or titles)
    return choice[0][:90] if choice else ""


def _areas(row: dict[str, str]) -> list[str]:
    keywords = _split(row.get("fit_keywords", ""), ",")
    labels: list[str] = []
    for keyword in keywords:
        label = AREA_LABELS.get(keyword.casefold())
        if label and label not in labels:
            labels.append(label)
    return labels[:2]


def _observation(row: dict[str, str]) -> str:
    company = row["firma"].strip()
    job = _best_job(row)
    city = (row.get("sehir") or "").split(",", 1)[0].strip()
    if job:
        location = f" in {city}" if city else ""
        return f"I noticed {company}'s {job} opening{location}."
    areas = _areas(row)
    if len(areas) >= 2:
        return f"Your work across {areas[0]} and {areas[1]} caught my attention."
    if areas:
        return f"Your work in {areas[0]} caught my attention."
    tracks = set(_split(row.get("fit_tracks", ""), ","))
    if "finance_ops" in tracks:
        return f"The way {company} approaches finance operations caught my attention."
    return f"The systems work behind {company}'s services caught my attention."


def _proofs(row: dict[str, str]) -> list[str]:
    tracks = set(_split(row.get("fit_tracks", ""), ","))
    proofs: list[str] = []
    if tracks & {"automation_ai", "finance_software"}:
        proofs.append(
            "I build workflow automation and backend tools with Python/FastAPI, SQL, Docker "
            "and GitHub Actions."
        )
    if "backend_platform" in tracks and not (tracks & {"automation_ai", "finance_software"}):
        proofs.append(
            "I also build production systems with Python/FastAPI, Go, Docker and GitHub Actions, "
            "including a self-hosted automation platform."
        )
    if "data_bi" in tracks and len(proofs) < 2:
        proofs.append(
            "I also use Excel and VBA macros to turn operational and financial data into clear, useful reporting."
        )
    if "finance_ops" in tracks and len(proofs) < 2:
        proofs.append(
            "My finance background covers accounts payable, reconciliations, FX calculations and "
            "GAAP-aligned month-end closing."
        )
    return proofs[:2]


def _roles(row: dict[str, str]) -> str:
    tracks = set(_split(row.get("fit_tracks", ""), ","))
    if "finance_software" in tracks or ({"finance_ops", "automation_ai"} <= tracks):
        return "finance systems, automation or operations technology"
    if "backend_platform" in tracks and "data_bi" in tracks:
        return "backend, automation or data roles"
    if "backend_platform" in tracks:
        return "backend or automation roles"
    if "data_bi" in tracks:
        return "data, BI or reporting automation roles"
    return "finance operations or reporting roles"


def _subject(row: dict[str, str]) -> str:
    tracks = set(_split(row.get("fit_tracks", ""), ","))
    if "finance_software" in tracks:
        return "finance systems"
    if "automation_ai" in tracks:
        return "workflow automation"
    if "data_bi" in tracks:
        return "reporting systems"
    if "backend_platform" in tracks:
        return "backend automation"
    return "finance operations"


def generate(row: dict[str, str]) -> tuple[str, str]:
    company = row["firma"].strip()
    body = f"""Dear {company},

I am writing to apply for a full-time position in finance or administration at {company}.

{_observation(row)}

I'm Emin, an economics graduate with around three years of experience in accounting and reporting: accounts payable, reconciliations, month-end closing, and Excel-based reporting. I am used to working to deadlines on client books and to keeping the documentation in order.

I also automate the repetitive parts of that work with Excel/VBA and Python. That is part of how I do the job rather than something separate: less time on manual data entry, more time on the work that needs judgement.

What I am looking for is one long-term, full-time role where I can take on day-to-day financial administration and grow with the team.

I would be happy to have a short introductory call if my profile could be relevant to your team.

I've also attached my CV for context.

I consent to you keeping my CV on file for future suitable opportunities.

Kind regards,
Emin Kilic
+90 552 942 88 39
LinkedIn: {LINKEDIN}
GitHub: {GITHUB}
"""
    return english_subject_for(company), body


def evidence_hash(row: dict[str, str]) -> str:
    payload = "|".join(row.get(key, "") for key in (
        "email", "firma", "fit_score", "fit_tracks", "fit_keywords", "job_titles",
        "email_source_url", "career_url",
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate(row: dict[str, str], subject: str, body: str) -> None:
    if subject != english_subject_for(row["firma"]):
        raise ValueError(f"gecersiz subject: {subject!r}")
    if row["firma"].strip() not in body:
        raise ValueError("firma adi govdede yok")
    # Metin bir is basvurusu olarak okunmali. Onceki surum bedava "proof of
    # concept" teklif ediyordu ve alicilar bunu otomasyon satis girisimi sandi.
    if body.count("I am writing to apply for a full-time position") != 1:
        raise ValueError("cover email tam bir basvuru acilisi icermeli")
    satis = [ifade for ifade in ("at no cost", "proof of concept", "free of charge",
                                 "i can build", "i will build", "our services")
             if ifade in body.casefold()]
    if satis:
        raise ValueError(f"basvuru metninde hizmet teklifi dili var: {satis}")
    if body.count("I've also attached my CV for context.") != 1:
        raise ValueError("CV eki ifadesi tam bir kez bulunmali")
    if body.count("I consent to you keeping my CV on file for future suitable opportunities.") != 1:
        raise ValueError("CV saklama izni tam bir kez bulunmali")
    if LINKEDIN not in body or GITHUB not in body:
        raise ValueError("HTTPS profil linkleri eksik")
    if not (row.get("email_source_url") or "").startswith("https://"):
        raise ValueError("e-posta kaynak kaniti HTTPS degil")
    if re.search(r"\{[a-z_]+\}", body):
        raise ValueError("doldurulmamis alan var")
    forbidden = ("i hope this email finds you well", "synergy", "leverage", "best-in-class")
    # A legitimate company name such as "sc synergy GmbH" must not be treated
    # as templated marketing language.  This check applies to our copy, not
    # the recipient's name inserted into it.
    style_body = body.casefold().replace(row["firma"].casefold(), "")
    if any(term in style_body for term in forbidden):
        raise ValueError("yasakli cold-email kalibi bulundu")
    body_folded = body.casefold()
    relocation_intent = (
        "preparing to relocate",
        "planning to relocate",
        "planning to move",
        "relocate my career",
    )
    if any(phrase in body_folded for phrase in relocation_intent):
        raise ValueError("ilk temasta tasinma ifadesi kullanilamaz")
    if "i am writing to apply for a full-time position" not in body_folded:
        raise ValueError("onayli acilis eksik")
    if "clemta" in body_folded or "acun media" in body_folded:
        raise ValueError("ilk temas metninde onceki isveren adi kullanilamaz")
    words = len(re.findall(r"\b[\w'-]+\b", body))
    if not 135 <= words <= 235:
        raise ValueError(f"uygunsuz uzunluk: {words} kelime")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="ultra audit CSV")
    parser.add_argument("output", help="sender tarafindan okunacak personalization CSV")
    parser.add_argument("--preview", default="", help="ilk 10 metin icin Markdown preview")
    args = parser.parse_args()
    rows = list(csv.DictReader(Path(args.input).open(encoding="utf-8")))
    generated: list[dict[str, str]] = []
    # 6 Eyl 2026: tek bir uygunsuz satir (ornegin HTTPS olmayan kanit adresi)
    # butun partiyi cokertiyordu; 1.842 adayin tamami tek bir http:// yuzunden
    # yayinlanamadi. Dogrulama kurallari aynen duruyor, sadece basarisiz satir
    # atlaniyor: kisisellestirmesi olmayan satiri yayinci zaten "missing
    # personalization" diyerek eliyor.
    skipped: Counter[str] = Counter()
    for row in rows:
        try:
            subject, body = generate(row)
            validate(row, subject, body)
        except ValueError as exc:
            skipped[str(exc)[:80]] += 1
            continue
        generated.append({
            "email": row["email"].strip().casefold(),
            "firma": row["firma"].strip(),
            "subject": subject,
            "body": body,
            "evidence_hash": evidence_hash(row),
            "fit_score": row.get("fit_score", ""),
            "email_source_url": row.get("email_source_url", ""),
        })
    fields = ("email", "firma", "subject", "body", "evidence_hash", "fit_score", "email_source_url")
    with Path(args.output).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(generated)
    if args.preview:
        sections = []
        for item in generated[:10]:
            sections.append(
                f"## {item['firma']}\n\n**Subject:** {item['subject']}\n\n{item['body']}"
            )
        Path(args.preview).write_text("\n\n---\n\n".join(sections) + "\n", encoding="utf-8")
    print(f"{len(generated)} kanitli cover email -> {args.output}")
    if skipped:
        print(f"  atlanan: {sum(skipped.values())}")
        for reason, count in skipped.most_common(8):
            print(f"    {count:>5}  {reason}")
    # Hicbir satir gecmiyorsa sorun veride degil sablondadir; sessizce bos
    # CSV uretip yayin turunu basarili saymayalim.
    if rows and not generated:
        raise SystemExit("hicbir satir dogrulamayi gecemedi; sablonu kontrol edin")


if __name__ == "__main__":
    main()
