"""Remote Rocketship — a Next.js site whose data is all in `__NEXT_DATA__`.

Reverse-engineered contract (undocumented, verified 2026-09-17):

  listing  /us/remote-jobs/?page=N&sort=DateAdded&locations=...&jobTitle=...
  detail   /us/publicjobs/company/<companySlug>/jobs/<jobSlug>/

  • Both pages embed a single `<script id="__NEXT_DATA__">` JSON blob. Nothing
    useful is in the markup, so this parses JSON rather than scraping HTML —
    no regex over tags, and the field names are the site's own.
  • The listing holds `props.pageProps.initialJobOpenings`, 20 per page.
  • THE APPLY URL IS IN THE LISTING. Each job carries `url`, already pointing at
    the employer's own board (greenhouse, lever, ...) rather than at Remote
    Rocketship. That is unusual and valuable: most sites here need a detail
    fetch or a redirect chain just to find it.
  • The DESCRIPTION is not. The listing has only summaries; the full text lives
    on the detail page as `roleDescription` / `roleRequirements` / `benefits`,
    which this joins into one document.

CLOUDFLARE. Plain HTTP gets a 403 from every IP tried, including a residential
proxy exit — the same wall Glassdoor sits behind. So this drives the real
Chrome `StealthBrowser` rather than curl_cffi, and it needs the proxy: the
operator's own address is blocked outright.

isOnLinkedIn. The site's whole pitch is postings you will NOT find on LinkedIn,
and it reports that per job. It is the only source here that does, so
`on_linkedin` is None everywhere else and the badge shows only on these rows.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from config import settings
from logger import log
from scraper.base_scraper import BaseScraper, ScrapedJob

_BASE = "https://www.remoterocketship.com"

#: The listing hands back 20 per page; `page` is 1-based.
_PAGE_SIZE = 20
#: Safety net. The dry streak below normally stops the run long before this.
_MAX_PAGES = 60
#: Consecutive pages with nothing new before calling it the end of the results.
_DRY_STREAK = 2

#: How far back "today" reaches, in hours.
#:
#: A rolling 24h window rather than a calendar day. The listing is sorted newest
#: first and stamps `created_at` in UTC, so a calendar-day cutoff would discard
#: everything posted yesterday evening the moment UTC midnight passed — jobs
#: that are hours old and still the freshest on the board.
_TODAY_HOURS = 24

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


def _next_data(html: str) -> dict:
    """The page's embedded JSON, or {} when the page is not what we expected.

    Returns empty rather than raising: a Cloudflare interstitial and a genuinely
    empty result page both arrive as "no jobs here", and the caller's dry-streak
    logic already handles that without a traceback.
    """
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {}


def _posted_at(raw: Optional[str]):
    """`created_at` is an ISO instant; store it as naive UTC like the others."""
    if not raw:
        return None
    try:
        # Python's parser wants +00:00, the site sends Z on some rows.
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).replace(tzinfo=None)
    except ValueError:
        return None


def _salary(sr: Optional[dict]) -> Optional[str]:
    """"$78,500 - $117,500 per year", or None when the site gives no figures."""
    if not isinstance(sr, dict):
        return None
    lo, hi = sr.get("min"), sr.get("max")
    if not lo and not hi:
        return None
    sym = sr.get("currencySymbol") or ""
    code = sr.get("currencyCode") or ""
    unit = sr.get("salaryType") or ""

    def fmt(v):
        return f"{sym}{int(v):,}" if isinstance(v, (int, float)) else None

    money = " - ".join([p for p in (fmt(lo), fmt(hi)) if p]) or None
    if not money:
        return None
    # Currency code only when the symbol is ambiguous — "$120,000 USD" is worth
    # saying, "£120,000 GBP" is not.
    tail = " ".join(p for p in (code if sym == "$" else "", unit) if p)
    return f"{money} {tail}".strip()


def _location(job: dict) -> Optional[str]:
    """Prefer the states list over the coarse country string when present."""
    states = job.get("locationUSStates")
    if isinstance(states, list) and states:
        return ", ".join(str(s) for s in states)[:255]
    for key in ("location", "locationCity"):
        v = job.get(key)
        if v:
            return str(v)[:255]
    return None


def _description(detail: dict, listing: dict) -> str:
    """One document from the detail page's three separate fields.

    Falls back to the listing's summary when the detail fetch failed. A summary
    is thin for resume generation, but a job with a short description still
    beats no job — and `save()` would drop it anyway if there were no apply URL,
    which is the thing that actually matters.
    """
    parts: list[str] = []
    for key, heading in (
        ("roleDescription", ""),
        ("roleRequirements", "Requirements"),
        ("benefits", "Benefits"),
    ):
        text = (detail.get(key) or "").strip()
        if not text:
            continue
        parts.append(f"{heading}\n{text}" if heading else text)
    if parts:
        return "\n\n".join(parts).strip()
    return (
        (listing.get("jobDescriptionSummary") or "")
        or (listing.get("twoLineJobDescriptionSummary") or "")
    ).strip()


class RemoteRocketshipScraper(BaseScraper):
    site = "remoterocketship"
    table = "jobs"
    #: Its own Chrome profile. The Cloudflare clearance cookie is bound to the
    #: TLS fingerprint and exit IP that earned it, so sharing a profile with a
    #: site on a different proxy invalidates it for both.
    user_data_dir = str(
        (__import__("pathlib").Path(settings.user_data_dir).parent / "remoterocketship")
    )

    def __init__(self):
        super().__init__()
        self._role = (
            re.compile(settings.remoterocketship_role_regex, re.I)
            if getattr(settings, "remoterocketship_role_regex", None)
            else None
        )

    # ── helpers ──────────────────────────────────────────────────────────
    def _matches(self, title: str) -> bool:
        return not self._role or bool(self._role.search(title or ""))

    def _saved(self) -> int:
        return self.counts["inserted"] + self.counts["updated"] + self.counts["unchanged"]

    def _list_url(self, page: int) -> str:
        """The configured search URL with `page` forced to ours.

        The setting holds the link you would paste from the site, so filters
        stay editable from the admin UI without anyone needing to know this
        endpoint exists — the same arrangement LinkedIn uses.
        """
        raw = (getattr(settings, "remoterocketship_search_url", "") or "").strip()
        if not raw:
            raw = f"{_BASE}/us/remote-jobs/?sort=DateAdded"
        base, _, query = raw.partition("?")
        kept = [
            p for p in query.split("&") if p and not p.lower().startswith("page=")
        ]
        kept.append(f"page={page}")
        return f"{base}?{'&'.join(kept)}"

    async def _payload(self, url: str) -> dict:
        """Navigate and return the page's `__NEXT_DATA__`."""
        page = self.browser.page
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as e:
            log.warning("[{}] navigation failed: {} — {}", self.site, url, e)
            return {}
        # The JSON is server-rendered into the document, so there is nothing to
        # wait for beyond the HTML itself. The short settle is for Cloudflare's
        # interstitial, which replaces the document when it fires.
        await asyncio.sleep(random.uniform(1.5, 3.0))
        try:
            return _next_data(await page.content())
        except Exception as e:
            log.warning("[{}] could not read the page: {}", self.site, e)
            return {}

    async def _detail(self, listing: dict) -> dict:
        """The full description, from the job's own page."""
        if not settings.fetch_descriptions:
            return {}
        company = ((listing.get("company") or {}).get("slug") or "").strip()
        slug = (listing.get("slug") or "").strip()
        if not company or not slug:
            return {}
        data = await self._payload(f"{_BASE}/us/publicjobs/company/{company}/jobs/{slug}/")
        props = (data.get("props") or {}).get("pageProps") or {}
        return props.get("jobOpening") or {}

    def _to_job(self, listing: dict, detail: dict) -> Optional[ScrapedJob]:
        site_job_id = str(listing.get("id") or "").strip()
        title = (listing.get("roleTitle") or "").strip()
        # The employer's own board, straight from the listing.
        apply_url = (listing.get("url") or "").strip() or None
        if not site_job_id or not title:
            return None

        company = listing.get("company") or {}
        company_slug = (company.get("slug") or "").strip()
        job_slug = (listing.get("slug") or "").strip()
        link = (
            f"{_BASE}/us/publicjobs/company/{company_slug}/jobs/{job_slug}/"
            if company_slug and job_slug
            else apply_url or _BASE
        )

        on_linkedin = listing.get("isOnLinkedIn")
        return ScrapedJob(
            site_job_id=site_job_id,
            title=title,
            description=_description(detail, listing),
            link=link,
            location=_location(listing),
            posted_at=_posted_at(listing.get("created_at")),
            apply_url=apply_url,
            company=(company.get("name") or "").strip() or None,
            company_url=(company.get("homePageURL") or "").strip() or None,
            job_type=(listing.get("employmentType") or None),
            # Every posting on this site is remote — `locationType` is "remote"
            # throughout and the site has no on-site listings to confuse it with.
            remote=True,
            salary=_salary(listing.get("salaryRange")),
            # Only stored when the site actually said so. A missing key is None,
            # not False: "not reported" and "not on LinkedIn" are different
            # facts, and the badge shows only the second.
            on_linkedin=bool(on_linkedin) if isinstance(on_linkedin, bool) else None,
        )

    # ── the run ──────────────────────────────────────────────────────────
    async def scrape(self) -> None:
        # Today's postings only. The listing is newest-first, so the first job
        # older than this means every job after it is older too — the run stops
        # there rather than paging through history it would only discard.
        cutoff = datetime.utcnow() - timedelta(hours=_TODAY_HOURS)
        log.info("[{}] taking postings newer than {} UTC",
                 self.site, cutoff.replace(microsecond=0))

        seen: set[str] = set()
        dry = 0
        delay = float(getattr(settings, "remoterocketship_delay_s", 2.0))

        for page_no in range(1, _MAX_PAGES + 1):
            if settings.max_jobs and self._saved() >= settings.max_jobs:
                log.info("[{}] hit max_jobs={} — stopping.", self.site, settings.max_jobs)
                break

            data = await self._payload(self._list_url(page_no))
            props = (data.get("props") or {}).get("pageProps") or {}
            openings: list[Any] = props.get("initialJobOpenings") or []

            if not openings:
                dry += 1
                log.info("[{}] page {} — no jobs ({}/{} dry)", self.site, page_no, dry, _DRY_STREAK)
                if dry >= _DRY_STREAK:
                    log.info("[{}] end of results.", self.site)
                    break
                await asyncio.sleep(delay)
                continue

            fresh = [j for j in openings if str(j.get("id")) not in seen]
            seen.update(str(j.get("id")) for j in openings)
            if not fresh:
                # Every id repeated: the site is serving the same page again,
                # which is what paging past the end looks like here.
                dry += 1
                log.info("[{}] page {} — all {} repeats ({}/{} dry)",
                         self.site, page_no, len(openings), dry, _DRY_STREAK)
                if dry >= _DRY_STREAK:
                    break
                continue
            dry = 0

            on_li = sum(1 for j in fresh if j.get("isOnLinkedIn") is True)
            log.info("[{}] page {} — {} jobs, {} new, {} of them on LinkedIn",
                     self.site, page_no, len(openings), len(fresh), on_li)

            # Newest-first means the page is ordered, so the first stale row
            # ends the run. Checked before any detail fetch: paying a request
            # per job only to discard it is the expensive way to be wrong.
            stale = [j for j in fresh if (_posted_at(j.get("created_at")) or cutoff) < cutoff]
            if stale:
                log.info("[{}] page {} — reached postings older than today, stopping.",
                         self.site, page_no)
                fresh = [j for j in fresh if j not in stale]

            for listing in fresh:
                if settings.max_jobs and self._saved() >= settings.max_jobs:
                    break
                title = (listing.get("roleTitle") or "").strip()
                if not self._matches(title):
                    continue
                detail = await self._detail(listing)
                job = self._to_job(listing, detail)
                if job:
                    self.save(job)
                await asyncio.sleep(random.uniform(delay * 0.6, delay * 1.4))

            if stale:
                break

            await asyncio.sleep(random.uniform(delay * 0.6, delay * 1.4))
        else:
            log.info("[{}] hit the {}-page budget (~{} jobs scanned)",
                     self.site, _MAX_PAGES, _MAX_PAGES * _PAGE_SIZE)
