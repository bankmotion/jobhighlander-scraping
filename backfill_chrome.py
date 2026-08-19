"""One-off: strip site chrome from descriptions stored before the themuse/jobicy fix.

Every row of `site='themuse'` and `site='jobicy'` was written by the old
`desc = body` code path (themuse.py:443 / jobicy.py:240), so each one carries the
site's nav — and, on The Muse, a newsletter widget spliced into the MIDDLE of the
posting. Re-scraping is NOT an alternative: `jobs` upserts on
(site, site_job_id), so a re-run of the OLD code reproduces byte-identical rows.

The cut markers and `_clean` are IMPORTED from the scrapers rather than copied,
so this backfill produces exactly what the fixed scrapers now produce.

Deliberately Python and not one UPDATE: MariaDB 10.4's REGEXP_REPLACE replaces
astral-plane characters (emoji) with '?' even when the pattern matches nothing,
which would corrupt 5 of the 13 jobicy rows.

    python backfill_chrome.py           # dry run — reports, writes nothing
    python backfill_chrome.py --apply   # writes
"""
from __future__ import annotations

import sys

import pymysql

from config import settings
from scraper.jobicy import _DETAILS_START, _RESUME_CTA, _SHOW_MORE_RE, _clean as _jobicy_clean
from scraper.themuse import _COMPANY_CARD_END, _NEWSLETTER_RE, _clean as _themuse_clean


def _themuse(desc: str) -> str:
    i = desc.find(_COMPANY_CARD_END)
    if 0 < i < 1200:
        desc = desc[i + len(_COMPANY_CARD_END):]
    desc = _NEWSLETTER_RE.sub("", desc)
    for marker in ("Client-provided location(s):", "APPLY ON COMPANY SITE",
                   "Similar Jobs", "About The Muse"):
        i = desc.find(marker)
        if i > 400:
            desc = desc[:i]
            break
    return _themuse_clean(desc)


def _jobicy(desc: str) -> str:
    i = desc.find(_DETAILS_START)
    if 0 < i < 2000:
        desc = desc[i + len(_DETAILS_START):]
    for marker in (_RESUME_CTA, "NEXT STEP"):
        i = desc.find(marker)
        if i > 400:
            desc = desc[:i]
            break
    return _jobicy_clean(_SHOW_MORE_RE.sub("", desc))


#: Chrome that must be GONE afterwards, and the shortest plausible real posting.
_RESIDUE = {
    "themuse": ("Skip to main content", "VIEW COMPANY PROFILE", "Want more jobs like this?",
                "Send me The Muse newsletters", "Client-provided location(s):", "Viewed on "),
    "jobicy": ("Online Resume Builder", "My Profile", "Role snapshot", "OPPORTUNITY DETAILS",
               "ABOUT THE COMPANY", "How verification works", "Upload your resume now"),
}
_MIN_LEN = 1000

CLEANERS = {"themuse": _themuse, "jobicy": _jobicy}


def main(apply: bool) -> int:
    cx = pymysql.connect(host=settings.db_host, port=settings.db_port, user=settings.db_user,
                         password=settings.db_password, database=settings.db_name,
                         charset="utf8mb4", autocommit=False)
    bad = 0
    try:
        for site, fn in CLEANERS.items():
            with cx.cursor() as cur:
                cur.execute("SELECT id, description FROM jobs WHERE site=%s ORDER BY id", (site,))
                rows = cur.fetchall()
            before = after = changed = 0
            updates = []
            for jid, desc in rows:
                clean = fn(desc)
                before += len(desc)
                after += len(clean)
                # Refuse to shrink a row into nothing or leave chrome behind.
                if len(clean) < _MIN_LEN:
                    print(f"  !! id={jid} would shrink to {len(clean)} chars — SKIPPED")
                    bad += 1
                    continue
                left = [m for m in _RESIDUE[site] if m in clean]
                if left:
                    print(f"  !! id={jid} residual chrome {left} — SKIPPED")
                    bad += 1
                    continue
                if clean != desc:
                    updates.append((clean, jid))
                    changed += 1
            pct = 100 * (before - after) / before if before else 0
            print(f"{site}: {len(rows)} rows, {changed} to change, "
                  f"avg {before // max(len(rows), 1)} -> {after // max(len(rows), 1)} chars "
                  f"({pct:.2f}% removed)")
            if apply and updates:
                with cx.cursor() as cur:
                    cur.executemany("UPDATE jobs SET description=%s WHERE id=%s", updates)
        if apply:
            cx.commit()
            print("committed")
        else:
            cx.rollback()
            print("dry run — nothing written (pass --apply to write)")
    finally:
        cx.close()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
