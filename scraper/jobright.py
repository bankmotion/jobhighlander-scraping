"""JobRight scraper (experimental).

jobright.ai is a login-gated, AI-personalized recommendation feed (an API-driven
React SPA — not a public search). We sign in with Google (its login modal uses a
Google Identity Services iframe button, same as Indeed → driven via the CDP FedCm
domain), open /jobs/recommend, and read the recommended jobs.

Writes to the live `jobs` table (site='jobright'), alongside Indeed & Glassdoor.
The recommendations depend on the signed-in account's résumé/preferences on
JobRight.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from config import settings
from logger import log
from scraper import human
from scraper.auth.google_auth import GoogleAuthService
from scraper.base_scraper import BaseScraper, ScrapedJob
from scraper.session import SessionStore

JOBRIGHT_DOMAINS = ("jobright.ai",)
GOOGLE_BUTTON = 'iframe[src*="accounts.google.com/gsi/button"]'


def extract_salary(jr: dict):
    """Best-effort salary string from a JobRight jobResult. Field names vary, so
    try known string fields, then a min/max object. Returns None if absent."""
    if not isinstance(jr, dict):
        return None
    for k in (
        "salaryDesc", "salaryRange", "estimatedSalaryDesc", "payRange",
        "compensation", "salaryText", "displaySalary", "salaryDisplayText",
        "estimatedSalary", "salary", "salaryEstimate",
    ):
        v = jr.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            lo = v.get("min") or v.get("minValue") or v.get("from") or v.get("minAmount")
            hi = v.get("max") or v.get("maxValue") or v.get("to") or v.get("maxAmount")
            if lo or hi:
                cur = v.get("currency") or "$"
                per = v.get("period") or v.get("unit") or v.get("interval") or ""
                parts = [f"{cur}{x}" for x in (lo, hi) if x]
                return (" - ".join(parts) + (f" {per}" if per else "")).strip() or None
    return None


class JobRightScraper(BaseScraper):
    site = "jobright"
    table = "jobs"  # live table — JobRight jobs show in the app
    user_data_dir = settings.jobright_user_data_dir

    def __init__(self):
        self.proxy_url = settings.jobright_proxy_url or settings.proxy_url
        super().__init__()
        self.google = GoogleAuthService()

    # ── auth ────────────────────────────────────────────────────────────────
    async def is_logged_in(self) -> bool:
        """Logged-out JobRight shows 'SIGN IN' / 'JOIN NOW' in the header and
        redirects /jobs/recommend → homepage. Logged-in keeps /jobs/recommend."""
        page = self.browser.page
        try:
            signin = await page.query_selector(
                'button:has-text("SIGN IN"), a:has-text("SIGN IN"), button:has-text("JOIN NOW")'
            )
            return not (signin and await signin.is_visible())
        except Exception:
            return False

    async def ensure_logged_in(self) -> bool:
        await self.google.load(self.browser.context)
        await SessionStore.load(self.browser.context, None, settings.jobright_session_file)

        await self.browser.goto("https://jobright.ai/")
        if await self.is_logged_in():
            log.success("Already logged in to JobRight.")
            return True

        log.info("Not logged in — signing in to JobRight with Google...")
        # Open the login modal (renders the GSI Google button).
        for sel in ('button:has-text("SIGN IN")', 'a:has-text("SIGN IN")'):
            el = await self.browser.page.query_selector(sel)
            if el and await el.is_visible():
                await el.click(timeout=5000)
                break
        try:
            await self.browser.page.wait_for_selector(GOOGLE_BUTTON, timeout=8000)
        except Exception:
            log.warning("GSI Google button did not appear in the login modal.")
        await human.think(settings.min_action_delay, settings.max_action_delay)
        await self._click_google_and_auth()

        await human.think(3, 6)
        await self.browser.goto("https://jobright.ai/")
        if await self.is_logged_in():
            await SessionStore.save(
                self.browser.context, self.browser.page,
                settings.jobright_session_file, domains=JOBRIGHT_DOMAINS,
            )
            await self.google.save(self.browser.context, self.browser.page)
            log.success("Logged in to JobRight via Google.")
            return True

        await self.browser.screenshot("screenshots/jobright_login_result.png")
        log.warning("JobRight login did not complete (see screenshots/jobright_login_result.png).")
        return False

    async def _click_google_and_auth(self) -> None:
        """JobRight's Google Identity Services button is in POPUP mode: clicking it
        opens a Google account-chooser popup (accounts.google.com) — NOT a FedCM
        dialog. We catch that popup and drive it (select our account + consent),
        since the Google session is already loaded."""
        page = self.browser.page
        context = self.browser.context

        popup_holder: dict = {}
        context.on("page", lambda p: popup_holder.setdefault("p", p))

        try:
            btn = page.frame_locator(GOOGLE_BUTTON).locator('div[role="button"], [role="button"], button').first
            await btn.click(timeout=8000)
            log.info("Clicked GSI Google button; waiting for account-chooser popup...")
        except Exception as e:
            log.warning("GSI button click failed: {}", e)

        popup = None
        for _ in range(12):
            if popup_holder.get("p"):
                popup = popup_holder["p"]
                break
            await asyncio.sleep(1)
        if popup is None:
            log.warning("Google account-chooser popup did not open.")
            return

        log.info("OAuth popup opened: {}", (popup.url or "")[:90])
        await self._drive_popup(popup)
        try:
            await self.google.save(context, page)
        except Exception:
            pass
        await human.think(4, 7)

    async def _drive_popup(self, popup) -> None:
        """Drive the Google OAuth popup to completion: choose our account, enter
        the password if the account is signed out, approve consent, wait for it to
        close. (The account chooser shows it as 'Signed out' in this isolated
        profile, so a password step is expected.)"""
        t = popup
        email = settings.google_email
        pwd = settings.google_password
        await human.think(2, 3)

        # 1) Account chooser — pick our account.
        for sel in (
            f'[data-identifier="{email}"]',
            f'div[role="link"][aria-label*="{email}"]',
            f'li:has-text("{email}")',
            f'div[role="link"]:has-text("{email}")',
        ):
            try:
                el = await t.wait_for_selector(sel, timeout=4000, state="visible")
                if el:
                    await el.click()
                    log.info("popup: chose account {}", email)
                    await human.think(2, 4)
                    break
            except Exception:
                continue

        # 2) Password page (the account is signed out).
        if pwd:
            try:
                pw = await t.wait_for_selector('input[type="password"]', timeout=7000, state="visible")
                await pw.click()
                await pw.fill(pwd)
                await human.think(0.5, 1.2)
                nxt = await t.wait_for_selector('#passwordNext button, #passwordNext, button:has-text("Next")', timeout=6000)
                await nxt.click()
                log.info("popup: entered password")
                await human.think(4, 7)
            except Exception:
                pass  # no password step (SSO'd straight through)

        # 3) Consent / "Continue to jobright.ai".
        for sel in ('button:has-text("Continue")', 'button:has-text("Allow")', '#submit_approve_access'):
            try:
                el = await t.wait_for_selector(sel, timeout=5000, state="visible")
                if el:
                    await el.click()
                    log.info("popup: approved consent")
                    await human.think(2, 4)
                    break
            except Exception:
                continue

        # 4) Wait for the popup to close (OAuth handshake finished).
        for _ in range(20):
            if popup.is_closed():
                break
            await asyncio.sleep(1)

    # ── scrape ──────────────────────────────────────────────────────────────
    async def scrape(self) -> list[ScrapedJob]:
        try:
            await self.ensure_logged_in()
        except Exception as exc:
            log.warning("JobRight login step failed, continuing: {}", exc)

        items = await self._fetch_recommendations(settings.max_jobs)
        log.info("JobRight: collected {} recommended job(s)", len(items))

        existing = self.repo.existing_keys(self.site)
        jobs: list[ScrapedJob] = []
        skipped = 0
        for item in items:
            if len(jobs) >= settings.max_jobs:
                break
            jr = (item or {}).get("jobResult") or {}
            jid = jr.get("jobId")
            if not jid:
                continue
            if jid in existing:
                skipped += 1
                continue
            job = self._to_job(item)
            self.save(job)  # persist immediately, one by one
            jobs.append(job)
        log.info("JobRight: {} new job(s) saved, {} already stored.", len(jobs), skipped)
        return jobs

    async def _fetch_recommendations(self, want: int) -> list:
        """Load the recommend feed: intercept the page's OWN authenticated XHR for
        the first page (a raw call returns empty — the SPA adds auth headers), then
        page for more by replaying those exact headers."""
        page = self.browser.page
        collected: dict = {}
        headers_box: dict = {}

        def _is_feed(u: str) -> bool:
            # Exactly the recommendation feed (not siblings like .../jobs/liked,
            # which return job-id strings rather than job objects).
            return u.split("?")[0].endswith("/recommend/list/jobs")

        def _result_items(res):
            # `result` is a dict wrapping the job list (e.g. {"jobList": [...]}).
            if isinstance(res, list):
                return res
            if isinstance(res, dict):
                for k in ("jobList", "jobs", "list", "items", "data"):
                    if isinstance(res.get(k), list):
                        return res[k]
                for v in res.values():
                    if isinstance(v, list):
                        return v
            return []

        def _add(items) -> int:
            n = 0
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                jid = ((it.get("jobResult") or {}).get("jobId"))
                if jid and jid not in collected:
                    collected[jid] = it
                    n += 1
            return n

        def _on_req(req):
            if _is_feed(req.url) and "hdrs" not in headers_box:
                try:
                    headers_box["hdrs"] = dict(req.headers)
                except Exception:
                    pass

        async def _collect(resp):
            try:
                data = await resp.json()
            except Exception:
                return
            if isinstance(data, dict):
                _add(_result_items(data.get("result")))

        def _on_resp(resp):
            if _is_feed(resp.url):
                asyncio.create_task(_collect(resp))

        page.on("request", _on_req)
        page.on("response", _on_resp)
        try:
            await self.browser.goto(settings.jobright_recommend_url)
            await human.think(settings.min_page_delay, settings.max_page_delay)
            await human.human_scroll(page, steps=4)
            for _ in range(24):  # wait for the feed XHR to land
                if collected:
                    break
                await asyncio.sleep(0.5)

            # Paginate by replaying the page's captured headers (cookies auto-attach).
            headers = {
                k: v
                for k, v in (headers_box.get("hdrs") or {}).items()
                if k.lower() not in ("host", "content-length", "connection", "accept-encoding", "content-type")
            }
            pos = len(collected)
            while len(collected) < want:
                url = (
                    f"{settings.jobright_recommend_api}"
                    f"?refresh=false&sortCondition=0&position={pos}&count=20&syncRerank=false"
                )
                try:
                    resp = await page.request.get(url, headers=headers, timeout=25000)
                    data = await resp.json()
                except Exception as exc:
                    log.warning("JobRight pagination failed at position {}: {}", pos, exc)
                    break
                batch = _result_items(data.get("result") if isinstance(data, dict) else None)
                if not batch:
                    break
                added = _add(batch)
                pos += len(batch)
                if added == 0 or len(batch) < 20:
                    break
                await human.think(1.0, 2.0)
        finally:
            try:
                page.remove_listener("request", _on_req)
                page.remove_listener("response", _on_resp)
            except Exception:
                pass
        return list(collected.values())[:want]

    def _to_job(self, item: dict) -> ScrapedJob:
        """Map one JobRight recommendation (jobResult + companyResult) to a ScrapedJob."""
        jr = item.get("jobResult") or {}
        cr = item.get("companyResult") or {}
        jid = jr.get("jobId")
        apply_url = jr.get("applyLink") or jr.get("originalUrl") or None
        return ScrapedJob(
            site_job_id=jid,
            title=(jr.get("jobTitle") or "(no title)"),
            description=self._build_description(jr, item),
            # Source link = the JobRight job detail page; apply_url = employer ATS.
            link=f"https://jobright.ai/jobs/info/{jid}",
            company=cr.get("companyName") or None,
            company_url=cr.get("companyURL") or None,
            job_type=jr.get("employmentType") or None,
            remote=bool(jr.get("isRemote")),
            location=jr.get("jobLocation") or None,
            salary=extract_salary(jr),
            posted_at=self._parse_publish_time(jr.get("publishTime")),
            apply_url=apply_url,
        )

    @staticmethod
    def _build_description(jr: dict, item: dict) -> str:
        """Compose a description from the summary + core responsibilities + match."""
        parts: list[str] = []
        summary = (jr.get("jobSummary") or "").strip()
        if summary:
            parts.append(summary)
        resp = jr.get("coreResponsibilities") or []
        if resp:
            parts.append("Key Responsibilities:\n" + "\n".join(f"- {r}" for r in resp))
        rank, score = item.get("rankDesc"), item.get("displayScore")
        if rank and score is not None:
            try:
                parts.append(f"JobRight match: {rank} ({round(float(score))}%)")
            except Exception:
                pass
        return "\n\n".join(parts)

    @staticmethod
    def _parse_publish_time(s):
        """JobRight's publishTime is 'YYYY-MM-DD HH:MM:SS' in UTC (naive)."""
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
        return None


async def main() -> None:
    await JobRightScraper().run()


if __name__ == "__main__":
    asyncio.run(main())
