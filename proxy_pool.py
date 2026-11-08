"""Пул статических прокси с независимыми лимитерами, карантином, half-open и горячей перезагрузкой."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from curl_cffi import requests as curl_requests

import logging
import config
from logger_setup import log_event
from runtime_metrics import METRICS, mask_proxy_url, sanitize_proxy_credentials, short_channel

log = logging.getLogger("proxy_pool")
# Smart mode pushes only real danger: Avito blocks most of the server's answers AND no proxy can take over.
# Proxies joining the load is routine and stays in the log (net.smart).
SMART_BLOCK_ALERT_INTERVAL = 6 * 3600.0
SMART_CRITICAL_BLOCK_RATIO = 0.5
SMART_CRITICAL_MIN_ANSWERS = 10

ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def compute_proxy_id(normalized_url: str | None) -> str:
    if not normalized_url:
        return "direct"
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:8]
    return f"p-{digest}"


def compute_cookie_path(normalized_url: str | None, data_dir: Path | None = None) -> Path:
    base_dir = config.DATA_DIR if data_dir is None else data_dir
    key = (normalized_url or "direct").strip().encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()[:16]
    return base_dir / f"avito_cookies_{digest}.json"


def parse_and_validate_proxy_line(raw_line: str, line_number: int = 1) -> str:
    """
    Парсит и валидирует одну строку прокси.
    Требует percent-encoding для спецсимволов (@, :) в логине/пароле.
    Никогда не включает сырые учетные данные в текст исключения.
    """
    stripped = (raw_line or "").strip()
    if not stripped:
        raise ValueError(f"Строка {line_number}: пустой адрес прокси")

    # Проверяем количество '@' до нормализации: если больше одного '@', значит спецсимвол не закодирован
    if stripped.count("@") > 1:
        raise ValueError(
            f"Строка {line_number}: некорректный формат учетных данных прокси "
            "(требуется percent-encoding спецсимволов '@' и ':')"
        )

    normalized = config.parse_proxy(stripped)
    if not normalized:
        raise ValueError(f"Строка {line_number}: не удалось распознать прокси")

    if normalized.count("@") > 1:
        raise ValueError(
            f"Строка {line_number}: некорректный формат учетных данных прокси "
            "(требуется percent-encoding спецсимволов '@' и ':')"
        )

    try:
        parsed = urlsplit(normalized)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ALLOWED_PROXY_SCHEMES:
            raise ValueError(
                f"Строка {line_number}: неподдерживаемая схема прокси '{scheme}' "
                "(разрешены http, https, socks5, socks5h)"
            )
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        if "Строка " in str(exc):
            raise
        raise ValueError(
            f"Строка {line_number}: синтаксическая ошибка URL прокси "
            "(проверьте порт и percent-encoding логина/пароля)"
        ) from exc

    if not host or port is None or not (1 <= int(port) <= 65535):
        raise ValueError(f"Строка {line_number}: должен быть указан корректный хост и порт (1..65535)")

    if "@" in parsed.netloc:
        userinfo, _ = parsed.netloc.rsplit("@", 1)
        if userinfo.count(":") != 1:
            raise ValueError(
                f"Строка {line_number}: некорректный формат логина/пароля прокси "
                "(требуется percent-encoding двоеточий)"
            )
        user, password = userinfo.split(":", 1)
        if not user or not password:
            raise ValueError(f"Строка {line_number}: пустой логин или пароль прокси")

    return f"{scheme}://{parsed.netloc}"


def load_proxy_urls_from_file(path: str | Path) -> list[str]:
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        raise ValueError(f"Файл пула прокси не найден: {file_path}")

    try:
        st = file_path.stat()
        if os.name != "nt" and (st.st_mode & 0o077):
            log.warning(
                f"⚠️ Файл прокси {file_path} имеет слишком широкие права доступа ({oct(st.st_mode & 0o777)}); "
                "рекомендуется chmod 600."
            )
    except OSError:
        pass

    text = file_path.read_text(encoding="utf-8")
    seen: set[str] = set()
    urls: list[str] = []
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        normalized = parse_and_validate_proxy_line(line, line_number=idx)
        if normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)

    if not urls:
        raise ValueError(f"Файл пула прокси {file_path} не содержит ни одного активного прокси")
    return urls


class ProxyClient:
    """Отдельный прокси-клиент со своим AsyncSession(max_clients=1), rate-лимитером и состоянием здоровья."""

    def __init__(
        self,
        proxy_url: str | None,
        *,
        configured_rate: float | None = None,
        max_in_flight: int | None = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        sync_sleep_fn: Callable[[float], None] = time.sleep,
        impersonate: str = "chrome120",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.proxy_url: str | None = config.parse_proxy(proxy_url) if proxy_url else None
        self.proxy_key: str = hashlib.sha256((self.proxy_url or "direct").strip().encode("utf-8")).hexdigest()
        self.proxy_id: str = compute_proxy_id(self.proxy_url)
        self.masked_url: str = mask_proxy_url(self.proxy_url)
        self.cookie_file: Path = compute_cookie_path(self.proxy_url)

        self._monotonic = monotonic_fn
        self._sleep = sleep_fn
        self._sync_sleep = sync_sleep_fn
        self.impersonate = impersonate
        self.default_headers: dict[str, str] = dict(headers or {})

        self.configured_rate: float = float(
            config.PROXY_RATE_LIMIT if configured_rate is None else configured_rate
        )
        self.is_direct: bool = (self.proxy_url is None)
        if self.is_direct:
            self.configured_rate = max(1.5, self.configured_rate)
        self.effective_rate: float = self.configured_rate
        # Lanes: requests through one IP at the same time, each lane with its own session and parser, while the rate
        # limit, health and quarantine stay per IP. The server keeps one lane: it is fast, its rate limit binds first.
        if max_in_flight is None:
            max_in_flight = 1 if self.is_direct else config.PROXY_MAX_IN_FLIGHT
        self.max_in_flight: int = max(1, int(max_in_flight))
        self._lane_users: dict[int, int] = {}

        self._state: str = "healthy"  # healthy | quarantined | half_open | draining
        self.quarantined_until: float = 0.0
        self.quarantine_reason: str = ""
        self.block_streak: int = 0
        self.transport_failure_streak: int = 0
        self.recovery_success_streak: int = 0
        # Rate limiter memory: answers counted so far, and that count at the last cut, so a cut is judged
        # only by answers that came at the already lowered rate (the old 403s led to the previous cut).
        self._answers_total: int = 0
        self._answers_at_cut: int = 0
        self._last_block_at: float = -math.inf
        self._last_rate_step_at: float = -math.inf
        self._half_open_probe_active: bool = False
        self.last_acquired_at: float = -math.inf

        # Pacing & concurrency state (protected by threading.Lock so both event loop and challenge thread are safe)
        self._pacing_lock = threading.Lock()
        self._next_request_at: float = 0.0
        self._tokens: float = max(1.0, float(config.PROXY_RATE_BURST))
        self._tokens_at: float = self._monotonic()
        self.in_flight: int = 0

        # Sliding 10m window of (monotonic_ts, status_code, is_transport_error)
        self._history_10m: deque[tuple[float, int | None, bool]] = deque()
        # Last answers however old: a reserve proxy may stay idle for hours, its record must not vanish.
        self._outcomes: deque[bool] = deque(maxlen=max(1, int(config.PROXY_QUALITY_WINDOW)))

        # Persistent AsyncSession per lane (created lazily)
        self._async_sessions: dict[int, curl_requests.AsyncSession] = {}
        self._session_generation: int = 0
        self._draining: bool = False

        self._sync_metrics_state()

    @property
    def state(self) -> str:
        if self._draining:
            return "draining"
        now = self._monotonic()
        if self._state == "quarantined" and now >= self.quarantined_until:
            self._state = "half_open"
            self.quarantine_reason = ""
            self._sync_metrics_state()
        return self._state

    def _sync_metrics_state(self) -> None:
        METRICS.update_proxy_state(
            self.proxy_id,
            masked_url=self.masked_url,
            state="draining" if self._draining else self._state,
            configured_rate=self.configured_rate,
            effective_rate=self.effective_rate,
            quarantined_until=self.quarantined_until,
            lanes=self.max_in_flight,
        )

    def _take_lane(self) -> int:
        """The least used lane (caller holds _pacing_lock); with every lane busy the scan queues on one."""
        lane = min(range(max(1, self.max_in_flight)), key=lambda index: (self._lane_users.get(index, 0), index))
        self._lane_users[lane] = self._lane_users.get(lane, 0) + 1
        return lane

    def _free_lane(self, lane: int) -> None:
        users = self._lane_users.get(lane, 0)
        if users > 1:
            self._lane_users[lane] = users - 1
        else:
            self._lane_users.pop(lane, None)

    @property
    def _async_session(self) -> curl_requests.AsyncSession | None:
        """Lane 0's session, for callers from before lanes."""
        return self._async_sessions.get(0)

    def lane_sessions(self) -> dict[int, curl_requests.AsyncSession]:
        return dict(self._async_sessions)

    def is_available_for_scan(self, now: float | None = None) -> bool:
        if self._draining:
            return False
        cur = self._monotonic() if now is None else now
        if self._state == "quarantined":
            if cur < self.quarantined_until:
                return False
            self._state = "half_open"
            self.quarantine_reason = ""
            self._sync_metrics_state()
        if self._state == "half_open" and self._half_open_probe_active:
            return False
        return True

    def is_available(self, now: float | None = None) -> bool:
        if self._draining:
            return False
        if self.is_direct:
            return True
        cur = self._monotonic() if now is None else now
        if self._state == "quarantined":
            if cur < self.quarantined_until:
                return False
            self._state = "half_open"
            self.quarantine_reason = ""
            self._sync_metrics_state()
        return True

    def cooldown_remaining(self, now: float | None = None) -> float:
        if self.is_direct:
            return 0.0
        cur = self._monotonic() if now is None else now
        if self.state == "quarantined":
            return max(0.0, self.quarantined_until - cur)
        return 0.0

    def estimated_ready_at(self, now: float | None = None) -> float:
        cur = self._monotonic() if now is None else now
        if not self.is_available_for_scan(cur):
            return max(cur + 9999.0, self.quarantined_until)
        with self._pacing_lock:
            base = max(cur, self._next_request_at)
            if self.in_flight >= self.max_in_flight:
                base += 1.0 / max(config.PROXY_MIN_RATE, self.effective_rate)
            return base

    async def get_or_create_async_session(
        self,
        factory_or_headers: Callable[[], curl_requests.AsyncSession] | dict[str, str] | None = None,
        lane: int = 0,
    ) -> curl_requests.AsyncSession:
        session = self._async_sessions.get(lane)
        if session is None:
            if callable(factory_or_headers):
                session = factory_or_headers()
            else:
                merged_headers = dict(self.default_headers)
                if isinstance(factory_or_headers, dict):
                    merged_headers.update(factory_or_headers)
                kwargs: dict[str, Any] = {
                    "impersonate": self.impersonate,
                    "headers": merged_headers,
                    "max_clients": 1,
                }
                if self.proxy_url:
                    kwargs["proxies"] = {"http": self.proxy_url, "https": self.proxy_url}
                session = curl_requests.AsyncSession(**kwargs)
            self._async_sessions[lane] = session
            self._session_generation += 1
        return session

    async def reset_async_session(self, lane: int | None = 0) -> None:
        """Drops one lane's session (None: every lane); another lane's request in flight is not touched."""
        for key in (list(self._async_sessions) if lane is None else [lane]):
            session = self._async_sessions.pop(key, None)
            if session is None:
                continue
            try:
                close_fn = getattr(session, "close", None)
                if close_fn is not None:
                    res = close_fn()
                    if asyncio.isfuture(res) or asyncio.iscoroutine(res):
                        await res
            except Exception:
                pass

    async def close(self) -> None:
        self._draining = True
        self._sync_metrics_state()
        await self.reset_async_session(None)

    def _reserve_pacing_delay(self) -> float:
        """
        Token bucket: idle clients answer a burst of PROXY_RATE_BURST requests instantly,
        the sustained rate never exceeds effective_rate. Returns how long this request must wait.
        """
        now = self._monotonic()
        self._calm_recovery(now)
        rate = max(config.PROXY_MIN_RATE, self.effective_rate)
        burst = max(1.0, float(config.PROXY_RATE_BURST))
        with self._pacing_lock:
            tokens = min(burst, self._tokens + max(0.0, now - self._tokens_at) * rate) - 1.0
            self._tokens, self._tokens_at = tokens, now
            self._next_request_at = now + max(0.0, (1.0 - tokens) / rate) if tokens < 1.0 else now
            return max(0.0, -tokens / rate)

    async def wait_for_rate_slot_async(self, deadline: float | None = None) -> None:
        delay = self._reserve_pacing_delay()
        if deadline is not None and self._monotonic() + delay > deadline:
            raise TimeoutError("HTTP operation deadline exceeded")
        if delay > 0:
            METRICS.record_pacing(self.proxy_id, self.masked_url, delay)
            await self._sleep(delay)

    async def wait_for_rate_slot(self, deadline: float | None = None) -> None:
        await self.wait_for_rate_slot_async(deadline=deadline)

    def wait_for_rate_slot_sync(self, deadline: float | None = None, cancel_event: threading.Event | None = None) -> None:
        delay = self._reserve_pacing_delay()
        if deadline is not None and self._monotonic() + delay > deadline:
            raise TimeoutError("HTTP operation deadline exceeded")
        if delay > 0:
            METRICS.record_pacing(self.proxy_id, self.masked_url, delay)
            if cancel_event is not None:
                if cancel_event.wait(timeout=delay):
                    raise TimeoutError("HTTP operation cancelled")
            else:
                self._sync_sleep(delay)

    def _prune_10m_history(self, now: float) -> None:
        cutoff = now - float(config.PROXY_BLOCK_WINDOW_SECONDS)
        while self._history_10m and self._history_10m[0][0] < cutoff:
            self._history_10m.popleft()

    def _recent_block_ratio(self, limit: int | None = None) -> tuple[int, float]:
        """(responses, share of 403/429/439) over the last PROXY_RATE_WINDOW HTTP responses (at most `limit`)."""
        window = max(1, int(config.PROXY_RATE_WINDOW))
        statuses = [status for _, status, transport in list(self._history_10m) if not transport and status is not None][-window:]
        if limit is not None:
            statuses = statuses[len(statuses) - max(0, limit):] if limit < len(statuses) else statuses
        if not statuses:
            return 0, 0.0
        bad = sum(1 for status in statuses if status in (403, 429, 439))
        return len(statuses), bad / len(statuses)

    def recent_health(self) -> float:
        """Share of successful answers among the last PROXY_HEALTH_WINDOW requests; a timeout counts as a failure."""
        window = max(1, int(config.PROXY_HEALTH_WINDOW))
        recent = list(self._history_10m)[-window:]
        if len(recent) < min(3, window):
            return 1.0  # a new channel gets its chance
        ok = sum(1 for _, status, transport in recent if not transport and status is not None and 200 <= status < 400)
        return ok / len(recent)

    def long_health(self) -> float:
        """Share of successful answers among the last PROXY_QUALITY_WINDOW, however old; neutral until 3 answers."""
        outcomes = list(self._outcomes)
        if len(outcomes) < 3:
            return float(config.PROXY_GOOD_HEALTH)
        return sum(outcomes) / len(outcomes)

    def recent_block_share(self, now: float) -> tuple[int, float]:
        """(answers, share of 403/429/439) among the last PROXY_RATE_WINDOW answers of the last 10 minutes."""
        cutoff = now - float(config.PROXY_BLOCK_WINDOW_SECONDS)
        window = max(1, int(config.PROXY_RATE_WINDOW))
        statuses = [
            status for at, status, transport in list(self._history_10m)
            if at >= cutoff and not transport and status is not None
        ][-window:]
        if not statuses:
            return 0, 0.0
        return len(statuses), sum(1 for status in statuses if status in (403, 429, 439)) / len(statuses)

    def routing_role(self, server_first: bool = False) -> str:
        """The channel's place in routing, for the admin card."""
        if server_first:
            if self.is_direct:
                return "основной: пока справляется, сканирует один"
            return "в запасе: берёт скан, только если сервер ошибся, без лишних проверок"
        if self.is_direct:
            return "запасной, берёт нагрузку, когда хорошие прокси заняты"
        if len(self._history_10m) < 3:
            return "новый, пока считается хорошим"
        if self.recent_health() >= float(config.PROXY_GOOD_HEALTH):
            return "основной, работает в первую очередь"
        return "слабый, работает как запасной и проверяется раз в минуту"

    def routing_score(self, now: float, server_first: bool = False) -> float:
        """Higher goes first: unproven channel due for a probe, proven proxy, server or weak proxy, overloaded proxy.
        With server_first (smart mode while the server copes) the server leads and proxies are a reserve
        ordered by their long record: no probes, so an idle proxy costs no scan a timeout."""
        if server_first:
            with self._pacing_lock:
                in_flight, limit = self.in_flight, self.max_in_flight
            if self.is_direct:
                # One more scan may queue behind the current request; the next one goes to a good reserve proxy.
                return 3.0 if in_flight <= limit else 0.9
            return min(0.99, self.long_health()) - (float(config.PROXY_HOT_PENALTY) if in_flight >= limit else 0.0)
        health = self.recent_health()
        good = health >= float(config.PROXY_GOOD_HEALTH)
        idle = now - self.last_acquired_at
        if not good or self.is_direct:
            last = self._history_10m[-1] if self._history_10m else None
            last_ok = last is not None and not last[2] and last[1] is not None and 200 <= last[1] < 400
            if idle >= float(config.PROXY_PROBE_RETRY if last_ok else config.PROXY_PROBE_INTERVAL):
                return 2.0
        # Every proven proxy scores the same, so the load rotates among them; the server is never one of them.
        score = 1.0 if good and not self.is_direct else min(health, float(config.PROXY_GOOD_HEALTH) * 0.99)
        with self._pacing_lock:
            busy = self.in_flight >= self.max_in_flight
        if busy or idle < 1.0 / float(config.PROXY_PREFERRED_RATE):
            score -= float(config.PROXY_HOT_PENALTY)
        return score

    def record_raw_http_response(
        self,
        status_code: int | None,
        latency_seconds: float,
        *,
        is_transport_error: bool = False,
        size_bytes: int = 0,
        ttfb_seconds: float | None = None,
    ) -> None:
        now = self._monotonic()
        self._prune_10m_history(now)
        self._history_10m.append((now, status_code, is_transport_error))
        self._outcomes.append(not is_transport_error and status_code is not None and 200 <= status_code < 400)

        METRICS.record_http_request(
            self.proxy_id,
            self.masked_url,
            status_code,
            latency_seconds,
            is_transport_error=is_transport_error,
            size_bytes=size_bytes,
            ttfb_seconds=ttfb_seconds,
        )

        if is_transport_error or status_code is None or status_code >= 500:
            # A timeout says nothing about Avito's patience, so it does not reset the rate recovery streak.
            self.transport_failure_streak += 1
            if self.transport_failure_streak >= config.PROXY_FAILURE_THRESHOLD:
                exp = self.transport_failure_streak - config.PROXY_FAILURE_THRESHOLD
                backoff = min(
                    float(config.PROXY_TRANSPORT_BACKOFF_MAX),
                    float(config.PROXY_TRANSPORT_BACKOFF_BASE) * (2 ** min(exp, 6)),
                )
                self._enter_quarantine(now, backoff, reason=f"transport_or_5xx_streak={self.transport_failure_streak}")
            return

        self._answers_total += 1
        if 200 <= status_code < 400:
            self.transport_failure_streak = 0
            self.recovery_success_streak += 1
            _, block_ratio = self._recent_block_ratio()
            if (
                self.recovery_success_streak >= config.PROXY_RECOVERY_SUCCESS_STREAK
                and block_ratio <= float(config.PROXY_RATE_CUT_RATIO)
                and self.effective_rate < self.configured_rate
            ):
                self._step_rate_up(now, "серия успешных ответов")
            self._sync_metrics_state()
            return

        if status_code in (403, 429, 439):
            # A single block is Avito's background noise: the window ratio decides, not the streak.
            # It does break the clean run that recovery waits for.
            self.recovery_success_streak = 0
            self._last_block_at = now
            self._sync_metrics_state()

    def _step_rate_up(self, now: float, why: str) -> None:
        self.effective_rate = min(self.configured_rate, round(self.effective_rate * config.PROXY_RECOVERY_FACTOR, 4))
        self.recovery_success_streak = 0
        self._last_rate_step_at = now
        log.debug(
            f"📈 Прокси {self.proxy_id} ({self.masked_url}) восстановил эффективную скорость до "
            f"{self.effective_rate:.2f}/{self.configured_rate:.2f} req/s ({why})"
        )

    def _calm_recovery(self, now: float) -> None:
        """A throttled proxy with no block for PROXY_RATE_CALM_SECONDS steps back up even with little traffic:
        a reserve proxy in smart mode may get a few requests an hour and would otherwise stay at the floor."""
        calm = float(config.PROXY_RATE_CALM_SECONDS)
        if (
            self.effective_rate < self.configured_rate
            and now - self._last_block_at >= calm
            and now - self._last_rate_step_at >= calm
        ):
            self._step_rate_up(now, "давно без блоков")

    def record_final_block(
        self,
        reason: str = "block",
        *,
        retry_after_seconds: float | None = None,
    ) -> float:
        now = self._monotonic()
        if self.is_direct:
            self.block_streak = 0
            self.recovery_success_streak = 0
            self.effective_rate = max(1.5, float(self.configured_rate or 1.5))
            self.quarantined_until = 0.0
            self._state = "healthy"
            self._sync_metrics_state()
            return 0.0
        self.block_streak += 1
        self._last_block_at = now
        # Judge only the answers since the previous cut: the blocks that caused it stay in the window for a while
        # and used to cut again on the very next 403, 1.5 → 0.75 → 0.5 within seconds.
        since_cut = self._answers_total - self._answers_at_cut
        responses, block_ratio = self._recent_block_ratio(limit=since_cut)
        if responses >= max(5, int(config.PROXY_RATE_WINDOW) // 2) and block_ratio > float(config.PROXY_RATE_CUT_RATIO):
            self.recovery_success_streak = 0
            self._answers_at_cut = self._answers_total
            self._last_rate_step_at = now
            before = self.effective_rate
            self.effective_rate = max(
                config.PROXY_MIN_RATE,
                round(self.effective_rate * float(config.PROXY_RATE_CUT_FACTOR), 4),
            )
            log.info(
                f"📉 Прокси {self.proxy_id} ({self.masked_url}) замедлен {before:.2f} → {self.effective_rate:.2f} req/s: "
                f"блоков {block_ratio:.0%} из {responses} ответов"
            )
        duration = min(
            float(config.PROXY_QUARANTINE_MAX),
            float(config.PROXY_QUARANTINE_BASE) * (2 ** min(self.block_streak - 1, 8)),
        )
        if retry_after_seconds is not None and math.isfinite(retry_after_seconds) and retry_after_seconds > 0:
            duration = min(float(config.PROXY_QUARANTINE_MAX), max(duration, float(retry_after_seconds)))
        self._enter_quarantine(now, duration, reason=reason)
        return duration

    def record_block_or_challenge_failure(
        self,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        reason: str = "block",
    ) -> float:
        return self.record_final_block(reason=reason, retry_after_seconds=retry_after)

    def record_transport_error(self, *, reason: str = "transport") -> float:
        if self.is_direct:
            self.quarantined_until = 0.0
            self._state = "healthy"
            self._sync_metrics_state()
            return 0.0
        self.record_raw_http_response(None, 0.0, is_transport_error=True)
        return self.cooldown_remaining()

    def record_success(self) -> None:
        if self._state == "half_open":
            self._state = "healthy"
            self.quarantine_reason = ""
        if self.block_streak > 0:
            self.block_streak = max(0, self.block_streak - 1)
        self._sync_metrics_state()

    def record_collection_outcome(
        self,
        *,
        success: bool,
        http_requests_count: int,
        duration_seconds: float,
        challenge_solved: bool = False,
        final_block: bool = False,
        fast_skipped: bool = False,
        unchanged: bool = False,
        has_new_items: bool = False,
    ) -> None:
        if success:
            if self._state == "half_open":
                self._state = "healthy"
                self.quarantine_reason = ""
            if self.block_streak > 0:
                self.block_streak = max(0, self.block_streak - 1)
            self._sync_metrics_state()

        METRICS.record_collection(
            self.proxy_id,
            self.masked_url,
            success=success,
            http_requests_count=http_requests_count,
            duration_seconds=duration_seconds,
            challenge_solved=challenge_solved,
            final_block=final_block,
            fast_skipped=fast_skipped,
            unchanged=unchanged,
            has_new_items=has_new_items,
        )

    def _enter_quarantine(self, now: float, duration_seconds: float, *, reason: str) -> None:
        if self.is_direct:
            self.quarantined_until = 0.0
            self.quarantine_reason = ""
            self._state = "healthy"
            self._sync_metrics_state()
            return
        until = now + max(1.0, float(duration_seconds))
        if until > self.quarantined_until:
            self.quarantined_until = until
        self._state = "quarantined"
        self.quarantine_reason = reason
        self._sync_metrics_state()
        channel = short_channel(self.proxy_id, self.masked_url)
        log_event(
            log,
            logging.INFO,
            "proxy.quarantine",
            f"🛑 {channel} в карантин на {duration_seconds:.1f} с · {reason} · {self.effective_rate:.2f} req/s",
            ch=channel,
            s=round(duration_seconds, 1),
            reason=reason,
            rate=self.effective_rate,
        )

    def snapshot(self) -> dict[str, Any]:
        st = self.state
        now = self._monotonic()
        return {
            "proxy_id": self.proxy_id,
            "masked_url": self.masked_url,
            "state": st,
            "configured_rate": self.configured_rate,
            "effective_rate": self.effective_rate,
            "current_rps": self.effective_rate,
            "in_flight": self.in_flight,
            "lanes": self.max_in_flight,
            "quarantined_until": self.quarantined_until,
            "quarantine_remaining": max(0.0, round(self.quarantined_until - now, 1)) if st == "quarantined" else 0.0,
            "quarantine_reason": self.quarantine_reason,
            "cookie_file": str(self.cookie_file),
        }


@dataclass
class ProxyLease:
    client: ProxyClient
    lane: int = 0
    _released: bool = False
    _on_release: Callable[[], None] | None = None

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with self.client._pacing_lock:
            self.client.in_flight = max(0, self.client.in_flight - 1)
            self.client._free_lane(self.lane)
            if self.client._half_open_probe_active:
                self.client._half_open_probe_active = False
        if self._on_release is not None:
            self._on_release()


class ProxyPool:
    """Статический пул прокси-клиентов с поддержкой /reload_proxies без остановки процесса."""

    def __init__(
        self,
        *,
        proxy_pool_file: str | Path | None = None,
        fallback_proxy_url: str | None = None,
        allow_direct: bool | None = None,
        configured_rate: float | None = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        sync_sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._monotonic = monotonic_fn
        self._sleep = sleep_fn
        self._sync_sleep = sync_sleep_fn
        self._configured_rate = configured_rate
        self._clients: dict[str, ProxyClient] = {}
        self._draining_clients: list[ProxyClient] = []
        self._rr_cursor: int = 0
        self._lock = threading.Lock()
        self._lane_freed: asyncio.Event | None = None  # set when a lease ends; scan workers wait on it
        # Smart mode state: planned load from the monitoring loop, until when proxies stay in work, and why.
        self._demand_rps: float = 0.0
        self._smart_mixed_until: float = -math.inf
        self._smart_reason: str = ""
        self._smart_server_first: bool | None = None
        self._alerts: list[str] = []
        self._block_alerted_at: float = -math.inf

        self.load_configuration(
            proxy_pool_file=proxy_pool_file,
            fallback_proxy_url=fallback_proxy_url,
            allow_direct=allow_direct,
        )

    def _resolve_urls(
        self,
        *,
        proxy_pool_file: str | Path | None = None,
        fallback_proxy_url: str | None = None,
        allow_direct: bool | None = None,
    ) -> list[str | None]:
        pool_file = config.PROXY_POOL_FILE if proxy_pool_file is None else str(proxy_pool_file or "").strip()
        single_proxy = config.PROXY_URL if fallback_proxy_url is None else fallback_proxy_url
        direct_ok = config.ALLOW_DIRECT_AVITO if allow_direct is None else bool(allow_direct)
        mode = config.NET_MODE

        if mode == "direct":
            return [None]

        proxies: list[str | None] = []
        if pool_file and (proxy_pool_file is not None or Path(pool_file).expanduser().is_file()):
            proxies = list(load_proxy_urls_from_file(pool_file))
        else:
            raw = list(config.PROXY_URLS) + ([str(single_proxy).strip()] if single_proxy and str(single_proxy).strip() else [])
            for line_number, url in enumerate(raw, start=1):
                normalized = parse_and_validate_proxy_line(url, line_number=line_number)
                if normalized not in proxies:
                    proxies.append(normalized)

        if mode in ("both", "smart"):
            return proxies + [None]
        if mode == "proxy":
            if not proxies:
                log.warning("⚠️ Режим сети «только прокси», но ни один прокси не задан — сканирование на паузе.")
            return proxies

        if proxies:
            return proxies
        if direct_ok:
            return [None]

        log.warning(
            "⚠️ Не настроен ни один прокси (PROXY_POOL_FILE или PROXY_URL), "
            "а прямое подключение без прокси выключено (ALLOW_DIRECT_AVITO=0). "
            "Сканирование Авито ожидает включения прямого режима в боте или добавления прокси."
        )
        return []

    def load_configuration(
        self,
        *,
        proxy_pool_file: str | Path | None = None,
        fallback_proxy_url: str | None = None,
        allow_direct: bool | None = None,
    ) -> dict[str, Any]:
        target_urls = self._resolve_urls(
            proxy_pool_file=proxy_pool_file,
            fallback_proxy_url=fallback_proxy_url,
            allow_direct=allow_direct,
        )

        with self._lock:
            new_map: dict[str, ProxyClient] = {}
            added = 0
            kept = 0
            for url in target_urls:
                pid = compute_proxy_id(url)
                if pid in self._clients:
                    client = self._clients[pid]
                    if self._configured_rate is not None:
                        client.configured_rate = float(self._configured_rate)
                    new_map[pid] = client
                    kept += 1
                else:
                    client = ProxyClient(
                        url,
                        configured_rate=self._configured_rate,
                        monotonic_fn=self._monotonic,
                        sleep_fn=self._sleep,
                        sync_sleep_fn=self._sync_sleep,
                    )
                    new_map[pid] = client
                    added += 1

            removed_clients: list[ProxyClient] = []
            for pid, old_client in self._clients.items():
                if pid not in new_map:
                    old_client._draining = True
                    old_client._sync_metrics_state()
                    removed_clients.append(old_client)

            self._clients = new_map
            self._draining_clients.extend(removed_clients)

        return {
            "total": len(new_map),
            "added": added,
            "kept": kept,
            "removed": len(removed_clients),
        }

    async def reload_async(
        self,
        *,
        proxy_pool_file: str | Path | None = None,
        fallback_proxy_url: str | None = None,
        allow_direct: bool | None = None,
    ) -> dict[str, Any]:
        result = self.load_configuration(
            proxy_pool_file=proxy_pool_file,
            fallback_proxy_url=fallback_proxy_url,
            allow_direct=allow_direct,
        )
        await self._cleanup_draining()
        return result

    async def set_direct_mode_async(self, enabled: bool) -> dict[str, Any]:
        config.ALLOW_DIRECT_AVITO = bool(enabled)
        return await self.reload_async(allow_direct=bool(enabled))

    async def set_proxy_list_async(self, proxy_urls: list[str]) -> dict[str, Any]:
        """Replace the bot-managed proxy list (any size) and hot-swap the pool without restarting."""
        normalized: list[str] = []
        for line_number, url in enumerate(proxy_urls, start=1):
            value = parse_and_validate_proxy_line(url, line_number=line_number)
            if value not in normalized:
                normalized.append(value)
        config.PROXY_URLS = normalized
        return await self.reload_async()

    async def set_net_mode_async(self, mode: str) -> dict[str, Any]:
        if mode not in config.NET_MODES:
            raise ValueError(f"Неизвестный режим сети: {mode}")
        config.NET_MODE = mode
        return await self.reload_async()

    @staticmethod
    def has_configured_proxy() -> bool:
        return bool(config.PROXY_URLS) or bool(config.PROXY_URL) or ProxyPool.pool_file_overrides_single_proxy()

    @staticmethod
    def pool_file_overrides_single_proxy() -> bool:
        """A readable PROXY_POOL_FILE always wins over PROXY_URL in _resolve_urls."""
        pool_file = (config.PROXY_POOL_FILE or "").strip()
        return bool(pool_file) and Path(pool_file).expanduser().is_file()

    async def _cleanup_draining(self) -> None:
        still_draining: list[ProxyClient] = []
        for client in list(self._draining_clients):
            if client.in_flight <= 0:
                await client.close()
            else:
                still_draining.append(client)
        self._draining_clients = still_draining

    @property
    def clients(self) -> list[ProxyClient]:
        with self._lock:
            return list(self._clients.values())

    @property
    def is_direct_only(self) -> bool:
        clients = self.clients
        return bool(clients) and all(c.is_direct for c in clients)

    def healthy_count(self) -> int:
        now = self._monotonic()
        return sum(1 for c in self.clients if c.is_available_for_scan(now))

    def note_demand(self, requests_per_second: float) -> None:
        """Planned load of all links in req/s, from the monitoring loop; smart mode compares it with the server's share."""
        try:
            value = float(requests_per_second)
        except (TypeError, ValueError):
            return
        if math.isfinite(value) and value >= 0:
            self._demand_rps = value

    def _evaluate_smart(self, now: float, clients: list[ProxyClient]) -> bool:
        """
        Smart mode: True while the server scans alone. Proxies join the load when the planned load passes
        SMART_DIRECT_MAX_RPS or the server gets more than SMART_DIRECT_BLOCK_RATIO blocks, and stay in work
        until neither has happened for SMART_HOLD_SECONDS.
        """
        if config.NET_MODE != "smart":
            return False
        direct = next((c for c in clients if c.is_direct), None)
        if direct is None:
            return False
        reason = ""
        limit = float(config.SMART_DIRECT_MAX_RPS)
        answers, blocks = direct.recent_block_share(now)
        if self._demand_rps > limit:
            reason = f"нагрузка {self._demand_rps:.2f} запр/с, сервер один тянет до {limit:.2f}"
        elif answers >= 10 and blocks > float(config.SMART_DIRECT_BLOCK_RATIO):
            reason = f"Авито блокирует сервер: {blocks:.0%} ответов"
        if reason and any(not c.is_direct for c in clients):
            self._smart_mixed_until = max(self._smart_mixed_until, now + float(config.SMART_HOLD_SECONDS))
            self._smart_reason = reason
        server_first = now >= self._smart_mixed_until
        if server_first != self._smart_server_first:
            self._smart_server_first = server_first
            log_event(
                log,
                logging.INFO,
                "net.smart",
                "Умный режим: сервер справляется, прокси в запасе" if server_first
                else f"Умный режим: прокси включены в работу · {self._smart_reason}",
                server_first=server_first,
                reason="" if server_first else self._smart_reason,
                rps=round(self._demand_rps, 3),
                blocks=round(blocks, 3),
            )
        if answers >= SMART_CRITICAL_MIN_ANSWERS and blocks >= SMART_CRITICAL_BLOCK_RATIO \
                and now - self._block_alerted_at >= SMART_BLOCK_ALERT_INTERVAL \
                and not any(not c.is_direct and c.is_available(now) for c in clients):
            self._block_alerted_at = now
            self._alerts.append(
                f"🚨 Авито блокирует IP сервера ({blocks:.0%} ответов), а рабочих прокси нет.\n"
                "Лоты могут опаздывать. Добавьте прокси (Админ-панель → Сеть и прокси) или смените IP сервера."
            )
        return server_first

    def pop_alerts(self) -> list[str]:
        """Messages for admins collected while routing; the monitoring loop sends them."""
        alerts, self._alerts = self._alerts, []
        return alerts

    def smart_status(self) -> dict[str, Any]:
        """What smart mode is doing now, for the network screen."""
        now = self._monotonic()
        with self._lock:
            clients = list(self._clients.values())
            server_first = self._evaluate_smart(now, clients)
        direct = next((c for c in clients if c.is_direct), None)
        answers, blocks = direct.recent_block_share(now) if direct is not None else (0, 0.0)
        return {
            "active": config.NET_MODE == "smart",
            "server_first": server_first,
            "reason": self._smart_reason,
            "demand_rps": self._demand_rps,
            "limit_rps": float(config.SMART_DIRECT_MAX_RPS),
            "server_answers": answers,
            "server_blocks": blocks,
            "hold_left": max(0.0, self._smart_mixed_until - now),
            "has_proxies": any(not c.is_direct for c in clients),
        }

    def acquire_client(self, *, exclude_ids: set[str] | None = None) -> ProxyLease | None:
        """
        Выбирает здоровый или half-open прокси-клиент: сначала по измеренному качеству
        (routing_score), затем по времени готовности с учётом лимита max_in_flight,
        исключая уже опробованные в этом скане прокси.
        """
        now = self._monotonic()
        with self._lock:
            picked = self._choose(now, exclude_ids or set())
            if picked is None:
                return None
            chosen, chosen_offset, n, _ = picked
            self._rr_cursor = (self._rr_cursor + chosen_offset + 1) % n

            chosen.last_acquired_at = now
            with chosen._pacing_lock:
                chosen.in_flight += 1
                lane = chosen._take_lane()
                if chosen._state == "half_open":
                    chosen._half_open_probe_active = True

            return ProxyLease(client=chosen, lane=lane, _on_release=self._notify_lane_freed)

    def _choose(self, now: float, excluded: set[str]) -> tuple[ProxyClient, int, int, bool] | None:
        """The channel a scan would get now (caller holds self._lock):
        (client, its round-robin offset, pool size, smart mode lets the server scan alone)."""
        all_clients = list(self._clients.values())
        if not all_clients:
            return None
        n = len(all_clients)
        server_first = self._evaluate_smart(now, all_clients)
        candidates: list[tuple[int, float, float, int, ProxyClient]] = []
        ready_within = float(config.PROXY_BUSY_PENALTY)
        for offset in range(n):
            idx = (self._rr_cursor + offset) % n
            client = all_clients[idx]
            if client.proxy_id in excluded:
                continue
            if not client.is_available_for_scan(now):
                continue
            score = client.routing_score(now, server_first=server_first)
            with client._pacing_lock:
                # A busy fast channel usually frees up sooner than a throttled idle one becomes ready.
                queued = max(0, client.in_flight - client.max_in_flight + 1)
                starts_at = max(now, client._next_request_at) + queued * float(config.PROXY_BUSY_PENALTY)
                limit = client.max_in_flight + (1 if server_first and client.is_direct else 0)
                free_now = client.in_flight < limit and client._next_request_at - now <= ready_within
            # A channel that can take the scan right now goes before a busy one, unless it is nearly dead (health under PROXY_SPILL_MIN_HEALTH).
            # Before, a busy good proxy (score 0.5) beat a free one whose health a block wave had dragged under
            # 0.5, so scans queued behind the few busy channels while the rest idled (07.11: 67 % of plan with
            # 33 % of channel time free).
            health = client.long_health() if server_first and not client.is_direct else client.recent_health()
            ready_class = 0 if free_now and health >= float(config.PROXY_SPILL_MIN_HEALTH) else 1
            candidates.append((ready_class, -score, starts_at, offset, client))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[:4])
        chosen_offset, chosen = candidates[0][3], candidates[0][4]
        return chosen, chosen_offset, n, server_first

    def has_free_lane(self) -> bool:
        """Would a scan started now get a lane of its own? False when the channel routing would pick is busy:
        the scan then waits in the planner (most overdue first) instead of queueing on that channel's lock
        while another channel idles."""
        with self._lock:
            picked = self._choose(self._monotonic(), set())
        if picked is None:
            return True  # nothing to wait for: the scan finds out by itself (no channel, cooldown)
        client, _, _, server_first = picked
        with client._pacing_lock:
            if server_first and client.is_direct:
                # Smart mode lets one scan queue behind the server; the next one routing sends to a reserve proxy.
                return client.in_flight <= client.max_in_flight
            return client.in_flight < client.max_in_flight

    def _notify_lane_freed(self) -> None:
        if self._lane_freed is not None:
            self._lane_freed.set()

    async def wait_for_free_lane(self, timeout: float = 1.0) -> None:
        if self._lane_freed is None:
            self._lane_freed = asyncio.Event()
        self._lane_freed.clear()
        try:
            await asyncio.wait_for(self._lane_freed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def total_lanes(self) -> int:
        return sum(max(1, int(getattr(client, "max_in_flight", 1))) for client in self.clients)

    def set_lanes(self, lanes: int) -> None:
        """Requests through one proxy at a time, switched live; the server keeps one lane."""
        value = max(1, int(lanes))
        for client in self.clients:
            if not client.is_direct:
                client.max_in_flight = value
                client._sync_metrics_state()

    def earliest_available_delay(self, *, exclude_ids: set[str] | None = None) -> float:
        now = self._monotonic()
        excluded = exclude_ids or set()
        clients = [c for c in self.clients if c.proxy_id not in excluded and not c._draining]
        if not clients:
            return 5.0
        min_ready = min(
            (c.quarantined_until if c.state == "quarantined" else c.estimated_ready_at(now))
            for c in clients
        )
        return max(0.05, min_ready - now)

    def snapshots(self) -> list[dict[str, Any]]:
        return [c.snapshot() for c in self.clients]

    async def close(self) -> None:
        for client in self.clients:
            await client.close()
        for client in list(self._draining_clients):
            await client.close()
        self._draining_clients.clear()


_GLOBAL_PROXY_POOL: ProxyPool | None = None


def get_proxy_pool() -> ProxyPool:
    global _GLOBAL_PROXY_POOL
    if _GLOBAL_PROXY_POOL is None:
        _GLOBAL_PROXY_POOL = ProxyPool()
    return _GLOBAL_PROXY_POOL


async def close_proxy_pool() -> None:
    global _GLOBAL_PROXY_POOL
    pool, _GLOBAL_PROXY_POOL = _GLOBAL_PROXY_POOL, None
    if pool is not None:
        await pool.close()
