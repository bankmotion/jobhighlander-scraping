"""Base scraper — shared browser + DB lifecycle for every job-site scraper.

New sites (the extra links you'll send later) subclass this and implement
`scrape()`. The reusable stealth browser and DB writer come for free.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from config import settings
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
    salary: Optional[str] = None


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
        #: Running write totals, updated by save() as each job is persisted.
        self.counts = {"inserted": 0, "updated": 0, "unchanged": 0, "unknown": 0, "skipped": 0, "too_old": 0}

    async def scrape(self) -> list[ScrapedJob]:
        """Scrape the site, calling `self.save(job)` for EACH job as it completes
        (so it lands in the DB immediately). May also return the jobs list."""
        raise NotImplementedError

    def save(self, job: ScrapedJob) -> str:
        """Upsert ONE job right away (the DB connection autocommits), and update
        the running counts. Called per-job by the scrapers so every finished job
        is persisted immediately instead of being batched at the end of the run.

        The apply URL is required: a listing with no way to apply is useless, so
        if detail-fetching didn't yield an apply_url we skip the job entirely
        (never write it to the DB) and count it as "skipped"."""
        if not (job.apply_url and job.apply_url.strip()):
            self.counts["skipped"] += 1
            log.info("[{}] skipped (no apply url) — {} ({})", self.site, (job.title or "")[:50], job.site_job_id)
            return "skipped"
        if self._too_old(job.posted_at):
            self.counts["too_old"] += 1
            log.info(
                "[{}] skipped (posted {} — older than {}d) — {}",
                self.site, job.posted_at, settings.max_age_days, (job.title or "")[:50],
            )
            return "too_old"
        result = self.repo.upsert_job(
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
            salary=job.salary,
        )
        self.counts[result] = self.counts.get(result, 0) + 1
        log.info("[{}] saved {} ({}) — {}", self.site, result, job.site_job_id, (job.title or "")[:50])
        return result

    @staticmethod
    def _too_old(posted) -> bool:
        """True if `posted` is older than settings.max_age_days (0 = no age limit).
        Jobs with an unknown date are kept (not skipped)."""
        if not settings.max_age_days or posted is None:
            return False
        if isinstance(posted, datetime):
            posted = posted.date()
        try:
            return posted < (date.today() - timedelta(days=settings.max_age_days))
        except TypeError:
            return False

    async def run(self) -> None:
        try:
            await self.browser.start()
            self.repo.connect()
            await self.scrape()  # each job is saved inline as it's scraped
            log.info(
                "[{}] done — inserted={inserted} updated={updated} unchanged={unchanged} "
                "skipped={skipped} too_old={too_old}",
                self.site,
                **self.counts,
            )
        finally:
            await self.browser.close()
            self.repo.close()
