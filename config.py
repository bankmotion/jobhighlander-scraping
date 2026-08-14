"""Typed application settings (pydantic-settings).

Defaults live here; anything environment-specific or secret is overridden via
`.env` (each field auto-binds to its UPPERCASE name, e.g. `PROXY_URL`).
"""
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    # ── Database (local XAMPP MariaDB, shared with the backend) ──
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "jobhighlander"

    # ── Browser / stealth ──
    headless: bool = False
    nav_timeout_ms: int = 60_000
    user_data_dir: str = str(BASE_DIR / "sessions" / "chrome-profile")
    user_agent: Optional[str] = None
    timezone: Optional[str] = None  # None = inherit OS tz (mismatch is a CF signal)
    proxy_url: Optional[str] = None  # http://user:pass@host:port; None = direct
    proxy_bypass: str = "*.google.com,*.gstatic.com,*.googleusercontent.com,*.googleapis.com"
    click_turnstile: bool = True
    warmup: bool = True

    # ── Human-like pacing (seconds) ──
    min_action_delay: float = 0.6
    max_action_delay: float = 2.2
    min_page_delay: float = 2.5
    max_page_delay: float = 6.0

    # ── Google / Gmail sign-in ──
    google_email: Optional[str] = None
    google_password: Optional[str] = None
    google_session_file: str = str(BASE_DIR / "sessions" / "google_session.json")

    # ── Enabled scrapers (gate `main.py all` / scheduler; naming a site runs it regardless) ──
    enable_indeed: bool = True
    enable_glassdoor: bool = False
    enable_jobright: bool = True
    enable_weworkremotely: bool = False

    # ── Indeed ──
    indeed_search_url: str = "https://www.indeed.com/q-us-remote-jobs.html"
    indeed_session_file: str = str(BASE_DIR / "sessions" / "indeed_session.json")
    max_jobs: int = 25
    fetch_descriptions: bool = True
    capture_apply_url: bool = True

    # ── Glassdoor (own Chrome profile; probes proxy for a challenge-capable exit IP) ──
    glassdoor_search_url: str = "https://www.glassdoor.com/Job/software-engineer-jobs-SRCH_KO0,17.htm"
    glassdoor_session_file: str = str(BASE_DIR / "sessions" / "glassdoor_session.json")
    glassdoor_user_data_dir: str = str(BASE_DIR / "sessions" / "glassdoor-chrome-profile")
    glassdoor_proxy_url: Optional[str] = None
    glassdoor_proxy_session_file: str = str(BASE_DIR / "sessions" / "glassdoor_proxy_session.txt")

    # ── JobRight (login-gated personalized recommend feed via Google sign-in) ──
    jobright_recommend_url: str = "https://jobright.ai/jobs/recommend"
    jobright_recommend_api: str = "https://jobright.ai/swan/recommend/list/jobs"
    jobright_session_file: str = str(BASE_DIR / "sessions" / "jobright_session.json")
    jobright_user_data_dir: str = str(BASE_DIR / "sessions" / "jobright-chrome-profile")
    jobright_proxy_url: Optional[str] = None

    # ── WeWorkRemotely (public HTTP, no login/browser; writes to jobs_temp until promoted) ──
    weworkremotely_search_url: str = "https://weworkremotely.com/remote-jobs/search?term=senior+software+engineer"
    weworkremotely_use_proxy: bool = True

    # ── Scheduler (random gap between runs, hours) ──
    schedule_min_hours: float = 1.0
    schedule_max_hours: float = 3.0

    log_level: str = "INFO"


settings = Settings()
