"""Google sign-in for the sites that gate data behind an account.

Called by the scrapers themselves — there is no separate "login tool" to run by
hand. A scraper asks for a session; if the DB doesn't have a usable one, this
signs in with GOOGLE_EMAIL/GOOGLE_PASSWORD and stores the cookies in the
`scraper_sessions` table (never a local file — the Chrome profile dir is scratch
space that can be deleted at any time).

Sites handled:
  • himalayas      — needed for the employer apply URL (logged out, "Apply now"
                     is just /signup/talent).
  • dice           — the Apply button is login-gated in the UI, and signing in
                     is the journey a real visitor takes.
  • weworkremotely — unlocks account-gated apply buttons (Toptal et al.). NOTE the
                     big-name postings sit behind WWR's PAID plan
                     (/job-seekers/onboarding/step_3?context=paywall), which no
                     amount of signing in unlocks.

Uses a genuine Chrome driven over CDP rather than a Playwright-launched one: it
carries no automation launch flags, which is what actually gets through
Cloudflare (see scraper/browser.py `clear_challenge`).
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from config import settings
from logger import log
from scraper.browser import clear_challenge
from scraper.session import SessionStore

_BASE = Path(__file__).resolve().parent.parent.parent
_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

#: site -> (login page, google oauth entry, signed-in probe url, logged-out marker)
SITES = {
    "himalayas": {
        "login": "https://himalayas.app/login",
        "oauth": None,  # a button on the page, not a direct endpoint
        "probe": "https://himalayas.app/",
        "logged_out": r"/(login|signup)\b",
        "domain": "himalayas.app",
    },
    "dice": {
        "login": "https://www.dice.com/dashboard/login",
        "oauth": None,  # a "Continue with Google" button, not a direct endpoint
        "probe": "https://www.dice.com/dashboard/jobs",
        # Dice keeps a "Login" link in the header of EVERY page, signed in or
        # not, so the anchor scan the other sites use always reads logged-out
        # here. Where the probe lands is the honest signal: signed in,
        # /dashboard/jobs resolves to /my-jobs; signed out it bounces to
        # /dashboard/login.
        "logged_out_url": r"/dashboard/login",
        "domain": "dice.com",
    },
    "weworkremotely": {
        "login": "https://weworkremotely.com/job-seekers/account/login",
        "oauth": "https://weworkremotely.com/job-seekers/account/auth/google_oauth2",
        "probe": "https://weworkremotely.com/",
        "logged_out": r"/account/sign_in",
        "domain": "weworkremotely.com",
    },
}


def session_file(site: str) -> str:
    """Path form only — SessionStore keys the DB row off the stem."""
    return str(_BASE / "sessions" / f"{site}_session.json")


def load_cookie_jar(site: str) -> dict:
    """{name: value} for curl_cffi, straight from the DB. {} when absent."""
    try:
        import pymysql
        conn = pymysql.connect(
            host=settings.db_host, port=settings.db_port, user=settings.db_user,
            password=settings.db_password, database=settings.db_name, charset="utf8mb4")
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT cookies FROM scraper_sessions WHERE site = %s", (site,))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return {}
        domain = SITES.get(site, {}).get("domain", "")
        return {c["name"]: c["value"] for c in json.loads(row[0]).get("cookies", [])
                if domain in (c.get("domain") or "")}
    except Exception as e:
        log.warning("[{}] could not read session from DB: {}", site, e)
        return {}


def _chrome_exe() -> str:
    import os
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    for p in _CHROME_PATHS:
        if Path(p).exists():
            return p
    raise RuntimeError("Chrome not found — set CHROME_PATH")


def kill_chrome_tree(proc) -> None:
    """Tree-kill: terminate() orphans every renderer and leaks memory."""
    if proc is None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        else:
            proc.terminate()
    except Exception:
        pass


def _cdp_alive(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1) as r:
            json.loads(r.read().decode())
        return True
    except Exception:
        return False


def _kill_stale_chrome(profile: str) -> None:
    """Kill any Chrome still holding this profile.

    A run killed by a timeout (or a crash) leaves Chrome alive, and Chrome
    IGNORES --remote-debugging-port when another instance already owns the
    profile — so the next run would fail with 'did not open a debugging port'
    and lose the whole pass."""
    name = Path(profile).name
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                 f"Where-Object {{ $_.CommandLine -like '*{name}*' }} | "
                 "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            subprocess.run(["pkill", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        time.sleep(2)
    except Exception:
        pass


def launch_chrome(profile: str, port: int, proxy_server: Optional[str] = None):
    """Real Chrome + CDP. `proxy_server` must already be credential-free (use the
    local relay for authenticated upstreams — Chrome can't send proxy auth).

    Self-healing: reuses an already-listening CDP endpoint, and clears a stale
    Chrome that would otherwise silently swallow --remote-debugging-port.
    """
    endpoint = f"http://127.0.0.1:{port}"
    if _cdp_alive(endpoint):
        log.info("reusing the Chrome already listening on {}", endpoint)
        return None, endpoint  # proc=None -> we didn't start it, so we won't kill it
    _kill_stale_chrome(profile)
    Path(profile).mkdir(parents=True, exist_ok=True)
    cmd = [_chrome_exe(), f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
           "--start-maximized", "--no-first-run", "--no-default-browser-check"]
    if proxy_server:
        cmd += [f"--proxy-server={proxy_server}", "--disable-quic"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1) as r:
                log.info("attached to {}", json.loads(r.read().decode()).get("Browser"))
            return proc, endpoint
        except Exception:
            time.sleep(0.5)
    kill_chrome_tree(proc)
    raise RuntimeError(f"Chrome did not open a debugging port on {port} "
                       "(close any Chrome already using this profile)")


async def is_signed_in(page, site: str) -> bool:
    """Whether `probe` renders as a signed-in page.

    Two ways to tell, per site. `logged_out_url` compares where the probe
    ACTUALLY LANDED, for sites that bounce an anonymous visitor to their login
    page; `logged_out` scans the anchors for a sign-in link. The URL test comes
    first because it is the stronger signal — a site that keeps a "Login" link in
    its header for signed-in users too (Dice) defeats the anchor scan entirely.
    """
    cfg = SITES[site]
    try:
        await page.goto(cfg["probe"], wait_until="domcontentloaded", timeout=45000)
        await clear_challenge(page, max_wait_s=90)
        await asyncio.sleep(2.5)
        if cfg.get("logged_out_url"):
            return not re.search(cfg["logged_out_url"], page.url or "")
        hrefs = await page.eval_on_selector_all("a", "els => els.map(e => e.getAttribute('href') || '')")
        return not any(re.search(cfg["logged_out"], h or "") for h in hrefs)
    except Exception:
        return False


async def _drive_google(page) -> None:
    """Fill Google's chooser / email / password / consent, whichever appears."""
    async def _visible(sel):
        try:
            el = await page.query_selector(sel)
            return el if el and await el.is_visible() else None
        except Exception:
            return None

    for _ in range(15):  # wait for the hop to Google
        await asyncio.sleep(1)
        if "accounts.google.com" in page.url:
            break
    done_email = done_pwd = False
    for _ in range(30):
        await asyncio.sleep(2)
        if "accounts.google.com" not in page.url:
            break
        try:
            picked = await _visible(f'[data-identifier="{settings.google_email}"]')
            if picked:
                await picked.click()
                continue
            # Google's box is #identifierId / name=identifier — NOT type=email.
            email = await _visible('#identifierId, input[name="identifier"], input[type="email"]')
            if email and not done_email:
                await email.fill(settings.google_email)
                await asyncio.sleep(1)
                nxt = await page.query_selector('#identifierNext button, #identifierNext, button:has-text("Next")')
                if nxt:
                    await nxt.click()
                done_email = True
                continue
            # A HIDDEN password field is pre-rendered, so check visibility.
            pwd = await _visible('input[type="password"][name="Passwd"], input[type="password"]')
            if pwd and not done_pwd:
                await pwd.fill(settings.google_password)
                await asyncio.sleep(1)
                nxt = await page.query_selector('#passwordNext button, #passwordNext, button:has-text("Next")')
                if nxt:
                    await nxt.click()
                done_pwd = True
                continue
            cont = await _visible('button:has-text("Continue"), button:has-text("Allow")')
            if cont:
                await cont.click()
                continue
        except Exception:
            pass
    await asyncio.sleep(3)


async def sign_in(site: str, ctx, page) -> bool:
    """Run the site's Google OAuth in an existing context; save cookies to the DB."""
    cfg = SITES[site]
    if await is_signed_in(page, site):
        log.info("[{}] already signed in", site)
        return True
    if not (settings.google_email and settings.google_password):
        log.warning("[{}] no GOOGLE_EMAIL/PASSWORD — cannot sign in", site)
        return False

    log.info("[{}] signing in with Google...", site)
    await page.goto(cfg["login"], wait_until="domcontentloaded", timeout=60000)
    await clear_challenge(page, max_wait_s=90)
    await asyncio.sleep(2)

    btn = None
    for sel in ('a:has-text("Continue with Google")', 'button:has-text("Continue with Google")',
                'button:has-text("Google")', '[class*="google" i]'):
        try:
            btn = await page.wait_for_selector(sel, timeout=4000, state="visible")
            if btn:
                break
        except Exception:
            continue
    if btn:
        await btn.click()
    elif cfg["oauth"]:
        await page.goto(cfg["oauth"], wait_until="domcontentloaded", timeout=60000)
    else:
        log.warning("[{}] no Google button found on the login page", site)
        return False

    await _drive_google(page)
    signed = await is_signed_in(page, site)
    if signed:
        try:
            await SessionStore.save(ctx, page, session_file(site), domains=(cfg["domain"],))
            log.info("[{}] signed in — session saved to DB", site)
        except Exception as e:
            log.warning("[{}] could not save session: {}", site, e)
    else:
        log.warning("[{}] sign-in did not complete", site)
    return signed


async def ensure_session(site: str, force: bool = False) -> dict:
    """Guarantee a usable session for `site`, signing in only if needed.

    Returns the cookie jar ({} on failure). Opens its own Chrome, so callers
    don't need a browser of their own.
    """
    if not force:
        jar = load_cookie_jar(site)
        if jar:
            log.info("[{}] using saved session from DB ({} cookies)", site, len(jar))
            return jar

    from patchright.async_api import async_playwright
    profile = str(_BASE / "sessions" / f"{site}-login-chrome")
    proc = relay = None
    pw = None
    try:
        server = None
        if settings.proxy_url:
            from scraper.local_proxy import LocalRoutingProxy
            direct = settings.proxy_bypass.split(",") if settings.proxy_bypass else []
            relay = LocalRoutingProxy(settings.proxy_url, direct)
            server = f"http://127.0.0.1:{await relay.start()}"
        proc, endpoint = launch_chrome(profile, 9222, server)
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(endpoint)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(60000)
        try:  # seed whatever we already have (may only need a refresh)
            await SessionStore.load(ctx, None, session_file(site))
        except Exception:
            pass
        await sign_in(site, ctx, page)
    except Exception as e:
        log.warning("[{}] login failed: {}", site, e)
    finally:
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
        kill_chrome_tree(proc)
        if relay:
            try:
                await relay.stop()
            except Exception:
                pass
    return load_cookie_jar(site)
