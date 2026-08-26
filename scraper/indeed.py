"""Indeed scraper.

Scope for now: load the public remote-jobs results page, clear Cloudflare,
scroll like a human, and extract the fields we agreed on — site job id, title,
description, link, location. Sign-in is deliberately NOT wired yet (waiting on
the flow you'll provide); this runs against the public listing.

Extraction reads the results DOM (the `data-jk` job key + testid'd fields).
Optionally opens each posting for the full description, throttled with
human-like delays because Indeed is aggressive about scraping.
"""
from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from config import settings, site_uses_proxy
from logger import log
from scraper import human
from scraper.auth.google_auth import GoogleAuthService
from scraper.base_scraper import BaseScraper, ScrapedJob
from scraper.browser import StealthBrowser, is_challenged
from scraper.dates import compute_posted_at, is_fine_grained
from scraper.local_proxy import remembered_challenge_proxy, rotate_challenge_proxy
from scraper.session import SessionStore

GOOGLE_BUTTON = 'iframe[src*="accounts.google.com/gsi/button"]'
INDEED_DOMAINS = ("indeed.com",)

#: How many consecutive organic postings older than `max_age_days` end a
#: date-sorted walk. >1 so a single out-of-order card can't cut the run short.
_STALE_RUN_TO_STOP = 3

#: Pagination safety net. The walk normally ends on its own — no new unique
#: cards, a stale-date streak, max_jobs, or a checkpoint — so this only bounds
#: a pathological run. A code constant rather than an admin setting, matching
#: LinkedIn's _MAX_PAGES.
_MAX_PAGES = 10

# Runs on a viewjob page: pull company, company link, job type, location, the
# external apply link ("Apply on company site"), and the full description.
_DETAIL_JS = r"""
() => {
  const out = { description: '', apply_url: '', company: '', company_url: '' };
  const desc = document.querySelector('#jobDescriptionText');
  if (desc) out.description = (desc.innerText || '').trim();

  const companyA = document.querySelector('[data-testid="inlineHeader-companyName"] a, [data-company-name="true"] a');
  if (companyA) {
    out.company = (companyA.innerText || '').trim();
    out.company_url = (companyA.href || '').split('?')[0];
  } else {
    const c = document.querySelector('[data-testid="inlineHeader-companyName"], [data-company-name="true"]');
    if (c) out.company = (c.innerText || '').trim();
  }

  // Only look inside the apply-buttons area, so we never grab an unrelated
  // Indeed link. "Apply with Indeed" jobs have a <button> (no <a>) here → null;
  // "Apply on company site" jobs have an <a> with the external redirect.
  const btnScope = document.querySelector('#applyButtonLinkContainer, [data-testid="apply-button-container"], #jobsearch-ViewJobButtons-container');
  if (btnScope) {
    // Only accept a directly-external link here. Indeed redirect stubs
    // (rc/clk, applystart, viewjob) are followed by clicking (see Python).
    const applyA = Array.from(btnScope.querySelectorAll('a[href]')).find(
      (a) => a.href && !/indeed\.com/.test(a.href),
    );
    if (applyA) out.apply_url = applyA.href;
  }
  return out;
}
"""

# Runs in the page: pull one record per result card from the results DOM.
_EXTRACT_JS = r"""
() => {
  const out = [];
  // Indeed's embedded job data has the true posted time per job (works even
  // when logged-in cards show a "Visited …" label instead).
  const rel = {};
  const addRecords = (results) => {
    (results || []).forEach((r) => {
      const jk = r.jobkey || r.jobKey;
      if (!jk) return;
      const wm = r.remoteWorkModel || {};
      const fl = (r.formattedLocation || '').trim();
      rel[jk] = {
        rt: (r.formattedRelativeTime || '').trim(),
        pub: r.pubDate || null,
        // "Remote" is a work model, not a place → store location empty for it.
        location: (fl && fl.toLowerCase() !== 'remote') ? fl : '',
        remote: wm.type === 'REMOTE_ALWAYS' || !!r.remoteLocation,
        jobType: (r.jobTypes && r.jobTypes.length) ? r.jobTypes.join(', ') : '',
        company: (r.company || '').trim(),
        salary: (function () {
          const s = r.salarySnippet || r.estimatedSalary || r.extractedSalary || {};
          return (s.text || s.salaryText || s.formattedRange || '').trim();
        })(),
        // Promoted cards are injected ahead of the organic list and ignore the
        // sort order, so a stale one must never end a date-sorted walk.
        sponsored: !!(r.adId || (r.link || '').indexOf('/pagead/clk') === 0),
      };
    });
  };

  // Preferred source: the live model, when the page's globals are reachable.
  try {
    const pd = window.mosaic && window.mosaic.providerData && window.mosaic.providerData['mosaic-provider-jobcards'];
    const results = pd && pd.metaData && pd.metaData.mosaicProviderJobCardsModel && pd.metaData.mosaicProviderJobCardsModel.results;
    if (results && results.length) addRecords(results);
  } catch (e) {}

  // Fallback, and in practice the one that runs: patchright evaluates in an
  // ISOLATED world, which shares the DOM but NOT the page's JS globals — so
  // `window.mosaic` above reads as undefined and every posted date, location
  // and job type came back empty. The same model is serialised into the
  // #mosaic-data script tag, and reading a tag's text is plain DOM access, so
  // scan it out of there: seek the jobcards model, then balance-match the
  // "results" array (a plain JSON.parse of the tag would choke — it is a
  // script assigning several globals, not a JSON document).
  if (!Object.keys(rel).length) {
    try {
      const el = document.getElementById('mosaic-data');
      const t = el ? (el.textContent || '') : '';
      const anchor = t.indexOf('mosaicProviderJobCardsModel');
      const rs = anchor >= 0 ? t.indexOf('"results":[', anchor) : -1;
      const start = rs >= 0 ? t.indexOf('[', rs) : -1;
      if (start >= 0) {
        let depth = 0, end = -1, inStr = false, esc = false;
        for (let j = start; j < t.length; j++) {
          const c = t[j];
          if (inStr) {
            if (esc) esc = false;
            else if (c.charCodeAt(0) === 92) esc = true;  // backslash
            else if (c === '"') inStr = false;
            continue;
          }
          if (c === '"') inStr = true;
          else if (c === '[' || c === '{') depth++;
          else if (c === ']' || c === '}') { depth--; if (!depth) { end = j; break; } }
        }
        if (end > start) addRecords(JSON.parse(t.slice(start, end + 1)));
      }
    } catch (e) {}
  }
  const cards = document.querySelectorAll('div.job_seen_beacon, td.resultContent, div.cardOutline');
  cards.forEach((card) => {
    const a = card.querySelector('a[data-jk]') || card.querySelector('[data-jk]');
    const jk = a ? a.getAttribute('data-jk') : null;
    if (!jk) return;
    // Skip placeholder/template cards: when we have the embedded job list, keep
    // only jk's that appear in it (drops fake ids like 123456789abcdef0).
    if (Object.keys(rel).length && !rel[jk]) return;
    const titleEl =
      card.querySelector('h2.jobTitle span[title]') ||
      card.querySelector('h2.jobTitle span') ||
      card.querySelector('h2.jobTitle') ||
      card.querySelector('[id^="jobTitle"]');
    const companyEl = card.querySelector('[data-testid="company-name"]');
    const locEl = card.querySelector('[data-testid="text-location"]');
    const snipEl =
      card.querySelector('[data-testid="jobsnippet_footer"]') ||
      card.querySelector('.job-snippet') ||
      card.querySelector('[class*="snippet" i]');
    const salEl =
      card.querySelector('[data-testid="salary-snippet-container"]') ||
      card.querySelector('.salary-snippet-container') ||
      card.querySelector('.estimated-salary') ||
      card.querySelector('[class*="salary" i]');
    // Posted-date text: prefer explicit date nodes, else any descendant text
    // that reads like a date ("posted/active … ago", "just posted").
    // Prefer the embedded posted-time; fall back to date-like card text
    // (excluding the logged-in "Visited …" personalization label).
    const info = rel[jk] || {};
    let posted = info.rt || '';
    if (!posted) {
      const m = (card.innerText || '').match(/(just posted|today|(?:posted|employer active|active)\b[^\n]*?\bago)/i);
      if (m && !/visited/i.test(m[0])) posted = m[0].trim();
    }
    // `jobTypes` is routinely empty in the model even when the card shows the
    // type, so read it off the card's attribute chips ("Full-time", "Contract"
    // — the chips also carry unrelated things like shift patterns, hence the
    // whitelist, and the salary chip shares the same testid).
    let jobType = info.jobType || '';
    if (!jobType) {
      const chips = Array.from(card.querySelectorAll('[data-testid*="attribute_snippet" i]'))
        .filter((e) => !/salary/i.test(e.getAttribute('data-testid') || ''))
        .map((e) => (e.innerText || '').trim())
        .filter((s) => /^(full.?time|part.?time|contract|temporary|internship|permanent|per diem|apprenticeship|seasonal|freelance|volunteer)$/i.test(s));
      jobType = Array.from(new Set(chips)).join(', ');
    }
    // A card with no location chip and no model location is remote-only.
    const location = info.location || (
      locEl && !/^remote$/i.test((locEl.innerText || '').trim()) ? locEl.innerText.trim() : ''
    );
    out.push({
      jk,
      title: titleEl ? titleEl.innerText.trim() : '',
      company: info.company || (companyEl ? companyEl.innerText.trim() : ''),
      location: location,
      remote: info.remote !== undefined
        ? !!info.remote
        : /remote/i.test((locEl && locEl.innerText) || ''),
      jobType: jobType,
      salary: info.salary || (salEl ? salEl.innerText.trim() : ''),
      snippet: snipEl ? snipEl.innerText.trim() : '',
      posted: posted.slice(0, 64),
      pubDate: info.pub || null,
      sponsored: !!info.sponsored,
    });
  });
  return out;
}
"""


def _posted_at(posted_text, pub_ms):
    """Best posting timestamp for an Indeed card.

    `pubDate` looks authoritative but is midnight US-Eastern of the posting DAY
    (every stored row landed on exactly 05:00 UTC), so it is COARSER than the
    card's own "5 hours ago" text. Prefer the text when it pins an hour.

    The guard matters: the same card slot also yields "employer active 2 hours
    ago" (see _EXTRACT_JS), which is when the employer last signed in, not when
    the job was posted. Anything mentioning "active" falls through to `pubDate`.
    """
    t = (posted_text or "").lower()
    if "active" not in t and is_fine_grained(t):
        ts = compute_posted_at(t, None)
        if ts is not None:
            return ts
    return compute_posted_at(posted_text, pub_ms)


class IndeedScraper(BaseScraper):
    site = "indeed"
    #: How many times a challenge-blocked exit IP is retired for a fresh one.
    #: Each rotation costs a whole pass, so keep it small.
    _MAX_PROXY_ROTATIONS = 2

    def __init__(self):
        # Indeed's results sit behind a Cloudflare Turnstile, and roughly half of
        # IPRoyal's residential exits 504 on CONNECT to its verification shard.
        # On such an exit the widget can never validate — the clicks land, the
        # checkbox stays empty, and every page reads as "0 results". Probe for an
        # exit that reaches the shard before we launch, and remember it so the IP
        # (and the profile's cf_clearance) stays stable between runs.
        base = (settings.indeed_proxy_url or settings.proxy_url) if site_uses_proxy("indeed") else ""
        if base:
            self.proxy_url = remembered_challenge_proxy(
                base, settings.indeed_proxy_session_file, prefix="in"
            )
        super().__init__()
        self.google = GoogleAuthService()
        #: Set by scrape() when a pass ends sitting behind the checkpoint, which
        #: is the signal run() uses to retire the exit IP.
        self.challenge_blocked = False

    async def run(self) -> None:
        """BaseScraper.run() plus exit-IP rotation.

        The pre-flight in __init__ only proves the remembered exit can still
        REACH Cloudflare's verification shard, not that Cloudflare still trusts
        it. An exit that has been scored as a bot therefore stays pinned run
        after run, and every pass reads as "0 results" while the pre-flight logs
        [OK] — Indeed lost hours that way. So when a pass ends blocked on the
        challenge rather than on the network, retire that exit and take the pass
        again on a fresh one.
        """
        for attempt in range(self._MAX_PROXY_ROTATIONS + 1):
            self.challenge_blocked = False
            await super().run()
            if not self.challenge_blocked:
                return
            base = (settings.indeed_proxy_url or settings.proxy_url) if site_uses_proxy("indeed") else ""
            if not base or attempt == self._MAX_PROXY_ROTATIONS:
                log.error("[indeed] still blocked on the Cloudflare challenge after {} "
                          "exit-IP rotation(s) — giving up this cycle.", attempt)
                return
            log.warning("[indeed] blocked on the Cloudflare challenge — retiring this exit IP "
                        "and retrying ({}/{}).", attempt + 1, self._MAX_PROXY_ROTATIONS)
            self.proxy_url = rotate_challenge_proxy(
                base, settings.indeed_proxy_session_file, prefix="in"
            )
            # A fresh context: the old one holds a cf_clearance bound to the
            # retired IP, which would just re-trigger the challenge.
            self.browser = StealthBrowser(user_data_dir=self.user_data_dir,
                                          proxy_url=self.proxy_url)

    def _job_url(self, jk: str) -> str:
        return f"https://www.indeed.com/viewjob?jk={jk}"

    async def is_logged_in(self) -> bool:
        """On indeed.com, logged-out shows a 'Sign in' gnav link; logged-in
        shows the account menu instead."""
        page = self.browser.page
        try:
            # A checkpoint page carries neither control; without this it matched
            # the account branch below and reported a bogus "Already logged in".
            challenged, _, _ = await is_challenged(page)
            if challenged:
                return False
            signin = await page.query_selector('a[data-gnav-element-name="SignIn"]')
            if signin and await signin.is_visible():
                return False
            account = await page.query_selector(
                '[data-gnav-element-name="AccountMenu"], [data-testid="gnav-AccountMenu"], a[href*="myjobs"], a[href*="/account"]'
            )
            return account is not None
        except Exception:
            return False

    async def ensure_logged_in(self) -> bool:
        """Sign in to Indeed via 'Continue with Google', reusing saved sessions."""
        # Seed cookies from any saved snapshots (the persistent profile usually
        # already has them, but this makes cold starts work too).
        await self.google.load(self.browser.context)
        await SessionStore.load(self.browser.context, None, settings.indeed_session_file)

        await self.browser.goto("https://www.indeed.com/")
        if await self.is_logged_in():
            log.success("Already logged in to Indeed.")
            return True

        log.info("Not logged in — signing in to Indeed with Google...")
        await self.browser.goto("https://secure.indeed.com/auth")
        await human.think(settings.min_action_delay, settings.max_action_delay)
        await self._click_google_and_auth()

        await human.think(3, 6)
        await self.browser.goto("https://www.indeed.com/")
        if await self.is_logged_in():
            await SessionStore.save(
                self.browser.context, self.browser.page, settings.indeed_session_file, domains=INDEED_DOMAINS
            )
            await self.google.save(self.browser.context, self.browser.page)
            log.success("Logged in to Indeed via Google.")
            return True

        await self.browser.screenshot("screenshots/indeed_login_result.png")
        log.error("Indeed login did not complete (see screenshots/indeed_login_result.png).")
        return False

    async def _click_google_and_auth(self) -> None:
        """Indeed's 'Continue with Google' uses Google Identity Services, which
        Chrome renders as a native FedCM account chooser (not a page iframe). We
        drive it via the CDP FedCm domain: enable it, click the button to raise
        the dialog, then select our account programmatically."""
        page = self.browser.page
        context = self.browser.context
        email = (settings.google_email or "").lower()

        cdp = await context.new_cdp_session(page)
        await cdp.send("FedCm.enable", {"disableRejectionDelay": True})

        loop = asyncio.get_event_loop()
        dialog_future: asyncio.Future = loop.create_future()

        def on_dialog(evt):
            if not dialog_future.done():
                dialog_future.set_result(evt)

        cdp.on("FedCm.dialogShown", on_dialog)

        # Click the GSI button (inside its iframe) to raise the FedCM dialog.
        try:
            btn = page.frame_locator(GOOGLE_BUTTON).locator('div[role="button"], [role="button"], button').first
            await btn.click(timeout=8000)
            log.info("Clicked 'Continue with Google'; waiting for FedCM dialog...")
        except Exception as e:
            log.warning("GSI button click failed: {}", e)

        try:
            evt = await asyncio.wait_for(dialog_future, timeout=25)
        except asyncio.TimeoutError:
            log.warning("FedCM dialog did not appear.")
            return

        dialog_id = evt.get("dialogId")
        accounts = evt.get("accounts", []) or []
        log.info("FedCM dialog shown with {} account(s)", len(accounts))
        idx = 0
        for i, a in enumerate(accounts):
            if (a.get("email") or "").lower() == email:
                idx = i
                break
        try:
            await cdp.send("FedCm.selectAccount", {"dialogId": dialog_id, "accountIndex": idx})
            log.info("Selected FedCM account index {}", idx)
        except Exception as e:
            log.warning("FedCm.selectAccount failed: {}", e)
        await human.think(5, 8)

    async def scrape(self) -> list[ScrapedJob]:
        # Sign in first — logged-in Indeed gives more consistent results and
        # fewer challenges. Also warms the persistent profile's cf_clearance.
        # Falls back to anonymous scraping if login fails.
        try:
            await self.ensure_logged_in()
        except Exception as exc:
            log.warning("Login step failed, continuing anonymously: {}", exc)

        # Skip jobs already in the DB — don't re-fetch their detail pages.
        existing = self.repo.existing_keys(self.site)
        seen: set[str] = set()  # dedupe jk across ALL pages
        jobs: list[ScrapedJob] = []
        skipped = 0

        # Walk the results pages (Indeed paginates via &start=N), stopping early
        # once max_jobs is reached, a page yields no new postings, or (for
        # date-sorted results) we reach postings older than max_age_days.
        date_sorted = "sort=date" in settings.indeed_search_url
        stop = False
        stale = 0  # consecutive organic postings older than max_age_days
        for page_num in range(_MAX_PAGES):
            if stop or (settings.max_jobs and len(jobs) >= settings.max_jobs):
                break
            ok = await self.browser.goto(self._page_url(settings.indeed_search_url, page_num))
            if not ok:
                await self.browser.screenshot(f"screenshots/indeed_blocked_p{page_num + 1}.png")
                log.error("Indeed page {} gated by a checkpoint we couldn't clear.", page_num + 1)
                self.challenge_blocked = True
                break

            # Behave like a human skimming the results.
            await human.human_scroll(self.browser.page, steps=random.randint(4, 8))
            await human.think(settings.min_action_delay, settings.max_action_delay)

            raw = await self.browser.page.evaluate(_EXTRACT_JS)
            if not raw:
                # No cards can mean the listing ended OR that a checkpoint is
                # still sitting on top of it. Say which, instead of silently
                # reporting "end of results" for a blocked run.
                challenged, title, _ = await is_challenged(self.browser.page)
                if challenged:
                    await self.browser.screenshot(f"screenshots/indeed_blocked_p{page_num + 1}.png")
                    log.error(
                        "Indeed page {} still behind a checkpoint (title={!r}) — stopping.",
                        page_num + 1, title,
                    )
                    self.challenge_blocked = True
                    break
            # Indeed repeats sponsored cards under the same jk — dedupe across all
            # pages so max_jobs counts UNIQUE postings.
            new_cards = []
            for item in raw:
                jk = item.get("jk")
                if jk and jk not in seen:
                    seen.add(jk)
                    new_cards.append(item)
            log.info(
                "Page {}/{}: {} cards, {} new unique",
                page_num + 1, _MAX_PAGES, len(raw), len(new_cards),
            )
            if not new_cards:
                if page_num == 0:
                    await self.browser.screenshot("screenshots/indeed_no_cards.png")
                log.info("No new cards on page {} — end of results.", page_num + 1)
                break

            for item in new_cards:
                if settings.max_jobs and len(jobs) >= settings.max_jobs:
                    break
                jk = item["jk"]
                if jk in existing:
                    skipped += 1
                    continue
                posted = _posted_at(item.get("posted"), item.get("pubDate"))
                # Only ORGANIC postings say anything about how far down the date
                # sort we've walked: promoted cards are injected at the top of
                # every page regardless of age (the first one is routinely
                # "30+ days ago"), so treating one as the end of the listing
                # would end the run on page 1. Require a RUN of stale organic
                # postings too — Indeed sprinkles the odd out-of-order card in.
                if date_sorted and not item.get("sponsored"):
                    if self._too_old(posted):
                        stale += 1
                        if stale >= _STALE_RUN_TO_STOP:
                            log.info(
                                "{} consecutive postings older than {}d — stopping (date-sorted).",
                                stale, settings.max_age_days,
                            )
                            stop = True
                            break
                    else:
                        stale = 0
                if self._too_old(posted):
                    # Would be dropped by save() anyway — don't spend a detail
                    # fetch (and its throttle) on it.
                    self.counts["too_old"] += 1
                    continue
                description = item.get("snippet", "")
                # location / remote / job_type / company come from the reliable
                # listing mosaic; the detail page only adds description,
                # company_url, apply_url (and refines the company name).
                company = item.get("company") or None
                location = item.get("location") or None
                job_type = item.get("jobType") or None
                remote = bool(item.get("remote"))
                company_url = None
                apply_url = None

                if settings.fetch_descriptions:
                    detail = await self._fetch_detail(jk)
                    description = detail.get("description") or description
                    apply_url = detail.get("apply_url")
                    company = detail.get("company") or company
                    company_url = detail.get("company_url")

                job = ScrapedJob(
                    site_job_id=jk,
                    title=item.get("title", "") or "(no title)",
                    description=description,
                    link=self._job_url(jk),
                    company=company,
                    company_url=company_url,
                    job_type=job_type,
                    remote=remote,
                    location=location,
                    salary=item.get("salary") or None,
                    posted_at=posted,
                    apply_url=apply_url,
                )
                self.save(job)  # persist this job immediately, one by one
                jobs.append(job)

            if stop:
                break

        log.info("Indeed: {} new job(s) saved, {} already stored (skipped).", len(jobs), skipped)
        return jobs

    @staticmethod
    def _search_url(base: str) -> str:
        """The configured search URL with Indeed's own recency filter applied.

        `sort=date` is not enough on its own. Indeed front-loads every page with
        promoted cards that ignore the sort, and for a narrow query its "newest"
        organic results can still be weeks old — the configured search returned
        ZERO postings inside the 7-day window, while the very same query plus
        `fromage` returned a full page of them (and almost no ads). `fromage`
        filters server-side, so ask for exactly the window `max_age_days`
        already defines. An explicit `fromage` in the URL is left alone."""
        if not settings.max_age_days:
            return base
        u = urlparse(base)
        q = parse_qs(u.query, keep_blank_values=True)
        if "fromage" in q:
            return base
        q["fromage"] = [str(settings.max_age_days)]
        return urlunparse(u._replace(query=urlencode(q, doseq=True)))

    @classmethod
    def _page_url(cls, base: str, page_num: int) -> str:
        """Indeed paginates via &start=N (10 results per page)."""
        base = cls._search_url(base)
        if page_num <= 0:
            return base
        u = urlparse(base)
        q = parse_qs(u.query, keep_blank_values=True)
        q["start"] = [str(page_num * 10)]
        return urlunparse(u._replace(query=urlencode(q, doseq=True)))

    async def _fetch_detail(self, jk: str) -> dict:
        """Open the posting; read description, company, company link, job type,
        location and the external apply URL. Heavily throttled."""
        out: dict = {}
        try:
            await human.think(settings.min_page_delay, settings.max_page_delay)
            await self.browser.goto(self._job_url(jk))
            data = await self.browser.page.evaluate(_DETAIL_JS)
            for key in ("description", "apply_url", "company", "company_url"):
                out[key] = (data.get(key) or "").strip() or None
            # "Apply with Indeed" jobs have no static link — click to get the
            # smartapply URL that opens in a new tab.
            if settings.capture_apply_url and not out.get("apply_url"):
                out["apply_url"] = await self._capture_apply_url()
        except Exception as exc:
            log.warning("Could not fetch detail for jk={}: {}", jk, exc)
        return out

    async def _capture_apply_url(self):
        """Click the job's Apply button and capture the FINAL apply URL — the
        smartapply page ("Apply with Indeed"), or the company ATS page after
        Indeed's redirect stub ("Apply on company site"). Doesn't submit."""
        page = self.browser.page
        ctx = self.browser.context
        popup: dict = {}

        def _on_page(p):
            popup["p"] = p

        def _is_final(u: str) -> bool:
            # Must be a real web page. A popup starts life on about:blank or
            # chrome://new-tab-page/, and "not an indeed.com URL" alone counted
            # those as the answer — one job was stored with an apply URL of
            # chrome://new-tab-page/.
            if not u or not u.startswith(("http://", "https://")):
                return False
            if "smartapply.indeed.com" in u:
                return True
            # An external company/ATS page (past Indeed's rc/clk redirect stub).
            return "indeed.com" not in u

        # Locate the apply control: Indeed Apply button, or a company-site link/button.
        # "Apply on company site" used to be an <a href> and is now a bare
        # <button> — no id, no testid, no aria-label, no href — so the id/href
        # selectors below all miss it and EVERY job was being dropped for having
        # no apply URL. Match it by text inside the apply containers too, most
        # specific first so we can't grab a neighbouring control like "Save".
        btn = None
        for sel in (
            "#indeedApplyButton",
            "[data-testid='indeedApplyButton-test']",
            "#applyButtonLinkContainer a",
            "[data-testid='apply-button-container'] a",
            "#jobsearch-ViewJobButtons-container a[aria-label*='Apply' i]",
            "#jobsearch-ViewJobButtons-container button[aria-label*='Apply' i]",
            "#applyButtonLinkContainer button:has-text('Apply')",
            "[data-testid='apply-button-container'] button:has-text('Apply')",
            "#jobsearch-ViewJobButtons-container button:has-text('Apply')",
            "button:has-text('Apply on company site')",
        ):
            btn = await page.query_selector(sel)
            if btn:
                break
        if not btn:
            log.warning("No apply control found on {}", (page.url or "")[:90])
            return None

        try:
            try:
                await page.wait_for_selector(
                    '[data-testid="indeed-apply-widget"][data-click-handler="attached"]', timeout=4000
                )
            except Exception:
                pass
            await human.think(0.5, 1.5)

            ctx.on("page", _on_page)
            try:
                await btn.click(timeout=8000)
            except Exception:
                try:
                    await btn.click(force=True, timeout=8000)
                except Exception:
                    pass

            url = None
            # "Apply on company site" lands on an Indeed rc/clk stub first and
            # only then bounces to the employer's ATS, so give the redirect
            # chain room to finish before calling it a miss.
            for _ in range(20):
                await asyncio.sleep(1)
                p = popup.get("p")
                if p and _is_final(p.url or ""):
                    url = p.url
                    break
                if _is_final(page.url or ""):
                    url = page.url
                    break
            return url
        except Exception as exc:
            log.warning("Could not capture apply URL: {}", exc)
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
    await IndeedScraper().run()


if __name__ == "__main__":
    asyncio.run(main())
