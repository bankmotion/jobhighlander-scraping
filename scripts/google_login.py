"""One-time / on-demand Google sign-in for the persistent profile.

Run from the project root:
    python scripts/google_login.py

Reuses an existing Google session if the profile already has one; otherwise
attempts automated login with GOOGLE_EMAIL/GOOGLE_PASSWORD. If Google blocks
automation (2FA / "verify it's you"), it saves a screenshot so we can see
exactly which wall we hit. Once signed in, the persistent Chrome profile keeps
the session and 'Continue with Google' on Indeed will work.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logger import log  # noqa: E402
from scraper.browser import StealthBrowser  # noqa: E402
from scraper.auth.google_auth import GoogleAuthService  # noqa: E402


async def main() -> None:
    browser = StealthBrowser()
    svc = GoogleAuthService()
    try:
        await browser.start()
        await svc.load(browser.context)  # seed cookies from any prior snapshot

        if await svc.is_signed_in(browser.page):
            log.success("Already signed in to Google.")
            await svc.save(browser.context, browser.page)
            return

        log.info("Not signed in — attempting automated Google login...")
        if await svc.try_auto_login(browser.page):
            log.success("Google auto-login succeeded.")
            await svc.save(browser.context, browser.page)
        else:
            await browser.screenshot("screenshots/google_login_blocked.png")
            try:
                log.error("Auto-login did not complete. Landed on: {}", browser.page.url[:160])
            except Exception:
                pass
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
