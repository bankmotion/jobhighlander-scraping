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
    enable_remoteok: bool = False
    enable_himalayas: bool = False

    # ── Indeed ──
    indeed_search_url: str = "https://www.indeed.com/q-us-remote-jobs.html"
    indeed_session_file: str = str(BASE_DIR / "sessions" / "indeed_session.json")
    # Global scrape limits (apply to every scraper). Prefer bounding by recency:
    max_jobs: int = 0  # 0 = no count cap (scrape all, bounded by max_age_days + pagination)
    max_age_days: int = 7  # only keep jobs posted within N days (0 = no age limit)
    indeed_max_pages: int = 10  # paginate the results (&start=N); stops early if exhausted / too old
    fetch_descriptions: bool = True
    capture_apply_url: bool = True

    # ── Glassdoor (own Chrome profile; probes proxy for a challenge-capable exit IP) ──
    glassdoor_search_url: str = "https://www.glassdoor.com/Job/united-states-remote-software-engineer-jobs-SRCH_IL.0,13_IN1_KO14,38.htm"
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

    # ── WeWorkRemotely (public HTTP, no login/browser; writes to jobs_temp until promoted).
    #    The category page (not the /search endpoint) carries the apply-exposed
    #    employers; big-name "senior" search results are login-gated & get skipped. ──
    weworkremotely_search_url: str = "https://weworkremotely.com/remote-software-developer-jobs"
    weworkremotely_use_proxy: bool = True
    weworkremotely_max_per_company: int = 3

    # ── RemoteOK (public JSON API + browser apply-URL resolution; jobs_temp until promoted) ──
    remoteok_api_url: str = "https://remoteok.com/api?order_by=date"
    remoteok_max_age_days: int = 7
    remoteok_role_regex: str = r"engineer|developer|software|programmer|full.?stack|back.?end|front.?end|devops"
    remoteok_user_data_dir: str = str(BASE_DIR / "sessions" / "remoteok-chrome-profile")

    # ── Himalayas (public JSON API; no login/browser; jobs_temp until promoted) ──
    himalayas_api_url: str = "https://himalayas.app/jobs/api"
    himalayas_country: str = "United States"  # matches the API's locationRestrictions
    himalayas_role_regex: str = r"engineer|developer|software|programmer"

    # ── Scheduler (random gap between runs, hours) ──
    schedule_min_hours: float = 1.0
    schedule_max_hours: float = 3.0

    log_level: str = "INFO"


settings = Settings()


# ── DB-managed settings (super-admins edit these via the scraper_settings table).
# They override the .env/code defaults above; everything else (db_*, secrets)
# stays .env-only. Add a key here to surface it in the admin UI. ──
DB_MANAGED_KEYS: tuple = (
    "max_jobs", "max_age_days", "proxy_url", "fetch_descriptions",
    "enable_indeed", "indeed_search_url", "indeed_max_pages",
    "enable_glassdoor", "glassdoor_search_url",
    "enable_jobright", "jobright_recommend_url", "jobright_recommend_api",
    "enable_weworkremotely", "weworkremotely_search_url", "weworkremotely_use_proxy",
    "weworkremotely_max_per_company",
    "enable_remoteok", "remoteok_api_url",
    "enable_himalayas", "himalayas_api_url", "himalayas_country", "himalayas_role_regex",
)


def _fmt(val) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    return "" if val is None else str(val)


def _coerce(current, raw: str):
    if isinstance(current, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return current
    return raw


def sync_settings_from_db() -> None:
    """Seed missing DB-managed keys from the current settings, then override the
    settings with the DB values (precedence: DB > .env > code default). Called at
    scraper startup; silently no-ops if the DB/table isn't reachable."""
    import pymysql

    try:
        conn = pymysql.connect(
            host=settings.db_host, port=settings.db_port, user=settings.db_user,
            password=settings.db_password, database=settings.db_name,
            charset="utf8mb4", autocommit=True,
        )
    except Exception:
        return
    rows = []
    try:
        with conn.cursor() as cur:
            for key in DB_MANAGED_KEYS:  # seed missing keys (never clobbers edits)
                cur.execute(
                    "INSERT IGNORE INTO scraper_settings (`key`, `value`, `updated_at`) "
                    "VALUES (%s, %s, UTC_TIMESTAMP(3))",
                    (key, _fmt(getattr(settings, key, None))),
                )
            cur.execute("SELECT `key`, `value` FROM scraper_settings")
            rows = cur.fetchall()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    for key, raw in rows:
        if key in DB_MANAGED_KEYS and hasattr(settings, key):
            try:
                setattr(settings, key, _coerce(getattr(settings, key), raw))
            except Exception:
                pass
