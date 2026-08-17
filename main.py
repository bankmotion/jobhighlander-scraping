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
import traceback

from config import settings
from logger import log
from scraper.db import ScrapeRunRepo
from scraper.glassdoor import GlassdoorScraper
from scraper.indeed import IndeedScraper
from scraper.jobright import JobRightScraper
from scraper.remoteok import RemoteOkScraper
from scraper.weworkremotely import WeWorkRemotelyScraper

SCRAPERS = {
    "indeed": IndeedScraper,
    "glassdoor": GlassdoorScraper,
    "jobright": JobRightScraper,
    "weworkremotely": WeWorkRemotelyScraper,
    "remoteok": RemoteOkScraper,
    # Add more sites here as their links arrive — each reuses the same core.
}


def enabled_sites() -> list[str]:
    """Registered sites whose ENABLE_<SITE> flag is on (order preserved)."""
    flags = {
        "indeed": settings.enable_indeed,
        "glassdoor": settings.enable_glassdoor,
        "jobright": settings.enable_jobright,
        "weworkremotely": settings.enable_weworkremotely,
        "remoteok": settings.enable_remoteok,
    }
    return [s for s in SCRAPERS if flags.get(s, True)]


def resolve_sites(argv: list[str]) -> list[str]:
    """Turn CLI args into an ordered site list. No args or 'all' → the ENABLED
    sites; explicitly named sites run regardless of their flag (for testing).
    Duplicates de-duped, order preserved."""
    args = [a.lower() for a in argv] or ["all"]
    if "all" in args:
        return enabled_sites()
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
    # Log the run to scrape_runs so super-admins can see status/counts in the app.
    run_log = ScrapeRunRepo()
    run_id = run_log.start(site)
    scraper = scraper_cls()
    try:
        await scraper.run()
        run_log.finish(run_id, "success", scraper.counts)
        return True
    except Exception:
        log.exception("Scraper '{}' failed", site)
        run_log.finish(run_id, "failed", scraper.counts, error=traceback.format_exc()[-2000:])
        return False
    finally:
        run_log.close()


async def main() -> None:
    sites = resolve_sites(sys.argv[1:])
    if not sites:
        log.warning("No sites to scrape — all ENABLE_* flags are off.")
        return
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
