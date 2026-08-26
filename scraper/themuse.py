"""The Muse scraper — real browser, no API.

The public API (`/api/public/jobs`) is NOT used, deliberately: it never returns the
employer's apply URL (only `refs.landing_page`, a Muse page), its ordering is a
relevance blend rather than a date sort — a 2024 posting shows up on page 1, so
only ~15% of results land inside a 7-day window — and their docs require app
registration for use beyond testing.

The site's own search does all of it properly:

    /search/location/remote-flexible/keyword/software-engineering/date-posted/last_7d

`date-posted/last_7d` filters server-side (so almost everything fetched is worth
keeping) and `?page=N` paginates. Per job we click "APPLY ON COMPANY SITE", which
opens the employer's ATS in a new tab — that tab's URL is the apply link.

Note the location slug is `remote-flexible`, not `flexible-remote`; the latter is
accepted by the router but silently matches 0 jobs.
"""
from __future__ import annotations

import asyncio
import html as _html
import random
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlsplit, urlunsplit

from config import settings, proxy_for
from logger import log
from scraper.base_scraper import BaseScraper, ScrapedJob
from scraper.browser import clear_challenge

#: Ad/analytics/media hosts to drop before they reach the network. The Muse pages
#: are ad-heavy, and through the residential relay EVERY one of these costs three
#: rejected CONNECT retries (~471 x 504 in one measured run) — they dominated the
#: runtime while contributing nothing to the scrape.
_BLOCK_HOST_RE = re.compile(
    r"(googletagmanager|google-analytics|googleadservices|googlesyndication|doubleclick|"
    r"facebook\.net|connect\.facebook|licdn\.com|linkedin\.com/px|jwplayer|jwpcdn|"
    r"track-1\.themuse|segment\.(io|com)|hotjar|mixpanel|amplitude|optimizely|"
    r"bing\.com|clarity\.ms|adsrvr|criteo|taboola|outbrain|quantserve|scorecardresearch|"
    r"gvt2\.com|adroll|pinterest|tiktok|snapchat)", re.I)

#: Resource kinds a scraper never needs. NOT stylesheets — Playwright decides
#: visibility/clickability from layout, and an unstyled page can make the
#: "APPLY ON COMPANY SITE" button untargetable.
_BLOCK_TYPES = {"image", "media", "font"}


async def _install_blocker(page) -> None:
    """Abort ads/analytics/heavy assets so page loads don't crawl behind a proxy."""
    async def _route(route):
        try:
            req = route.request
            if req.resource_type in _BLOCK_TYPES or _BLOCK_HOST_RE.search(req.url or ""):
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass
    try:
        await page.route("**/*", _route)
    except Exception:
        pass


_MAX_CF_FAILURES = 3
#: Runaway guard only — the `date-posted` filter bounds the set, so paging ends
#: naturally when a page comes back empty.
_PAGE_GUARD = 40

#: Hosts that are never the employer's application target.
_JUNK_HOST_RE = re.compile(
    r"(themuse\.com|challenges\.cloudflare|cloudflare\.com|google\.|facebook\.|twitter\.|x\.com|"
    r"instagram\.|youtube\.|t\.me|discord\.|apple\.com|play\.google|linkedin\.com/(company|feed))",
    re.I,
)


def _is_employer_url(url: str) -> bool:
    """Employer/ATS link? Match scheme+host+path only, never the query string —
    tracking params routinely carry the aggregator's own domain."""
    if not url or not url.startswith("http"):
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    return bool(p.hostname) and not _JUNK_HOST_RE.search(f"{p.scheme}://{p.netloc}{p.path}")


#: US states + DC/territories — a trailing ", XX" is the tell for a US city, since
#: The Muse never writes "United States" on these listings.
_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA",
    "ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
    "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC","PR","VI","GU",
}
_US_WORDS = re.compile(r"\b(United States|USA|U\.S\.A?\.?)\b", re.I)
#: Countries seen on "Flexible / Remote" postings that are NOT US roles.
_FOREIGN = re.compile(
    r"\b(India|Canada|United Kingdom|UK|Ireland|Germany|France|Spain|Portugal|Poland|Romania|"
    r"Netherlands|Belgium|Sweden|Norway|Denmark|Finland|Italy|Switzerland|Austria|Czechia|Czech|"
    r"Hungary|Greece|Turkey|Israel|Egypt|Nigeria|Kenya|South Africa|Brazil|Argentina|Chile|"
    r"Colombia|Mexico|Peru|Australia|New Zealand|Singapore|Malaysia|Indonesia|Philippines|"
    r"Thailand|Vietnam|China|Hong Kong|Taiwan|Japan|Korea|Pakistan|Bangladesh|Sri Lanka|UAE|"
    r"United Arab Emirates|Saudi|Qatar|Ukraine|Serbia|Bulgaria|Croatia|Lithuania|Latvia|"
    r"Estonia|Slovakia|Slovenia|Morocco|Ghana|Uruguay|Costa Rica|Panama|Ecuador)\b",
    re.I)


def _is_us_location(text: str) -> bool:
    """True unless the location names a non-US country.

    The Muse renders remote roles as "Flexible / Remote (+N more)" and hides the
    real city behind that control — expanded, a Kyndryl "remote" job reads
    "Bangalore, India". Detection: an explicit US marker or a ", XX" state code
    wins; a named foreign country loses; a bare "Flexible / Remote" with no
    country at all is kept.
    """
    t = text or ""
    if _US_WORDS.search(t):
        return True
    if any(m in _US_STATES for m in re.findall(r",\s*([A-Z]{2})\b", t)):
        return True
    return not _FOREIGN.search(t)


#: Employment Type comes through as FULL_TIME / PART_TIME / CONTRACTOR.
_EMPLOYMENT = {
    "FULL_TIME": "Full-Time", "PART_TIME": "Part-Time", "CONTRACTOR": "Contract",
    "TEMPORARY": "Temporary", "INTERN": "Internship", "OTHER": None,
}


#: `Posted:` on the detail page is a full ISO stamp; the time part is optional.
_POSTED_RE = r"Posted:\s*(\d{4}-\d{2}-\d{2}(?:T[\d:]+)?)"


def _detail_fields(body: str) -> dict:
    """Pull the structured block The Muse prints above the apply button.

    Preferred over the page header: `Posted:` is an exact ISO timestamp (the
    header only says "5 days ago") and `Client-provided location(s):` lists every
    location, so the city isn't hidden behind "(+N more)".
    """
    out: dict = {}
    m = re.search(r"Client-provided location\(s\):\s*(.+)", body)
    if m:
        out["location"] = re.sub(r"\s+", " ", m.group(1)).strip()[:255]
    # Last match, not first: one row (5499) has the employer's own
    # "Employment Type: Full-Time" above the Muse block, and a first-match
    # regex captured a bare "F" from it. The Muse-generated value is last.
    types = re.findall(r"Employment Type:\s*([A-Z_]+)", body)
    if types:
        val = types[-1].strip()
        out["job_type"] = _EMPLOYMENT.get(val, val.title())
    # Capture the whole ISO stamp: the old pattern matched the time but threw
    # it away in a non-capturing group, backdating every posting to midnight.
    m = re.search(_POSTED_RE, body)
    if m:
        raw = m.group(1)
        try:
            if "T" in raw:
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                out["posted"] = dt
            else:
                out["posted"] = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    return out


#: Last line of the company card The Muse prints above every posting; the
#: employer's own text begins immediately after it. Exactly one occurrence in
#: all 270 stored rows, at offset 263-545 — the upper bound in `_scrape_job`
#: keeps a malformed page from swallowing the posting instead of the chrome.
#: Matched case-insensitively: the caps come from CSS `text-transform`, and the
#: `company_url` lookup 40 lines above already hedges the same way.
_COMPANY_CARD_RE = re.compile(r"VIEW COMPANY PROFILE", re.I)

#: Newsletter signup spliced into the MIDDLE of the posting text (seen anywhere
#: from offset ~1.1k to ~6.9k), so no amount of end-trimming reaches it.
#:
#: The gap is BOUNDED. With an unbounded `.*?`, a page that renders the widget
#: twice while the first copy loses its closing line — the closer is legal
#: boilerplate that gets reworded — makes the lazy span run from the first
#: opener to the only closer and swallow the entire posting between them.
#: Measured at 6646 chars in, 10 chars out. `db.py` upserts with
#: `description = VALUES(description)`, so that would overwrite a good row.
#: Observed block length is 259-298 chars across 270/270 rows; 600 is ~2x.
_NEWSLETTER_RE = re.compile(
    r"Want more jobs like this\?.{0,600}?"
    r"By signing up, you agree to our Terms of Service & Privacy Policy\.",
    re.S)

#: Anchors the Muse-generated metadata block that closes every posting. The
#: three markers this replaced (`APPLY ON COMPANY SITE`, `Similar Jobs`,
#: `About The Muse`) occur ZERO times in all 270 stored rows — `inner_text`
#: never reached them — so they were decorative, not fallbacks. These two are
#: what actually appears; a miss is logged rather than passing silently.
_TAIL_MARKERS = ("Client-provided location(s):",)
_TAIL_ANCHOR_RE = re.compile(r"\nJob ID:\s*\S+\n|\nPosted:\s*\d{4}-")

#: A cleaned description shorter than this means the carving went wrong. Storing
#: it would overwrite a good row via the upsert, so fall back to the raw body.
_MIN_DESC_LEN = 1000


def _clean(t: str) -> str:
    t = _html.unescape(t or "")
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _relative_posted(text: str) -> Optional[date]:
    """The detail page prints a relative age beside the title — "5 days ago",
    "Yesterday", "2 days ago". It never says "Posted", which the first version
    required, so every date came back null."""
    t = (text or "").lower()
    today = datetime.now(timezone.utc).date()   # UTC, matching _too_old
    if re.search(r"\b(just now|today)\b", t):
        return today
    if re.search(r"\byesterday\b", t):
        return today - timedelta(days=1)
    m = re.search(r"\b(\d{1,2})\s*(minute|hour|day|week|month)s?\s+ago\b", t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    days = {"minute": 0, "hour": 0, "day": n, "week": n * 7, "month": n * 30}[unit]
    return today - timedelta(days=days)


class TheMuseScraper(BaseScraper):
    site = "themuse"
    #: Live table — non-US postings are rejected on the listing page and rows
    #: without an employer apply URL are never stored, so what lands here is
    #: app-ready as scraped.
    table = "jobs"

    def __init__(self):
        super().__init__()
        self._role = (re.compile(settings.themuse_role_regex, re.I)
                      if settings.themuse_role_regex else None)
        self._delay = max(1.0, float(settings.themuse_delay_s))
        self._seen: set = set()

    # Bespoke browser lifecycle (real Chrome over CDP) — BaseScraper.run()'s
    # StealthBrowser path is deliberately not used.
    async def run(self) -> None:
        from scraper.auth.site_login import kill_chrome_tree, launch_chrome, session_file
        from scraper.session import SessionStore

        self.repo.connect()
        proc = relay = pw = None
        try:
            try:
                # Stored ids carry a hash that has since rotated, so both
                # sides of the membership test are stripped to their stable base.
                self._seen = {self._skip_key(k) for k in self.repo.existing_keys(self.site)}
                log.info("[themuse] {} jobs already stored — their pages will be skipped",
                         len(self._seen))
            except Exception:
                self._seen = set()

            from patchright.async_api import async_playwright
            server = None
            if proxy_for("themuse"):
                # Chrome cannot send proxy credentials; front it with the relay.
                from scraper.local_proxy import LocalRoutingProxy
                direct = settings.proxy_bypass.split(",") if settings.proxy_bypass else []
                relay = LocalRoutingProxy(proxy_for("themuse"), direct)
                server = "http://127.0.0.1:%d" % await relay.start()
            profile = str(Path(settings.user_data_dir).parent / "muse-chrome")
            proc, endpoint = launch_chrome(profile, int(settings.themuse_cdp_port), server)
            pw = await async_playwright().start()
            browser = await pw.chromium.connect_over_cdp(endpoint)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.set_default_timeout(60000)
            await _install_blocker(page)
            # Google session from the DB, never a local file (same as the others).
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
        """The Muse paginates with ?page=N (the /page/N path form is ignored)."""
        if n <= 1:
            return base
        s = urlsplit(base)
        q = "&".join(x for x in (s.query, f"page={n}") if x)
        return urlunsplit((s.scheme, s.netloc, s.path, q, s.fragment))

    @staticmethod
    def _job_id(url: str) -> str:
        """/jobs/<company>/<slug> — the listing id we store.

        NOT stable: The Muse appends a six-hex hash to the slug and regenerates
        it on every render, so the same posting comes back under a new id each
        pass. The `(site, fingerprint)` unique key is what actually keeps those
        out of the table; see scraper/db.py. Use `_skip_key` for comparisons.
        """
        m = re.search(r"/jobs/([^/?#]+/[^/?#]+)", url or "")
        return (m.group(1) if m else (url or ""))[:190]

    @staticmethod
    def _skip_key(job_id_or_url: str) -> str:
        """The listing id with the volatile hash suffix removed.

        Used ONLY to decide whether a posting has already been scraped, never
        as the stored key. Without it the "already seen" test compares a stored
        id against a freshly rotated one, never matches, and every detail page
        is re-opened on every run — each one a Cloudflare challenge plus delays.

        The trade-off is that two genuinely different requisitions at the same
        company with the same title share a slug base, so the second would be
        skipped rather than fetched. That is a miss on a rare posting, weighed
        against re-fetching several hundred pages every run; nothing is lost
        from the table either way, because the skip only suppresses a fetch.
        """
        base = TheMuseScraper._job_id(job_id_or_url)
        return re.sub(r"-[0-9a-f]{6}$", "", base)

    async def _job_cards(self, page) -> list:
        """[(url, card_text)] for every result on the page.

        Walks up from each job link until an ancestor's text carries a location,
        so the card's "… At <Company> - <City, Region> Posted on <date>" line can
        be read without opening the job."""
        return await page.evaluate(
            """() => {
                const out = [], seen = new Set();
                document.querySelectorAll('a[href*="/jobs/"]').forEach(a => {
                    const href = a.href;
                    if (!/\\/jobs\\/[^\\/?#]+\\/[^\\/?#]+/.test(href) || seen.has(href)) return;
                    seen.add(href);
                    let el = a, txt = '';
                    for (let i = 0; i < 6 && el; i++) {
                        el = el.parentElement;
                        if (!el) break;
                        const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (/,\\s*[A-Za-z]/.test(t) && t.length > 30) { txt = t; break; }
                    }
                    out.push([href, txt.slice(0, 200)]);
                });
                return out;
            }""")

    async def _employer_url(self, ctx, page) -> Optional[str]:
        """Click "APPLY ON COMPANY SITE" and keep the URL of the tab it opens."""
        tabs: list = []

        def _on_page(p):
            tabs.append(p)

        ctx.on("page", _on_page)
        try:
            # The page carries THREE apply buttons and one of them is HIDDEN.
            # Playwright's :has-text() is case-insensitive, so query_selector()
            # returned the hidden "Apply on company site" first and
            # scroll_into_view_if_needed() timed out on it — the click never
            # fired and every job was skipped. Take the first VISIBLE one.
            btn = None
            try:
                cands = await page.query_selector_all(
                    "button:has-text('APPLY ON COMPANY SITE'), a:has-text('APPLY ON COMPANY SITE')")
            except Exception:
                cands = []
            for c in cands:
                try:
                    if await c.is_visible():
                        btn = c
                        break
                except Exception:
                    continue
            if not btn:
                return None
            try:
                # Best-effort scroll; never let it abort the click.
                try:
                    await btn.scroll_into_view_if_needed(timeout=4000)
                except Exception:
                    pass
                await asyncio.sleep(0.4)
                await btn.click(timeout=8000)
            except Exception:
                try:
                    await btn.click(timeout=6000, force=True)
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

    async def _expand_locations(self, page) -> str:
        """Reveal the real city and return the location text.

        Remote roles render as "Flexible / Remote (+N more)"; the actual city
        only appears after clicking that control — which is how an India-based
        posting passes for remote."""
        try:
            el = await page.query_selector(r"text=/\+\d+ more/")
            if el:
                try:
                    await el.click(timeout=5000)
                    await asyncio.sleep(1.2)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            head = await page.evaluate(
                r"() => (document.body.innerText || '').slice(0, 700).replace(/\s+/g, ' ')")
        except Exception:
            head = ""
        return head

    async def _scrape_job(self, ctx, page, url: str) -> Optional[ScrapedJob]:
        body = ((await page.inner_text("body")) or "")
        page_title = (await page.title()) or ""
        title = (await page.evaluate(
            "() => (document.querySelector('h1')?.innerText || document.title || '').trim()")) or ""
        title = re.sub(r"\s+", " ", title).strip()
        if self._role and not self._role.search(title):
            return None

        # Prefer the structured block ("Client-provided location(s):", "Posted:",
        # "Employment Type:"). Only fall back to expanding the "(+N more)" header
        # control when a page doesn't carry it.
        fields = _detail_fields(body)
        loc_text = fields.get("location") or ""
        if not loc_text:
            head = await self._expand_locations(page)
            loc_text = head
        if settings.themuse_us_only and not _is_us_location(loc_text):
            self.counts["skipped"] += 1
            log.info("[themuse] skipped (not US) — {} | {}", title[:38], loc_text[:60])
            return None

        posted = fields.get("posted") or _relative_posted(body)
        # The Muse's own URL filter bottoms out at last_7d, so the 1-day
        # narrowing happens here, per posting. Its ordering is a relevance
        # blend rather than a date sort, so we cannot early-stop — every
        # page has to be checked.
        max_age = settings.themuse_max_age_days
        if self._too_old(posted, max_age):
            self.counts["too_old"] += 1
            log.info("[themuse] skipped (posted {} — older than {}d) — {}",
                     posted, max_age, title[:44])
            return None

        apply_url = await self._employer_url(ctx, page)
        if not apply_url:
            return None  # never store a Muse link as the apply target

        # "<role> at <Company> | The Muse"
        company = None
        m = re.search(r"\bat\s+(.+?)\s*\|\s*The Muse", page_title)
        if m:
            company = m.group(1).strip()[:255]
        # "VIEW COMPANY PROFILE" points at this job's employer; plain /profiles/
        # links further down belong to unrelated "similar jobs" cards.
        company_url = await page.evaluate(
            """() => {
                const vp = Array.from(document.querySelectorAll('a[href*="/profiles/"]'))
                    .find(e => /view company profile/i.test(e.innerText || ''));
                if (vp) return vp.href;
                const first = Array.from(document.querySelectorAll('a[href*="/profiles/"]'))
                    .map(e => e.href)[0];
                return first || '';
            }""") or None

        location = fields.get("location") or (loc_text[:255] if loc_text else None)
        job_type = fields.get("job_type")

        # `body` is the WHOLE page, so the posting has to be carved out of it:
        # chrome sits above it, below it, and — uniquely here — spliced into the
        # middle of it. `body` itself is left intact; `_detail_fields` and
        # `_relative_posted` above still regex the full page text.
        desc = body
        # Above: the nav, then the company card. Cut through the card's last line.
        m = _COMPANY_CARD_RE.search(desc)
        if m and 0 < m.start() < 1200:
            desc = desc[m.end():]
        # Inside: the newsletter widget, which a boundary slice cannot reach.
        desc = _NEWSLETTER_RE.sub("", desc)
        # Below: the Muse-generated metadata block. `_detail_fields` has already
        # parsed it, so it is duplicate text in the description.
        cut = -1
        for marker in _TAIL_MARKERS:
            i = desc.find(marker)
            if i > 400:
                cut = i
                break
        if cut < 0:
            m = _TAIL_ANCHOR_RE.search(desc, 400)
            cut = m.start() if m else -1
        if cut > 0:
            desc = desc[:cut]
        else:
            # Not fatal — but it means the template changed, and the metadata
            # block is now riding along in every description. Surface it in the
            # run log instead of letting it reach the prompt unnoticed.
            log.warning("themuse: no tail marker matched for {} — template may have changed", url)

        cleaned = _clean(desc)
        if len(cleaned) < _MIN_DESC_LEN:
            # Carving produced something implausibly short. The upsert would
            # replace a good stored row with this, so keep the uncarved text.
            log.warning(
                "themuse: carved description only {} chars for {} — keeping raw body",
                len(cleaned), url)
            cleaned = _clean(body)

        return ScrapedJob(
            site_job_id=self._job_id(url),
            title=title[:500],
            description=cleaned,
            link=url,
            location=location,
            posted_at=posted,
            apply_url=apply_url,
            company=company,
            company_url=(company_url[:1024] if company_url else None),
            job_type=job_type,
            remote=bool(location and "remote" in location.lower()),
            salary=None,
        )

    def _saved(self) -> int:
        return self.counts["inserted"] + self.counts["updated"] + self.counts["unchanged"]

    async def scrape_browser(self, ctx, page) -> None:
        base = settings.themuse_search_url
        log.info("[themuse] listing: {}", base)
        await self._scrape_listing(ctx, page, base)

    async def _scrape_listing(self, ctx, page, base: str) -> None:
        cf_fails = 0
        for pageno in range(1, _PAGE_GUARD + 1):
            if settings.max_jobs and self._saved() >= settings.max_jobs:
                break
            try:
                await page.goto(self._page_url(base, pageno), wait_until="domcontentloaded",
                                timeout=60000)
            except Exception as e:
                log.warning("[themuse] listing page {} failed: {}", pageno, str(e)[:70])
                break
            if not await clear_challenge(page, max_wait_s=120):
                cf_fails += 1
                log.warning("[themuse] listing page {} — challenge not cleared", pageno)
                if cf_fails >= _MAX_CF_FAILURES:
                    log.warning("[themuse] repeated challenge failures — stopping")
                    break
                continue
            cf_fails = 0
            await asyncio.sleep(4)  # the results render client-side

            cards = await self._job_cards(page)
            if not cards:
                log.info("[themuse] page {} listed no jobs — end of the filtered set", pageno)
                break

            fresh, off_us = [], 0
            for href, card in cards:
                if self._skip_key(href) in self._seen:
                    continue
                # Reject non-US HERE — opening the job first would cost a page
                # load and an apply click for nothing.
                if settings.themuse_us_only and card and not _is_us_location(card):
                    off_us += 1
                    self.counts["skipped"] += 1
                    continue
                fresh.append(href)
            log.info("[themuse] page {}: {} jobs ({} new, {} skipped as non-US)",
                     pageno, len(cards), len(fresh), off_us)
            if not fresh:
                continue

            for link in fresh:
                if settings.max_jobs and self._saved() >= settings.max_jobs:
                    return
                try:
                    await page.goto(link, wait_until="domcontentloaded", timeout=60000)
                    if not await clear_challenge(page, max_wait_s=120):
                        cf_fails += 1
                        if cf_fails >= _MAX_CF_FAILURES:
                            log.warning("[themuse] repeated challenge failures — stopping")
                            return
                        continue
                    cf_fails = 0
                    await asyncio.sleep(2)
                    job = await self._scrape_job(ctx, page, link)
                    if job:
                        self.save(job)
                        self._seen.add(self._skip_key(job.site_job_id))
                except Exception as e:
                    log.warning("[themuse] {} error: {}", link[-44:], str(e)[:70])
                await asyncio.sleep(self._delay + random.uniform(0, 2.0))
