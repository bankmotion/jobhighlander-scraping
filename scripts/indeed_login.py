"""Test Indeed 'Continue with Google' sign-in in isolation."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logger import log  # noqa: E402
from scraper.indeed import IndeedScraper  # noqa: E402


async def main() -> None:
    scraper = IndeedScraper()
    try:
        await scraper.browser.start()
        ok = await scraper.ensure_logged_in()
        log.info("Indeed logged in: {}", ok)
    finally:
        await scraper.browser.close()


if __name__ == "__main__":
    asyncio.run(main())
