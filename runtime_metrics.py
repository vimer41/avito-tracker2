"""Кольцевые runtime-метрики (60 минутных бакетов), калькулятор емкости и алерты."""

from __future__ import annotations

import bisect
from html import escape as html_escape
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

import config

SUBSECOND_LATENCY_BOUNDS: tuple[float, ...] = (
    0.05, 0.10, 0.25, 0.50, 0.75, 1.00, 1.25, 1.50, 2.00, 2.50, 3.00, 4.00, 5.00, 10.0, 30.0, 60.0, float("inf")
)
HTTP_LATENCY_BOUNDS: tuple[float, ...] = (
    0.05, 0.10, 0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00, 5.00, 8.00, 12.0, 20.0, float("inf")
)
SCAN_INTERVAL_BOUNDS: tuple[float, ...] = (
    0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 17.5, 20.0, 25.0, 30.0, 45.0, 60.0, 120.0, 300.0, float("inf")
)
HTTP_COST_BOUNDS: tuple[float, ...] = (
    1.0, 1.2, 1.3, 1.5, 2.0, 3.0, 4.0, 5.0, 8.0, float("inf")
)
PUB_DELAY_BOUNDS: tuple[float, ...] = (
    5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 120.0, 180.0, 300.0, 600.0, 900.0, 1800.0, float("inf")
)
# Load time of a search page (HTTP 200) through a channel, seconds.
PAGE_LATENCY_BOUNDS: tuple[float, ...] = (
    0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 11.0, 20.0, float("inf")
)
CHANNEL_WAIT_NOTICE = 0.05  # a fetch that waited longer for its channel queued behind another scan
THROUGHPUT_MIN_REQUESTS = 10  # requests in 10 minutes before a channel's own speed replaces the pool's typical one

TARIFF_KEYS: tuple[str, ...] = ("pro", "standart", "lite", "base")

# Why a scan or a proxy probe failed (FetchFailure.kind), in words an admin understands.
FAILURE_LABELS = {
    "timeout": "таймаут (прокси медленный или мёртвый)",
    "network": "сетевая ошибка (прокси недоступен или неверные данные)",
    "forbidden": "Авито заблокировал IP (403)",
    "rate_limit": "Авито ограничил частоту (429)",
    "challenge": "антибот-проверка не пройдена",
    "cooldown": "канал на паузе после блокировки",
    "no_channel": "нет свободного канала (все на паузе или заняты)",
    "catalog_missing": "страница без каталога (логин/заглушка)",
    "proxy_auth": "прокси не принял логин/пароль (407) — проверьте данные или привязку по IP",
    "proxy_forbidden": "провайдер прокси блокирует Авито (403 на CONNECT) — нужен другой провайдер или тариф",
    "proxy_refused": "прокси не отвечает на этом порту (закрыт или неверный адрес)",
    "proxy_dns": "адрес прокси не найден",
    "proxy_error": "прокси вернул ошибку",
    "unknown": "неизвестная ошибка",
}

_URL_CREDENTIALS_RE = re.compile(
    r"(?i)\b(https?|socks5h?)://([^\s/@:]+):([^\s/@]+)@([^\s/]+)"
)
_COLON_PROXY_RE = re.compile(
    r"\b((?:\d{1,3}\.){3}\d{1,3}:\d{2,5}):([^\s:]+):([^\s]+)\b"
)


def sanitize_proxy_credentials(text: str | None) -> str:
    """Маскирует логин и пароль прокси в любом тексте лога или ошибки."""
    if not text:
        return ""
    masked = _URL_CREDENTIALS_RE.sub(r"\1://***@\4", str(text))
    masked = _COLON_PROXY_RE.sub(r"\1:***", masked)
    return masked


def mask_proxy_url(proxy_url: str | None) -> str:
    if not proxy_url or not proxy_url.strip():
        return "direct://local"
    normalized = config.parse_proxy(proxy_url) or proxy_url.strip()
    return sanitize_proxy_credentials(normalized)


@dataclass
class HistogramSlice:
    bounds: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    sample_count: int = 0
    total_sum: float = 0.0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * len(self.bounds)

    def reset(self) -> None:
        for i in range(len(self.counts)):
            self.counts[i] = 0
        self.sample_count = 0
        self.total_sum = 0.0

    def observe(self, value: float) -> None:
        if not math.isfinite(value):
            return
        v = max(0.0, float(value))
        idx = bisect.bisect_left(self.bounds, v)
        if idx >= len(self.bounds):
            idx = len(self.bounds) - 1
        self.counts[idx] += 1
        self.sample_count += 1
        self.total_sum += v


def summarize_histograms(slices: Iterable[HistogramSlice], bounds: tuple[float, ...]) -> dict[str, Any]:
    merged = [0] * len(bounds)
    total_samples = 0
    total_sum = 0.0
    for sl in slices:
        if sl.sample_count <= 0:
            continue
        total_samples += sl.sample_count
        total_sum += sl.total_sum
        for idx, c in enumerate(sl.counts):
            merged[idx] += c

    if total_samples == 0:
        return {
            "sample_count": 0,
            "mean": None,
            "p50": None,
            "p95": None,
        }

    def _percentile(q: float) -> float:
        target = max(1, math.ceil(total_samples * q))
        running = 0
        for idx, count in enumerate(merged):
            running += count
            if running >= target:
                bound = bounds[idx]
                if math.isinf(bound) and idx > 0:
                    return bounds[idx - 1]
                return bound
        return bounds[-2] if len(bounds) > 1 and math.isinf(bounds[-1]) else bounds[-1]

    return {
        "sample_count": total_samples,
        "mean": round(total_sum / total_samples, 4),
        "p50": _percentile(0.50),
        "p95": _percentile(0.95),
    }


def sparse_histogram(sl: HistogramSlice) -> dict[str, int]:
    """Non-empty buckets keyed by their upper bound ("inf" for the last), so a report merges minutes exactly."""
    return {("inf" if math.isinf(bound) else f"{bound:g}"): count for bound, count in zip(sl.bounds, sl.counts) if count}


def _make_tariff_histograms(bounds: tuple[float, ...]) -> dict[str, HistogramSlice]:
    return {k: HistogramSlice(bounds) for k in TARIFF_KEYS}


@dataclass
class ProxyMinuteSlice:
    minute_id: int = -1
    raw_http_total: int = 0
    raw_http_2xx_3xx: int = 0
    raw_http_403: int = 0
    raw_http_429: int = 0
    raw_http_5xx: int = 0
    transport_errors: int = 0
    collection_total: int = 0
    collection_success: int = 0
    challenges_solved: int = 0
    final_blocks: int = 0
    scans_new: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    http_latency: HistogramSlice = field(default_factory=lambda: HistogramSlice(HTTP_LATENCY_BOUNDS))
    # Load accounting. A channel serves one fetch at a time, so busy_s / 60 is how much of the minute it worked.
    fetches: int = 0
    busy_s: float = 0.0  # held by a fetch: answers, broken requests, rate-limit waits, anti-bot
    wait_s: float = 0.0  # scans queued behind another scan's fetch on this channel
    waited: int = 0
    pace_s: float = 0.0  # token-bucket waits inside busy_s
    http_s: float = 0.0  # Avito answered (any status)
    terr_s: float = 0.0  # spent on requests that broke: timeouts, resets
    kb: float = 0.0
    pages: int = 0  # HTTP 200
    page_kb: float = 0.0
    ttfb_s: float = 0.0
    ttfb_n: int = 0
    page_latency: HistogramSlice = field(default_factory=lambda: HistogramSlice(PAGE_LATENCY_BOUNDS))

    def reset(self, minute_id: int) -> None:
        self.minute_id = minute_id
        self.fetches = self.waited = self.pages = self.ttfb_n = 0
        self.busy_s = self.wait_s = self.pace_s = self.http_s = self.terr_s = 0.0
        self.kb = self.page_kb = self.ttfb_s = 0.0
        self.page_latency.reset()
        self.raw_http_total = 0
        self.raw_http_2xx_3xx = 0
        self.raw_http_403 = 0
        self.raw_http_429 = 0
        self.raw_http_5xx = 0
        self.transport_errors = 0
        self.collection_total = 0
        self.collection_success = 0
        self.challenges_solved = 0
        self.final_blocks = 0
        self.scans_new = 0
        self.failures.clear()
        self.http_latency.reset()


@dataclass
class MinuteBucket:
    minute_id: int = -1
    # Histograms (normal flow)
    pub_to_seen: dict[str, HistogramSlice] = field(default_factory=lambda: _make_tariff_histograms(PUB_DELAY_BOUNDS))
    seen_to_queued: dict[str, HistogramSlice] = field(default_factory=lambda: _make_tariff_histograms(SUBSECOND_LATENCY_BOUNDS))
    queued_to_sent: dict[str, HistogramSlice] = field(default_factory=lambda: _make_tariff_histograms(SUBSECOND_LATENCY_BOUNDS))
    deliver_at_to_sent: dict[str, HistogramSlice] = field(default_factory=lambda: _make_tariff_histograms(SUBSECOND_LATENCY_BOUNDS))
    deliver_at_to_sent_all: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))
    queued_to_sent_all: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))

    # Catch-up flow histograms (separated from normal SLA)
    catchup_queued_to_sent: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))
    catchup_deliver_at_to_sent: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))

    # Scan & HTTP & Bot API
    http_cost_per_scan: HistogramSlice = field(default_factory=lambda: HistogramSlice(HTTP_COST_BOUNDS))
    scan_duration: HistogramSlice = field(default_factory=lambda: HistogramSlice(SCAN_INTERVAL_BOUNDS))
    source_interval: HistogramSlice = field(default_factory=lambda: HistogramSlice(SCAN_INTERVAL_BOUNDS))
    fanout_duration: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))
    bot_api_service_time: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))
    scan_due_lag: HistogramSlice = field(default_factory=lambda: HistogramSlice(SCAN_INTERVAL_BOUNDS))
    pub_to_sent: HistogramSlice = field(default_factory=lambda: HistogramSlice(PUB_DELAY_BOUNDS))

    # Counters
    scans_coverage_gap: int = 0
    items_new: int = 0
    items_queued: int = 0
    items_stop_word: int = 0
    items_filtered: int = 0
    items_seller: int = 0
    items_too_old: int = 0
    items_title: int = 0
    bot_api_flood_max_seconds: float = 0.0
    loop_lag_max: float = 0.0
    loop_stalls: int = 0
    challenges: dict[str, list[float]] = field(default_factory=dict)  # kind -> [ok, failed, seconds]
    pub_timestamp_missing_count: int = 0
    scans_completed: int = 0
    scans_fast_skipped: int = 0
    scans_unchanged: int = 0
    scans_with_new_items: int = 0
    scans_failed: int = 0
    bot_api_sent_total: int = 0
    bot_api_retry_after_count: int = 0
    photo_file_id_hit_count: int = 0
    photo_url_direct_count: int = 0
    photo_cdn_upload_count: int = 0
    photo_fallback_text_count: int = 0
    text_only_sent_count: int = 0
    catchup_sent_count: int = 0
    catchup_omitted_count: int = 0
    outbox_expired_count: int = 0
    outbox_failed_count: int = 0
    # Load accounting: every scan's duration (failed ones too), channels tried, scan workers sampled once a second.
    scan_any: HistogramSlice = field(default_factory=lambda: HistogramSlice(SCAN_INTERVAL_BOUNDS))
    scan_busy_s: float = 0.0
    scan_tries: dict[int, int] = field(default_factory=dict)
    worker_samples: int = 0
    workers_sum: int = 0
    workers_busy_sum: int = 0
    workers_busy_max: int = 0
    queue_sum: int = 0
    queue_max: int = 0
    queue_oldest_max: float = 0.0

    def reset(self, minute_id: int) -> None:
        self.minute_id = minute_id
        self.scan_any.reset()
        self.scan_busy_s = self.queue_oldest_max = 0.0
        self.scan_tries.clear()
        self.worker_samples = self.workers_sum = self.workers_busy_sum = self.workers_busy_max = 0
        self.queue_sum = self.queue_max = 0
        for d in (self.pub_to_seen, self.seen_to_queued, self.queued_to_sent, self.deliver_at_to_sent):
            for sl in d.values():
                sl.reset()
        self.deliver_at_to_sent_all.reset()
        self.queued_to_sent_all.reset()
        self.catchup_queued_to_sent.reset()
        self.catchup_deliver_at_to_sent.reset()
        self.http_cost_per_scan.reset()
        self.scan_duration.reset()
        self.source_interval.reset()
        self.fanout_duration.reset()
        self.bot_api_service_time.reset()
        self.scan_due_lag.reset()
        self.pub_to_sent.reset()
        self.scans_coverage_gap = 0
        self.items_new = 0
        self.items_queued = 0
        self.items_stop_word = 0
        self.items_filtered = 0
        self.items_seller = 0
        self.items_too_old = 0
        self.items_title = 0
        self.bot_api_flood_max_seconds = 0.0
        self.loop_lag_max = 0.0
        self.loop_stalls = 0
        self.challenges.clear()
        self.pub_timestamp_missing_count = 0
        self.scans_completed = 0
        self.scans_fast_skipped = 0
        self.scans_unchanged = 0
        self.scans_with_new_items = 0
        self.scans_failed = 0
        self.bot_api_sent_total = 0
        self.bot_api_retry_after_count = 0
        self.photo_file_id_hit_count = 0
        self.photo_url_direct_count = 0
        self.photo_cdn_upload_count = 0
        self.photo_fallback_text_count = 0
        self.text_only_sent_count = 0
        self.catchup_sent_count = 0
        self.catchup_omitted_count = 0
        self.outbox_expired_count = 0
        self.outbox_failed_count = 0


def calculate_capacity_report(
    *,
    unique_sources: int,
    proxy_endpoints: list[dict[str, Any]],
    rolling_mean_cost: float | None,
    p95_cost: float | None,
    completed_scans_count: int,
    interval_min: float | None = None,
    interval_max: float | None = None,
    initial_cost: float | None = None,
    min_samples: int | None = None,
    reserve_fraction: float | None = None,
    recommendation_max_rate: float | None = None,
) -> dict[str, Any]:
    s_min = float(config.SCAN_INTERVAL_MIN if interval_min is None else interval_min)
    s_max = float(config.SCAN_INTERVAL_MAX if interval_max is None else interval_max)
    s_target = (s_min + s_max) / 2.0
    init_cost = float(config.PROXY_INITIAL_HTTP_COST_PER_SCAN if initial_cost is None else initial_cost)
    min_samp = int(config.HTTP_COST_MIN_SAMPLES if min_samples is None else min_samples)
    reserve = float(config.PROXY_CAPACITY_RESERVE if reserve_fraction is None else reserve_fraction)
    rec_max = float(
        config.PROXY_RATE_RECOMMENDATION_MAX if recommendation_max_rate is None else recommendation_max_rate
    )

    use_cold_start = completed_scans_count < min_samp or rolling_mean_cost is None
    effective_mean_cost = init_cost if use_cold_start else max(1.0, float(rolling_mean_cost))
    effective_p95_cost = init_cost if (use_cold_start or p95_cost is None) else max(effective_mean_cost, float(p95_cost))

    u = max(0, int(unique_sources))
    required_rate_at_max_mean = round(u * effective_mean_cost / s_max, 4)
    required_rate_target_mean = round(u * effective_mean_cost / s_target, 4)
    required_rate_at_max_p95 = round(u * effective_p95_cost / s_max, 4)
    required_rate_target_p95 = round(u * effective_p95_cost / s_target, 4)

    total_proxies = len(proxy_endpoints)
    healthy_list = [
        p for p in proxy_endpoints
        if p.get("state") in ("healthy", "half_open")
        and float(p.get("effective_rate") or p.get("current_rps") or 0.0) > 0
    ]
    quarantined_list = [p for p in proxy_endpoints if p.get("state") == "quarantined"]
    healthy_count = len(healthy_list)
    quarantined_count = len(quarantined_list)

    # A channel gives what it measurably does (lanes / service time, capped by its rate limit), not its rate
    # limit: with pages loading 2-3 s a proxy did ~0.35 req/s while the limit said 1.5. A channel not measured
    # yet counts as the typical measured one.
    measured = [float(p["throughput_rps"]) for p in healthy_list if p.get("throughput_rps")]
    typical_rps = sum(measured) / len(measured) if measured else None

    def channel_rps(p: dict[str, Any]) -> float:
        if p.get("throughput_rps"):
            return float(p["throughput_rps"])
        limit = float(p.get("effective_rate") or p.get("current_rps") or 0.0)
        return min(limit, typical_rps) if typical_rps else limit

    available_rate_now = round(sum(channel_rps(p) for p in healthy_list), 4)

    # Reserve: exclude top 20% endpoints by effective_rate; already quarantined count toward the 20%
    reserve_target_healthy = math.floor(total_proxies * (1.0 - reserve)) if total_proxies > 0 else 0
    if total_proxies == 1 and healthy_count == 1 and reserve < 1.0:
        # With 1 proxy, reserve cannot drop below 0; reserve_keep_count is 0 for strict 20% reserve or 1 for capacity_proxy_count
        reserve_keep_count = 0
    else:
        reserve_keep_count = min(healthy_count, max(0, reserve_target_healthy))

    sorted_healthy = sorted(healthy_list, key=channel_rps)
    kept_for_reserve = sorted_healthy[:reserve_keep_count]
    available_rate_with_reserve = round(sum(channel_rps(p) for p in kept_for_reserve), 4)

    deficit_now = round(max(0.0, required_rate_at_max_mean - available_rate_now), 4)
    deficit_with_reserve = round(max(0.0, required_rate_target_mean - available_rate_with_reserve), 4)
    deficit_now_p95 = round(max(0.0, required_rate_at_max_p95 - available_rate_now), 4)
    deficit_with_reserve_p95 = round(max(0.0, required_rate_target_p95 - available_rate_with_reserve), 4)

    if total_proxies > 0 and healthy_count > 0:
        capacity_proxy_count = max(1, min(healthy_count, max(1, reserve_target_healthy)))
    elif total_proxies > 0:
        capacity_proxy_count = max(1, reserve_target_healthy)
    else:
        capacity_proxy_count = 1

    required_configured_rate_hard = round(required_rate_at_max_mean / capacity_proxy_count, 4)
    required_configured_rate_target = round(required_rate_target_mean / capacity_proxy_count, 4)
    required_configured_rate_hard_p95 = round(required_rate_at_max_p95 / capacity_proxy_count, 4)
    required_configured_rate_target_p95 = round(required_rate_target_p95 / capacity_proxy_count, 4)

    # Determine planner capacity mode
    if u == 0 or available_rate_with_reserve >= required_rate_target_mean:
        capacity_mode = "normal"
        recommended_interval_low = s_min
    elif available_rate_now >= required_rate_target_mean:
        capacity_mode = "full_capacity"
        recommended_interval_low = min(s_max, max(s_min, 16.0))
    elif available_rate_now >= required_rate_at_max_mean:
        capacity_mode = "boundary"
        recommended_interval_low = min(s_max, max(s_min, 18.0))
    else:
        capacity_mode = "deficit"
        recommended_interval_low = min(s_max, max(s_min, 19.0))

    # Next safe manual step evaluation across pool
    total_raw_60m = sum(int(p.get("raw_http_total_60m", 0)) for p in proxy_endpoints)
    total_2xx_3xx_60m = sum(int(p.get("raw_http_2xx_3xx_60m", 0)) for p in proxy_endpoints)
    total_403_429_10m = sum(int(p.get("raw_http_403_429_10m", 0)) for p in proxy_endpoints)
    raw_success_ratio_60m = (total_2xx_3xx_60m / total_raw_60m) if total_raw_60m > 0 else None

    p95_latencies = [
        float(p["p95_latency_seconds"])
        for p in proxy_endpoints
        if p.get("p95_latency_seconds") is not None and float(p["p95_latency_seconds"]) > 0
    ]
    pool_p95_latency = max(p95_latencies) if p95_latencies else None
    configured_rates = [float(p.get("configured_rate", config.PROXY_RATE_LIMIT)) for p in proxy_endpoints]
    current_configured_rate = min(configured_rates) if configured_rates else float(config.PROXY_RATE_LIMIT)

    latency_rate_cap = round(0.85 / pool_p95_latency, 4) if (pool_p95_latency and pool_p95_latency > 0) else rec_max

    eligible_for_step = (
        total_raw_60m >= 100
        and total_403_429_10m == 0
        and raw_success_ratio_60m is not None
        and raw_success_ratio_60m >= 0.99
    )

    needs_more_proxies = (
        required_configured_rate_target > rec_max
        or required_configured_rate_target > latency_rate_cap
        or total_proxies == 0
    )

    if eligible_for_step:
        next_safe_manual_step = round(
            min(current_configured_rate * 1.25, latency_rate_cap, rec_max),
            4,
        )
    else:
        next_safe_manual_step = None

    if total_proxies == 0:
        recommendation_ru = "добавить прокси (пул прокси пуст)"
    elif needs_more_proxies and deficit_with_reserve > 0:
        recommendation_ru = (
            f"добавить прокси (требуется {required_configured_rate_target:.2f} req/s/прокси "
            f"при потолке {min(rec_max, latency_rate_cap):.2f} req/s)"
        )
    elif deficit_now > 0:
        if next_safe_manual_step and next_safe_manual_step > current_configured_rate:
            recommendation_ru = (
                f"дефицит емкости: можно осторожно поднять PROXY_RATE_LIMIT до {next_safe_manual_step:.2f} "
                "или добавить прокси"
            )
        else:
            recommendation_ru = "дефицит емкости: добавить прокси (условия для повышения PROXY_RATE_LIMIT не выполнены)"
    elif eligible_for_step and next_safe_manual_step and next_safe_manual_step > current_configured_rate:
        recommendation_ru = f"емкость в норме; допустим ручной шаг PROXY_RATE_LIMIT до {next_safe_manual_step:.2f}"
    else:
        recommendation_ru = "емкость в норме; изменение PROXY_RATE_LIMIT не требуется"

    return {
        "unique_sources": u,
        "cold_start_cost_used": use_cold_start,
        "rolling_mean_http_requests_per_scan": round(effective_mean_cost, 4),
        "p95_http_requests_per_scan": round(effective_p95_cost, 4),
        "required_rate_at_max": required_rate_at_max_mean,
        "required_rate_target": required_rate_target_mean,
        "required_rate_at_max_p95": required_rate_at_max_p95,
        "required_rate_target_p95": required_rate_target_p95,
        "total_proxies": total_proxies,
        "measured_channels": len(measured),
        "typical_channel_rps": round(typical_rps, 4) if typical_rps else None,
        "healthy_proxies": healthy_count,
        "quarantined_proxies": quarantined_count,
        "capacity_proxy_count": capacity_proxy_count,
        "available_rate_now": available_rate_now,
        "available_rate_with_reserve": available_rate_with_reserve,
        "deficit_now": deficit_now,
        "deficit_with_reserve": deficit_with_reserve,
        "deficit_now_p95": deficit_now_p95,
        "deficit_with_reserve_p95": deficit_with_reserve_p95,
        "required_configured_rate_hard": required_configured_rate_hard,
        "required_configured_rate_target": required_configured_rate_target,
        "required_configured_rate_hard_p95": required_configured_rate_hard_p95,
        "required_configured_rate_target_p95": required_configured_rate_target_p95,
        "capacity_mode": capacity_mode,
        "recommended_interval_low": recommended_interval_low,
        "recommended_interval_high": s_max,
        "eligible_for_rate_increase": eligible_for_step,
        "next_safe_manual_step": next_safe_manual_step,
        "latency_rate_cap": latency_rate_cap,
        "needs_more_proxies": needs_more_proxies,
        "recommendation_ru": recommendation_ru,
    }


def calculate_required_bot_workers(
    p95_service_time_seconds: float | None,
    target_rate: float | None = None,
) -> int:
    rate = float(config.TELEGRAM_GLOBAL_RATE if target_rate is None else target_rate)
    if p95_service_time_seconds is None or p95_service_time_seconds <= 0:
        return config.NOTIFICATION_WORKERS
    return max(1, math.ceil(rate * float(p95_service_time_seconds) * 1.2))


# Admin alerts are for real trouble only (the owner asked on 2026-11-06: no noise about load, server or proxies).
# A rule must stay bad for its whole streak, repeats at most every ALERT_REPEAT_SECONDS while still bad,
# and says «recovered» only after ALERT_RECOVER_MINUTES good minutes in a row. Day-to-day load lives in /stats,
# the 09:00 report and the readiness report, not in pushes.
ALERT_REPEAT_SECONDS = 6 * 3600
ALERT_RECOVER_MINUTES = 10
CRITICAL_DELIVERY_P95_SECONDS = 60.0   # lots wait over a minute after they were due
CRITICAL_DELIVERY_MINUTES = 10
CRITICAL_OUTBOX_MINUTES = 5            # the send queue stays above OUTBOX_ALERT_DEPTH
CRITICAL_CAPACITY_SHARE = 0.5          # the channels give less than half of what the tariff intervals need
CRITICAL_CAPACITY_MINUTES = 30


@dataclass
class AlertRuleState:
    consecutive_bad: int = 0
    consecutive_ok: int = 0
    active: bool = False
    last_sent_at: float = -1e9


class RuntimeMetricsRegistry:
    """Потокобезопасный in-memory реестр 60-минутных метрик и детектор алертов."""

    NUM_BUCKETS = 60

    def __init__(
        self,
        monotonic_fn: Callable[[], float] = time.monotonic,
        wall_clock_fn: Callable[[], float] = time.time,
    ) -> None:
        self._monotonic = monotonic_fn
        self._wall_clock = wall_clock_fn
        self._started_at = wall_clock_fn()
        self._buckets: list[MinuteBucket] = [MinuteBucket() for _ in range(self.NUM_BUCKETS)]
        self._proxy_rings: dict[str, list[ProxyMinuteSlice]] = {}
        self._proxy_meta: dict[str, dict[str, Any]] = {}
        self._alert_states: dict[str, AlertRuleState] = {
            "delivery_sla": AlertRuleState(),
            "outbox_depth": AlertRuleState(),
            "capacity_deficit": AlertRuleState(),
        }
        self._source_stats: dict[str, dict[str, Any]] = {}
        self._alert_cooldown_seconds: float = ALERT_REPEAT_SECONDS
        self._last_capacity_report: dict[str, Any] = {}
        # Scans per second the planner aims for (sum of 1/interval over URLs); None = uniform SCAN_INTERVAL_* range.
        self._planned_scan_rate: float | None = None
        self._outbox_live_state: dict[str, Any] = {
            "pending_depth": 0,
            "leased_depth": 0,
            "oldest_pending_age_seconds": 0.0,
        }
        self._scheduler_live_state: dict[str, Any] = {
            "unique_sources": 0,
            "overdue_sources": 0,
            "backoff_sources": 0,
            "full_coverage_age_seconds": None,
            "capacity_mode": "normal",
        }
        self._reset_daily()
        self._last_row_minute = self._current_minute_id() - 1
        self._cpu_mark = (time.process_time(), self._monotonic())

    # ── Daily totals for the morning admin report (the minute buckets keep only an hour) ──

    def _reset_daily(self) -> None:
        self._daily_started = self._wall_clock()
        self._daily_scans_ok = 0
        self._daily_scans_failed = 0
        self._daily_sent = 0
        self._daily_pub_to_sent = HistogramSlice(PUB_DELAY_BOUNDS)

    def pop_daily_summary(self, reset: bool = True) -> dict[str, Any]:
        """Totals since the previous reset (or start); the morning report resets them, a preview does not."""
        summary = {
            "hours": max(0.0, (self._wall_clock() - self._daily_started) / 3600.0),
            "scans_ok": self._daily_scans_ok,
            "scans_failed": self._daily_scans_failed,
            "sent": self._daily_sent,
            "pub_to_sent": summarize_histograms([self._daily_pub_to_sent], PUB_DELAY_BOUNDS),
        }
        if reset:
            self._reset_daily()
        return summary

    def _current_minute_id(self) -> int:
        return int(self._monotonic() // 60.0)

    def _bucket(self) -> MinuteBucket:
        mid = self._current_minute_id()
        idx = mid % self.NUM_BUCKETS
        bucket = self._buckets[idx]
        if bucket.minute_id != mid:
            bucket.reset(mid)
        return bucket

    def _active_buckets(self, window_minutes: int = 60) -> list[MinuteBucket]:
        now_mid = self._current_minute_id()
        min_mid = now_mid - max(1, min(self.NUM_BUCKETS, window_minutes)) + 1
        return [b for b in self._buckets if min_mid <= b.minute_id <= now_mid]

    def _proxy_slice(self, proxy_id: str, masked_url: str | None = None) -> ProxyMinuteSlice:
        if proxy_id not in self._proxy_rings:
            self._proxy_rings[proxy_id] = [ProxyMinuteSlice() for _ in range(self.NUM_BUCKETS)]
        if masked_url and proxy_id not in self._proxy_meta:
            self._proxy_meta[proxy_id] = {
                "proxy_id": proxy_id,
                "masked_url": sanitize_proxy_credentials(masked_url),
                "state": "healthy",
                "configured_rate": config.PROXY_RATE_LIMIT,
                "effective_rate": config.PROXY_RATE_LIMIT,
                "quarantined_until": 0.0,
                "quarantine_reason": "",
                "quarantine_remaining": 0.0,
            }
        mid = self._current_minute_id()
        idx = mid % self.NUM_BUCKETS
        sl = self._proxy_rings[proxy_id][idx]
        if sl.minute_id != mid:
            sl.reset(mid)
        return sl

    def update_proxy_state(
        self,
        proxy_id: str,
        *,
        masked_url: str,
        state: str,
        configured_rate: float,
        effective_rate: float,
        quarantined_until: float = 0.0,
        quarantine_reason: str = "",
        quarantine_remaining: float = 0.0,
        lanes: int = 1,
    ) -> None:
        self._proxy_slice(proxy_id, masked_url)
        self._proxy_meta[proxy_id] = {
            "proxy_id": proxy_id,
            "masked_url": sanitize_proxy_credentials(masked_url),
            "state": state,
            "configured_rate": float(configured_rate),
            "effective_rate": float(effective_rate),
            "quarantined_until": float(quarantined_until),
            "quarantine_reason": sanitize_proxy_credentials(quarantine_reason),
            "quarantine_remaining": float(quarantine_remaining or 0.0),
            "lanes": max(1, int(lanes or 1)),
        }

    def record_http_request(
        self,
        proxy_id: str,
        masked_url: str,
        status_code: int | None,
        latency_seconds: float,
        *,
        is_transport_error: bool = False,
        size_bytes: int = 0,
        ttfb_seconds: float | None = None,
    ) -> None:
        sl = self._proxy_slice(proxy_id, masked_url)
        sl.raw_http_total += 1
        if is_transport_error or status_code is None:
            sl.transport_errors += 1  # no answer, so no latency: a zero would pull the channel's p50 down
            return
        sl.http_latency.observe(latency_seconds)
        sl.http_s += max(0.0, latency_seconds)
        size_kb = max(0, int(size_bytes or 0)) / 1024.0
        sl.kb += size_kb
        if status_code == 200:
            sl.pages += 1
            sl.page_kb += size_kb
            sl.page_latency.observe(latency_seconds)
            if ttfb_seconds is not None and 0 < ttfb_seconds <= latency_seconds + 1.0:
                sl.ttfb_s += ttfb_seconds
                sl.ttfb_n += 1
        if 200 <= status_code < 400:
            sl.raw_http_2xx_3xx += 1
        elif status_code == 403:
            sl.raw_http_403 += 1
        elif status_code == 429:
            sl.raw_http_429 += 1
        elif status_code >= 500:
            sl.raw_http_5xx += 1

    def record_collection(
        self,
        proxy_id: str,
        masked_url: str,
        *,
        success: bool,
        http_requests_count: int,
        duration_seconds: float,
        challenge_solved: bool = False,
        final_block: bool = False,
        fast_skipped: bool = False,
        unchanged: bool = False,
        has_new_items: bool = False,
        failure_kind: str | None = None,
    ) -> None:
        b = self._bucket()
        sl = self._proxy_slice(proxy_id, masked_url)
        sl.collection_total += 1
        if success and has_new_items:
            sl.scans_new += 1
        if not success:
            kind = failure_kind or "unknown"
            sl.failures[kind] = sl.failures.get(kind, 0) + 1
        if challenge_solved:
            sl.challenges_solved += 1
        if final_block:
            sl.final_blocks += 1

        if success:
            sl.collection_success += 1
            b.scans_completed += 1
            if http_requests_count > 0:
                b.http_cost_per_scan.observe(float(http_requests_count))
            b.scan_duration.observe(duration_seconds)
            if fast_skipped:
                b.scans_fast_skipped += 1
            if unchanged:
                b.scans_unchanged += 1
            if has_new_items:
                b.scans_with_new_items += 1
        else:
            b.scans_failed += 1

    def record_channel_use(self, proxy_id: str, masked_url: str, *, wait_seconds: float, busy_seconds: float) -> None:
        """One fetch through a channel: how long it queued behind another scan, then how long it held the channel."""
        sl = self._proxy_slice(proxy_id, masked_url)
        sl.fetches += 1
        sl.busy_s += max(0.0, busy_seconds)
        sl.wait_s += max(0.0, wait_seconds)
        sl.waited += int(wait_seconds >= CHANNEL_WAIT_NOTICE)

    def record_pacing(self, proxy_id: str, masked_url: str, seconds: float) -> None:
        """A request waited for the channel's token bucket."""
        if seconds > 0:
            self._proxy_slice(proxy_id, masked_url).pace_s += seconds

    def record_transport_time(self, proxy_id: str, masked_url: str, seconds: float) -> None:
        """Time lost on a request that broke (timeout, reset); its error is counted by record_http_request."""
        if seconds > 0:
            self._proxy_slice(proxy_id, masked_url).terr_s += seconds

    def sample_workers(self, *, total: int, busy: int, queued: int, oldest_due: float) -> None:
        """Once a second from the dispatch loop: scan workers in use and due links waiting for one."""
        b = self._bucket()
        b.worker_samples += 1
        b.workers_sum += max(0, int(total))
        b.workers_busy_sum += max(0, int(busy))
        b.workers_busy_max = max(b.workers_busy_max, int(busy))
        b.queue_sum += max(0, int(queued))
        b.queue_max = max(b.queue_max, int(queued))
        b.queue_oldest_max = max(b.queue_oldest_max, float(oldest_due or 0.0))

    def record_source_interval(self, interval_seconds: float) -> None:
        if interval_seconds > 0:
            self._bucket().source_interval.observe(interval_seconds)

    def record_fanout_batch(self, duration_seconds: float) -> None:
        self._bucket().fanout_duration.observe(duration_seconds)

    def record_publication_delay(
        self,
        tariff: str,
        pub_to_seen_seconds: float | None,
        *,
        reliable_t_pub: bool = True,
        is_catchup: bool = False,
    ) -> None:
        b = self._bucket()
        if not reliable_t_pub or pub_to_seen_seconds is None:
            b.pub_timestamp_missing_count += 1
            return
        if is_catchup:
            return
        t_key = tariff if tariff in TARIFF_KEYS else "base"
        b.pub_to_seen[t_key].observe(pub_to_seen_seconds)

    def record_seen_to_queued(self, tariff: str, seconds: float, *, is_catchup: bool = False) -> None:
        if is_catchup:
            return
        b = self._bucket()
        t_key = tariff if tariff in TARIFF_KEYS else "base"
        b.seen_to_queued[t_key].observe(seconds)

    def record_delivery(
        self,
        tariff: str,
        *,
        queued_to_sent_seconds: float | None = None,
        deliver_at_to_sent_seconds: float | None = None,
        bot_api_service_seconds: float = 0.0,
        delivery_mode: str = "text",
        is_catchup: bool = False,
        t_pub: float | None = None,
        t_pub_reliable: bool = False,
        t_seen: float | None = None,
        t_queued: float | None = None,
        deliver_at: float | None = None,
        t_sent: float | None = None,
    ) -> None:
        if t_sent is not None and t_queued is not None and queued_to_sent_seconds is None:
            queued_to_sent_seconds = max(0.0, float(t_sent) - float(t_queued))
        if t_sent is not None and deliver_at is not None and deliver_at_to_sent_seconds is None:
            deliver_at_to_sent_seconds = max(0.0, float(t_sent) - float(deliver_at))
        if t_seen is not None:
            pub_delay = max(0.0, float(t_seen) - float(t_pub)) if (t_pub is not None and t_pub_reliable) else None
            self.record_publication_delay(
                tariff,
                pub_delay,
                reliable_t_pub=bool(t_pub_reliable and t_pub is not None),
                is_catchup=is_catchup,
            )
            if t_queued is not None:
                self.record_seen_to_queued(tariff, max(0.0, float(t_queued) - float(t_seen)), is_catchup=is_catchup)

        q_sec = float(queued_to_sent_seconds or 0.0)
        d_sec = float(deliver_at_to_sent_seconds or 0.0)

        self._daily_sent += 1
        b = self._bucket()
        if t_sent is not None and t_pub is not None and t_pub_reliable and not is_catchup:
            self._daily_pub_to_sent.observe(max(0.0, float(t_sent) - float(t_pub)))
            b.pub_to_sent.observe(max(0.0, float(t_sent) - float(t_pub)))

        b.bot_api_sent_total += 1
        b.bot_api_service_time.observe(float(bot_api_service_seconds or 0.0))

        if delivery_mode == "photo_file_id":
            b.photo_file_id_hit_count += 1
        elif delivery_mode == "photo_url":
            b.photo_url_direct_count += 1
        elif delivery_mode == "photo_upload":
            b.photo_cdn_upload_count += 1
        elif delivery_mode == "photo_fallback_text":
            b.photo_fallback_text_count += 1
        else:
            b.text_only_sent_count += 1

        if is_catchup:
            b.catchup_sent_count += 1
            b.catchup_queued_to_sent.observe(q_sec)
            b.catchup_deliver_at_to_sent.observe(d_sec)
            return

        t_key = tariff if tariff in TARIFF_KEYS else "base"
        b.queued_to_sent[t_key].observe(q_sec)
        b.deliver_at_to_sent[t_key].observe(d_sec)
        b.queued_to_sent_all.observe(q_sec)
        b.deliver_at_to_sent_all.observe(d_sec)

    def record_outbox_expired(self, count: int = 1) -> None:
        self._bucket().outbox_expired_count += int(count)

    def record_outbox_failed(self, count: int = 1) -> None:
        self._bucket().outbox_failed_count += int(count)

    def record_source_scan(self, key: str, *, ok: bool, attempts: list[str], query: str = "", target: str = "") -> None:
        """Per-link totals for the readiness report; they leave with pop_source_stats, not with every minute row."""
        stats = self._source_stats.setdefault(key, {"ok": 0, "fail": 0, "retry": 0, "blocked": 0})
        stats["ok" if ok else "fail"] += 1
        stats["retry"] += len(attempts)
        stats["blocked"] += sum(1 for attempt in attempts if attempt.endswith("forbidden"))
        stats.update(q=query, to=target)

    def pop_source_stats(self) -> dict[str, dict[str, Any]]:
        stats, self._source_stats = self._source_stats, {}
        return stats

    def record_scan_attempt(
        self,
        *,
        success: bool,
        elapsed_ms: float = 0.0,
        pages_fetched: int = 0,
        http_requests: int = 1,
        fast_skipped: bool = False,
        coverage_gap: bool = False,
        revisit_seconds: float | None = None,
        due_lag_seconds: float | None = None,
        is_catchup: bool = False,
        proxy_id: str = "direct",
        masked_url: str = "direct",
        failure_kind: str | None = None,
        channels_tried: int = 0,
    ) -> None:
        bucket = self._bucket()
        duration = max(0.0, float(elapsed_ms) / 1000.0)
        bucket.scan_any.observe(duration)
        bucket.scan_busy_s += duration
        if channels_tried > 0:
            bucket.scan_tries[int(channels_tried)] = bucket.scan_tries.get(int(channels_tried), 0) + 1
        if revisit_seconds is not None and revisit_seconds > 0:
            self.record_source_interval(revisit_seconds)
        if due_lag_seconds is not None:
            self._bucket().scan_due_lag.observe(due_lag_seconds)
        if coverage_gap:
            self._bucket().scans_coverage_gap += 1
        if success:
            self._daily_scans_ok += 1
        else:
            self._daily_scans_failed += 1
        self.record_collection(
            proxy_id,
            masked_url,
            success=success,
            http_requests_count=max(0, int(http_requests)),
            duration_seconds=max(0.0, float(elapsed_ms) / 1000.0),
            fast_skipped=fast_skipped,
            has_new_items=success and not fast_skipped,
            failure_kind=failure_kind,
        )

    def record_catchup_omitted(self, omitted_count: int) -> None:
        if omitted_count > 0:
            self._bucket().catchup_omitted_count += int(omitted_count)

    def record_bot_api_retry_after(self, seconds: float = 0.0) -> None:
        b = self._bucket()
        b.bot_api_retry_after_count += 1
        b.bot_api_flood_max_seconds = max(b.bot_api_flood_max_seconds, float(seconds))

    def record_loop_lag(self, lag_seconds: float, *, stall: bool = False) -> None:
        b = self._bucket()
        b.loop_lag_max = max(b.loop_lag_max, float(lag_seconds))
        b.loop_stalls += int(stall)

    def record_challenge(self, kind: str, *, solved: bool, seconds: float) -> None:
        stats = self._bucket().challenges.setdefault(kind, [0, 0, 0.0])
        stats[0 if solved else 1] += 1
        stats[2] += float(seconds)

    def record_fanout_items(
        self, *, new: int = 0, queued: int = 0, stop_word: int = 0, seller: int = 0, too_old: int = 0, title: int = 0,
        filtered: int = 0,
    ) -> None:
        """Lot funnel of one radar: what the scan found and where each lot went."""
        b = self._bucket()
        b.items_new += new
        b.items_queued += queued
        b.items_stop_word += stop_word
        b.items_filtered += filtered
        b.items_seller += seller
        b.items_too_old += too_old
        b.items_title += title

    def process_stats(self) -> dict[str, Any]:
        """RSS, this process's share of one CPU since the previous call, and the system load."""
        cpu_time, wall = time.process_time(), self._monotonic()
        prev_cpu, prev_wall = self._cpu_mark
        self._cpu_mark = (cpu_time, wall)
        stats: dict[str, Any] = {"cpu_pct": round(100.0 * (cpu_time - prev_cpu) / max(wall - prev_wall, 0.001), 1)}
        try:
            with open("/proc/self/statm", encoding="ascii") as statm:
                stats["rss_mb"] = round(int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576, 1)
            stats["load1"] = round(os.getloadavg()[0], 2)
        except (OSError, AttributeError, ValueError):
            pass  # not Linux
        stats.update(self._system_deltas())
        return stats

    def _system_deltas(self) -> dict[str, Any]:
        """Whole-server CPU split since the previous call: busy, waiting for the disk, taken by the host (steal),
        and pages swapped in/out. Tells a slow server from slow code when the loop stalls. Linux only."""
        current = _read_cpu_and_swap()
        if current is None:
            return {}
        previous, self._sys_mark = getattr(self, "_sys_mark", None), current
        out = _read_memory()
        if previous is None:
            return out
        cpu_now, cpu_before = current["cpu"], previous["cpu"]
        total = max(1, sum(cpu_now) - sum(cpu_before))
        delta = [now - before for now, before in zip(cpu_now, cpu_before)]
        # /proc/stat: user nice system idle iowait irq softirq steal
        out.update(
            sys_cpu=round(100.0 * (delta[0] + delta[1] + delta[2] + delta[5] + delta[6]) / total, 1),
            iowait=round(100.0 * delta[4] / total, 1),
            steal=round(100.0 * delta[7] / total, 1) if len(delta) > 7 else 0.0,
            swap_in=max(0, current["pswpin"] - previous["pswpin"]),
            swap_out=max(0, current["pswpout"] - previous["pswpout"]),
        )
        return out

    def system_now(self) -> dict[str, Any]:
        """Instant server state for an alert: load, swap in use, available memory."""
        out = _read_memory()
        try:
            out["load1"] = round(os.getloadavg()[0], 2)
        except (OSError, AttributeError):
            pass
        return out

    def pop_minute_rows(self) -> list[dict[str, Any]]:
        """Finished minutes not yet returned, oldest first. Each is one row of logs/metrics.jsonl."""
        now_mid = self._current_minute_id()
        first = max(self._last_row_minute + 1, now_mid - self.NUM_BUCKETS + 1)
        wall_now = self._wall_clock()
        rows = []
        for mid in range(first, now_mid):
            bucket = self._buckets[mid % self.NUM_BUCKETS]
            if bucket.minute_id == mid:
                rows.append(self._minute_row(bucket, wall_now - (now_mid - mid - 1) * 60.0))
        self._last_row_minute = max(self._last_row_minute, now_mid - 1)
        return rows

    def _minute_row(self, b: MinuteBucket, epoch: float) -> dict[str, Any]:
        failures: dict[str, int] = {}
        channels: dict[str, dict[str, Any]] = {}
        for proxy_id, ring in self._proxy_rings.items():
            sl = ring[b.minute_id % self.NUM_BUCKETS]
            if sl.minute_id != b.minute_id:
                continue
            for kind, count in sl.failures.items():
                failures[kind] = failures.get(kind, 0) + count
            meta = self._proxy_meta.get(proxy_id) or {}
            entry = channels[short_channel(proxy_id, meta.get("masked_url"))] = {
                "http": sl.raw_http_total,
                "ok": sl.raw_http_2xx_3xx,
                "403": sl.raw_http_403,
                "429": sl.raw_http_429,
                "5xx": sl.raw_http_5xx,
                "terr": sl.transport_errors,
                "coll": sl.collection_total,
                "coll_ok": sl.collection_success,
                "ms_p50": _ms(summarize_histograms([sl.http_latency], HTTP_LATENCY_BOUNDS)["p50"]),
                "fail": dict(sl.failures),
                "state": meta.get("state", "healthy"),
                "rate": meta.get("effective_rate"),
            }
            # Load fields only when non-zero: twenty channels a minute must not bloat metrics.jsonl.
            extra = {
                "fetch": sl.fetches, "busy_s": round(sl.busy_s, 1), "wait_s": round(sl.wait_s, 1), "waited": sl.waited,
                "pace_s": round(sl.pace_s, 1), "http_s": round(sl.http_s, 1), "terr_s": round(sl.terr_s, 1),
                "kb": round(sl.kb), "pages": sl.pages, "page_kb": round(sl.page_kb),
                "ttfb_ms": round(1000 * sl.ttfb_s / sl.ttfb_n) if sl.ttfb_n else 0,
                "lat_h": sparse_histogram(sl.page_latency),
            }
            entry.update({key: value for key, value in extra.items() if value})
        pub_seen = summarize_histograms(b.pub_to_seen.values(), PUB_DELAY_BOUNDS)
        pub_sent = summarize_histograms([b.pub_to_sent], PUB_DELAY_BOUNDS)
        due_lag = summarize_histograms([b.scan_due_lag], SCAN_INTERVAL_BOUNDS)
        scan_time = summarize_histograms([b.scan_duration], SCAN_INTERVAL_BOUNDS)
        interval = summarize_histograms([b.source_interval], SCAN_INTERVAL_BOUNDS)
        fanout = summarize_histograms([b.fanout_duration], SUBSECOND_LATENCY_BOUNDS)
        return {
            "t": round(epoch),
            "ts": time.strftime("%m-%d %H:%M", time.gmtime(epoch + 3 * 3600)),
            "scans": {
                "ok": b.scans_completed,
                "fail": b.scans_failed,
                "fast": b.scans_fast_skipped,
                "new": b.scans_with_new_items,
                "gap": b.scans_coverage_gap,
            },
            "fail": failures,
            "ch": channels,
            "scan_s": {"p50": scan_time["p50"], "p95": scan_time["p95"]},
            "due_s": {"p50": due_lag["p50"], "p95": due_lag["p95"]},
            "revisit_s": {"p50": interval["p50"], "p95": interval["p95"]},
            "fanout_s_p95": fanout["p95"],
            "lots": {
                "new": b.items_new,
                "queued": b.items_queued,
                "stop": b.items_stop_word,
                "filter": b.items_filtered,
                "seller": b.items_seller,
                "old": b.items_too_old,
                "title": b.items_title,
            },
            "pub_seen_s": {"n": pub_seen["sample_count"], "p50": pub_seen["p50"], "p95": pub_seen["p95"]},
            "pub_sent_s": {"n": pub_sent["sample_count"], "p50": pub_sent["p50"], "p95": pub_sent["p95"]},
            "sent": b.bot_api_sent_total,
            "send_s_p95": summarize_histograms([b.queued_to_sent_all], SUBSECOND_LATENCY_BOUNDS)["p95"],
            "lost": {"expired": b.outbox_expired_count, "failed": b.outbox_failed_count},
            "flood": b.bot_api_retry_after_count,
            "flood_max_s": round(b.bot_api_flood_max_seconds, 1),
            "challenge": {
                kind: {"ok": int(ok), "fail": int(failed), "avg_s": round(seconds / max(ok + failed, 1), 2)}
                for kind, (ok, failed, seconds) in b.challenges.items()
            },
            "loop_max_ms": _ms(b.loop_lag_max),
            "stalls": b.loop_stalls,
            "load": self._load_fields(b),
        }

    def _load_fields(self, b: MinuteBucket) -> dict[str, Any]:
        """Plan against fact for the load report: scans the planner wants, how long scans took and waited,
        how busy the scan workers were. Histograms are sparse so minutes merge exactly."""
        samples = max(1, b.worker_samples)
        return {
            "plan_sps": round(self._planned_scan_rate or 0.0, 3),
            "scan_busy_s": round(b.scan_busy_s, 1),
            "scan_h": sparse_histogram(b.scan_any),
            "due_h": sparse_histogram(b.scan_due_lag),
            "revisit_h": sparse_histogram(b.source_interval),
            "tries": {str(tries): count for tries, count in sorted(b.scan_tries.items())},
            "workers": round(b.workers_sum / samples, 1),
            "busy": round(b.workers_busy_sum / samples, 2),
            "busy_max": b.workers_busy_max,
            "queue": round(b.queue_sum / samples, 2),
            "queue_max": b.queue_max,
            "oldest_s": round(b.queue_oldest_max, 1),
        }

    def set_planned_scan_rate(self, scans_per_second: float | None) -> None:
        self._planned_scan_rate = scans_per_second if scans_per_second and scans_per_second > 0 else None

    def update_outbox_state(
        self,
        *,
        pending_depth: int,
        leased_depth: int = 0,
        oldest_pending_age_seconds: float = 0.0,
    ) -> None:
        self._outbox_live_state = {
            "pending_depth": max(0, int(pending_depth)),
            "leased_depth": max(0, int(leased_depth)),
            "oldest_pending_age_seconds": round(max(0.0, float(oldest_pending_age_seconds)), 2),
        }

    def update_scheduler_state(
        self,
        *,
        unique_sources: int,
        overdue_sources: int = 0,
        backoff_sources: int = 0,
        full_coverage_age_seconds: float | None = None,
        capacity_mode: str = "normal",
    ) -> None:
        self._scheduler_live_state = {
            "unique_sources": max(0, int(unique_sources)),
            "overdue_sources": max(0, int(overdue_sources)),
            "backoff_sources": max(0, int(backoff_sources)),
            "full_coverage_age_seconds": (
                round(float(full_coverage_age_seconds), 2)
                if full_coverage_age_seconds is not None
                else None
            ),
            "capacity_mode": capacity_mode,
        }

    def get_proxy_summaries(self) -> list[dict[str, Any]]:
        now_mid = self._current_minute_id()
        min_60 = now_mid - 59
        min_10 = now_mid - 9
        results: list[dict[str, Any]] = []
        for proxy_id, meta in sorted(self._proxy_meta.items()):
            ring = self._proxy_rings.get(proxy_id, [])
            slices_60 = [s for s in ring if min_60 <= s.minute_id <= now_mid]
            slices_10 = [s for s in ring if min_10 <= s.minute_id <= now_mid]

            raw_60 = sum(s.raw_http_total for s in slices_60)
            ok_60 = sum(s.raw_http_2xx_3xx for s in slices_60)
            c403_60 = sum(s.raw_http_403 for s in slices_60)
            c429_60 = sum(s.raw_http_429 for s in slices_60)
            c5xx_60 = sum(s.raw_http_5xx for s in slices_60)
            terr_60 = sum(s.transport_errors for s in slices_60)
            coll_total_60 = sum(s.collection_total for s in slices_60)
            coll_ok_60 = sum(s.collection_success for s in slices_60)
            chal_60 = sum(s.challenges_solved for s in slices_60)
            fblk_60 = sum(s.final_blocks for s in slices_60)

            raw_10 = sum(s.raw_http_total for s in slices_10)
            c403_429_10 = sum(s.raw_http_403 + s.raw_http_429 for s in slices_10)
            failures_60: dict[str, int] = {}
            for s in slices_60:
                for kind, count in s.failures.items():
                    failures_60[kind] = failures_60.get(kind, 0) + count

            lat_summary = summarize_histograms([s.http_latency for s in slices_60], HTTP_LATENCY_BOUNDS)
            # What the channel really gives: it serves `lanes` requests at a time, each takes its service time
            # (answers, broken requests, anti-bot; not rate-limit waits), and never more than its rate limit.
            work_10 = sum(s.busy_s - s.pace_s for s in slices_10)
            lanes = int(meta.get("lanes") or 1)
            service_time = work_10 / raw_10 if raw_10 >= THROUGHPUT_MIN_REQUESTS and work_10 > 0 else None
            throughput = (
                round(min(float(meta["effective_rate"]), lanes / service_time), 4) if service_time else None
            )

            results.append({
                "lanes": lanes,
                "service_time_seconds": round(service_time, 3) if service_time else None,
                "throughput_rps": throughput,
                "proxy_id": proxy_id,
                "masked_url": meta["masked_url"],
                "state": meta["state"],
                "configured_rate": meta["configured_rate"],
                "effective_rate": meta["effective_rate"],
                "quarantined_until": meta["quarantined_until"],
                "quarantine_reason": meta.get("quarantine_reason", ""),
                "quarantine_remaining": meta.get("quarantine_remaining", 0.0),
                "raw_http_total_60m": raw_60,
                "raw_http_2xx_3xx_60m": ok_60,
                "raw_http_success_ratio_60m": round(ok_60 / raw_60, 4) if raw_60 > 0 else None,
                "raw_http_403_60m": c403_60,
                "raw_http_429_60m": c429_60,
                "raw_http_5xx_60m": c5xx_60,
                "transport_errors_60m": terr_60,
                "raw_http_total_10m": raw_10,
                "raw_http_403_429_10m": c403_429_10,
                "collection_total_60m": coll_total_60,
                "collection_success_60m": coll_ok_60,
                "collection_success_ratio_60m": round(coll_ok_60 / coll_total_60, 4) if coll_total_60 > 0 else None,
                "challenges_solved_60m": chal_60,
                "final_blocks_60m": fblk_60,
                "scans_new_60m": sum(s.scans_new for s in slices_60),
                "failures_60m": failures_60,
                "collection_total_10m": sum(s.collection_total for s in slices_10),
                "collection_success_10m": sum(s.collection_success for s in slices_10),
                "p50_latency_seconds": lat_summary["p50"],
                "p95_latency_seconds": lat_summary["p95"],
                "latency_samples": lat_summary["sample_count"],
            })
        return results

    def snapshot(
        self,
        window_minutes: int = 60,
        *,
        active_unique_urls: int | None = None,
        proxy_snapshots: list[dict[str, Any]] | None = None,
        outbox_metrics: dict[str, Any] | None = None,
        full_coverage_age_seconds: float | None = None,
    ) -> dict[str, Any]:
        if active_unique_urls is not None or full_coverage_age_seconds is not None:
            self.update_scheduler_state(
                unique_sources=(
                    active_unique_urls
                    if active_unique_urls is not None
                    else self._scheduler_live_state["unique_sources"]
                ),
                overdue_sources=self._scheduler_live_state["overdue_sources"],
                backoff_sources=self._scheduler_live_state["backoff_sources"],
                full_coverage_age_seconds=(
                    full_coverage_age_seconds
                    if full_coverage_age_seconds is not None
                    else self._scheduler_live_state["full_coverage_age_seconds"]
                ),
                capacity_mode=self._scheduler_live_state["capacity_mode"],
            )
        if outbox_metrics is not None:
            self.update_outbox_state(
                pending_depth=outbox_metrics.get("pending", outbox_metrics.get("pending_depth", 0)),
                leased_depth=outbox_metrics.get("leased", outbox_metrics.get("leased_depth", 0)),
                oldest_pending_age_seconds=outbox_metrics.get(
                    "oldest_seconds", outbox_metrics.get("oldest_pending_age_seconds", 0.0)
                ),
            )
        if proxy_snapshots is not None:
            for ps in proxy_snapshots:
                pid = ps.get("proxy_id")
                if pid:
                    self.update_proxy_state(
                        pid,
                        masked_url=ps.get("masked_url") or "direct",
                        state=ps.get("state") or "healthy",
                        configured_rate=float(ps.get("configured_rate") or config.PROXY_RATE_LIMIT),
                        effective_rate=float(ps.get("effective_rate") or ps.get("current_rps") or config.PROXY_RATE_LIMIT),
                        quarantined_until=float(ps.get("quarantined_until") or 0.0),
                        quarantine_reason=str(ps.get("quarantine_reason") or ""),
                        quarantine_remaining=float(ps.get("quarantine_remaining") or 0.0),
                        lanes=int(ps.get("lanes") or 1),
                    )
        buckets = self._active_buckets(window_minutes)
        cost_summary = summarize_histograms([b.http_cost_per_scan for b in buckets], HTTP_COST_BOUNDS)
        scan_dur_summary = summarize_histograms([b.scan_duration for b in buckets], SCAN_INTERVAL_BOUNDS)
        interval_summary = summarize_histograms([b.source_interval for b in buckets], SCAN_INTERVAL_BOUNDS)
        fanout_summary = summarize_histograms([b.fanout_duration for b in buckets], SUBSECOND_LATENCY_BOUNDS)
        bot_api_summary = summarize_histograms([b.bot_api_service_time for b in buckets], SUBSECOND_LATENCY_BOUNDS)

        deliver_all = summarize_histograms([b.deliver_at_to_sent_all for b in buckets], SUBSECOND_LATENCY_BOUNDS)
        queued_all = summarize_histograms([b.queued_to_sent_all for b in buckets], SUBSECOND_LATENCY_BOUNDS)
        catchup_deliver = summarize_histograms(
            [b.catchup_deliver_at_to_sent for b in buckets], SUBSECOND_LATENCY_BOUNDS
        )
        catchup_queued = summarize_histograms(
            [b.catchup_queued_to_sent for b in buckets], SUBSECOND_LATENCY_BOUNDS
        )

        by_tariff: dict[str, dict[str, Any]] = {}
        for t_key in TARIFF_KEYS:
            by_tariff[t_key] = {
                "pub_to_seen_seconds": summarize_histograms([b.pub_to_seen[t_key] for b in buckets], PUB_DELAY_BOUNDS),
                "seen_to_queued_seconds": summarize_histograms(
                    [b.seen_to_queued[t_key] for b in buckets], SUBSECOND_LATENCY_BOUNDS
                ),
                "queued_to_sent_seconds": summarize_histograms(
                    [b.queued_to_sent[t_key] for b in buckets], SUBSECOND_LATENCY_BOUNDS
                ),
                "deliver_at_to_sent_seconds": summarize_histograms(
                    [b.deliver_at_to_sent[t_key] for b in buckets], SUBSECOND_LATENCY_BOUNDS
                ),
            }

        scans_completed = sum(b.scans_completed for b in buckets)
        scans_fast_skipped = sum(b.scans_fast_skipped for b in buckets)
        scans_unchanged = sum(b.scans_unchanged for b in buckets)
        scans_with_new = sum(b.scans_with_new_items for b in buckets)
        scans_failed = sum(b.scans_failed for b in buckets)

        proxy_summaries = self.get_proxy_summaries()
        unique_sources = self._scheduler_live_state["unique_sources"]
        # Tariff intervals differ per URL: the mean interval that gives the planned scan rate stands in for the range.
        mean_interval = (
            unique_sources / self._planned_scan_rate
            if self._planned_scan_rate and unique_sources
            else None
        )
        capacity = calculate_capacity_report(
            unique_sources=unique_sources,
            proxy_endpoints=proxy_summaries,
            rolling_mean_cost=cost_summary["mean"],
            p95_cost=cost_summary["p95"],
            completed_scans_count=scans_completed,
            interval_min=mean_interval,
            interval_max=mean_interval,
        )
        capacity["available_rps"] = capacity["available_rate_now"]
        capacity["required_rps"] = capacity["required_rate_target"]
        capacity["deficit"] = capacity["deficit_now"] > 0
        self._last_capacity_report = capacity

        req_workers = calculate_required_bot_workers(bot_api_summary["p95"])

        return {
            "window_minutes": window_minutes,
            "scheduler": dict(self._scheduler_live_state),
            "outbox": dict(self._outbox_live_state),
            "capacity": capacity,
            "avg_http_requests_per_scan": capacity["rolling_mean_http_requests_per_scan"],
            "proxies": proxy_summaries,
            "by_tariff": by_tariff,
            "delivery_sla": {
                "deliver_at_to_sent_seconds": deliver_all,
                "queued_to_sent_seconds": queued_all,
                "catchup_deliver_at_to_sent_seconds": catchup_deliver,
                "catchup_queued_to_sent_seconds": catchup_queued,
                "by_tariff": by_tariff,
                "pub_timestamp_missing_count": sum(b.pub_timestamp_missing_count for b in buckets),
            },
            "scan_pipeline": {
                "scans_completed": scans_completed,
                "scans_fast_skipped": scans_fast_skipped,
                "scans_unchanged": scans_unchanged,
                "scans_with_new_items": scans_with_new,
                "scans_failed": scans_failed,
                "fast_skip_ratio": round(scans_fast_skipped / scans_completed, 4) if scans_completed > 0 else None,
                "http_cost_per_scan": cost_summary,
                "scan_duration_seconds": scan_dur_summary,
                "source_interval_seconds": interval_summary,
                "fanout_duration_seconds": fanout_summary,
            },
            "bot_api": {
                "sent_total": sum(b.bot_api_sent_total for b in buckets),
                "retry_after_count": sum(b.bot_api_retry_after_count for b in buckets),
                "service_time_seconds": bot_api_summary,
                "configured_workers": config.NOTIFICATION_WORKERS,
                "required_workers_estimate": req_workers,
                "photo_file_id_hit_count": sum(b.photo_file_id_hit_count for b in buckets),
                "photo_url_direct_count": sum(b.photo_url_direct_count for b in buckets),
                "photo_cdn_upload_count": sum(b.photo_cdn_upload_count for b in buckets),
                "photo_fallback_text_count": sum(b.photo_fallback_text_count for b in buckets),
                "text_only_sent_count": sum(b.text_only_sent_count for b in buckets),
                "catchup_sent_count": sum(b.catchup_sent_count for b in buckets),
                "catchup_omitted_count": sum(b.catchup_omitted_count for b in buckets),
            },
        }

    def evaluate_minute_alerts(
        self,
        *,
        active_unique_urls: int | None = None,
        proxy_snapshots: list[dict[str, Any]] | None = None,
        outbox_metrics: dict[str, Any] | None = None,
        full_coverage_age_seconds: float | None = None,
    ) -> list[str]:
        """
        Вызывается раз в минуту. Возвращает алерты и восстановления — только о серьёзных сбоях:
        - лоты ждут отправки дольше CRITICAL_DELIVERY_P95_SECONDS (p95) CRITICAL_DELIVERY_MINUTES минут подряд;
        - очередь отправки больше OUTBOX_ALERT_DEPTH CRITICAL_OUTBOX_MINUTES минут подряд;
        - каналы дают меньше половины нужной скорости CRITICAL_CAPACITY_MINUTES минут подряд.
        Повтор того же алерта — не чаще ALERT_REPEAT_SECONDS, «восстановлено» — после ALERT_RECOVER_MINUTES хороших минут.
        """
        now = self._monotonic()
        current_bucket = self._bucket()
        snap = self.snapshot(
            window_minutes=60,
            active_unique_urls=active_unique_urls,
            proxy_snapshots=proxy_snapshots,
            outbox_metrics=outbox_metrics,
            full_coverage_age_seconds=full_coverage_age_seconds,
        )
        cap = snap["capacity"]
        outbox = snap["outbox"]

        messages: list[str] = []

        def _process_rule(
            key: str,
            is_bad: bool,
            threshold_minutes: int,
            has_data: bool,
            alert_text: str,
            recovery_text: str,
        ) -> None:
            st = self._alert_states[key]
            if not has_data:
                return
            if is_bad:
                st.consecutive_bad += 1
                st.consecutive_ok = 0
                if st.consecutive_bad >= threshold_minutes:
                    if not st.active or (now - st.last_sent_at) >= self._alert_cooldown_seconds:
                        st.active = True
                        st.last_sent_at = now
                        messages.append(alert_text)
            else:
                st.consecutive_bad = 0
                st.consecutive_ok += 1
                # One good minute in a bad hour is not a recovery: no «восстановлено» / alert ping-pong.
                if st.active and st.consecutive_ok >= ALERT_RECOVER_MINUTES:
                    st.active = False
                    messages.append(recovery_text)

        # 1. Lots go out late: a Telegram flood limit or a stuck sender.
        cur_deliv = summarize_histograms([current_bucket.deliver_at_to_sent_all], SUBSECOND_LATENCY_BOUNDS)
        has_deliv_data = cur_deliv["sample_count"] > 0
        deliv_bad = bool(has_deliv_data and cur_deliv["p95"] is not None
                         and cur_deliv["p95"] > CRITICAL_DELIVERY_P95_SECONDS)
        _process_rule(
            "delivery_sla",
            deliv_bad,
            CRITICAL_DELIVERY_MINUTES,
            has_deliv_data,
            f"🚨 Лоты уходят с опозданием уже {CRITICAL_DELIVERY_MINUTES} мин: отправка ждёт до {cur_deliv['p95']} с.\n"
            "Обычно это ограничение Telegram или зависшая отправка. Пришлите «Отчёт для отладки» (Админ-панель → Логи).",
            "✅ Лоты снова уходят без задержки.",
        )

        # 2. The send queue does not drain.
        pending_depth = int(outbox["pending_depth"])
        _process_rule(
            "outbox_depth",
            pending_depth > config.OUTBOX_ALERT_DEPTH,
            CRITICAL_OUTBOX_MINUTES,
            True,
            f"🚨 Очередь отправки не разгребается {CRITICAL_OUTBOX_MINUTES} мин: ждут {pending_depth} лотов.\n"
            "Пришлите «Отчёт для отладки» (Админ-панель → Логи).",
            "✅ Очередь отправки разобрана.",
        )

        # 3. Far too little network: links are scanned at well under half of their tariff speed.
        # A few weak or resting proxies are normal and stay out of pushes; the 09:00 report shows them.
        total_p = cap["total_proxies"]
        is_direct_only = (total_p == 1 and bool((proxy_snapshots or [{}])[0].get("is_direct", False)))
        need = float(cap["required_rate_at_max"] or 0.0)
        have = float(cap["available_rate_now"] or 0.0)
        cap_bad = not is_direct_only and need > 0 and have < need * CRITICAL_CAPACITY_SHARE
        typical = float(cap.get("typical_channel_rps") or 0.0)
        more = math.ceil(max(0.0, need * 1.2 - have) / typical) if typical > 0 else 0
        _process_rule(
            "capacity_deficit",
            cap_bad,
            CRITICAL_CAPACITY_MINUTES,
            cap["unique_sources"] > 0 or total_p > 0,
            f"🚨 Сети не хватает уже {CRITICAL_CAPACITY_MINUTES} мин: каналы дают {have:.2f} запр/с из нужных {need:.2f}.\n"
            "Ссылки проверяются заметно реже, лоты опаздывают. Добавьте прокси: Админ-панель → Сеть и прокси."
            + (f"\nНужно ещё примерно {more} хороших прокси (один даёт ~{typical:.2f} запр/с)." if more else ""),
            "✅ Мощности сети снова хватает.",
        )

        return messages

    def format_stats_messages(self, max_len: int = 3800) -> list[str]:
        snap = self.snapshot(window_minutes=60)
        sched = snap["scheduler"]
        outbox = snap["outbox"]
        cap = snap["capacity"]
        sla = snap["delivery_sla"]
        pipe = snap["scan_pipeline"]
        bapi = snap["bot_api"]
        proxies = snap["proxies"]
        buckets_10 = self._active_buckets(10)

        scans_ok = pipe["scans_completed"]
        scans_bad = pipe["scans_failed"]
        scans_all = scans_ok + scans_bad
        scans_ok_10 = sum(b.scans_completed for b in buckets_10)
        scans_bad_10 = sum(b.scans_failed for b in buckets_10)
        blocks_10 = sum(p["raw_http_403_429_10m"] for p in proxies)
        quarantined = [p for p in proxies if p["state"] == "quarantined"]

        # Plain-language verdict first, so a glance answers "is everything fine?".
        problems: list[str] = []
        if scans_all >= 20 and scans_bad / scans_all > 0.10:
            problems.append(f"много неудачных сканов: {_pct(scans_bad, scans_all)}")
        if cap["deficit_now"] > 0:
            problems.append(f"не хватает скорости сети: {_dec(cap['deficit_now'], 2)} запр/с")
        if quarantined:
            problems.append(f"на паузе каналов: {len(quarantined)} из {len(proxies)}")
        if outbox["pending_depth"] > config.OUTBOX_ALERT_DEPTH:
            problems.append(f"очередь уведомлений растёт: {outbox['pending_depth']}")
        deliver = sla["deliver_at_to_sent_seconds"]
        if deliver["sample_count"] and deliver["p95"] is not None and deliver["p95"] > float(config.DELIVERY_SLA_ALERT_SECONDS):
            problems.append(f"уведомления уходят медленно: до {_dur(deliver['p95'])}")
        if sched["unique_sources"] and scans_all == 0:
            problems.append("за час не было ни одного скана")

        if problems:
            verdict = ["🔴 <b>Есть проблемы</b>"] + [f"• {p}" for p in problems]
        elif scans_all == 0:
            verdict = ["⚪️ <b>Данных пока нет</b> — сканов ещё не было"]
        else:
            verdict = ["🟢 <b>Всё работает нормально</b>"]

        if cap["available_rate_now"] > 0:
            if cap["deficit_now"] > 0:
                net_line = (
                    f"Нужно <b>{_dec(cap['required_rate_target'], 2)}</b> запр/с, сеть даёт "
                    f"<b>{_dec(cap['available_rate_now'], 2)}</b> — не хватает"
                )
            else:
                spare = 1.0 - cap["required_rate_target"] / cap["available_rate_now"]
                net_line = (
                    f"Нужно <b>{_dec(cap['required_rate_target'], 2)}</b> запр/с, сеть даёт "
                    f"<b>{_dec(cap['available_rate_now'], 2)}</b> — запас {max(0.0, spare) * 100:.0f}%"
                )
        else:
            net_line = "Рабочих каналов нет — сканирование стоит"

        new_ratio = _pct(pipe["scans_with_new_items"], scans_ok) if scans_ok else "—"
        lines = [
            "📊 <b>Мониторинг</b> · последний час",
            f"Бот работает {_dur(self._wall_clock() - self._started_at)}",
            "",
            *verdict,
            "",
            "🔎 <b>Сканирование</b>",
            f"Ссылок в работе: <b>{sched['unique_sources']}</b>"
            + (f", опаздывают: {sched['overdue_sources']}" if sched["overdue_sources"] else ""),
            f"Каждая ссылка проверяется: {_hist_text(pipe['source_interval_seconds'])}",
            f"Сканов: <b>{_num(scans_ok)}</b> успешно, <b>{_num(scans_bad)}</b> с ошибкой"
            + (f" ({_pct(scans_bad, scans_all)})" if scans_all else ""),
            f"Нашли новые лоты: {_num(pipe['scans_with_new_items'])} ({new_ratio} сканов)",
            f"Один скан длится: {_hist_text(pipe['scan_duration_seconds'])}",
            f"Запросов на скан: {_dec(cap['rolling_mean_http_requests_per_scan'], 1)}"
            + (" (оценка)" if cap["cold_start_cost_used"] else ""),
            f"За 10 мин: сканов {_num(scans_ok_10 + scans_bad_10)}, ошибок {_num(scans_bad_10)}, блоков 403/429 {_num(blocks_10)}",
        ]
        failures = _merge_failures(proxies)
        if failures:
            lines.append("Почему падали сканы:")
            for kind, count in failures[:5]:
                lines.append(f"  · {FAILURE_LABELS.get(kind, kind)} — {_num(count)}")

        lines += [
            "",
            "🌐 <b>Сеть</b>",
            f"Каналов: {cap['total_proxies']}, рабочих {cap['healthy_proxies']}"
            + (f", на паузе {cap['quarantined_proxies']}" if cap["quarantined_proxies"] else ""),
            net_line,
        ]
        if cap["deficit_now"] > 0 or cap["quarantined_proxies"]:
            lines.append(f"<blockquote>{cap['recommendation_ru']}</blockquote>")

        lines += [
            "",
            "📬 <b>Доставка</b>",
            f"Отправлено уведомлений: <b>{_num(bapi['sent_total'])}</b>",
        ]
        pub_hist = _merge_tariff_hist(sla["by_tariff"], "pub_to_seen_seconds")
        if pub_hist:
            lines.append(f"От публикации на Авито до бота: {pub_hist}")
        lines.append(f"Из очереди в Telegram: {_hist_text(sla['queued_to_sent_seconds'])}")
        tariff_parts = []
        for t_key in TARIFF_KEYS:
            h = sla["by_tariff"][t_key]["queued_to_sent_seconds"]
            if h["sample_count"]:
                tariff_parts.append(f"{config.plan_title(t_key)} ~{_dur(h['mean'])}")
        if tariff_parts:
            lines.append("По тарифам: " + " · ".join(tariff_parts))
        queue = f"В очереди: {outbox['pending_depth']}"
        if outbox["pending_depth"]:
            queue += f", самое старое {_dur(outbox['oldest_pending_age_seconds'])}"
        lines.append(queue)
        if bapi["retry_after_count"]:
            lines.append(f"Telegram просил подождать: {bapi['retry_after_count']} раз")
        if bapi["catchup_sent_count"] or bapi["catchup_omitted_count"]:
            lines.append(
                f"Догон после паузы: отправлено {bapi['catchup_sent_count']}, "
                f"пропущено {bapi['catchup_omitted_count']}"
            )

        messages = ["\n".join(lines)]

        if proxies:
            cards = ["🔌 <b>Каналы</b> · последний час"]
            for p in sorted(proxies, key=_channel_sort_key):
                cards.append("")
                cards.extend(channel_card(p))
            current: list[str] = []
            current_len = 0
            for line in cards:
                if current and current_len + len(line) + 1 > max_len:
                    messages.append("\n".join(current))
                    current, current_len = [], 0
                current.append(line)
                current_len += len(line) + 1
            if current:
                messages.append("\n".join(current))

        return messages

    def format_stats_report(self, max_len: int = 3800) -> str:
        return "\n\n".join(self.format_stats_messages(max_len=max_len))


def _ms(seconds: float | None) -> int | None:
    return None if seconds is None else int(round(seconds * 1000))


def short_channel(proxy_id: str, masked_url: str | None = None) -> str:
    """`direct` or the proxy's ip:port, for log lines."""
    if proxy_id == "direct" or str(masked_url or "").startswith("direct"):
        return "direct"
    return sanitize_proxy_credentials(masked_url or proxy_id).split("@", 1)[-1].split("://", 1)[-1] or proxy_id


def _read_cpu_and_swap() -> dict[str, Any] | None:
    try:
        with open("/proc/stat", encoding="ascii") as stat:
            cpu = [int(value) for value in stat.readline().split()[1:9]]
        swap = {"pswpin": 0, "pswpout": 0}
        with open("/proc/vmstat", encoding="ascii") as vmstat:
            for line in vmstat:
                key, _, value = line.partition(" ")
                if key in swap:
                    swap[key] = int(value)
    except (OSError, ValueError):
        return None
    return {"cpu": cpu, **swap}


def _read_memory() -> dict[str, Any]:
    try:
        fields = {}
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            for line in meminfo:
                key, _, value = line.partition(":")
                fields[key] = int(value.split()[0])
        return {
            "mem_avail_mb": round(fields["MemAvailable"] / 1024),
            "swap_mb": round((fields["SwapTotal"] - fields["SwapFree"]) / 1024),
        }
    except (OSError, ValueError, KeyError, IndexError):
        return {}


def format_minute_line(row: dict[str, Any]) -> str:
    """The minute's vitals in one line of bot.log."""
    scans = row["scans"]
    total = scans["ok"] + scans["fail"]
    fails = sorted(row["fail"].items(), key=lambda pair: -pair[1])[:3]
    parts = [f"сканы {scans['ok']}/{total} ({_pct(scans['ok'], total)})"]
    if fails:
        parts.append(" ".join(f"{kind}×{count}" for kind, count in fails))
    lots = row["lots"]
    parts.append(f"лоты {lots['new']}→{lots['queued']}, отпр {row['sent']}")
    if row["pub_sent_s"]["p50"] is not None:
        parts.append(f"pub→sent {row['pub_sent_s']['p50']:g}/{row['pub_sent_s']['p95']:g}с")
    if row["due_s"]["p95"] is not None:
        parts.append(f"опоздание p95 {row['due_s']['p95']:g}с")
    if "rps_need" in row:
        parts.append(f"rps {row['rps_need']:g}/{row['rps_have']:g}")
    if "outbox" in row:
        parts.append(f"outbox {row['outbox']}")
    parts.append(f"loop {row['loop_max_ms']} мс")
    if row["stalls"]:
        parts.append(f"зависаний {row['stalls']}")
    if row["flood"]:
        parts.append(f"flood {row['flood']} (до {row['flood_max_s']:g} с)")
    if "rss_mb" in row:
        parts.append(f"{row['rss_mb']:g} МБ")
    if "cpu_pct" in row:
        parts.append(f"cpu {row['cpu_pct']:g}%")
    if row.get("steal") or row.get("iowait"):
        parts.append(f"steal {row.get('steal', 0):g}% iowait {row.get('iowait', 0):g}%")
    if row.get("swap_in"):
        parts.append(f"swap-in {row['swap_in']}")
    return " · ".join(parts)


def _num(value: int | float) -> str:
    return f"{int(value):,}".replace(",", " ")


def _dec(value: float, digits: int) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _pct(part: int | float, total: int | float) -> str:
    if not total:
        return "—"
    value = part / total * 100
    return f"{_dec(value, 1)}%" if value < 10 else f"{_dec(value, 0)}%"


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0.0, float(seconds))
    if seconds < 10:
        return f"{_dec(seconds, 1)} с"
    if seconds < 60:
        return f"{_dec(seconds, 0)} с"
    if seconds < 3600:
        return f"{seconds / 60:.0f} мин"
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours} ч {rest // 60} мин"


def _hist_text(h: dict[str, Any]) -> str:
    if not h or not h.get("sample_count"):
        return "нет данных"
    return f"в среднем {_dur(h['mean'])}, почти всегда до {_dur(h['p95'])}"


def _merge_tariff_hist(by_tariff: dict[str, dict[str, Any]], key: str) -> str:
    total = sum(by_tariff[t][key]["sample_count"] for t in TARIFF_KEYS)
    if not total:
        return ""
    mean = sum((by_tariff[t][key]["mean"] or 0.0) * by_tariff[t][key]["sample_count"] for t in TARIFF_KEYS) / total
    p95 = max(by_tariff[t][key]["p95"] or 0.0 for t in TARIFF_KEYS)
    return f"в среднем {_dur(mean)}, почти всегда до {_dur(p95)}"


def _merge_failures(proxies: list[dict[str, Any]]) -> list[tuple[str, int]]:
    merged: dict[str, int] = {}
    for p in proxies:
        for kind, count in p.get("failures_60m", {}).items():
            merged[kind] = merged.get(kind, 0) + count
    return sorted(merged.items(), key=lambda kv: -kv[1])


def _is_direct_channel(p: dict[str, Any]) -> bool:
    return p["proxy_id"] == "direct" or str(p.get("masked_url") or "").startswith("direct")


def _channel_name(p: dict[str, Any]) -> str:
    if _is_direct_channel(p):
        return "Сервер (без прокси)"
    url = sanitize_proxy_credentials(p.get("masked_url") or p["proxy_id"])
    return url.split("@", 1)[-1].split("://", 1)[-1] or p["proxy_id"]


def _channel_sort_key(p: dict[str, Any]) -> tuple[int, str]:
    return (0 if _is_direct_channel(p) else 1, _channel_name(p))


def channel_card(p: dict[str, Any]) -> list[str]:
    raw = p["raw_http_total_60m"]
    blocks = p["raw_http_403_60m"] + p["raw_http_429_60m"]
    raw_10 = p["raw_http_total_10m"]
    blocks_10 = p["raw_http_403_429_10m"]
    if p["state"] == "quarantined":
        icon, status = "🔴", "на паузе"
        if p.get("quarantine_remaining"):
            status += f" ещё {_dur(p['quarantine_remaining'])}"
    elif p["state"] == "draining":
        icon, status = "⚪️", "отключается"
    elif raw == 0 and p["collection_total_60m"] == 0:
        icon, status = "⚪️", "простаивает"
    elif (raw_10 and blocks_10 / raw_10 > 0.10) or p["effective_rate"] < p["configured_rate"] - 1e-6:
        icon, status = "🟡", "Авито притормаживает"
    elif p["state"] == "half_open":
        icon, status = "🟡", "проверяется после паузы"
    else:
        icon, status = "🟢", "работает"

    card = [f"{icon} <b>{html_escape(_channel_name(p))}</b> — {status}"]
    if p.get("quarantine_reason") and p["state"] == "quarantined":
        card.append(f"Причина: {html_escape(p['quarantine_reason'])}")
    if raw:
        card.append(
            f"Запросов {_num(raw)} (~{raw / 60:.0f}/мин), успешных {_pct(p['raw_http_2xx_3xx_60m'], raw)}"
        )
        card.append(
            f"Блоки 403/429: {_num(blocks)}"
            + (f" ({_pct(blocks, raw)})" if blocks else "")
            + f", за 10 мин {_num(blocks_10)}"
        )
        if p["transport_errors_60m"] or p["raw_http_5xx_60m"]:
            card.append(f"Обрывы связи: {_num(p['transport_errors_60m'])}, ошибки Авито 5xx: {_num(p['raw_http_5xx_60m'])}")
        if p["latency_samples"]:
            card.append(f"Ответ Авито: обычно до {_dur(p['p50_latency_seconds'])}, медленные до {_dur(p['p95_latency_seconds'])}")
    coll = p["collection_total_60m"]
    if coll:
        card.append(
            f"Сканов {_num(coll)}, удачных {_pct(p['collection_success_60m'], coll)}, с новыми лотами {_num(p['scans_new_60m'])}"
        )
        fails = sorted(p.get("failures_60m", {}).items(), key=lambda kv: -kv[1])
        if fails:
            card.append("Ошибки: " + ", ".join(f"{FAILURE_LABELS.get(k, k)} — {v}" for k, v in fails[:3]))
    rate = f"Скорость {_dec(p['effective_rate'], 2)} из {_dec(p['configured_rate'], 2)} запр/с"
    if p["effective_rate"] < p["configured_rate"] - 1e-6:
        rate += " (снижена из-за блоков)"
    card.append(rate)
    return card

METRICS = RuntimeMetricsRegistry()
