"""LinkedIn scraper — public "guest" endpoints, no login, no browser.

LinkedIn's job search has an unauthenticated surface intended for logged-out
visitors, and it carries everything this table stores:

  listing  /jobs-guest/jobs/api/seeMoreJobPostings/search?<filters>&start=N
  detail   /jobs-guest/jobs/api/jobPosting/<jobId>

Reverse-engineered contract (undocumented, verified 2026-08-21):
  • The listing returns a bare <li> fragment list, NOT JSON — hence regex
    parsing rather than json.loads.
  • It returns 10 cards per call NO MATTER WHAT `start` is. Stepping by 25 (the
    stride the website's own infinite scroll uses) therefore SKIPS 15 jobs per
    call: a step=25 sweep of this search found 59 unique jobs where step=10
    found 372. Step by _PAGE_STEP.
  • Results are not strictly newest-first, so we cannot early-stop on age the
    way findmyremote does. The search URL's own `f_TPR` filter bounds the window
    and BaseScraper.save() skips anything past max_age_days.
  • Cards repeat across offsets, so ids are de-duped for the whole run.

APPLY URL. Logged out, LinkedIn knows a job applies offsite — it tags the button
`public_jobs_apply-link-offsite` — but swaps the employer link for a "Join or
sign in" modal, and the public page carries no JSON-LD and no companyApplyUrl.
There is no unauthenticated route to it. Since BaseScraper.save() drops any job
without an apply_url, we use the posting's own canonical LinkedIn URL: that is a
real place a person applies, so the row is honest rather than skipped.
"""
from __future__ import annotations

import asyncio
import html as _html
import random
import re
from datetime import date, datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse

from curl_cffi.requests import AsyncSession

from config import settings, proxy_for
from logger import log
from scraper.base_scraper import BaseScraper, ScrapedJob

_IMPERSONATE = "chrome"
_GUEST = "https://www.linkedin.com/jobs-guest/jobs/api"
_SEARCH = f"{_GUEST}/seeMoreJobPostings/search"
_DETAIL = f"{_GUEST}/jobPosting"

#: The endpoint hands back 10 cards per call regardless of `start`; stepping by
#: anything larger silently drops the difference. See the module docstring.
_PAGE_STEP = 10
_MAX_PAGES = 80          # safety net (~800 offsets); the dry streak stops us first
_DRY_STREAK = 2          # consecutive empty responses that mean "end of results"

#: The DETAIL endpoint throttles independently of the listing one, and a 429
#: there costs only that job's description. Retry it once after a pause, then —
#: if the throttling is sustained — stop asking for descriptions for the rest of
#: the pass. Hammering a throttled endpoint only lengthens the block, and a row
#: with no description still beats no row.
_DETAIL_RETRY_BACKOFF_S = 8.0
_MAX_DETAIL_429 = 3

#: Params that address a POSITION in the result set. The configured search URL is
#: a normal browsable link, so it carries the ones the website's own UI uses; we
#: drive paging ourselves and must not forward a conflicting offset.
_POSITIONAL = {"start", "position", "pagenum", "activefilter"}

_CARD_RE = re.compile(r"<li>(.*?)</li>", re.S)
_ID_RE = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
_TITLE_RE = re.compile(r'base-search-card__title[^"]*">\s*(.*?)\s*<', re.S)
_COMPANY_RE = re.compile(r'hidden-nested-link[^>]*>\s*(.*?)\s*<', re.S)
_COMPANY_URL_RE = re.compile(r'href="(https://www\.linkedin\.com/company/[^"?]+)')
_LOCATION_RE = re.compile(r'job-search-card__location[^"]*">\s*(.*?)\s*<', re.S)
#: Fresh postings get the `--new` modifier on the same element, so match a prefix.
_DATE_RE = re.compile(r'job-search-card__listdate[^"]*"\s+datetime="([\d-]+)"')
_DESC_RE = re.compile(r'show-more-less-html__markup[^>]*>(.*?)</div>', re.S)


def _clean_html(h: str) -> str:
    """LinkedIn descriptions are HTML fragments (<p>, <ul>/<li>, <strong>, <br>)."""
    h = _html.unescape(h or "")
    h = h.replace("\r\n", "\n").replace("\r", "\n")
    h = re.sub(r"(?i)<\s*(br|/p|/div|/h[1-6]|/li|/tr|/ul)\s*/?>", "\n", h)
    h = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = _html.unescape(h).replace("\xa0", " ")
    h = re.sub(r"[ \t]+\n", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()


def _text(pattern: re.Pattern, card: str) -> Optional[str]:
    m = pattern.search(card)
    if not m:
        return None
    return re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip() or None


class LinkedInScraper(BaseScraper):
    site = "linkedin"
    table = "jobs"

    def __init__(self):
        super().__init__()  # repo + counts; the browser attribute stays unused
        self._role = (re.compile(settings.linkedin_role_regex, re.I)
                      if settings.linkedin_role_regex else None)
        self._proxies = None
        if proxy_for("linkedin"):
            self._proxies = {"http": proxy_for("linkedin"), "https": proxy_for("linkedin")}
        self._detail_429 = 0        # consecutive 429s from the detail endpoint
        self._descriptions_off = False

    # HTTP-only lifecycle — no Chrome, so none of the Xvfb/sandbox machinery.
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

    @staticmethod
    def _filters() -> str:
        """Forward the configured search URL's filters verbatim, minus paging.

        The setting holds the link you'd paste from LinkedIn, so keywords /
        geoId / f_TPR stay editable from the admin UI without anyone needing to
        know the guest endpoint exists.
        """
        try:
            qs = parse_qsl(urlparse(settings.linkedin_search_url).query, keep_blank_values=False)
        except Exception:
            qs = []
        return urlencode([(k, v) for k, v in qs if k.lower() not in _POSITIONAL])

    def _matches(self, title: str) -> bool:
        return not self._role or bool(self._role.search(title or ""))

    def _saved(self) -> int:
        return self.counts["inserted"] + self.counts["updated"] + self.counts["unchanged"]

    async def _description(self, session: AsyncSession, job_id: str) -> str:
        if not settings.fetch_descriptions or self._descriptions_off:
            return ""
        for attempt in range(2):
            try:
                r = await session.get(f"{_DETAIL}/{job_id}", timeout=45)
            except Exception as e:
                log.warning("[linkedin] detail fetch failed for {}: {}", job_id, e)
                return ""
            if r.status_code == 200:
                self._detail_429 = 0
                m = _DESC_RE.search(r.text)
                return _clean_html(m.group(1)) if m else ""
            if r.status_code != 429:
                log.warning("[linkedin] detail {} -> HTTP {}", job_id, r.status_code)
                return ""
            self._detail_429 += 1
            if self._detail_429 >= _MAX_DETAIL_429:
                self._descriptions_off = True
                log.warning(
                    "[linkedin] detail endpoint threw 429 {} times running — dropping "
                    "descriptions for the REST of this pass (jobs still save, and the "
                    "ones already fetched keep theirs).", self._detail_429)
                return ""
            if attempt == 0:
                log.warning("[linkedin] detail {} -> 429, retrying in {:.0f}s",
                            job_id, _DETAIL_RETRY_BACKOFF_S)
                await asyncio.sleep(_DETAIL_RETRY_BACKOFF_S)
        return ""

    @staticmethod
    def _parse_card(card: str) -> Optional[dict]:
        m = _ID_RE.search(card)
        if not m:
            return None
        job_id = m.group(1)
        title = _text(_TITLE_RE, card)
        if not title:
            return None
        posted = None
        d = _DATE_RE.search(card)
        if d:
            try:
                posted = datetime.strptime(d.group(1), "%Y-%m-%d").date()
            except ValueError:
                posted = None
        co_url = _COMPANY_URL_RE.search(card)
        return {
            "id": job_id,
            "title": title,
            "company": _text(_COMPANY_RE, card),
            "company_url": co_url.group(1) if co_url else None,
            "location": _text(_LOCATION_RE, card),
            "posted": posted,
        }

    def _to_job(self, card: dict, description: str) -> ScrapedJob:
        # Canonical, tracking-free posting URL. Also the apply_url — see the
        # module docstring for why there is no employer link without a login.
        url = f"https://www.linkedin.com/jobs/view/{card['id']}"
        loc = card.get("location")
        return ScrapedJob(
            site_job_id=card["id"],
            title=card["title"],
            description=description,
            link=url,
            location=loc,
            posted_at=card.get("posted"),
            apply_url=url,
            company=card.get("company"),
            company_url=card.get("company_url"),
            job_type=None,          # not exposed on the guest card
            # The guest card has no structured workplace-type field (Indeed's
            # `remoteWorkModel` has no equivalent here), so fall back to text —
            # and read the TITLE as well as the location, because LinkedIn
            # routinely files a "… (USA Remote)" posting under the hiring
            # office's city, which a location-only test reads as on-site.
            remote=bool(re.search(r"remote", f"{card['title']} {loc or ''}", re.I)),
            salary=None,            # not exposed on the guest card
        )

    async def scrape(self) -> None:
        filters = self._filters()
        log.info("[linkedin] filters: {}", filters or "(none)")
        seen: set[str] = set()
        dry = 0
        delay = float(settings.linkedin_delay_s)

        async with AsyncSession(impersonate=_IMPERSONATE, proxies=self._proxies) as session:
            for page in range(_MAX_PAGES):
                if settings.max_jobs and self._saved() >= settings.max_jobs:
                    break
                start = page * _PAGE_STEP
                url = f"{_SEARCH}?{filters}&start={start}" if filters else f"{_SEARCH}?start={start}"
                try:
                    r = await session.get(url, timeout=45)
                except Exception as e:
                    log.warning("[linkedin] fetch error at start={}: {}", start, e)
                    break
                if r.status_code == 429:
                    # Guest endpoints throttle hard; pushing through just earns a
                    # longer block, so end the pass and keep what we have.
                    log.warning("[linkedin] HTTP 429 at start={} — throttled, stopping.", start)
                    break
                if r.status_code != 200:
                    log.warning("[linkedin] HTTP {} at start={} — stopping.", r.status_code, start)
                    break

                cards = [c for c in (self._parse_card(c) for c in _CARD_RE.findall(r.text)) if c]
                fresh = [c for c in cards if c["id"] not in seen]
                seen.update(c["id"] for c in cards)

                if not cards:
                    dry += 1
                    if dry >= _DRY_STREAK:
                        log.info("[linkedin] {} empty responses — end of results.", dry)
                        break
                    await asyncio.sleep(delay)
                    continue
                dry = 0
                log.info("[linkedin] start={} — {} cards, {} new (unique so far {})",
                         start, len(cards), len(fresh), len(seen))

                for c in fresh:
                    if settings.max_jobs and self._saved() >= settings.max_jobs:
                        break
                    if not self._matches(c["title"]):
                        continue
                    desc = await self._description(session, c["id"])
                    self.save(self._to_job(c, desc))
                    await asyncio.sleep(random.uniform(delay * 0.6, delay * 1.4))
                await asyncio.sleep(random.uniform(delay * 0.6, delay * 1.4))
            else:
                log.info("[linkedin] hit the {}-page budget (~{} offsets scanned)",
                         _MAX_PAGES, _MAX_PAGES * _PAGE_STEP)
