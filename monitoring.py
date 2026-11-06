"""Runtime orchestration: proxy pool, heap deadline scheduler, decoupled HTTP & DB fanout."""

from __future__ import annotations

import asyncio
import dataclasses
import html
import logging
import os
import time
import traceback
import zlib
from collections import defaultdict
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import config
import database
import logger_setup
import scan_journal
import ui
from collector import collect_source, settled_item_ids
from logger_setup import get_trace, log_event, new_trace, set_trace, write_metrics_row
from parser import AvitoParser, channel_parsers, lane_parser
from proxy_pool import get_proxy_pool
from request_budget import get_network_block_state
from runtime_metrics import FAILURE_LABELS, METRICS, format_minute_line, short_channel
from scan_planner import DeadlineScheduler, ScheduledScan, calculate_interval_plan, stretch_factor
from seen_cache import InMemorySeenStore

logger = logging.getLogger(__name__)
MONITORING_STATS: dict = {}
MAX_SCAN_WORKERS = 256
SLOW_SCAN_MS = 15000
LOOP_PROBE_SECONDS = 0.25
LOOP_STALL_SECONDS = 1.0
STALL_DUMP_SECONDS = 4.0
# Pushes only for a real freeze: shorter ones (and the one-off first import of the captcha solver)
# are in the debug report, section «Зависания».
LONG_STALL_ALERT_SECONDS = 180.0
LONG_STALL_ALERT_INTERVAL = 3 * 3600.0
STALL_DUMP_REARM_SECONDS = 1.0
SOURCE_STATS_SECONDS = 600
WORKER_CRASH_PAUSE = 1.0
STRETCH_CHECK_SECONDS = 30.0  # how often the interval stretch is reconsidered
STRETCH_STEP = 0.1  # smaller changes of the stretch are not applied, so intervals do not jitter
CRASH_ALERT_INTERVAL = 24 * 3600  # the same code error pushes once a day; every repeat is in errors.log
# Failures that describe the page itself. Blocks and timeouts say nothing about the link.
DEAD_LINK_KINDS = frozenset({"catalog_missing", "invalid_redirect", "invalid_html", "invalid_response", "http_client"})
DEAD_LINK_OK_WINDOW = 120.0
DEAD_LINK_OWNER_COOLDOWN = 86400.0  # a flaky link must not repeat the same warning to its owner


def group_sources(searches: list[dict]) -> dict[str, list[dict]]:
    groups = defaultdict(list)
    for radar in searches:
        try:
            key = AvitoParser.normalize_url(radar["url"])
        except ValueError:
            logger.warning("Некорректный URL у радара #%s: сканирование пропущено", radar["id"])
            continue
        groups[key].append(radar)
    return dict(groups)


async def nothing_new_for_fanout(seen_store, task, collection, has_catchup: bool) -> bool:
    """
    Top-3 may reshuffle (promoted lots rotate) while the page brings nothing new. When every ID
    already went through fanout for the same radar group, the DB fanout is a guaranteed no-op.
    The planner's own fast-skip guards still apply: periodic full scans, resume, calibration.
    """
    if collection.fast_skipped or not task.can_fast_skip or has_catchup:
        return False
    return await seen_store.all_known(task.key, task.group_revision, (it.item_id for it in collection.items))


def source_interval(radars: list[dict], key: str = "") -> float:
    """Scan interval of a URL: the fastest tariff among its radars, minus a stable per-URL jitter of up to 10 %."""
    base = min(
        (config.TARIFF_SCAN_INTERVAL.get(database.classify_user_tariff(r)[0], config.TARIFF_SCAN_INTERVAL["base"])
         for r in radars),
        default=config.TARIFF_SCAN_INTERVAL["base"],
    )
    # Stable per URL so the planner does not reschedule every second, yet URLs of one tariff do not tick in step.
    jitter = (zlib.crc32(key.encode("utf-8")) % 1000) / 10000.0
    return round(float(base) * (1.0 - jitter), 2)


def pool_lanes(pool) -> int:
    """Scans the channels can serve at once: one worker per lane keeps every lane busy."""
    total = getattr(pool, "total_lanes", None)
    return int(total()) if callable(total) else len(getattr(pool, "clients", []) or [])


def source_label(radars) -> str:
    title = str(radars[0].get("title") or "радар")[:40] if radars else "?"
    return f"{title} +{len(radars) - 1}" if len(radars) > 1 else title


def record_source_result(task, client, ok: bool, attempts: list[str]) -> None:
    """Totals per link: the address Avito really serves it from shows why some links fail more often."""
    cache = getattr(getattr(client, "_bound_parser", None), "_canonical_cache", None)
    target = cache.get(task.key, task.key) if isinstance(cache, dict) else task.key
    parts = urlsplit(target)
    METRICS.record_source_scan(
        str(task.radars[0].get("id")) if task.radars else task.key[:60],
        ok=ok,
        attempts=attempts,
        query=parse_qs(urlsplit(task.key).query).get("q", [""])[0][:60],
        target=parts.path + (" +context" if "context=" in parts.query else ""),
    )


def log_scan_failure(task, failure, attempts: list[str], elapsed_ms: float, http_requests: int) -> None:
    kind = failure.kind if failure else ("unknown" if attempts else "no_channel")
    label = source_label(task.radars)
    detail = failure.detail if failure else ""
    log_event(
        logger,
        logging.INFO,
        "scan.fail",
        f"✗ «{label}» {kind}{f' ({detail})' if detail else ''} · {elapsed_ms / 1000:.1f} с · {', '.join(attempts) or 'нет попыток'}",
        src=label,
        rids=[radar.get("id") for radar in task.radars][:5],
        kind=kind,
        status=failure.status if failure else None,
        detail=detail,
        attempts=attempts,
        ms=int(elapsed_ms),
        http=http_requests,
    )


def log_scan_success(task, collection, channel: str, elapsed_ms: float, http_requests: int, fast_skipped: bool) -> None:
    label = source_label(task.radars)
    log_event(
        logger,
        logging.INFO if elapsed_ms >= SLOW_SCAN_MS else logging.DEBUG,
        "scan.ok",
        f"✓ «{label}» {len(collection.items)} лотов, стр. {collection.pages} · {channel} · {int(elapsed_ms)} мс"
        + (" · без изменений" if fast_skipped else ""),
        src=label,
        ch=channel,
        ms=int(elapsed_ms),
        items=len(collection.items),
        pages=collection.pages,
        http=http_requests,
        fast=fast_skipped,
        gap=bool(collection.coverage_gap),
    )


async def watch_event_loop(on_long_stall=None) -> None:
    """Measures how late the loop wakes up. A stall here means something blocked every coroutine."""
    armed_at = 0.0
    alerted_at = -LONG_STALL_ALERT_INTERVAL
    try:
        while True:
            started = time.monotonic()
            if started - armed_at >= STALL_DUMP_REARM_SECONDS:
                logger_setup.arm_stall_dump(STALL_DUMP_SECONDS)
                armed_at = started
            await asyncio.sleep(LOOP_PROBE_SECONDS)
            lag = time.monotonic() - started - LOOP_PROBE_SECONDS
            stalled = lag >= LOOP_STALL_SECONDS
            METRICS.record_loop_lag(lag, stall=stalled)
            if stalled:
                log_event(logger, logging.WARNING, "loop.stall", f"Event loop завис на {lag:.1f} с", s=round(lag, 2))
            if lag >= STALL_DUMP_SECONDS:
                logger_setup.mark_stall_dump(lag)
                armed_at = 0.0  # the dump fired once: arm it again for the next stall
            if lag >= LONG_STALL_ALERT_SECONDS and on_long_stall is not None \
                    and time.monotonic() - alerted_at >= LONG_STALL_ALERT_INTERVAL:
                alerted_at = time.monotonic()
                try:
                    await on_long_stall(lag)
                except Exception:
                    logger.warning("Не удалось сообщить о зависании", exc_info=True)
    finally:
        logger_setup.disarm_stall_dump()


async def run_monitoring(bot, parser: Optional[AvitoParser] = None):
    import scheduler
    from handlers.searches import pop_resumed_source_urls

    budget = get_network_block_state(config.PROXY_URL)
    pool = get_proxy_pool()
    planner = DeadlineScheduler(
        config.SCAN_MAX_RETRIES,
        config.SCAN_RETRY_BASE_DELAY,
        config.SCAN_RETRY_MAX_DELAY,
        interval_low=config.SCAN_INTERVAL_MIN,
        interval_high=config.SCAN_INTERVAL_MAX,
        use_source_backoff=False,
    )

    worker_count = max(1, int(config.MAX_CONCURRENT_WORKERS), pool_lanes(pool))
    parsers: list[AvitoParser] = [parser] if parser is not None else []
    try:
        for _ in range(worker_count - len(parsers)):
            parsers.append(AvitoParser(proxy=config.PROXY_URL))
    except Exception:
        for entry in parsers:
            if entry is not parser:
                entry.close()
        raise
    owned_parsers = [entry for entry in parsers if entry is not parser]

    changed = asyncio.Event()
    seen_store = InMemorySeenStore()
    extra_workers: list[asyncio.Task] = []
    crash_alerted_at: dict[str, float] = {}
    fanout_queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(config.FANOUT_QUEUE_SIZE)))
    state_updates_lock = asyncio.Lock()
    pending_state_updates: dict[str, tuple[str, float, list[str], int]] = {}

    request_cost = 1.0
    stats = {"successful_scans": 0, "failed_scans": 0, "coverage_gaps": 0, "fast_skipped_scans": 0}
    consecutive_failures = 0
    alert_sent = False
    last_ok_at = time.monotonic()
    started_at = time.monotonic()

    def catalog_down() -> bool:
        # 403s on single scans are routine; alert admins only when nothing succeeded for a long time.
        return consecutive_failures >= 5 and time.monotonic() - last_ok_at >= float(config.HEALTH_ALERT_AFTER_SECONDS)

    logger.info(
        "📡 Планировщик: %s HTTP-воркеров, %s fanout-воркеров, прокси в пуле=%s, интервалы тарифов %s",
        len(parsers),
        config.FANOUT_WORKERS,
        len(pool.clients),
        config.TARIFF_SCAN_INTERVAL,
    )

    async def queue_state_update(source_url: str, processed_at: float, top_ids: tuple[str, ...], failure_streak: int):
        async with state_updates_lock:
            pending_state_updates[source_url] = (source_url, processed_at, list(top_ids[:3]), failure_streak)

    async def flush_state_updates():
        async with state_updates_lock:
            if not pending_state_updates:
                return
            batch = list(pending_state_updates.values())
            pending_state_updates.clear()
        try:
            await database.upsert_source_scan_states_batch(batch)
        except Exception:
            logger.debug("Не удалось сохранить пакет source_scan_state", exc_info=True)

    async def notify_health(recovered: bool):
        text = (
            "✅ Получение каталога Авито возобновилось."
            if recovered
            else (
                f"⚠️ Авито не отдаёт каталог уже {int(config.HEALTH_ALERT_AFTER_SECONDS) // 60} мин.: ни одного удачного скана. "
                "Проверки продолжаются; уведомления в outbox сохранены. Подробности — в логах."
            )
        )
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=text)
            except Exception:
                logger.warning("Не удалось отправить администратору состояние мониторинга", exc_info=True)

    async def notify_admins_text(text: str):
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=text, parse_mode=None)
            except Exception:
                logger.warning("Не удалось отправить алерт администратору", exc_info=True)

    async def alert_worker_crash(task: Optional[ScheduledScan], exc: Exception):
        """The same line failing on many links is one alert per hour, whatever the message says."""
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        where = f"{os.path.basename(frame.filename)}:{frame.lineno}"
        signature = f"{type(exc).__name__}@{where}"
        now = time.monotonic()
        last = crash_alerted_at.get(signature)
        if last is not None and now - last < CRASH_ALERT_INTERVAL:
            return
        crash_alerted_at[signature] = now
        label = source_label(task.radars) if task else "вне скана"
        await notify_admins_text(
            f"⚠️ Ошибка в коде: «{label}»\n{type(exc).__name__}: {str(exc)[:200]}\n{where}\n\n"
            "Мониторинг не остановлен: остальные ссылки работают, эта повторится позже. "
            "Подробности — в logs/errors.log."
        )

    async def alert_long_stall(lag: float):
        await notify_admins_text(
            f"🚨 Бот завис на {lag / 60:.0f} мин: сканы и отправки стояли.\n"
            "Если повторится, пришлите «Отчёт для отладки» (Админ-панель → Логи), раздел «Зависания»."
        )

    dead_link_warned_at: dict[int, float] = {}

    async def alert_dead_link(task: ScheduledScan, failure, streak: int):
        """The verdict comes from scans that already ran, so a user's bad link costs no extra requests."""
        if streak != config.SOURCE_DEAD_ALERT_STREAK or failure is None or failure.kind not in DEAD_LINK_KINDS:
            return
        if time.monotonic() - last_ok_at >= DEAD_LINK_OK_WINDOW:
            return  # Nothing scans at all: that is an outage, not one bad link
        radars = "\n".join(
            f"#{radar.get('id')} «{radar.get('title') or 'Радар'}», пользователь {radar.get('user_id')}"
            for radar in task.radars[:5]
        )
        await notify_admins_text(
            f"⚠️ Ссылка не работает: {streak} проверок подряд\n"
            f"Причина: {FAILURE_LABELS.get(failure.kind, failure.kind)}\n\n{radars}\n{task.key[:300]}\n\n"
            "Радар продолжает проверяться реже. Удалить: Админка → пользователь → Удалить радары."
        )
        await notify_dead_link_owners(task)

    async def notify_dead_link_owners(task: ScheduledScan):
        """Only the owner can fix the link. Admins already got the alert above, and their test radars
        could otherwise bring a flood-wait on the admin chat."""
        for radar in task.radars:
            owner = int(radar.get("user_id") or 0)
            radar_id = int(radar.get("id") or 0)
            if not owner or config.is_admin(owner):
                continue
            if time.monotonic() - dead_link_warned_at.get(radar_id, -DEAD_LINK_OWNER_COOLDOWN) < DEAD_LINK_OWNER_COOLDOWN:
                continue
            dead_link_warned_at[radar_id] = time.monotonic()
            text = (
                f"{ui.e('warn')} <b>Радар не получает лоты</b>\n\n"
                f"«{html.escape(str(radar.get('title') or 'Радар'))}»: несколько проверок подряд Авито не отдал выдачу по этой ссылке.\n\n"
                "Если поиск открывается в браузере, ничего делать не нужно: бот продолжит проверять. "
                "Если нет, пришлите новую ссылку."
            )
            markup = ui.kb([ui.btn("Сменить ссылку", "refresh", callback_data=f"change_url:{radar.get('id')}")])
            try:
                await bot.send_message(chat_id=owner, text=text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                logger.warning("Не удалось предупредить владельца радара #%s", radar.get("id"), exc_info=True)

    async def fanout_worker(f_id: int):
        while True:
            item = await fanout_queue.get()
            if item is None:
                fanout_queue.task_done()
                break
            task, collection, worker_id, scan_trace = item
            set_trace(scan_trace)
            fanout_t0 = time.monotonic()
            fanout_ok = False
            try:
                await scheduler.process_collected_group_fanout(
                    task.key,
                    list(task.radars),
                    collection,
                    worker_id=worker_id,
                    previous_processed_at=task.previous_processed_at,
                )
                await seen_store.remember(task.key, task.group_revision, settled_item_ids(collection))
                fanout_ok = True
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Сбой fanout-обработки источника %s", task.key[:60])
            finally:
                try:
                    now_mono = time.monotonic()
                    METRICS.record_fanout_batch(now_mono - fanout_t0)
                    # After a failed fanout the next scan must repeat it: a saved top-3 would fast-skip these lots.
                    top_ids = collection.top_ids if fanout_ok else ()
                    planner.complete(
                        task,
                        now_mono,
                        True,
                        top_ids=top_ids,
                        fast_skipped=collection.fast_skipped,
                        processed_epoch=collection.captured_at if fanout_ok else None,
                    )
                    state = planner.sources.get(task.key)
                    streak = state.source_failure_streak if state else 0
                    processed_at = collection.captured_at if fanout_ok else task.previous_processed_at
                    await queue_state_update(task.key, processed_at, top_ids, streak)
                except Exception as exc:
                    logger.exception("Сбой завершения fanout источника %s", task.key[:60])
                    planner.abandon(task, time.monotonic())
                    await alert_worker_crash(task, exc)
                finally:
                    changed.set()
                    fanout_queue.task_done()

    def uses_patched_process(worker_parser: AvitoParser) -> bool:
        is_mock_parser = type(worker_parser).__name__ != "AvitoParser"
        return getattr(scheduler.process_url_group, "__name__", "") != "process_url_group" or is_mock_parser

    async def next_scan(worker_parser: AvitoParser) -> Optional[ScheduledScan]:
        """Waits for a usable channel and a due source; None means look again."""
        if not pool.clients:
            if not uses_patched_process(worker_parser):
                await asyncio.sleep(1.0)
                return None
            cooldown = budget.cooldown_remaining()
            if cooldown > 0:
                await asyncio.sleep(min(cooldown, 1.0))
                return None
        elif pool.healthy_count() == 0:
            min_cd = min((c.cooldown_remaining() for c in pool.clients), default=0.5)
            if min_cd > 0:
                await asyncio.sleep(min(min_cd, 1.0))
                return None
        elif not uses_patched_process(worker_parser) and callable(getattr(pool, "has_free_lane", None)) \
                and not pool.has_free_lane():
            # Every channel routing would use is busy: the link waits here, most overdue first, instead of
            # queueing on one channel's lock while another channel frees up and idles.
            await pool.wait_for_free_lane(1.0)
            return None

        task = planner.take_due(time.monotonic(), now_epoch=time.time())
        if task is None:
            changed.clear()
            timeout = min(1.0, max(0.01, planner.seconds_until_next(time.monotonic())))
            try:
                await asyncio.wait_for(changed.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return task

    async def run_scan(task: ScheduledScan, worker_id: int, worker_parser: AvitoParser):
        nonlocal consecutive_failures, alert_sent, last_ok_at
        is_patched_process = uses_patched_process(worker_parser)
        now_mono = task.started_at
        now_epoch = time.time()
        new_trace()
        revisit_seconds = None
        state_before = planner.sources.get(task.key)
        if state_before and state_before.last_success is not None:
            revisit_seconds = max(0.0, now_mono - state_before.last_success)
        due_lag_seconds = max(0.0, now_mono - task.scheduled_at)

        if is_patched_process:
            ok = False
            scan_t0 = time.monotonic()
            try:
                ok = await scheduler.process_url_group(
                    bot,
                    worker_parser,
                    task.key,
                    list(task.radars),
                    worker_id=worker_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Сбой обработки источника; повтор будет отложен")
            elapsed_ms = (time.monotonic() - scan_t0) * 1000.0
            failure = getattr(worker_parser, "last_failure", None) if not ok else None
            gap = bool(getattr(worker_parser, "last_coverage_gap", False))
            raw_reqs = getattr(worker_parser, "last_http_requests", 1)
            http_reqs = max(1, int(raw_reqs)) if isinstance(raw_reqs, (int, float)) else 1
            METRICS.record_scan_attempt(
                success=ok,
                elapsed_ms=elapsed_ms,
                pages_fetched=1,
                http_requests=http_reqs,
                fast_skipped=False,
                coverage_gap=gap,
                revisit_seconds=revisit_seconds,
                due_lag_seconds=due_lag_seconds,
                failure_kind=failure.kind if failure else None,
            )
            planner.complete(
                task,
                time.monotonic(),
                ok,
                retry_after=max(budget.cooldown_remaining(), failure.retry_after if failure else 0),
                retryable=failure.retryable if failure else True,
                processed_epoch=time.time() if ok else None,
                exhausted_proxies=not ok,
            )
            changed.set()
            stats["successful_scans" if ok else "failed_scans"] += 1
            stats["coverage_gaps"] += int(gap)
            consecutive_failures = 0 if ok else consecutive_failures + 1
            if ok:
                last_ok_at = time.monotonic()
            if ok and alert_sent:
                alert_sent = False
                await notify_health(True)
            elif catalog_down() and not alert_sent:
                alert_sent = True
                await notify_health(False)
            return

        # Production multi-proxy collection: up to SCAN_PROXY_ATTEMPTS distinct channels (at least
        # every channel of a small pool, so one dead proxy never fails a scan on its own).
        tried_proxy_ids: set[str] = set()
        scan_client = None
        collection = None
        last_failure = None
        total_http_reqs = 0
        attempts: list[str] = []
        scan_t0 = time.monotonic()
        max_age = max((min(30, max(1, int(r.get("max_age_minutes") or 5))) for r in task.radars), default=5)
        has_catchup = (
            task.previous_processed_at > 0
            and (now_epoch - task.previous_processed_at) > (max_age * 60)
        )
        collection_max_age = config.CATCHUP_MAX_AGE_MINUTES
        collection_max_pages = max(1, int(config.BROAD_RADAR_MAX_PAGES))

        async def page_is_all_new(page_items, _task=task) -> bool:
            organic = [item.item_id for item in page_items if not item.is_promotion]
            return await seen_store.none_known(_task.key, _task.group_revision, organic)

        scan_attempts = max(1, int(config.SCAN_PROXY_ATTEMPTS), min(3, len(pool.clients)))
        for _attempt in range(scan_attempts):
            lease = pool.acquire_client(exclude_ids=tried_proxy_ids)
            if lease is None and len(pool.clients) == 1:
                only_client = pool.clients[0]
                only_client.quarantined_until = 0.0
                only_client._state = "healthy"
                only_client._half_open_probe_active = False
                lease = pool.acquire_client()
            if lease is None:
                break
            client = lease.client
            scan_client = client  # the channel that produced this scan's final outcome
            tried_proxy_ids.add(client.proxy_id)
            proxy_parser = lane_parser(client, getattr(lease, "lane", 0), AvitoParser)
            try:
                collection = await collect_source(
                    proxy_parser,
                    task.key,
                    collection_max_age,
                    max_pages=collection_max_pages,
                    can_fast_skip=(task.can_fast_skip and not has_catchup),
                    read_next_page=page_is_all_new,
                    last_top_ids=task.last_top_ids,
                )
                total_http_reqs += max(1, int(proxy_parser.last_http_requests or 1))
                if collection is not None:
                    break
                last_failure = proxy_parser.last_failure
                attempts.append(
                    f"{short_channel(client.proxy_id, client.masked_url)}: {last_failure.kind if last_failure else 'unknown'}"
                )
                if last_failure and not last_failure.retryable:
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Сбой HTTP-сбора на прокси %s", client.proxy_id)
                attempts.append(f"{short_channel(client.proxy_id, client.masked_url)}: exception")
            finally:
                lease.release()

        elapsed_ms = (time.monotonic() - scan_t0) * 1000.0
        ok = collection is not None
        record_source_result(task, scan_client, ok, attempts)
        if ok and scan_client is not None:
            collection = dataclasses.replace(collection, channel=short_channel(scan_client.proxy_id, scan_client.masked_url))
        if ok and await nothing_new_for_fanout(seen_store, task, collection, has_catchup):
            collection = dataclasses.replace(collection, fast_skipped=True)
        gap = bool(collection.coverage_gap) if collection is not None else False
        fast_skipped = bool(collection.fast_skipped) if collection is not None else False
        pages_fetched = collection.pages if collection is not None else 0

        METRICS.record_scan_attempt(
            success=ok,
            elapsed_ms=elapsed_ms,
            pages_fetched=pages_fetched,
            http_requests=max(1, total_http_reqs),
            fast_skipped=fast_skipped,
            coverage_gap=gap,
            revisit_seconds=revisit_seconds,
            due_lag_seconds=due_lag_seconds,
            is_catchup=has_catchup,
            proxy_id=scan_client.proxy_id if scan_client is not None else "direct",
            masked_url=scan_client.masked_url if scan_client is not None else "direct",
            failure_kind=None if ok else (last_failure.kind if last_failure else ("unknown" if attempts else "no_channel")),
            channels_tried=len(tried_proxy_ids),
        )
        try:
            scan_journal.record_scan(
                [radar.get("id") for radar in task.radars],
                ok=ok,
                at=collection.captured_at if ok else time.time(),
                ms=elapsed_ms,
                channel=collection.channel if ok else ", ".join(attempts)[:80],
                items=len(collection.items) if ok else 0,
                pages=pages_fetched,
                fast=fast_skipped,
                kind="" if ok else (last_failure.kind if last_failure else ("unknown" if attempts else "no_channel")),
                detail="" if ok or not last_failure else last_failure.detail,
                due_lag=due_lag_seconds,
            )
        except Exception:
            logger.debug("Журнал сканов не записан", exc_info=True)

        if not ok:
            log_scan_failure(task, last_failure, attempts, elapsed_ms, max(1, total_http_reqs))
            is_direct_run = getattr(pool, "is_direct_only", False) or (
                bool(getattr(pool, "clients", []))
                and len(pool.clients) == 1
                and pool.clients[0].is_direct
            )
            fast_retry = float(config.SCAN_INTERVAL_MIN) if is_direct_run else 0.0
            planner.complete(
                task,
                time.monotonic(),
                False,
                retry_after=max(fast_retry, last_failure.retry_after if last_failure else 0.0),
                retryable=last_failure.retryable if last_failure else True,
                exhausted_proxies=False if is_direct_run else True,
            )
            state_after = planner.sources.get(task.key)
            if is_direct_run and state_after:
                state_after.source_failure_streak = 0
            await queue_state_update(
                task.key,
                task.previous_processed_at,
                task.last_top_ids,
                0 if is_direct_run else (state_after.source_failure_streak if state_after else 1),
            )
            changed.set()
            stats["failed_scans"] += 1
            consecutive_failures += 1
            if catalog_down() and not alert_sent:
                alert_sent = True
                await notify_health(False)
            await alert_dead_link(task, last_failure, state_after.source_failure_streak if state_after else 0)
            return

        log_scan_success(
            task,
            collection,
            short_channel(scan_client.proxy_id, scan_client.masked_url) if scan_client is not None else "direct",
            elapsed_ms,
            max(1, total_http_reqs),
            fast_skipped,
        )
        stats["successful_scans"] += 1
        stats["coverage_gaps"] += int(gap)
        stats["fast_skipped_scans"] += int(fast_skipped)
        consecutive_failures = 0
        last_ok_at = time.monotonic()
        if alert_sent:
            alert_sent = False
            await notify_health(True)

        if fast_skipped:
            # Top-3 items on page 1 unchanged and all radars calibrated -> complete immediately without DB fanout
            scheduler.LAST_SCAN_TIMESTAMP = time.time()
            planner.complete(
                task,
                time.monotonic(),
                True,
                top_ids=collection.top_ids,
                fast_skipped=True,
                processed_epoch=collection.captured_at,
            )
            state_after = planner.sources.get(task.key)
            await queue_state_update(
                task.key,
                collection.captured_at,
                collection.top_ids,
                state_after.source_failure_streak if state_after else 0,
            )
            changed.set()
        else:
            if planner.mark_http_finished_fanout_pending(task, time.monotonic()):
                await fanout_queue.put((task, collection, worker_id, get_trace()))
            else:
                planner.complete(
                    task,
                    time.monotonic(),
                    True,
                    top_ids=collection.top_ids,
                    fast_skipped=False,
                    processed_epoch=collection.captured_at,
                )
                changed.set()

    async def worker(worker_id: int, worker_parser: AvitoParser):
        while True:
            task = None
            try:
                task = await next_scan(worker_parser)
                if task is not None:
                    await run_scan(task, worker_id, worker_parser)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Сбой воркера %s на источнике %s; остальные продолжают", worker_id, task.key[:60] if task else "-")
                if task is not None:
                    planner.abandon(task, time.monotonic())
                    changed.set()
                stats["failed_scans"] += 1
                await alert_worker_crash(task, exc)
                if time.monotonic() - last_ok_at >= float(config.HEALTH_ALERT_AFTER_SECONDS):
                    raise  # Nothing scans at all: let the supervisor rebuild everything
                await asyncio.sleep(WORKER_CRASH_PAUSE)

    async def dispatch():
        nonlocal request_cost
        startup = True
        no_proxy_notified = False
        last_report = last_cleanup = last_terminal_cleanup = last_sources = time.monotonic()
        previous_requests = budget.snapshot()["requests"]
        previous_scans = 0
        outbox_metrics = await database.get_outbox_metrics()
        persisted_states = await database.load_all_source_scan_states()
        capacity_rps: Optional[float] = None  # the channels' measured capacity, req/s
        stretch, stretch_checked_at = 1.0, 0.0  # all tariff intervals × stretch while the capacity falls short

        while True:
            searches = await database.get_all_active_searches()
            groups = group_sources(searches)
            seen_store.prune(groups)
            network = budget.snapshot()
            proxy_snaps = pool.snapshots()
            # A deleted proxy must not linger in screens or capacity math.
            METRICS.forget_proxies_except({s["proxy_id"] for s in proxy_snaps if s.get("proxy_id")})
            if proxy_snaps:
                no_proxy_notified = False
            elif not no_proxy_notified and parser is None and getattr(scheduler.process_url_group, "__name__", "") == "process_url_group":
                no_proxy_notified = True
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🟢 Включить прямой парсинг (без прокси)",
                        callback_data="admin_toggle_direct_avito:alert",
                    )],
                    [InlineKeyboardButton(
                        text="👑 Открыть админ-панель",
                        callback_data="admin_refresh",
                    )],
                ])
                msg_text = (
                    "⚠️ <b>Прокси не настроены — мониторинг Авито на паузе</b>\n\n"
                    "Для защиты IP сервера сканирование без прокси по умолчанию выключено.\n"
                    "Вы можете <b>включить прямой парсинг (1.0 req/s с IP сервера)</b> прямо кнопкой ниже, "
                    "в 👑 Админ-панели или командой <code>/direct_mode</code>."
                )
                for admin_id in config.ADMIN_IDS:
                    try:
                        await bot.send_message(chat_id=admin_id, text=msg_text, reply_markup=kb, parse_mode="HTML")
                    except Exception:
                        pass
            healthy_proxies = sum(1 for p in proxy_snaps if p.get("state") in ("healthy", "degraded", "half_open"))
            # Runtime workers are outside the main gather: a dead one must reach the supervisor, not vanish.
            for extra in extra_workers:
                if extra.done():
                    extra.result()
            # Proxies (and lanes) added at runtime get their own scan workers, so capacity grows without a restart.
            missing_workers = min(MAX_SCAN_WORKERS, pool_lanes(pool)) - len(parsers) - len(extra_workers)
            for _ in range(max(0, missing_workers)):
                index = len(parsers) + len(extra_workers) + 1
                extra_workers.append(asyncio.create_task(worker(index, parsers[0]), name=f"scan-worker-{index}"))
            limits_rps = (
                sum(float(p.get("current_rps") or 0.0) for p in proxy_snaps if p.get("state") in ("healthy", "degraded", "half_open"))
                if proxy_snaps
                else float(network["current_rps"])
            )
            # What the channels measurably give (previous pass of this loop); their rate limits until measured.
            effective_rps = max(0.1, capacity_rps if (capacity_rps and proxy_snaps) else limits_rps)

            base = {key: source_interval(radars, key) for key, radars in groups.items()}
            METRICS.set_planned_scan_rate(sum(1.0 / interval for interval in base.values()))
            plan = calculate_interval_plan(
                base,
                effective_rps,
                config.SCAN_MAX_INTERVAL,
                config.SCAN_BUDGET_UTILIZATION,
                request_cost,
            )
            now = time.monotonic()
            if now - stretch_checked_at >= STRETCH_CHECK_SECONDS:
                stretch_checked_at = now
                demand_rps = sum(request_cost / interval for interval in base.values())
                target = stretch_factor(
                    demand_rps,
                    capacity_rps or 0.0,
                    utilization=float(config.SCAN_STRETCH_UTILIZATION),
                    max_factor=float(config.SCAN_STRETCH_MAX),
                ) if (capacity_rps and proxy_snaps) else 1.0
                if abs(target - stretch) >= STRETCH_STEP or (target == 1.0) != (stretch == 1.0):
                    log_event(
                        logger, logging.INFO, "scan.stretch",
                        f"Мощности не хватает: интервалы ×{target:g} (нужно {demand_rps:.2f} запр/с, каналы дают {capacity_rps:.2f})"
                        if target > 1.0 else "Мощности хватает: интервалы по тарифам",
                        factor=target, need=round(demand_rps, 2), have=round(capacity_rps or 0.0, 2),
                    )
                    stretch = target
            planner.sync(
                groups,
                base if stretch <= 1.0 else {key: round(value * stretch, 2) for key, value in base.items()},
                now,
                startup=startup,
                startup_spread=config.SCAN_STARTUP_SPREAD,
                persisted_states=persisted_states if startup else None,
            )
            startup = False

            resumed_urls = pop_resumed_source_urls()
            if resumed_urls:
                planner.force_due_now(resumed_urls, now)

            await flush_state_updates()
            changed.set()

            planner_snap = planner.snapshot(now)
            METRICS.sample_workers(
                total=len(parsers) + len(extra_workers),
                busy=planner_snap["http_in_flight"],
                queued=planner_snap["due"],
                oldest_due=planner_snap["oldest_due_seconds"],
            )
            metrics_snap = METRICS.snapshot(
                active_unique_urls=len(groups),
                proxy_snapshots=proxy_snaps,
                outbox_metrics=outbox_metrics,
                full_coverage_age_seconds=planner_snap["full_coverage_age_seconds"],
            )
            cap = metrics_snap["capacity"]
            capacity_rps = float(cap.get("available_rps") or 0.0) or None
            note_demand = getattr(pool, "note_demand", None)
            if callable(note_demand):
                note_demand(cap.get("required_rps") or plan.requested_rps)
            pop_alerts = getattr(pool, "pop_alerts", None)
            if callable(pop_alerts):
                for text in pop_alerts():
                    await notify_admins_text(text)

            MONITORING_STATS.clear()
            MONITORING_STATS.update(
                planner_snap,
                **stats,
                requested_rps=plan.requested_rps,
                planned_rps=plan.planned_rps,
                available_rps=cap["available_rps"] if proxy_snaps else plan.available_rps,
                overloaded=bool(cap["deficit"] if proxy_snaps else plan.overloaded),
                requests_per_scan=metrics_snap["avg_http_requests_per_scan"],
                network=network,
                proxies=proxy_snaps,
                healthy_proxies=healthy_proxies,
                outbox=outbox_metrics,
                metrics=metrics_snap,
                capacity=cap,
                fanout_queue_depth=fanout_queue.qsize(),
            )

            if now - last_report >= float(config.METRICS_REPORT_INTERVAL):
                outbox_metrics = await database.get_outbox_metrics()
                MONITORING_STATS["outbox"] = outbox_metrics
                is_direct_run = getattr(pool, "is_direct_only", False) or (
                    bool(getattr(pool, "clients", []))
                    and len(pool.clients) == 1
                    and pool.clients[0].is_direct
                )
                if is_direct_run:
                    from scan_planner import get_capacity_mode_intervals
                    planner.capacity_mode = "normal"
                    intervals = get_capacity_mode_intervals()
                    planner.interval_low, planner.interval_high = intervals["normal"]
                else:
                    planner.update_capacity_mode(cap["available_rps"], cap["required_rps"], now)
                request_cost = float(metrics_snap["avg_http_requests_per_scan"] or 1.0)
                rows = METRICS.pop_minute_rows()
                if rows and now - last_sources >= SOURCE_STATS_SECONDS:
                    rows[-1]["src"] = METRICS.pop_source_stats()
                    last_sources = now
                for row in rows:
                    row.update(
                        mode=planner.capacity_mode,
                        urls=len(groups),
                        radars=len(searches),
                        rps_need=round(cap["required_rps"], 2),
                        rps_have=round(cap["available_rps"], 2),
                        rps_limits=round(limits_rps, 2),
                        stretch=stretch,
                        lanes=int(config.PROXY_MAX_IN_FLIGHT),
                        channels=f"{healthy_proxies}/{len(proxy_snaps)}",
                        coverage_s=round(planner_snap["full_coverage_age_seconds"] or 0.0, 1),
                        coverage_rid=planner_snap["least_covered_radar"],
                        outbox=metrics_snap["outbox"]["pending_depth"],
                        outbox_oldest_s=metrics_snap["outbox"]["oldest_pending_age_seconds"],
                        fanout_q=fanout_queue.qsize(),
                        uptime_s=int(now - started_at),
                        log_drops=logger_setup.dropped_records,
                        **METRICS.process_stats(),
                    )
                    write_metrics_row(row)
                    log_event(logger, logging.INFO, "minute", format_minute_line(row))
                alerts = METRICS.evaluate_minute_alerts(
                    active_unique_urls=len(groups),
                    proxy_snapshots=proxy_snaps,
                    outbox_metrics=outbox_metrics,
                    full_coverage_age_seconds=planner_snap["full_coverage_age_seconds"],
                )
                for alert_msg in alerts:
                    logger.warning(alert_msg)
                    await notify_admins_text(alert_msg)
                last_report = now

            if now - last_terminal_cleanup >= 3600:
                await database.cleanup_terminal_notification_events()
                last_terminal_cleanup = now

            if now - last_cleanup >= 86400:
                await database.cleanup_old_seen_items(days=7)
                from logger_setup import cleanup_logs
                await asyncio.to_thread(cleanup_logs, max_total_mb=40, keep_days=3)
                last_cleanup = now

            await asyncio.sleep(1)

    tasks = [
        asyncio.create_task(dispatch(), name="scan-dispatcher"),
        asyncio.create_task(watch_event_loop(alert_long_stall), name="loop-watchdog"),
    ]
    tasks.extend(
        asyncio.create_task(fanout_worker(index), name=f"fanout-worker-{index}")
        for index in range(1, max(1, int(config.FANOUT_WORKERS)) + 1)
    )
    tasks.extend(
        asyncio.create_task(worker(index, entry), name=f"scan-worker-{index}")
        for index, entry in enumerate(parsers, 1)
    )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in (*tasks, *extra_workers):
            task.cancel()
        await asyncio.gather(*tasks, *extra_workers, return_exceptions=True)
        await flush_state_updates()
        for entry in owned_parsers:
            entry.close()
        for client in pool.clients:
            for bound in channel_parsers(client):
                bound.close()
            setattr(client, "_bound_parser", None)
            setattr(client, "_lane_parsers", None)
