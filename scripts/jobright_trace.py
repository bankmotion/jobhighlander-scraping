"""Run the JobRight scraper's own _fetch_recommendations with an observer.

    venv/Scripts/python.exe scripts/jobright_trace.py

The feed returns 20 jobs and the item shape still matches what the scraper
parses, yet it collects 0. That leaves the listener itself: this attaches a
second response listener to the same page, so we can see whether the events the
scraper depends on ever reach it. Read-only — nothing is saved.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from scraper.jobright import JobRightScraper


async def main() -> None:
    scraper = JobRightScraper()
    await scraper.browser.start()
    t0 = time.time()

    def stamp() -> str:
        return f"{time.time() - t0:6.1f}s"

    try:
        await scraper.ensure_logged_in()
        print(f"{stamp()}  logged in; page at {scraper.browser.page.url}")

        page = scraper.browser.page
        seen: list[tuple[float, str]] = []

        def observer(resp):
            if "/recommend/list/jobs" in resp.url:
                seen.append((time.time() - t0, resp.url))
                print(f"{stamp()}  OBSERVER saw feed response: {resp.url[:100]}")

        page.on("response", observer)

        print(f"{stamp()}  calling the scraper's own _fetch_recommendations()...")
        items = await scraper._fetch_recommendations(settings.max_jobs or 20)
        print(f"{stamp()}  _fetch_recommendations returned {len(items)} item(s)")

        print(f"\nobserver saw {len(seen)} feed response(s):")
        for t, u in seen:
            print(f"  at {t:6.1f}s  {u[:110]}")

        if seen and not items:
            print(
                "\n=> The response DID reach a listener on this page, but the "
                "scraper's own handler produced nothing from it."
            )
        elif not seen:
            print(
                "\n=> The feed response never reached any listener on this page "
                "during the scraper's window."
            )
    finally:
        await scraper.browser.close()


if __name__ == "__main__":
    asyncio.run(main())
