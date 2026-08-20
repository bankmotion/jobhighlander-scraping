"""Diagnose why the JobRight recommend feed comes back empty.

    venv/Scripts/python.exe scripts/jobright_probe.py

Read-only: navigates, records every /swan/ and /recommend/ call the page makes,
and reports what the feed actually returned. Writes nothing to the database.

The scraper logs "Already logged in" and then "collected 0 recommended job(s)",
which are consistent with three very different causes — a changed endpoint path,
a session that looks valid but is not, or a feed that is genuinely empty. This
tells them apart.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from logger import log
from scraper.browser import StealthBrowser
from scraper.session import SessionStore
from scraper.auth.google_auth import GoogleAuthService


async def main() -> None:
    calls: list[dict] = []
    sample: list = []

    browser = StealthBrowser(
        user_data_dir=settings.jobright_user_data_dir,
        proxy_url=settings.jobright_proxy_url or settings.proxy_url,
    )
    await browser.start()
    page = browser.page

    def interesting(u: str) -> bool:
        return "/swan/" in u or "recommend" in u

    def on_request(req):
        if interesting(req.url):
            calls.append({"kind": "req", "method": req.method, "url": req.url})

    async def read(resp):
        entry = {"kind": "resp", "status": resp.status, "url": resp.url, "shape": None}
        try:
            data = await resp.json()
            if isinstance(data, dict):
                result = data.get("result")
                entry["keys"] = sorted(data.keys())[:8]
                if isinstance(result, dict):
                    entry["shape"] = {k: (len(v) if isinstance(v, list) else type(v).__name__)
                                      for k, v in list(result.items())[:8]}
                    if not sample and resp.url.split("?")[0].endswith("/recommend/list/jobs"):
                        for k in ("jobList", "jobs", "list", "items", "data"):
                            if isinstance(result.get(k), list) and result[k]:
                                sample.extend(result[k])
                                break
                elif isinstance(result, list):
                    entry["shape"] = f"list[{len(result)}]"
                else:
                    entry["shape"] = type(result).__name__
                for k in ("message", "code", "status", "success"):
                    if k in data:
                        entry[k] = data[k]
        except Exception as e:
            entry["shape"] = f"<not json: {type(e).__name__}>"
        calls.append(entry)

    def on_response(resp):
        if interesting(resp.url):
            asyncio.create_task(read(resp))

    page.on("request", on_request)
    page.on("response", on_response)

    try:
        await GoogleAuthService().load(browser.context)
        await SessionStore.load(browser.context, None, settings.jobright_session_file)

        await browser.goto("https://jobright.ai/")
        await asyncio.sleep(3)
        signin = await page.query_selector(
            'button:has-text("SIGN IN"), a:has-text("SIGN IN"), button:has-text("JOIN NOW")'
        )
        print(f"\nhome url            : {page.url}")
        print(f"SIGN IN visible     : {bool(signin and await signin.is_visible())}"
              f"   (is_logged_in would say {not (signin and await signin.is_visible())})")

        await browser.goto(settings.jobright_recommend_url)
        await asyncio.sleep(12)
        # A logged-out session is bounced off /jobs/recommend. The scraper's
        # is_logged_in never checks this, so it cannot tell the difference.
        print(f"recommend url after : {page.url}")
        print(f"redirected away     : {'/jobs/recommend' not in page.url}")

        body = (await page.inner_text("body"))[:700].replace("\n", " | ")
        print(f"\nvisible text        : {body}\n")

        await page.screenshot(path="screenshots/jobright_probe.png", full_page=False)
        print("screenshot          : screenshots/jobright_probe.png")

        print(f"\n--- {len(calls)} swan/recommend call(s) ---")
        for c in calls:
            if c["kind"] == "req":
                print(f"  REQ  {c['method']:5} {c['url'][:120]}")
            else:
                extra = {k: v for k, v in c.items() if k not in ("kind", "status", "url")}
                print(f"  RESP {c['status']}   {c['url'][:110]}")
                print(f"        {json.dumps(extra, default=str)[:300]}")

        feed = [c for c in calls if c["kind"] == "resp"
                and c["url"].split("?")[0].endswith("/recommend/list/jobs")]
        print(f"\ncalls matching the scraper's _is_feed test: {len(feed)}")
        if not feed:
            print("  -> the endpoint the scraper listens for was never called.")

        # The decisive check. The scraper keys every item on
        # item["jobResult"]["jobId"]; if that path moved, the feed can return 20
        # jobs and the scraper still collects zero.
        if sample:
            item = sample[0]
            print("\n--- shape of one feed item ---")
            print("  top-level keys :", sorted(item.keys())[:20])
            jr = item.get("jobResult")
            print("  has jobResult  :", isinstance(jr, dict))
            if isinstance(jr, dict):
                print("  jobResult keys :", sorted(jr.keys())[:20])
                print("  jobResult.jobId:", jr.get("jobId"))
            usable = sum(1 for it in sample if isinstance(it, dict)
                         and ((it.get("jobResult") or {}).get("jobId")))
            print(f"  scraper would collect: {usable} of {len(sample)}")
        else:
            print("\n(no feed items captured to inspect)")
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
