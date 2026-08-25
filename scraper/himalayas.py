"""Himalayas scraper — public JSON API, no login/browser.

`himalayas.app/jobs/api` is a public, paginated, newest-first JSON feed — clean
structured data, curl_cffi gets 200. We page until we reach jobs older than
max_age_days, filtering to a country (locationRestrictions) and a role regex.
`limit` is capped server-side at 20, so asking for more silently returns 20 (and
stepping `offset` by more than 20 would skip listings).

The apply link points to the Himalayas job PAGE, not the employer's own form,
because Himalayas doesn't publish the employer URL anywhere we can reach:
  • the API/RSS never carry it (no job description contains an external link);
  • the job page is Cloudflare-challenged (403 to plain HTTP — tools/
    resolve_himalayas_apply.py drives a real browser through it); and
  • even past Cloudflare, "Apply now" is `/signup/talent?...` and the page's
    JSON-LD says `"directApply": false` — it's login-gated.
The Himalayas job page is still a working apply destination; the visitor signs up
there. Run tools/resolve_himalayas_apply.py with a signed-in profile to upgrade
rows to real employer URLs.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

from curl_cffi.requests import AsyncSession

from config import settings, proxy_for
from logger import log
from scraper.base_scraper import BaseScraper, ScrapedJob
from scraper.browser import clear_challenge

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
        # Route through the shared residential proxy (same as WWR) so the API
        # isn't hit from the server's own datacenter IP.
        self._proxies = None
        if proxy_for("himalayas"):
            self._proxies = {"http": proxy_for("himalayas"), "https": proxy_for("himalayas")}

    # HTTP-only lifecycle (no browser) + a browser pass for the apply URLs.
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
        # The API only ever gives us the Himalayas job page. Upgrading rows to the
        # employer's real apply URL needs a signed-in browser (the link only
        # exists inside the "Apply now" modal), so it runs as a second pass here
        # rather than as a tool you have to remember to run.
        if settings.himalayas_resolve_apply:
            try:
                await resolve_pending()
            except Exception as e:
                log.warning("[himalayas] apply-URL resolution skipped: {}", e)

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
        async with AsyncSession(impersonate=_IMPERSONATE, proxies=self._proxies) as session:
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


# ══════════════════════════════════════════════════════════════════════════════
# Apply-URL resolution — a second pass, in a signed-in browser.
#
# The API's `applicationLink` is only the Himalayas job page. The employer's real
# ATS link exists ONLY inside the "Apply now" modal on that page, and only for a
# signed-in visitor:
#     "Apply now" (button) -> modal -> "I'm ready to apply"
#     -> himalayas.app/apply/<code> -> 302 -> employer ATS
#        (Greenhouse / Workday / Lever / BambooHR / iCIMS / …)
# The <code> is in no served HTML, so this must be driven with a browser rather
# than scraped. Cloudflare also challenges these pages; a genuine Chrome attached
# over CDP clears it (see scraper/browser.py `clear_challenge`).
# ══════════════════════════════════════════════════════════════════════════════

#: Hosts that show up on a job page but are never the employer's apply link.
_JUNK_HOST_RE = re.compile(
    r"(himalayas\.app|challenges\.cloudflare|cloudflare\.com|google\.|facebook\.|twitter\.|x\.com|"
    r"linkedin\.com/(company|feed)|instagram\.|youtube\.|t\.me|discord\.|apple\.com|play\.google)",
    re.I,
)


def _is_employer_url(url: str) -> bool:
    """True if `url` is a real employer/ATS link.

    Match scheme+host+path ONLY, never the query string: Himalayas appends
    `?utm_source=himalayas.app&utm_medium=himalayas.app` to the destination, so
    testing the whole URL rejects every genuine employer link it finds.
    """
    if not url or not url.startswith("http"):
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    return bool(p.hostname) and not _JUNK_HOST_RE.search(f"{p.scheme}://{p.netloc}{p.path}")


def _off_himalayas(url: str) -> bool:
    """Host-based check — the utm_* params also contain 'himalayas.app'."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return bool(host) and not host.endswith("himalayas.app")


def _pending_rows(limit: int = 0) -> list:
    """Rows whose apply_url is still the Himalayas page.

    Prefix match, NOT '%himalayas.app%' — a RESOLVED row's employer URL still
    carries `utm_source=himalayas.app`, so a substring match would drag every
    already-finished job back into the queue.
    """
    import pymysql
    conn = pymysql.connect(
        host=settings.db_host, port=settings.db_port, user=settings.db_user,
        password=settings.db_password, database=settings.db_name,
        charset="utf8mb4", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, apply_url FROM jobs "
                "WHERE site='himalayas' AND (apply_url LIKE 'https://himalayas.app/%%' "
                "                            OR apply_url IS NULL OR apply_url = '') "
                "ORDER BY id DESC" + (" LIMIT %s" if limit else ""),
                ((limit,) if limit else ()))
            return [{"id": r[0], "url": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def _write_apply_urls(rows: list) -> int:
    if not rows:
        return 0
    import pymysql
    conn = pymysql.connect(
        host=settings.db_host, port=settings.db_port, user=settings.db_user,
        password=settings.db_password, database=settings.db_name,
        charset="utf8mb4", autocommit=True)
    n = 0
    try:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute("UPDATE jobs SET apply_url=%s, updated_at=UTC_TIMESTAMP(3) "
                            "WHERE id=%s AND site='himalayas'", (r["apply_url"], r["id"]))
                n += cur.rowcount
    finally:
        conn.close()
    return n


async def _settle(page, tries: int = 6) -> None:
    """Wait for the REAL job page to render. Can't just count <h1>s — Cloudflare's
    interstitial has one too ("himalayas.app")."""
    for _ in range(tries):
        await asyncio.sleep(1.5)
        try:
            texts = await page.eval_on_selector_all(
                "h1", "els => els.map(e => (e.textContent || '').trim())")
        except Exception:
            continue
        if any(t and t.lower() not in ("himalayas.app", "himalayas") for t in texts):
            return


async def _follow_apply_redirect(ctx, apply_url: str) -> Optional[str]:
    """`himalayas.app/apply/<code>` 302s to the employer's ATS — follow it in a
    throwaway tab and keep the destination."""
    tab = await ctx.new_page()
    try:
        await tab.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
        for _ in range(8):  # the hop takes a moment to settle
            await asyncio.sleep(1.5)
            if _off_himalayas(tab.url):
                break
        return tab.url if _is_employer_url(tab.url) else None
    except Exception:
        return None
    finally:
        try:
            await tab.close()
        except Exception:
            pass


async def _apply_via_modal(page, ctx) -> Optional[str]:
    """Open the "Apply now" modal and follow its "I'm ready to apply" link."""
    async def _href() -> str:
        return await page.evaluate(
            """() => {
                const inModal = Array.from(document.querySelectorAll(
                    '[role=dialog] a, [class*=modal i] a, [class*=Modal] a'));
                const any = inModal.length ? inModal : Array.from(document.querySelectorAll('a'));
                const hit = any.find(e => /ready to apply/i.test(e.textContent || '')
                                       || (e.getAttribute('href') || '').indexOf('/apply/') >= 0);
                return hit ? hit.href : '';
            }""")

    # React page: a button that exists isn't necessarily wired yet, so a too-early
    # click silently does nothing. Retry across the visible buttons.
    for attempt in range(3):
        if await _href():
            break
        try:
            buttons = await page.query_selector_all("button:has-text('Apply')")
        except Exception:
            buttons = []
        clicked = False
        for b in buttons:
            try:
                if not await b.is_visible():
                    continue
                await b.click(timeout=6000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked and attempt == 2:
            return None
        for _ in range(5):  # wait for the modal to mount
            await asyncio.sleep(1.0)
            if await _href():
                break
        if await _href():
            break
        await asyncio.sleep(1.5)  # let hydration finish, then retry

    href = await _href()
    return await _follow_apply_redirect(ctx, href) if href else None


async def _extract_apply(page, ctx) -> Optional[str]:
    # 1) an "Apply"-labelled anchor already pointing off-site
    try:
        anchors = await page.eval_on_selector_all(
            "a", "els => els.map(e => ({t:(e.textContent||'').trim(), h:e.href}))")
    except Exception:
        anchors = []
    for a in anchors:
        if re.search(r"\bapply\b", a["t"], re.I) and _is_employer_url(a["h"]):
            return a["h"]
    # 2) the signed-in modal — the path that actually works
    return await _apply_via_modal(page, ctx)


async def _is_login_gated(page) -> bool:
    """Logged out, every "Apply now" is just /signup/talent."""
    try:
        hrefs = await page.eval_on_selector_all(
            "a", "els => els.filter(e => /apply/i.test(e.textContent || ''))"
                 "        .map(e => e.getAttribute('href') || '')")
    except Exception:
        return False
    return bool(hrefs) and all(re.search(r"/(signup|login|signin)\b", h) for h in hrefs)


async def resolve_pending(limit: int = 0) -> int:
    """Resolve employer apply URLs for every unresolved Himalayas row.

    Runs automatically after scrape() when settings.himalayas_resolve_apply is on.
    Needs a signed-in session: ensure_session() reuses the one in the DB and only
    opens a login browser when there isn't one.
    """
    from scraper.auth.site_login import (ensure_session, kill_chrome_tree,
                                         launch_chrome, session_file)
    from scraper.session import SessionStore

    jobs = _pending_rows(limit)
    if not jobs:
        log.info("[himalayas] no rows need apply-URL resolution")
        return 0
    await ensure_session("himalayas")  # sign in only if the DB has no session
    log.info("[himalayas] resolving employer apply URLs for {} job(s)...", len(jobs))

    from patchright.async_api import async_playwright
    profile = str(Path(settings.user_data_dir).parent / "himalayas-chrome")
    resolved: list = []
    proc = relay = pw = None
    try:
        server = None
        if proxy_for("himalayas"):
            # Chrome can't send proxy credentials, so front the authenticated
            # upstream with the project's local relay.
            from scraper.local_proxy import LocalRoutingProxy
            direct = settings.proxy_bypass.split(",") if settings.proxy_bypass else []
            relay = LocalRoutingProxy(proxy_for("himalayas"), direct)
            server = "http://127.0.0.1:%d" % await relay.start()
        proc, endpoint = launch_chrome(profile, 9222, server)
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(endpoint)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(60000)
        try:  # cookies come from the DB, never a local file
            await SessionStore.load(ctx, None, session_file("himalayas"))
        except Exception:
            pass

        cf_fails = 0
        for i, job in enumerate(jobs, 1):
            try:
                await page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
                if not await clear_challenge(page, max_wait_s=100):
                    cf_fails += 1
                    log.warning("[himalayas] {}/{} id={} Cloudflare not cleared",
                                i, len(jobs), job["id"])
                    # Each failure hardens the block for this IP — stop rather than
                    # grind a soft block into a hard one.
                    if cf_fails >= 3:
                        log.warning("[himalayas] 3 Cloudflare failures — stopping early")
                        break
                    continue
                cf_fails = 0
                await _settle(page)
                url = await _extract_apply(page, ctx)
                if url:
                    resolved.append({"id": job["id"], "apply_url": url})
                    log.info("[himalayas] {}/{} id={} -> {}", i, len(jobs), job["id"], url[:70])
                elif await _is_login_gated(page):
                    log.info("[himalayas] {}/{} id={} login-gated (session expired?)",
                             i, len(jobs), job["id"])
                else:
                    log.info("[himalayas] {}/{} id={} no employer URL", i, len(jobs), job["id"])
            except Exception as e:
                log.warning("[himalayas] {}/{} id={} error: {}",
                            i, len(jobs), job["id"], str(e)[:70])
                if any(k in str(e) for k in ("Connection closed", "Target closed", "Browser closed")):
                    log.warning("[himalayas] browser died — stopping (re-run to continue)")
                    break
            if i < len(jobs):
                await asyncio.sleep(random.uniform(1.0, 2.5))

        try:  # push refreshed cookies back so the next run starts signed in
            await SessionStore.save(ctx, page, session_file("himalayas"),
                                    domains=("himalayas.app",))
        except Exception:
            pass
    except Exception as e:
        log.warning("[himalayas] apply-URL pass failed: {}", e)
    finally:
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
        kill_chrome_tree(proc)  # tree-kill: terminate() orphans renderers
        if relay:
            try:
                await relay.stop()
            except Exception:
                pass

    n = _write_apply_urls(resolved)
    log.info("[himalayas] apply URLs resolved {}/{} (updated {} rows)",
             len(resolved), len(jobs), n)
    return n
