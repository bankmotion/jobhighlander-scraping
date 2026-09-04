"""MySQL writer — upserts scraped jobs into the Prisma-owned `jobs` table.

Prisma owns the schema/migrations; here we only INSERT ... ON DUPLICATE KEY
UPDATE against it. Column names match the Prisma @map()s.

TWO unique keys guard the table, and they answer different questions:

  (site, site_job_id)   identifies a LISTING on a board.
  (site, fingerprint)   identifies the JOB itself — see `_fingerprint`.

The second exists because the first is not stable at the source. The Muse
regenerates the hash in its URL slug on every render, and Glassdoor issues
several listing ids for one posting, so keying only on the source id let the
same job into the table five times.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Optional

import pymysql

from config import settings
from logger import log


def _norm(s: Optional[str]) -> str:
    """Lower-case, strip accents, collapse every run of non-alphanumerics to one
    space. That last step is what makes the comparison hold: two captures of the
    same posting differed by a single character of whitespace, which an exact
    hash would have treated as two different jobs."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


#: Field separator between fingerprint parts. Every part normalises to
#: [a-z0-9 ] and `site` is an enum of lowercase words, so a pipe can never occur
#: inside a part and cannot blur the boundary between two of them.
#: MUST match SEP in backend/src/scripts/dedupe-jobs.ts — the two write the same
#: column, and a separator mismatch makes them hash the same job differently.
_SEP = "|"


def _fingerprint(
    site: str,
    company: Optional[str],
    title: str,
    description: str,
) -> str:
    """Content identity: site + company + title + the first 100 normalised
    characters of the description.

    LOCATION IS DELIBERATELY EXCLUDED. It used to be part of the key, on the
    reasoning that the same title at the same company in two cities is two jobs.
    Measured against real data that is wrong far more often than it is right:
    one requisition is broadcast to every metro, and the key then admitted one
    row per city — Pearson's "Senior Software Engineer" arrived 51 times, once
    per state capital, and Harris Computer 48 times, once per US state. 8.8% of
    the table was the same handful of postings repeated.

    The cost is real but smaller: an employer genuinely staffing the same role
    in several offices now collapses to one row.

    Company is included even though the description prefix usually names it:
    without it, "Software Engineer (Full Stack)" in Lehi, UT collapsed two
    different employers into one row.

    Only a PREFIX of the description, because boards append their own footers
    and the same posting is re-worded over time; the opening paragraph is the
    part that stays put.

    Must stay in lockstep with `fingerprint()` in
    backend/src/scripts/dedupe-jobs.ts — the two write the same column.
    """
    parts = [
        site,
        _norm(company),
        _norm(title),
        _norm(description)[:100],
    ]
    return hashlib.sha1(_SEP.join(parts).encode("utf-8")).hexdigest()


#: Column widths in the Prisma-owned `jobs` table, which Prisma alone may change
#: — so oversized values are cut to fit here rather than widened over there.
#:
#: MySQL runs strict by default, so ONE oversized value rejects the whole row and
#: raises out of upsert_job, which no scraper catches: a single Himalayas job
#: open in enough countries that `location` (a join of every one of them) passed
#: 255 characters ended a run that had already stored 380 jobs. A clamped field
#: costs the tail of one string; an unclamped one costs the rest of the pass.
_MAX_LEN = {
    "site_job_id": 191,
    "title": 512,
    "job_url": 1024,
    "apply_url": 2048,
    "company": 255,
    "company_url": 1024,
    "job_type": 64,
    "location": 255,
    "salary": 255,
}
# `description` is deliberately absent: it is longtext, and the fingerprint
# already reads only its first 100 normalised characters.


def _clamp(field: str, value: Optional[str]) -> Optional[str]:
    """Cut `value` to its column width. Counts codepoints, which is what
    utf8mb4 counts too, so `len` here and CHAR_LENGTH there agree."""
    limit = _MAX_LEN[field]
    if value is None or len(value) <= limit:
        return value
    log.warning("{} is {} chars, truncating to {}: {!r}...",
                field, len(value), limit, value[:60])
    return value[:limit]


# Tables this writer is allowed to target (guards against SQL injection since
# the table name is interpolated into the statement, not bound as a param).
_ALLOWED_TABLES = {"jobs", "jobs_temp"}

_UPSERT_SQL = """
INSERT INTO {table}
    (site, site_job_id, title, description, job_url, apply_url, company, company_url,
     job_type, remote, location, salary, posted_at, fingerprint, created_at, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3))
ON DUPLICATE KEY UPDATE
    title = VALUES(title),
    description = VALUES(description),
    job_url = VALUES(job_url),
    -- keep detail-only fields if a listing-only re-scrape has none
    apply_url = COALESCE(VALUES(apply_url), apply_url),
    company = COALESCE(VALUES(company), company),
    company_url = COALESCE(VALUES(company_url), company_url),
    job_type = COALESCE(VALUES(job_type), job_type),
    remote = VALUES(remote),
    location = VALUES(location),
    salary = COALESCE(VALUES(salary), salary),
    posted_at = VALUES(posted_at),
    -- Deliberately NOT updated: site_job_id and fingerprint.
    -- Whichever unique key matched is the row's identity; rewriting it on every
    -- pass would rename the listing each time a source rotates its id, and
    -- would make existing_keys() miss on the next run.
    updated_at = UTC_TIMESTAMP(3)
"""


class JobRepository:
    def __init__(self, table: str = "jobs"):
        if table not in _ALLOWED_TABLES:
            raise ValueError(f"Unknown jobs table {table!r}; allowed: {sorted(_ALLOWED_TABLES)}")
        self.table = table
        self._conn: Optional[pymysql.connections.Connection] = None

    def connect(self) -> None:
        self._conn = pymysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            database=settings.db_name,
            charset="utf8mb4",
            autocommit=True,
        )
        log.info("Connected to MySQL {}:{}/{}", settings.db_host, settings.db_port, settings.db_name)

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def existing_keys(self, site: str) -> set:
        """Return the set of site_job_ids already stored for `site` — so the
        scraper can skip re-fetching their (expensive) detail pages."""
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT site_job_id FROM {self.table} WHERE site = %s", (site,))
            return {row[0] for row in cur.fetchall()}

    def keys_with_descriptions(self, site: str) -> set:
        """site_job_ids already stored for `site` WITH a non-empty description.

        Stricter than `existing_keys` on purpose. A scraper that skips every id
        it has seen would also skip rows whose description failed to load, and
        those would stay empty for good; keying on "already has the expensive
        part" makes a failed fetch retry on the next run instead.
        """
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT site_job_id FROM {self.table} "
                "WHERE site = %s AND description IS NOT NULL AND description <> ''",
                (site,))
            return {row[0] for row in cur.fetchall()}

    def upsert_job(
        self,
        *,
        site: str,
        site_job_id: str,
        title: str,
        description: str,
        link: str,
        location: Optional[str],
        posted_at=None,
        apply_url: Optional[str] = None,
        company: Optional[str] = None,
        company_url: Optional[str] = None,
        job_type: Optional[str] = None,
        remote: bool = False,
        salary: Optional[str] = None,
    ) -> str:
        """Insert or update one job. Returns 'inserted' | 'updated' | 'unchanged'."""
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        # Clamp BEFORE fingerprinting, not after. dedupe-jobs.ts hashes the
        # STORED row, so hashing the full-length value here would have the two
        # disagree on exactly the rows that got truncated.
        site_job_id = _clamp("site_job_id", site_job_id)
        title = _clamp("title", title)
        link = _clamp("job_url", link)
        apply_url = _clamp("apply_url", apply_url)
        company = _clamp("company", company)
        company_url = _clamp("company_url", company_url)
        job_type = _clamp("job_type", job_type)
        location = _clamp("location", location)
        salary = _clamp("salary", salary)
        fingerprint = _fingerprint(site, company, title, description)
        with self._conn.cursor() as cur:
            cur.execute(
                _UPSERT_SQL.format(table=self.table),
                (site, site_job_id, title, description, link, apply_url, company,
                 company_url, job_type, remote, location, salary, posted_at, fingerprint),
            )
            # PyMySQL/MySQL rowcount: 1 = inserted, 2 = updated, 0 = no change.
            #
            # A duplicate posting arriving under a NEW site_job_id now matches on
            # (site, fingerprint) instead, so it lands here as "updated" — the
            # stored row is refreshed and no second row is created. That is the
            # skip: the write is not rejected, it is absorbed.
            return {1: "inserted", 2: "updated", 0: "unchanged"}.get(cur.rowcount, "unknown")

    def __enter__(self) -> "JobRepository":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class ScrapeRunRepo:
    """Logs one row per scraper run to `scrape_runs`. Uses its own connection so
    it's independent of the per-site JobRepository lifecycle (which opens/closes
    inside each scraper's run())."""

    def __init__(self):
        self._conn: Optional[pymysql.connections.Connection] = None

    def _cursor(self):
        if self._conn is None:
            self._conn = pymysql.connect(
                host=settings.db_host,
                port=settings.db_port,
                user=settings.db_user,
                password=settings.db_password,
                database=settings.db_name,
                charset="utf8mb4",
                autocommit=True,
            )
        return self._conn.cursor()

    def start(self, site: str) -> Optional[int]:
        """Insert a 'running' row; returns its id (or None if logging failed)."""
        try:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO scrape_runs (site, status, started_at, created_at) "
                    "VALUES (%s, 'running', UTC_TIMESTAMP(3), UTC_TIMESTAMP(3))",
                    (site,),
                )
                return cur.lastrowid
        except Exception as e:
            log.warning("scrape_runs start failed: {}", e)
            return None

    def finish(self, run_id: Optional[int], status: str, counts: dict, error: Optional[str] = None) -> None:
        """Mark a run finished with its final status + counts."""
        if run_id is None:
            return
        try:
            with self._cursor() as cur:
                cur.execute(
                    "UPDATE scrape_runs SET status=%s, finished_at=UTC_TIMESTAMP(3), "
                    "inserted=%s, updated=%s, unchanged=%s, skipped=%s, error=%s WHERE id=%s",
                    (
                        status,
                        int(counts.get("inserted", 0)),
                        int(counts.get("updated", 0)),
                        int(counts.get("unchanged", 0)),
                        int(counts.get("skipped", 0)),
                        (error or None),
                        run_id,
                    ),
                )
        except Exception as e:
            log.warning("scrape_runs finish failed: {}", e)

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
