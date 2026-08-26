"""Shared posting-date parsing.

Job boards express "when was this posted" two ways: a machine-readable value
(an epoch, or a `datetime` attribute) and human relative text ("2 hours ago").
Which of the two is more PRECISE varies by site, so the parsing lives here and
each scraper picks its own precedence:

  • Indeed  — `pubDate` is midnight US-Eastern of the posting DAY, so it is
    coarser than the card's own "5 hours ago" text.
  • LinkedIn — the `datetime` attribute is date-only, and the element's text is
    the sole source of time-of-day.

Everything here returns NAIVE UTC, matching what the `jobs` table stores.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

#: Relative text that pins a posting to within an hour. Coarser phrasings
#: ("3 days ago") are deliberately excluded: they carry no time-of-day, so
#: resolving them against `now` would smear the current clock time across a
#: day the site already told us exactly.
_FINE_GRAINED_RE = re.compile(r"\b(?:just|moment|\d+\s*(?:minute|hour))", re.I)


def is_fine_grained(text: str | None) -> bool:
    """True if `text` resolves to a time-of-day, not just a day."""
    return bool(_FINE_GRAINED_RE.search(text or ""))


def compute_posted_at(posted_text, pub_ms):
    """Full posting timestamp (naive UTC). Prefers an exact `pubDate` epoch
    (ms); otherwise derives it from the relative text ("5 hours ago") against now."""
    if pub_ms:
        try:
            return datetime.fromtimestamp(float(pub_ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            pass
    t = (posted_text or "").lower()
    if not t:
        return None
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    if "just posted" in t or "moment" in t or "today" in t:
        return now
    m = re.search(r"(\d+)\s*minute", t)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    m = re.search(r"(\d+)\s*hour", t)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    if "yesterday" in t:
        return now - timedelta(days=1)
    for unit, mult in (("day", 1), ("week", 7), ("month", 30), ("year", 365)):
        m = re.search(rf"(\d+)\s*\+?\s*{unit}", t)
        if m:
            return now - timedelta(days=int(m.group(1)) * mult)
    return None
