"""Entry point — run a site scraper and persist results to the local MySQL DB.

Usage:
    python main.py            # default: indeed
    python main.py indeed
"""
import asyncio
import sys

from logger import log
from scraper.glassdoor import GlassdoorScraper
from scraper.indeed import IndeedScraper

SCRAPERS = {
    "indeed": IndeedScraper,
    "glassdoor": GlassdoorScraper,
    # Add more sites here as their links arrive — each reuses the same core.
}


async def main() -> None:
    site = (sys.argv[1] if len(sys.argv) > 1 else "indeed").lower()
    scraper_cls = SCRAPERS.get(site)
    if scraper_cls is None:
        log.error("Unknown site '{}'. Available: {}", site, ", ".join(SCRAPERS))
        sys.exit(1)

    log.info("Starting scraper for '{}'", site)
    try:
        await scraper_cls().run()
    except Exception:
        log.exception("Scraper '{}' failed", site)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
