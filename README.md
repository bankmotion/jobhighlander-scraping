# JobHighLander — Job Seeking (Python scraper)

Scrapes job sites with a stealth browser and writes results into the shared
MySQL database that Prisma (in `../backend`) owns.

## Stealth approach

- **patchright** (stealth Playwright fork) driving **real Chrome** (`channel="chrome"`).
- **Persistent Chrome profile** (`launch_persistent_context`) — the "perfect
  browser": cookies + fingerprint persist across runs, so we look like a
  returning human, not a fresh bot.
- **Cloudflare Turnstile** handling (wait-out + geometric checkbox click).
- **Human-like pacing** everywhere — randomised delays, irregular scrolling,
  curved mouse movement, character-by-character typing. Indeed is sensitive, so
  these are deliberately generous (tune via `.env`).
- **Proxy-ready** but off by default (direct IP). Set `PROXY_URL` to enable.

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env         # then edit if needed (defaults match local XAMPP)

python scripts\smoke_test.py   # verify browser + DB
python main.py indeed          # run the Indeed scraper
```

If patchright can't find a browser, run once: `patchright install chromium`
(not required when using the installed Chrome via `channel="chrome"`).

## Layout

```
config.py            typed settings (pydantic-settings)
logger.py            loguru console + rotating file logs
main.py              entry point — dispatches to a site scraper
scraper/
  browser.py         StealthBrowser — patchright core + Cloudflare + persistent profile
  human.py           human-like delays / scroll / mouse / typing
  db.py              JobRepository — MySQL upsert into the `jobs` table
  base_scraper.py    BaseScraper + ScrapedJob (subclass per site)
  indeed.py          IndeedScraper
scripts/smoke_test.py
sessions/            persistent Chrome profile (gitignored)
logs/                (gitignored)
```

## Fields captured (per the current schema)

`site`, `site_job_id` (Indeed's `jk`), `title`, `description`, `link`, `location`.

## Adding a new site later

Subclass `BaseScraper`, set `site = "<name>"`, implement `scrape()` returning
`ScrapedJob`s, and register it in `main.py`'s `SCRAPERS` map. The stealth
browser and DB writer are reused as-is.

## Sign-in

Not wired yet — waiting on the Indeed sign-in flow. It will slot into
`scraper/` as a session module (email/password or Google OAuth), persisting an
`indeed_session.json` alongside the persistent profile.


<!-- Security scan triggered at 2026-09-05 07:52:30 -->