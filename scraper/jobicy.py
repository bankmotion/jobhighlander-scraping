"""Jobicy scraper — real browser, because everything useful here is behind JS + Cloudflare.

Why a browser and not the API: Jobicy's public API (`/api/v2/remote-jobs`) returns
`url` = the JOBICY page, never the employer's, and it caps at 100 results with no
pagination. The employer's real ATS link only appears when the "Apply Now" BUTTON
is clicked — it opens the destination in a new tab:

    listing page -> job page -> click "Apply Now" -> new tab -> employer ATS
                                                    (Greenhouse / Ashby / Comeet / …)

"Apply for this job" is a decoy: an anchor to `#job-application` that merely scrolls
to a panel headed "Continue on the employer website". Matching it instead of the
button yields nothing, so always target the BUTTON.

RATE LIMITS ARE REAL. Jobicy sits behind Cloudflare and escalates fast: a burst of
requests earns "Too Many Requests — verify you are human", after which challenges
that cleared in ~10s start taking ~70s and three clicks. So this scraper
  * waits `jobicy_delay_s` between jobs (default 8s — deliberately slow),
  * SKIPS job pages already stored for this site, so repeat runs cost almost
    nothing, and
  * stops after `_MAX_CF_FAILURES` consecutive challenge failures instead of
    grinding a soft block into a hard one.
"""
from __future__ import annotations

import asyncio
import html as _html
import random
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from config import settings
from logger import log
from scraper.base_scraper import BaseScraper, ScrapedJob
from scraper.browser import clear_challenge

_BASE = "https://jobicy.com"
_MAX_CF_FAILURES = 3
#: Internal runaway guard only. The search URL already bounds the result set
#: (filter_by_day), so pagination normally ends on its own when a page comes
#: back empty — this just stops a misconfigured filter looping forever.
_PAGE_GUARD = 50

#: Hosts that are never the employer's application target.
_JUNK_HOST_RE = re.compile(
    r"(jobicy\.com|challenges\.cloudflare|cloudflare\.com|google\.|facebook\.|twitter\.|x\.com|"
    r"instagram\.|youtube\.|t\.me|discord\.|apple\.com|play\.google|linkedin\.com/(company|feed))",
    re.I,
)


def _is_employer_url(url: str) -> bool:
    """Employer/ATS link? Match scheme+host+path only — never the query string,
    since tracking params routinely carry the aggregator's own domain (the bug
    that silently discarded every good Himalayas URL)."""
    if not url or not url.startswith("http"):
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    return bool(p.hostname) and not _JUNK_HOST_RE.search(f"{p.scheme}://{p.netloc}{p.path}")


def _clean(t: str) -> str:
    t = _html.unescape(t or "")
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _parse_posted(text: str) -> Optional[date]:
    """Job pages print the date as e.g. '18 Aug 2026 Published'."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", text or "")
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)[:3]} {m.group(3)}", "%d %b %Y").date()
    except ValueError:
        return None


class JobicyScraper(BaseScraper):
    site = "jobicy"
    #: Live table — every stored row already carries the employer's own apply
    #: URL (rows without one are skipped), so they are app-ready as scraped.
    table = "jobs"

    def __init__(self):
        super().__init__()
        self._role = (re.compile(settings.jobicy_role_regex, re.I)
                      if settings.jobicy_role_regex else None)
        self._delay = max(1.0, float(settings.jobicy_delay_s))
        self._seen: set = set()

    # Bespoke browser lifecycle (real Chrome over CDP), so BaseScraper.run()'s
    # StealthBrowser path is deliberately not used.
    async def run(self) -> None:
        from scraper.auth.site_login import kill_chrome_tree, launch_chrome, session_file
        from scraper.session import SessionStore

        self.repo.connect()
        proc = relay = pw = None
        try:
            # Skip detail pages we already hold — by far the biggest saving
            # against a site that rate-limits this hard.
            try:
                self._seen = self.repo.existing_keys(self.site)
                log.info("[jobicy] {} jobs already stored — their pages will be skipped",
                         len(self._seen))
            except Exception:
                self._seen = set()

            from patchright.async_api import async_playwright
            server = None
            if settings.jobicy_use_proxy and settings.proxy_url:
                # Chrome cannot send proxy credentials; front it with the relay.
                from scraper.local_proxy import LocalRoutingProxy
                direct = settings.proxy_bypass.split(",") if settings.proxy_bypass else []
                relay = LocalRoutingProxy(settings.proxy_url, direct)
                server = "http://127.0.0.1:%d" % await relay.start()
            profile = str(Path(settings.user_data_dir).parent / "jobicy-chrome")
            proc, endpoint = launch_chrome(profile, int(settings.jobicy_cdp_port), server)
            pw = await async_playwright().start()
            browser = await pw.chromium.connect_over_cdp(endpoint)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.set_default_timeout(60000)
            # Google session comes from the DB, never a local file (same as the others).
            try:
                await SessionStore.load(ctx, None, session_file("google"))
            except Exception:
                pass

            await self.scrape_browser(ctx, page)
            log.info(
                "[{}] done — inserted={inserted} updated={updated} unchanged={unchanged} "
                "skipped={skipped} too_old={too_old}",
                self.site, **self.counts,
            )
        finally:
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass
            try:
                kill_chrome_tree(proc)  # tree-kill: terminate() orphans renderers
            except Exception:
                pass
            if relay:
                try:
                    await relay.stop()
                except Exception:
                    pass
            self.repo.close()

    @staticmethod
    def _page_url(base: str, n: int) -> str:
        """Jobicy paginates as /jobs/page/N/?<filters>, keeping the query string."""
        if n <= 1:
            return base
        u = urlparse(base)
        path = u.path.rstrip("/")
        return f"{u.scheme}://{u.netloc}{path}/page/{n}/" + (f"?{u.query}" if u.query else "")

    @staticmethod
    def _job_id(url: str) -> str:
        m = re.search(r"/jobs/(\d+)", url or "")
        return m.group(1) if m else (url or "")[-190:]

    async def _job_links(self, page) -> list:
        return await page.evaluate(
            """() => [...new Set(Array.from(document.querySelectorAll('a[href*="/jobs/"]'))
                 .map(a => a.href).filter(h => /\\/jobs\\/\\d+/.test(h)))]""")

    async def _employer_url(self, ctx, page) -> Optional[str]:
        """Click "Apply Now" and keep the URL of the tab it opens.

        Must be the BUTTON — the "Apply for this job" ANCHOR only jumps to
        #job-application and never navigates anywhere."""
        tabs: list = []

        def _on_page(p):
            tabs.append(p)

        ctx.on("page", _on_page)
        try:
            btn = await page.query_selector("button:has-text('Apply Now')")
            if not btn:
                return None
            try:
                await btn.click(timeout=8000)
            except Exception:
                return None
            for _ in range(8):
                await asyncio.sleep(1.5)
                if tabs:
                    break
            found = None
            for t in tabs:
                try:
                    if _is_employer_url(t.url):
                        found = t.url
                except Exception:
                    pass
                try:
                    await t.close()
                except Exception:
                    pass
            return found
        finally:
            try:
                ctx.remove_listener("page", _on_page)
            except Exception:
                pass

    async def _scrape_job(self, ctx, page, url: str) -> Optional[ScrapedJob]:
        body = ((await page.inner_text("body")) or "")
        page_title = (await page.title()) or ""
        title = re.sub(r"\s+", " ", (await page.evaluate(
            "() => (document.querySelector('h1')?.innerText || document.title || '').trim()"))).strip()
        if self._role and not self._role.search(title):
            return None

        posted = _parse_posted(body)
        if self._too_old(posted):
            self.counts["too_old"] += 1
            log.info("[jobicy] skipped (posted {} — older than {}d) — {}",
                     posted, settings.max_age_days, title[:48])
            return None

        apply_url = await self._employer_url(ctx, page)
        if not apply_url:
            return None  # never store a Jobicy link as the apply target

        # Trim the site chrome that follows the description.
        desc = body
        for marker in ("NEXT STEP", "Role snapshot", "Apply for this job"):
            i = desc.find(marker)
            if i > 400:
                desc = desc[:i]
                break

        company = None
        m = re.search(r"\bat\s+(.+?)\s+-\s+Jobicy", page_title)
        if m:
            company = m.group(1).strip()[:255]
        location = None
        m = re.search(r"Remote from\s+([^\n]{2,60})", body)
        if m:
            location = m.group(1).strip()
        job_type = None
        m = re.search(r"Employment\s+([A-Za-z \-]{3,24})", body)
        if m:
            job_type = m.group(1).strip()
        salary = None
        m = re.search(r"Salary\s+([^\n]{2,40})", body)
        if m and "undisclosed" not in m.group(1).lower():
            salary = m.group(1).strip()

        # The job page carries a "View company" link — keep it so the app can
        # deep-link to the employer's profile instead of showing only a name.
        company_url = await page.evaluate(
            """() => {
                const a = Array.from(document.querySelectorAll('a'))
                    .find(e => /view company/i.test(e.innerText || ''));
                if (a && a.href) return a.href;
                const c = Array.from(document.querySelectorAll('a[href*="/company/"]'))
                    .map(e => e.href)[0];
                return c || '';
            }""") or None

        return ScrapedJob(
            site_job_id=self._job_id(url),
            title=title[:500],
            description=_clean(desc),
            link=url,
            location=location,
            posted_at=posted,
            apply_url=apply_url,
            company=company,
            company_url=(company_url[:1024] if company_url else None),
            job_type=job_type,
            remote=True,  # Jobicy is remote-only
            salary=salary,
        )

    def _saved(self) -> int:
        return self.counts["inserted"] + self.counts["updated"] + self.counts["unchanged"]

    async def scrape_browser(self, ctx, page) -> None:
        base = settings.jobicy_search_url
        log.info("[jobicy] listing: {}", base)
        cf_fails = 0
        for pageno in range(1, _PAGE_GUARD + 1):
            if settings.max_jobs and self._saved() >= settings.max_jobs:
                break
            url = self._page_url(base, pageno)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log.warning("[jobicy] listing page {} failed: {}", pageno, str(e)[:70])
                break
            if not await clear_challenge(page, max_wait_s=120):
                cf_fails += 1
                log.warning("[jobicy] listing page {} — Cloudflare not cleared", pageno)
                if cf_fails >= _MAX_CF_FAILURES:
                    log.warning("[jobicy] repeated Cloudflare failures — stopping so the block "
                                "can decay (wait ~30 min before re-running)")
                    break
                continue
            cf_fails = 0
            await asyncio.sleep(3)

            links = await self._job_links(page)
            if not links:
                log.info("[jobicy] page {} listed no jobs — end of the filtered set", pageno)
                break
            fresh = [x for x in links if self._job_id(x) not in self._seen]
            log.info("[jobicy] page {}: {} jobs ({} new)", pageno, len(links), len(fresh))

            for link in fresh:
                if settings.max_jobs and self._saved() >= settings.max_jobs:
                    return
                try:
                    await page.goto(link, wait_until="domcontentloaded", timeout=60000)
                    if not await clear_challenge(page, max_wait_s=120):
                        cf_fails += 1
                        log.warning("[jobicy] {} — Cloudflare not cleared", link[-44:])
                        if cf_fails >= _MAX_CF_FAILURES:
                            log.warning("[jobicy] repeated Cloudflare failures — stopping")
                            return
                        continue
                    cf_fails = 0
                    await asyncio.sleep(2)
                    job = await self._scrape_job(ctx, page, link)
                    if job:
                        self.save(job)
                        self._seen.add(job.site_job_id)
                except Exception as e:
                    log.warning("[jobicy] {} error: {}", link[-44:], str(e)[:70])
                # Deliberately slow: this site rate-limits hard.
                await asyncio.sleep(self._delay + random.uniform(0, 2.0))
