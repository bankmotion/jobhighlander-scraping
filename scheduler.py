"""Run the scraper on a loop with a RANDOM gap between runs.

The gap is a random duration in [schedule_min_hours, schedule_max_hours],
re-rolled after every run. Each run is a fresh `python main.py <site>`
subprocess, so every run gets a clean browser/session.

Both bounds are DB-managed (super-admins edit them in the scraper settings UI,
precedence DB > .env > code default) and are RE-READ at the top of every cycle.
This process runs for days at a time, so reading them once at startup would mean
an edit did not take effect until somebody remembered to restart it.

Usage:
    python scheduler.py                    # site: indeed
    python scheduler.py indeed glassdoor   # both each cycle
    python scheduler.py all                # every registered site each cycle
"""
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import settings, sync_settings_from_db
from logger import log

BASE_DIR = Path(__file__).resolve().parent


def _run_once(sites: list[str]) -> None:
    start = datetime.now()
    label = ", ".join(sites)
    log.info("=== Scrape run start: {} ({}) ===", start.strftime("%Y-%m-%d %H:%M:%S"), label)
    try:
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "main.py"), *sites],
            cwd=str(BASE_DIR),
            check=False,
        )
        took = (datetime.now() - start).total_seconds()
        log.info("=== Scrape run finished (exit={}, {:.0f}s) ===", result.returncode, took)
    except Exception as exc:
        log.error("Scrape run failed to launch: {}", exc)


def _gap_hours() -> tuple[float, float]:
    """The configured cycle gap, freshly read from the database.

    Ordered rather than trusted: the two bounds are edited independently in the
    admin UI, and a min above a max would make `random.uniform` return values
    outside the range the operator thinks they set. Floored at a few minutes so
    a mistyped 0 cannot turn the scheduler into a hot loop that re-launches
    Chrome continuously.
    """
    sync_settings_from_db()
    a = float(settings.schedule_min_hours)
    b = float(settings.schedule_max_hours)
    return max(0.05, min(a, b)), max(0.05, max(a, b))


def main() -> None:
    # Default 'all' → main.py runs the ENABLED sites (ENABLE_* in .env).
    sites = [a.lower() for a in sys.argv[1:]] or ["all"]
    lo, hi = _gap_hours()
    log.info("Scheduler started — running [{}] with a random {:.2f}–{:.2f}h gap between runs.", ", ".join(sites), lo, hi)

    while True:
        _run_once(sites)
        # Re-read per cycle so an edit applies to the very next gap.
        lo, hi = _gap_hours()
        delay_hours = random.uniform(lo, hi)
        next_run = datetime.now() + timedelta(hours=delay_hours)
        log.info("Next run in {:.2f}h (range {:.2f}–{:.2f}h) — at {}",
                 delay_hours, lo, hi, next_run.strftime("%Y-%m-%d %H:%M:%S"))
        time.sleep(delay_hours * 3600)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Scheduler stopped.")
