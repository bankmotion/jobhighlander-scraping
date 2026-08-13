"""Entry point — run one or more site scrapers and persist to the local MySQL DB.

Usage:
    python main.py                    # default: indeed
    python main.py indeed
    python main.py glassdoor
    python main.py indeed glassdoor   # both, one after another
    python main.py all                # every registered site, one after another

Sites run sequentially (each has its own Chrome profile + proxy session, so they
never contend for a browser-profile lock).
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


def resolve_sites(argv: list[str]) -> list[str]:
    """Turn CLI args into an ordered site list. 'all' expands to every scraper;
    no args defaults to indeed. Duplicates are de-duped, order preserved."""
    args = [a.lower() for a in argv] or ["indeed"]
    if "all" in args:
        return list(SCRAPERS.keys())
    seen: set[str] = set()
    out: list[str] = []
    for s in args:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


async def run_site(site: str) -> bool:
    scraper_cls = SCRAPERS.get(site)
    if scraper_cls is None:
        log.error("Unknown site '{}'. Available: {}, all", site, ", ".join(SCRAPERS))
        return False
    log.info("Starting scraper for '{}'", site)
    try:
        await scraper_cls().run()
        return True
    except Exception:
        log.exception("Scraper '{}' failed", site)
        return False


async def main() -> None:
    sites = resolve_sites(sys.argv[1:])
    log.info("Sites to scrape (in order): {}", ", ".join(sites))

    results: dict[str, bool] = {}
    for site in sites:
        results[site] = await run_site(site)

    if len(sites) > 1:
        log.info(
            "All sites done — {}",
            ", ".join(f"{s}={'ok' if ok else 'FAILED'}" for s, ok in results.items()),
        )
    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
