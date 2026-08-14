"""Typed application settings (pydantic-settings).

Convention (same as the reference scraper): non-secret defaults live here in
code; anything environment-specific or secret is overridden via `.env`.
"""
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    # ── Database (defaults match the local XAMPP MariaDB the backend uses) ──
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "jobhighlander"

    # ── Browser / stealth ──
    headless: bool = False
    nav_timeout_ms: int = 60_000
    # Persistent Chrome profile dir — the "perfect browser": cookies + fingerprint
    # survive across runs so we look like a returning human, not a fresh bot.
    user_data_dir: str = str(BASE_DIR / "sessions" / "chrome-profile")
    user_agent: Optional[str] = None
    # None = inherit the real OS timezone (forcing one that mismatches the IP's
    # geolocation is a Cloudflare bot signal).
    timezone: Optional[str] = None
    # None = direct IP. Format: http://user:pass@host:port
    proxy_url: Optional[str] = None
    # Hosts that bypass the proxy (go direct). Chrome can't tunnel Google
    # through this proxy (HTTP/2 coalescing hangs), so Google OAuth goes direct
    # while Indeed still routes through the proxy to clear Cloudflare.
    proxy_bypass: str = "*.google.com,*.gstatic.com,*.googleusercontent.com,*.googleapis.com"
    # Whether to click the Cloudflare Turnstile checkbox (vs. wait-only).
    click_turnstile: bool = True
    # Visit the site root before the target URL to bank a cf_clearance cookie.
    warmup: bool = True

    # ── Human-like pacing (seconds) ──
    min_action_delay: float = 0.6
    max_action_delay: float = 2.2
    min_page_delay: float = 2.5
    max_page_delay: float = 6.0

    # ── Google / Gmail sign-in (Indeed only supports "Continue with Google") ──
    google_email: Optional[str] = None
    google_password: Optional[str] = None
    google_session_file: str = str(BASE_DIR / "sessions" / "google_session.json")

    # ── Enabled scrapers (gate what `main.py all` and the scheduler run).
    #    Explicitly naming a site on the CLI runs it regardless (for testing). ──
    enable_indeed: bool = True
    enable_glassdoor: bool = False  # off: needs a proxy IP that can reach brunhild
    enable_jobright: bool = True

    # ── Indeed scraper ──
    indeed_search_url: str = "https://www.indeed.com/q-us-remote-jobs.html"
    indeed_session_file: str = str(BASE_DIR / "sessions" / "indeed_session.json")
    max_jobs: int = 25
    fetch_descriptions: bool = True
    # For "Apply with Indeed" jobs, click the button to capture the smartapply
    # URL it opens (they have no static apply link).
    capture_apply_url: bool = True

    # ── Glassdoor scraper (writes to the live `jobs` table, site='glassdoor') ──
    glassdoor_search_url: str = "https://www.glassdoor.com/Job/software-engineer-jobs-SRCH_KO0,17.htm"
    glassdoor_session_file: str = str(BASE_DIR / "sessions" / "glassdoor_session.json")
    # Its own Chrome profile so it never contends with the Indeed scheduler for
    # the (single-owner) persistent profile lock.
    glassdoor_user_data_dir: str = str(BASE_DIR / "sessions" / "glassdoor-chrome-profile")
    # Optional dedicated upstream proxy for Glassdoor. None → reuse PROXY_URL but
    # probe its sticky session for a challenge-capable exit IP at startup.
    glassdoor_proxy_url: Optional[str] = None
    # Remembers the last challenge-capable sticky session so the exit IP stays
    # stable across runs (keeps Cloudflare's cf_clearance valid).
    glassdoor_proxy_session_file: str = str(BASE_DIR / "sessions" / "glassdoor_proxy_session.txt")

    # ── JobRight scraper (writes to live `jobs`, site='jobright'; login-gated,
    #    personalized recommendation feed via Google sign-in) ──
    jobright_recommend_url: str = "https://jobright.ai/jobs/recommend"
    # Internal paginated JSON feed the recommend page calls (position/count).
    jobright_recommend_api: str = "https://jobright.ai/swan/recommend/list/jobs"
    jobright_session_file: str = str(BASE_DIR / "sessions" / "jobright_session.json")
    jobright_user_data_dir: str = str(BASE_DIR / "sessions" / "jobright-chrome-profile")
    jobright_proxy_url: Optional[str] = None

    # ── Scheduler (random gap between runs) ──
    schedule_min_hours: float = 1.0
    schedule_max_hours: float = 3.0

    log_level: str = "INFO"


settings = Settings()
