"""Standalone Cloudflare check for Himalayas job pages.

Run this on ANY server to find out whether that machine's IP can pass the
Cloudflare Turnstile on a Himalayas job page (and therefore reach the real
employer "Apply" link). Self-contained — does NOT import the JobHighLander
project, so you can copy just this one file.

SETUP (on the test server):
    pip install patchright curl_cffi
    patchright install chrome        # installs a real Chrome for patchright

RUN:
    python check_cf_himalayas.py                       # direct (server IP), headed
    python check_cf_himalayas.py --headless            # no visible window
    python check_cf_himalayas.py --proxy http://user:pass@host:port
    python check_cf_himalayas.py --url https://himalayas.app/companies/.../jobs/...

WHAT IT REPORTS:
    • whether the CF challenge cleared (PASS/FAIL) and how long it took
    • the real page <title> as proof it's past the wall
    • every external / apply-looking link found on the page (the goal)

If it prints "PASS" and shows an external apply link, that server can be used to
resolve real Himalayas apply URLs. If it prints "FAIL", that IP is CF-walled too.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from urllib.parse import urlparse

# ── Cloudflare interstitial detection (title/body hints) ─────────────────────
_TITLE_HINTS = (
    "just a moment", "attention required", "checking your browser",
    "verifying you are human", "loading",
)


async def _cf_bbox(page):
    """Bounding box of the largest visible challenges.cloudflare.com iframe."""
    best = None
    try:
        for frame in page.frames:
            if not frame.url.startswith("https://challenges.cloudflare.com"):
                continue
            try:
                box = await (await frame.frame_element()).bounding_box()
            except Exception:
                continue
            if not box or box["width"] < 50 or box["height"] < 20:
                continue
            if best is None or box["width"] * box["height"] > best["width"] * best["height"]:
                best = box
    except Exception:
        pass
    return best


async def _is_challenged(page):
    try:
        title = (await page.title()) or ""
    except Exception:
        title = ""
    box = await _cf_bbox(page)
    if box is not None:
        return True, title, box
    if any(h in title.lower() for h in _TITLE_HINTS):
        return True, title, None
    try:
        body = ((await page.inner_text("body")) or "")[:3000].lower()
        if any(h in body for h in ("verify you are human", "needs to review the security", "ray id")):
            return True, title, None
    except Exception:
        pass
    return False, title, None


async def clear_and_check(page, max_wait_s: int, max_clicks: int = 4):
    """Wait out / click the Turnstile, then confirm REAL content loaded."""
    start = time.monotonic()
    clicks, last_click = 0, -1e9
    while time.monotonic() - start < max_wait_s:
        elapsed = time.monotonic() - start
        challenged, title, box = await _is_challenged(page)
        if not challenged:
            # Confirm it's genuinely the job page (has an <h1>), not a transient
            # reload where the iframe momentarily vanished.
            try:
                h1 = await page.eval_on_selector_all("h1", "els => els.length")
            except Exception:
                h1 = 0
            if h1:
                return True, round(elapsed, 1), title
        if box and elapsed >= 6 and clicks < max_clicks and elapsed - last_click >= 12:
            try:
                cx = box["x"] + box["width"] / 9
                cy = box["y"] + box["height"] / 2
                await page.mouse.move(cx, cy, steps=15)
                await asyncio.sleep(0.4)
                await page.mouse.click(cx, cy)
                clicks += 1
                last_click = elapsed
                print(f"  clicked Turnstile ({clicks}/{max_clicks}) at ~{elapsed:.0f}s")
            except Exception as e:
                print("  turnstile click failed:", e)
        await asyncio.sleep(2)
    return False, round(time.monotonic() - start, 1), (await page.title() if page else "")


def _pick_job_url() -> str:
    """Grab one live job-page URL from the public (unwalled) API."""
    import json
    from curl_cffi import requests
    s = requests.Session(impersonate="chrome")
    d = json.loads(s.get("https://himalayas.app/jobs/api?offset=0&limit=1", timeout=45).text)
    return d["jobs"][0]["applicationLink"]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="specific Himalayas job page (default: newest from the API)")
    ap.add_argument("--proxy", help="upstream proxy http://user:pass@host:port (Chrome points at it directly)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait", type=int, default=90, help="max seconds to wait for CF (default 90)")
    args = ap.parse_args()

    from patchright.async_api import async_playwright

    url = args.url or _pick_job_url()
    print("JOB PAGE:", url)
    print("proxy   :", args.proxy or "NONE (direct — this server's IP)")
    print("mode    :", "headless" if args.headless else "headed")
    print("-" * 60)

    launch = dict(
        channel="chrome",
        headless=args.headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--start-maximized", "--disable-quic",
            "--no-first-run", "--no-default-browser-check",
        ],
        no_viewport=True,
        locale="en-US",
        user_data_dir="./cf-check-profile",  # persistent so a cleared CF cookie survives reruns
    )
    if args.proxy:
        u = urlparse(args.proxy)
        launch["proxy"] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            launch["proxy"]["username"] = u.username
        if u.password:
            launch["proxy"]["password"] = u.password

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(**launch)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    page.set_default_timeout(60000)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        ok, secs, title = await clear_and_check(page, args.wait)
        print("-" * 60)
        print(f"RESULT: {'PASS ✅' if ok else 'FAIL ❌'}  (after {secs}s)")
        print("title :", title[:70])
        if ok:
            anchors = await page.eval_on_selector_all(
                "a",
                "els => els.map(e => ({t:(e.textContent||'').trim().slice(0,30), h:e.href}))",
            )
            ext = [a for a in anchors if a["h"].startswith("http") and "himalayas.app" not in a["h"]]
            apply_ish = [a for a in anchors if "apply" in (a["t"] + " " + a["h"]).lower()]
            print("\nEXTERNAL links on page:")
            for a in ext[:15]:
                print("   ", a["t"], "->", a["h"])
            print("\nAPPLY-looking links:")
            for a in apply_ish[:15]:
                print("   ", a["t"], "->", a["h"])
            if not ext and not apply_ish:
                print("   (none found — inspect the page manually; the apply button may be a JS handler)")
    finally:
        await ctx.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
