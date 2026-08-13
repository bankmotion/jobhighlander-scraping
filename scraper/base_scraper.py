"""Base scraper — shared browser + DB lifecycle for every job-site scraper.

New sites (the extra links you'll send later) subclass this and implement
`scrape()`. The reusable stealth browser and DB writer come for free.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from logger import log
from scraper.browser import StealthBrowser
from scraper.db import JobRepository


@dataclass
class ScrapedJob:
    site_job_id: str
    title: str
    description: str
    link: str
    location: Optional[str] = None
    posted_at: Optional["date"] = None
    apply_url: Optional[str] = None
    company: Optional[str] = None
    company_url: Optional[str] = None
    job_type: Optional[str] = None
    remote: bool = False


class BaseScraper:
    #: Value stored in the `site` column, e.g. "indeed".
    site: str = "base"
    #: Destination table. Production sites use "jobs"; experimental scrapers can
    #: point at "jobs_temp" so they don't touch the live table.
    table: str = "jobs"
    #: Optional per-site Chrome profile dir. None → the shared default profile.
    #: A dedicated profile lets a site run without contending for the shared
    #: profile's single-owner lock (e.g. alongside the Indeed scheduler).
    user_data_dir: Optional[str] = None
    #: Optional per-site upstream proxy URL. None → the shared default. Set as an
    #: instance attr before super().__init__() to pick a proxy dynamically.
    proxy_url: Optional[str] = None

    def __init__(self):
        self.browser = StealthBrowser(user_data_dir=self.user_data_dir, proxy_url=self.proxy_url)
        self.repo = JobRepository(table=self.table)

    async def scrape(self) -> list[ScrapedJob]:
        """Return the jobs found. Implemented per site."""
        raise NotImplementedError

    def save(self, job: ScrapedJob) -> str:
        return self.repo.upsert_job(
            site=self.site,
            site_job_id=job.site_job_id,
            title=job.title,
            description=job.description,
            link=job.link,
            location=job.location,
            posted_at=job.posted_at,
            apply_url=job.apply_url,
            company=job.company,
            company_url=job.company_url,
            job_type=job.job_type,
            remote=job.remote,
        )

    async def run(self) -> None:
        counts = {"inserted": 0, "updated": 0, "unchanged": 0, "unknown": 0}
        try:
            await self.browser.start()
            self.repo.connect()
            jobs = await self.scrape()
            log.info("[{}] scraped {} jobs; writing to DB...", self.site, len(jobs))
            for job in jobs:
                result = self.save(job)
                counts[result] = counts.get(result, 0) + 1
            log.info(
                "[{}] done — inserted={inserted} updated={updated} unchanged={unchanged}",
                self.site,
                **counts,
            )
        finally:
            await self.browser.close()
            self.repo.close()
