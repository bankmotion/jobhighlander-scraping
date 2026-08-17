"""Resolve real employer apply URLs for Himalayas jobs, and write them back.

Himalayas stores `apply_url` = the Himalayas job page, because the real employer
"Apply" link only lives ON that page — which is behind a Cloudflare managed
challenge that this datacenter server can't clear. Run THIS script from a machine
whose IP clears the challenge (a trusted residential IP auto-clears it; or point
Chrome at a CF-solving proxy via --proxy). It opens each job page, extracts the
real apply URL, and updates the row.

It pays the Cloudflare cost ONCE: the first page solve leaves a cf_clearance
cookie in the persistent profile, and every later job page in the same run reuses
it (no re-challenge).

Two connectivity modes — because the MariaDB lives on the server:
  • DB mode (default): read himalayas rows straight from the DB, update in place.
    Run where the DB is reachable (e.g. on the server, or with a tunnel).
        python tools/resolve_himalayas_apply.py --limit 20
        python tools/resolve_himalayas_apply.py --dry-run          # show, don't write
  • File mode: resolve on one machine, import on another.
        # on the server (DB reachable):
        python tools/resolve_himalayas_apply.py --export pending.json --limit 50
        # on your local (CF-clearing) machine:
        python tools/resolve_himalayas_apply.py --from-file pending.json --to-file resolved.json
        # back on the server:
        python tools/resolve_himalayas_apply.py --import-file resolved.json

Common flags: --limit N, --dry-run, --headless, --proxy URL, --profile DIR.
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

_HINTS = ("just a moment", "attention required", "checking your browser",
          "verifying you are human", "security checkpoint")

# Hosts that appear on a job page but are never the employer apply link.
_JUNK_HOST_RE = re.compile(
    r"(himalayas\.app|challenges\.cloudflare|cloudflare\.com|google\.|facebook\.|twitter\.|x\.com|"
    r"linkedin\.com/(company|feed)|instagram\.|youtube\.|t\.me|discord\.|apple\.com|play\.google)",
    re.I,
)


# ── DB helpers (lazy pymysql; creds from the project config) ─────────────────
def _connect():
    import pymysql
    sys.path.insert(0, str(Path.cwd()))
    from config import settings
    return pymysql.connect(
        host=settings.db_host, port=settings.db_port, user=settings.db_user,
        password=settings.db_password, database=settings.db_name,
        charset="utf8mb4", autocommit=True,
    )


def fetch_pending(limit: int) -> list[dict]:
    """himalayas rows whose apply_url is still the Himalayas page."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, apply_url FROM jobs "
                "WHERE site='himalayas' AND (apply_url LIKE '%%himalayas.app%%' OR apply_url IS NULL) "
                "ORDER BY id DESC" + (" LIMIT %s" if limit else ""),
                ((limit,) if limit else ()),
            )
            return [{"id": r[0], "url": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def write_updates(rows: list[dict]) -> int:
    """rows: [{id, apply_url}]. Updates jobs.apply_url. Returns count changed."""
    if not rows:
        return 0
    conn = _connect()
    n = 0
    try:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    "UPDATE jobs SET apply_url=%s, updated_at=UTC_TIMESTAMP(3) "
                    "WHERE id=%s AND site='himalayas'",
                    (r["apply_url"], r["id"]),
                )
                n += cur.rowcount
    finally:
        conn.close()
    return n


# ── Cloudflare clear (patient: grace, single click, title detection) ─────────
async def _cf_bbox(page):
    try:
        for frame in page.frames:
            if not frame.url.startswith("https://challenges.cloudflare.com"):
                continue
            try:
                b = await (await frame.frame_element()).bounding_box()
                if b:
                    return b
            except Exception:
                continue
    except Exception:
        pass
    return None


async def clear_cf(page, max_wait_s=100, grace_s=20, max_clicks=1, click_gap_s=15) -> bool:
    elapsed, clicks, last = 0, 0, -10_000
    while elapsed < max_wait_s:
        try:
            title = (await page.title()) or ""
        except Exception:
            title = ""
        if not any(h in title.lower() for h in _HINTS):
            return True
        box = await _cf_bbox(page)
        if box and elapsed >= grace_s and clicks < max_clicks and elapsed - last >= click_gap_s:
            try:
                await page.mouse.click(box["x"] + box["width"] / 9, box["y"] + box["height"] / 2)
                clicks += 1
                last = elapsed
            except Exception:
                pass
        await asyncio.sleep(2)
        elapsed += 2
    return False


# ── apply-URL extraction ─────────────────────────────────────────────────────
async def _external_from_next_data(page) -> str | None:
    try:
        html = await page.content()
    except Exception:
        return None
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', html, re.S)
    if not m:
        return None
    blob = m.group(1)
    for key in ("applyUrl", "applicationUrl", "externalUrl", "applyLink",
                "externalApplyUrl", "applicationLink", "url"):
        for hit in re.findall(rf'"{key}":"([^"]{{8,200}})"', blob):
            u = hit.replace("\\u002F", "/").replace("\\/", "/")
            if u.startswith("http") and not _JUNK_HOST_RE.search(u):
                return u
    return None


async def _click_capture(page, ctx, handle) -> str | None:
    """Click an apply control; capture the external URL it opens (popup or nav)."""
    before = page.url
    try:  # popup / new tab
        async with ctx.expect_page(timeout=6000) as pinfo:
            await handle.click(timeout=4000)
        popup = await pinfo.value
        try:
            await popup.wait_for_load_state("commit", timeout=12000)
        except Exception:
            pass
        url = popup.url
        try:
            await popup.close()
        except Exception:
            pass
        if url and url.startswith("http") and not _JUNK_HOST_RE.search(url):
            return url
    except Exception:
        pass
    # same-tab navigation / redirect
    try:
        if page.url != before and page.url.startswith("http") and not _JUNK_HOST_RE.search(page.url):
            return page.url
    except Exception:
        pass
    return None


async def extract_apply(page, ctx) -> str | None:
    # 1) an "Apply"-labelled anchor pointing straight off-site
    anchors = await page.eval_on_selector_all(
        "a", "els => els.map(e => ({t:(e.textContent||'').trim(), h:e.href, rel:e.rel||''}))")
    apply_anchors = [a for a in anchors if re.search(r"\bapply\b", (a["t"] + " " + a["rel"]), re.I)]
    for a in apply_anchors:
        if a["h"].startswith("http") and not _JUNK_HOST_RE.search(a["h"]):
            return a["h"]
    # 2) __NEXT_DATA__ embedded URL
    nxt = await _external_from_next_data(page)
    if nxt:
        return nxt
    # 3) click an Apply control and capture where it goes
    for sel in ("a:has-text('Apply')", "button:has-text('Apply')",
                "a:has-text('apply')", "[href*='apply']"):
        try:
            handle = await page.query_selector(sel)
        except Exception:
            handle = None
        if handle:
            got = await _click_capture(page, ctx, handle)
            if got:
                return got
            # a click may have navigated the page; get it back for the next try
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=15000)
                await clear_cf(page, max_wait_s=40)
            except Exception:
                pass
    return None


# ── run ──────────────────────────────────────────────────────────────────────
async def resolve_all(jobs: list[dict], args) -> list[dict]:
    from patchright.async_api import async_playwright

    launch = dict(
        user_data_dir=args.profile, channel="chrome", headless=args.headless,
        no_viewport=True, locale="en-US",
        args=["--disable-blink-features=AutomationControlled", "--start-maximized",
              "--disable-quic", "--no-first-run", "--no-default-browser-check"],
    )
    if args.proxy:
        u = urlparse(args.proxy)
        launch["proxy"] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            launch["proxy"]["username"] = u.username
        if u.password:
            launch["proxy"]["password"] = u.password

    resolved: list[dict] = []
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(**launch)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    page.set_default_timeout(60000)
    try:
        for i, job in enumerate(jobs, 1):
            url = job["url"]
            t0 = time.monotonic()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                cleared = await clear_cf(page, max_wait_s=args.cf_wait)
                if not cleared:
                    print(f"[{i}/{len(jobs)}] id={job['id']} CF NOT cleared — skipping")
                    continue
                # let the real content settle
                for _ in range(4):
                    await asyncio.sleep(2)
                    if await page.eval_on_selector_all("h1", "e=>e.length"):
                        break
                apply_url = await extract_apply(page, ctx)
                took = round(time.monotonic() - t0, 1)
                if apply_url:
                    resolved.append({"id": job["id"], "apply_url": apply_url})
                    print(f"[{i}/{len(jobs)}] id={job['id']} OK ({took}s) -> {apply_url[:70]}")
                else:
                    print(f"[{i}/{len(jobs)}] id={job['id']} no external apply URL found ({took}s)")
            except Exception as e:
                print(f"[{i}/{len(jobs)}] id={job['id']} error: {str(e)[:80]}")
    finally:
        for closer in (ctx.close, pw.stop):
            try:
                await closer()
            except Exception:
                pass
    return resolved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max jobs (0 = all pending)")
    ap.add_argument("--dry-run", action="store_true", help="resolve but don't write to DB")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--proxy", help="upstream proxy http://user:pass@host:port")
    ap.add_argument("--profile", default="sessions/himalayas-resolve-profile",
                    help="persistent Chrome profile dir (keeps the cf_clearance cookie)")
    ap.add_argument("--cf-wait", type=int, default=100, help="max seconds to wait for CF per page")
    ap.add_argument("--from-file", help="resolve URLs from this JSON [{id,url}] instead of the DB")
    ap.add_argument("--to-file", help="write resolved [{id,apply_url}] here instead of the DB")
    ap.add_argument("--export", help="DB -> write pending [{id,url}] to this file, then exit")
    ap.add_argument("--import-file", help="write resolved [{id,apply_url}] from this file to the DB, then exit")
    args = ap.parse_args()

    # import-only / export-only shortcuts
    if args.import_file:
        rows = json.loads(Path(args.import_file).read_text(encoding="utf-8"))
        print(f"updated {write_updates(rows)} row(s) in the DB")
        return
    if args.export:
        jobs = fetch_pending(args.limit)
        Path(args.export).write_text(json.dumps(jobs, indent=2), encoding="utf-8")
        print(f"exported {len(jobs)} pending job(s) -> {args.export}")
        return

    # gather jobs
    if args.from_file:
        jobs = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    else:
        jobs = fetch_pending(args.limit)
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"{len(jobs)} Himalayas job(s) to resolve"
          f"{' (dry-run)' if args.dry_run else ''}"
          f"{' via ' + urlparse(args.proxy).hostname if args.proxy else ' (direct)'}")
    if not jobs:
        return

    resolved = asyncio.run(resolve_all(jobs, args))
    print("-" * 60)
    print(f"resolved {len(resolved)}/{len(jobs)} apply URLs")

    if args.to_file:
        Path(args.to_file).write_text(json.dumps(resolved, indent=2), encoding="utf-8")
        print(f"wrote -> {args.to_file}")
    elif args.dry_run:
        for r in resolved:
            print(f"  would set id={r['id']} -> {r['apply_url']}")
    else:
        print(f"updated {write_updates(resolved)} row(s) in the DB")


if __name__ == "__main__":
    main()
