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
    enable_findmyremote: bool = True
    enable_jobicy: bool = True
    enable_themuse: bool = True
    enable_linkedin: bool = True

    # ── Per-site proxy routing ──
    # Whether each site's traffic goes through `proxy_url` (the shared IPRoyal
    # residential upstream). Per site because the trade-off differs: Indeed and
    # Glassdoor NEED a residential exit to clear Cloudflare, while a public JSON
    # API works fine direct and only pays the proxy's latency and bandwidth for
    # nothing. Read through `proxy_for()` below, never by touching these flags.
    #
    # Defaults preserve the behaviour each scraper had before the toggles
    # existed: everything routed through the proxy except RemoteOK, which was
    # pinned direct in code.
    indeed_use_proxy: bool = True
    glassdoor_use_proxy: bool = True
    jobright_use_proxy: bool = True
    weworkremotely_use_proxy: bool = True
    remoteok_use_proxy: bool = False
    himalayas_use_proxy: bool = True
    findmyremote_use_proxy: bool = True
    jobicy_use_proxy: bool = True
    themuse_use_proxy: bool = True
    linkedin_use_proxy: bool = True

    # ── Indeed ──
    indeed_search_url: str = "https://www.indeed.com/q-us-remote-jobs.html"
    indeed_session_file: str = str(BASE_DIR / "sessions" / "indeed_session.json")
    # Indeed fronts its results with a Cloudflare Turnstile whose verification
    # backend 504s on a good half of the residential exit IPs — the widget then
    # never validates no matter how well we click it. Pre-flight for an exit that
    # reaches it (own session, isolated from Glassdoor's) and remember it.
    indeed_proxy_url: Optional[str] = None
    indeed_proxy_session_file: str = str(BASE_DIR / "sessions" / "indeed_proxy_session.txt")
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
    weworkremotely_max_per_company: int = 3

    # ── RemoteOK (public JSON API + browser apply-URL resolution; jobs_temp until promoted) ──
    remoteok_api_url: str = "https://remoteok.com/api?order_by=date"
    remoteok_max_age_days: int = 7
    remoteok_role_regex: str = r"engineer|developer|software|programmer|full.?stack|back.?end|front.?end|devops"
    remoteok_user_data_dir: str = str(BASE_DIR / "sessions" / "remoteok-chrome-profile")

    # ── Himalayas (public JSON API; browser only for apply-URL resolution) ──
    himalayas_api_url: str = "https://himalayas.app/jobs/api"
    himalayas_country: str = "United States"  # matches the API's locationRestrictions
    himalayas_role_regex: str = r"engineer|developer|software|programmer"
    himalayas_resolve_apply: bool = True  # 2nd pass: browser resolves real employer apply URLs

    # ── FindMyRemote (public JSON API; apply URL is the employer's own ATS link) ──
    #    Store the normal browsable link — its query string is forwarded to the API.
    findmyremote_search_url: str = (
        "https://findmyremote.ai/jobs?employmentType=fulltime&employmentType=parttime&location=us")
    findmyremote_role_regex: str = r"engineer|developer|software|programmer"  # "" = every role

    # ── LinkedIn (public "guest" endpoints; no login, no browser) ──
    #    Store the normal browsable search link — its filters (keywords, geoId,
    #    f_TPR, …) are forwarded to the guest endpoint verbatim.
    linkedin_search_url: str = (
        "https://www.linkedin.com/jobs/remote-software-engineer-jobs"
        "?keywords=Remote%20Software%20Engineer&location=United%20States"
        "&geoId=103644278&f_TPR=r604800")
    linkedin_role_regex: str = r"engineer|developer|software|programmer"  # "" = every role
    #: Guest endpoints throttle to a 429 if hit hard, and a pass costs one detail
    #: request per job, so pace it.
    linkedin_delay_s: float = 1.5

    # ── Jobicy (real browser: the employer link only appears after clicking
    #    "Apply Now"). Cloudflare here rate-limits hard, hence the pacing knobs. ──
    jobicy_search_url: str = (
        "https://jobicy.com/jobs?search_region=usa"
        "&filter_job_industry%5B%5D=software-engineering"
        "&filter_job_type%5B%5D=contract&filter_job_type%5B%5D=full-time"
        "&filter_job_type%5B%5D=part-time"
        "&filter_job_level%5B%5D=junior&filter_job_level%5B%5D=midweight"
        "&filter_job_level%5B%5D=senior"
        "&filter_by_day_check=on&filter_by_day=7")
    jobicy_role_regex: str = ""          # "" = keep every role the listing returns
    jobicy_delay_s: float = 8.0          # pause between job pages — do NOT lower
    jobicy_cdp_port: int = 9224

    # ── The Muse (real browser; the API never yields the employer's apply URL).
    #    The search URL does the work: `date-posted/last_7d` filters server-side
    #    and `?page=N` paginates. NOTE the slug is `remote-flexible` — the
    #    reversed form is routable but silently matches 0 jobs. ──
    themuse_search_url: str = (
        "https://www.themuse.com/search/location/remote-flexible/keyword/software-engineering/date-posted/last_7d")
    themuse_us_only: bool = True   # drop postings whose hidden city is outside the US
    themuse_role_regex: str = ""      # "" = keep every role the listing returns
    themuse_delay_s: float = 6.0      # pause between job pages
    themuse_cdp_port: int = 9225      # own port so runs can't clash          # own port so it can't clash with other runs

    # ── Scheduler (random gap between runs, hours) ──
    schedule_min_hours: float = 1.0
    schedule_max_hours: float = 3.0

    log_level: str = "INFO"

settings = Settings()

# ── DB-managed settings (super-admins edit these via the scraper_settings table).
# They override the .env/code defaults above; everything else (db_*, secrets)
# stays .env-only. Add a key here to surface it in the admin UI. ──
DB_MANAGED_KEYS: tuple = (
    # Gap between scheduler cycles, re-read every cycle — see scheduler.py.
    "schedule_min_hours", "schedule_max_hours",
    "max_jobs", "max_age_days", "proxy_url", "fetch_descriptions",
    "enable_indeed", "indeed_search_url", "indeed_max_pages",
    "enable_glassdoor", "glassdoor_search_url",
    "enable_jobright", "jobright_recommend_url", "jobright_recommend_api",
    "enable_weworkremotely", "weworkremotely_search_url",
    "weworkremotely_max_per_company",
    "enable_remoteok", "remoteok_api_url",
    "enable_himalayas", "himalayas_api_url", "himalayas_country", "himalayas_role_regex",
    "himalayas_resolve_apply",
    "enable_findmyremote", "findmyremote_search_url", "findmyremote_role_regex",
    
    "enable_jobicy", "jobicy_search_url", "jobicy_role_regex",
    "jobicy_delay_s",
    "enable_themuse", "themuse_search_url", "themuse_us_only", "themuse_role_regex",
    "themuse_delay_s",
    "enable_linkedin", "linkedin_search_url", "linkedin_role_regex", "linkedin_delay_s",
    # Per-site proxy routing (see `proxy_for`).
    "indeed_use_proxy", "glassdoor_use_proxy", "jobright_use_proxy",
    "weworkremotely_use_proxy", "remoteok_use_proxy", "himalayas_use_proxy",
    "findmyremote_use_proxy", "jobicy_use_proxy", "themuse_use_proxy",
    "linkedin_use_proxy",
)


def site_uses_proxy(site: str) -> bool:
    """Whether `site` is configured to route through the shared proxy.

    Unknown sites default to True, matching how every scraper behaved before
    these toggles existed — a new scraper is proxied until someone says not to.
    """
    return bool(getattr(settings, f"{site}_use_proxy", True))


def proxy_for(site: str) -> str:
    """The upstream proxy URL `site` should use, or "" to go direct.

    Returns "" and NOT None on purpose. None already means something else here:
    `StealthBrowser` treats a None proxy as "fall back to settings.proxy_url",
    so returning it for a site whose toggle is OFF would silently proxy the
    site anyway. Only a non-None falsy value actually disables it — the same
    idiom RemoteOK used when it pinned `proxy_url = ""` in code.

    Call this instead of reading `settings.proxy_url` directly; a scraper that
    reads the global is a toggle that does nothing.
    """
    if not site_uses_proxy(site):
        return ""
    return settings.proxy_url or ""

def _fmt(val) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    return "" if val is None else str(val)

def _coerce(current, raw: str):
    """Parse a stored string back to the type the setting already has.

    Order matters: bool before int, because `isinstance(True, int)` is True in
    Python and a checkbox would otherwise come back as 0/1.

    Floats are handled explicitly. Without this branch every float setting fell
    through to `return raw` and came back as a STRING, so `random.uniform()` or
    `asyncio.sleep()` on it raises. The existing float settings only survived
    because each call site happened to wrap them in `float(...)` itself — a
    per-site workaround that the next float setting would not inherit.
    """
    if isinstance(current, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return current
    if isinstance(current, float):
        try:
            return float(str(raw).strip())
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
