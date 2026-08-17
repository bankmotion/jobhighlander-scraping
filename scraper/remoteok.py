"""RemoteOK scraper — public JSON API + browser apply-URL resolution.

RemoteOK exposes a clean public JSON API (`/api`) with no login and no Cloudflare
interstitial for a Chrome-fingerprinted client, so the job list + all metadata
come from one curl_cffi call. The real employer apply URL, though, sits behind
RemoteOK's `/l/<id>` click-tracker — a JavaScript redirect only a real browser
can follow — so each one is resolved in the stealth browser (like the Indeed
apply capture). A job whose `/l/` doesn't leave remoteok.com falls back to its
RemoteOK posting URL (a working, login-free apply page).

Note: RemoteOK's API ignores `location`/`tags` filtering (it returns the latest
~100 jobs regardless), so age + role filtering is done here, client-side.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from curl_cffi.requests import AsyncSession

from config import settings
from logger import log
from scraper.base_scraper import BaseScraper, ScrapedJob

_IMPERSONATE = "chrome"


def _clean_html(h: str) -> str:
    h = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", h or "")
    h = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = _html.unescape(h)
    h = re.sub(r"[ \t]+\n", "\n", h)
    return re.sub(r"\n{3,}", "\n\n", h).strip()


def _salary(jr: dict) -> Optional[str]:
    try:
        mn, mx = int(jr.get("salary_min") or 0), int(jr.get("salary_max") or 0)
    except Exception:
        return None
    if mn and mx:
        return f"${mn:,}–${mx:,}"
    if mn:
        return f"${mn:,}+"
    return None


class RemoteOkScraper(BaseScraper):
    site = "remoteok"
    #: Promote to "jobs" once verified.
    table = "jobs_temp"
    user_data_dir = settings.remoteok_user_data_dir
    #: Direct — RemoteOK is global, and external employer apply pages shouldn't
    #: be tunnelled through the residential proxy.
    proxy_url = ""

    BASE = "https://remoteok.com"

    def __init__(self):
        super().__init__()
        self._role = re.compile(settings.remoteok_role_regex, re.I) if settings.remoteok_role_regex else None

    async def scrape(self) -> None:
        jobs = await self._fetch_api()
        recent = self._select(jobs)
        log.info(
            "[remoteok] {} from API -> {} after {}d + role filter (cap {})",
            len(jobs), len(recent), settings.remoteok_max_age_days, settings.max_jobs,
        )
        for jr in recent[: settings.max_jobs]:
            job = self._to_job(jr)
            if not job:
                continue
            # Prefer the real employer URL; fall back to the (working) posting page.
            job.apply_url = await self._resolve_apply_url(jr["id"]) or job.link
            self.save(job)

    async def _fetch_api(self) -> list[dict]:
        try:
            async with AsyncSession(impersonate=_IMPERSONATE) as s:
                r = await s.get(settings.remoteok_api_url, timeout=45)
            if r.status_code != 200:
                log.error("[remoteok] API HTTP {}", r.status_code)
                return []
            data = json.loads(r.text)
        except Exception as e:
            log.error("[remoteok] API fetch/parse failed: {}", e)
            return []
        return [j for j in data if isinstance(j, dict) and j.get("position") and j.get("id")]

    def _select(self, jobs: list[dict]) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.remoteok_max_age_days)).timestamp()
        out = []
        for j in jobs:
            try:
                if int(j.get("epoch") or 0) < cutoff:
                    continue
            except Exception:
                continue
            if self._role and not self._role.search(j.get("position", "")):
                continue
            out.append(j)
        out.sort(key=lambda j: int(j.get("epoch") or 0), reverse=True)  # newest first
        return out

    def _to_job(self, jr: dict) -> Optional[ScrapedJob]:
        link = (jr.get("url") or "").replace("remoteOK.com", "remoteok.com")
        if not link:
            return None
        return ScrapedJob(
            site_job_id=str(jr["id"]),
            title=(jr.get("position") or "").strip(),
            description=_clean_html(jr.get("description") or ""),
            link=link,
            location=(jr.get("location") or "").strip(" ,") or None,
            posted_at=self._date(jr.get("epoch")),
            apply_url=None,  # resolved separately via the browser
            company=(jr.get("company") or "").strip() or None,
            company_url=None,
            job_type=None,  # RemoteOK has no reliable employment-type field
            remote=True,
            salary=_salary(jr),
        )

    @staticmethod
    def _date(epoch):
        try:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date()
        except Exception:
            return None

    async def _resolve_apply_url(self, jid) -> Optional[str]:
        """Follow RemoteOK's `/l/<id>` JS redirect in the browser to the real
        employer apply URL. Returns None if it never leaves remoteok.com."""
        page = self.browser.page
        try:
            await page.goto(f"{self.BASE}/l/{jid}", wait_until="commit", timeout=20_000)
        except Exception:
            pass
        for _ in range(25):  # up to ~5s for the JS redirect to fire
            u = page.url or ""
            if u.startswith("http") and "remoteok.com" not in u.lower():
                return u.split("#")[0]
            await asyncio.sleep(0.2)
        return None
