"""Test: solve Himalayas' Cloudflare Managed Challenge via CapSolver, then fetch
the real job page and look for the employer apply URL.

Himalayas serves a Cloudflare Managed Challenge interstitial (cf-mitigated:
challenge, no standalone Turnstile sitekey). CapSolver's AntiCloudflareTask
solves that kind of page and returns a `cf_clearance` cookie + the user-agent it
solved with. That cookie is bound to (IP + user-agent), so we MUST fetch the page
through the SAME proxy CapSolver used, with the SAME user-agent.

WHAT YOU NEED:
  • A CapSolver account + a few dollars of balance (managed-challenge solves cost
    ~$0.02-0.05 each). Get the API key from https://dashboard.capsolver.com
  • A proxy CapSolver can use (we pass the IPRoyal US residential proxy).

RUN (from job-seeking/, venv active):
    set CAPSOLVER_KEY=CAP-xxxxxxxx           # Windows cmd
    python tools/solve_cf_capsolver.py
    # or: python tools/solve_cf_capsolver.py --key CAP-xxxx --proxy http://user:pass@host:port

It prints: whether the solve succeeded, whether the page then loaded (200 vs 403),
and any external/apply links found in the HTML or __NEXT_DATA__ — which also tells
us whether the real employer URL is even present on the page.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

from curl_cffi import requests

CAPSOLVER_BASE = "https://api.capsolver.com"


def _proxy_to_capsolver(proxy_url: str) -> str:
    """IPRoyal 'http://user:pass@host:port' -> CapSolver 'host:port:user:pass'."""
    u = urlparse(proxy_url)
    return f"{u.hostname}:{u.port}:{u.username}:{u.password}"


def solve(api_key: str, url: str, proxy_url: str) -> dict:
    s = requests.Session(impersonate="chrome")
    payload = {
        "clientKey": api_key,
        "task": {
            "type": "AntiCloudflareTask",
            "websiteURL": url,
            "proxy": _proxy_to_capsolver(proxy_url),
        },
    }
    r = s.post(f"{CAPSOLVER_BASE}/createTask", json=payload, timeout=45)
    j = r.json()
    if j.get("errorId"):
        raise RuntimeError(f"createTask error: {j.get('errorCode')} / {j.get('errorDescription')}")
    task_id = j["taskId"]
    print(f"  task created: {task_id} — polling…")
    for i in range(60):  # up to ~120s
        time.sleep(2)
        r = s.post(f"{CAPSOLVER_BASE}/getTaskResult",
                   json={"clientKey": api_key, "taskId": task_id}, timeout=45)
        j = r.json()
        if j.get("errorId"):
            raise RuntimeError(f"getTaskResult error: {j.get('errorCode')} / {j.get('errorDescription')}")
        if j.get("status") == "ready":
            print(f"  solved in ~{(i + 1) * 2}s")
            return j["solution"]
        if i % 5 == 0:
            print(f"    …still solving ({(i + 1) * 2}s)")
    raise TimeoutError("CapSolver did not return a solution in time")


def fetch_with_clearance(url: str, solution: dict, proxy_url: str) -> tuple[int, str]:
    # CapSolver returns cookies as a dict OR a cf_clearance string, plus userAgent.
    ua = solution.get("userAgent") or solution.get("user_agent")
    cookies = solution.get("cookies")
    cf = None
    if isinstance(cookies, dict):
        cf = cookies.get("cf_clearance")
    cf = cf or solution.get("cf_clearance") or solution.get("token")
    if not cf:
        raise RuntimeError(f"no cf_clearance in solution keys={list(solution.keys())}")
    headers = {"User-Agent": ua} if ua else {}
    jar = {"cf_clearance": cf}
    s = requests.Session()  # NOTE: no impersonate — must match the solved UA exactly
    r = s.get(url, headers=headers, cookies=jar, timeout=45,
              proxies={"http": proxy_url, "https": proxy_url}, allow_redirects=True)
    return r.status_code, r.text


def find_apply(html: str) -> None:
    ext = sorted(set(re.findall(r'https?://(?!himalayas\.app|challenges\.cloudflare)[^\s"\'<>\\]{10,90}', html)))
    print(f"\n  external URLs found: {len(ext)}")
    for u in ext[:20]:
        print("   ", u)
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', html, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            blob = json.dumps(data)
            for key in ("applyUrl", "applicationUrl", "externalUrl", "url", "applyLink", "externalApplyUrl"):
                hits = re.findall(rf'"{key}":"([^"]{{8,120}})"', blob)
                if hits:
                    print(f"  __NEXT_DATA__ {key}:", hits[:5])
        except Exception as e:
            print("  __NEXT_DATA__ parse error:", e)
    else:
        print("  (no __NEXT_DATA__ script found)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("CAPSOLVER_KEY"))
    ap.add_argument("--proxy")
    ap.add_argument("--url")
    a = ap.parse_args()
    if not a.key:
        sys.exit("Provide a CapSolver key via --key or the CAPSOLVER_KEY env var.")

    proxy = a.proxy
    if not proxy:
        # pull the IPRoyal proxy from the DB-backed settings
        sys.path.insert(0, os.getcwd())
        from config import settings, sync_settings_from_db
        sync_settings_from_db()
        proxy = settings.proxy_url
    if not proxy:
        sys.exit("No proxy available — pass --proxy (managed-challenge solves need one).")

    url = a.url or json.loads(
        requests.Session(impersonate="chrome").get(
            "https://himalayas.app/jobs/api?limit=1", timeout=45).text)["jobs"][0]["applicationLink"]

    print("URL   :", url)
    print("proxy :", urlparse(proxy).hostname)
    print("-" * 60)
    print("[1] solving Cloudflare via CapSolver…")
    sol = solve(a.key, url, proxy)
    print("[2] fetching page with cf_clearance…")
    status, html = fetch_with_clearance(url, sol, proxy)
    print("-" * 60)
    print(f"RESULT: page returned {status} ({'PASS' if status == 200 else 'still blocked'}), {len(html)} bytes")
    if status == 200:
        find_apply(html)


if __name__ == "__main__":
    main()
