"""Sign in to Himalayas with Google, in the profile the resolver uses.

Why: Cloudflare is solvable (see tools/resolve_himalayas_apply.py), but past it
Himalayas still hides the employer's apply URL from logged-out visitors — every
"Apply now" is `/signup/talent?...` and the JSON-LD says `"directApply": false`.
A signed-in session is the only remaining way to find out whether the real
employer link is ever exposed.

Runs against a GENUINE Chrome attached over CDP (same `--real-chrome` approach
the resolver uses), so the session lands in the very profile the resolver will
reuse, alongside its cf_clearance cookie.

    python tools/himalayas_login.py                 # sign in, then probe one job
    python tools/himalayas_login.py --keep-open     # leave Chrome up afterwards

After it reports SIGNED IN, run the resolver against the same profile:

    python tools/resolve_himalayas_apply.py --real-chrome --limit 5 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from scraper.browser import clear_challenge  # noqa: E402

_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

LOGIN_URL = "https://himalayas.app/login"


def _chrome_exe() -> str:
    import os
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    for p in _CHROME_PATHS:
        if Path(p).exists():
            return p
    raise SystemExit("Chrome not found — set CHROME_PATH")


def _launch(profile: str, port: int):
    Path(profile).mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [_chrome_exe(), f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--start-maximized", "--no-first-run", "--no-default-browser-check"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1) as r:
                print("attached to:", json.loads(r.read().decode()).get("Browser"))
            return proc, endpoint
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit(f"Chrome did not open a debugging port on {port} "
                     "(close any Chrome already using this profile).")


async def _google_button(page):
    """The site's 'Continue with Google' control, whatever shape it takes."""
    for sel in ('button:has-text("Google")', 'a:has-text("Google")',
                '[class*="google" i]', 'button:has-text("Continue with Google")'):
        try:
            el = await page.wait_for_selector(sel, timeout=4000, state="visible")
            if el:
                return el, sel
        except Exception:
            continue
    return None, None


async def _signed_in(page) -> bool:
    """Signed in => the site stops offering Login/Sign up."""
    try:
        await page.goto("https://himalayas.app/", wait_until="domcontentloaded", timeout=45000)
        await clear_challenge(page, max_wait_s=90)
        await asyncio.sleep(2.5)
        txt = await page.eval_on_selector_all(
            "a", "els => els.map(e => (e.getAttribute('href')||''))")
        return not any(re.search(r"/(login|signup)\b", h) for h in txt)
    except Exception:
        return False


async def go(args):
    from patchright.async_api import async_playwright

    proc, endpoint = _launch(str(Path(args.chrome_profile).resolve()), args.cdp_port)
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(endpoint)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    page.set_default_timeout(60000)
    try:
        print("opening", LOGIN_URL)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        if not await clear_challenge(page, max_wait_s=args.cf_wait):
            print("RESULT: Cloudflare not cleared on the login page — rerun, or solve it "
                  "in the window yourself and re-run with --skip-cf")
            if not args.manual:
                return
        await asyncio.sleep(2)
        print("login page title:", (await page.title())[:70])

        btn, sel = await _google_button(page)
        if not btn:
            print("RESULT: no 'Continue with Google' control found on the login page.")
            print("        (solve it manually in the window; --keep-open leaves it up)")
            if args.manual:
                print(f"\n  >>> Sign in yourself in the Chrome window ({args.manual}s) <<<")
                for _ in range(args.manual // 3):
                    await asyncio.sleep(3)
                    if await _signed_in(page):
                        print("  detected signed-in — continuing")
                        break
        else:
            print(f"clicking Google button ({sel})")
            from scraper.auth.google_auth import GoogleAuthService
            ok = await GoogleAuthService().complete_site_oauth(ctx, page, sel, timeout=90000)
            print("oauth round-trip reported:", ok)
            await asyncio.sleep(3)

        signed = await _signed_in(page)
        print("-" * 60)
        print("RESULT:", "SIGNED IN" if signed else "NOT signed in")

        # Probe one job page for the employer link now that we may be signed in.
        if args.probe:
            print("\nprobing a job page for the employer apply URL...")
            await page.goto(args.probe, wait_until="domcontentloaded", timeout=60000)
            await clear_challenge(page, max_wait_s=args.cf_wait)
            await asyncio.sleep(3)
            anchors = await page.eval_on_selector_all(
                "a", "els => els.map(e => ({t:(e.textContent||'').trim().slice(0,26), "
                     "h:e.getAttribute('href')||''}))")
            apply_ish = [a for a in anchors if re.search(r"\bapply\b", a["t"], re.I)]
            print("  Apply controls:")
            for a in apply_ish[:6]:
                print(f"    {a['t']!r:26} -> {a['h'][:80]}")
            btns = await page.eval_on_selector_all(
                "button", "els => els.filter(e=>/apply/i.test(e.textContent||''))"
                          ".map(e=>(e.textContent||'').trim().slice(0,30))")
            print("  Apply buttons (js):", btns[:5])
            gated = bool(apply_ish) and all(
                re.search(r"/(signup|login|signin)\b", a["h"]) for a in apply_ish)
            print("  ->", "STILL LOGIN-GATED" if gated
                  else "employer link may now be reachable — run the resolver")
    finally:
        try:
            await pw.stop()
        except Exception:
            pass
        if proc and not args.keep_open:
            try:
                proc.terminate()
            except Exception:
                pass
        elif args.keep_open:
            print("\n(Chrome left running — close it before the next --real-chrome run)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome-profile", default="sessions/himalayas-chrome")
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--cf-wait", type=int, default=90)
    ap.add_argument("--manual", type=int, nargs="?", const=180, default=0, metavar="SECONDS",
                    help="pause and let YOU sign in / solve the challenge in the window")
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--probe", default="https://himalayas.app/companies/ypo/jobs/full-stack-engineer",
                    help="job page to inspect after signing in ('' to skip)")
    asyncio.run(go(ap.parse_args()))


if __name__ == "__main__":
    main()
