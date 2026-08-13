"""Google account sign-in + session persistence.

Ported from the stake.com-crawl reference (`google_auth.py` +
`_login_with_google`), adapted to our persistent-profile StealthBrowser:

- `try_auto_login`  — programmatic Google login with GOOGLE_EMAIL/PASSWORD.
                      Google frequently blocks this (2FA / "verify it's you"),
                      especially from a new IP — caller should handle failure.
- `is_signed_in`    — check the profile currently has a live Google session.
- `save` / `load`   — persist google cookies to google_session.json.
- `complete_site_oauth` — click a site's "Continue with Google" button and drive
                      the OAuth round-trip (popup / inline / silent).
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from config import settings
from logger import log
from scraper.session import SessionStore

GOOGLE_DOMAINS = ("google.com", "youtube.com", "gstatic.com")


class GoogleAuthService:
    def __init__(self, session_file: Optional[str] = None):
        self.session_file = session_file or settings.google_session_file

    async def is_signed_in(self, page) -> bool:
        # Signed-in: myaccount.google.com/ shows the dashboard and STAYS on that
        # host. Signed-out: it redirects away (to www.google.com/account/about
        # or accounts.google.com/signin). So the final host is the tell.
        try:
            await page.goto("https://myaccount.google.com/", wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2)
            return page.url.startswith("https://myaccount.google.com")
        except Exception:
            return False

    async def try_auto_login(self, page) -> bool:
        if not (settings.google_email and settings.google_password):
            log.info("No GOOGLE_EMAIL/GOOGLE_PASSWORD set; skipping Google auto-login")
            return False
        try:
            log.info("Attempting Google auto-login for {}...", settings.google_email)
            await page.goto(
                "https://accounts.google.com/signin/v2/identifier?flowName=GlifWebSignIn",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await asyncio.sleep(3)

            email_input = await page.wait_for_selector(
                'input[type="email"], #identifierId, input[name="identifier"]', timeout=30000, state="visible"
            )
            await email_input.click()
            await email_input.fill(settings.google_email)
            await asyncio.sleep(1)
            btn = await page.wait_for_selector('#identifierNext button, #identifierNext, button:has-text("Next")', timeout=10000)
            await btn.click()
            await asyncio.sleep(6)

            pwd = await page.wait_for_selector(
                'input[type="password"], input[name="Passwd"]', timeout=30000, state="visible"
            )
            await pwd.click()
            await pwd.fill(settings.google_password)
            await asyncio.sleep(1)
            btn = await page.wait_for_selector('#passwordNext button, #passwordNext, button:has-text("Next")', timeout=10000)
            await btn.click()
            await asyncio.sleep(8)

            # Capture the immediate post-password state for diagnosis.
            try:
                Path("screenshots").mkdir(exist_ok=True)
                await page.screenshot(path="screenshots/google_after_password.png")
                log.info("Post-password URL: {}", page.url[:160])
            except Exception:
                pass

            try:
                body = ((await page.inner_text("body")) or "").lower()
            except Exception:
                body = ""
            if "wrong password" in body or "couldn't sign you in" in body:
                log.error("Google rejected the password (wrong password).")
                return False

            url = page.url
            # Real 2FA / verification steps — NOT the normal /challenge/pwd password page.
            if any(
                x in url
                for x in (
                    "challenge/selection", "challenge/totp", "challenge/ipp", "challenge/az",
                    "challenge/dp", "challenge/sk", "challenge/kpp", "deviceverification",
                    "rejected", "gaplustos",
                )
            ):
                log.warning("Google requires additional verification (2FA). URL: {}", url[:160])
                return False
            return await self.is_signed_in(page)
        except Exception as e:
            log.warning("Google auto-login failed: {}", e)
            return False

    async def save(self, context, page) -> bool:
        return await SessionStore.save(context, page, self.session_file, domains=GOOGLE_DOMAINS)

    async def load(self, context, page=None) -> bool:
        # page=None → cookies only (don't navigate away from the current site).
        return await SessionStore.load(context, page, self.session_file, restore_storage=False)

    async def drive_popup(self, context, opener_page, popup, timeout: int = 60000) -> bool:
        """Drive a Google OAuth popup to completion: pick the account (we're
        already signed in), click through any consent, wait for it to close."""
        target = popup
        try:
            await asyncio.sleep(2)
            log.info("OAuth popup on: {}", (target.url or "")[:90])
            if "accounts.google.com" in (target.url or "") and settings.google_email:
                # Account chooser (we're signed in → the account is listed)
                for sel in (
                    f'[data-identifier="{settings.google_email}"]',
                    f'div[role="link"][aria-label*="{settings.google_email}"]',
                    f'li:has-text("{settings.google_email}")',
                    f'div[role="link"]:has-text("{settings.google_email}")',
                ):
                    try:
                        el = await target.wait_for_selector(sel, timeout=3000, state="visible")
                        if el:
                            log.info("Choosing account in popup")
                            await el.click()
                            await asyncio.sleep(4)
                            break
                    except Exception:
                        continue
                # Consent / Continue
                for sel in ('button:has-text("Continue")', 'button:has-text("Allow")', '#submit_approve_access'):
                    try:
                        el = await target.wait_for_selector(sel, timeout=3000, state="visible")
                        if el:
                            log.info("Approving OAuth consent")
                            await el.click()
                            await asyncio.sleep(4)
                            break
                    except Exception:
                        continue

            deadline = time.monotonic() + timeout / 1000
            while time.monotonic() < deadline:
                if popup.is_closed():
                    break
                try:
                    if "accounts.google.com" not in (target.url or ""):
                        break
                except Exception:
                    break
                await asyncio.sleep(1)
            await asyncio.sleep(2)
            try:
                await self.save(context, opener_page)
            except Exception:
                pass
            return True
        except Exception as e:
            log.warning("drive_popup error: {}", e)
            return False

    async def complete_site_oauth(self, context, page, button_selector: str, timeout: int = 60000) -> bool:
        """Click a site's Google button and finish the OAuth round-trip."""
        try:
            log.info("Locating Google sign-in button ({})...", button_selector)
            btn = await page.wait_for_selector(button_selector, timeout=10000, state="visible")
            url_before = page.url

            popup = None
            try:
                async with context.expect_page(timeout=5000) as popup_info:
                    log.info("Clicking Google button...")
                    try:
                        await btn.click(timeout=3000)
                    except Exception:
                        await btn.click(force=True, timeout=3000)
                popup = await popup_info.value
                log.info("Popup opened: {}", popup.url[:80])
            except Exception:
                await asyncio.sleep(2)
                current_url = page.url
                if "accounts.google.com" in current_url:
                    log.info("Inline redirect to Google detected")
                else:
                    btn_there = False
                    try:
                        btn_there = bool(await btn.is_visible())
                    except Exception:
                        btn_there = False
                    if not btn_there:
                        log.info("Auth modal dismissed, no redirect — assuming silent OAuth success")
                        return True

            target = popup if popup else page
            await asyncio.sleep(3)
            log.info("On URL: {}", target.url[:90])

            if "accounts.google.com" in target.url and settings.google_email:
                # Account picker (if the Google session already exists)
                clicked_account = False
                if "accountchooser" in target.url or "oauth" in target.url:
                    for sel in (
                        f'[data-identifier="{settings.google_email}"]',
                        f'div[role="link"][aria-label*="{settings.google_email}"]',
                        f'li:has-text("{settings.google_email}")',
                        f'div[role="link"]:has-text("{settings.google_email}")',
                    ):
                        try:
                            picker = await target.wait_for_selector(sel, timeout=3000, state="visible")
                            if picker:
                                log.info("Clicking account from picker")
                                await picker.click()
                                await asyncio.sleep(5)
                                clicked_account = True
                                break
                        except Exception:
                            continue

                # Email + password form (no session)
                if not clicked_account and settings.google_password:
                    try:
                        email_input = await target.wait_for_selector(
                            'input[name="identifier"], input[type="email"]', timeout=5000
                        )
                        await email_input.fill(settings.google_email)
                        await asyncio.sleep(1)
                        nxt = await target.wait_for_selector('#identifierNext button, button:has-text("Next")', timeout=5000)
                        await nxt.click()
                        await asyncio.sleep(5)
                    except Exception:
                        pass

                needs_password = bool(settings.google_password) and (
                    not clicked_account or "/challenge/pwd" in (target.url or "") or "/signin/challenge" in (target.url or "")
                )
                if needs_password:
                    try:
                        pwd = await target.wait_for_selector('input[type="password"]', timeout=15000, state="visible")
                        await pwd.fill(settings.google_password)
                        await asyncio.sleep(1)
                        nxt = await target.wait_for_selector('#passwordNext button, button:has-text("Next")', timeout=5000)
                        await nxt.click()
                        await asyncio.sleep(8)
                    except Exception as e:
                        log.warning("Password step skipped: {}", e)

                # Consent screen
                try:
                    cont = await target.wait_for_selector(
                        'button:has-text("Continue"), button:has-text("Allow")', timeout=5000
                    )
                    if cont:
                        await cont.click()
                        await asyncio.sleep(5)
                except Exception:
                    pass

            # Wait for OAuth to finish
            deadline = time.monotonic() + (timeout / 1000)
            done = False
            while time.monotonic() < deadline:
                if popup and popup.is_closed():
                    done = True
                    break
                try:
                    if "accounts.google.com" not in target.url:
                        done = True
                        break
                except Exception:
                    done = True
                    break
                await asyncio.sleep(1)

            if not done:
                log.warning("OAuth flow did not complete within timeout")
                return False

            await asyncio.sleep(2)
            _ = url_before
            try:
                await self.save(context, page)
            except Exception:
                pass
            return True
        except Exception as e:
            log.error("Google sign-in failed: {}", e)
            return False
