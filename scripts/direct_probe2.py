"""Round 2: fairest possible DIRECT (no-proxy) test.
Fresh profile (no proxy-bound cf_clearance), homepage warmup, longer challenge
budget, several navigation attempts — mirrors glassdoor._load_search()."""
import asyncio, shutil, sys, pathlib
sys.path.insert(0, "/var/www/jobhighlander-scraping")
from config import settings, sync_settings_from_db
from scraper.browser import StealthBrowser, is_challenged, clear_challenge
from scraper import human

HOME = {"indeed": "https://www.indeed.com/", "glassdoor": "https://www.glassdoor.com/"}
CARDS = {"indeed": "div.job_seen_beacon, td.resultContent, div.cardOutline",
         "glassdoor": 'li[data-test="jobListing"], [data-test="jobListing"], li.JobsList_jobListItem__wjTHv'}

async def main():
    sync_settings_from_db()
    site = sys.argv[1]
    url = {"indeed": settings.indeed_search_url, "glassdoor": settings.glassdoor_search_url}[site]
    prof = pathlib.Path(f"/tmp/probe2-{site}-profile")
    shutil.rmtree(prof, ignore_errors=True)          # guarantee NO stale cf_clearance
    print(f"===== {site} DIRECT round2 (fresh profile {prof}) =====", flush=True)
    _px = ""
    if len(sys.argv) > 2 and sys.argv[2] == "proxy":
        # Use the SAME exit-selection the real scraper uses, so the control is fair.
        from scraper.local_proxy import remembered_challenge_proxy
        _sess = {"indeed": settings.indeed_proxy_session_file,
                 "glassdoor": settings.glassdoor_proxy_session_file}[site]
        _px = remembered_challenge_proxy(settings.proxy_url, _sess,
                                         prefix=("in" if site == "indeed" else "gd"))
    print(f"mode={'PROXY' if _px else 'DIRECT'}", flush=True)
    b = StealthBrowser(user_data_dir=str(prof), proxy_url=_px)
    try:
        await b.start()
        # 1) warm up on the homepage like a returning human
        print("--- warmup: homepage ---", flush=True)
        await b.page.goto(HOME[site], wait_until="domcontentloaded", timeout=60000)
        wu = await clear_challenge(b.page, max_wait_s=180, max_clicks=5, click_gap_s=15)
        ch, title, _ = await is_challenged(b.page)
        print(f"WARMUP site={site} cleared={wu} still_challenged={ch} title={title[:60]!r}", flush=True)
        await human.think(3, 7)
        await human.human_mouse_move(b.page)
        await human.human_scroll(b.page, steps=3)
        await human.think(2, 5)
        # 2) now the search page, several attempts (each nav gets a fresh challenge)
        for attempt in range(1, 4):
            print(f"--- search attempt {attempt} ---", flush=True)
            await b.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            ok = await clear_challenge(b.page, max_wait_s=180, max_clicks=5, click_gap_s=15)
            ch, title, _ = await is_challenged(b.page)
            n = len(await b.page.query_selector_all(CARDS[site]))
            print(f"ATTEMPT {attempt} site={site} cleared={ok} still_challenged={ch} "
                  f"cards={n} title={title[:60]!r}", flush=True)
            if n > 0 and not ch:
                break
            await human.think(5, 10)
        await b.screenshot(f"screenshots/probe2_{site}_direct.png")
        print(f"FINAL site={site} cards={n} challenged={ch} title={title[:60]!r}", flush=True)
    except Exception as e:
        print(f"FINAL site={site} EXCEPTION {type(e).__name__}: {str(e)[:200]}", flush=True)
    finally:
        await b.close()
        shutil.rmtree(prof, ignore_errors=True)

asyncio.run(main())
