"""Stealth browser core — patchright + real Chrome + persistent profile.

Anti-detection strategy (built on the proven stake.com-crawl setup, upgraded
with a persistent profile):
  1. `patchright` (a stealth Playwright fork) patches the usual automation leaks
     (`navigator.webdriver`, CDP runtime, etc.).
  2. `channel="chrome"` drives the *real* installed Chrome — real fingerprint/UA.
  3. `launch_persistent_context(user_data_dir=...)` — the "perfect browser":
     one durable Chrome profile whose cookies + fingerprint persist across runs,
     so we present as a returning human rather than a clean-room bot.
  4. Cloudflare Turnstile handling copied from the reference project: detect the
     interstitial by page title, then geometrically click the checkbox inside
     the challenges.cloudflare.com iframe.
  5. Human-shaped pacing on every navigation (see `human.py`).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from patchright.async_api import async_playwright

from config import settings
from logger import log
from scraper import human
from scraper.local_proxy import LocalRoutingProxy

_CHECKPOINT_TITLE_HINTS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "verifying you are human",
    "security checkpoint",
    "performing security verification",
    "additional verification required",
)

# Some sites (e.g. Indeed) embed a Cloudflare Turnstile in their OWN branded
# page, so the title is unremarkable. Detect those by on-page text too.
_CHALLENGE_TEXT_HINTS = (
    "verify you are human",
    "additional verification required",
    "checking if the site connection is secure",
    "needs to review the security of your connection",
    "ray id",
)


class StealthBrowser:
    """A single persistent patchright Chrome context."""

    def __init__(
        self,
        headless: Optional[bool] = None,
        user_data_dir: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ):
        self.headless = settings.headless if headless is None else headless
        self.user_data_dir = Path(user_data_dir or settings.user_data_dir)
        # Per-site proxy override; None → the shared default from settings.
        self.proxy_url = proxy_url if proxy_url is not None else settings.proxy_url
        self._pw = None
        self.context = None
        self.page = None
        self._local_proxy: Optional[LocalRoutingProxy] = None

    async def __aenter__(self) -> "StealthBrowser":
        await self.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    def _proxy_config(self) -> Optional[dict]:
        if not self.proxy_url:
            return None
        u = urlparse(self.proxy_url)
        cfg: dict = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            cfg["username"] = u.username
        if u.password:
            cfg["password"] = u.password
        if settings.proxy_bypass:
            cfg["bypass"] = settings.proxy_bypass
        return cfg

    async def start(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()

        launch_kwargs: dict = {
            "user_data_dir": str(self.user_data_dir),
            "channel": "chrome",
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                # QUIC (UDP) can't traverse an HTTP proxy; Chrome prefers it for
                # Google domains and stalls at about:blank. Force TCP.
                "--disable-quic",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            # Use the real window size (no synthetic viewport) — fewer fingerprint
            # inconsistencies for Cloudflare to catch.
            "no_viewport": True,
            "locale": "en-US",
        }
        if settings.timezone:
            launch_kwargs["timezone_id"] = settings.timezone
        if settings.user_agent:
            launch_kwargs["user_agent"] = settings.user_agent

        # Route through a local split-tunnel proxy: Google direct, Indeed via
        # the residential upstream (Chrome can't tunnel Google through it).
        if self.proxy_url:
            direct = settings.proxy_bypass.split(",") if settings.proxy_bypass else []
            self._local_proxy = LocalRoutingProxy(self.proxy_url, direct)
            port = await self._local_proxy.start()
            launch_kwargs["proxy"] = {"server": f"http://127.0.0.1:{port}"}
            log.info("Routing via local split-tunnel proxy -> upstream {}", urlparse(self.proxy_url).hostname)

        self.context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(settings.nav_timeout_ms)
        self.page.set_default_navigation_timeout(settings.nav_timeout_ms)
        log.info(
            "Stealth browser started (headless={}, profile={})",
            self.headless,
            self.user_data_dir,
        )

    async def close(self) -> None:
        for closer in (
            getattr(self.context, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer:
                try:
                    await closer()
                except Exception:
                    pass
        if self._local_proxy:
            try:
                await self._local_proxy.stop()
            except Exception:
                pass
            self._local_proxy = None

    # ── Cloudflare / checkpoint handling ────────────────────────────────────
    async def _cloudflare_bbox(self):
        """Bounding box of the VISIBLE challenges.cloudflare.com Turnstile widget.

        A full-page interstitial often has more than one challenges.cloudflare.com
        iframe — a hidden/tiny management iframe plus the visible checkbox widget.
        Returning the first one (as we used to) could yield the 0-size hidden
        iframe, so the geometric click lands on empty space. Pick the largest
        visibly-sized iframe instead."""
        best = None
        try:
            for frame in self.page.frames:
                if not frame.url.startswith("https://challenges.cloudflare.com"):
                    continue
                try:
                    fe = await frame.frame_element()
                    bbox = await fe.bounding_box()
                except Exception:
                    continue
                # Skip hidden/management iframes; keep only widget-sized ones.
                if not bbox or bbox["width"] < 50 or bbox["height"] < 20:
                    continue
                if best is None or bbox["width"] * bbox["height"] > best["width"] * best["height"]:
                    best = bbox
        except Exception:
            pass
        return best

    async def _is_challenged(self) -> tuple[bool, str, Optional[dict]]:
        """Return (challenged?, title, turnstile_bbox). A page is challenged if a
        challenges.cloudflare.com iframe is present, OR the title/body text match
        known interstitial phrases (covers Indeed's own-branded challenge page)."""
        try:
            title = (await self.page.title()) or ""
        except Exception:
            title = ""
        bbox = await self._cloudflare_bbox()
        if bbox is not None:
            return True, title, bbox
        if any(h in title.lower() for h in _CHECKPOINT_TITLE_HINTS):
            return True, title, None
        try:
            body = ((await self.page.inner_text("body")) or "")[:4000].lower()
            if any(h in body for h in _CHALLENGE_TEXT_HINTS):
                return True, title, None
        except Exception:
            pass
        return False, title, None

    async def clear_checkpoint(
        self,
        max_wait_s: int = 120,
        grace_s: int = 8,
        max_clicks: int = 2,
        click_gap_s: int = 12,
    ) -> bool:
        """Wait out / click through a bot-checkpoint interstitial.

        Some checkpoints auto-clear if you simply wait (hence the grace period
        before touching anything). If a Cloudflare Turnstile is present, click
        its checkbox at (x + width/9, y + height/2) of the iframe — at most
        `max_clicks` times, spaced `click_gap_s` apart (rapid re-clicking loops
        forever on sticky challenges, common on datacenter/server IPs).
        """
        elapsed = 0
        clicks = 0
        last_click = -10_000
        title = ""
        while elapsed < max_wait_s:
            challenged, title, bbox = await self._is_challenged()
            if not challenged:
                if elapsed:
                    log.info("Checkpoint cleared after ~{}s (title={!r})", elapsed, title)
                return True

            if (
                bbox
                and settings.click_turnstile
                and elapsed >= grace_s
                and clicks < max_clicks
                and elapsed - last_click >= click_gap_s
            ):
                try:
                    # Approach the checkbox like a human before clicking. The
                    # Turnstile checkbox sits ~30px from the widget's left edge,
                    # vertically centered (≈ width/9 for the ~300px widget).
                    cx = bbox["x"] + bbox["width"] / 9
                    cy = bbox["y"] + bbox["height"] / 2
                    await self.page.mouse.move(cx, cy, steps=15)
                    await asyncio.sleep(0.4)
                    await self.page.mouse.click(cx, cy)
                    clicks += 1
                    last_click = elapsed
                    log.info(
                        "Clicked Turnstile ({}/{}) at ~{}s — widget {}x{}, click=({:.0f},{:.0f})",
                        clicks, max_clicks, elapsed,
                        round(bbox["width"]), round(bbox["height"]), cx, cy,
                    )
                except Exception as exc:
                    log.warning("Turnstile click failed: {}", exc)

            await asyncio.sleep(2)
            elapsed += 2

        log.warning("Checkpoint NOT cleared after {}s; last title={!r}", max_wait_s, title)
        return False

    # ── navigation ──────────────────────────────────────────────────────────
    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> bool:
        """Navigate, clear any checkpoint, then settle with human-like pacing."""
        log.info("Navigating to {}", url)
        await self.page.goto(url, wait_until=wait_until, timeout=settings.nav_timeout_ms)
        ok = await self.clear_checkpoint()
        await human.think(settings.min_page_delay, settings.max_page_delay)
        await human.human_mouse_move(self.page)
        return ok

    async def screenshot(self, path: str) -> None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=path, full_page=False)
            log.info("Saved screenshot -> {}", path)
        except Exception as exc:
            log.warning("Screenshot failed: {}", exc)
