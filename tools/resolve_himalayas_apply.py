"""Resolve real employer apply URLs for Himalayas jobs, and write them back.

Himalayas stores `apply_url` = the Himalayas job page. Two separate walls stand
between us and the employer's own apply URL, and it's worth keeping them apart:

  1. CLOUDFLARE, on every job page (`cf-mitigated: challenge`, so plain HTTP gets
     403 and only a real browser can get through). This one is SOLVED — see the
     Cloudflare notes below.
  2. A LOGIN WALL behind it. Even on a fully rendered job page, an anonymous
     visitor never sees the employer link: every "Apply now" button points at
     `/signup/talent?redirect=...&showModal=true`, and the page's own JSON-LD
     declares `"directApply": false`. No amount of CF-clearing reveals it —
     resolving these needs a SIGNED-IN Himalayas session in --profile.

So on a logged-out run expect "LOGIN-GATED" for most jobs. That is not a failure
of this script, and the stored Himalayas job page remains a working apply
destination (the visitor just signs up on Himalayas to continue).

The Cloudflare cost is usually paid ONCE per run: the first solve leaves a
cf_clearance cookie in the persistent profile, and later job pages in the same
session normally sail through un-challenged.

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

CLOUDFLARE — what actually works (measured, not guessed):
  • Run HEADED and keep the window in the FOREGROUND. Turnstile ignores clicks
    while `document.hasFocus()` is false, so an unfocused/headless window sits on
    the checkbox until it times out. `clear_challenge()` calls bring_to_front().
  • `--real-chrome` attaches to a Chrome we start ourselves, so the browser
    carries no automation launch flags at all and its profile ages like a human's:
        python tools/resolve_himalayas_apply.py --real-chrome --manual --limit 20
  • `--manual` lets you click the checkbox yourself when automation can't; the run
    then continues on its own.
  • Failed challenges compound — each one makes Cloudflare stricter with your IP,
    so the run aborts after --max-cf-failures (3) instead of digging the hole
    deeper. If you get blocked, wait ~30 min or switch IP/--proxy.

Common flags: --limit N, --dry-run, --proxy URL, --profile DIR, --real-chrome,
--manual [SECONDS], --keep-open, --max-cf-failures N.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The one shared, fixed Cloudflare routine (focus + correct checkbox offset).
from scraper.browser import clear_challenge, is_challenged  # noqa: E402

# Hosts that appear on a job page but are never the employer apply link.
_JUNK_HOST_RE = re.compile(
    r"(himalayas\.app|challenges\.cloudflare|cloudflare\.com|google\.|facebook\.|twitter\.|x\.com|"
    r"linkedin\.com/(company|feed)|instagram\.|youtube\.|t\.me|discord\.|apple\.com|play\.google)",
    re.I,
)


def _is_employer_url(url: str) -> bool:
    """True if `url` looks like a real employer/ATS link.

    Match on scheme+host+path ONLY, never the query string: Himalayas appends
    `?utm_source=himalayas.app&utm_medium=himalayas.app` to the destination, so
    testing the whole URL rejected every genuine employer link it ever found.
    """
    if not url or not url.startswith("http"):
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    if not p.hostname:
        return False
    return not _JUNK_HOST_RE.search(f"{p.scheme}://{p.netloc}{p.path}")


def _off_himalayas(url: str) -> bool:
    """Host-based check — again, the utm_* params carry 'himalayas.app'."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return bool(host) and not host.endswith("himalayas.app")


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
            # Prefix match, NOT '%himalayas.app%': a RESOLVED row's employer URL
            # still carries `utm_source=himalayas.app`, so a substring match would
            # drag every already-done job back into the queue.
            cur.execute(
                "SELECT id, apply_url FROM jobs "
                "WHERE site='himalayas' AND (apply_url LIKE 'https://himalayas.app/%%' "
                "                            OR apply_url IS NULL OR apply_url = '') "
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
            if _is_employer_url(u):
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
        if _is_employer_url(url):
            return url
    except Exception:
        pass
    # same-tab navigation / redirect
    try:
        if page.url != before and _is_employer_url(page.url):
            return page.url
    except Exception:
        pass
    return None


async def _is_login_gated(page) -> bool:
    """True if the page's own Apply control just points at Himalayas' signup.

    Himalayas does not expose the employer link to anonymous visitors: every
    "Apply now" button is `/signup/talent?redirect=...&showModal=true`, and the
    JSON-LD advertises `"directApply": false`. Worth reporting distinctly —
    it is a LOGIN wall, not a Cloudflare failure, and no amount of
    CF-clearing will reveal the URL."""
    try:
        hrefs = await page.eval_on_selector_all(
            "a", "els => els.filter(e => /\\bapply\\b/i.test(e.textContent || ''))"
                 "        .map(e => e.getAttribute('href') || '')")
    except Exception:
        return False
    return bool(hrefs) and all(re.search(r"/(signup|login|signin)\b", h) for h in hrefs)


async def _follow_apply_redirect(ctx, apply_url: str) -> str | None:
    """`himalayas.app/apply/<code>` 302s to the employer's own ATS. Follow it in a
    throwaway tab and keep the destination (Workday/Greenhouse/Lever/…)."""
    tab = await ctx.new_page()
    try:
        await tab.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
        for _ in range(8):  # the hop can take a moment to settle
            await asyncio.sleep(1.5)
            if _off_himalayas(tab.url):
                break
        final = tab.url
        return final if _is_employer_url(final) else None
    except Exception:
        return None
    finally:
        try:
            await tab.close()
        except Exception:
            pass


async def _apply_via_modal(page, ctx) -> str | None:
    """The signed-in flow, and the ONLY one that yields the employer URL.

    Clicking "Apply now" opens a cover-letter upsell modal; its "I'm ready to
    apply" link is `himalayas.app/apply/<code>`, which redirects to the employer's
    real application. The code appears nowhere in the served DOM — it only exists
    once the modal is opened — so this has to be driven, not scraped.
    """
    async def _modal_href() -> str:
        return await page.evaluate(
            """() => {
                const inModal = Array.from(document.querySelectorAll(
                    '[role=dialog] a, [class*=modal i] a, [class*=Modal] a'));
                const any = inModal.length ? inModal : Array.from(document.querySelectorAll('a'));
                const hit = any.find(e => /ready to apply/i.test(e.textContent || '')
                                       || /\\/apply\\//.test(e.getAttribute('href') || ''));
                return hit ? hit.href : '';
            }"""
        )

    # The page is React — a button that exists isn't necessarily wired yet, so a
    # too-early click silently does nothing. Retry across the visible buttons.
    for attempt in range(3):
        if await _modal_href():  # already open from a previous attempt
            break
        try:
            buttons = await page.query_selector_all("button:has-text('Apply')")
        except Exception:
            buttons = []
        clicked = False
        for b in buttons:
            try:
                if not await b.is_visible():
                    continue
                await b.click(timeout=6000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked and attempt == 2:
            return None
        for _ in range(5):  # wait for the modal to mount
            await asyncio.sleep(1.0)
            if await _modal_href():
                break
        if await _modal_href():
            break
        await asyncio.sleep(1.5)  # let hydration finish, then try again

    href = await _modal_href()
    return await _follow_apply_redirect(ctx, href) if href else None


async def extract_apply(page, ctx) -> str | None:
    # 1) an "Apply"-labelled anchor pointing straight off-site
    anchors = await page.eval_on_selector_all(
        "a", "els => els.map(e => ({t:(e.textContent||'').trim(), h:e.href, rel:e.rel||''}))")
    apply_anchors = [a for a in anchors if re.search(r"\bapply\b", (a["t"] + " " + a["rel"]), re.I)]
    for a in apply_anchors:
        if _is_employer_url(a["h"]):
            return a["h"]
    # 2) signed-in modal -> /apply/<code> -> employer ATS (the one that works)
    via_modal = await _apply_via_modal(page, ctx)
    if via_modal:
        return via_modal
    # 3) __NEXT_DATA__ embedded URL
    nxt = await _external_from_next_data(page)
    if nxt:
        return nxt
    # 4) click an Apply control and capture where it goes
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
                await clear_challenge(page, max_wait_s=60)
            except Exception:
                pass
    return None


async def _dump(page, job_id) -> None:
    """Save the page we couldn't extract from, so a miss is debuggable."""
    try:
        out = Path("sessions") / f"himalayas-unresolved-{job_id}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(await page.content(), encoding="utf-8")
        print(f"      (saved page -> {out})")
    except Exception:
        pass


async def _settle(page, tries: int = 6) -> None:
    """Wait for the REAL job page to render.

    Can't just wait for an <h1>: Cloudflare's own interstitial has one too
    ("himalayas.app"), so the old check returned instantly while still on the
    challenge. Wait for an <h1> that isn't the challenge's."""
    for _ in range(tries):
        await asyncio.sleep(1.5)
        try:
            texts = await page.eval_on_selector_all(
                "h1", "els => els.map(e => (e.textContent || '').trim())")
        except Exception:
            continue
        if any(t and t.lower() not in ("himalayas.app", "himalayas") for t in texts):
            return


# ── "real Chrome" mode: launch Chrome ourselves, then attach over CDP ────────
# Nothing about this browser is Playwright-launched, so it carries none of the
# automation launch flags (no --enable-automation, no --no-sandbox, not even
# patchright's --disable-blink-features=AutomationControlled banner). It is the
# same Chrome you use by hand, and its profile ages normally across runs, which
# is exactly what Cloudflare's reputation signals reward.
_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def _chrome_exe() -> str:
    import os
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    for p in _CHROME_PATHS:
        if Path(p).exists():
            return p
    raise SystemExit("Chrome not found — set CHROME_PATH=<path to chrome.exe>")


def _launch_real_chrome(args) -> tuple:
    """Start real Chrome with a debugging port; return (process, cdp_endpoint)."""
    import subprocess
    import urllib.request

    profile = str(Path(args.chrome_profile).resolve())
    Path(profile).mkdir(parents=True, exist_ok=True)
    cmd = [
        _chrome_exe(),
        f"--remote-debugging-port={args.cdp_port}",
        f"--user-data-dir={profile}",
        "--start-maximized",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if args.proxy:
        u = urlparse(args.proxy)
        cmd.append(f"--proxy-server={u.scheme}://{u.hostname}:{u.port}")
    print(f"launching real Chrome (profile={profile})")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint = f"http://127.0.0.1:{args.cdp_port}"
    for _ in range(60):  # wait for the debugging endpoint to answer
        try:
            with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1) as r:
                ver = json.loads(r.read().decode())
            print("attached to:", ver.get("Browser"))
            return proc, endpoint
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit(
        f"Chrome did not open a debugging port on {args.cdp_port}.\n"
        "If Chrome was ALREADY running with this profile it ignores the flag — "
        "close every Chrome window for that profile first (or use a different "
        "--chrome-profile / --cdp-port)."
    )


def _kill_chrome_tree(proc) -> None:
    """Kill Chrome AND its renderers.

    `proc.terminate()` only ends the parent; every renderer child is orphaned and
    keeps its memory. Across a few long runs that leaked ~35 processes / 3.7 GB
    here, which starved the Playwright driver and made it die mid-run ("Connection
    closed while reading from the driver")."""
    if proc is None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


async def _wait_manual(page, seconds: int) -> bool:
    """Let a HUMAN solve the challenge in the open window. Never uses input() —
    that blocks the asyncio loop and wedges patchright's driver."""
    print(f"\n  >>> Solve the Cloudflare checkbox in the Chrome window "
          f"(waiting up to {seconds}s) <<<", flush=True)
    for _ in range(seconds // 2):
        await asyncio.sleep(2)
        challenged, _, _ = await is_challenged(page)
        if not challenged:
            print("  thanks — challenge cleared, continuing automatically", flush=True)
            return True
    return False


# ── run ──────────────────────────────────────────────────────────────────────
async def resolve_all(jobs: list[dict], args) -> list[dict]:
    from patchright.async_api import async_playwright

    if args.headless:
        print("WARNING: headless Chrome cannot pass an interactive Cloudflare "
              "challenge (and Turnstile ignores clicks when the window isn't "
              "focused). Run headed, and leave the window in the foreground.")

    # Minimal launch args on purpose — patchright's own defaults are the stealth
    # config, and extra flags (notably re-adding
    # --disable-blink-features=AutomationControlled, which patchright already
    # sets) only add fingerprint surface.
    launch = dict(
        user_data_dir=args.profile, channel="chrome", headless=args.headless,
        no_viewport=True, locale="en-US",
        # Playwright defaults chromium_sandbox to False, which appends
        # `--no-sandbox` (Chrome then shows its yellow "unsupported
        # command-line flag" infobar, and the flag itself is a bot signal).
        chromium_sandbox=True,
        # patchright already passes --no-first-run / --no-default-browser-check /
        # --disable-blink-features=AutomationControlled; don't duplicate them.
        # (Chrome's yellow "unsupported command-line flag" infobar about that last
        # one is cosmetic — it's patchright's own stealth flag, leave it alone.)
        args=["--start-maximized"],
    )
    if args.proxy:
        u = urlparse(args.proxy)
        launch["proxy"] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            launch["proxy"]["username"] = u.username
        if u.password:
            launch["proxy"]["password"] = u.password

    resolved: list[dict] = []
    cf_fails = 0
    gated = 0
    proc = None
    pw = await async_playwright().start()

    async def _open_browser():
        """(ctx, page, proc) for a fresh browser session."""
        nonlocal proc
        if args.real_chrome or args.cdp:
            endpoint = args.cdp
            if not endpoint:
                proc, endpoint = _launch_real_chrome(args)
            browser = await pw.chromium.connect_over_cdp(endpoint)
            c = browser.contexts[0] if browser.contexts else await browser.new_context()
            p = c.pages[0] if c.pages else await c.new_page()
        else:
            c = await pw.chromium.launch_persistent_context(**launch)
            p = c.pages[0] if c.pages else await c.new_page()
        p.set_default_timeout(60000)
        return c, p

    ctx, page = await _open_browser()

    def _browser_died(exc: Exception) -> bool:
        """Chrome/CDP went away — every later call would fail the same way."""
        return any(s in str(exc) for s in (
            "Connection closed", "Target closed", "Browser closed",
            "has been closed", "Target page, context or browser has been closed",
        ))

    async def _reconnect() -> bool:
        """Relaunch Chrome after a crash so the rest of the queue isn't wasted."""
        nonlocal ctx, page, proc, pw
        print("      browser connection died — restarting driver + Chrome...")
        _kill_chrome_tree(proc)
        proc = None
        # "Connection closed while reading from the driver" means the Playwright
        # NODE process died too, so reconnecting with the old handle can't work —
        # the driver itself has to be restarted.
        try:
            await pw.stop()
        except Exception:
            pass
        await asyncio.sleep(3)
        try:
            pw = await async_playwright().start()
            ctx, page = await _open_browser()
            return True
        except Exception as e:
            print("      relaunch failed:", str(e)[:70])
            return False
    try:
        for i, job in enumerate(jobs, 1):
            url = job["url"]
            t0 = time.monotonic()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                cleared = await clear_challenge(page, max_wait_s=args.cf_wait)
                if not cleared and args.manual:
                    cleared = await _wait_manual(page, args.manual)
                if not cleared:
                    cf_fails += 1
                    print(f"[{i}/{len(jobs)}] id={job['id']} CF NOT cleared — skipping")
                    # Every failed challenge makes Cloudflare stricter with this
                    # IP, so grinding on turns a soft block into a hard one. Stop
                    # and let the reputation decay instead.
                    if cf_fails >= args.max_cf_failures:
                        print(f"\nAborting: {cf_fails} Cloudflare failures in a row.\n"
                              "  Each failure hardens the block for this IP, so retrying now\n"
                              "  makes it worse. In rough order of effectiveness:\n"
                              "   • --real-chrome   attach to a genuine Chrome instead of a\n"
                              "                     Playwright-launched one (no automation flags)\n"
                              "   • --manual 120    solve the checkbox yourself when we can't\n"
                              "   • wait ~30 min and re-run (CF reputation decays), or\n"
                              "   • run from a residential IP / --proxy")
                        break
                    continue
                cf_fails = 0
                await _settle(page)
                apply_url = await extract_apply(page, ctx)
                took = round(time.monotonic() - t0, 1)
                if apply_url:
                    resolved.append({"id": job["id"], "apply_url": apply_url})
                    print(f"[{i}/{len(jobs)}] id={job['id']} OK ({took}s) -> {apply_url[:70]}")
                elif await _is_login_gated(page):
                    gated += 1
                    print(f"[{i}/{len(jobs)}] id={job['id']} LOGIN-GATED ({took}s) — "
                          "Himalayas hides the employer link behind /signup")
                else:
                    print(f"[{i}/{len(jobs)}] id={job['id']} no external apply URL found ({took}s)")
                    await _dump(page, job["id"])
            except Exception as e:
                print(f"[{i}/{len(jobs)}] id={job['id']} error: {str(e)[:80]}")
                # A dead browser fails EVERY remaining job identically, so
                # relaunch and retry this one instead of burning the queue.
                if _browser_died(e):
                    if not await _reconnect():
                        print("      giving up — re-run to continue where this left off")
                        break
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        if await clear_challenge(page, max_wait_s=args.cf_wait):
                            await _settle(page)
                            retry_url = await extract_apply(page, ctx)
                            if retry_url:
                                resolved.append({"id": job["id"], "apply_url": retry_url})
                                print(f"      recovered -> {retry_url[:70]}")
                    except Exception as e2:
                        print("      retry after relaunch failed:", str(e2)[:60])
            if i < len(jobs):  # don't machine-gun the next page
                await asyncio.sleep(random.uniform(2.0, 5.0))
    finally:
        if proc is not None and not args.keep_open:
            # Only ever kill a Chrome WE launched — tree-kill so no renderer leaks.
            _kill_chrome_tree(proc)
        elif not (args.real_chrome or args.cdp):
            try:
                await ctx.close()
            except Exception:
                pass
        try:
            await pw.stop()
        except Exception:
            pass
    if gated:
        print(f"\n{gated} job page(s) were LOGIN-GATED. Cloudflare was cleared fine — "
              "Himalayas simply\nnever shows the employer URL to a logged-out visitor "
              '("Apply now" -> /signup/talent,\nand its JSON-LD says "directApply": false). '
              "Resolving those needs a signed-in\nHimalayas session in the same profile; "
              "otherwise the Himalayas job page stays the\napply destination (it works — "
              "the visitor just signs up there).")
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
    ap.add_argument("--max-cf-failures", type=int, default=3,
                    help="give up after N Cloudflare failures in a row (default 3)")
    # ── real-Chrome mode ──
    ap.add_argument("--real-chrome", action="store_true",
                    help="launch a GENUINE Chrome (no Playwright automation flags) and "
                         "attach over CDP — the best odds against Cloudflare")
    ap.add_argument("--cdp", help="attach to an ALREADY-running Chrome, e.g. http://127.0.0.1:9222 "
                                  "(start it yourself with --remote-debugging-port=9222)")
    ap.add_argument("--chrome-profile", default="sessions/himalayas-chrome",
                    help="profile dir for --real-chrome. Point it at your own Chrome profile "
                         "to inherit its history/cookies — Chrome must be fully CLOSED first")
    ap.add_argument("--cdp-port", type=int, default=9222, help="debugging port for --real-chrome")
    ap.add_argument("--keep-open", action="store_true",
                    help="leave the --real-chrome window running afterwards, so the next run "
                         "reuses the already-cleared session")
    ap.add_argument("--manual", type=int, nargs="?", const=120, default=0, metavar="SECONDS",
                    help="if we can't clear Cloudflare, pause and let YOU click the checkbox "
                         "in the open window (default 120s)")
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
