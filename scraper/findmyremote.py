"""FindMyRemote scraper — public JSON API, no login, no browser, no Cloudflare.

By far the easiest source we have: `findmyremote.ai/api/jobs` is public, answers
200 to plain curl_cffi, and — unlike Himalayas — hands us the EMPLOYER'S OWN
apply URL directly in the listing (`url` → Lever / Greenhouse / Rippling /
Recruitee / …). No sign-in, no modal, no apply-URL resolution pass.

API contract (reverse-engineered — it is undocumented):
  • 21 jobs per page, newest first; `limit`/`page`/`offset` are IGNORED.
  • Pagination is by cursor: `?cursor=<id of the last job you saw>`, since ids
    descend. Anything else silently returns page 1 again.
  • Filters mirror the site's own URL, so `findmyremote_search_url` is stored as
    a normal browsable link and its query string is forwarded verbatim
    (`employmentType=fulltime&employmentType=parttime&location=us`, `category=…`).

The listing has no description, so each job's detail page is fetched when
`fetch_descriptions` is on. That page is a Next.js App Router stream: the JSON
carries `"description":"$13"`, a REFERENCE to RSC chunk 13, whose body holds the
real HTML — hence `_description_from_rsc()` rather than a plain regex.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse

from curl_cffi.requests import AsyncSession

from config import settings
from logger import log
from scraper.base_scraper import BaseScraper, ScrapedJob

_IMPERSONATE = "chrome"
_API = "https://findmyremote.ai/api/jobs"
_BASE = "https://findmyremote.ai"
_PAGE_SIZE = 21          # fixed server-side
_MAX_PAGES = 60          # safety net; the age cutoff normally stops us first

_EMPLOYMENT = {
    "fulltime": "Full-Time",
    "parttime": "Part-Time",
    "contract": "Contract",
    "internship": "Internship",
    "temporary": "Temporary",
}


def _clean_html(h: str) -> str:
    h = _html.unescape(h or "")
    h = h.replace("\r\n", "\n").replace("\r", "\n")
    h = re.sub(r"(?i)<\s*(br|/p|/div|/h[1-6]|/li|/tr)\s*/?>", "\n", h)
    h = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = _html.unescape(h).replace("\xa0", " ")
    h = re.sub(r"[ \t]+\n", "\n", h)
    return re.sub(r"\n{3,}", "\n\n", h).strip()


def _description_from_rsc(html: str) -> str:
    """Pull the job description out of the Next.js RSC stream.

    The payload says `"description":"$13"` — a pointer, not the text. The body
    lives in a separate chunk emitted as `13:T<hexlen>,<html…>`, so resolve the
    reference and then read that chunk.
    """
    raw = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    if not raw:
        return ""
    # Each push payload is a JSON string literal. Decode it AS JSON — NOT with
    # `unicode_escape`, which maps bytes through latin-1 and turns every UTF-8
    # character into mojibake ("Airflow®" -> "AirflowÂ®").
    parts = []
    for c in raw:
        try:
            parts.append(json.loads('"' + c + '"'))
        except Exception:
            parts.append(c)
    blob = "".join(parts)

    m = re.search(r'"description":"\$([0-9a-fA-F]+)"', blob)
    if not m:
        # Occasionally the text is inlined instead of referenced.
        inline = re.search(r'"description":"(.{80,}?)","', blob, re.S)
        return _clean_html(inline.group(1)) if inline else ""
    ref = m.group(1)
    # Chunks are emitted as `<id>:T<hexlen>,<payload>` — the header carries the
    # payload's EXACT length, so slice by it. Scanning ahead to "the next chunk"
    # instead is what let JS chunk headers (`16:I[...]`) leak into the text,
    # since a chunk id can be followed by T, I, J, ...
    head = re.search(rf'(?<![0-9a-fA-F]){re.escape(ref)}:T([0-9a-f]+),', blob)
    if not head:
        return ""
    start = head.end()
    try:
        # The length is a BYTE count, so slice UTF-8 bytes, not characters —
        # counting characters overruns by one per multi-byte char ("•", "®") and
        # drags the next chunk's header into the text.
        nbytes = int(head.group(1), 16)
        return _clean_html(blob[start:].encode("utf-8")[:nbytes].decode("utf-8", errors="ignore"))
    except Exception:
        return ""


def _employment(types) -> Optional[str]:
    if not types:
        return None
    names = [_EMPLOYMENT.get(str(t).lower(), str(t).title()) for t in types]
    return ", ".join(dict.fromkeys(names)) or None


class FindMyRemoteScraper(BaseScraper):
    site = "findmyremote"
    #: Live table — the API hands us the employer's own apply URL directly, so
    #: these rows need no resolution pass and are app-ready as scraped.
    table = "jobs"

    def __init__(self):
        super().__init__()  # sets up self.repo + counts (browser stays unused)
        self._role = (re.compile(settings.findmyremote_role_regex, re.I)
                      if settings.findmyremote_role_regex else None)
        self._proxies = None
        if settings.proxy_url:
            self._proxies = {"http": settings.proxy_url, "https": settings.proxy_url}

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

    @staticmethod
    def _api_query() -> str:
        """Forward the configured search URL's filters to the API verbatim.

        The setting holds a normal browsable link (what you'd paste from the site),
        so filters stay editable from the admin UI without knowing the API.
        """
        try:
            qs = parse_qsl(urlparse(settings.findmyremote_search_url).query, keep_blank_values=False)
        except Exception:
            qs = []
        return urlencode(qs) if qs else ""

    @staticmethod
    def _posted(job: dict):
        try:
            return datetime.fromisoformat(str(job.get("createdAt")).replace("Z", "+00:00"))
        except Exception:
            return None

    def _matches(self, job: dict) -> bool:
        return not self._role or bool(self._role.search(job.get("title") or ""))

    def _saved(self) -> int:
        return self.counts["inserted"] + self.counts["updated"] + self.counts["unchanged"]

    async def _description(self, session: AsyncSession, job: dict) -> str:
        if not settings.fetch_descriptions:
            return ""
        co = (job.get("company") or {}).get("slug") or ""
        url = f"{_BASE}/companies/{co}/jobs/{job.get('slug')}"
        try:
            r = await session.get(url, timeout=45)
            return _description_from_rsc(r.text) if r.status_code == 200 else ""
        except Exception as e:
            log.warning("[findmyremote] detail fetch failed for {}: {}", job.get("slug"), e)
            return ""

    def _to_job(self, job: dict, description: str) -> Optional[ScrapedJob]:
        apply_url = (job.get("url") or "").strip()
        if not apply_url:
            return None  # base_scraper would skip it anyway; don't waste a detail fetch
        company = job.get("company") or {}
        co_slug = company.get("slug")
        posted = self._posted(job)
        countries = job.get("countries") or []
        return ScrapedJob(
            site_job_id=str(job.get("id"))[:190],
            title=(job.get("title") or "").strip(),
            description=description,
            link=f"{_BASE}/companies/{co_slug}/jobs/{job.get('slug')}" if co_slug else apply_url,
            location=", ".join(str(c).upper() for c in countries) or None,
            posted_at=posted.date() if posted else None,
            apply_url=apply_url,  # the EMPLOYER's own ATS link, straight from the API
            company=(company.get("name") or "").strip() or None,
            company_url=(f"{_BASE}/companies/{co_slug}" if co_slug else None),
            job_type=_employment(job.get("employmentTypes")),
            remote=True,  # the whole board is remote-only
            salary=None,  # not exposed by the API
        )

    async def scrape(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.max_age_days or 3650)
        query = self._api_query()
        log.info("[findmyremote] filters: {}", query or "(none)")
        cursor: Optional[int] = None

        async with AsyncSession(impersonate=_IMPERSONATE, proxies=self._proxies) as session:
            for page in range(_MAX_PAGES):
                if settings.max_jobs and self._saved() >= settings.max_jobs:
                    break
                url = f"{_API}?{query}" if query else _API
                if cursor is not None:
                    url += ("&" if "?" in url else "?") + f"cursor={cursor}"
                try:
                    r = await session.get(url, timeout=45)
                    data = json.loads(r.text)
                except Exception as e:
                    log.warning("[findmyremote] fetch error (cursor={}): {}", cursor, e)
                    break
                jobs = data.get("jobs") or []
                if not jobs:
                    break
                log.info("[findmyremote] page {} ({} jobs, total {})",
                         page + 1, len(jobs), data.get("totalCount"))

                stop = False
                for jr in jobs:
                    posted = self._posted(jr)
                    if posted and posted < cutoff:  # newest-first → the rest are older
                        stop = True
                        break
                    if not self._matches(jr):
                        continue
                    desc = await self._description(session, jr)
                    job = self._to_job(jr, desc)
                    if job:
                        self.save(job)
                if stop:
                    log.info("[findmyremote] reached postings older than {}d — stopping.",
                             settings.max_age_days)
                    break
                if len(jobs) < _PAGE_SIZE:
                    break
                cursor = jobs[-1]["id"]  # ids descend; cursor is "last id I saw"
                await asyncio.sleep(random.uniform(0.4, 1.0))
            else:
                log.info("[findmyremote] hit the {}-page budget (~{} listings scanned)",
                         _MAX_PAGES, _MAX_PAGES * _PAGE_SIZE)
