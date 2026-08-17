"""Standalone Cloudflare check for Himalayas job pages.

Mirrors the CF-clearing logic from the stake.com-crawl project (box_ev/browser.py)
that is known to pass Cloudflare on a clean IP: be PATIENT (long grace period so
the managed challenge auto-clears), click the Turnstile AT MOST ONCE, and detect
"cleared" by page TITLE only — aggressive/rapid clicking resets a sticky Turnstile
and loops forever.

Run on ANY server to find out whether that machine's IP can pass the challenge on
a Himalayas job page (and therefore reach the real employer "Apply" link).

SETUP:
    pip install patchright curl_cffi
    patchright install chrome

RUN:
    python check_cf_himalayas.py                         # direct (this server IP), headed
    python check_cf_himalayas.py --headless
    python check_cf_himalayas.py --proxy http://user:pass@host:port
    python check_cf_himalayas.py --url https://himalayas.app/companies/.../jobs/...

Prints PASS/FAIL, the real page <title>, and every external/apply link found.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from urllib.parse import urlparse

# Title hints that mean we're still on a bot-checkpoint interstitial (stake's set).
_HINTS = (
    "security checkpoint", "just a moment", "attention required",
    "checking your browser", "verifying you are human",
)


async def _cf_bbox(page):
    """Bounding box of a challenges.cloudflare.com Turnstile iframe, or None."""
    try:
        for frame in page.frames:
            if not frame.url.startswith("https://challenges.cloudflare.com"):
                continue
            try:
                bbox = await (await frame.frame_element()).bounding_box()
                if bbox is not None:
                    return bbox
            except Exception:
                continue
    except Exception:
        pass
    return None


async def clear_checkpoint(page, max_wait_s=100, grace_s=28, max_clicks=1, click_gap_s=15):
    """stake.com-crawl's proven approach: patient grace, single click, title-only
    detection. Returns True once the title is no longer a checkpoint page."""
    title = ""
    elapsed = 0
    clicks = 0
    last_click = -10_000
    while elapsed < max_wait_s:
        try:
            title = (await page.title()) or ""
        except Exception:
            title = ""
        if not any(h in title.lower() for h in _HINTS):
            if elapsed:
                print(f"  checkpoint cleared after ~{elapsed}s (title={title[:44]!r})")
            return True
        bbox = await _cf_bbox(page)
        if bbox and elapsed >= grace_s and clicks < max_clicks and elapsed - last_click >= click_gap_s:
            try:
                await page.mouse.click(bbox["x"] + bbox["width"] / 9, bbox["y"] + bbox["height"] / 2)
                clicks += 1
                last_click = elapsed
                print(f"  clicked Turnstile ({clicks}/{max_clicks}) at ~{elapsed}s (after {grace_s}s grace)")
            except Exception:
                pass
        await asyncio.sleep(2)
        elapsed += 2
    print(f"  NOT cleared after {max_wait_s}s; last title={title[:44]!r}")
    return False


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

    pw = await async_playwright().start()
    # Match stake's setup: real Chrome, minimal args, a FIXED viewport, fresh context.
    launch = dict(channel="chrome", headless=a.headless,
                  args=["--disable-blink-features=AutomationControlled"])
    if a.proxy:
        u = urlparse(a.proxy)
        launch["proxy"] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            launch["proxy"]["username"] = u.username
        if u.password:
            launch["proxy"]["password"] = u.password

    browser = await pw.chromium.launch(**launch)
    ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US")
    page = await ctx.new_page()
    page.set_default_timeout(60000)
    page.set_default_navigation_timeout(60000)
    try:
        t0 = time.monotonic()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        ok = await clear_checkpoint(page, max_wait_s=a.wait)
        # Settle, then confirm we reached the REAL job page (has an <h1>).
        real = False
        for _ in range(4):
            await asyncio.sleep(2.5)
            title = (await page.title()) or ""
            h1 = await page.eval_on_selector_all("h1", "els => els.length")
            if ok and h1 and not any(h in title.lower() for h in _HINTS):
                real = True
                break
        secs = round(time.monotonic() - t0, 1)
        print("-" * 60)
        print(f"RESULT: {'PASS' if real else 'FAIL'}  (after {secs}s)")
        print("title :", ((await page.title()) or "")[:70])
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
            if not ext and not apply_ish:
                print("   (none found — the apply button may be a JS handler; inspect manually)")
    finally:
        for closer in (page.close, ctx.close, browser.close, pw.stop):
            try:
                await closer()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
