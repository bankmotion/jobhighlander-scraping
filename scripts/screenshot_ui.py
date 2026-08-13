"""Screenshot the auth flow + authed UI (login -> home -> admin -> detail)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from patchright.async_api import async_playwright  # noqa: E402

BASE = "http://localhost:3000"
EMAIL = "pavel@jobhighlander.local"
PASSWORD = "secret123"


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome", headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        page = await (await browser.new_context(viewport={"width": 1360, "height": 1400})).new_page()

        # Unauthenticated → middleware redirects to /login
        await page.goto(f"{BASE}/", wait_until="networkidle", timeout=45000)
        await asyncio.sleep(1)
        await page.screenshot(path="screenshots/ui_login.png")
        print("login shot; url:", page.url)

        # Sign in as the super_admin
        await page.fill('input[type="email"]', EMAIL)
        await page.fill('input[type="password"]', PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_url(f"{BASE}/", timeout=15000)
        await asyncio.sleep(1.5)
        await page.screenshot(path="screenshots/ui_home.png", full_page=True)
        print("home shot")

        # Job detail — first job in the list
        href = await page.get_attribute('a[href^="/jobs/"]', 'href')
        if href:
            await page.goto(f"{BASE}{href}", wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)
            await page.screenshot(path="screenshots/ui_detail.png", full_page=True)
            print("detail shot:", href)

        # Admin user management
        await page.goto(f"{BASE}/admin", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(1)
        await page.screenshot(path="screenshots/ui_admin.png", full_page=True)
        print("admin shot")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
