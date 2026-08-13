"""MySQL writer — upserts scraped jobs into the Prisma-owned `jobs` table.

Prisma owns the schema/migrations; here we only INSERT ... ON DUPLICATE KEY
UPDATE against it, keyed on the unique (site, site_job_id). Column names match
the Prisma @map()s.
"""
from __future__ import annotations

from typing import Optional

import pymysql

from config import settings
from logger import log

# Tables this writer is allowed to target (guards against SQL injection since
# the table name is interpolated into the statement, not bound as a param).
_ALLOWED_TABLES = {"jobs", "jobs_temp"}

_UPSERT_SQL = """
INSERT INTO {table}
    (site, site_job_id, title, description, job_url, apply_url, company, company_url,
     job_type, remote, location, salary, posted_at, created_at, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3))
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
        with self._conn.cursor() as cur:
            cur.execute(
                _UPSERT_SQL.format(table=self.table),
                (site, site_job_id, title, description, link, apply_url, company,
                 company_url, job_type, remote, location, salary, posted_at),
            )
            # PyMySQL/MySQL rowcount: 1 = inserted, 2 = updated, 0 = no change.
            return {1: "inserted", 2: "updated", 0: "unchanged"}.get(cur.rowcount, "unknown")

    def __enter__(self) -> "JobRepository":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
