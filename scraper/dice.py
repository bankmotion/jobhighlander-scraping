"""Dice scraper — the search page's own server-rendered payload, over HTTP.

dice.com is a Next.js App Router site, so every page ships its data twice: once
as rendered HTML and once as an RSC "flight" payload inside
`self.__next_f.push([1,"<chunk>"])` script tags. Concatenating those chunks gives
back the JSON the server used, which is far cleaner than scraping the DOM:

  search  /jobs?<filters>&page=N   -> jobList.data[]  (30 per page) + jobList.meta
  detail  /job-detail/<guid>       -> applyButtonData + the description markup

The search payload alone carries title, company, salary, employment type,
workplace types and an exact `postedDate` epoch, so the listing pass needs no
browser and no HTML parsing. `meta.pageCount` says how many pages exist — the
configured search filters to a single day, which is only ever a handful.

APPLY URL. This is the reason the detail page is fetched at all. Clicking "Apply
Now" on a Dice job goes to `dice.com/job-applications/<guid>/start-apply`, which
bounces to the employer's real ATS (Workday / Greenhouse / iCIMS / ADP / …). That
hop is a client-side Next.js redirect, NOT a 302 — following it with an HTTP
client just lands back on the Dice URL — but the destination it will use is
already in the detail page's flight payload:

    applyButtonData.jobApplyData.applicationDetail
        {"type": "APPLY_TO_URL",   "url": "https://…employer ATS…"}
        {"type": "APPLY_TO_EMAIL", "url": null, "email": "recruiter@…"}

so we read it straight from there instead of driving a browser through the
redirect. `APPLY_TO_EMAIL` postings (Dice's own "easy apply" — the application is
emailed to a recruiter) have no employer URL at all; those keep the Dice job page,
which is a real place a person applies, rather than being dropped.

SIGN-IN. `ensure_session("dice")` signs in with Google and reuses the cookies
from the DB. Dice serves `applicationDetail` to signed-out requests too, so the
pass is deliberately tolerant of a failed login rather than aborting on it — but
it stays signed in by default: that is the journey a real visitor takes, the
Apply button is login-gated in the UI, and an anonymous crawl is the first thing
a site tightens.
"""
from __future__ import annotations

import asyncio
import html as _html
import random
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from curl_cffi.requests import AsyncSession

from config import settings, proxy_for
from logger import log
from scraper.base_scraper import BaseScraper, ScrapedJob
from scraper.flight import flight_text as _flight_text, flight_value as _flight_value

_IMPERSONATE = "chrome"
#: Safety net only — `meta.pageCount` ends the walk long before this. The search
#: is filtered to one day, which has been ~3 pages.
_MAX_PAGES = 40

#: Query params that address a POSITION or UI state in the result set. The
#: setting holds the link you would paste out of Dice, so it carries whichever
#: card happened to be open (`selectedJobId`) and possibly a page number; we
#: drive paging ourselves and must not forward a conflicting one.
_POSITIONAL = {"page", "selectedjobid", "pagesize"}

#: Opening tag of the description block; the class is CSS-module-hashed
#: (`job-detail-description-module__EJDWFq__jobDescription`), so match the stable
#: suffix rather than the whole name.
_DESC_OPEN_RE = re.compile(r'<div[^>]*class="[^"]*jobDescription[^"]*"[^>]*>')
_DIV_TAG_RE = re.compile(r"<(/?)div\b[^>]*>", re.I)


def _description_html(html: str) -> str:
    """The description block, matched by DIV DEPTH rather than to the first
    `</div>`. Descriptions are employer-supplied markup: the ones seen so far are
    flat, but a single nested <div> would silently truncate the stored text."""
    m = _DESC_OPEN_RE.search(html)
    if not m:
        return ""
    depth = 1
    for tag in _DIV_TAG_RE.finditer(html, m.end()):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return html[m.end():tag.start()]
    return ""


def _clean_html(h: str) -> str:
    h = _html.unescape(h or "")
    h = h.replace("\r\n", "\n").replace("\r", "\n")
    h = re.sub(r"(?i)<\s*(br|/p|/div|/h[1-6]|/li|/tr|/ul)\s*/?>", "\n", h)
    h = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = _html.unescape(h).replace("\xa0", " ")
    h = re.sub(r"[ \t]+\n", "\n", h)
    return re.sub(r"\n{3,}", "\n\n", h).strip()


def _salary(raw) -> Optional[str]:
    """Tidy Dice's free-text salary.

    Employers type this field themselves, so it arrives in every shape:
    "165000 - 195000", "USD 96,600.00 - 130,400.00 per year", "$$170,000 -
    $190,000". Only the outright typos are fixed (a doubled currency sign, ".00"
    padding, stray whitespace) — the wording is left alone rather than reformatted
    into a house style that would misreport what the employer actually offered.
    """
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"\$\s*\$+", "$", s)          # "$$170,000" -> "$170,000"
    s = re.sub(r"(\d)\.00\b", r"\1", s)      # "130,400.00" -> "130,400"
    s = re.sub(r"\s+", " ", s).strip(" -–,")
    return s or None


def _posted_at(raw) -> Optional[datetime]:
    """`postedDate` is ISO-8601 UTC ("2026-09-03T21:06:22Z") — an exact instant,
    so keep the time of day. Stored naive UTC, like every other scraper."""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(
            timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _location(loc) -> Optional[str]:
    """Dice ships a structured location with a ready-made `displayName`
    ("San Diego, California, USA"). Fully-remote postings carry no location at
    all, which is honest — `remote` records that separately."""
    if not isinstance(loc, dict):
        return None
    name = (loc.get("displayName") or "").strip()
    if name:
        return name[:255]
    parts = [loc.get(k) for k in ("city", "state", "country") if loc.get(k)]
    return ", ".join(parts)[:255] or None


class DiceScraper(BaseScraper):
    site = "dice"
    table = "jobs"

    def __init__(self):
        super().__init__()  # repo + counts; the browser attribute stays unused
        self._role = (re.compile(settings.dice_role_regex, re.I)
                      if settings.dice_role_regex else None)
        self._proxies = None
        if proxy_for("dice"):
            p = proxy_for("dice")
            self._proxies = {"http": p, "https": p}

    # HTTP-only lifecycle — the only browser is the one ensure_session() opens
    # to sign in, and only when the DB has no usable session.
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

    # ── auth ────────────────────────────────────────────────────────────────
    @staticmethod
    async def _cookies() -> dict:
        """Signed-in cookie jar for curl_cffi, signing in via Google if needed.

        Best-effort by design: Dice serves the same `applicationDetail` to
        anonymous requests, so a login failure costs nothing today and must not
        take the whole pass down with it.
        """
        try:
            from scraper.auth.site_login import ensure_session
            return await ensure_session("dice") or {}
        except Exception as e:
            log.warning("[dice] sign-in unavailable, continuing signed out: {}", e)
            return {}

    # ── search ──────────────────────────────────────────────────────────────
    @staticmethod
    def _page_url(page: int) -> str:
        """The configured search URL with our own paging applied.

        Kept as a normal browsable dice.com link in settings so the filters
        (keywords, posted-date window, workplace type) stay editable from the
        admin UI without anyone needing to know about the flight payload.
        """
        u = urlparse(settings.dice_search_url)
        qs = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=False)
              if k.lower() not in _POSITIONAL]
        qs.append(("page", str(page)))
        return urlunparse(u._replace(query=urlencode(qs)))

    def _matches(self, title: str) -> bool:
        return not self._role or bool(self._role.search(title or ""))

    def _saved(self) -> int:
        return self.counts["inserted"] + self.counts["updated"] + self.counts["unchanged"]

    async def _search_page(self, session: AsyncSession, page: int) -> tuple[list, int]:
        """(jobs, pageCount) for one search page. (**[], 0**) on any failure."""
        try:
            r = await session.get(self._page_url(page), timeout=45)
        except Exception as e:
            log.warning("[dice] search fetch failed on page {}: {}", page, e)
            return [], 0
        if r.status_code != 200:
            log.warning("[dice] search page {} -> HTTP {}", page, r.status_code)
            return [], 0
        jl = _flight_value(_flight_text(r.text), "jobList")
        if not isinstance(jl, dict):
            log.warning("[dice] search page {} carried no jobList payload "
                        "(markup changed?)", page)
            return [], 0
        meta = jl.get("meta") or {}
        return (jl.get("data") or []), int(meta.get("pageCount") or 0)

    # ── detail ──────────────────────────────────────────────────────────────
    async def _detail(self, session: AsyncSession, guid: str) -> tuple[str, dict]:
        """(description, applicationDetail) for one posting.

        Both come out of a single GET: the description from the rendered markup,
        the apply target from the flight payload.
        """
        try:
            r = await session.get(f"https://www.dice.com/job-detail/{guid}", timeout=45)
        except Exception as e:
            log.warning("[dice] detail fetch failed for {}: {}", guid, e)
            return "", {}
        if r.status_code != 200:
            log.warning("[dice] detail {} -> HTTP {}", guid, r.status_code)
            return "", {}
        desc = _clean_html(_description_html(r.text)) if settings.fetch_descriptions else ""
        abd = _flight_value(_flight_text(r.text), "applyButtonData") or {}
        detail = ((abd.get("jobApplyData") or {}).get("applicationDetail")) or {}
        return desc, detail

    # ── mapping ─────────────────────────────────────────────────────────────
    def _to_job(self, jr: dict, description: str, apply_detail: dict) -> Optional[ScrapedJob]:
        guid = (jr.get("guid") or "").strip()
        if not guid:
            return None
        page_url = (jr.get("detailsPageUrl") or f"https://www.dice.com/job-detail/{guid}")
        # An employer ATS link when Dice has one; otherwise the Dice posting,
        # which is where an APPLY_TO_EMAIL application is actually submitted.
        # Never dropped to None — BaseScraper.save() discards a job without an
        # apply_url, and "apply through Dice" is a real answer, not a missing one.
        apply_url = (apply_detail.get("url") or "").strip() or page_url
        company_id = jr.get("companyProfileId")
        return ScrapedJob(
            site_job_id=guid[:190],
            title=(jr.get("title") or "").strip(),
            description=description,
            link=page_url,
            location=_location(jr.get("jobLocation")),
            posted_at=_posted_at(jr.get("postedDate")),
            apply_url=apply_url,
            company=(jr.get("companyName") or "").strip() or None,
            company_url=(f"https://www.dice.com/company-profile/{company_id}"
                         if company_id else None),
            job_type=(jr.get("employmentType") or None),
            # The configured search filters to workplaceTypes=Remote, but read
            # the posting's own flag rather than assuming the filter held.
            remote=bool(jr.get("isRemote")),
            salary=_salary(jr.get("salary")),
        )

    # ── run ─────────────────────────────────────────────────────────────────
    async def scrape(self) -> None:
        jar = await self._cookies()
        log.info("[dice] {}", f"signed in ({len(jar)} cookies)" if jar else "running signed out")
        delay = float(settings.dice_delay_s)

        async with AsyncSession(impersonate=_IMPERSONATE, proxies=self._proxies,
                                cookies=jar or None) as session:
            # ── 1. walk the listing ─────────────────────────────────────────
            # Collected whole before fetching details: the same posting can
            # appear on two pages (83 rows -> 77 unique in a sample run), and
            # de-duping first avoids paying for its detail page twice.
            listing: dict[str, dict] = {}
            pages = 1
            for page in range(1, _MAX_PAGES + 1):
                jobs, page_count = await self._search_page(session, page)
                if page == 1:
                    if not jobs:
                        log.warning("[dice] no jobs on page 1 — nothing to do.")
                        return
                    pages = min(page_count or 1, _MAX_PAGES)
                    log.info("[dice] {} page(s) of results", pages)
                if not jobs:
                    break
                new = sum(1 for j in jobs if (j.get("guid") or "") not in listing)
                for j in jobs:
                    g = (j.get("guid") or "").strip()
                    if g:
                        listing.setdefault(g, j)
                log.info("[dice] page {}/{} — {} cards, {} new (unique so far {})",
                         page, pages, len(jobs), new, len(listing))
                if page >= pages:
                    break
                await asyncio.sleep(random.uniform(delay * 0.6, delay * 1.4))
            if pages >= _MAX_PAGES:
                log.info("[dice] hit the {}-page budget — raise _MAX_PAGES to go deeper.",
                         _MAX_PAGES)

            # ── 2. detail + save, one job at a time ─────────────────────────
            candidates = [j for j in listing.values() if self._matches(j.get("title", ""))]
            log.info("[dice] {} unique posting(s), {} match the role filter",
                     len(listing), len(candidates))
            for i, jr in enumerate(candidates, 1):
                if settings.max_jobs and self._saved() >= settings.max_jobs:
                    log.info("[dice] reached max_jobs={} — stopping.", settings.max_jobs)
                    break
                guid = (jr.get("guid") or "").strip()
                if not guid:
                    continue
                # Age-gate BEFORE the detail fetch — a posting we are going to
                # discard should not cost an HTTP round trip. The search URL
                # already filters to one day; this is what still holds the line
                # if someone widens `filters.postedDate` in the admin UI and
                # forgets that the global window is a whole week.
                posted = _posted_at(jr.get("postedDate"))
                if self._too_old(posted, settings.dice_max_age_days):
                    self.counts["too_old"] += 1
                    log.info("[dice] skipped (posted {} — older than {}d) — {}",
                             posted, settings.dice_max_age_days,
                             (jr.get("title") or "")[:44])
                    continue
                desc, apply_detail = await self._detail(session, guid)
                job = self._to_job(jr, desc, apply_detail)
                if job is None:
                    continue
                if not apply_detail.get("url"):
                    # Worth naming: these are Dice's own email applications, so
                    # the row deliberately keeps the Dice page as its apply URL.
                    log.info("[dice] {}/{} {} — no employer URL ({}), using the Dice page",
                             i, len(candidates), (job.title or "")[:40],
                             apply_detail.get("type") or "unknown")
                self.save(job)
                await asyncio.sleep(random.uniform(delay * 0.6, delay * 1.4))


async def main() -> None:
    await DiceScraper().run()


if __name__ == "__main__":
    asyncio.run(main())
