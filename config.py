import math
import os
import warnings
from pathlib import Path
from typing import Mapping
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
TESTING = os.getenv("AVITO_RADAR_TESTING") == "1"
if not TESTING:
    load_dotenv(BASE_DIR / ".env")

DEPRECATED_ENV_VARS = (
    "CHECK_INTERVAL",
    "AVITO_REQUESTS_PER_SECOND",
    "MAX_CONCURRENT_WORKERS",
    "AVITO_MAX_IN_FLIGHT",
    "SCAN_MAX_INTERVAL",
    "PARSE_WORKERS",
    "TARIFF_DELAY_PRO",
    "TARIFF_DELAY_STANDART",
    "TARIFF_DELAY_LITE",
    "TARIFF_DELAY_BASE",
)


def check_deprecated_env_vars(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    messages: list[str] = []
    for name in DEPRECATED_ENV_VARS:
        if name in env and str(env.get(name, "")).strip() != "":
            msg = (
                f"⚠️ Устаревшая переменная окружения {name}={env[name]!r} игнорируется; "
                "используйте параметры пула прокси и планировщика (SCAN_INTERVAL_*, PROXY_*)."
            )
            messages.append(msg)
            warnings.warn(msg, DeprecationWarning, stacklevel=2)
    return messages


def positive_float_setting(name: str, default: str, environ: Mapping[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Параметр {name} должен быть числом") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"Параметр {name} должен быть положительным конечным числом")
    return value


def non_negative_float_setting(name: str, default: str, environ: Mapping[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Параметр {name} должен быть числом") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Параметр {name} должен быть неотрицательным конечным числом")
    return value


def positive_int_setting(name: str, default: str, environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Параметр {name} должен быть целым числом") from exc
    if value <= 0:
        raise ValueError(f"Параметр {name} должен быть положительным целым числом (> 0)")
    return value


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# Bot look: "clean" (own monochrome icons, button colour = meaning; default since 2026-11-08) or "emoji"
# (the former colour emoji on buttons and in texts).
UI_THEME = (os.getenv("UI_THEME", "clean").strip().lower() or "clean")
MAX_SEARCHES_PER_USER = positive_int_setting("MAX_SEARCHES_PER_USER", "50")
# Admins may paste up to this many search links in one message.
BULK_RADAR_MAX = positive_int_setting("BULK_RADAR_MAX", "50")

# --- Планировщик и интервалы ---
SCAN_INTERVAL_MIN = positive_float_setting("SCAN_INTERVAL_MIN", "5.0")
SCAN_INTERVAL_MAX = positive_float_setting("SCAN_INTERVAL_MAX", "10.0")
SCAN_CAPACITY_MODE_WINDOWS = positive_int_setting("SCAN_CAPACITY_MODE_WINDOWS", "2")
PROXY_INITIAL_HTTP_COST_PER_SCAN = positive_float_setting("PROXY_INITIAL_HTTP_COST_PER_SCAN", "1.3")
HTTP_COST_MIN_SAMPLES = positive_int_setting("HTTP_COST_MIN_SAMPLES", "30")
SOURCE_BACKOFF_BASE = positive_float_setting("SOURCE_BACKOFF_BASE", "5.0")
SOURCE_BACKOFF_MAX = positive_float_setting("SOURCE_BACKOFF_MAX", "20.0")
SOURCE_RECOVERY_SUCCESSES = positive_int_setting("SOURCE_RECOVERY_SUCCESSES", "1")
SOURCE_SLOW_AFTER = positive_int_setting("SOURCE_SLOW_AFTER", "3")
SOURCE_SLOW_MAX_FACTOR = positive_int_setting("SOURCE_SLOW_MAX_FACTOR", "5")
SOURCE_DEAD_ALERT_STREAK = positive_int_setting("SOURCE_DEAD_ALERT_STREAK", "8")
SCAN_PROXY_ATTEMPTS = positive_int_setting("SCAN_PROXY_ATTEMPTS", "3")

# --- Прокси и HTTP ---
PROXY_POOL_FILE = (os.getenv("PROXY_POOL_FILE") or "").strip()
ALLOW_DIRECT_AVITO = os.getenv("ALLOW_DIRECT_AVITO", "1" if TESTING else "0").strip() == "1"
# Network channels for scanning: auto (proxies if any, else server IP when ALLOW_DIRECT_AVITO),
# direct (server IP only), proxy (proxies only), both (proxies and the server IP share the load),
# smart (the server IP alone while it copes, proxies in reserve; see SMART_* below).
NET_MODES = ("auto", "direct", "proxy", "both", "smart")
NET_MODE = (os.getenv("NET_MODE", "auto").strip().lower() or "auto")
if NET_MODE not in NET_MODES:
    raise ValueError(f"NET_MODE должен быть одним из: {', '.join(NET_MODES)}")
PROXY_RATE_LIMIT = positive_float_setting("PROXY_RATE_LIMIT", "1.5")
PROXY_RATE_RECOMMENDATION_MAX = positive_float_setting("PROXY_RATE_RECOMMENDATION_MAX", "2.0")
PROXY_CAPACITY_RESERVE = non_negative_float_setting("PROXY_CAPACITY_RESERVE", "0.20")
PROXY_MIN_RATE = positive_float_setting("PROXY_MIN_RATE", "0.5")
# Requests through one proxy at the same time (lanes). A page takes 2-3 s through a proxy, so one lane gave
# ~0.35 req/s per IP; two nearly double it, the rate limit and the 403 ratio limiter still hold per IP.
# The admin switches 1/2 live (bot_settings.proxy_lanes); the server channel always has one.
PROXY_MAX_IN_FLIGHT = positive_int_setting("PROXY_MAX_IN_FLIGHT", "2")
# «Start over» mark of the admin panel (epoch seconds, bot_settings.report_since): reports count from it; 0 = off.
REPORT_SINCE = 0.0
PROXY_FAILURE_THRESHOLD = positive_int_setting("PROXY_FAILURE_THRESHOLD", "3")
PROXY_BLOCK_WINDOW_SECONDS = positive_int_setting("PROXY_BLOCK_WINDOW_SECONDS", "600")
PROXY_BLOCK_RATIO_THRESHOLD = positive_float_setting("PROXY_BLOCK_RATIO_THRESHOLD", "0.05")
PROXY_BLOCK_RATIO_MIN_REQUESTS = positive_int_setting("PROXY_BLOCK_RATIO_MIN_REQUESTS", "20")
PROXY_RECOVERY_SUCCESS_STREAK = positive_int_setting("PROXY_RECOVERY_SUCCESS_STREAK", "10")
PROXY_RECOVERY_FACTOR = positive_float_setting("PROXY_RECOVERY_FACTOR", "1.25")
PROXY_QUARANTINE_BASE = positive_float_setting("PROXY_QUARANTINE_BASE", "2")
PROXY_QUARANTINE_MAX = positive_float_setting("PROXY_QUARANTINE_MAX", "15")
PROXY_TRANSPORT_BACKOFF_BASE = positive_float_setting("PROXY_TRANSPORT_BACKOFF_BASE", "2")
PROXY_TRANSPORT_BACKOFF_MAX = positive_float_setting("PROXY_TRANSPORT_BACKOFF_MAX", "2")
# Token bucket per proxy client: sustained PROXY_RATE_LIMIT req/s with bursts up to PROXY_RATE_BURST requests.
PROXY_RATE_BURST = positive_float_setting("PROXY_RATE_BURST", "3")
# Avito answers ~10-15% of requests from any IP with 403/439 as background noise. A proxy's rate is cut only
# when its share of blocks among the last PROXY_RATE_WINDOW responses exceeds PROXY_RATE_CUT_RATIO.
PROXY_RATE_WINDOW = positive_int_setting("PROXY_RATE_WINDOW", "20")
PROXY_RATE_CUT_RATIO = positive_float_setting("PROXY_RATE_CUT_RATIO", "0.25")
# One cut multiplies the rate by this (1.5 → 1.05 → 0.74 → 0.52); the next cut needs a fresh half window.
PROXY_RATE_CUT_FACTOR = positive_float_setting("PROXY_RATE_CUT_FACTOR", "0.7")
# A throttled proxy with no 403/429 for this long steps up by PROXY_RECOVERY_FACTOR, traffic or not.
PROXY_RATE_CALM_SECONDS = positive_float_setting("PROXY_RATE_CALM_SECONDS", "300")
# Channel choice: an extra in-flight request on a busy channel is assumed to cost this many seconds of waiting.
PROXY_BUSY_PENALTY = positive_float_setting("PROXY_BUSY_PENALTY", "1.0")
# Channel choice by measured quality: a proxy is proven when at least PROXY_GOOD_HEALTH of its last
# PROXY_HEALTH_WINDOW requests succeeded. Proven proxies rotate; the rest get load only as a spill-over.
PROXY_HEALTH_WINDOW = positive_int_setting("PROXY_HEALTH_WINDOW", "10")
PROXY_GOOD_HEALTH = positive_float_setting("PROXY_GOOD_HEALTH", "0.7")
# A proven IP stays preferred up to this rate; above it the score drops by PROXY_HOT_PENALTY and load spills over.
PROXY_PREFERRED_RATE = positive_float_setting("PROXY_PREFERRED_RATE", "0.5")
PROXY_HOT_PENALTY = positive_float_setting("PROXY_HOT_PENALTY", "0.5")
# A proven proxy that loads a catalog page faster than the pool's typical one goes first while it is cold
# (up to this score bonus, kept under PROXY_HOT_PENALTY). 0 turns speed-aware routing off.
PROXY_SPEED_BONUS = non_negative_float_setting("PROXY_SPEED_BONUS", "0.3")
# A free channel takes a scan before a busy one when at least this share of its recent answers succeeded:
# below it the channel mostly answers 403 and the scan would only lose a round trip. 0.3 (08.11 evening) let
# the server, blocked on 70 % of answers, take more scans than any proxy.
PROXY_SPILL_MIN_HEALTH = positive_float_setting("PROXY_SPILL_MIN_HEALTH", "0.6")
# After this many 403/429 in a row a channel starts from scratch: saved cookies deleted, every lane's session
# closed. On 08.11 a bot restart lifted the success share from ~55 % to 95 % for ~20 min, and a plain
# session reset after a 403 reloads the same cookies from the file. At most once per SESSION_REFRESH_GAP s.
SESSION_REFRESH_BLOCKS = positive_int_setting("SESSION_REFRESH_BLOCKS", "3")
SESSION_REFRESH_GAP = positive_float_setting("SESSION_REFRESH_GAP", "120")
# An unproven channel still gets one request per PROXY_PROBE_INTERVAL s, or per PROXY_PROBE_RETRY s right
# after a success, so that a proxy back from an outage is proven again within a minute.
PROXY_PROBE_INTERVAL = positive_float_setting("PROXY_PROBE_INTERVAL", "60")
PROXY_PROBE_RETRY = positive_float_setting("PROXY_PROBE_RETRY", "5")
# Smart mode: the server IP scans alone while the planned load stays under SMART_DIRECT_MAX_RPS and its share
# of blocks under SMART_DIRECT_BLOCK_RATIO; proxies are not probed and only take the retry after a server failure.
# Past either limit the proxies join the load as in «both» for at least SMART_HOLD_SECONDS.
SMART_DIRECT_MAX_RPS = positive_float_setting("SMART_DIRECT_MAX_RPS", "0.5")
SMART_DIRECT_BLOCK_RATIO = positive_float_setting("SMART_DIRECT_BLOCK_RATIO", "0.2")
SMART_HOLD_SECONDS = positive_float_setting("SMART_HOLD_SECONDS", "600")
# A reserve proxy is chosen by its record over this many last answers, however old they are.
PROXY_QUALITY_WINDOW = positive_int_setting("PROXY_QUALITY_WINDOW", "50")
CHALLENGE_WORKERS = positive_int_setting("CHALLENGE_WORKERS", "1")
COOKIE_SAVE_INTERVAL = non_negative_float_setting("COOKIE_SAVE_INTERVAL", "30")

AVITO_CONNECT_TIMEOUT = positive_float_setting("AVITO_CONNECT_TIMEOUT", "3.0")
AVITO_READ_TIMEOUT = positive_float_setting("AVITO_READ_TIMEOUT", "8.0")
# Not a single byte for this long = a dead request: dropped and retried on another channel (it held 11 s before).
AVITO_STALL_TIMEOUT = positive_float_setting("AVITO_STALL_TIMEOUT", "6.0")
AVITO_COLLECTION_DEADLINE = positive_float_setting("AVITO_COLLECTION_DEADLINE", "20.0")
AVITO_MAX_REQUESTS_PER_SCAN = positive_int_setting("AVITO_MAX_REQUESTS_PER_SCAN", "8")
AVITO_MAX_PAGES_PER_SCAN = min(5, positive_int_setting("AVITO_MAX_PAGES_PER_SCAN", "2"))
# Cache-busting (_t) for catalog GETs: "ms" (fresh ms every request), "bucket" (rotated every AVITO_CACHE_BUST_WINDOW s), or "off".
AVITO_CACHE_BUST_MODES = ("ms", "bucket", "off")
AVITO_CACHE_BUST_MODE = (os.getenv("AVITO_CACHE_BUST_MODE", "ms").strip().lower() or "ms")
if AVITO_CACHE_BUST_MODE not in AVITO_CACHE_BUST_MODES:
    raise ValueError(f"AVITO_CACHE_BUST_MODE должен быть одним из: {', '.join(AVITO_CACHE_BUST_MODES)}")
AVITO_CACHE_BUST_WINDOW = non_negative_float_setting("AVITO_CACHE_BUST_WINDOW", "15")  # used by "bucket" only
# Warm up empty cookie sessions with one homepage GET before hitting a search URL (off in offline test mode),
# at most once per AVITO_SESSION_WARMUP_GAP seconds per channel.
AVITO_SESSION_WARMUP = os.getenv("AVITO_SESSION_WARMUP", "0" if TESTING else "1").strip() == "1"
AVITO_SESSION_WARMUP_GAP = non_negative_float_setting("AVITO_SESSION_WARMUP_GAP", "600")

# --- Доставка и outbox ---
# Tariffs differ only by scan interval (TARIFF_SCAN_INTERVAL): a found lot goes out at once, with no
# per-tariff delivery delay (the old TARIFF_DELAY_* settings are gone).

# --- Тарифы ---
# Internal keys stay as stored in the DB and old orders: lite = «Базовый», standart = «Средний», pro = «Полный».
# A trial works like the Полный tariff with TRIAL_RADARS radars for TRIAL_HOURS. "base" is the classification of test orders.
# A URL watched by several tariffs is scanned at the fastest one.
PAID_PLANS: tuple[str, ...] = ("lite", "standart", "pro")
PLAN_TITLES: dict[str, str] = {"lite": "Базовый", "standart": "Средний", "pro": "Полный", "test": "Тест", "base": "Пробный", "rubtest": "Тест 1 ₽"}
PLAN_RADARS: dict[str, int] = {"lite": 1, "standart": 2, "pro": 4}
# Tariffs that also copy every lot into a group chat (the trial counts as Полный).
GROUP_DELIVERY_PLANS: frozenset[str] = frozenset({"standart", "pro"})
TRIAL_RADARS = 1
TRIAL_HOURS = 24
# The trial reminder goes out this many seconds before the end.
TRIAL_REMIND_BEFORE = 1800


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(24, "час", "часа", "часов") -> "24 часа"."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} {few}"
    return f"{n} {many}"


def hours_word(n: int) -> str:
    return plural(n, "час", "часа", "часов")


TRIAL_TEXT = hours_word(TRIAL_HOURS)
# The trial is granted for subscribing to this channel; the bot must be its admin to check that.
TRIAL_CHANNEL = os.getenv("TRIAL_CHANNEL", "@avitoradar1").strip()
TARIFF_SCAN_INTERVAL: dict[str, float] = {
    "pro": positive_float_setting("TARIFF_INTERVAL_PRO", "20"),
    "standart": positive_float_setting("TARIFF_INTERVAL_STANDART", "30"),
    "lite": positive_float_setting("TARIFF_INTERVAL_LITE", "40"),
    "base": positive_float_setting("TARIFF_INTERVAL_TRIAL", "30"),
}

NOTIFICATION_WORKERS = positive_int_setting("NOTIFICATION_WORKERS", "10")
TELEGRAM_GLOBAL_RATE = positive_float_setting("TELEGRAM_GLOBAL_RATE", "25.0")
TELEGRAM_CHAT_INTERVAL = positive_float_setting("TELEGRAM_CHAT_INTERVAL", "1.0")
# Telegram allows ~1 message/s per chat. When a chat has at least DIGEST_MIN ready notifications,
# up to DIGEST_MAX of them go out as one list message instead of a minute-long queue.
NOTIFICATION_DIGEST_MIN = positive_int_setting("NOTIFICATION_DIGEST_MIN", "4")
# Daily admin report time (MSK) and renewal reminders.
DAILY_REPORT_HOUR = 9
REMINDER_CHECK_INTERVAL = 300
# Weekly digest for paid users: MSK weekday (0 = Monday) and hour.
WEEKLY_DIGEST_WEEKDAY = 0
WEEKLY_DIGEST_HOUR = 10
# Support tickets: an answered ticket closes itself after this much silence from the user;
# a closed one can be reopened (button or reply) within SUPPORT_REOPEN_DAYS, later a new one starts.
SUPPORT_AUTOCLOSE_HOURS = 72
SUPPORT_REOPEN_DAYS = 7


def is_quiet_hour_msk(start_hour: int, end_hour: int, now_epoch: float | None = None) -> bool:
    """Whether the MSK hour falls in a user's night window [start, end); the window may cross midnight."""
    import time as _time
    hour = _time.gmtime((now_epoch if now_epoch is not None else _time.time()) + 3 * 3600).tm_hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


NOTIFICATION_DIGEST_MAX = positive_int_setting("NOTIFICATION_DIGEST_MAX", "10")
NOTIFICATION_MAX_AGE = positive_int_setting("NOTIFICATION_MAX_AGE", "1800")
NOTIFICATION_LEASE_SECONDS = positive_int_setting("NOTIFICATION_LEASE_SECONDS", "60")
NOTIFICATION_RETRY_MAX = positive_int_setting("NOTIFICATION_RETRY_MAX", "60")
NOTIFICATION_GENERIC_ATTEMPTS = positive_int_setting("NOTIFICATION_GENERIC_ATTEMPTS", "3")
NOTIFICATION_TERMINAL_RETENTION_HOURS = positive_int_setting("NOTIFICATION_TERMINAL_RETENTION_HOURS", "24")
OUTBOX_MAX_PENDING = positive_int_setting("OUTBOX_MAX_PENDING", "3000")
OUTBOX_ALERT_DEPTH = positive_int_setting("OUTBOX_ALERT_DEPTH", "200")
# Telegram sends ~1 message/s per chat, so a burst of lots always waits a few seconds; alert only on real stalls.
DELIVERY_SLA_ALERT_SECONDS = positive_float_setting("DELIVERY_SLA_ALERT_SECONDS", "30.0")
# "Catalog unavailable" alert fires only after this long without a single successful scan.
HEALTH_ALERT_AFTER_SECONDS = positive_float_setting("HEALTH_ALERT_AFTER_SECONDS", "600")

# --- БД, fanout, catch-up и метрики ---
DB_READ_POOL_SIZE = positive_int_setting("DB_READ_POOL_SIZE", "2")
FANOUT_WORKERS = positive_int_setting("FANOUT_WORKERS", "4")
FANOUT_QUEUE_SIZE = positive_int_setting("FANOUT_QUEUE_SIZE", "64")
SOURCE_SNAPSHOT_CACHE_SIZE = positive_int_setting("SOURCE_SNAPSHOT_CACHE_SIZE", "64")
SOURCE_SNAPSHOT_TTL = positive_float_setting("SOURCE_SNAPSHOT_TTL", "20")
BLACKLIST_CACHE_TTL = positive_float_setting("BLACKLIST_CACHE_TTL", "45")
FAST_SKIP_FULL_SCAN_INTERVAL = positive_float_setting("FAST_SKIP_FULL_SCAN_INTERVAL", "60")
# A live scan reads further pages only while every organic lot on the page is new (a flood on a broad radar).
BROAD_RADAR_MAX_PAGES = positive_int_setting("BROAD_RADAR_MAX_PAGES", "3")
# In-memory per-source memory of already processed item IDs (skips DB fanout when a scan brings nothing new).
SEEN_CACHE_TTL_HOURS = positive_float_setting("SEEN_CACHE_TTL_HOURS", "48")
SEEN_CACHE_MAX_PER_SOURCE = positive_int_setting("SEEN_CACHE_MAX_PER_SOURCE", "1000")
CATCHUP_MAX_AGE_MINUTES = positive_int_setting("CATCHUP_MAX_AGE_MINUTES", "30")
# The first scan after a radar is resumed delivers lots up to this age.
RESUME_MAX_AGE_MINUTES = 10
CATCHUP_MAX_ITEMS = positive_int_setting("CATCHUP_MAX_ITEMS", "10")
CATCHUP_MAX_PAGES = positive_int_setting("CATCHUP_MAX_PAGES", "5")
METRICS_WINDOW_SECONDS = positive_int_setting("METRICS_WINDOW_SECONDS", "3600")
METRICS_REPORT_INTERVAL = positive_int_setting("METRICS_REPORT_INTERVAL", "60")

# --- Совместимость со старыми ссылками в коде/тестах ---
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "5"))
if CHECK_INTERVAL <= 0:
    raise ValueError("Параметр CHECK_INTERVAL должен быть положительным")
MAX_CONCURRENT_WORKERS = min(32, max(1, int(os.getenv("MAX_CONCURRENT_WORKERS", "4"))))
AVITO_REQUESTS_PER_SECOND = positive_float_setting("AVITO_REQUESTS_PER_SECOND", "0.5")
AVITO_MAX_IN_FLIGHT = min(32, max(1, int(os.getenv("AVITO_MAX_IN_FLIGHT", "1"))))
AVITO_BLOCK_BASE_COOLDOWN = positive_float_setting("AVITO_BLOCK_BASE_COOLDOWN", "30")
AVITO_BLOCK_MAX_COOLDOWN = max(
    AVITO_BLOCK_BASE_COOLDOWN,
    positive_float_setting("AVITO_BLOCK_MAX_COOLDOWN", "900"),
)
AVITO_FETCH_TIMEOUT = positive_float_setting("AVITO_FETCH_TIMEOUT", str(AVITO_COLLECTION_DEADLINE))
AVITO_MAX_REQUESTS_PER_FETCH = max(1, int(os.getenv("AVITO_MAX_REQUESTS_PER_FETCH", str(AVITO_MAX_REQUESTS_PER_SCAN))))
SCAN_MAX_INTERVAL = max(float(SCAN_INTERVAL_MIN), positive_float_setting("SCAN_MAX_INTERVAL", str(SCAN_INTERVAL_MAX)))
SCAN_BUDGET_UTILIZATION = positive_float_setting("SCAN_BUDGET_UTILIZATION", "0.8")
# When the measured capacity falls short, every tariff interval stretches by one factor so the load uses
# SCAN_STRETCH_UTILIZATION of it (the rest absorbs retries); never more than ×SCAN_STRETCH_MAX.
SCAN_STRETCH_UTILIZATION = positive_float_setting("SCAN_STRETCH_UTILIZATION", "0.9")
SCAN_STRETCH_MAX = positive_float_setting("SCAN_STRETCH_MAX", "3.0")
if SCAN_BUDGET_UTILIZATION > 1:
    raise ValueError("SCAN_BUDGET_UTILIZATION must not exceed 1")
SCAN_MAX_RETRIES = min(3, max(0, int(os.getenv("SCAN_MAX_RETRIES", "2"))))
SCAN_RETRY_BASE_DELAY = positive_float_setting("SCAN_RETRY_BASE_DELAY", "5")
SCAN_RETRY_MAX_DELAY = max(SCAN_RETRY_BASE_DELAY, positive_float_setting("SCAN_RETRY_MAX_DELAY", "60"))
SCAN_STARTUP_SPREAD = positive_float_setting("SCAN_STARTUP_SPREAD", "15")
INITIAL_SNAPSHOT_TTL = positive_float_setting("INITIAL_SNAPSHOT_TTL", str(SOURCE_SNAPSHOT_TTL))


def validate_runtime_config(
    *,
    scan_interval_min: float | None = None,
    scan_interval_max: float | None = None,
    proxy_min_rate: float | None = None,
    proxy_rate_limit: float | None = None,
    proxy_rate_recommendation_max: float | None = None,
    proxy_capacity_reserve: float | None = None,
    avito_connect_timeout: float | None = None,
    avito_read_timeout: float | None = None,
    avito_collection_deadline: float | None = None,
) -> None:
    s_min = SCAN_INTERVAL_MIN if scan_interval_min is None else float(scan_interval_min)
    s_max = SCAN_INTERVAL_MAX if scan_interval_max is None else float(scan_interval_max)
    p_min = PROXY_MIN_RATE if proxy_min_rate is None else float(proxy_min_rate)
    p_rate = PROXY_RATE_LIMIT if proxy_rate_limit is None else float(proxy_rate_limit)
    p_rec_max = (
        PROXY_RATE_RECOMMENDATION_MAX
        if proxy_rate_recommendation_max is None
        else float(proxy_rate_recommendation_max)
    )
    reserve = PROXY_CAPACITY_RESERVE if proxy_capacity_reserve is None else float(proxy_capacity_reserve)
    conn_to = AVITO_CONNECT_TIMEOUT if avito_connect_timeout is None else float(avito_connect_timeout)
    read_to = AVITO_READ_TIMEOUT if avito_read_timeout is None else float(avito_read_timeout)
    deadline = AVITO_COLLECTION_DEADLINE if avito_collection_deadline is None else float(avito_collection_deadline)

    if not math.isfinite(s_min) or s_min <= 0 or not math.isfinite(s_max) or s_max <= 0:
        raise ValueError("SCAN_INTERVAL_MIN и SCAN_INTERVAL_MAX должны быть положительными числами")
    if s_min > s_max:
        raise ValueError("Некорректная конфигурация: SCAN_INTERVAL_MIN должен быть <= SCAN_INTERVAL_MAX")

    if not (0 < p_min <= p_rate <= p_rec_max):
        raise ValueError(
            "Некорректная конфигурация скорости прокси: требуется 0 < PROXY_MIN_RATE <= PROXY_RATE_LIMIT <= PROXY_RATE_RECOMMENDATION_MAX"
        )

    if not math.isfinite(reserve) or not (0.0 <= reserve < 1.0):
        raise ValueError("Некорректная конфигурация резерва: требуется 0 <= PROXY_CAPACITY_RESERVE < 1")

    if conn_to <= 0 or read_to <= 0 or deadline <= 0:
        raise ValueError("Таймауты AVITO_CONNECT_TIMEOUT, AVITO_READ_TIMEOUT и AVITO_COLLECTION_DEADLINE должны быть > 0")
    if conn_to + read_to > deadline:
        raise ValueError(
            "Некорректная конфигурация таймаутов: AVITO_CONNECT_TIMEOUT + AVITO_READ_TIMEOUT не может превышать AVITO_COLLECTION_DEADLINE"
        )


validate_runtime_config()

# Stars — единственный способ оплаты цифровой подписки. Включается явно.
STARS_PAYMENTS_ENABLED = os.getenv("STARS_PAYMENTS_ENABLED", "0").strip() == "1"
# New env names on purpose: the deploy still writes the old STARS_PRICE_STANDART/PRO (170/470), which are ignored now.
STARS_PRICE_LITE = int(os.getenv("STARS_PRICE_BASIC", "229"))
STARS_PRICE_STANDART = int(os.getenv("STARS_PRICE_MEDIUM", "449"))
STARS_PRICE_PRO = int(os.getenv("STARS_PRICE_FULL", "829"))
STARS_PRICE_TEST = int(os.getenv("STARS_PRICE_TEST", "10"))
# Hidden 1-day plan for checking checkout end to end. Off, it cannot be bought even with a forged button.
STARS_TEST_ENABLED = os.getenv("STARS_TEST_ENABLED", "0").strip() == "1"
STARS_PRICE_MAX = 100000
STARS_SUBSCRIPTION_DAYS = 30
if min(STARS_PRICE_LITE, STARS_PRICE_STANDART, STARS_PRICE_PRO, STARS_PRICE_TEST) <= 0:
    raise ValueError("Telegram Stars prices must be positive integers")
if not STARS_PRICE_LITE < STARS_PRICE_STANDART < STARS_PRICE_PRO:
    raise ValueError("Tariff prices must grow: Базовый < Средний < Полный")


def apply_stars_prices(lite: int, standart: int, pro: int) -> None:
    """Live prices set by an admin (bot_settings.stars_prices); they win over the env defaults."""
    global STARS_PRICE_LITE, STARS_PRICE_STANDART, STARS_PRICE_PRO
    if not all(type(price) is int for price in (lite, standart, pro)) or not 0 < lite < standart < pro <= STARS_PRICE_MAX:
        raise ValueError("Tariff prices must grow: Базовый < Средний < Полный")
    STARS_PRICE_LITE, STARS_PRICE_STANDART, STARS_PRICE_PRO = lite, standart, pro


# A subscription is bought for one of these periods; longer ones are cheaper per month.
PERIOD_MONTHS: tuple[int, ...] = (1, 3)
# Periods offered for sale right now. 3 months is hidden for the time being (PERIOD_MONTHS still knows it,
# so orders already created for 3 months complete normally). Return it with SALE_PERIOD_MONTHS=1,3.
SALE_PERIOD_MONTHS: tuple[int, ...] = tuple(
    m for m in (int(x) for x in os.getenv("SALE_PERIOD_MONTHS", "1").replace(" ", "").split(",") if x.isdigit())
    if m in PERIOD_MONTHS
) or (1,)
PERIOD_DISCOUNT_PERCENT: dict[int, int] = {1: 0, 3: positive_int_setting("PERIOD_3M_DISCOUNT", "10")}
if not all(0 <= PERIOD_DISCOUNT_PERCENT[m] < 100 for m in PERIOD_MONTHS):
    raise ValueError("PERIOD_3M_DISCOUNT must be below 100")


def period_days(months: int) -> int:
    return STARS_SUBSCRIPTION_DAYS * months


def period_months(days: int) -> int:
    """Months of a paid order by its days; old orders are always 30 days."""
    return max(1, int(days) // STARS_SUBSCRIPTION_DAYS)


def period_title(months: int) -> str:
    return plural(months, "месяц", "месяца", "месяцев")


def nice_price(value: int) -> int:
    """Nearest price that ends in 9 (below 1000) or 99 (from 1000): 537 → 539, 1212 → 1199, 2427 → 2399."""
    step = 100 if value >= 1000 else 10
    return max(step - 1, int((value + 1) / step + 0.5) * step - 1)


def _period_total(monthly: int, months: int) -> int:
    if months not in PERIOD_MONTHS:
        raise ValueError("Unknown subscription period")
    if months == 1:
        return monthly
    return nice_price(int(monthly * months * (100 - PERIOD_DISCOUNT_PERCENT[months]) / 100 + 0.5))


def stars_price(plan: str, months: int = 1) -> int:
    if plan == "test":
        return STARS_PRICE_TEST
    if plan == "lite":
        return _period_total(STARS_PRICE_LITE, months)
    if plan == "standart":
        return _period_total(STARS_PRICE_STANDART, months)
    if plan == "pro":
        return _period_total(STARS_PRICE_PRO, months)
    raise ValueError("Unknown tariff plan")


# The ruble price is the main one shown to users; СБП and crypto checkouts are not connected yet.
PRICE_RUB: dict[str, int] = {
    "lite": positive_int_setting("PRICE_RUB_BASIC", "349"),
    "standart": positive_int_setting("PRICE_RUB_MEDIUM", "699"),
    "pro": positive_int_setting("PRICE_RUB_FULL", "1290"),
}
# The payment provider refuses a single transaction above this.
PAYMENT_LIMIT_RUB = 10000
if max(PRICE_RUB.values()) > PAYMENT_LIMIT_RUB:
    raise ValueError(f"Цена тарифа не должна превышать {PAYMENT_LIMIT_RUB} ₽ за одну оплату")


# Hidden checkout test: /arseniytest1ruble sells 1 hour of «Полный» for 1 ₽ through Platega.
RUB_TEST_PLAN = "rubtest"
RUB_TEST_PRICE = 1
RUB_TEST_SECONDS = 3600
RUB_TEST_GRANTS = "pro"
RUB_TEST_COMMAND = "arseniytest1ruble"

# Referral program (referrals.py): links are issued by an admin to one owner.
REF_PERCENT = int(os.getenv("REF_PERCENT", "15"))
REF_HOLD_DAYS = int(os.getenv("REF_HOLD_DAYS", "7"))          # a reward can be paid out this many days after the payment
REF_STAR_RATE = float(os.getenv("REF_STAR_RATE", "1.55"))     # rubles per star; admins change it in bot_settings.ref_star_rate
REF_PAYOUT_CONTACT = os.getenv("REF_PAYOUT_CONTACT", "@prostatock").strip() or "@prostatock"


def order_plan(plan: str | None) -> str | None:
    """The tariff an order actually grants (the ruble test order grants «Полный»)."""
    return RUB_TEST_GRANTS if plan == RUB_TEST_PLAN else plan


def rub_price(plan: str, months: int = 1) -> int:
    if plan == RUB_TEST_PLAN:
        return RUB_TEST_PRICE
    return _period_total(PRICE_RUB[plan], months)


# Platega: СБП, карта и крипта на их странице оплаты. Без обоих ключей кнопки оплаты рублями
# только объясняют, что оплата скоро появится.
PLATEGA_MERCHANT_ID = os.getenv("PLATEGA_MERCHANT_ID", "").strip()
PLATEGA_SECRET = os.getenv("PLATEGA_SECRET", "").strip()
PLATEGA_ENABLED = bool(PLATEGA_MERCHANT_ID and PLATEGA_SECRET)
# Вебхук Platega: локальный порт, который обратный прокси с HTTPS открывает наружу. 0 — выключен,
# тогда бот узнаёт статусы сам, запросами по расписанию.
PLATEGA_CALLBACK_PORT = int(non_negative_float_setting("PLATEGA_CALLBACK_PORT", "0"))
PLATEGA_CALLBACK_HOST = os.getenv("PLATEGA_CALLBACK_HOST", "127.0.0.1").strip() or "127.0.0.1"
PLATEGA_CALLBACK_PATH = "/" + (os.getenv("PLATEGA_CALLBACK_PATH", "platega/callback").strip().strip("/") or "platega/callback")
if PLATEGA_CALLBACK_PORT > 65535:
    raise ValueError("PLATEGA_CALLBACK_PORT должен быть от 1 до 65535 (0 — вебхук выключен)")


def plan_title(plan: str | None) -> str:
    return PLAN_TITLES.get((plan or "standart").lower(), PLAN_TITLES["standart"])


def plan_radars(plan: str | None) -> int:
    return PLAN_RADARS.get((plan or "standart").lower(), PLAN_RADARS["standart"])


def plan_interval(plan: str | None) -> int:
    return int(TARIFF_SCAN_INTERVAL.get((plan or "standart").lower(), TARIFF_SCAN_INTERVAL["standart"]))


def plan_switch_seconds(old_plan: str | None, new_plan: str | None, seconds_left: int) -> int:
    """Paid time keeps its value in rubles when the tariff changes: cheap days must not become expensive ones.
    Rubles, not Stars: Stars prices are changed by admins and carry Telegram's cut, ruble prices are the base."""
    old, new = (old_plan or "").lower(), (new_plan or "").lower()
    if old == new or old not in PAID_PLANS or new not in PAID_PLANS or seconds_left <= 0:
        return max(0, int(seconds_left))
    return int(seconds_left * rub_price(old) / rub_price(new))


def parse_proxy(raw_proxy: str | None) -> str | None:
    """
    Нормализует прокси в стандартный RFC-формат (http://LOGIN:PASS@IP:PORT).
    Поддерживает:
    - http://IP:PORT:LOGIN:PASS
    - IP:PORT:LOGIN:PASS
    - http://LOGIN:PASS@IP:PORT
    - socks5://...
    - socks5h://...
    """
    if not raw_proxy or not raw_proxy.strip():
        return None
    raw = raw_proxy.strip()

    scheme = "http"
    clean = raw
    if "://" in clean:
        scheme, clean = clean.split("://", 1)
    scheme = scheme.lower()

    if "@" in clean:
        return f"{scheme}://{clean}"

    parts = clean.split(":")
    if len(parts) == 4:
        ip, port, user, password = parts
        return f"{scheme}://{user}:{password}@{ip}:{port}"
    elif len(parts) == 2:
        ip, port = parts
        return f"{scheme}://{ip}:{port}"

    return raw


RAW_PROXY = os.getenv("PROXY_URL", "").strip() or None
PROXY_URL = parse_proxy(RAW_PROXY)
# Proxies added from the bot (/admin -> network); persisted in bot_settings.proxy_urls and merged with PROXY_URL.
PROXY_URLS: list[str] = []

# Значение ADMIN_IDS имеет приоритет; ADMIN_ID поддерживается для совместимости.
raw_admin_ids = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or "986832959,455386428"
try:
    ADMIN_IDS = list(dict.fromkeys(int(aid.strip()) for aid in raw_admin_ids.split(",") if aid.strip()))
except ValueError as exc:
    raise ValueError("ADMIN_IDS / ADMIN_ID must contain comma-separated integer IDs") from exc
if not ADMIN_IDS or any(aid <= 0 for aid in ADMIN_IDS):
    raise ValueError("At least one positive administrator ID is required")
ADMIN_ID = ADMIN_IDS[0]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def tariff_delivery_delay(tariff_key: str) -> float:
    """Kept for old outbox rows and callers: delivery is never delayed by tariff."""
    return 0.0


# Database path
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "bot.db"))).expanduser().resolve()
# The deploy's copies (bot-before-*.db) and the daily ones (daily-YYYYMMDD.db.gz) live here.
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(BASE_DIR / "backups"))).expanduser().resolve()
DAILY_BACKUP_KEEP = positive_int_setting("DAILY_BACKUP_KEEP", "14")
