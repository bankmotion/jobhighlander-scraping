"""Standalone smoke test: verify the stealth browser + MySQL both work.

Run from the project root:
    python scripts/smoke_test.py

Loads a benign page headlessly (proves patchright + Chrome launch), then checks
the DB connection. Does NOT hit Indeed — keep that for the real scraper run.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logger import log  # noqa: E402
from scraper.browser import StealthBrowser  # noqa: E402
from scraper.db import JobRepository  # noqa: E402


async def main() -> None:
    log.info("[1/2] Browser launch test...")
    browser = StealthBrowser(headless=True)
    try:
        await browser.start()
        await browser.page.goto("https://example.com", wait_until="domcontentloaded")
        title = await browser.page.title()
        log.info("Loaded example.com — title={!r}", title)
    finally:
        await browser.close()

    log.info("[2/2] Database connection test...")
    with JobRepository() as repo:
        assert repo  # connected in __enter__
        log.info("DB connection OK")

    log.success("Smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
