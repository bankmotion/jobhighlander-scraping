"""Human-like interaction helpers to reduce bot-detection signals.

Indeed and Cloudflare score *behaviour*: instant clicks, uniform timing, no
mouse movement, and machine-speed typing all look robotic. Every helper here
adds randomised, human-shaped delays and motion. Tune the ranges via the
MIN_/MAX_ settings in `.env`.
"""
import asyncio
import random


async def think(min_s: float, max_s: float) -> None:
    """Pause for a random 'thinking' interval."""
    await asyncio.sleep(random.uniform(min_s, max_s))


def jitter(base: float, spread: float = 0.35) -> float:
    """Return `base` scaled by +/- up to `spread` fraction (never negative)."""
    return max(0.0, base * (1 + random.uniform(-spread, spread)))


async def human_scroll(
    page,
    steps: int = 6,
    min_px: int = 250,
    max_px: int = 700,
    min_pause: float = 0.4,
    max_pause: float = 1.4,
) -> None:
    """Scroll down in irregular increments, occasionally drifting back up."""
    for _ in range(steps):
        await page.mouse.wheel(0, random.randint(min_px, max_px))
        if random.random() < 0.15:  # re-read, like a human
            await asyncio.sleep(random.uniform(0.2, 0.6))
            await page.mouse.wheel(0, -random.randint(80, 200))
        await asyncio.sleep(random.uniform(min_pause, max_pause))


async def human_mouse_move(page, moves: int = 3) -> None:
    """Drift the cursor to a few random points with multi-step (curved) motion."""
    vp = page.viewport_size or {"width": 1440, "height": 900}
    for _ in range(moves):
        x = random.randint(0, max(1, vp["width"] - 1))
        y = random.randint(0, max(1, vp["height"] - 1))
        await page.mouse.move(x, y, steps=random.randint(5, 20))
        await asyncio.sleep(random.uniform(0.1, 0.5))


async def human_type(page, locator, text: str, min_delay: float = 0.06, max_delay: float = 0.22) -> None:
    """Focus a field and type character-by-character with human cadence."""
    await locator.click()
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(min_delay, max_delay))
        if random.random() < 0.05:  # occasional longer pause
            await asyncio.sleep(random.uniform(0.2, 0.6))
