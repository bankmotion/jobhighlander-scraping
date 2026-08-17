"""Browser session persistence — cookies + localStorage + sessionStorage stored
in the DB (scraper_sessions table), one row per site.

Previously these were JSON files under sessions/. They now live in the DB so the
app can manage them (and a session captured elsewhere — e.g. a WWR cookie from a
clean IP — can be dropped straight into the table). The callers still pass the
old file path; we derive the site key from it (indeed_session.json → "indeed")
and fall back to reading a legacy file once, migrating it to the DB on save.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

import pymysql

from config import settings
from logger import log

_DUMP_STORAGE_JS = """
() => {
  const ls = {}, ss = {};
  try { for (let i=0;i<localStorage.length;i++){const k=localStorage.key(i); ls[k]=localStorage.getItem(k);} } catch(e){}
  try { for (let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i); ss[k]=sessionStorage.getItem(k);} } catch(e){}
  return { localStorage: ls, sessionStorage: ss };
}
"""

_RESTORE_STORAGE_JS = """
({ localStorage, sessionStorage }) => {
  for (const [k,v] of Object.entries(localStorage||{})) { try { window.localStorage.setItem(k,v); } catch(e){} }
  for (const [k,v] of Object.entries(sessionStorage||{})) { try { window.sessionStorage.setItem(k,v); } catch(e){} }
}
"""


def _site_key(path: str) -> str:
    """indeed_session.json → 'indeed'; google_session.json → 'google'."""
    return Path(path).stem.replace("_session", "").strip() or "default"


def _conn():
    return pymysql.connect(
        host=settings.db_host, port=settings.db_port, user=settings.db_user,
        password=settings.db_password, database=settings.db_name,
        charset="utf8mb4", autocommit=True,
    )


class SessionStore:
    """Save/restore cookies + web storage to the scraper_sessions table."""

    @staticmethod
    async def save(context, page, path: str, domains: Optional[Iterable[str]] = None) -> bool:
        try:
            cookies = await context.cookies()
            if domains:
                doms = tuple(domains)
                cookies = [c for c in cookies if any(d in (c.get("domain") or "") for d in doms)]
            storage = {}
            try:
                if page is not None:
                    storage = await page.evaluate(_DUMP_STORAGE_JS)
            except Exception:
                pass
            data = {
                "cookies": cookies,
                "localStorage": storage.get("localStorage", {}),
                "sessionStorage": storage.get("sessionStorage", {}),
                "url": page.url if page is not None else None,
            }
            site = _site_key(path)
            conn = _conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO scraper_sessions (site, cookies, updated_at) "
                    "VALUES (%s, %s, UTC_TIMESTAMP(3)) "
                    "ON DUPLICATE KEY UPDATE cookies = VALUES(cookies), updated_at = UTC_TIMESTAMP(3)",
                    (site, json.dumps(data)),
                )
            conn.close()
            log.info("Saved session -> DB[{}] ({} cookies)", site, len(cookies))
            return True
        except Exception as e:
            log.error("Failed to save session {}: {}", path, e)
            return False

    @staticmethod
    async def load(context, page, path: str, restore_storage: bool = False) -> bool:
        site = _site_key(path)
        data = None
        try:
            conn = _conn()
            with conn.cursor() as cur:
                cur.execute("SELECT cookies FROM scraper_sessions WHERE site = %s", (site,))
                row = cur.fetchone()
            conn.close()
            if row and row[0]:
                data = json.loads(row[0])
        except Exception as e:
            log.warning("DB session load failed for {}: {}", site, e)

        # Legacy JSON file fallback (one-time — it migrates to the DB on next save).
        if data is None:
            p = Path(path)
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    log.info("Loaded legacy file session {} (moves to DB on next save)", path)
                except Exception:
                    pass

        if data is None:
            log.info("No saved session for {}", site)
            return False

        cookies = data.get("cookies", [])
        if cookies:
            try:
                await context.add_cookies(cookies)
                log.info("Loaded {} cookies from session[{}]", len(cookies), site)
            except Exception as e:
                log.warning("Some cookies rejected for {}: {}", site, e)
        if restore_storage and page is not None:
            ls = data.get("localStorage", {})
            ss = data.get("sessionStorage", {})
            url = data.get("url")
            if (ls or ss) and url:
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.evaluate(_RESTORE_STORAGE_JS, {"localStorage": ls, "sessionStorage": ss})
                except Exception:
                    pass
        return True
