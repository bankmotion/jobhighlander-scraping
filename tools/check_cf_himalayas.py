"""Standalone Cloudflare check for Himalayas job pages.

Uses the one shared implementation in scraper/browser.py (`clear_challenge`), so
this probe can't drift from what the scrapers actually do — it previously carried
its own weaker copy that detected "cleared" by page TITLE only and clicked at
`width / 9`, which lands just PAST the Turnstile checkbox, and so it reported
FAIL on machines that in fact pass.

Run on ANY machine to find out whether its IP can clear the challenge on a
Himalayas job page. Note two things the answer depends on:
  • HEADED + FOREGROUND. Turnstile ignores clicks while the window is unfocused,
    and headless Chrome can't pass an interactive challenge at all.
  • Repeated failures compound — each one makes Cloudflare stricter with your IP
    for a while, so don't judge a machine by a burst of back-to-back attempts.

A PASS here still doesn't mean the employer's apply URL is reachable: past
Cloudflare, Himalayas gates "Apply now" behind /signup (JSON-LD
`"directApply": false`). See tools/resolve_himalayas_apply.py.

SETUP:
    pip install -r requirements.txt
    patchright install chrome

RUN:
    python tools/check_cf_himalayas.py                   # direct (this IP), headed
    python tools/check_cf_himalayas.py --proxy http://user:pass@host:port
    python tools/check_cf_himalayas.py --url https://himalayas.app/companies/.../jobs/...

Prints PASS/FAIL, the real page <title>, and every external/apply link found.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.browser import _CHECKPOINT_TITLE_HINTS as _HINTS  # noqa: E402
from scraper.browser import clear_challenge, is_challenged  # noqa: E402


def _pick_job_url():
    from curl_cffi import requests as creq
    s = creq.Session(impersonate="chrome")
    d = json.loads(s.get("https://himalayas.app/jobs/api?offset=0&limit=1", timeout=45).text)
    return d["jobs"][0]["applicationLink"]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="specific Himalayas job page (default: newest from API)")
    ap.add_argument("--proxy", help="upstream proxy http://user:pass@host:port")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait", type=int, default=100, help="max seconds to wait for CF (default 100)")
    a = ap.parse_args()

    from patchright.async_api import async_playwright

    url = a.url or _pick_job_url()
    print("JOB PAGE:", url)
    print("proxy   :", a.proxy or "NONE (direct - this server's IP)")
    print("mode    :", "headless" if a.headless else "headed")
    print("-" * 60)

    if a.headless:
        print("WARNING: headless can't pass an interactive Cloudflare challenge — "
              "expect FAIL regardless of your IP.")

    pw = await async_playwright().start()
    # patchright's documented config: real Chrome, a PERSISTENT profile, no
    # synthetic viewport, no custom UA/headers, and as few extra args as possible
    # (it already sets --disable-blink-features=AutomationControlled itself).
    launch = dict(
        user_data_dir=str(Path("sessions/himalayas-cfcheck-profile").resolve()),
        channel="chrome", headless=a.headless, no_viewport=True, locale="en-US",
        chromium_sandbox=True,  # else Playwright appends the detectable --no-sandbox
        args=["--start-maximized"],
    )
    if a.proxy:
        u = urlparse(a.proxy)
        launch["proxy"] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            launch["proxy"]["username"] = u.username
        if u.password:
            launch["proxy"]["password"] = u.password

    ctx = await pw.chromium.launch_persistent_context(**launch)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    page.set_default_timeout(60000)
    page.set_default_navigation_timeout(60000)
    try:
        t0 = time.monotonic()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        ok = await clear_challenge(page, max_wait_s=a.wait)
        # Settle, then confirm we reached the REAL job page. Can't just count
        # <h1>s — Cloudflare's interstitial has one too ("himalayas.app").
        real = False
        for _ in range(4):
            await asyncio.sleep(2.5)
            title = (await page.title()) or ""
            challenged, _, _ = await is_challenged(page)
            h1s = await page.eval_on_selector_all(
                "h1", "els => els.map(e => (e.textContent || '').trim())")
            if ok and not challenged and any(
                t and t.lower() not in ("himalayas.app", "himalayas") for t in h1s
            ):
                real = True
                break
        secs = round(time.monotonic() - t0, 1)
        print("-" * 60)
        print(f"RESULT: {'PASS' if real else 'FAIL'}  (after {secs}s)")
        print("title :", ((await page.title()) or "")[:70])
        if not real:
            print("\nA FAIL here does NOT prove your IP is blocked. This probe uses a")
            print("PLAYWRIGHT-LAUNCHED Chrome, which is the weakest configuration — measured")
            print("on the same machine and IP, a genuinely-launched Chrome attached over CDP")
            print("cleared the same page in ~4s while this failed. Try:")
            print("    python tools/resolve_himalayas_apply.py --real-chrome --manual --limit 1")
            print("Also: keep the window in the FOREGROUND, and give Cloudflare ~30 min to")
            print("cool off if you've just burned several attempts.")
        if real:
            anchors = await page.eval_on_selector_all(
                "a", "els => els.map(e => ({t:(e.textContent||'').trim().slice(0,30), h:e.href}))")
            ext = [x for x in anchors if x["h"].startswith("http") and "himalayas.app" not in x["h"]]
            apply_ish = [x for x in anchors if "apply" in (x["t"] + " " + x["h"]).lower()]
            print("\nEXTERNAL links on page:")
            for x in ext[:15]:
                print("   ", x["t"], "->", x["h"])
            print("\nAPPLY-looking links:")
            for x in apply_ish[:15]:
                print("   ", x["t"], "->", x["h"])
            if all(re.search(r"/(signup|login|signin)\b", x["h"]) for x in apply_ish) and apply_ish:
                print("\n   NOTE: every Apply link points at Himalayas' own signup — the")
                print("   employer URL is login-gated, so PASS here still won't yield it.")
            elif not ext and not apply_ish:
                print("   (none found — the apply button may be a JS handler; inspect manually)")
    finally:
        for closer in (page.close, ctx.close, pw.stop):
            try:
                await closer()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
