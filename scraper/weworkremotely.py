"""WeWorkRemotely scraper — public HTTP, no login/browser needed.

Unlike Indeed/Glassdoor/JobRight, WWR's listing + detail pages are fully public
(no login interstitial), so this scraper skips the stealth browser and fetches
HTML with curl_cffi, impersonating Chrome's TLS+HTTP2 fingerprint — plain
HTTP/1.1 clients (aiohttp) get a Cloudflare 403 regardless of headers/IP. Routed
through the shared residential proxy for a stable US exit IP.

Each detail page carries a schema.org JobPosting JSON-LD block — the cleanest
structured source for title/description/company/date/salary/location. The apply
URL is required (base_scraper skips rows without one): we take it from the
detail page's unlocked "Apply" button, falling back to a link embedded in the
description ("How to apply: …"). Postings that only expose a *locked* apply
button (e.g. Toptal, which gates it behind a WWR account) yield no apply URL and
are skipped.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import random
import re
from collections import Counter
from datetime import datetime
from typing import Optional

from curl_cffi.requests import AsyncSession

from config import settings, proxy_for
from logger import log
from scraper.base_scraper import BaseScraper, ScrapedJob

# Impersonate a recent Chrome so curl_cffi replays its TLS/JA3 + HTTP2 fingerprint.
_IMPERSONATE = "chrome"

# URLs that are never a real apply target (WWR internal, socials, CDNs, ads).
_SKIP_URL = re.compile(
    r"weworkremotely\.com|/remote-jobs/|/company/|/account/|/job-seekers/|"
    r"/career-services/|/listing_ads/|cloudflare|imgix|gstatic|google|facebook|"
    r"twitter|linkedin|youtube|instagram|jsdelivr|cdnjs|fontawesome|jquery|"
    r"unpkg|tapfiliate|doubleclick|posthog|tiktok|clarity\.ms",
    re.I,
)

_EMPLOYMENT = {
    "FULL_TIME": "Full-Time",
    "PART_TIME": "Part-Time",
    "CONTRACTOR": "Contract",
    "TEMPORARY": "Temporary",
    "INTERN": "Internship",
    "OTHER": None,
}


def _html_to_text(h: str) -> str:
    """Flatten the JSON-LD description HTML into readable plain text (matches how
    the other scrapers store `description`). WWR entity-encodes the HTML
    (`&lt;h4&gt;`), so unescape FIRST — otherwise stripping tags does nothing and
    the later unescape re-creates them."""
    h = _html.unescape(h)  # &lt;h4&gt; -> <h4>  (reveal the real tags first)
    h = h.replace("\r\n", "\n").replace("\r", "\n")
    h = re.sub(r"(?i)<\s*(br|/p|/div|/h[1-6]|/tr)\s*/?>", "\n", h)
    h = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = _html.unescape(h).replace("\xa0", " ")  # residual entities + nbsp -> space
    h = re.sub(r"[ \t]+\n", "\n", h)
    h = re.sub(r"\n[ \t]+", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()


def _parse_date(s: Optional[str]):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _salary(base) -> Optional[str]:
    if not isinstance(base, dict):
        return None
    val = base.get("value")
    cur = (base.get("currency") or "").strip()

    def _num(x) -> int:
        try:
            return int(float(x))
        except (TypeError, ValueError):
            return 0

    if isinstance(val, dict):
        mn, mx = _num(val.get("minValue")), _num(val.get("maxValue"))
        unit = (val.get("unitText") or "").strip().lower()
        per = {"year": "/yr", "hour": "/hr", "month": "/mo", "week": "/wk", "day": "/day"}.get(unit, "")
        if mn and mx:  # 0 / missing → no salary (not "USD 0–0")
            return f"{cur} {mn:,}–{mx:,}{per}".strip()
        if mn or mx:
            return f"{cur} {(mn or mx):,}{per}".strip()
    else:
        n = _num(val)
        if n:
            return f"{cur} {n:,}".strip()
    return None


def _employment(v) -> Optional[str]:
    if isinstance(v, list):
        v = v[0] if v else None
    if not v:
        return None
    return _EMPLOYMENT.get(str(v).upper().strip(), str(v).title())


def _location(jp: dict) -> Optional[str]:
    names = []
    alr = jp.get("applicantLocationRequirements")
    for x in (alr if isinstance(alr, list) else [alr] if alr else []):
        if isinstance(x, dict) and x.get("name"):
            names.append(x["name"].strip())
    if names:
        return ", ".join(dict.fromkeys(names))
    jl = jp.get("jobLocation")
    for x in (jl if isinstance(jl, list) else [jl] if jl else []):
        addr = (x or {}).get("address") if isinstance(x, dict) else None
        if isinstance(addr, dict):
            for k in ("addressCountry", "addressRegion", "addressLocality"):
                if addr.get(k):
                    return str(addr[k]).strip()
    return None


class WeWorkRemotelyScraper(BaseScraper):
    site = "weworkremotely"
    #: Promoted to the live table — WWR jobs show in the app alongside the others.
    table = "jobs"

    BASE = "https://weworkremotely.com"

    def __init__(self):
        super().__init__()  # sets up self.repo + counts (browser stays unused)
        self.listing_url = settings.weworkremotely_search_url
        self._proxies = None
        if proxy_for("weworkremotely"):
            self._proxies = {"http": proxy_for("weworkremotely"), "https": proxy_for("weworkremotely")}
        self._cookies = {}  # filled in run() — may need an interactive sign-in

    # ── HTTP-only lifecycle (override the browser-based BaseScraper.run) ──────
    async def run(self) -> None:
        # Some employers (Toptal et al.) only show the real apply URL to a
        # signed-in job seeker; logged out those postings yield no apply_url and
        # get skipped. ensure_session() reuses the DB session and only opens a
        # browser when there isn't one. (WWR's big-name postings sit behind its
        # PAID plan — /job-seekers/onboarding/step_3?context=paywall — which no
        # sign-in can unlock.)
        try:
            from scraper.auth.site_login import ensure_session
            self._cookies = await ensure_session("weworkremotely")
        except Exception as e:
            log.warning("[wwr] sign-in skipped: {}", e)
            self._cookies = {}
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

    async def _get(self, session: AsyncSession, url: str) -> Optional[str]:
        try:
            r = await session.get(url, timeout=45)
            if r.status_code != 200:
                log.warning("[wwr] {} -> HTTP {}", url, r.status_code)
                return None
            text = r.text
            if "Just a moment" in text or "Performing security verification" in text:
                log.warning("[wwr] {} -> Cloudflare challenge (skipped)", url)
                return None
            return text
        except Exception as e:
            log.warning("[wwr] fetch error {}: {}", url, e)
            return None

    async def scrape(self) -> None:
        # A single staffing agency (e.g. Proxify) can flood the listing, so cap
        # how many SAVED jobs any one company contributes per run.
        cap = settings.weworkremotely_max_per_company
        saved_by_company: Counter = Counter()
        async with AsyncSession(impersonate=_IMPERSONATE, proxies=self._proxies,
                                cookies=self._cookies or None) as session:
            listing = await self._get(session, self.listing_url)
            if not listing:
                log.error("[wwr] could not load listing {}", self.listing_url)
                return
            slugs = self._listing_slugs(listing)
            limit = settings.max_jobs or None  # None = no count cap
            log.info("[wwr] {} jobs on listing; scraping up to {}", len(slugs), limit or "all")

            for slug in slugs[:limit]:
                detail = await self._get(session, self.BASE + slug)
                if detail:
                    job = self._parse_detail(detail, slug)
                    if job:
                        key = (job.company or "").strip().lower()
                        if cap and key and saved_by_company[key] >= cap:
                            log.info("[wwr] skipped (>{} from {}) — {}", cap, job.company, job.site_job_id)
                        else:
                            result = self.save(job)  # base_scraper skips it if apply_url is empty
                            if key and result != "skipped":
                                saved_by_company[key] += 1
                await asyncio.sleep(random.uniform(0.4, 1.1))  # be polite

    #: WWR feature pages that live under /remote-jobs/ but aren't job postings.
    _NON_JOB = ("/apply", "find-your-plan", "job-copilot")

    def _listing_slugs(self, html: str) -> list[str]:
        """Ordered, de-duplicated `/remote-jobs/<slug>` paths from the listing."""
        out: list[str] = []
        for s in re.findall(r'href="(/remote-jobs/[^"#?]+)"', html):
            if s not in out and not any(x in s for x in self._NON_JOB):
                out.append(s)
        return out

    def _apply_url(self, detail_html: str, desc_html: str) -> Optional[str]:
        # 1) The detail page's own apply button, when it carries a real (external) URL.
        for href in re.findall(r'<a[^>]*id="job-cta-alt"[^>]*href="([^"]+)"', detail_html):
            u = _html.unescape(href)
            if u.startswith("http") and not _SKIP_URL.search(u):
                return u
        # 2) A link embedded in the description ("How to apply: <a>…").
        for href in re.findall(r'href="(https?://[^"]+)"', desc_html):
            u = _html.unescape(href)
            if not _SKIP_URL.search(u):
                return u
        return None

    def _job_posting(self, html: str) -> Optional[dict]:
        for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
            try:
                # WWR embeds literal newlines in the JSON-LD description, which is
                # invalid JSON under the default strict parser — allow control chars.
                data = json.loads(block.strip(), strict=False)
            except Exception:
                continue
            for it in (data if isinstance(data, list) else [data]):
                t = it.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    return it
        return None

    def _parse_detail(self, html: str, slug: str) -> Optional[ScrapedJob]:
        jp = self._job_posting(html)
        if not jp:
            log.warning("[wwr] no JobPosting JSON-LD for {}", slug)
            return None
        raw_desc = jp.get("description") or ""
        org = jp.get("hiringOrganization") or {}
        company = org.get("name") if isinstance(org, dict) else None
        company_url = None
        if isinstance(org, dict):
            same = org.get("sameAs") or org.get("url")
            if isinstance(same, str) and not _SKIP_URL.search(same):
                company_url = same

        return ScrapedJob(
            site_job_id=slug.rsplit("/", 1)[-1],
            title=(jp.get("title") or "").strip(),
            description=_html_to_text(raw_desc),
            link=self.BASE + slug,
            location=_location(jp),
            posted_at=_parse_date(jp.get("datePosted")),
            apply_url=self._apply_url(html, raw_desc),
            company=(company or "").strip() or None,
            company_url=company_url,
            job_type=_employment(jp.get("employmentType")),
            remote=True,  # every WWR posting is remote
            salary=_salary(jp.get("baseSalary")),
        )
