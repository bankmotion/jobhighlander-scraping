"""Backfill the `salary` column for existing Indeed / Glassdoor rows by
re-visiting each job's detail page and reading its salary. Resumable — only
touches rows where salary IS NULL, so it can be re-run to continue.

Run:  ./venv/Scripts/python.exe scripts/backfill_salary.py [indeed] [glassdoor]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logger import log  # noqa: E402
from scraper.glassdoor import GlassdoorScraper  # noqa: E402
from scraper.indeed import IndeedScraper  # noqa: E402

SCRAPERS = {"indeed": IndeedScraper, "glassdoor": GlassdoorScraper}

# Per-site salary extraction from a job DETAIL page.
SALARY_JS = {
    "indeed": r"""
      () => {
        const el = document.querySelector('#salaryInfoAndJobType, [data-testid="jobsearch-OtherJobDetailsContainer"], [id*="salary" i]');
        const scope = el ? el.innerText : (document.body ? document.body.innerText.slice(0, 4000) : '');
        const m = (scope || '').match(/\$[\d.,]+\s*[kK]?\s*(?:-|–|—|to)\s*\$?[\d.,]+\s*[kK]?(?:\s*(?:a year|an hour|per year|per hour|\/yr|\/hr|a month|a week))?/i);
        return m ? m[0].trim() : '';
      }
    """,
    "glassdoor": r"""
      () => {
        const sal = document.querySelector('[data-test="detailSalary"], [class*="SalaryEstimate" i], [class*="salaryEstimate" i]');
        if (sal && (sal.innerText || '').trim()) return sal.innerText.trim();
        const pay = document.querySelector('[class*="JobDetails_locationAndPay" i]');
        if (pay) { const m = (pay.innerText || '').match(/\$[\d.,kK]+\s*[-–—]\s*\$[\d.,kK]+[^\n]{0,40}/); if (m) return m[0].trim(); }
        return '';
      }
    """,
}


async def backfill(site: str) -> None:
    scraper = SCRAPERS[site]()
    await scraper.browser.start()
    scraper.repo.connect()
    try:
        try:
            await scraper.ensure_logged_in()
        except Exception as exc:
            log.warning("[{}] login failed, continuing: {}", site, exc)

        with scraper.repo._conn.cursor() as cur:
            cur.execute(
                "SELECT id, job_url FROM jobs WHERE site=%s AND (salary IS NULL OR salary='') AND job_url<>''",
                (site,),
            )
            rows = cur.fetchall()
        log.info("[{}] {} rows to backfill salary", site, len(rows))

        updated = 0
        for i, (rid, url) in enumerate(rows, 1):
            try:
                await scraper.browser.goto(url)
                sal = (await scraper.browser.page.evaluate(SALARY_JS[site]) or "").strip()
            except Exception as exc:
                log.warning("[{}] {}/{} id={} skipped: {}", site, i, len(rows), rid, str(exc)[:60])
                continue
            if sal:
                with scraper.repo._conn.cursor() as cur:
                    cur.execute(
                        "UPDATE jobs SET salary=%s, updated_at=UTC_TIMESTAMP(3) WHERE id=%s",
                        (sal[:255], rid),
                    )
                updated += 1
                log.info("[{}] {}/{} id={} -> {}", site, i, len(rows), rid, sal[:40])
            else:
                log.info("[{}] {}/{} id={} no salary", site, i, len(rows), rid)
        log.info("[{}] salary backfill done: {} / {} updated", site, updated, len(rows))
    finally:
        await scraper.browser.close()
        scraper.repo.close()


async def main() -> None:
    sites = [a.lower() for a in sys.argv[1:]] or ["indeed", "glassdoor"]
    for site in sites:
        if site in SCRAPERS:
            await backfill(site)
        else:
            log.error("unknown site {}", site)


if __name__ == "__main__":
    asyncio.run(main())
