"""Ad-hoc: can each proxied site be reached DIRECT (no proxy) with the real
stealth browser? Reports challenge-cleared + result-card count per site."""
import asyncio, sys
sys.path.insert(0, "/var/www/jobhighlander-scraping")
from config import settings, sync_settings_from_db
from scraper.browser import StealthBrowser, is_challenged

CARDS = {
 "indeed": "div.job_seen_beacon, td.resultContent, div.cardOutline",
 "glassdoor": 'li[data-test="jobListing"], [data-test="jobListing"], li.JobsList_jobListItem__wjTHv',
 "jobicy": 'a[href*="/jobs/"]',
}

async def probe(site, url, profile, proxy):
    label = "PROXY" if proxy else "DIRECT"
    print(f"\n===== {site} [{label}] =====", flush=True)
    b = StealthBrowser(user_data_dir=profile, proxy_url=proxy)
    try:
        await b.start()
        ok = await b.goto(url)
        page = b.page
        ch, title, _ = await is_challenged(page)
        n = len(await page.query_selector_all(CARDS[site]))
        ip = "?"
        try:
            p2 = await b.context.new_page()
            await p2.goto("https://api.ipify.org?format=json", timeout=30000)
            ip = (await p2.inner_text("body")).strip()[:60]
            await p2.close()
        except Exception as e: ip = f"err {str(e)[:40]}"
        await b.screenshot(f"screenshots/probe_{site}_{label.lower()}.png")
        print(f"RESULT site={site} mode={label} cleared={ok} still_challenged={ch} "
              f"cards={n} title={title[:70]!r} exit_ip={ip}", flush=True)
    except Exception as e:
        print(f"RESULT site={site} mode={label} EXCEPTION {type(e).__name__}: {str(e)[:200]}", flush=True)
    finally:
        await b.close()

async def main():
    sync_settings_from_db()
    site = sys.argv[1]
    proxy = "" if len(sys.argv) < 3 or sys.argv[2] != "proxy" else settings.proxy_url
    urls = {"indeed": settings.indeed_search_url,
            "glassdoor": settings.glassdoor_search_url,
            "jobicy": settings.jobicy_search_url}
    profs = {"indeed": settings.user_data_dir,
             "glassdoor": settings.glassdoor_user_data_dir,
             "jobicy": str(__import__("pathlib").Path(settings.user_data_dir).parent / "jobicy-chrome-profile")}
    await probe(site, urls[site], profs[site], proxy)

asyncio.run(main())
