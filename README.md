# Job Application Outreach

![A queue of messages passes through a locked checkpoint that deflects duplicates, so each destination receives exactly one](docs/banner.png)

A personal outreach system for a multi-country job search: it renders a tailored application email per company, attaches a CV, and delivers it under strict guarantees — **no recipient is ever contacted twice**, even across crashes, restarts, or concurrent runs.

Built because sending a few hundred applications by hand is slow, and sending them naively is worse: a duplicate application reads as spam and costs you the opportunity.

## Delivery guarantees

The hard part of outreach automation is not sending mail, it is *never sending the same mail twice*. This system treats that as a correctness problem:

| Risk | Mitigation |
|---|---|
| Crash between send and bookkeeping | The delivery claim is committed to SQLite **before** SMTP DATA. A restart sees the claim and skips. |
| Two processes running at once | Single OS-level `flock`; a second process exits immediately. The kernel releases the lock if a process dies. |
| Ambiguous SMTP outcome | Marked `uncertain` and **never retried automatically** — a human decides. |
| Same company reached twice via two addresses | Uniqueness enforced on both email **and** organization (country + domain). |
| Shared mailbox providers (Gmail, GMX) | Organizations on shared providers are keyed by address, so distinct firms never collapse into one record. |
| Sending to someone who asked to stop | A mandatory exclusion list is applied *before* the queue is built; the run fails closed if the file is missing. |

A preflight check validates the whole queue — templates, routing, attachment, duplicates — before a single message goes out.

## Sending behaviour

- Per-country routing: each recipient's message names their country and city correctly.
- Randomised delays between messages and a configurable daily cap.
- A send window (working hours in a chosen timezone); the run stops mid-batch when the window closes and resumes the next day.
- Language templates are separated, and a guard rejects a message rendered with the wrong language for its route.

## Usage

```bash
export OUTREACH_EMAIL="you@example.com"
export OUTREACH_PASSWORD_FILE="/path/to/app-password.txt"   # SMTP app password

python3 send_mails.py --dry-run     # show what would be sent
python3 send_mails.py --test        # send samples to yourself
python3 daily_batch.py 100          # one capped batch (cron entry point)
python3 test_automation.py          # safety test suite
```

Input is a CSV of companies (`oncelik,firma,sehir,email,site,dil_notu`) plus an exclusion list. Neither is included here — see below.

## What is not in this repository

Company contact data, the delivery log, the delivery-state database and the CV are all git-ignored. This repository contains the mechanism, never the recipients.

## Responsible use

This is a tool for sending a personal job application to an employer, once. It is deliberately built so that repeat contact is impossible, and it honours an exclusion list on every run. It is not built for, and should not be used for, bulk commercial mail.
