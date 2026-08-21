"""Local split-tunnel proxy.

Chrome cannot tunnel Google domains through the authenticated IPRoyal HTTP proxy
(the connection hangs — curl works, Chrome doesn't), and Playwright's proxy
option ignores any bypass list. So we run a tiny localhost proxy that Chrome
points at WITHOUT auth, and it decides per-host:

  * Google domains (`direct_suffixes`)  -> connect DIRECT (no upstream)
  * everything else (Indeed, etc.)      -> chain to the IPRoyal upstream,
                                           adding Proxy-Authorization here.

Handles HTTP CONNECT (all HTTPS) and plain-HTTP absolute-URI requests.
"""
from __future__ import annotations

import asyncio
import base64
import random
import re
import socket
import string
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

from logger import log

# Cloudflare serves the Turnstile verification backend from a regional shard
# (e.g. brunhild.challenges.cloudflare.com). Some residential exit IPs 504 when
# tunnelling to it, which silently breaks the challenge → a hard "Humans only"
# block. We probe for an exit that can actually reach it before scraping.
CHALLENGE_PROBE_HOST = "brunhild.challenges.cloudflare.com"

#: Fresh sticky sessions to try when retiring a challenge-blocked exit IP.
_ROTATE_ATTEMPTS = 8


def with_session(proxy_url: str, session: str) -> str:
    """Swap the IPRoyal sticky-session token in a proxy URL (→ a different IP)."""
    return re.sub(r"session-[^_@:/]+", f"session-{session}", proxy_url)


def _tunnel_ok(proxy_url: str, host: str, port: int = 443, timeout: float = 12.0) -> bool:
    """True if the upstream proxy can establish a CONNECT tunnel to host:port
    (i.e. it answers 200, not 504). Synchronous — used for pre-flight probing."""
    u = urlparse(proxy_url)
    if not u.hostname or not u.port:
        return False
    s = None
    try:
        s = socket.create_connection((u.hostname, u.port), timeout=timeout)
        s.settimeout(timeout)
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        if u.username:
            auth = base64.b64encode(f"{u.username}:{u.password or ''}".encode()).decode()
            req += f"Proxy-Authorization: Basic {auth}\r\n"
        req += "Proxy-Connection: Keep-Alive\r\n\r\n"
        s.sendall(req.encode())
        resp = s.recv(256)
        return b" 200 " in resp or resp.startswith(b"HTTP/1.1 200")
    except Exception:
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def session_of(proxy_url: str) -> Optional[str]:
    """Extract the sticky-session token from a proxy URL, or None."""
    m = re.search(r"session-([^_@:/]+)", proxy_url or "")
    return m.group(1) if m else None


def pick_challenge_capable_proxy(
    base_url: str,
    attempts: int = 8,
    host: str = CHALLENGE_PROBE_HOST,
    preferred_session: Optional[str] = None,
    prefix: str = "gd",
) -> str:
    """Return a proxy URL whose exit IP can tunnel to the Cloudflare challenge
    host. Tries a previously-good session first (stable IP keeps cf_clearance
    valid), then the base session, then fresh sticky sessions (each a new exit
    IP). Falls back to `base_url` if none pass (the proxy's own CONNECT retries
    may still recover)."""
    if not base_url:
        return base_url

    # A remembered good session first — a stable IP preserves cf_clearance.
    if preferred_session and preferred_session != session_of(base_url):
        pref = with_session(base_url, preferred_session)
        if _tunnel_ok(pref, host):
            log.info("Proxy pre-flight: remembered session {!r} still reaches {} [OK]", preferred_session, host)
            return pref
        log.info("Proxy pre-flight: remembered session {!r} no longer reaches {}", preferred_session, host)

    # The base session as-is.
    if _tunnel_ok(base_url, host):
        log.info("Proxy pre-flight: base session reaches {} [OK]", host)
        return base_url

    for i in range(attempts):
        session = prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        candidate = with_session(base_url, session)
        if _tunnel_ok(candidate, host):
            log.info("Proxy pre-flight: session {!r} reaches {} [OK] (try {}/{})", session, host, i + 1, attempts)
            return candidate
        log.info("Proxy pre-flight: session {!r} cannot reach {} (try {}/{})", session, host, i + 1, attempts)
    log.warning("Proxy pre-flight: no exit IP reached {} in {} tries — using base session.", host, attempts)
    return base_url


def remembered_challenge_proxy(base_url: str, session_file: str, prefix: str = "gd") -> str:
    """`pick_challenge_capable_proxy` backed by a remembered-session file.

    Probing costs a few seconds and lands on a different exit IP each time, so
    we persist whichever sticky session worked and try it first on the next run
    — a stable IP is what keeps the cf_clearance cookie in the Chrome profile
    valid instead of re-triggering the challenge every time."""
    if not base_url:
        return base_url
    path = Path(session_file)
    try:
        preferred = path.read_text(encoding="utf-8").strip() or None
    except Exception:
        preferred = None
    chosen = pick_challenge_capable_proxy(base_url, preferred_session=preferred, prefix=prefix)
    session = session_of(chosen)
    if session:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(session, encoding="utf-8")
        except Exception:
            pass
    return chosen


def rotate_challenge_proxy(base_url: str, session_file: str, prefix: str = "gd") -> str:
    """Retire the remembered sticky session and pin a DIFFERENT exit IP.

    `remembered_challenge_proxy` deliberately re-pins whichever session last
    worked, because a stable IP is what keeps cf_clearance valid. Its only
    health check is `_tunnel_ok` — "can this exit still open a CONNECT tunnel to
    the verification shard" — and that is a different question from "is this
    exit still trusted". When Cloudflare starts scoring a residential exit as a
    bot, the probe keeps reporting [OK], so the remembered file pins the scraper
    to a dead IP on every subsequent run. Indeed returned 0 jobs for hours that
    way while the pre-flight logged success each time.

    Call this when the CHALLENGE failed, as opposed to the tunnel.
    """
    if not base_url:
        return base_url
    path = Path(session_file)
    try:
        burned = path.read_text(encoding="utf-8").strip() or None
    except Exception:
        burned = None

    # Skip the burned session AND the base one — we want an exit we have not
    # just been refused on, and the base session may be the flagged one.
    avoid = {burned, session_of(base_url)}
    for i in range(_ROTATE_ATTEMPTS):
        session = prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        if session in avoid:
            continue
        candidate = with_session(base_url, session)
        if _tunnel_ok(candidate, CHALLENGE_PROBE_HOST):
            log.info("Proxy rotate: retired {!r}, pinned fresh session {!r} (try {}/{})",
                     burned, session, i + 1, _ROTATE_ATTEMPTS)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(session, encoding="utf-8")
            except Exception:
                pass
            return candidate

    # Nothing fresh answered. Still forget the burned token, so the next run
    # re-probes from scratch instead of preferring the exit that just failed.
    log.warning("Proxy rotate: no fresh exit reached {} in {} tries — forgetting {!r} anyway.",
                CHALLENGE_PROBE_HOST, _ROTATE_ATTEMPTS, burned)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    return base_url


class LocalRoutingProxy:
    def __init__(self, upstream_url: str, direct_suffixes: Iterable[str], host: str = "127.0.0.1"):
        u = urlparse(upstream_url)
        self.up_host = u.hostname
        self.up_port = u.port
        self.up_auth: Optional[str] = None
        if u.username:
            raw = f"{u.username}:{u.password or ''}".encode()
            self.up_auth = base64.b64encode(raw).decode()
        # Normalise suffixes: strip wildcards/leading dots -> bare "google.com".
        self.direct = tuple(s.strip().lstrip("*").lstrip(".") for s in direct_suffixes if s.strip())
        self.host = host
        self.port: Optional[int] = None
        self._server: Optional[asyncio.AbstractServer] = None
        # Live connection-handler tasks, so stop() can cancel any that are
        # blocked on a half-open upstream read instead of hanging wait_closed().
        self._conns: set[asyncio.Task] = set()

    def _is_direct(self, host: str) -> bool:
        h = (host or "").lower()
        return any(h == d or h.endswith("." + d) for d in self.direct)

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("Local routing proxy on 127.0.0.1:{} (direct: {})", self.port, ", ".join(self.direct))
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            # Cancel in-flight tunnels first — a copy loop parked on a half-open
            # upstream read would otherwise keep wait_closed() (and the whole
            # run) hanging on Windows.
            for t in list(self._conns):
                t.cancel()
            self._conns.clear()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)
            except Exception:
                pass
            self._server = None

    async def _handle(self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._conns.add(task)
        try:
            request_line = await creader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin1", "ignore").split()
            if len(parts) < 3:
                return
            method, target, version = parts[0], parts[1], parts[2]

            header_lines = []
            while True:
                line = await creader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
                header_lines.append(line)
            headers = b"".join(header_lines)

            if method.upper() == "CONNECT":
                host, _, port_s = target.partition(":")
                await self._connect(host, int(port_s or 443), creader, cwriter)
            else:
                await self._http_forward(method, target, version, headers, creader, cwriter)
        except Exception:
            pass
        finally:
            if task is not None:
                self._conns.discard(task)
            try:
                cwriter.close()
            except Exception:
                pass

    async def _open_upstream(self, host: str, port: int, retries: int = 3):
        """Return (reader, writer) tunneled to host:port — direct or via upstream.

        Residential upstreams intermittently answer CONNECT with a 5xx
        (e.g. 504 Gateway Timeout) for a host they *can* reach — a fresh CONNECT
        usually succeeds. So we retry the upstream tunnel a few times before
        giving up (this is what lets Cloudflare's challenge subresources load)."""
        if self._is_direct(host) or not self.up_host:
            return await asyncio.open_connection(host, port)

        last_status = b""
        for attempt in range(retries):
            try:
                r, w = await asyncio.open_connection(self.up_host, self.up_port)
            except Exception:
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
            req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if self.up_auth:
                req += f"Proxy-Authorization: Basic {self.up_auth}\r\n"
            req += "Proxy-Connection: Keep-Alive\r\n\r\n"
            w.write(req.encode())
            await w.drain()
            status = await r.readline()
            if b" 200 " in status or status.startswith(b"HTTP/1.1 200"):
                while True:  # consume the rest of the CONNECT response headers
                    line = await r.readline()
                    if not line or line in (b"\r\n", b"\n"):
                        break
                if attempt:
                    log.info("upstream CONNECT to {}:{} ok on retry {}", host, port, attempt)
                return r, w
            last_status = status.strip()
            try:
                w.close()
            except Exception:
                pass
            await asyncio.sleep(0.3 * (attempt + 1))
        raise ConnectionError(f"upstream CONNECT rejected after {retries} tries: {last_status!r}")

    async def _connect(self, host, port, creader, cwriter):
        try:
            ur, uw = await self._open_upstream(host, port)
        except Exception as e:
            log.warning("tunnel to {}:{} failed: {}", host, port, e)
            cwriter.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            try:
                await cwriter.drain()
            except Exception:
                pass
            return
        cwriter.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await cwriter.drain()
        await self._pipe(creader, cwriter, ur, uw)

    async def _http_forward(self, method, target, version, headers, creader, cwriter):
        u = urlparse(target)
        host, port = u.hostname, (u.port or 80)
        if not host:
            return
        if self._is_direct(host) or not self.up_host:
            ur, uw = await asyncio.open_connection(host, port)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            head = f"{method} {path} {version}\r\n".encode() + headers + b"\r\n"
        else:
            ur, uw = await asyncio.open_connection(self.up_host, self.up_port)
            auth = f"Proxy-Authorization: Basic {self.up_auth}\r\n".encode() if self.up_auth else b""
            head = f"{method} {target} {version}\r\n".encode() + auth + headers + b"\r\n"
        uw.write(head)
        await uw.drain()
        await self._pipe(creader, cwriter, ur, uw)

    @staticmethod
    async def _pipe(cr, cw, ur, uw):
        async def copy(r, w):
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except Exception:
                pass
            finally:
                try:
                    w.close()
                except Exception:
                    pass

        await asyncio.gather(copy(cr, uw), copy(ur, cw), return_exceptions=True)


def verify_proxy(timeout: int = 25) -> tuple:
    """Check the upstream proxy actually works. Returns (ok, detail).

    Run at the start of every scrape cycle: if the residential proxy is down or
    the credentials have lapsed, the scrapers would silently fall back to the
    server's own datacenter IP — which gets the sites' Cloudflare tiers angry and
    quietly poisons a run. Better to stop.
    """
    from config import settings
    if not settings.proxy_url:
        return False, "PROXY_URL is not set"
    try:
        from curl_cffi import requests as creq
    except Exception as e:  # pragma: no cover
        return False, f"curl_cffi unavailable: {e}"
    px = {"http": settings.proxy_url, "https": settings.proxy_url}
    try:
        direct = creq.get("https://api.ipify.org?format=json", timeout=timeout).json().get("ip")
    except Exception:
        direct = None
    try:
        r = creq.get("https://api.ipify.org?format=json", proxies=px, timeout=timeout)
        exit_ip = r.json().get("ip")
    except Exception as e:
        return False, f"proxy request failed: {str(e)[:90]}"
    if not exit_ip:
        return False, "proxy returned no IP"
    if direct and exit_ip == direct:
        # Traffic isn't actually leaving through the proxy.
        return False, f"exit IP {exit_ip} equals the direct IP — proxy not in use"
    return True, f"exit IP {exit_ip}" + (f" (direct {direct})" if direct else "")
