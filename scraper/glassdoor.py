"""Glassdoor scraper (experimental).

Writes to `jobs_temp` (a clone of `jobs`) so it never touches the live table
while we validate it. Structure mirrors the Indeed scraper: subclass
`BaseScraper`, clear Cloudflare, behave like a human, extract the listing, then
open each posting for the full description.

Glassdoor differs from Indeed in two ways that shape this code:
  1. It throws a "sign up" / auth modal over the results a few seconds in — we
     dismiss it (Escape + close button) rather than logging in.
  2. Results use a two-pane layout: the left list + a right detail pane that
     updates in place when you click a card (no navigation). We read the
     description from that right pane instead of visiting a separate URL, which
     keeps us on the already-cleared search page.
"""
from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timedelta, timezone

from pathlib import Path

from config import settings
from logger import log
from scraper import human
from scraper.auth.google_auth import GoogleAuthService
from scraper.base_scraper import BaseScraper, ScrapedJob
from scraper.local_proxy import pick_challenge_capable_proxy, session_of
from scraper.session import SessionStore

GLASSDOOR_DOMAINS = ("glassdoor.com",)
GLASSDOOR_LOGIN_URL = "https://www.glassdoor.com/profile/login_input.htm"
# Google Identity Services button (rendered inside a GSI iframe).
GOOGLE_BUTTON = 'iframe[src*="accounts.google.com/gsi/button"]'

# Buttons/links that close the interstitial "sign up" modal Glassdoor overlays
# on the results. We try each; Escape usually works too.
_MODAL_CLOSE_SELECTORS = (
    'button.CloseButton',
    '[data-test="job-alert-modal-close"]',
    'span.SVGInline.modal_closeIcon',
    'button[aria-label="Close"]',
    '[alt="Close"]',
    '.modal_closeIcon',
)

# Runs in the page: one record per result card, read from the results DOM.
# Glassdoor hashes its class names, so we anchor on stable data-test attrs and
# fall back to class-name prefixes.
_EXTRACT_JS = r"""
() => {
  const out = [];
  const pickText = (root, sels) => {
    for (const s of sels) { const el = root.querySelector(s); if (el && el.innerText.trim()) return el.innerText.trim(); }
    return '';
  };
  const cards = document.querySelectorAll(
    'li[data-test="jobListing"], [data-test="jobListing"], li.JobsList_jobListItem__wjTHv'
  );
  cards.forEach((card) => {
    const titleA =
      card.querySelector('a[data-test="job-title"]') ||
      card.querySelector('a[data-test="job-link"]') ||
      card.querySelector('a[id^="job-title"]');
    if (!titleA) return;

    // Job id: prefer an explicit data-jobid anywhere on the card, else the
    // ?jl= listing id in the href, else the JV_ token in the path.
    let jobId = '';
    const idHolder = card.querySelector('[data-jobid]') || (card.getAttribute('data-jobid') ? card : null);
    if (idHolder) jobId = (idHolder.getAttribute('data-jobid') || '').trim();
    const href = titleA.href || '';
    if (!jobId) { const m = href.match(/[?&]jl=(\d+)/); if (m) jobId = m[1]; }
    if (!jobId) { const m = href.match(/_JV[_]?[^?]*?(\d{6,})/); if (m) jobId = m[1]; }
    if (!jobId) return;

    const company = pickText(card, [
      '[data-test="emp-name"]',
      '.EmployerProfile_compactEmployerName__9MGcV',
      '[class*="EmployerProfile_compactEmployerName"]',
      '[class*="employerName"]',
    ]);
    const location = pickText(card, [
      '[data-test="emp-location"]',
      '[class*="JobCard_location"]',
      '[data-test="location"]',
    ]);
    let age = pickText(card, [
      '[data-test="job-age"]',
      '[class*="JobCard_listingAge"]',
      '[class*="listingAge"]',
    ]);
    if (!age) {
      // Fallback: scan the card text for a Glassdoor age token (e.g. 24h, 30d+).
      const m = (card.innerText || '').match(/\b(\d+\s*d\+?|\d+\s*h\+?|today|just posted)\b/i);
      if (m) age = m[0];
    }
    const salary = pickText(card, [
      '[data-test="detailSalary"]',
      '[class*="JobCard_salaryEstimate"]',
    ]);
    const snippet = pickText(card, [
      '[data-test="descSnippet"]',
      '[class*="JobCard_jobDescriptionSnippet"]',
    ]);

    out.push({
      jobId,
      title: (titleA.innerText || '').trim(),
      url: href.split('?')[0] || href,   // clean URL for the DB job_url
      detailUrl: href,                    // full URL (with ?jl=) for navigation
      company,
      location,
      age,
      salary,
      snippet,
    });
  });
  return out;
}
"""

# Runs on a logged-in job page (JobDetails_* two-pane / standalone): read the
# full description, company, apply link, and posted date.
_DETAIL_JS = r"""
() => {
  const out = { description: '', apply_url: '', company: '', company_url: '', posted: '' };

  // Description: prefer a description-specific node; else take the JobDetails
  // container and slice from the "Job Description" heading onward.
  let desc = '';
  const dsel = [
    '[class*="JobDetails_jobDescription" i]',
    '[data-test="jobDescriptionText"]',
    '#JobDescriptionContainer',
    '[class*="JobDescription" i]',
  ];
  for (const s of dsel) {
    const el = document.querySelector(s);
    if (el) { const t = (el.innerText || '').trim(); if (t.length > desc.length) desc = t; }
  }
  if (desc.length < 200) {
    const cont = document.querySelector('[class*="JobDetails_jobDetailsContainer" i]');
    if (cont) {
      let t = cont.innerText || '';
      const i = t.search(/job description/i);
      if (i >= 0) t = t.slice(i);
      if (t.trim().length > desc.length) desc = t.trim();
    }
  }
  out.description = desc;

  const empA =
    document.querySelector('[data-test="employer-short-name"]') ||
    document.querySelector('[class*="EmployerProfile_employerName"] a') ||
    document.querySelector('[data-test="employerName"] a') ||
    document.querySelector('[class*="JobDetails_employerName" i]');
  if (empA) {
    out.company = (empA.innerText || '').trim();
    if (empA.href) out.company_url = (empA.href || '').split('?')[0];
  }

  // "Apply on employer site" carries the real/partner URL when it's an <a>;
  // "Easy Apply" is a button (no external URL) → leave apply_url empty.
  const applyA = document.querySelector('a[data-test="applyButton"], a[href][data-test*="apply" i]');
  if (applyA && applyA.href && !/glassdoor\.com/.test(applyA.href)) out.apply_url = applyA.href;

  // Posted date, if the page shows one.
  const pm = (document.body.innerText || '').match(/(\d+\s*days?\s*ago|\d+\s*hours?\s*ago|\d+[dh]\+?\b|today|just posted)/i);
  if (pm) out.posted = pm[0];
  return out;
}
"""


def compute_posted_at(age_text):
    """Full posting timestamp (naive UTC) from Glassdoor's short age label
    ("24h", "3d", "30d+", "Today")."""
    t = (age_text or "").strip().lower()
    if not t:
        return None
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    if "today" in t or "just" in t or t in ("0d", "1h"):
        return now
    m = re.search(r"(\d+)\s*\+?\s*h", t)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    m = re.search(r"(\d+)\s*\+?\s*d", t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\s*\+?\s*m", t)  # "30m+" minutes (rare) / months — treat as minutes
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    return None


class GlassdoorScraper(BaseScraper):
    site = "glassdoor"
    table = "jobs_temp"  # experimental — keep off the live `jobs` table
    user_data_dir = settings.glassdoor_user_data_dir  # isolated Chrome profile

    def __init__(self):
        # Glassdoor's fresh profile must solve a Cloudflare Turnstile, whose
        # backend shard 504s on many residential exit IPs. Pre-flight probe for
        # an exit that can reach it (its own session, isolated from Indeed), and
        # remember it so the IP — and thus cf_clearance — stays stable.
        base = settings.glassdoor_proxy_url or settings.proxy_url
        if base:
            self.proxy_url = pick_challenge_capable_proxy(
                base, preferred_session=self._load_proxy_session()
            )
            self._save_proxy_session(session_of(self.proxy_url))
        super().__init__()
        self.google = GoogleAuthService()

    @staticmethod
    def _load_proxy_session():
        try:
            return Path(settings.glassdoor_proxy_session_file).read_text(encoding="utf-8").strip() or None
        except Exception:
            return None

    @staticmethod
    def _save_proxy_session(session) -> None:
        if not session:
            return
        try:
            p = Path(settings.glassdoor_proxy_session_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(session, encoding="utf-8")
        except Exception:
            pass

    async def is_logged_in(self) -> bool:
        """When logged in, glassdoor.com redirects to the member dashboard
        (/home or /member) and the header shows the account avatar instead of a
        'Sign In' control. The redirect is client-side, so poll briefly for it."""
        page = self.browser.page
        try:
            for _ in range(6):
                u = page.url or ""
                if "/member" in u or "/home" in u:
                    return True
                signin = await page.query_selector(
                    'a[href*="login_input"], button:has-text("Sign In"), a:has-text("Sign In")'
                )
                if signin and await signin.is_visible():
                    return False
                await asyncio.sleep(1)
            # Settled on a real Glassdoor page with no visible Sign In → logged in.
            header = await page.query_selector('header, [data-test="site-header"], nav')
            signin = await page.query_selector('button:has-text("Sign In"), a:has-text("Sign In")')
            return header is not None and not (signin and await signin.is_visible())
        except Exception:
            return False

    async def ensure_logged_in(self) -> bool:
        """Sign in to Glassdoor via 'Continue with Google', reusing saved sessions.
        Full job descriptions are gated behind login, so this is needed to get
        more than the ~200-char listing snippet."""
        await self.google.load(self.browser.context)
        await SessionStore.load(self.browser.context, None, settings.glassdoor_session_file)

        await self.browser.goto("https://www.glassdoor.com/")
        await self._dismiss_modal()
        if await self.is_logged_in():
            log.success("Already logged in to Glassdoor.")
            return True

        log.info("Not logged in — signing in to Glassdoor with Google...")
        await self.browser.goto(GLASSDOOR_LOGIN_URL)
        await human.think(settings.min_action_delay, settings.max_action_delay)
        await self._click_google_and_auth()

        await human.think(3, 6)
        await self.browser.goto("https://www.glassdoor.com/")
        await self._dismiss_modal()
        if await self.is_logged_in():
            await SessionStore.save(
                self.browser.context, self.browser.page,
                settings.glassdoor_session_file, domains=GLASSDOOR_DOMAINS,
            )
            await self.google.save(self.browser.context, self.browser.page)
            log.success("Logged in to Glassdoor via Google.")
            return True

        await self.browser.screenshot("screenshots/glassdoor_login_result.png")
        log.warning("Glassdoor login did not complete (see screenshots/glassdoor_login_result.png).")
        return False

    async def _click_google_and_auth(self) -> None:
        """Glassdoor renders a plain 'Continue with Google' button (not a GSI
        iframe / FedCM dialog). Click it and drive the resulting OAuth popup /
        inline redirect to completion (we're signed into Google via cookies, so
        it's an account-picker or silent round-trip)."""
        page = self.browser.page
        context = self.browser.context
        for sel in (
            'button:has-text("Continue with Google")',
            'div[role="button"]:has-text("Continue with Google")',
            'a:has-text("Continue with Google")',
            'button:has-text("Google")',
        ):
            try:
                el = await page.query_selector(sel)
            except Exception:
                el = None
            if el and await el.is_visible():
                log.info("Clicking Glassdoor 'Continue with Google' ({})", sel)
                await self.google.complete_site_oauth(context, page, sel)
                return
        log.warning("Could not find Glassdoor 'Continue with Google' button.")

    async def _dismiss_modal(self) -> None:
        """Close the sign-up / auth modal Glassdoor overlays on results."""
        page = self.browser.page
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        for sel in _MODAL_CLOSE_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click(timeout=2000)
                    log.info("Dismissed Glassdoor modal via {}", sel)
                    await human.think(0.5, 1.2)
                    return
            except Exception:
                continue

    async def _load_more(self, rounds: int = 4) -> None:
        """Scroll + click 'Show more jobs' to grow the list before extracting."""
        page = self.browser.page
        for _ in range(rounds):
            await human.human_scroll(page, steps=random.randint(2, 4))
            await self._dismiss_modal()
            try:
                btn = await page.query_selector('[data-test="load-more"], button[data-test="load-more"]')
                if btn and await btn.is_visible():
                    await btn.click(timeout=4000)
                    await human.think(settings.min_page_delay, settings.max_page_delay)
            except Exception:
                pass

    async def _looks_broken(self) -> bool:
        """True if the results didn't render — either Glassdoor's 'didn't load
        properly' soft-error, or an unsolved Cloudflare block ('Just a moment' /
        'Humans only' / 'Verify you are human'). Both warrant a re-navigation."""
        try:
            title = ((await self.browser.page.title()) or "").lower()
        except Exception:
            title = ""
        try:
            body = ((await self.browser.page.inner_text("body")) or "")[:4000].lower()
        except Exception:
            body = ""
        markers = (
            "didn't load properly", "did not load properly", "try your search again",
            "just a moment", "humans only", "verify you are human", "additional verification",
        )
        return any(h in title or h in body for h in markers)

    async def _retry_load(self) -> None:
        """Recover from the soft-error page: click 'Try again' if present, else reload."""
        page = self.browser.page
        try:
            btn = await page.query_selector('button:has-text("Try again"), a:has-text("Try again")')
            if btn and await btn.is_visible():
                await btn.click(timeout=4000)
                await self.browser.clear_checkpoint()
                await human.think(settings.min_page_delay, settings.max_page_delay)
                return
        except Exception:
            pass
        try:
            await page.reload(wait_until="domcontentloaded")
            await self.browser.clear_checkpoint()
            await human.think(settings.min_page_delay, settings.max_page_delay)
        except Exception:
            pass

    async def _load_search(self, attempts: int = 3) -> bool:
        """Warm up on the homepage, then load the search results, re-navigating
        past an intermittent Cloudflare block or Glassdoor's flaky soft-error
        (each fresh navigation gets a new challenge that usually clears)."""
        if settings.warmup:
            try:
                await self.browser.goto("https://www.glassdoor.com/")
                await self._dismiss_modal()
                await human.think(settings.min_action_delay, settings.max_action_delay)
            except Exception as exc:
                log.warning("Glassdoor warmup failed: {}", exc)

        for attempt in range(1, attempts + 1):
            # goto() already waits out / clicks through the checkpoint (up to 120s).
            await self.browser.goto(settings.glassdoor_search_url)
            await self._dismiss_modal()
            if not await self._looks_broken():
                if attempt > 1:
                    log.info("Glassdoor results loaded on attempt {}.", attempt)
                return True
            # A quick in-place 'Try again' click can rescue the soft-error page.
            await self._retry_load()
            if not await self._looks_broken():
                return True
            log.warning("Glassdoor results still blocked (attempt {}/{}).", attempt, attempts)
            await human.think(3, 6)
        log.error("Glassdoor results page stayed blocked after {} attempts.", attempts)
        await self.browser.screenshot("screenshots/glassdoor_blocked.png")
        return False

    async def _debug_dump(self) -> None:
        """When 0 cards are found, log the page's real structure to guide selectors."""
        try:
            info = await self.browser.page.evaluate(
                r"""() => {
                  const tests = Array.from(document.querySelectorAll('[data-test]'))
                    .map(e => e.getAttribute('data-test'));
                  return {
                    title: document.title,
                    url: location.href,
                    bodyLen: document.body ? document.body.innerText.length : 0,
                    jobListing: document.querySelectorAll('[data-test="jobListing"]').length,
                    liCount: document.querySelectorAll('li').length,
                    jobTitleLinks: document.querySelectorAll('a[data-test="job-title"]').length,
                    dataTests: Array.from(new Set(tests)).slice(0, 80),
                    bodyHead: (document.body ? document.body.innerText : '').slice(0, 300),
                  };
                }"""
            )
            log.info(
                "DEBUG page: title={!r} url={} bodyLen={} jobListing={} li={} jobTitleLinks={}",
                info.get("title"), info.get("url"), info.get("bodyLen"),
                info.get("jobListing"), info.get("liCount"), info.get("jobTitleLinks"),
            )
            log.info("DEBUG data-test values present: {}", info.get("dataTests"))
            log.info("DEBUG body head: {!r}", info.get("bodyHead"))
        except Exception as exc:
            log.warning("debug dump failed: {}", exc)

    async def scrape(self) -> list[ScrapedJob]:
        # Sign in first (full descriptions are gated behind login); fall back to
        # anonymous scraping if it fails.
        try:
            await self.ensure_logged_in()
        except Exception as exc:
            log.warning("Glassdoor login step failed, continuing anonymously: {}", exc)

        await self._load_search()
        await self._load_more()
        await human.think(settings.min_action_delay, settings.max_action_delay)

        raw = await self.browser.page.evaluate(_EXTRACT_JS)
        seen: set[str] = set()
        unique = []
        for item in raw:
            jid = item.get("jobId")
            if jid and jid not in seen:
                seen.add(jid)
                unique.append(item)
        log.info("Found {} result cards ({} unique) on the listing page", len(raw), len(unique))
        if not unique:
            await self._debug_dump()
            await self.browser.screenshot("screenshots/glassdoor_no_cards.png")

        # Snapshot Glassdoor cookies so future runs start warmer.
        try:
            await SessionStore.save(
                self.browser.context, self.browser.page,
                settings.glassdoor_session_file, domains=GLASSDOOR_DOMAINS,
            )
        except Exception:
            pass

        existing = self.repo.existing_keys(self.site)

        jobs: list[ScrapedJob] = []
        skipped = 0
        for item in unique:
            if len(jobs) >= settings.max_jobs:
                break
            jid = item["jobId"]
            if jid in existing:
                skipped += 1
                continue

            location = (item.get("location") or "").strip()
            remote = "remote" in location.lower()
            description = item.get("snippet", "")
            company = item.get("company") or None
            company_url = None
            apply_url = None
            posted_text = item.get("age") or ""

            if settings.fetch_descriptions:
                detail = await self._fetch_detail(item.get("detailUrl") or item.get("url"))
                description = detail.get("description") or description
                apply_url = detail.get("apply_url")
                company = detail.get("company") or company
                company_url = detail.get("company_url")
                posted_text = posted_text or detail.get("posted") or ""

            jobs.append(
                ScrapedJob(
                    site_job_id=jid,
                    title=item.get("title", "") or "(no title)",
                    description=description,
                    link=item.get("url") or self._job_url(jid),
                    company=company,
                    company_url=company_url,
                    job_type=None,
                    remote=remote,
                    location=location or None,
                    posted_at=compute_posted_at(posted_text),
                    apply_url=apply_url,
                )
            )
        log.info(
            "This page: {} new job(s) to fetch, {} already stored (detail skipped).",
            len(jobs),
            skipped,
        )
        return jobs

    def _job_url(self, jid: str) -> str:
        return f"https://www.glassdoor.com/job-listing/?jl={jid}"

    async def _fetch_detail(self, detail_url: str) -> dict:
        """Open the job page, expand its (collapsed) description, and read the
        full description + company + apply URL + posted date. Being logged in is
        what makes the full description visible."""
        out: dict = {}
        page = self.browser.page
        try:
            await human.think(settings.min_page_delay, settings.max_page_delay)
            await self.browser.goto(detail_url)
            await self._dismiss_modal()
            # Expand the collapsed description ("Show more") so we capture all of it.
            for sel in ('[data-test="show-more-cta"]', 'button:has-text("Show more")'):
                try:
                    more = await page.query_selector(sel)
                    if more and await more.is_visible():
                        await more.click(timeout=2500)
                        await human.think(0.5, 1.2)
                        break
                except Exception:
                    continue
            data = await page.evaluate(_DETAIL_JS)
            for key in ("description", "apply_url", "company", "company_url", "posted"):
                out[key] = (data.get(key) or "").strip() or None
            # Glassdoor's "Apply on employer site" is a button (no static href),
            # so capture the employer URL by clicking it and following the redirect.
            if settings.capture_apply_url and not out.get("apply_url"):
                out["apply_url"] = await self._capture_apply_url()
        except Exception as exc:
            log.warning("Could not fetch detail for glassdoor {}: {}", detail_url, exc)
        return out

    async def _capture_apply_url(self):
        """Click 'Apply on employer site' and capture the FINAL employer/ATS URL
        it redirects to (past Glassdoor's own redirect stub). Returns None for
        Easy-Apply-only jobs (internal Glassdoor apply — no external URL). Does
        NOT submit anything."""
        page = self.browser.page
        ctx = self.browser.context
        popup: dict = {}

        def _on_page(p):
            popup["p"] = p

        def _is_final(u: str) -> bool:
            return bool(u) and "about:blank" not in u and "glassdoor.com" not in u

        btn = await page.query_selector('[data-test="applyButton"]')
        if btn is None or not await btn.is_visible():
            return None
        try:
            label = (await btn.inner_text() or "").lower()
        except Exception:
            label = ""
        if "easy apply" in label:
            return None  # internal Glassdoor apply — no external URL

        ctx.on("page", _on_page)
        try:
            try:
                await btn.click(timeout=6000)
            except Exception:
                await btn.click(force=True, timeout=6000)
            url = None
            for _ in range(10):
                await asyncio.sleep(1)
                p = popup.get("p")
                if p:
                    try:
                        if _is_final(p.url or ""):
                            url = p.url
                            break
                    except Exception:
                        pass
                if _is_final(page.url or ""):
                    url = page.url
                    break
            if url:
                log.info("Captured apply URL -> {}", url[:120])
            return url
        except Exception as exc:
            log.warning("Could not capture Glassdoor apply URL: {}", exc)
            return None
        finally:
            try:
                ctx.remove_listener("page", _on_page)
            except Exception:
                pass
            p = popup.get("p")
            if p:
                try:
                    if not p.is_closed():
                        await p.close()
                except Exception:
                    pass


async def main() -> None:
    await GlassdoorScraper().run()


if __name__ == "__main__":
    asyncio.run(main())
