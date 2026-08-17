"""Test: solve Himalayas' Cloudflare Managed Challenge with 2Captcha (Turnstile
challenge-page mode), in a real browser, then read the job page for apply links.

Himalayas serves a Cloudflare Managed Challenge interstitial (no standalone
sitekey). The working approach:
  1. Launch a real browser and inject a hook over `window.turnstile.render` that
     captures the challenge params (sitekey, action, cData, chlPageData) the
     Cloudflare JS passes when it mounts the invisible Turnstile.
  2. Send those params to 2Captcha (method=turnstile) -> get a real solved token.
  3. Call the captured Turnstile callback with the token; Cloudflare validates it
     and issues cf_clearance, so the page proceeds to the real content.

A genuine solved token can validate where a blind checkbox-click cannot — which
is exactly why this page resisted the click.

RUN (job-seeking/, venv active). Key is read from --key, TWOCAPTCHA_API_KEY, or
the stake project's .env:
    python tools/solve_cf_2captcha.py                 # direct (this server IP), headed
    python tools/solve_cf_2captcha.py --proxy http://user:pass@host:port
    python tools/solve_cf_2captcha.py --headless --url https://himalayas.app/companies/.../jobs/...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from urllib.parse import urlparse

from curl_cffi import requests

_STAKE_ENV = r"C:/Users/Administrator/Documents/tolmany/stake.com-crawl/.env"

# Hook installed BEFORE any page script: overrides turnstile.render to grab the
# challenge params and the success callback, without rendering the real widget.
_HOOK = """
(() => {
  window.__cf = null; window.__cfcb = null;
  const iv = setInterval(() => {
    if (window.turnstile && window.turnstile.render) {
      clearInterval(iv);
      window.turnstile.render = (container, params) => {
        try {
          window.__cf = {
            sitekey: params.sitekey,
            pageurl: location.href,
            action: params.action || '',
            data: params.cData || '',
            pagedata: params.chlPageData || '',
            userAgent: navigator.userAgent
          };
          window.__cfcb = params.callback;
        } catch (e) {}
        return 'hook-widget-id';
      };
    }
  }, 20);
})();
"""

_HINTS = ("just a moment", "attention required", "checking your browser",
          "verifying you are human", "security checkpoint")


def _read_key() -> str | None:
    k = os.environ.get("TWOCAPTCHA_API_KEY")
    if k:
        return k
    try:
        with open(_STAKE_ENV, encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = re.match(r"\s*TWOCAPTCHA_API_KEY\s*=\s*(\S+)", line)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return None


def solve_turnstile(key: str, p: dict) -> str:
    data = {"key": key, "method": "turnstile", "sitekey": p["sitekey"],
            "pageurl": p["pageurl"], "json": 1}
    if p.get("action"):
        data["action"] = p["action"]
    if p.get("data"):
        data["data"] = p["data"]
    if p.get("pagedata"):
        data["pagedata"] = p["pagedata"]
    if p.get("userAgent"):
        data["useragent"] = p["userAgent"]
    r = requests.post("https://2captcha.com/in.php", data=data, timeout=30).json()
    if r.get("status") != 1:
        raise RuntimeError(f"in.php: {r}")
    cid = r["request"]
    print(f"  2captcha job {cid} — solving…")
    for i in range(40):  # up to ~200s
        time.sleep(5)
        rr = requests.get(f"https://2captcha.com/res.php?key={key}&action=get&id={cid}&json=1", timeout=30).json()
        if rr.get("status") == 1:
            print(f"  token received (~{(i + 1) * 5}s)")
            return rr["request"]
        if rr.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"res.php: {rr}")
    raise TimeoutError("2captcha did not solve in time")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=_read_key())
    ap.add_argument("--proxy")
    ap.add_argument("--url")
    ap.add_argument("--headless", action="store_true")
    a = ap.parse_args()
    if not a.key:
        raise SystemExit("No 2Captcha key (pass --key or set TWOCAPTCHA_API_KEY).")

    from patchright.async_api import async_playwright

    url = a.url or json.loads(requests.Session(impersonate="chrome").get(
        "https://himalayas.app/jobs/api?limit=1", timeout=45).text)["jobs"][0]["applicationLink"]
    print("URL   :", url)
    print("proxy :", (urlparse(a.proxy).hostname if a.proxy else "DIRECT (this server IP)"))
    print("-" * 60)

    pw = await async_playwright().start()
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
    await ctx.add_init_script(_HOOK)
    page = await ctx.new_page()
    page.set_default_timeout(60000)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print("[1] waiting for the Turnstile hook to capture challenge params…")
        params = None
        for _ in range(45):
            params = await page.evaluate("() => window.__cf")
            if params and params.get("sitekey"):
                break
            await asyncio.sleep(1)
        if not params or not params.get("sitekey"):
            print("-" * 60)
            print("RESULT: FAIL — hook never captured Turnstile params")
            print("        (title:", ((await page.title()) or "")[:50], ")")
            return
        print(f"  captured sitekey={params['sitekey']}  action={params.get('action')!r}  "
              f"data?={bool(params.get('data'))} pagedata?={bool(params.get('pagedata'))}")
        print("[2] solving via 2Captcha…")
        token = solve_turnstile(a.key, params)
        print("[3] injecting token into the Turnstile callback…")
        await page.evaluate("(t) => { if (window.__cfcb) window.__cfcb(t); }", token)
        # wait for CF to accept + page to proceed
        real = False
        for _ in range(30):
            await asyncio.sleep(2)
            title = (await page.title()) or ""
            h1 = await page.eval_on_selector_all("h1", "els => els.length")
            if h1 and not any(h in title.lower() for h in _HINTS):
                real = True
                break
        print("-" * 60)
        print(f"RESULT: {'PASS' if real else 'FAIL'} | title: {((await page.title()) or '')[:60]}")
        if real:
            html = await page.content()
            anchors = await page.eval_on_selector_all(
                "a", "els => els.map(e => ({t:(e.textContent||'').trim().slice(0,30), h:e.href}))")
            ext = [x for x in anchors if x["h"].startswith("http") and "himalayas.app" not in x["h"]]
            apply_ish = [x for x in anchors if "apply" in (x["t"] + " " + x["h"]).lower()]
            print("\nEXTERNAL links:")
            for x in ext[:15]:
                print("   ", x["t"], "->", x["h"])
            print("APPLY-looking links/buttons:")
            for x in apply_ish[:15]:
                print("   ", x["t"], "->", x["h"])
            # also probe __NEXT_DATA__ for an embedded apply url
            m = re.search(r'__NEXT_DATA__[^>]*>(\{.*?\})</script>', html, re.S)
            if m:
                for k in ("applyUrl", "applicationUrl", "externalUrl", "applyLink", "externalApplyUrl"):
                    hits = re.findall(rf'"{k}":"([^"]{{8,120}})"', m.group(1))
                    if hits:
                        print(f"  __NEXT_DATA__ {k}:", hits[:4])
    finally:
        for closer in (page.close, ctx.close, browser.close, pw.stop):
            try:
                await closer()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
