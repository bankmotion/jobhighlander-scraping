"""Generic browser session persistence — cookies + localStorage + sessionStorage
saved to a JSON file per site.

The persistent Chrome profile already keeps state across runs, but the user
wants explicit saved cookies for Gmail and every site; these JSON snapshots
provide that (portable, inspectable, and a backup the profile can be re-seeded
from).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

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


class SessionStore:
    """Save/restore cookies + web storage to a JSON file."""

    @staticmethod
    async def save(context, page, path: str, domains: Optional[Iterable[str]] = None) -> bool:
        try:
            cookies = await context.cookies()
            if domains:
                doms = tuple(domains)
                cookies = [c for c in cookies if any(d in (c.get("domain") or "") for d in doms)]
            storage = {}
            try:
                storage = await page.evaluate(_DUMP_STORAGE_JS)
            except Exception:
                pass
            data = {
                "cookies": cookies,
                "localStorage": storage.get("localStorage", {}),
                "sessionStorage": storage.get("sessionStorage", {}),
                "url": page.url,
            }
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log.info("Saved session -> {} ({} cookies)", path, len(cookies))
            return True
        except Exception as e:
            log.error("Failed to save session {}: {}", path, e)
            return False

    @staticmethod
    async def load(context, page, path: str, restore_storage: bool = False) -> bool:
        p = Path(path)
        if not p.exists():
            log.info("No saved session at {}", path)
            return False
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            cookies = data.get("cookies", [])
            if cookies:
                try:
                    await context.add_cookies(cookies)
                    log.info("Loaded {} cookies from {}", len(cookies), path)
                except Exception as e:
                    log.warning("Some cookies rejected from {}: {}", path, e)
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
        except Exception as e:
            log.error("Failed to load session {}: {}", path, e)
            return False
