"""ZipRecruiter scraper — browser-driven, reading the page's own RSC payload.

Unlike the other JSON-API sites here, ZipRecruiter gives no HTTP path at all:

  • Cloudflare fronts every page. Plain requests get 403 "Just a moment…", and
    clearing the challenge in a browser does NOT hand over a reusable session —
    `cf_clearance` is bound to the TLS fingerprint that earned it, so replaying
    the cookies through curl_cffi still 403s (measured, not assumed).
  • THE PROXY IS NOT OPTIONAL. It is what makes the visitor look American.
    Straight from the server (a Finnish IP) ZipRecruiter geo-redirects to
    ziprecruiter.ie and there are no US listings to scrape at all.

So this one drives a real browser throughout. What it reads there is still
structured data rather than scraped markup — the Next.js App Router payload
(see scraper/flight.py):

  listing  /jobs-search?<filters>&page=N
           -> twoPaneData.serializedJobCardsData.jobKeysMap  {listingKey: card}
  detail   /jobs-search?<filters>&page=N&lk=<listingKey>
           -> twoPaneData.serializedJobDetailsData.jobDetails.htmlFullDescription

The two-pane layout is what makes the detail cheap: re-requesting the search URL
with `lk=` set renders that posting into the right-hand pane, so one navigation
yields the full description. `page=N` MUST be the page the card came from — `lk`
resolves against that page's own result set, and a key from page 3 asked for on
page 1 silently renders page 1's first job instead, with no error anywhere. Every
description is therefore checked against the listingKey it came back with, and a
posting whose pane disagrees is re-fetched from its own canonical job page.
(The RSC endpoint would be lighter still, but Next.js answers a bare `&page=`
with a route shell and no job data, so a plain navigation is the honest route.)

APPLY URL. "Apply" points at `ziprecruiter.com/job-redirect?match_token=…`, which
bounces to the employer. The token is base64 and *does* embed a URL, but decoding
it is NOT good enough: for some postings the embedded link is itself an
intermediary (easyapply.jobs → app.usebraintrust.com), so the decoded value is a
staging post rather than the employer's page. We therefore FOLLOW the redirect in
a throwaway tab and keep where it actually lands. Postings whose apply flow runs
on ZipRecruiter itself (`OPEN_APPLY_FLOW`, ~30%) have no employer URL to find;
those keep the ZipRecruiter posting, which is a real place a person applies.
"""
from __future__ import annotations

import asyncio
import base64
import html as _html
import random
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from config import settings
from logger import log
from scraper import human
from scraper.auth.google_auth import GoogleAuthService
from scraper.base_scraper import BaseScraper, ScrapedJob
from scraper.flight import flight_text, flight_value, text_chunk
from scraper.session import SessionStore

_BASE = "https://www.ziprecruiter.com"
ZIP_DOMAINS = ("ziprecruiter.com",)
#: ZipRecruiter's login modal uses a Google Identity Services iframe button, in
#: POPUP mode — the same component JobRight uses, driven the same way.
GOOGLE_BUTTON = 'iframe[src*="accounts.google.com/gsi/button"]'

#: Safety net; the walk stops on the first page that returns nothing new.
_MAX_PAGES = 30
#: Params that address a POSITION or UI state. The setting holds the link you
#: would paste out of ZipRecruiter, so it can carry the card that happened to be
#: open (`lk`) and a page number; we drive both ourselves.
_POSITIONAL = {"page", "lk"}

#: Enum -> what a person would call it.
_EMPLOYMENT = {
    "EMPLOYMENT_TYPE_NAME_FULL_TIME": "Full-time",
    "EMPLOYMENT_TYPE_NAME_PART_TIME": "Part-time",
    "EMPLOYMENT_TYPE_NAME_CONTRACTOR": "Contractor",
    "EMPLOYMENT_TYPE_NAME_TEMPORARY": "Temporary",
    "EMPLOYMENT_TYPE_NAME_INTERNSHIP": "Internship",
    "EMPLOYMENT_TYPE_NAME_OTHER": "Other",
}
_INTERVAL = {
    "PAY_INTERVAL_HOUR": "/hr",
    "PAY_INTERVAL_DAY": "/day",
    "PAY_INTERVAL_WEEK": "/wk",
    "PAY_INTERVAL_MONTH": "/mo",
    "PAY_INTERVAL_YEAR": "/yr",
}


def _clean_html(h: str) -> str:
    h = _html.unescape(h or "")
    h = h.replace("\r\n", "\n").replace("\r", "\n")
    h = re.sub(r"(?i)<\s*(br|/p|/div|/h[1-6]|/li|/tr|/ul)\s*/?>", "\n", h)
    h = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = _html.unescape(h).replace("\xa0", " ")
    h = re.sub(r"[ \t]+\n", "\n", h)
    return re.sub(r"\n{3,}", "\n\n", h).strip()


def _abs(url: Optional[str]) -> Optional[str]:
    """Card URLs are site-relative ("/co/Braintrust/Jobs")."""
    u = (url or "").strip()
    if not u:
        return None
    return u if u.startswith("http") else _BASE + ("" if u.startswith("/") else "/") + u


def _salary(pay: dict) -> Optional[str]:
    """Advertised pay, or None.

    Gated on `metadata.visible`. ZipRecruiter attaches its OWN estimate to
    postings where the employer published nothing, and marks the difference with
    this flag — 78 of 132 postings in a sample were visible, and the other 54
    carried an estimate with no displayed pay. Storing those would present a
    guess as the employer's offer, so they are dropped.

    Built from the structured min/max rather than `display.pay.text`, which
    renders a doubled currency sign ("$$75 - $90/hr") and abbreviates
    ("$150K - $190K/yr").
    """
    if not isinstance(pay, dict):
        return None
    if not (pay.get("metadata") or {}).get("visible"):
        return None
    lo, hi = pay.get("min"), pay.get("max")
    if lo is None and hi is None:
        return None
    per = _INTERVAL.get(pay.get("interval") or "", "")
    cur = "$" if (pay.get("currency") or "").endswith("USD") else ""

    def n(x):
        f = float(x)
        return f"{int(f):,}" if f == int(f) else f"{f:,.2f}"

    try:
        if lo is not None and hi is not None and float(lo) != float(hi):
            return f"{cur}{n(lo)} - {cur}{n(hi)}{per}"
        return f"{cur}{n(lo if lo is not None else hi)}{per}"
    except (TypeError, ValueError):
        return None


def _location(loc: dict) -> Optional[str]:
    """"City, ST" — composed rather than taken from `displayName`, which appends
    the country and postal code ("Alexandria, KY US 41001")."""
    if not isinstance(loc, dict):
        return None
    parts = [p for p in (loc.get("city"), loc.get("stateCode") or loc.get("state")) if p]
    if parts:
        return ", ".join(parts)[:255]
    return ((loc.get("displayName") or "").strip() or None) and loc["displayName"][:255]


def _posted_at(status: dict) -> Optional[datetime]:
    """`postedAtUtc` is ISO-8601 UTC — an exact instant, kept as naive UTC."""
    s = ((status or {}).get("postedAtUtc") or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(
            timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _decoded_apply_target(redirect_url: str) -> Optional[str]:
    """The employer URL packed inside a `job-redirect?match_token=…` token.

    A FALLBACK ONLY. The token is base64 and carries the destination, but for
    some postings that destination is itself an intermediary that redirects
    again, so following the link is what yields the employer's real page. This
    exists for when following is impossible — the residential proxy refuses some
    hosts outright (`403 SITE_PERMANENTLY_BLOCKED`), and an employer link we
    could not verify still beats falling back to the ZipRecruiter posting.
    """
    m = re.search(r"match_token=([A-Za-z0-9%_\-=]+)", redirect_url or "")
    if not m:
        return None
    try:
        raw = unquote(m.group(1))
        blob = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception:
        return None
    urls = re.findall(rb"https?://[ -~]{5,400}", blob)
    for cand in reversed(urls):
        u = cand.decode("utf-8", "replace").strip()
        if "ziprecruiter.com" not in u:
            return u
    return None


def _job_type(types: list) -> Optional[str]:
    names = [_EMPLOYMENT.get((t or {}).get("name"), "") for t in (types or [])]
    return ", ".join([n for n in names if n]) or None


def _is_remote(types: list) -> bool:
    return any("REMOTE" in ((t or {}).get("name") or "") for t in (types or []))


class ZipRecruiterScraper(BaseScraper):
    site = "ziprecruiter"
    table = "jobs"
    user_data_dir = settings.ziprecruiter_user_data_dir

    def __init__(self):
        super().__init__()
        self._role = (re.compile(settings.ziprecruiter_role_regex, re.I)
                      if settings.ziprecruiter_role_regex else None)
        self.google = GoogleAuthService()

    # ── auth ────────────────────────────────────────────────────────────────
    async def is_logged_in(self) -> bool:
        """Whether the CURRENT page is being served to a signed-in visitor.

        Requires POSITIVE evidence (a sign-out link), and treats anything
        ambiguous as signed out — attempting a login we did not need is
        harmless, whereas skipping one we did need silently scrapes as an
        anonymous visitor.

        Call this on a normal page, never on /authn/login: that page offers
        "Continue with email / Google / Apple" and never says "sign in", so a
        text probe there reports every visitor as already authenticated. That
        is exactly the bug this replaced.
        """
        try:
            state = await self.browser.page.evaluate(
                """() => {
                    const hrefs = Array.from(document.querySelectorAll('a[href]'))
                        .map(e => e.getAttribute('href') || '');
                    const t = (document.body.innerText || '').slice(0, 4000).toLowerCase();
                    return {
                        out: hrefs.some(h => h.indexOf('/authn/logout') >= 0),
                        offersSignIn: t.indexOf('sign in') >= 0 || t.indexOf('log in') >= 0,
                    };
                }""")
        except Exception:
            return False
        if state.get("out"):
            return True
        return not state.get("offersSignIn", True)

    async def ensure_logged_in(self) -> bool:
        """Sign in with Google, reusing the session in the DB when there is one.

        Best-effort: the listing and detail payloads render for signed-out
        visitors too, so a login failure costs nothing today and must not take
        the pass down with it. It is still the default — it is the journey a real
        visitor takes, and an anonymous crawl is the first thing a site tightens.
        """
        try:
            # The SHARED Google session first: with it the OAuth popup opens
            # straight on the account chooser instead of a full email+password
            # login, which is both faster and far less likely to trip Google's
            # "this browser may not be secure" check on a fresh profile.
            await self.google.load(self.browser.context)
            await SessionStore.load(self.browser.context, None, settings.ziprecruiter_session_file)
        except Exception:
            pass
        # Check on the HOMEPAGE, not the login page — see is_logged_in().
        await self.browser.goto(_BASE + "/")
        if await self.is_logged_in():
            log.info("[ziprecruiter] already signed in")
            return True
        if not (settings.google_email and settings.google_password):
            log.warning("[ziprecruiter] no GOOGLE_EMAIL/PASSWORD — continuing signed out")
            return False

        log.info("[ziprecruiter] signing in with Google...")
        await self.browser.goto(_BASE + "/authn/login?realm=candidates")
        page, ctx = self.browser.page, self.browser.context
        popup_holder: dict = {}
        ctx.on("page", lambda p: popup_holder.setdefault("p", p))
        try:
            await page.wait_for_selector(GOOGLE_BUTTON, timeout=15000)
            btn = page.frame_locator(GOOGLE_BUTTON).locator(
                'div[role="button"], [role="button"], button').first
            await btn.click(timeout=10000)
        except Exception as e:
            log.warning("[ziprecruiter] Google button not clickable: {}", str(e)[:90])
            return False

        popup = None
        for _ in range(15):
            if popup_holder.get("p"):
                popup = popup_holder["p"]
                break
            await asyncio.sleep(1)
        if popup is None:
            log.warning("[ziprecruiter] Google account-chooser popup did not open")
            return False
        await self._drive_popup(popup)

        await human.think(3, 6)
        await self.browser.goto(_BASE + "/")
        if await self.is_logged_in():
            try:
                await SessionStore.save(ctx, page, settings.ziprecruiter_session_file,
                                        domains=ZIP_DOMAINS)
            except Exception:
                pass
            try:  # keep the shared Google session fresh for the other sites
                await self.google.save(ctx, page)
            except Exception:
                pass
            log.info("[ziprecruiter] signed in via Google")
            return True
        log.warning("[ziprecruiter] sign-in did not complete — continuing signed out")
        return False

    async def _drive_popup(self, popup) -> None:
        """Account chooser -> password -> consent, whichever steps appear."""
        email, pwd = settings.google_email, settings.google_password
        await human.think(2, 3)
        for sel in (f'[data-identifier="{email}"]',
                    f'div[role="link"][aria-label*="{email}"]',
                    f'li:has-text("{email}")',
                    f'div[role="link"]:has-text("{email}")'):
            try:
                el = await popup.wait_for_selector(sel, timeout=4000, state="visible")
                if el:
                    await el.click()
                    await human.think(2, 4)
                    break
            except Exception:
                continue
        if pwd:
            try:
                box = await popup.wait_for_selector('input[type="password"]', timeout=7000,
                                                    state="visible")
                await box.click()
                await box.fill(pwd)
                await human.think(0.5, 1.2)
                nxt = await popup.wait_for_selector(
                    '#passwordNext button, #passwordNext, button:has-text("Next")', timeout=6000)
                await nxt.click()
                await human.think(4, 7)
            except Exception:
                pass  # SSO'd straight through
        for sel in ('button:has-text("Continue")', 'button:has-text("Allow")',
                    '#submit_approve_access'):
            try:
                el = await popup.wait_for_selector(sel, timeout=5000, state="visible")
                if el:
                    await el.click()
                    await human.think(2, 4)
                    break
            except Exception:
                continue
        for _ in range(20):
            if popup.is_closed():
                break
            await asyncio.sleep(1)

    # ── search ──────────────────────────────────────────────────────────────
    @staticmethod
    def _url(**extra) -> str:
        u = urlparse(settings.ziprecruiter_search_url)
        qs = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
              if k.lower() not in _POSITIONAL]
        qs += [(k, str(v)) for k, v in extra.items()]
        return urlunparse(u._replace(query=urlencode(qs)))

    async def _payload(self, url: str) -> str:
        """Navigate and hand back the page's flight stream ("" if blocked)."""
        if not await self.browser.goto(url):
            log.warning("[ziprecruiter] checkpoint not cleared at {}", url[:90])
            return ""
        return flight_text(await self.browser.page.content())

    def _matches(self, title: str) -> bool:
        return not self._role or bool(self._role.search(title or ""))

    def _saved(self) -> int:
        return self.counts["inserted"] + self.counts["updated"] + self.counts["unchanged"]

    # ── detail ──────────────────────────────────────────────────────────────
    @staticmethod
    def _pane_description(ft: str, listing_key: str) -> Optional[str]:
        """The right pane's description, but ONLY if the pane is showing the job
        we asked for. Returns None when it is showing something else.

        This check is not paranoia. `lk=` selects a card out of the CURRENT
        result set, so asking for a page-3 key on a page-1 URL silently renders
        page 1's first job instead — the pane looks perfectly healthy and every
        posting from page 2 onward would have been stored with the same wrong
        description. Verifying the key is what turns that into a miss we can see.
        """
        # Two payload shapes carry the same object, so accept either:
        #   search page   twoPaneData.serializedJobDetailsData.jobDetails
        #   canonical job page   a top-level `jobDetails` (no twoPaneData at all)
        # Only handling the first is why the canonical fallback never actually
        # rescued anything — it silently found nothing every time.
        tp = flight_value(ft, "twoPaneData") or {}
        jd = ((tp.get("serializedJobDetailsData") or {}).get("jobDetails")) or {}
        if not jd:
            jd = flight_value(ft, "jobDetails") or {}
        if jd.get("listingKey") != listing_key:
            return None
        raw = jd.get("htmlFullDescription")
        text = text_chunk(ft, raw)
        if text is None and isinstance(raw, str):
            text = raw            # already inline rather than a reference
        return _clean_html(text or "")

    async def _description(self, card: dict, page_no: int) -> str:
        """Full description for one posting.

        Asks the search page the card came FROM — `lk=` only resolves against
        that page's own results — and falls back to the posting's canonical page,
        which addresses the job directly and so cannot be confused by paging.
        """
        if not settings.fetch_descriptions:
            return ""
        key = card["listingKey"]
        ft = await self._payload(self._url(page=page_no, lk=key))
        if ft:
            desc = self._pane_description(ft, key)
            if desc:
                return desc
        canonical = _abs(card.get("rawCanonicalZipJobPageUrl"))
        if canonical:
            ft = await self._payload(canonical)
            if ft:
                desc = self._pane_description(ft, key)
                if desc:
                    return desc
        log.warning("[ziprecruiter] no description for {} ({})",
                    key, (card.get("title") or "")[:40])
        return ""

    async def _employer_url(self, redirect_url: str) -> Optional[str]:
        """Where `job-redirect?match_token=…` actually lands.

        Followed rather than decoded: the token embeds a URL, but for some
        postings that is an intermediary which redirects again, so the decoded
        value is not the employer's page. Done in a throwaway tab so a slow or
        hanging destination cannot strand the pane we are scraping from.
        """
        ctx = self.browser.context
        tab = await ctx.new_page()
        try:
            await tab.goto(redirect_url, wait_until="domcontentloaded", timeout=45000)
            waited = 0.0
            while "ziprecruiter.com" in (tab.url or "") and waited < 12.0:
                await asyncio.sleep(0.4)
                waited += 0.4
            final = tab.url or ""
            return final if final.startswith("http") and "ziprecruiter.com" not in final else None
        except Exception as e:
            log.warning("[ziprecruiter] apply redirect failed: {}", str(e)[:70])
            return None
        finally:
            try:
                await tab.close()
            except Exception:
                pass

    # ── mapping ─────────────────────────────────────────────────────────────
    def _to_job(self, card: dict, description: str, apply_url: str) -> Optional[ScrapedJob]:
        key = (card.get("listingKey") or "").strip()
        if not key:
            return None
        page_url = _abs(card.get("rawCanonicalZipJobPageUrl")) or _BASE
        company = card.get("company") or {}
        return ScrapedJob(
            site_job_id=key[:190],
            title=(card.get("title") or "").strip(),
            description=description,
            link=page_url,
            location=_location(card.get("location")),
            posted_at=_posted_at(card.get("status")),
            apply_url=apply_url,
            company=(company.get("name") or "").strip() or None,
            company_url=_abs(card.get("companyUrl")),
            job_type=_job_type(card.get("employmentTypes")),
            remote=_is_remote(card.get("locationTypes")),
            salary=_salary(card.get("pay")),
        )

    # ── run ─────────────────────────────────────────────────────────────────
    async def scrape(self) -> None:
        try:
            await self.ensure_logged_in()
        except Exception as e:
            log.warning("[ziprecruiter] sign-in step failed, continuing: {}", str(e)[:90])

        delay = float(settings.ziprecruiter_delay_s)
        loop = asyncio.get_running_loop()
        # Every posting costs a navigation, and an external one costs a second
        # tab on top. That is bounded work per job, but the job COUNT is not
        # ours to control — a widened filter turns this into hours. Stop on the
        # clock instead, and let the next run pick up where this one left off.
        deadline = loop.time() + max(1, settings.ziprecruiter_budget_min) * 60

        # ── 1. walk the listing ─────────────────────────────────────────────
        cards: dict[str, dict] = {}
        #: listingKey -> the page it appeared on. `lk=` only resolves against
        #: that page's own result set, so the detail fetch needs it.
        card_page: dict[str, int] = {}
        for page in range(1, _MAX_PAGES + 1):
            ft = await self._payload(self._url(page=page))
            if not ft:
                break
            cd = ((flight_value(ft, "twoPaneData") or {})
                  .get("serializedJobCardsData")) or {}
            jkm = cd.get("jobKeysMap") or {}
            before = len(cards)
            for k in jkm:
                card_page.setdefault(k, page)
            cards.update(jkm)
            log.info("[ziprecruiter] page {} — {} cards, {} new (unique so far {}, site says {})",
                     page, len(jkm), len(cards) - before, len(cards),
                     cd.get("totalListings"))
            if not jkm or len(cards) == before:
                break
            await asyncio.sleep(random.uniform(delay * 0.6, delay * 1.4))
        else:
            log.info("[ziprecruiter] hit the {}-page budget — raise _MAX_PAGES to go deeper.",
                     _MAX_PAGES)
        if not cards:
            log.warning("[ziprecruiter] no cards collected — nothing to do.")
            return

        # ── 2. detail + apply URL, one posting at a time ───────────────────
        wanted = [c for c in cards.values() if self._matches(c.get("title", ""))]
        # Skip postings we already hold a description for. Each one costs a
        # navigation plus a redirect tab, so a full sweep runs past the time
        # budget — and without this the next run would restart at page 1 and
        # re-walk the same head every time, leaving the tail permanently
        # unscraped. Keyed on "has a description" rather than "have I seen this
        # id", so a posting whose detail failed is retried instead of written
        # off. Together with the age gate the backlog stays bounded.
        try:
            done = self.repo.keys_with_descriptions(self.site)
        except Exception as e:
            log.warning("[ziprecruiter] could not read stored ids, doing a full pass: {}",
                        str(e)[:80])
            done = set()
        fresh = [c for c in wanted if c.get("listingKey") not in done]
        log.info("[ziprecruiter] {} unique posting(s), {} match the role filter, "
                 "{} still need a description",
                 len(cards), len(wanted), len(fresh))
        wanted = fresh
        if not wanted:
            log.info("[ziprecruiter] everything on this search is already stored.")
            return
        for i, card in enumerate(wanted, 1):
            if loop.time() >= deadline:
                log.warning("[ziprecruiter] {}-minute budget reached at {}/{} — stopping; "
                            "the rest are picked up next run.",
                            settings.ziprecruiter_budget_min, i, len(wanted))
                break
            if settings.max_jobs and self._saved() >= settings.max_jobs:
                log.info("[ziprecruiter] reached max_jobs={} — stopping.", settings.max_jobs)
                break
            title = (card.get("title") or "")[:44]
            posted = _posted_at(card.get("status"))
            # Age-gate BEFORE the two navigations this posting would cost. The
            # search URL's own `days=` filter is the primary bound; this holds
            # the line if it is ever widened in the admin UI.
            if self._too_old(posted, settings.ziprecruiter_max_age_days):
                self.counts["too_old"] += 1
                log.info("[ziprecruiter] skipped (posted {} — older than {}d) — {}",
                         posted, settings.ziprecruiter_max_age_days, title)
                continue

            key = card["listingKey"]
            description = await self._description(card, card_page.get(key, 1))

            abc = card.get("applyButtonConfig") or {}
            page_url = _abs(card.get("rawCanonicalZipJobPageUrl")) or _BASE
            apply_url = None
            if abc.get("externalApplyUrl"):
                apply_url = await self._employer_url(abc["externalApplyUrl"])
                if not apply_url:
                    # Following it failed (commonly: the proxy refuses that
                    # host). Fall back to the destination named inside the
                    # token rather than giving up on the employer entirely.
                    apply_url = _decoded_apply_target(abc["externalApplyUrl"])
                    if apply_url:
                        log.info("[ziprecruiter] {}/{} {} — redirect unreachable, "
                                 "using the token's target {}",
                                 i, len(wanted), title, apply_url[:60])
            if not apply_url:
                # Either the apply flow runs on ZipRecruiter (OPEN_APPLY_FLOW) or
                # the redirect did not leave the site. The posting itself is a
                # real place to apply, so keep the row rather than drop it.
                apply_url = page_url
                log.info("[ziprecruiter] {}/{} {} — no employer URL ({}), using the ZR page",
                         i, len(wanted), title, abc.get("applyButtonType") or "unknown")

            job = self._to_job(card, description, apply_url)
            if job:
                self.save(job)
            await asyncio.sleep(random.uniform(delay * 0.6, delay * 1.4))


async def main() -> None:
    await ZipRecruiterScraper().run()


if __name__ == "__main__":
    asyncio.run(main())
