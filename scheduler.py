"""Run the scraper on a loop with a RANDOM gap between runs.

The gap is a random duration in [SCHEDULE_MIN_HOURS, SCHEDULE_MAX_HOURS]
(from .env; defaults 1–3h), re-rolled after every run. Each run is a fresh
`python main.py <site>` subprocess, so every run gets a clean browser/session.

Usage:
    python scheduler.py            # site: indeed
    python scheduler.py indeed
"""
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import settings
from logger import log

BASE_DIR = Path(__file__).resolve().parent


def _run_once(site: str) -> None:
    start = datetime.now()
    log.info("=== Scrape run start: {} ({}) ===", start.strftime("%Y-%m-%d %H:%M:%S"), site)
    try:
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "main.py"), site],
            cwd=str(BASE_DIR),
            check=False,
        )
        took = (datetime.now() - start).total_seconds()
        log.info("=== Scrape run finished (exit={}, {:.0f}s) ===", result.returncode, took)
    except Exception as exc:
        log.error("Scrape run failed to launch: {}", exc)


def main() -> None:
    site = (sys.argv[1] if len(sys.argv) > 1 else "indeed").lower()
    lo = min(settings.schedule_min_hours, settings.schedule_max_hours)
    hi = max(settings.schedule_min_hours, settings.schedule_max_hours)
    log.info("Scheduler started — running '{}' with a random {:.2f}–{:.2f}h gap between runs.", site, lo, hi)

    while True:
        _run_once(site)
        delay_hours = random.uniform(lo, hi)
        next_run = datetime.now() + timedelta(hours=delay_hours)
        log.info("Next run in {:.2f}h — at {}", delay_hours, next_run.strftime("%Y-%m-%d %H:%M:%S"))
        time.sleep(delay_hours * 3600)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Scheduler stopped.")
