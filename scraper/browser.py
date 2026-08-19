"""Stealth browser core — patchright + real Chrome + persistent profile.

Anti-detection strategy (built on the proven stake.com-crawl setup, upgraded
with a persistent profile):
  1. `patchright` (a stealth Playwright fork) patches the usual automation leaks
     (`navigator.webdriver`, CDP runtime, etc.).
  2. `channel="chrome"` drives the *real* installed Chrome — real fingerprint/UA.
  3. `launch_persistent_context(user_data_dir=...)` — the "perfect browser":
     one durable Chrome profile whose cookies + fingerprint persist across runs,
     so we present as a returning human rather than a clean-room bot.
  4. Cloudflare Turnstile handling: detect the interstitial (iframe presence,
     page title, or body text), bring the window to the FOREGROUND — Turnstile
     ignores clicks while the window is unfocused — and geometrically click the
     checkbox inside the challenges.cloudflare.com iframe. See
     `clear_challenge()`, which tools/ shares.
  5. Human-shaped pacing on every navigation (see `human.py`).

Headful only: per patchright's maintainers, headless Chrome is always detectable
without a patched Chromium, so `HEADLESS=true` will not pass an interactive
Cloudflare challenge.
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

#: X-offsets (CSS px, from the widget's left edge) of the Turnstile checkbox,
#: tried in order across successive clicks. Measured on the live 300x65 "normal"
#: widget: the box spans roughly x+8 … x+28, so its centre is ~x+18. The old
#: `width / 9` (≈33px on a 300px widget) landed just PAST the checkbox's right
#: edge and silently did nothing.
_TURNSTILE_CLICK_DX = (18, 24, 12)


# ── Cloudflare / checkpoint handling (page-scoped so standalone tools in
#    tools/ can share the exact same logic instead of re-implementing it) ──────
async def turnstile_bbox(page) -> Optional[dict]:
    """Bounding box of the VISIBLE challenges.cloudflare.com Turnstile widget.

    A full-page interstitial often has more than one challenges.cloudflare.com
    iframe — a hidden/tiny management iframe plus the visible checkbox widget.
    Returning the first one could yield the 0-size hidden iframe, so the
    geometric click lands on empty space. Pick the largest visibly-sized one."""
    best = None
    try:
        for frame in page.frames:
            if not frame.url.startswith("https://challenges.cloudflare.com"):
                continue
            try:
                bbox = await (await frame.frame_element()).bounding_box()
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


async def is_challenged(page) -> tuple[bool, str, Optional[dict]]:
    """Return (challenged?, title, turnstile_bbox). A page is challenged if a
    challenges.cloudflare.com iframe is present, OR the title/body text match
    known interstitial phrases (covers Indeed's own-branded challenge page)."""
    try:
        title = (await page.title()) or ""
    except Exception:
        title = ""
    bbox = await turnstile_bbox(page)
    if bbox is not None:
        return True, title, bbox
    if any(h in title.lower() for h in _CHECKPOINT_TITLE_HINTS):
        return True, title, None
    try:
        body = ((await page.inner_text("body")) or "")[:4000].lower()
        if any(h in body for h in _CHALLENGE_TEXT_HINTS):
            return True, title, None
    except Exception:
        pass
    return False, title, None


async def is_settled(page, title: str) -> bool:
    """False while Chrome is still mid-navigation.

    During a reload the document is empty and the title is blank or Chrome's
    "Loading <url>" placeholder — the challenge iframe simply hasn't been parsed
    yet, so `is_challenged()` reads clean. Treating that snapshot as "cleared" is
    how a still-challenged page slipped through and scraped 0 cards."""
    t = (title or "").strip().lower()
    if not t or t.startswith("loading "):
        return False
    try:
        return bool(((await page.inner_text("body")) or "").strip())
    except Exception:
        return False


async def clear_challenge(
    page,
    max_wait_s: int = 120,
    grace_s: int = 8,
    max_clicks: int = 3,
    click_gap_s: int = 12,
    click: bool = True,
) -> bool:
    """Wait out / click through a bot-checkpoint interstitial.

    Some checkpoints auto-clear if you simply wait (hence the grace period before
    touching anything). An *interactive* Turnstile ("Verify you are human" with a
    checkbox) never auto-clears — it has to be clicked. Two things make that click
    actually register, and both used to be missing here:

      1. THE WINDOW MUST BE FOCUSED. Turnstile ignores pointer input while
         `document.hasFocus()` is false, which is the normal state for a browser
         driven from a terminal — the click was dispatched, the checkbox stayed
         empty, and the challenge sat there until the timeout. `bring_to_front()`
         fixes it.
      2. THE CLICK MUST LAND ON THE CHECKBOX (see `_TURNSTILE_CLICK_DX`).

    We click at most `max_clicks` times, spaced `click_gap_s` apart, walking
    through the candidate offsets (rapid re-clicking loops forever on sticky
    challenges, common on datacenter/server IPs).

    NOTE: we deliberately do NOT click via `frame_locator(...).locator(...)` into
    the Cloudflare iframe. Locator clicks execute from patchright's utility
    context, which Turnstile has been known to flag as a context leak and then
    freeze its own event listeners — after which even a real human click fails.
    A plain trusted mouse event at the right coordinates has none of that risk.
    """
    elapsed = 0
    clicks = 0
    last_click = -10_000
    title = ""
    saw_challenge = False  # once true, a clean read has to be confirmed
    clean_reads = 0
    while elapsed < max_wait_s:
        challenged, title, bbox = await is_challenged(page)
        if not challenged and await is_settled(page, title):
            # A page that was never challenged is done the moment it settles.
            # After a challenge, take a second consecutive clean read first: the
            # interstitial blanks itself while it reloads, and that gap used to
            # be mistaken for success.
            if not saw_challenge:
                return True
            clean_reads += 1
            if clean_reads >= 2:
                log.info("Checkpoint cleared after ~{}s (title={!r})", elapsed, title)
                return True
            await asyncio.sleep(2)
            elapsed += 2
            continue
        clean_reads = 0
        saw_challenge = saw_challenge or challenged

        if (
            bbox
            and click
            and elapsed >= grace_s
            and clicks < max_clicks
            and elapsed - last_click >= click_gap_s
        ):
            dx = _TURNSTILE_CLICK_DX[min(clicks, len(_TURNSTILE_CLICK_DX) - 1)]
            cx = bbox["x"] + dx
            cy = bbox["y"] + bbox["height"] / 2
            try:
                # Focus first — without this the click is silently ignored.
                await page.bring_to_front()
                await asyncio.sleep(0.5)
                focused = await page.evaluate("() => document.hasFocus()")
                # Approach the checkbox like a human, then press/release with a
                # human-length dwell rather than an instantaneous click.
                await page.mouse.move(cx - 140, cy - 90, steps=12)
                await asyncio.sleep(0.3)
                await page.mouse.move(cx - 40, cy - 20, steps=10)
                await asyncio.sleep(0.25)
                await page.mouse.move(cx, cy, steps=10)
                await asyncio.sleep(0.5)
                await page.mouse.down()
                await asyncio.sleep(0.09)
                await page.mouse.up()
                clicks += 1
                last_click = elapsed
                log.info(
                    "Clicked Turnstile ({}/{}) at ~{}s — widget {}x{}, click=({:.0f},{:.0f}) "
                    "dx=+{}, hasFocus={}",
                    clicks, max_clicks, elapsed,
                    round(bbox["width"]), round(bbox["height"]), cx, cy, dx, focused,
                )
                if not focused:
                    log.warning(
                        "Window is NOT focused — Turnstile ignores clicks in that state. "
                        "Keep the Chrome window visible/foreground (headless can't pass this)."
                    )
            except Exception as exc:
                log.warning("Turnstile click failed: {}", exc)

        await asyncio.sleep(2)
        elapsed += 2

    log.warning("Checkpoint NOT cleared after {}s; last title={!r}", max_wait_s, title)
    return False


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
            # Keep this list as SHORT as possible: patchright's own default args
            # are tuned for stealth, and every extra flag is one more thing to
            # fingerprint. It ALREADY passes
            # `--disable-blink-features=AutomationControlled`, `--no-first-run`
            # and `--no-default-browser-check`, so don't repeat those — a
            # maximized real window is the only thing we still want.
            "args": ["--start-maximized"]
            # QUIC (UDP) can't traverse an HTTP proxy; Chrome prefers it for
            # Google domains and stalls at about:blank. Force TCP — but only
            # when we're actually proxied.
            + (["--disable-quic"] if self.proxy_url else []),
            # Use the real window size (no synthetic viewport) — fewer fingerprint
            # inconsistencies for Cloudflare to catch.
            "no_viewport": True,
            "locale": "en-US",
            # Playwright defaults this to False, which silently appends
            # `--no-sandbox` — a flag real Chrome never runs with (it also raises
            # Chrome's yellow "unsupported command-line flag" infobar) and one
            # patchright's maintainers call potentially detectable. Keep the
            # sandbox ON so our command line looks like a normal browser's.
            "chromium_sandbox": True,
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
        # Each teardown step is time-boxed: on Windows the Proactor loop can
        # otherwise block forever closing sockets whose peer already reset
        # (WinError 10054), which would stall a multi-site run / the scheduler
        # after one scraper instead of moving on to the next.
        async def _safe(make, timeout: float = 15.0) -> None:
            if not make:
                return
            try:
                await asyncio.wait_for(make(), timeout=timeout)
            except Exception:
                pass

        await _safe(getattr(self.context, "close", None))
        await _safe(getattr(self._pw, "stop", None))
        if self._local_proxy:
            await _safe(self._local_proxy.stop)
            self._local_proxy = None

    # ── Cloudflare / checkpoint handling ────────────────────────────────────
    async def clear_checkpoint(
        self,
        max_wait_s: int = 120,
        grace_s: int = 8,
        max_clicks: int = 3,
        click_gap_s: int = 12,
    ) -> bool:
        """Wait out / click through a bot-checkpoint interstitial (see
        `clear_challenge`, which standalone tools share)."""
        return await clear_challenge(
            self.page,
            max_wait_s=max_wait_s,
            grace_s=grace_s,
            max_clicks=max_clicks,
            click_gap_s=click_gap_s,
            click=settings.click_turnstile,
        )

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
