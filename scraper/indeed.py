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

from config import settings
from logger import log
from scraper import human
from scraper.auth.google_auth import GoogleAuthService
from scraper.base_scraper import BaseScraper, ScrapedJob
from scraper.session import SessionStore

GOOGLE_BUTTON = 'iframe[src*="accounts.google.com/gsi/button"]'
INDEED_DOMAINS = ("indeed.com",)

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
  try {
    const pd = window.mosaic && window.mosaic.providerData && window.mosaic.providerData['mosaic-provider-jobcards'];
    const results = pd && pd.metaData && pd.metaData.mosaicProviderJobCardsModel && pd.metaData.mosaicProviderJobCardsModel.results;
    if (results) results.forEach((r) => {
      if (!r.jobkey) return;
      const wm = r.remoteWorkModel || {};
      const fl = (r.formattedLocation || '').trim();
      rel[r.jobkey] = {
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
      };
    });
  } catch (e) {}
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
    out.push({
      jk,
      title: titleEl ? titleEl.innerText.trim() : '',
      company: info.company || (companyEl ? companyEl.innerText.trim() : ''),
      location: info.location || '',
      remote: !!info.remote,
      jobType: info.jobType || '',
      salary: info.salary || (salEl ? salEl.innerText.trim() : ''),
      snippet: snipEl ? snipEl.innerText.trim() : '',
      posted: posted.slice(0, 64),
      pubDate: info.pub || null,
    });
  });
  return out;
}
"""


def compute_posted_at(posted_text, pub_ms):
    """Full posting timestamp (naive UTC). Prefers Indeed's exact `pubDate` epoch
    (ms); otherwise derives it from the relative text ("5 hours ago") against now."""
    if pub_ms:
        try:
            return datetime.fromtimestamp(float(pub_ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            pass
    t = (posted_text or "").lower()
    if not t:
        return None
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    if "just posted" in t or "moment" in t or "today" in t:
        return now
    m = re.search(r"(\d+)\s*minute", t)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    m = re.search(r"(\d+)\s*hour", t)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    if "yesterday" in t:
        return now - timedelta(days=1)
    for unit, mult in (("day", 1), ("week", 7), ("month", 30), ("year", 365)):
        m = re.search(rf"(\d+)\s*\+?\s*{unit}", t)
        if m:
            return now - timedelta(days=int(m.group(1)) * mult)
    return None


class IndeedScraper(BaseScraper):
    site = "indeed"

    def __init__(self):
        super().__init__()
        self.google = GoogleAuthService()

    def _job_url(self, jk: str) -> str:
        return f"https://www.indeed.com/viewjob?jk={jk}"

    async def is_logged_in(self) -> bool:
        """On indeed.com, logged-out shows a 'Sign in' gnav link; logged-in
        shows the account menu instead."""
        page = self.browser.page
        try:
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

        ok = await self.browser.goto(settings.indeed_search_url)
        if not ok:
            await self.browser.screenshot("screenshots/indeed_blocked.png")
            log.error("Indeed appears to be gated by a checkpoint we couldn't clear.")

        # Behave like a human skimming the results.
        await human.human_scroll(self.browser.page, steps=random.randint(4, 8))
        await human.think(settings.min_action_delay, settings.max_action_delay)

        raw = await self.browser.page.evaluate(_EXTRACT_JS)
        # Indeed repeats sponsored cards under the same jk — dedupe (keep order)
        # so max_jobs counts UNIQUE postings.
        seen: set[str] = set()
        unique = []
        for item in raw:
            jk = item.get("jk")
            if jk and jk not in seen:
                seen.add(jk)
                unique.append(item)
        log.info("Found {} result cards ({} unique) on the listing page", len(raw), len(unique))
        if not unique:
            await self.browser.screenshot("screenshots/indeed_no_cards.png")

        # Skip jobs already in the DB — don't re-fetch their detail pages.
        existing = self.repo.existing_keys(self.site)

        jobs: list[ScrapedJob] = []
        skipped = 0
        for item in unique:
            if len(jobs) >= settings.max_jobs:
                break
            jk = item["jk"]
            if jk in existing:
                skipped += 1
                continue
            description = item.get("snippet", "")
            # location / remote / job_type / company come from the reliable
            # listing mosaic; the detail page only adds description, company_url,
            # apply_url (and refines the company name).
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
                posted_at=compute_posted_at(item.get("posted"), item.get("pubDate")),
                apply_url=apply_url,
            )
            self.save(job)  # persist this job immediately, one by one
            jobs.append(job)
        log.info(
            "This page: {} new job(s) fetched & saved, {} already stored (detail skipped).",
            len(jobs),
            skipped,
        )
        return jobs

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
            if not u or "about:blank" in u:
                return False
            if "smartapply.indeed.com" in u:
                return True
            # An external company/ATS page (past Indeed's rc/clk redirect stub).
            return "indeed.com" not in u

        # Locate the apply control: Indeed Apply button, or a company-site link/button.
        btn = None
        for sel in (
            "#indeedApplyButton",
            "[data-testid='indeedApplyButton-test']",
            "#applyButtonLinkContainer a",
            "[data-testid='apply-button-container'] a",
            "#jobsearch-ViewJobButtons-container a[aria-label*='Apply' i]",
            "#jobsearch-ViewJobButtons-container button[aria-label*='Apply' i]",
        ):
            btn = await page.query_selector(sel)
            if btn:
                break
        if not btn:
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
            for _ in range(10):
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
