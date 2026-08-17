"""Himalayas scraper — public JSON API, no login/browser.

`himalayas.app/jobs/api` is a public, paginated (offset, 20/page), newest-first
JSON feed — clean structured data, curl_cffi gets 200. We page until we reach
jobs older than max_age_days, filtering to a country (locationRestrictions) and
a role regex.

The apply link points to the Himalayas job PAGE (its pages are Cloudflare-walled,
so the real employer URL isn't reachable from this datacenter — same as
RemoteOK); that page is a working, login-free apply destination.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from curl_cffi.requests import AsyncSession

from config import settings
from logger import log
from scraper.base_scraper import BaseScraper, ScrapedJob

_IMPERSONATE = "chrome"
_MAX_PAGES = 40


def _clean_html(h: str) -> str:
    h = _html.unescape(h or "")
    h = h.replace("\r\n", "\n").replace("\r", "\n")
    h = re.sub(r"(?i)<\s*(br|/p|/div|/h[1-6]|/li|/tr)\s*/?>", "\n", h)
    h = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = _html.unescape(h).replace("\xa0", " ")
    h = re.sub(r"[ \t]+\n", "\n", h)
    return re.sub(r"\n{3,}", "\n\n", h).strip()


def _salary(jr: dict) -> Optional[str]:
    def n(x) -> int:
        try:
            return int(float(x))
        except (TypeError, ValueError):
            return 0

    mn, mx = n(jr.get("minSalary")), n(jr.get("maxSalary"))
    cur = (jr.get("currency") or "").strip()
    per = {"yearly": "/yr", "hourly": "/hr", "monthly": "/mo", "weekly": "/wk", "daily": "/day"}.get(
        (jr.get("salaryPeriod") or "").strip().lower(), ""
    )
    if mn and mx:
        return f"{cur} {mn:,}–{mx:,}{per}".strip()
    if mn or mx:
        return f"{cur} {(mn or mx):,}{per}".strip()
    return None


class HimalayasScraper(BaseScraper):
    site = "himalayas"
    #: Clean, structured API data — writes straight to the live jobs table.
    table = "jobs"

    def __init__(self):
        super().__init__()  # sets up self.repo + counts (browser stays unused)
        self._role = re.compile(settings.himalayas_role_regex, re.I) if settings.himalayas_role_regex else None
        self._country = (settings.himalayas_country or "").strip().lower()

    # HTTP-only lifecycle (no browser).
    async def run(self) -> None:
        self.repo.connect()
        try:
            await self.scrape()
            log.info(
                "[{}] done — inserted={inserted} updated={updated} unchanged={unchanged} "
                "skipped={skipped} too_old={too_old}",
                self.site,
                **self.counts,
            )
        finally:
            self.repo.close()

    def _matches(self, jr: dict) -> bool:
        if self._role and not self._role.search(jr.get("title", "")):
            return False
        if self._country:
            locs = [str(x).lower() for x in (jr.get("locationRestrictions") or [])]
            # No restrictions = worldwide (eligible); else require our country.
            if locs and not any(self._country in x for x in locs):
                return False
        return True

    @staticmethod
    def _date(epoch):
        try:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date()
        except Exception:
            return None

    def _to_job(self, jr: dict) -> Optional[ScrapedJob]:
        guid = jr.get("guid") or ""
        slug = guid.rsplit("/", 1)[-1] if guid else ""
        sid = (f"{jr.get('companySlug', '')}/{slug}".strip("/") or slug)[:190]
        if not sid:
            return None
        link = jr.get("applicationLink") or guid
        company_slug = jr.get("companySlug")
        return ScrapedJob(
            site_job_id=sid,
            title=(jr.get("title") or "").strip(),
            description=_clean_html(jr.get("description") or jr.get("excerpt") or ""),
            link=link,
            location=", ".join(jr.get("locationRestrictions") or []) or None,
            posted_at=self._date(jr.get("pubDate")),
            apply_url=link,  # Himalayas job page — its pages are CF-walled, so no external URL
            company=(jr.get("companyName") or "").strip() or None,
            company_url=(f"https://himalayas.app/companies/{company_slug}" if company_slug else None),
            job_type=(jr.get("employmentType") or None),
            remote=True,  # Himalayas is all remote
            salary=_salary(jr),
        )

    def _saved(self) -> int:
        return self.counts["inserted"] + self.counts["updated"] + self.counts["unchanged"]

    async def scrape(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.max_age_days or 3650)).timestamp()
        base = settings.himalayas_api_url
        async with AsyncSession(impersonate=_IMPERSONATE) as session:
            offset = 0
            for page in range(_MAX_PAGES):
                if settings.max_jobs and self._saved() >= settings.max_jobs:
                    break
                try:
                    r = await session.get(f"{base}?offset={offset}&limit=20", timeout=45)
                    data = json.loads(r.text)
                except Exception as e:
                    log.warning("[himalayas] fetch error at offset {}: {}", offset, e)
                    break
                jobs = data.get("jobs") or []
                if not jobs:
                    break
                log.info("[himalayas] page {} (offset {}): {} jobs", page + 1, offset, len(jobs))
                stop = False
                for jr in jobs:
                    if int(jr.get("pubDate") or 0) < cutoff:  # newest-first → the rest are older
                        stop = True
                        break
                    if not self._matches(jr):
                        continue
                    job = self._to_job(jr)
                    if job:
                        self.save(job)
                if stop:
                    log.info("[himalayas] reached postings older than {}d — stopping.", settings.max_age_days)
                    break
                offset += 20
                await asyncio.sleep(random.uniform(0.3, 0.8))
            else:
                # Ran the full page budget without reaching the age cutoff — there
                # may be more matching jobs within the window past this point.
                log.info(
                    "[himalayas] hit the {}-page budget (scanned {} listings); "
                    "raise _MAX_PAGES to go deeper.",
                    _MAX_PAGES, offset + 20,
                )
