"""Background jobs that talk to people rather than Avito: renewal reminders and the morning admin report."""

import asyncio
import gzip
import html
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import FSInputFile

import config
import database
import ui
from runtime_metrics import METRICS

logger = logging.getLogger(__name__)

DAY = 86400.0
BACKUP_MAX_BYTES = 49 * 1024 * 1024  # Telegram refuses a bot's document over 50 MB


def _until_epoch(raw) -> Optional[float]:
    try:
        return datetime.strptime(str(raw).strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def reminder_kind(user: dict, now: float) -> Optional[str]:
    """Which reminder is due: '3d', '1d', '30m' (trial), 'end', or None."""
    until = _until_epoch(user.get("subscription_until"))
    if until is None or config.is_admin(int(user["user_id"])):
        return None
    left = until - now
    is_trial = _is_trial(user)
    if left <= 0:
        return "end" if left > -DAY else None
    if is_trial:
        return "30m" if left <= config.TRIAL_REMIND_BEFORE else None
    if left <= DAY:
        return "1d"
    if left <= 3 * DAY:
        return "3d"
    return None


def _is_trial(user: dict) -> bool:
    return bool(user.get("trial_used")) and not bool(user.get("is_allowed"))


def reminder_text(user: dict, kind: str, found: Optional[int] = None) -> str:
    plan = config.plan_title(user.get("tariff_plan"))
    is_trial = _is_trial(user)
    date = database.format_subscription_date(user.get("subscription_until")).split(" · ")[0]
    if kind == "end" and is_trial:
        result = ""
        if found:
            lots = config.plural(found, "новый лот", "новых лота", "новых лотов")
            result = f"За пробный период радар прислал вам <b>{lots}</b>.\n"
        return (
            f"{ui.e('time')} <b>Пробный период закончился</b>\n\n"
            f"{result}"
            "Радары и настройки сохранены. Выберите тариф, и бот сразу снова начнёт присылать новые лоты."
        )
    if kind == "end":
        return (
            f"{ui.e('time')} <b>Подписка «{plan}» закончилась</b>\n\n"
            "Новые лоты больше не приходят. Радары и стоп-слова сохранены: продлите подписку, и всё заработает сразу."
        )
    if kind == "30m":
        minutes = config.plural(max(1, round(config.TRIAL_REMIND_BEFORE / 60)), "минуту", "минуты", "минут")
        return (
            f"{ui.e('time')} <b>Пробный период закончится через {minutes}</b>\n\n"
            "Чтобы не пропустить новые лоты, выберите тариф заранее."
        )
    when = "завтра" if kind == "1d" else "через 3 дня"
    return (
        f"{ui.e('time')} <b>Подписка «{plan}» закончится {when}</b>\n"
        f"{date}\n\n"
        "Продлите заранее: оплаченные дни добавятся к текущему сроку, без автосписаний."
    )


async def send_due_reminders(bot: Bot, now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    sent = 0
    for user in await database.get_users_with_subscription():
        kind = reminder_kind(user, now)
        if kind is None:
            continue
        key = f"{user.get('subscription_until')}|{kind}"
        if user.get("reminded_key") == key:
            continue
        user_id = int(user["user_id"])
        found = None
        if kind == "end" and _is_trial(user):
            until = _until_epoch(user.get("subscription_until")) or now
            try:
                found = await database.count_sent_lots(user_id, until - config.TRIAL_HOURS * 3600, until)
            except Exception as exc:
                logger.warning("Не посчитаны лоты за триал %s: %s", user_id, exc)
        try:
            await bot.send_message(user_id, reminder_text(user, kind, found), parse_mode="HTML", reply_markup=ui.kb(
                [ui.btn("Продлить" if kind != "end" else "Выбрать тариф", "tariffs",
                        callback_data="open_subscription_menu", style="success")],
            ))
            sent += 1
        except TelegramForbiddenError:
            pass
        except Exception as exc:
            logger.warning("Напоминание о подписке не отправлено %s: %s", user_id, exc)
            continue
        await database.set_reminded_key(user_id, key)
    return sent


async def reminder_worker(bot: Bot) -> None:
    while True:
        try:
            count = await send_due_reminders(bot)
            if count:
                logger.info("Отправлено напоминаний о подписке: %s", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой рассылки напоминаний")
        try:
            from handlers.support import retry_undelivered_tickets

            count = await retry_undelivered_tickets(bot)
            if count:
                logger.info("Доставлено отложенных обращений в поддержку: %s", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой повторной отправки обращений")
        await asyncio.sleep(config.REMINDER_CHECK_INTERVAL)


def _fmt_seconds(value) -> str:
    if value is None:
        return "нет данных"
    value = float(value)
    return f"{value:.0f} с" if value < 120 else f"{value / 60:.0f} мин"


async def build_daily_report(reset: bool = True) -> str:
    stats = await database.get_daily_report_stats(24)
    system = await database.get_system_stats()
    day = METRICS.pop_daily_summary(reset=reset)
    scans_total = day["scans_ok"] + day["scans_failed"]
    scan_ok = f"{day['scans_ok'] * 100 / scans_total:.0f} %" if scans_total else "нет данных"
    pay_lines = [
        f"• {config.plan_title(plan)}: {count} шт · {amount} ⭐" for plan, count, amount in stats["payments"]
    ] or ["• нет"]
    stars_total = sum(amount for _plan, _count, amount in stats["payments"])
    rub_total = sum(amount for _plan, _count, amount in stats["payments_rub"])
    pay_lines += [f"• {config.plan_title(plan)}: {count} шт · {amount} ₽" for plan, count, amount in stats["payments_rub"]]
    if stats["payments_rub"] and pay_lines[0] == "• нет":
        pay_lines = pay_lines[1:]
    lat = day["pub_to_sent"]
    try:
        deficit = float(METRICS.snapshot(window_minutes=60)["capacity"].get("deficit_now") or 0.0)
    except Exception:
        deficit = 0.0
    lines = [
        f"{ui.e('stats')} <b>Отчёт за сутки</b>" + (f" ({day['hours']:.0f} ч с запуска)" if day["hours"] < 23 else ""),
        "",
        f"{ui.e('users')} Новых пользователей: <b>{stats['new_users']}</b> · всего {system['total_users']}, с доступом {system['allowed_users']}",
        f"{ui.e('time')} Подписка кончается за 3 дня: <b>{stats['expiring_3d']}</b>",
        f"{ui.e('star')} Оплаты: <b>{rub_total} ₽</b> · <b>{stars_total} ⭐</b>",
        *pay_lines,
        "",
        f"{ui.e('radar')} Радары: {system['active_searches']} активных из {system['total_searches']}",
        f"{ui.e('ok')} Удачных сканов: <b>{scan_ok}</b> ({day['scans_ok']} из {scans_total})",
        f"{ui.e('new')} Отправлено лотов: <b>{day['sent']}</b>",
        f"{ui.e('wait')} Публикация → уведомление: p50 <b>{_fmt_seconds(lat.get('p50'))}</b>, "
        f"p95 {_fmt_seconds(lat.get('p95'))} (n={lat.get('sample_count', 0)})",
    ]
    if deficit > 0:
        lines.append(f"{ui.e('warn')} Не хватает мощности: {deficit:.2f} req/s — нужны ещё прокси")
    return "\n".join(lines)


def seconds_until_report(now: Optional[float] = None) -> float:
    now = time.time() if now is None else now
    msk = now + 3 * 3600
    target = (msk // DAY) * DAY + config.DAILY_REPORT_HOUR * 3600
    if target <= msk:
        target += DAY
    return target - msk


def pack_database_backup() -> Optional[str]:
    path = database.create_database_backup_file()
    if not path:
        return None
    packed = f"{path}.gz"
    try:
        with open(path, "rb") as source, gzip.open(packed, "wb", compresslevel=6) as target:
            shutil.copyfileobj(source, target)
    finally:
        os.remove(path)
    return packed


# Admins get the current note once, when a release carrying it is up: then they know the deploy went through.
# A new note needs a new id; restarts of the same release send nothing.
RELEASE_NOTE_KEY = "release_note_sent"
RELEASE_NOTE_ID = "2026-11-06-nojson"
RELEASE_NOTE_TITLE = "Обновлено 06.11.2026: вернули версию 22:05"
RELEASE_NOTE_LINES = (
    "код как в версии 22:05, проверка JSON (/jsonprobe) убрана совсем",
)
# The deploy calls a release good after 20 s of uptime; the note waits past that.
RELEASE_NOTE_DELAY = 30.0


async def announce_release(bot: Bot, version: str, delay: float = RELEASE_NOTE_DELAY) -> int:
    """Sends the release note to every admin once; returns how many got it."""
    try:
        await asyncio.sleep(delay)
        if await database.get_bot_setting(RELEASE_NOTE_KEY) == RELEASE_NOTE_ID:
            return 0
        text = (
            f"{ui.e('ok')} <b>{RELEASE_NOTE_TITLE}</b>\n"
            f"Деплой прошёл, версия {html.escape(version)}.\n\n"
            + "\n".join(f"• {html.escape(line)}" for line in RELEASE_NOTE_LINES)
        )
        delivered = 0
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
                delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Сообщение об обновлении не доставлено %s: %s", admin_id, exc)
        if delivered:  # nobody reached: try again at the next start
            await database.set_bot_setting(RELEASE_NOTE_KEY, RELEASE_NOTE_ID)
        return delivered
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Сообщение об обновлении не отправлено")
        return 0


DAILY_BACKUP_PREFIX = "daily-"


def keep_daily_copy(packed: str, now: Optional[float] = None) -> Path:
    """A dated copy of the packed backup in BACKUP_DIR; only the newest DAILY_BACKUP_KEEP stay."""
    directory = Path(config.BACKUP_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y%m%d", time.gmtime((time.time() if now is None else now) + 3 * 3600))
    target = directory / f"{DAILY_BACKUP_PREFIX}{day}.db.gz"
    shutil.copyfile(packed, target)
    copies = sorted(directory.glob(f"{DAILY_BACKUP_PREFIX}*.db.gz"), reverse=True)  # the names sort by date
    for old in copies[max(1, int(config.DAILY_BACKUP_KEEP)):]:
        old.unlink(missing_ok=True)
    return target


async def send_daily_backup(bot: Bot) -> None:
    """A dated copy stays on the server (two weeks); the one sent to the admin survives losing it."""
    packed = await asyncio.to_thread(pack_database_backup)
    if packed is None:
        await bot.send_message(config.ADMIN_ID, "Не удалось создать ежедневную копию базы, смотрите errors.log.")
        return
    try:
        await asyncio.to_thread(keep_daily_copy, packed)
    except OSError as exc:
        logger.warning("Ежедневная копия базы не сохранена на сервере: %s", exc)
    try:
        size = os.path.getsize(packed)
        if size > BACKUP_MAX_BYTES:
            await bot.send_message(
                config.ADMIN_ID, f"Копия базы {size // (1024 * 1024)} МБ: больше лимита Telegram, не отправлена."
            )
            return
        await bot.send_document(
            config.ADMIN_ID, FSInputFile(packed), caption=f"Ежедневная копия базы, {size // 1024} КБ. Храните её как личные данные."
        )
    finally:
        os.remove(packed)


async def daily_report_worker(bot: Bot) -> None:
    while True:
        await asyncio.sleep(seconds_until_report())
        try:
            text = await build_daily_report()
            for admin_id in config.ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=ui.kb(
                        [ui.btn("Мониторинг", "stats", callback_data="admin_runtime_stats"),
                         ui.btn("Админ-панель", "admin", callback_data="admin_refresh")],
                    ))
                except Exception as exc:
                    logger.warning("Утренний отчёт не доставлен %s: %s", admin_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой утреннего отчёта")
        try:
            await send_daily_backup(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой ежедневной копии базы")


# One-off /stats report at a time the admin picked (/stats_at 5:00); kept in bot_settings to survive restarts.
STATS_AT_KEY = "stats_report_at"
STATS_AT_POLL_SECONDS = 20


def next_msk_epoch(raw: str, now: Optional[float] = None) -> Optional[float]:
    """'5:00' / '05:30' → the next such moment in Moscow time, as a Unix epoch."""
    match = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", raw or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    now = time.time() if now is None else now
    msk = now + 3 * 3600
    target = (msk // DAY) * DAY + hour * 3600 + minute * 60
    if target <= msk:
        target += DAY
    return target - 3 * 3600


async def refresh_live_metrics() -> None:
    """Feed the current proxy and outbox state into METRICS before a report is rendered."""
    from proxy_pool import get_proxy_pool

    try:
        ob = await database.get_outbox_metrics()
        METRICS.update_outbox_state(
            pending_depth=ob.get("pending", 0),
            leased_depth=ob.get("leased", 0),
            oldest_pending_age_seconds=ob.get("oldest_seconds", 0),
        )
        snaps = get_proxy_pool().snapshots()
        METRICS.forget_proxies_except({s["proxy_id"] for s in snaps if s.get("proxy_id")})
        METRICS.snapshot(proxy_snapshots=snaps)
    except Exception:
        logger.debug("Не удалось обновить живые метрики", exc_info=True)


async def send_stats_report(bot: Bot) -> None:
    await refresh_live_metrics()
    chunks = METRICS.format_stats_messages()
    plain = "\n\n".join(re.sub(r"<[^>]+>", "", chunk) for chunk in chunks)
    logger.info("Отчёт мониторинга по расписанию:\n%s", plain)
    for admin_id in config.ADMIN_IDS:
        for chunk in chunks:
            try:
                await bot.send_message(admin_id, chunk, parse_mode="HTML")
            except Exception as exc:
                logger.warning("Отчёт мониторинга не доставлен %s: %s", admin_id, exc)
                break


async def stats_at_worker(bot: Bot) -> None:
    while True:
        await asyncio.sleep(STATS_AT_POLL_SECONDS)
        try:
            raw = await database.get_bot_setting(STATS_AT_KEY)
            if not raw or time.time() < float(raw):
                continue
            await database.set_bot_setting(STATS_AT_KEY, "")
            await send_stats_report(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой отчёта мониторинга по расписанию")


def start_report_services(bot: Bot) -> list[asyncio.Task]:
    return [
        asyncio.create_task(reminder_worker(bot), name="subscription-reminders"),
        asyncio.create_task(daily_report_worker(bot), name="daily-report"),
        asyncio.create_task(stats_at_worker(bot), name="stats-at"),
    ]
