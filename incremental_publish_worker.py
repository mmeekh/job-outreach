#!/usr/bin/env python3
"""Continuously hand off newly verified job-finder contacts to outreach.

The lead scraper may take hours to finish a large research pass. This worker
publishes a small, fully verified batch during that pass instead of waiting for
the final export. It delegates all correctness decisions to the existing
exporter, personalizer and atomic publisher; it never sends SMTP itself.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from project_paths import LOCK_DIR, LOG_DIR
from country_campaign import COUNTRIES as ACTIVE_COUNTRIES
from country_campaign import MIN_FIT_SCORE

BASE = Path(__file__).resolve().parent
SCRAPER = Path("/root/projects/otomasyon-paneli/apps/personal-job-outreach/lead-scraper")
COUNTRIES = ",".join(ACTIVE_COUNTRIES)
LOCK_PATH = LOCK_DIR / "incremental-publish.lock"
LOG_PATH = LOG_DIR / "incremental-publish.log"


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, timeout=180, env=environment
    )


def dispatch_sender() -> None:
    unit = f"outreach-incremental-dispatch-{int(time.time())}"
    result = run(
        ["systemd-run", "--collect", f"--unit={unit}", "/usr/bin/python3", "-B", str(BASE / "daily_batch.py")],
        cwd=BASE,
    )
    if result.returncode:
        log(f"dispatch failed: {result.stderr.strip()[:240]}")
    else:
        log(f"sender dispatched: {unit}")


def publish_once() -> None:
    # A private temporary directory prevents a partially written CSV from ever
    # being mistaken for a completed pipeline export.
    with tempfile.TemporaryDirectory(prefix="job-finder-incremental-") as temp:
        work = Path(temp)
        candidates = work / "candidates.csv"
        audit = work / "audit.csv"
        personalizations = work / "personalizations.csv"
        steps = [
            ([sys.executable, "scrape.py", "export", "--countries", COUNTRIES,
              "--min-fit-score", str(MIN_FIT_SCORE),
              "--require-email-source", "--limit", "3000",
              "--out", str(candidates)], SCRAPER),
            ([sys.executable, "export_fit_audit.py", str(candidates), str(audit)], SCRAPER),
            ([sys.executable, str(BASE / "personalize_outreach.py"), str(audit), str(personalizations)], BASE),
            ([sys.executable, str(BASE / "publish_verified_batch.py"), str(audit),
              str(personalizations), "--apply"], BASE),
        ]
        output = ""
        for command, cwd in steps:
            result = run(command, cwd=cwd)
            output += "\n" + result.stdout + "\n" + result.stderr
            if result.returncode:
                log(f"step failed ({Path(command[1]).name}): {result.stderr.strip()[:300] or result.stdout.strip()[:300]}")
                return
        match = re.search(r"published=(\d+)", output)
        count = int(match.group(1)) if match else 0
        if count:
            log(f"published={count}; starting sender")
            dispatch_sender()
        else:
            log("no newly publishable contacts")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=120, help="poll interval in seconds")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 30:
        raise ValueError("interval must be at least 30 seconds")
    LOCK_PATH.touch(exist_ok=True)
    with LOCK_PATH.open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another incremental publisher owns the lock; exiting")
            return
        while True:
            try:
                publish_once()
            except Exception as exc:
                log(f"worker error: {type(exc).__name__}: {exc}")
            if args.once:
                return
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
