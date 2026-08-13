"""Open the stealth browser (persistent profile + proxy, logged into Indeed)
and leave it open for manual control. Close the browser window when done.

Usage:
    python scripts/open_browser.py                 # opens indeed.com
    python scripts/open_browser.py "https://..."   # opens a specific URL
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from logger import log  # noqa: E402
from scraper.browser import StealthBrowser  # noqa: E402
from scraper.session import SessionStore  # noqa: E402
from scraper.auth.google_auth import GoogleAuthService  # noqa: E402


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.indeed.com/"
    browser = StealthBrowser(headless=False)
    await browser.start()

    # Seed the saved sessions so we open already signed in.
    await GoogleAuthService().load(browser.context)
    await SessionStore.load(browser.context, None, settings.indeed_session_file)

    try:
        await browser.goto(url)
    except Exception as exc:
        log.warning("Navigation issue (you can still drive it manually): {}", exc)

    log.success("Browser is OPEN and logged in — control it manually.")
    log.info("Close the browser window when you're done to end this session.")

    # Keep the process (and browser) alive until the window is closed.
    closed = asyncio.Event()
    try:
        browser.context.on("close", lambda: closed.set())
        await closed.wait()
    except Exception:
        await asyncio.sleep(6 * 3600)  # fallback: stay up to 6h
    finally:
        await browser.close()
        log.info("Browser session ended.")


if __name__ == "__main__":
    asyncio.run(main())
