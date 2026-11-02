import asyncio
import base64
import contextvars
import copy
import hashlib
import json
import logging
import math
import os
import random
import re
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from contextlib import contextmanager
from http.cookiejar import Cookie
from threading import Event, Lock, RLock
from typing import List, Optional, TYPE_CHECKING
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from curl_cffi import requests
import config
from config import DATA_DIR
from freshness import normalize_timestamp
from request_budget import (
    BLOCK_BASE_COOLDOWN, BLOCK_MAX_COOLDOWN,
    NetworkBlockState, get_network_block_state, parse_retry_after,
    RequestDeferred, RequestBudgetExceeded,
)
from runtime_metrics import METRICS
from anti_bot import (
    get_browser_user_agent,
    handle_anti_bot_challenge,
    is_qrator_challenge_response,
    extract_pow_challenge,
    solve_pow_challenge,
    is_geetest_challenge,
)
from anti_bot.manager import BROWSER_PROFILE
from anti_bot.pow_budget import PowSearchCancelled, PowSearchError, check_cancelled

if TYPE_CHECKING:
    from proxy_pool import ProxyClient

try:
    import orjson as _orjson
    _json_loads = _orjson.loads
except ImportError:
    _json_loads = json.loads

logger = logging.getLogger(__name__)

COOKIE_FILE = DATA_DIR / "avito_cookies.json"
COOKIE_IO_LOCK = Lock()

_CHALLENGE_EXECUTOR: Optional[ThreadPoolExecutor] = None
_COOKIE_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_LOCK = Lock()

# Cheap regex scans replace a full DOM parse for the few HTML facts we still need.
_ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.I)
_HREF_RE = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_NEXT_MARKER_RE = re.compile(
    r"""\bdata-marker\s*=\s*["']pagination-button/nextPage["']|\brel\s*=\s*["'][^"']*\bnext\b""", re.I
)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script[^>]*>([\s\S]*?)</script>")


def _get_challenge_executor() -> ThreadPoolExecutor:
    global _CHALLENGE_EXECUTOR
    if _CHALLENGE_EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _CHALLENGE_EXECUTOR is None:
                _CHALLENGE_EXECUTOR = ThreadPoolExecutor(
                    max_workers=max(1, int(config.CHALLENGE_WORKERS)),
                    thread_name_prefix="avito-challenge",
                )
    return _CHALLENGE_EXECUTOR


def _get_cookie_executor() -> ThreadPoolExecutor:
    global _COOKIE_EXECUTOR
    if _COOKIE_EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _COOKIE_EXECUTOR is None:
                # One thread: a slow disk must not occupy the default pool that aiogram filters run in.
                _COOKIE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="avito-cookies")
    return _COOKIE_EXECUTOR


@dataclass
class FetchFailure:
    kind: str
    retryable: bool = True
    retry_after: float = 0.0
    status: Optional[int] = None
    detail: str = ""


def redirect_failure(match: Optional[re.Match], redirects: int) -> FetchFailure:
    """Another channel may get past the redirect, so the scan goes on; the detail shows where this one stopped."""
    where = urlparse(match.group(1).replace(r"\u0026", "&")).path if match else "нет адреса"
    return FetchFailure("invalid_redirect", detail=f"переход {redirects + 1}: {where}")


_STUB_MARKERS = ("firewall", "captcha", "geetest", "qrator", "login", "доступ ограничен", "доступ временно", "blocked")


def page_fingerprint(html: str) -> str:
    """What a page without a catalog actually was: title, size and anti-bot markers. Failures only."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = " ".join(match.group(1).split())[:60] if match else "без title"
    lowered = html.lower()
    markers = [marker for marker in _STUB_MARKERS if marker in lowered]
    return f"«{title}» {len(html) // 1024} КБ" + (f" [{', '.join(markers)}]" if markers else "")


def _error_detail(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:120]


SELLER_PROFILE_PATH_RE = re.compile(r"^/(user|brands)/[^/?#]+")
SELLER_LINK_FIELDS = ("link", "url", "uri", "href", "profileUrl", "profileLink")
SELLER_ID_FIELDS = ("userKey", "userHash", "sellerId", "userId")
_seller_keys_reported = False


def seller_key_from(item: dict, user_step: dict) -> Optional[str]:
    """A stable seller id: names repeat, a profile link does not.

    Only /user/<id> and /brands/<id> links count: another link of the block may be common to all sellers,
    and hiding one seller must never hide everybody.
    """
    raw_seller = item.get("seller") if isinstance(item.get("seller"), dict) else {}
    profile = user_step.get("profile") if isinstance(user_step.get("profile"), dict) else {}
    for source in (profile, raw_seller):
        for field in SELLER_LINK_FIELDS:
            value = source.get(field)
            match = SELLER_PROFILE_PATH_RE.match(urlparse(value).path) if isinstance(value, str) else None
            if match:
                return match.group(0)
    for source in (profile, user_step, raw_seller, item):
        for field in SELLER_ID_FIELDS:
            value = source.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                return f"id:{str(value).strip()}"
    return None


def report_missing_seller_keys(items: list, profile_fields: list[str]) -> None:
    """Avito's seller block is undocumented. If no id is found, the log names the fields to look at instead."""
    global _seller_keys_reported
    named = [item for item in items if item.seller_name]
    if _seller_keys_reported or len(named) < 5:
        return
    _seller_keys_reported = True
    found = sum(1 for item in named if item.seller_key)
    if found:
        logger.info("Ключ продавца найден у %s из %s лотов", found, len(named))
    else:
        logger.warning("Ключ продавца не найден, чёрный список работает по имени. Поля блока: %s", profile_fields)


@dataclass
class AvitoItem:
    item_id: str
    title: str
    price: str
    url: str
    image_url: Optional[str] = None
    address: Optional[str] = None
    date: Optional[str] = None
    description: Optional[str] = None
    seller: Optional[str] = None
    seller_name: Optional[str] = None
    seller_key: Optional[str] = None
    delivery: Optional[str] = None
    condition: Optional[str] = None
    timestamp: Optional[int] = None
    is_reserved: bool = False
    is_promotion: bool = False
    views_total: Optional[int] = None
    views_today: Optional[int] = None
    raw_price: Optional[int] = None
    was_lowered: bool = False
    string_without_discount: Optional[str] = None
    discount_percent: Optional[int] = None
    badges: Optional[List[str]] = None
    t_pub_reliable: bool = False
    market_median: Optional[int] = None
    market_percent: Optional[int] = None


def _default_headers() -> dict[str, str]:
    ua = get_browser_user_agent()
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": ua,
    }


_PROXY_STATUS_RE = re.compile(r"CONNECT tunnel failed, response (\d{3})|HTTP code (\d{3}) from proxy", re.I)


def classify_proxy_error(exc: BaseException) -> Optional[tuple[str, Optional[int]]]:
    """
    Tell proxy-side failures from network noise using libcurl's message:
    407 = wrong login/password (or IP not whitelisted), 403 on CONNECT = the provider forbids the target site.
    """
    text = str(exc)
    match = _PROXY_STATUS_RE.search(text)
    if match:
        status = int(match.group(1) or match.group(2))
        if status == 407:
            return "proxy_auth", status
        if status == 403:
            return "proxy_forbidden", status
        return "proxy_error", status
    lowered = text.lower()
    if "socks" in lowered and ("auth" in lowered or "user" in lowered):
        return "proxy_auth", None
    if "could not resolve proxy" in lowered:
        return "proxy_dns", None
    if "connection refused" in lowered or "couldn't connect to server" in lowered or "failed to connect" in lowered:
        return "proxy_refused", None
    return None


def apply_cache_busting(url: str) -> str:
    """
    Добавляет динамический timestamp (_t) к URL каталога Авито,
    чтобы пробивать кэш CDN (Qrator / Nginx edge) и получать
    свежие объявления без 30-60 секундного лага кэша.
    """
    try:
        parsed = urlparse(url)
        if not (parsed.hostname and (parsed.hostname == "avito.ru" or parsed.hostname.endswith(".avito.ru"))):
            return url
        if any(parsed.path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".js", ".css", ".ico")):
            return url
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs["_t"] = [str(int(time.time() * 1000))]
        new_query = urlencode(qs, doseq=True)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        return url


def remove_cache_busting(url: str) -> str:
    """Удаляет параметр _t из URL для сохранения канонического ключа в кэше и логах."""
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        if "_t" in qs:
            del qs["_t"]
            new_query = urlencode(qs, doseq=True)
            return urlunparse(parsed._replace(query=new_query))
        return url
    except Exception:
        return url


def _copy_cookie_jar(source_jar, target_jar) -> None:
    try:
        target_jar.clear()
    except Exception:
        pass
    for cookie in list(source_jar):
        try:
            target_jar.set_cookie(copy.deepcopy(cookie))
        except Exception:
            pass


class AvitoParser:
    def __init__(
        self,
        proxy: Optional[str] = None,
        *,
        block_state: Optional[NetworkBlockState] = None,
        proxy_client: Optional["ProxyClient"] = None,
    ):
        self.proxy_client: Optional["ProxyClient"] = proxy_client
        if proxy_client is not None and proxy is None:
            proxy = proxy_client.proxy_url
        self.current_proxy: Optional[str] = proxy
        self.network_block = block_state if block_state is not None else get_network_block_state(proxy)
        self._session_lock = RLock()
        self._async_fetch_lock = asyncio.Lock()
        self._cancel_event: Event | None = None
        self._operation_deadline: float | None = None
        self._operation_requests = 0
        self._last_block_response = None
        self.last_failure: Optional[FetchFailure] = None
        self.last_coverage_gap = False
        self.last_http_requests = 0
        self.blocked_until = 0.0
        self.block_streak = 0
        if proxy_client is not None:
            self._proxy_key = proxy_client.proxy_key
            self.cookie_file = proxy_client.cookie_file
        else:
            self._proxy_key = hashlib.sha256((proxy or "direct").encode()).hexdigest()
            self.cookie_file = COOKIE_FILE if not proxy else DATA_DIR / f"avito_cookies_{self._proxy_key[:16]}.json"
        self.session = self._create_session()
        self._install_request_budget()
        self._last_saved_cookies: list = []
        self._cookies_saved_at = 0.0
        self._cookie_write: Optional[Future] = None
        self._load_cookies()

        # Кэш SSR-редиректов Авито: исходный URL -> канонический URL (экономит 1 запрос на радар)
        self._canonical_cache: dict = {}

    # ─── Состояние блокировки ────────────────────────────────────────────
    def is_blocked(self) -> bool:
        if self.proxy_client is not None:
            return not self.proxy_client.is_available()
        return time.time() < max(self.blocked_until, self.network_block.blocked_until)

    def block_remaining(self) -> int:
        if self.proxy_client is not None:
            return max(0, math.ceil(self.proxy_client.cooldown_remaining()))
        return max(0, math.ceil(max(self.blocked_until, self.network_block.blocked_until) - time.time()))

    def _register_block(self, status: int, headers=None, *, response=None, kind=None):
        if response is not None and response is self._last_block_response:
            return
        retry_after = parse_retry_after(headers)
        cooldown, self.block_streak = self.network_block.register_block(retry_after)
        self.blocked_until = time.time() + cooldown
        self._last_block_response = response
        failure_kind = kind or ("rate_limit" if status == 429 else "forbidden" if status == 403 else "challenge")
        if self.proxy_client is not None:
            proxy_cooldown = self.proxy_client.record_block_or_challenge_failure(
                status=status,
                retry_after=retry_after,
                reason=failure_kind,
            )
            cooldown = max(cooldown, proxy_cooldown)
        self.last_failure = FetchFailure(failure_kind, retry_after=cooldown, status=status)
        logger.debug(
            "⚠️ HTTP %s (%s): общая пауза %.1f с, серия %s. Это не подтверждение IP-бана.",
            status, self.last_failure.kind, cooldown, self.block_streak,
        )

    @staticmethod
    def _is_recognized_challenge(response, session) -> bool:
        return bool(
            is_qrator_challenge_response(response) or is_geetest_challenge(response)
            or extract_pow_challenge(response, session)
            or "firewall-container" in (getattr(response, "text", "") or "")
        )

    @staticmethod
    def _needs_challenge(response, text: str, session) -> bool:
        return bool(
            is_qrator_challenge_response(response)
            or is_geetest_challenge(response)
            or extract_pow_challenge(response, session)
            or "firewall-container" in text
        )

    @contextmanager
    def _operation(self, cancel_event: Event | None = None, deadline: float | None = None):
        previous = self._cancel_event, self._operation_deadline, self._operation_requests
        self._cancel_event = cancel_event
        self._operation_deadline = min(deadline or float("inf"), time.monotonic() + config.AVITO_FETCH_TIMEOUT)
        self._operation_requests = 0
        try:
            yield
        finally:
            self.last_http_requests = self._operation_requests
            self._cancel_event, self._operation_deadline, self._operation_requests = previous

    def _install_request_budget(self):
        # Keep the actual curl session identity/cookies for the existing handlers.
        # curl_cffi 0.16.3 get/post dispatch through this assignable method.
        raw_request = getattr(self.session, "request", None)
        if not callable(raw_request):
            return  # Injected test doubles can implement get/post directly.

        def budgeted_request(*args, **kwargs):
            with self._session_lock:
                event, deadline = self._cancel_event, self._operation_deadline
                max_reqs = min(int(config.AVITO_MAX_REQUESTS_PER_FETCH), int(config.AVITO_MAX_REQUESTS_PER_SCAN))
                if self._operation_requests >= max_reqs:
                    raise RequestBudgetExceeded("HTTP request limit per fetch exceeded")
                with self.network_block.request_slot(event, deadline):
                    self._operation_requests += 1
                    remaining = max(0.001, deadline - time.monotonic()) if deadline else 20.0
                    timeout = kwargs.get("timeout", (config.AVITO_CONNECT_TIMEOUT, config.AVITO_READ_TIMEOUT))
                    if isinstance(timeout, tuple):
                        kwargs["timeout"] = tuple(min(float(part), remaining) for part in timeout)
                    else:
                        kwargs["timeout"] = min(float(timeout or 20), remaining)
                    # Redirects must be explicit, validated and budgeted separately.
                    kwargs["allow_redirects"] = False
                    req_t0 = time.monotonic()
                    response = raw_request(*args, **kwargs)
                    req_elapsed = max(0.0, time.monotonic() - req_t0)
                    self.network_block.record_response(response.status_code)
                    if self.proxy_client is not None:
                        self.proxy_client.record_raw_http_response(response.status_code, req_elapsed)
                    else:
                        METRICS.record_http_request("direct", "direct", response.status_code, req_elapsed)
                    recognized = self._is_recognized_challenge(response, self.session)
                    retry_after = parse_retry_after(response.headers)
                    if (response.status_code in (403, 429) and (retry_after or not recognized)) or (response.status_code == 503 and retry_after):
                        self._register_block(response.status_code, response.headers, response=response,
                                             kind="server_retry_after" if response.status_code == 503 else None)
                    check_cancelled(event)
                    return response

        self.session.request = budgeted_request

    @staticmethod
    def _is_firewall(r) -> bool:
        if r is None:
            return False
        text = getattr(r, "text", "") or ""
        return (
            r.status_code in (403, 429, 439)
            or is_qrator_challenge_response(r)
            or is_geetest_challenge(r)
            or "firewall-container" in text
        )

    def _create_session(self) -> requests.Session:
        headers = _default_headers()
        proxies = {"http": self.current_proxy, "https": self.current_proxy} if self.current_proxy else None
        # All uses are serialized by _session_lock, so one Curl handle preserves
        # the connection and can be closed even when to_thread uses another worker.
        return requests.Session(impersonate=BROWSER_PROFILE, headers=headers, timeout=20, proxies=proxies, use_thread_local_curl=False)

    def close(self):
        with self._session_lock:
            self.session.close()

    @staticmethod
    def _snapshot_cookies(cookie_jar) -> list:
        """Scopes, expiry and flags of the Avito cookies, without merging proxy identities."""
        cookies = []
        for cookie in cookie_jar:
            domain = cookie.domain.lstrip(".").lower()
            if cookie.name in ("pow_challenge", "pow_solved") or cookie.is_expired() or not (domain == "avito.ru" or domain.endswith(".avito.ru")):
                continue
            cookies.append({
                "name": cookie.name, "value": cookie.value, "domain": cookie.domain,
                "domain_specified": cookie.domain_specified, "path": cookie.path,
                "secure": cookie.secure, "expires": cookie.expires, "discard": cookie.discard,
                "rest": copy.deepcopy(cookie._rest),
            })
        cookies.sort(key=lambda c: (c["domain"], c["path"], c["name"]))
        return cookies

    def _save_cookies_from_jar(self, cookie_jar):
        try:
            cookies = self._snapshot_cookies(cookie_jar)
        except Exception as e:
            logger.debug(f"Не удалось сохранить куки: {e}")
            return
        if cookies != self._last_saved_cookies:
            self._write_cookies(cookies)

    def _queue_cookie_save(self, cookie_jar):
        """Hot path: the snapshot stays on the event loop, the slow fsync goes to the background thread."""
        if self._cookie_write is not None and not self._cookie_write.done():
            return
        try:
            cookies = self._snapshot_cookies(cookie_jar)
        except Exception as e:
            logger.debug(f"Не удалось сохранить куки: {e}")
            return
        if cookies != self._last_saved_cookies:
            self._cookie_write = _get_cookie_executor().submit(self._write_cookies, cookies)

    def _write_cookies(self, cookies: list) -> None:
        """Atomic replace. The lock covers only the swap, so the event loop never waits for an fsync on it."""
        temporary_path = None
        try:
            payload = {"version": 2, "profile": BROWSER_PROFILE, "proxy_key": self._proxy_key, "cookies": cookies}
            self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cookie_file.parent, delete=False) as f:
                temporary_path = f.name
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            with COOKIE_IO_LOCK:
                os.replace(temporary_path, self.cookie_file)
            temporary_path = None
            self._last_saved_cookies = cookies
            logger.debug("Cookies saved (%d scoped records)", len(cookies))
        except Exception as e:
            logger.debug(f"Не удалось сохранить куки: {e}")
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def _save_cookies(self):
        self._save_cookies_from_jar(self.session.cookies.jar)

    def _maybe_save_cookies_from_jar(self, cookie_jar):
        """Hot-path save: an fsync per scan would stall the event loop, so persist at most once per interval."""
        now = time.monotonic()
        if now - self._cookies_saved_at < float(config.COOKIE_SAVE_INTERVAL):
            return
        self._cookies_saved_at = now
        self._queue_cookie_save(cookie_jar)

    def _load_cookies_into_jar(self, cookies_obj):
        if not self.cookie_file.exists():
            return
        try:
            with COOKIE_IO_LOCK, open(self.cookie_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return
            if payload.get("version") == 2:
                if payload.get("profile") != BROWSER_PROFILE or payload.get("proxy_key") != self._proxy_key:
                    return
                for entry in payload.get("cookies", []):
                    domain = entry.get("domain", "").lstrip(".").lower()
                    expires = entry.get("expires")
                    if not (domain == "avito.ru" or domain.endswith(".avito.ru")) or entry.get("name") in ("pow_challenge", "pow_solved"):
                        continue
                    if expires is not None and expires <= time.time():
                        continue
                    cookies_obj.jar.set_cookie(Cookie(
                        version=0, name=entry["name"], value=entry["value"], port=None, port_specified=False,
                        domain=entry["domain"], domain_specified=entry.get("domain_specified", True),
                        domain_initial_dot=entry["domain"].startswith("."), path=entry["path"], path_specified=True,
                        secure=bool(entry.get("secure")), expires=expires, discard=bool(entry.get("discard")),
                        comment=None, comment_url=None, rest=entry.get("rest", {}), rfc2109=False,
                    ))
            elif not self.current_proxy and "version" not in payload:
                for name, value in payload.items():
                    if name not in ("pow_challenge", "pow_solved") and isinstance(value, str):
                        cookies_obj.set(name, value, domain=".avito.ru", path="/")
        except Exception as e:
            logger.warning(f"Ошибка загрузки кук из файла: {e}")

    def _load_cookies(self):
        """Загружает сохраненные куки с диска в текущую сессию."""
        self._load_cookies_into_jar(self.session.cookies)

    @staticmethod
    def _human_url(url: str) -> str:
        """Превращает длинный технический URL Авито в читаемое название для логов."""
        try:
            from urllib.parse import unquote, urlparse
            p = urlparse(url)
            path = unquote(p.path).strip("/")
            parts = [x for x in path.split("/") if x]
            name = parts[-1].split("-")[0] if parts else "каталог"
            qs = unquote(p.query)
            m = re.search(r"q=([^&]+)", qs)
            if m:
                query = m.group(1).replace("+", " ").strip()
                return f"«{query}»"
            return f"«{name}»"
        except Exception:
            return url[:45]

    @staticmethod
    def _validate_avito_url(url: str):
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in ("http", "https") or not (hostname == "avito.ru" or hostname.endswith(".avito.ru")):
            raise ValueError("Ссылка должна быть с сайта avito.ru")
        if parsed.username is not None or parsed.password is not None or parsed.port not in (None, 80, 443):
            raise ValueError("Ссылка Авито не должна содержать логин, пароль или нестандартный порт")
        return parsed

    @staticmethod
    def normalize_url(raw_url: str) -> str:
        """
        Нормализует ссылку:
        - добавляет https:// если нет
        - проверяет, что это avito.ru
        - устанавливает s=104 (сортировка 'Сначала новые')
        - удаляет динамические параметры сессий и трекинга (context, localPriority, utm_*, etc.)
        - сортирует query params для дедупликации одинаковых поисков
        """
        raw_url = raw_url.strip()
        if not raw_url.lower().startswith("http://") and not raw_url.lower().startswith("https://"):
            raw_url = "https://" + raw_url

        parsed = AvitoParser._validate_avito_url(raw_url)

        # Парсим параметры запроса
        query_params = parse_qs(parsed.query)

        # Удаляем мусорные / сессионные параметры, меняющиеся между запросами
        junk_keys = {"context", "localPriority", "presentationType", "from", "view", "p", "_t"}
        for k in list(query_params.keys()):
            if k in junk_keys or k.startswith("utm_"):
                query_params.pop(k, None)

        # Устанавливаем сортировку по дате (сначала новые)
        query_params["s"] = ["104"]

        # Если bt=0 (по умолчанию 'везде'), убираем для каноничности и дедупликации
        if query_params.get("bt") == ["0"]:
            query_params.pop("bt", None)

        # Сортируем параметры для стабильного хеша URL при группировке
        sorted_params = sorted(query_params.items())
        new_query = urlencode(sorted_params, doseq=True)

        normalized = urlunparse((
            "https",
            "www.avito.ru",
            parsed.path,
            parsed.params,
            new_query,
            ""  # fragment убираем
        ))
        return normalized

    def _solve_pow_challenge(self, pow_challenge: str) -> bool:
        """Решает PoW challenge через модуль anti_bot."""
        with self._session_lock, self._operation():
            return solve_pow_challenge(self.session, pow_challenge)

    def _solve_challenge_sync_bridge(
        self,
        cookie_jar,
        response,
        actual_url: str,
        cancel_event: Event,
        deadline: float,
        request_counter: list[int],
        max_requests: int,
    ) -> bool:
        """Execute synchronous anti-bot challenge solver in bounded thread pool using a
        temporary sync Session sharing the same proxy, cookies, and rate budget.
        """
        assert self.proxy_client is not None
        client = self.proxy_client
        proxies = {"http": client.proxy_url, "https": client.proxy_url} if client.proxy_url else None
        temp_session = requests.Session(
            impersonate=BROWSER_PROFILE,
            headers=_default_headers(),
            timeout=20,
            proxies=proxies,
            use_thread_local_curl=False,
        )
        try:
            _copy_cookie_jar(cookie_jar, temp_session.cookies.jar)
            raw_request = getattr(temp_session, "request", None)
            if callable(raw_request):
                def budgeted_bridge_request(*args, **kwargs):
                    check_cancelled(cancel_event)
                    if request_counter[0] >= max_requests:
                        raise RequestBudgetExceeded("HTTP request limit per scan exceeded in challenge bridge")
                    client.wait_for_rate_slot_sync(cancel_event=cancel_event, deadline=deadline)
                    request_counter[0] += 1
                    remaining = max(0.001, deadline - time.monotonic())
                    timeout = kwargs.get("timeout", (config.AVITO_CONNECT_TIMEOUT, config.AVITO_READ_TIMEOUT))
                    if isinstance(timeout, tuple):
                        kwargs["timeout"] = tuple(min(float(part), remaining) for part in timeout)
                    else:
                        kwargs["timeout"] = min(float(timeout or 15), remaining)
                    kwargs["allow_redirects"] = False
                    req_t0 = time.monotonic()
                    resp = raw_request(*args, **kwargs)
                    req_elapsed = max(0.0, time.monotonic() - req_t0)
                    client.record_raw_http_response(resp.status_code, req_elapsed)
                    check_cancelled(cancel_event)
                    return resp

                temp_session.request = budgeted_bridge_request

            accepted = handle_anti_bot_challenge(temp_session, response, actual_url, cancel_event=cancel_event)
            check_cancelled(cancel_event)
            if accepted:
                _copy_cookie_jar(temp_session.cookies.jar, cookie_jar)
            return bool(accepted)
        finally:
            try:
                temp_session.close()
            except Exception:
                pass

    async def _get_page_response_async_client(
        self,
        async_session,
        url: str,
        deadline: float,
        request_counter: list[int],
        max_requests: int,
    ):
        assert self.proxy_client is not None
        client = self.proxy_client
        for _ in range(5):
            if time.monotonic() >= deadline:
                raise RequestBudgetExceeded("Scan deadline exceeded")
            self._validate_avito_url(url)
            if request_counter[0] >= max_requests:
                raise RequestBudgetExceeded("HTTP request limit per scan exceeded")
            await client.wait_for_rate_slot(deadline=deadline)
            request_counter[0] += 1
            remaining = max(0.001, deadline - time.monotonic())
            timeout = (
                min(float(config.AVITO_CONNECT_TIMEOUT), remaining),
                min(float(config.AVITO_READ_TIMEOUT), remaining),
            )
            req_t0 = time.monotonic()
            req_url = apply_cache_busting(url)
            response = await async_session.request("GET", req_url, timeout=timeout, allow_redirects=False)
            req_elapsed = max(0.0, time.monotonic() - req_t0)
            client.record_raw_http_response(response.status_code, req_elapsed)
            text = getattr(response, "text", "") or ""
            clean_url = remove_cache_busting(url)
            if self._is_firewall(response) or self._needs_challenge(response, text, async_session):
                return response, clean_url, text
            if response.status_code not in (301, 302, 303, 307, 308):
                return response, clean_url, text
            location = response.headers.get("Location") or response.headers.get("location")
            if not location:
                return response, clean_url, text
            url = urljoin(url, location)
        raise ValueError("Avito redirect limit exceeded")

    async def _fetch_page_async_with_proxy_client(self, url: str, *, deadline: float | None = None) -> Optional[str]:
        assert self.proxy_client is not None
        client = self.proxy_client
        self.last_failure = None
        self.last_http_requests = 0

        op_deadline = min(deadline or float("inf"), time.monotonic() + float(config.AVITO_COLLECTION_DEADLINE))
        if time.monotonic() >= op_deadline:
            self.last_failure = FetchFailure("timeout")
            return None

        if not client.is_available():
            self.last_failure = FetchFailure("cooldown", retry_after=client.cooldown_remaining())
            return None

        max_requests = min(int(config.AVITO_MAX_REQUESTS_PER_SCAN), int(config.AVITO_MAX_REQUESTS_PER_FETCH))
        request_counter = [0]
        target = self._canonical_cache.get(url, url)
        human = self._human_url(url)
        challenge_attempts = 0
        ssr_redirects = 0

        def _create_async_session():
            proxies = {"http": client.proxy_url, "https": client.proxy_url} if client.proxy_url else None
            sess = requests.AsyncSession(
                impersonate=BROWSER_PROFILE,
                headers=_default_headers(),
                proxies=proxies,
                max_clients=1,
                timeout=(config.AVITO_CONNECT_TIMEOUT, config.AVITO_READ_TIMEOUT),
            )
            self._load_cookies_into_jar(sess.cookies)
            return sess

        try:
            async_session = await client.get_or_create_async_session(_create_async_session)
            while True:
                if not client.is_available():
                    self.last_failure = self.last_failure or FetchFailure("cooldown", retry_after=client.cooldown_remaining())
                    return None
                try:
                    response, actual_url, page_html = await self._get_page_response_async_client(
                        async_session, target, op_deadline, request_counter, max_requests
                    )
                    challenged = self._needs_challenge(response, page_html, async_session)
                    retry_after = parse_retry_after(response.headers)

                    if response.status_code in (403, 429) and (retry_after or not challenged):
                        cooldown = client.record_block_or_challenge_failure(
                            status=response.status_code,
                            retry_after=retry_after,
                            reason="rate_limit" if response.status_code == 429 else "forbidden",
                        )
                        await client.reset_async_session()
                        self.last_failure = FetchFailure(
                            "rate_limit" if response.status_code == 429 else "forbidden",
                            retry_after=cooldown,
                            status=response.status_code,
                        )
                        return None

                    if response.status_code == 503 and retry_after:
                        cooldown = client.record_block_or_challenge_failure(
                            status=503,
                            retry_after=retry_after,
                            reason="server_retry_after",
                        )
                        self.last_failure = FetchFailure("server_retry_after", retry_after=cooldown, status=503)
                        return None

                    if challenged:
                        if challenge_attempts >= 2:
                            cooldown = client.record_block_or_challenge_failure(
                                status=response.status_code,
                                retry_after=retry_after,
                                reason="challenge",
                            )
                            await client.reset_async_session()
                            self.last_failure = FetchFailure("challenge", retry_after=cooldown, status=response.status_code)
                            return None
                        challenge_attempts += 1
                        logger.debug("🛡 %s [%s]: проверка HTTP %s (попытка %d/2)", human, client.proxy_id, response.status_code, challenge_attempts)
                        cancel_event = Event()
                        loop = asyncio.get_running_loop()
                        fut = loop.run_in_executor(
                            _get_challenge_executor(),
                            contextvars.copy_context().run,  # keeps the scan's trace id in anti-bot logs
                            self._solve_challenge_sync_bridge,
                            async_session.cookies.jar,
                            response,
                            actual_url,
                            cancel_event,
                            op_deadline,
                            request_counter,
                            max_requests,
                        )
                        try:
                            accepted = await asyncio.shield(fut)
                        except asyncio.CancelledError:
                            cancel_event.set()
                            while not fut.done():
                                try:
                                    await asyncio.shield(fut)
                                except asyncio.CancelledError:
                                    continue
                                except Exception:
                                    break
                            raise
                        if not accepted:
                            cooldown = client.record_block_or_challenge_failure(
                                status=response.status_code,
                                retry_after=retry_after,
                                reason="challenge",
                            )
                            await client.reset_async_session()
                            self.last_failure = FetchFailure("challenge", retry_after=cooldown, status=response.status_code)
                            return None
                        await asyncio.sleep(random.uniform(0.2, 0.3))
                        continue

                    if response.status_code != 200:
                        if response.status_code >= 500:
                            cooldown = client.record_transport_error(reason=f"http_{response.status_code}")
                        else:
                            cooldown = 0.0
                        self.last_failure = FetchFailure(
                            "server" if response.status_code >= 500 else "http_client",
                            response.status_code >= 500,
                            retry_after=cooldown,
                            status=response.status_code,
                        )
                        logger.debug("⚠️ %s [%s]: Авито ответил HTTP %s", human, client.proxy_id, response.status_code)
                        return None

                    if '"isSuspenseRedirect":true' in page_html:
                        match = re.search(r'"redirect"\s*:\s*"([^"]+)"', page_html)
                        if not match or ssr_redirects >= 2:
                            self.last_failure = redirect_failure(match, ssr_redirects)
                            return None
                        redirect_path = match.group(1).replace(r"\u0026", "&")
                        target = urljoin(actual_url, redirect_path)
                        self._validate_avito_url(target)
                        ssr_redirects += 1
                        continue

                    if not page_html or not re.search(r"<(?:!doctype\s+html|html|head|body|script)\b", page_html, re.I):
                        self.last_failure = FetchFailure("invalid_html")
                        logger.debug("⚠️ %s [%s]: HTTP 200 без HTML страницы", human, client.proxy_id)
                        return None

                    client.record_success()
                    self.block_streak = 0
                    self.blocked_until = 0.0
                    if ssr_redirects:
                        if len(self._canonical_cache) >= 1000:
                            self._canonical_cache.pop(next(iter(self._canonical_cache)))
                        self._canonical_cache[url] = target
                    if challenge_attempts:
                        # Fresh clearance cookies are worth persisting immediately.
                        self._cookies_saved_at = time.monotonic()
                        self._queue_cookie_save(async_session.cookies.jar)
                    else:
                        self._maybe_save_cookies_from_jar(async_session.cookies.jar)
                    return page_html
                except PowSearchCancelled:
                    raise
                except RequestDeferred as exc:
                    self.last_failure = self.last_failure or FetchFailure("cooldown", retry_after=exc.retry_after)
                    return None
                except RequestBudgetExceeded as exc:
                    self.last_failure = FetchFailure("request_budget")
                    logger.debug("🌐 %s [%s]: %s", human, client.proxy_id, exc)
                    return None
                except PowSearchError as exc:
                    logger.debug("PoW stopped [%s]: %s", client.proxy_id, exc)
                    cooldown = client.record_block_or_challenge_failure(status=439, reason="pow_error")
                    await client.reset_async_session()
                    self.last_failure = FetchFailure("challenge", retry_after=cooldown, status=439, detail=_error_detail(exc))
                    return None
                except ValueError as exc:
                    self.last_failure = FetchFailure("invalid_response", False)
                    logger.debug("Некорректный ответ/URL Авито: %s", exc)
                    return None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    proxy_failure = classify_proxy_error(exc) if client.proxy_url else None
                    if proxy_failure is not None:
                        kind, status = proxy_failure
                        cooldown = client.record_transport_error(reason=kind)
                        self.last_failure = self.last_failure or FetchFailure(
                            kind, retry_after=cooldown, status=status, detail=_error_detail(exc)
                        )
                        logger.debug("🌐 %s [%s]: ошибка прокси %s%s", human, client.proxy_id, kind, f" (HTTP {status})" if status else "")
                        return None
                    is_timeout = "timeout" in type(exc).__name__.lower() or isinstance(exc, TimeoutError)
                    cooldown = client.record_transport_error(reason="timeout" if is_timeout else "network")
                    self.last_failure = self.last_failure or FetchFailure(
                        "timeout" if is_timeout else "network",
                        retry_after=cooldown,
                        detail=_error_detail(exc),
                    )
                    logger.debug("🌐 %s [%s]: сетевой сбой (%s), повтор отложен планировщиком", human, client.proxy_id, type(exc).__name__)
                    return None
        finally:
            self.last_http_requests = request_counter[0]

    async def fetch_page_async(self, url: str, *, deadline: float | None = None) -> Optional[str]:
        """Signal cancellation to the worker and wait for it before reusing its session."""
        async with self._async_fetch_lock:
            if self.proxy_client is not None:
                return await self._fetch_page_async_with_proxy_client(url, deadline=deadline)

            cancel_event = Event()
            kwargs = {"cancel_event": cancel_event}
            if deadline is not None:
                kwargs["deadline"] = deadline
            task = asyncio.create_task(asyncio.to_thread(self.fetch_page, url, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                cancel_event.set()
                # Even repeated shutdown cancellations must not orphan the CPU/network worker.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    try:
                        task.result()
                    except Exception:
                        pass
                raise

    def _get_page_response(self, url: str, cancel_event: Event | None):
        """Follow a finite number of redirects, validating each target before sending cookies."""
        for _ in range(5):
            check_cancelled(cancel_event)
            self._validate_avito_url(url)
            req_url = apply_cache_busting(url)
            response = self.session.get(req_url, timeout=20, allow_redirects=False)
            check_cancelled(cancel_event)
            clean_url = remove_cache_busting(url)
            if self._is_firewall(response) or extract_pow_challenge(response, self.session):
                return response, clean_url
            if response.status_code not in (301, 302, 303, 307, 308):
                return response, clean_url
            location = response.headers.get("Location") or response.headers.get("location")
            if not location:
                return response, clean_url
            url = urljoin(url, location)
        raise ValueError("Avito redirect limit exceeded")

    def fetch_page(self, url: str, *, cancel_event: Event | None = None, deadline: float | None = None) -> Optional[str]:
        """Only an unchallenged target-page response is success, never a solver token."""
        with self._session_lock, self._operation(cancel_event, deadline):
            self.last_failure = None
            check_cancelled(cancel_event)
            if self.is_blocked():
                self.last_failure = FetchFailure("cooldown", retry_after=self.block_remaining())
                return None
            target = self._canonical_cache.get(url, url)
            human = self._human_url(url)
            challenge_attempts = 0
            ssr_redirects = 0
            while True:
                check_cancelled(cancel_event)
                if self.is_blocked():
                    self.last_failure = self.last_failure or FetchFailure("cooldown", retry_after=self.block_remaining())
                    return None
                try:
                    response, actual_url = self._get_page_response(target, cancel_event)
                    challenged = self._is_recognized_challenge(response, self.session)
                    if response.status_code in (403, 429) and (parse_retry_after(response.headers) or not challenged):
                        self._register_block(response.status_code, response.headers, response=response)
                        return None
                    if response.status_code == 503 and parse_retry_after(response.headers):
                        self._register_block(503, response.headers, response=response, kind="server_retry_after")
                        return None
                    if challenged:
                        if challenge_attempts >= 2:
                            self._register_block(response.status_code, response.headers, response=response, kind="challenge")
                            return None
                        challenge_attempts += 1
                        logger.debug("🛡 %s: проверка HTTP %s (попытка %d/2)", human, response.status_code, challenge_attempts)
                        accepted = handle_anti_bot_challenge(self.session, response, actual_url, cancel_event=cancel_event)
                        check_cancelled(cancel_event)
                        if not accepted:
                            if not self.is_blocked():
                                self._register_block(response.status_code, response.headers, response=response, kind="challenge")
                            return None
                        # Preserve the same session/IP. Do not save clearance cookies yet.
                        continue

                    if response.status_code != 200:
                        self.last_failure = FetchFailure("server" if response.status_code >= 500 else "http_client", response.status_code >= 500, status=response.status_code)
                        logger.debug("⚠️ %s: Авито ответил HTTP %s", human, response.status_code)
                        return None
                    page_html = response.text
                    if '"isSuspenseRedirect":true' in page_html:
                        match = re.search(r'"redirect"\s*:\s*"([^"]+)"', page_html)
                        if not match or ssr_redirects >= 2:
                            self.last_failure = redirect_failure(match, ssr_redirects)
                            return None
                        redirect_path = match.group(1).replace(r"\u0026", "&")
                        target = urljoin(actual_url, redirect_path)
                        self._validate_avito_url(target)
                        ssr_redirects += 1
                        continue

                    if not page_html or not re.search(r"<(?:!doctype\s+html|html|head|body|script)\b", page_html, re.I):
                        self.last_failure = FetchFailure("invalid_html")
                        logger.debug("⚠️ %s: HTTP 200 без HTML страницы", human)
                        return None
                    if self.block_streak:
                        logger.info("✅ Доступ к целевой странице Авито восстановлен")
                    self.block_streak = 0
                    self.blocked_until = 0
                    self.network_block.register_success()
                    if self.proxy_client is not None:
                        self.proxy_client.record_success()
                    if ssr_redirects:
                        if len(self._canonical_cache) >= 1000:
                            self._canonical_cache.pop(next(iter(self._canonical_cache)))
                        self._canonical_cache[url] = target
                    self._save_cookies()
                    return page_html
                except PowSearchCancelled:
                    raise
                except RequestDeferred as exc:
                    self.last_failure = self.last_failure or FetchFailure("cooldown", retry_after=exc.retry_after)
                    return None
                except RequestBudgetExceeded as exc:
                    self.last_failure = FetchFailure("request_budget")
                    logger.debug("🌐 %s: %s", human, exc)
                    return None
                except PowSearchError as exc:
                    logger.debug("PoW stopped: %s", exc)
                    self._register_block(439)
                    return None
                except ValueError as exc:
                    self.last_failure = FetchFailure("invalid_response", False)
                    logger.debug("Некорректный ответ/URL Авито: %s", exc)
                    return None
                except Exception as exc:
                    check_cancelled(cancel_event)
                    self.last_failure = self.last_failure or FetchFailure("timeout" if "timeout" in type(exc).__name__.lower() else "network")
                    logger.debug("🌐 %s: сетевой сбой (%s), повтор отложен планировщиком", human, type(exc).__name__)
                    return None

    def fetch_item_details(self, item_id: str | int) -> Optional[dict]:
        """
        Загружает карточку лота через легковесный эндпоинт Mobile API (m.avito.ru):
        - точное количество просмотров (всего и за сегодня)
        - полное развернутое описание лота
        """
        if self.is_blocked() or not str(item_id).isdigit():
            return None
        with self._session_lock, self._operation():
            if self.is_blocked():
                return None
            return self._fetch_item_details(item_id)

    def _fetch_item_details(self, item_id: str | int) -> Optional[dict]:
        try:
            url = f"https://m.avito.ru/api/1/card/items/{item_id}"
            headers = {
                "accept": "application/json, text/plain, */*",
                "referer": "https://m.avito.ru/",
                "user-agent": get_browser_user_agent(),
            }
            r = self.session.get(url, headers=headers, timeout=10, allow_redirects=False)
            if self._is_firewall(r) or extract_pow_challenge(r, self.session):
                self._register_block(r.status_code, r.headers, response=r)
                return None
            if r.status_code == 200:
                payload = r.json()
                if isinstance(payload, dict):
                    mobile = payload.get("success", {}).get("mobile", {})
                    stats = mobile.get("stats", {}) or {}
                    views = stats.get("views", {}) or {}
                    total_views = views.get("total")
                    today_views = views.get("today")
                    full_desc = mobile.get("description")
                    return {
                        "total_views": total_views,
                        "today_views": today_views,
                        "full_description": full_desc.strip() if isinstance(full_desc, str) else None,
                    }
            return None
        except Exception as e:
            logger.debug(f"Ошибка получения деталей лота {item_id}: {e}")
            return None

    @staticmethod
    def extract_search_title(html: str, fallback_url: str) -> str:
        """Извлекает читаемое название поиска из HTML (например 'iPhone 15')."""
        try:
            m_h1 = _H1_RE.search(html)
            if m_h1:
                text = _TAG_RE.sub("", m_h1.group(1)).replace("\xa0", " ").replace("&nbsp;", " ").strip()
                if text:
                    return text
            m_title = _TITLE_RE.search(html)
            if m_title:
                t = _TAG_RE.sub("", m_title.group(1)).replace("\xa0", " ").replace("&nbsp;", " ").strip()
                t = t.split("—")[0].split("|")[0].split("купить")[-1].strip()
                if t:
                    return t
        except Exception:
            pass

        parsed = urlparse(fallback_url)
        parts = [p for p in parsed.path.split("/") if p and p != "all"]
        return parts[-1] if parts else "Поиск Авито"

    @classmethod
    def _parse_items_from_ssr_json(cls, html: str) -> Optional[List[AvitoItem]]:
        """
        Извлекает структурированный каталог объявлений из встроенного в страницу SSR JSON (loaderData -> catalog -> items).
        Отдает точный timestamp публикации в миллисекундах.
        """
        try:
            catalog_items = None
            candidates: List[str] = []

            # Быстрый путь: блок состояния страницы всегда начинается с {"i18n" внутри <script>
            start = html.find('{"i18n"')
            if start != -1:
                end = html.find("</script>", start)
                if end != -1:
                    candidates.append(html[start:end])

            # Запасной путь: перебор всех script-тегов
            if not candidates:
                candidates = [
                    s for s in _SCRIPT_RE.findall(html)
                    if '"catalog"' in s and '"items"' in s
                ]

            for raw in candidates:
                raw = raw.strip()
                if not raw.startswith("{"):
                    m = re.search(r'(\{[\s\S]*\})', raw)
                    if not m:
                        continue
                    raw = m.group(1)
                try:
                    data = _json_loads(raw)
                except Exception:
                    continue
                items = data.get("loaderData", {}).get("data", {}).get("catalog", {}).get("items")
                if items is not None and isinstance(items, list):
                    catalog_items = items
                    break

            if catalog_items is None:
                return None
            if not catalog_items:
                return []

            results: List[AvitoItem] = []
            profile_fields: list[str] = []
            for it in catalog_items:
                if not isinstance(it, dict):
                    continue
                raw_id = it.get("id")
                if not raw_id:
                    continue
                item_id = str(raw_id)
                title = (it.get("title") or "Объявление").replace("\xa0", " ").strip()

                url_path = it.get("urlPath") or ""
                clean_path = url_path.split("?")[0] if url_path else f"/item/{item_id}"
                full_url = f"https://www.avito.ru{clean_path}" if clean_path.startswith("/") else clean_path

                # Цена
                price_det = it.get("priceDetailed") or {}
                raw_price = None
                if price_det.get("value") is not None:
                    try:
                        raw_price = int(price_det.get("value"))
                    except Exception:
                        pass
                was_lowered = bool(price_det.get("wasLowered"))
                string_without_discount = price_det.get("stringWithoutDiscount")
                discount_percent = price_det.get("discountPercent")

                price = price_det.get("fullString")
                if not price and raw_price is not None:
                    try:
                        price = f"{raw_price:,} ₽".replace(",", " ")
                    except Exception:
                        price = f"{raw_price} ₽"
                if not price:
                    price = "Цена не указана"

                # Фото (выбираем наибольшее доступное разрешение)
                image_url = None
                imgs = it.get("images") or []
                if imgs and isinstance(imgs, list) and isinstance(imgs[0], dict):
                    first_img = imgs[0]
                    for res in ["636x636", "472x472", "416x416", "318x318", "236x236", "208x208"]:
                        if res in first_img and first_img[res].startswith("http"):
                            image_url = first_img[res]
                            break
                    if not image_url and first_img:
                        for v in first_img.values():
                            if isinstance(v, str) and v.startswith("http"):
                                image_url = v
                                break

                if image_url:
                    # Нормализуем на быстрый глобальный Anycast CDN c80 (без региональных сбросов соединений)
                    image_url = re.sub(r"https?://[^.]+\.img\.avito\.st/", "https://c80.img.avito.st/", image_url)

                # Точный timestamp публикации (в секундах)
                sort_ts = it.get("sortTimeStamp") or it.get("allowTimeStamp")
                exact_ts = normalize_timestamp(sort_ts)
                t_pub_reliable = exact_ts is not None

                # Компоненты IVA (описание, продавец, доставка)
                iva = it.get("iva") or {}

                # Описание
                description = None
                desc_steps = iva.get("DescriptionStep") or []
                if desc_steps and isinstance(desc_steps, list):
                    description = (desc_steps[0].get("payload") or {}).get("description")

                # Продавец и рейтинг
                seller = None
                seller_name = None
                seller_profile = {}
                user_steps = iva.get("UserInfoStep") or []
                for step in user_steps:
                    payload = step.get("payload") or {}
                    if "profile" in payload:
                        seller_profile = payload
                        prof_title = (payload.get("profile") or {}).get("title")
                        if prof_title:
                            seller_name = prof_title.strip()
                        rating_data = payload.get("rating") or {}
                        score = rating_data.get("score")
                        summary = rating_data.get("summary")
                        if seller_name and score:
                            seller = f"{seller_name} · ⭐ {score}" + (f" ({summary})" if summary else "")
                        elif seller_name:
                            seller = seller_name
                        break

                if not seller_name:
                    raw_seller = it.get("seller") or {}
                    if isinstance(raw_seller, dict):
                        seller_name = raw_seller.get("name")
                        score = raw_seller.get("score")
                        if seller_name and score:
                            seller = f"{seller_name} · ⭐ {score}"
                        elif seller_name:
                            seller = seller_name

                seller_key = seller_key_from(it, seller_profile)
                if seller_profile and not profile_fields:
                    profile_fields = sorted({*seller_profile, *(seller_profile.get("profile") or {})})

                # Доставка
                delivery = None
                for step in iva.get("GeoStep") or []:
                    if (step.get("componentData") or {}).get("component") == "delivery":
                        deliv_text = (step.get("payload") or {}).get("text")
                        if deliv_text:
                            delivery = f"Авито {deliv_text}" if not deliv_text.lower().startswith("авито") else deliv_text
                        break
                if not delivery:
                    for step in iva.get("PriceStep") or []:
                        if (step.get("componentData") or {}).get("component") == "delivery":
                            delivery = "Авито Доставка"
                            break

                # Адрес (выбираем максимально подробный адрес: улица, метро, город)
                address = None
                geo = it.get("geo") or {}
                coords = it.get("coords") or {}
                addr_det = it.get("addressDetailed") or {}
                loc = it.get("location") or {}
                gallery = it.get("gallery") or {}
                if geo.get("formattedAddress"):
                    address = geo["formattedAddress"]
                elif coords.get("address_user"):
                    address = coords["address_user"]
                elif addr_det.get("locationName"):
                    address = addr_det["locationName"]
                elif loc.get("name"):
                    address = loc["name"]
                elif gallery.get("imageAlt"):
                    alt = gallery["imageAlt"]
                    if alt and "," in alt:
                        address = alt.split(",")[-1].strip()

                # Дата в читаемом виде: ПРИОРИТЕТ ТОЧНОМУ TIMESTAMP!
                date_info = None
                date_steps = iva.get("DateInfoStep") or []
                if exact_ts:
                    diff_sec = max(0, int(time.time() - exact_ts))
                    if diff_sec < 60:
                        date_info = f"{diff_sec} сек. назад"
                    elif diff_sec < 3600:
                        date_info = f"{diff_sec // 60} мин. назад"
                    else:
                        date_info = f"{diff_sec // 3600} ч. назад"
                elif date_steps:
                    date_info = (date_steps[0].get("payload") or {}).get("relative") or None
                if not date_info and not exact_ts:
                    date_info = "Дата не указана"

                # Резерв и Авито Доставка
                is_reserved = bool(it.get("isReserved"))

                # Продвижение (VAS / Promotion)
                is_promotion = bool(it.get("isPromotion") or it.get("isReMapPromotion") or it.get("vip"))
                if not is_promotion:
                    for step in date_steps:
                        vas = (step.get("payload") or {}).get("vas") or []
                        if any(isinstance(v, dict) and (v.get("title") == "Продвинуто" or v.get("name") in ("vas", "xl", "highlight")) for v in vas):
                            is_promotion = True
                            break

                # Преимущества и бейджи (скидка на доставку, 14 дней на возврат, торг и т.д.)
                badges_list = []
                raw_badges = it.get("badges") or []
                if isinstance(raw_badges, list):
                    for b in raw_badges:
                        if isinstance(b, dict):
                            t = b.get("title") or b.get("text") or b.get("description")
                            if t and isinstance(t, str) and t.strip():
                                badges_list.append(t.strip())
                        elif isinstance(b, str) and b.strip():
                            badges_list.append(b.strip())

                for step in (iva.get("BadgeStep") or []):
                    payload = step.get("payload") or {}
                    t = payload.get("text") or payload.get("title")
                    if t and t not in badges_list:
                        badges_list.append(t)

                for step in (iva.get("TrustStep") or []):
                    payload = step.get("payload") or {}
                    t = payload.get("text") or payload.get("title")
                    if t and t not in badges_list:
                        badges_list.append(t)

                if it.get("hasBargain") or it.get("bargain"):
                    if "Торг уместен" not in badges_list:
                        badges_list.append("Торг уместен")

                results.append(AvitoItem(
                    item_id=item_id,
                    title=title,
                    price=price,
                    url=full_url,
                    image_url=image_url,
                    address=address,
                    date=date_info,
                    description=description,
                    seller=seller,
                    seller_name=seller_name,
                    seller_key=seller_key,
                    delivery=delivery,
                    condition=None,
                    timestamp=exact_ts,
                    is_reserved=is_reserved,
                    is_promotion=is_promotion,
                    raw_price=raw_price,
                    was_lowered=was_lowered,
                    string_without_discount=string_without_discount,
                    discount_percent=discount_percent,
                    badges=badges_list if badges_list else None,
                    t_pub_reliable=t_pub_reliable,
                ))

            report_missing_seller_keys(results, profile_fields)
            return results
        except Exception as e:
            logger.debug(f"Ошибка парсинга SSR JSON: {e}")
            return None

    @classmethod
    def parse_catalog(cls, html: str) -> Optional[List[AvitoItem]]:
        """
        Единственный источник истины по выдаче — встроенный SSR JSON каталога.
        None — каталога на странице нет (логин, заглушка, смена вёрстки); [] — валидная пустая выдача.
        """
        return cls._parse_items_from_ssr_json(html)

    @classmethod
    def has_catalog(cls, html: str) -> bool:
        """A login/error HTML page is not a successful empty search."""
        return cls.parse_catalog(html) is not None

    @classmethod
    def parse_items(cls, html: str) -> List[AvitoItem]:
        """Органические объявления страницы выдачи (пустой список, если каталога нет)."""
        return cls.parse_catalog(html) or []

    @classmethod
    async def parse_items_async(cls, html: str) -> List[AvitoItem]:
        """orjson-разбор SSR JSON занимает единицы миллисекунд, поэтому выполняется прямо в event loop."""
        return cls.parse_items(html)

    @classmethod
    def next_page_url(cls, html: str, current_url: str) -> Optional[str]:
        """Follow only an explicit next-page link for the exact same search."""
        for tag in _ANCHOR_RE.finditer(html):
            anchor = tag.group(0)
            if not _NEXT_MARKER_RE.search(anchor):
                continue
            href_match = _HREF_RE.search(anchor)
            if not href_match:
                continue
            href = (href_match.group(1) if href_match.group(1) is not None else href_match.group(2)).replace("&amp;", "&")
            candidate = urljoin(current_url, href)
            try:
                parsed = cls._validate_avito_url(candidate)
                params = parse_qs(parsed.query)
                old_page = int(parse_qs(urlparse(current_url).query).get("p", ["1"])[0])
                new_page = int(params.get("p", ["1"])[0])
                if new_page != old_page + 1 or cls.normalize_url(candidate) != cls.normalize_url(current_url):
                    continue
            except (ValueError, TypeError):
                continue
            canonical = urlparse(cls.normalize_url(candidate))
            params = parse_qs(canonical.query)
            params["p"] = [str(new_page)]
            return urlunparse(canonical._replace(query=urlencode(sorted(params.items()), doseq=True)))
        return None
