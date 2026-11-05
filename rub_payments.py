"""Рублёвые оплаты через Platega: счёт, ожидание оплаты, споры и возвраты, доказательства по заказу.

Platega сообщает о смене статуса вебхуком (platega_webhook.py), но только на публичный HTTPS-адрес.
Поэтому бот спрашивает статус и сам: часто, пока жива ссылка на оплату, и редко потом, по расписанию,
чтобы без вебхука увидеть позднюю оплату, возврат или чарджбэк. Нагрузка: один SELECT в минуту
и несколько запросов к Platega на заказ за всё время его жизни.

Тариф выдаётся только по ответу самой Platega на запрос статуса (CONFIRMED, сумма и номер заказа сходятся),
и только один раз на заказ. Тело вебхука может лишь добавить то, что запрос статуса ещё не показывает:
спор (DISPUTED), частичный или полный возврат.
"""

import asyncio
import calendar
import html
import logging
import time
import weakref
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

import billing
import config
import database
import legal_docs
import platega
import ui

logger = logging.getLogger(__name__)

# Способ → (подпись, слот иконки). Порядок — порядок кнопок.
METHODS: dict[str, tuple[str, str]] = {
    "sbp": ("СБП", "card"),
    "card": ("Банковская карта", "bank"),
    "crypto": ("Криптовалюта", "coin"),
}
STATUS_TITLES = {
    "new": "создан", "pending": "ждёт оплаты", "paid": "оплачен", "disputed": "спор, деньги удержаны",
    "refunded": "деньги возвращены покупателю", "canceled": "не оплачен", "expired": "ссылка истекла",
    "review": "на сверке: сумма не сошлась", "failed": "касса не ответила",
}
LINK_SECONDS_DEFAULT = 15 * 60
LINK_SECONDS_MAX = 2 * 3600
WATCH_GRACE_SECONDS = 5 * 60
SWEEP_SECONDS = 60
SWEEP_BATCH = 20
SWEEP_RETRY_SECONDS = 600
# Проверки после ссылки (от создания заказа): поздняя оплата, CANCELED, сменившийся на CONFIRMED.
UNPAID_RECHECKS = (30 * 60, 3 * 3600, 24 * 3600)
# Проверки оплаченного (от оплаты): возвраты и чарджбэки приходят и через месяцы.
PAID_RECHECKS = tuple(int(hours * 3600) for hours in (1, 6, 24, 72, 168, 336, 720, 1440, 2160, 2880))
DISPUTE_RECHECK_SECONDS = 6 * 3600
DISPUTE_WATCH_DAYS = 90

_watchers: dict[int, asyncio.Task] = {}
_order_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()
_user_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()


def _lock(table, key: int) -> asyncio.Lock:
    lock = table.get(key)
    if lock is None:
        lock = asyncio.Lock()
        table[key] = lock
    return lock


def _num(value) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def rub(value: float | int | None) -> str:
    """449 → «449», 10.85 → «10,85», 1500.5 → «1 500,50»."""
    text = f"{float(value or 0):,.2f}".replace(",", " ").replace(".", ",")
    return text[:-3] if text.endswith(",00") else text


def _epoch(stamp) -> float | None:
    """SQLite UTC «YYYY-MM-DD HH:MM:SS» → epoch."""
    if not stamp:
        return None
    try:
        return float(calendar.timegm(time.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S")))
    except ValueError:
        return None


def msk(stamp) -> str:
    epoch = _epoch(stamp)
    if epoch is None:
        return "—"
    return time.strftime("%d.%m.%Y %H:%M", time.gmtime(epoch + 3 * 3600)) + " МСК"


def _msk_day(stamp) -> str | None:
    epoch = _epoch(stamp)
    return None if epoch is None else time.strftime("%Y-%m-%d", time.gmtime(epoch + 3 * 3600))


def link_seconds(expires_in) -> int:
    """Platega's «00:15:00» (sometimes «00:30:00») → seconds, kept within sane bounds."""
    try:
        hours, minutes, seconds = (int(float(part)) for part in str(expires_in).split(":"))
        total = hours * 3600 + minutes * 60 + seconds
    except (TypeError, ValueError):
        return LINK_SECONDS_DEFAULT
    return max(60, min(LINK_SECONDS_MAX, total))


def link_deadline(order: dict) -> float:
    if order.get("expires_at"):
        return float(order["expires_at"])
    return (_epoch(order.get("created_at")) or time.time()) + LINK_SECONDS_DEFAULT


def order_title(order: dict) -> str:
    if order["tariff_plan"] == "test":
        return "Тест оплаты · 1 день «Среднего»"
    return f"{config.plan_title(order['tariff_plan'])} · {config.period_title(config.period_months(order['days']))}"


def method_title(order: dict) -> str:
    return METHODS.get(order.get("payment_method") or "", ("Platega", ""))[0]


def method_buttons(plan: str, months: int, amount: int) -> list[list[InlineKeyboardButton]]:
    return [[ui.btn(f"{title} — {amount} ₽", icon, callback_data=f"pay_method:{key}:{plan}:{months}",
                    style="success" if key == "sbp" else None)]
            for key, (title, icon) in METHODS.items()]


def next_check_at(order: dict, now: float | None = None) -> float | None:
    """When the sweep asks Platega about this order next; None — never again."""
    now = time.time() if now is None else now
    status = order["status"]
    if status in ("new", "pending"):
        return link_deadline(order) + WATCH_GRACE_SECONDS
    if status == "disputed":
        since = _epoch(order.get("disputed_at")) or now
        return now + DISPUTE_RECHECK_SECONDS if now < since + DISPUTE_WATCH_DAYS * 86400 else None
    if status in ("canceled", "expired"):
        base, steps = _epoch(order.get("created_at")), UNPAID_RECHECKS
    elif status == "paid":
        base, steps = _epoch(order.get("paid_at")), PAID_RECHECKS
    else:  # refunded, review, failed: an admin takes it from here
        return None
    if base is None:
        return None
    return next((base + step for step in steps if base + step > now + 1), None)


def poll_interval(age_seconds: float) -> float:
    """While the link is alive. With the webhook on, polling is only a safety net."""
    if config.PLATEGA_CALLBACK_PORT:
        return 30.0
    if age_seconds < 120:
        return 5.0
    return 15.0 if age_seconds < 600 else 30.0


# ---------- счёт ----------

def _invoice_markup(order: dict, *, with_pay: bool = True) -> InlineKeyboardMarkup:
    icon = METHODS.get(order.get("payment_method") or "", ("", "card"))[1]
    rows = []
    if with_pay and order.get("pay_url"):
        rows.append([ui.btn(f"Оплатить {rub(order.get('pay_total') or order['amount'])} ₽", icon,
                            url=order["pay_url"], style="success")])
    rows.append([ui.btn("Проверить оплату", "refresh", callback_data=f"rub_check:{order['id']}")])
    return ui.kb(*rows)


def _invoice_text(order: dict) -> str:
    amount, total = order["amount"], _num(order.get("pay_total"))
    if total and total > amount + 0.004:
        to_pay = (f"К оплате: <b>{rub(total)} ₽</b> — цена {rub(amount)} ₽ "
                  f"и комиссия платёжной системы {rub(total - amount)} ₽")
    else:
        to_pay = f"К оплате: <b>{rub(amount)} ₽</b>"
    minutes = max(1, round((link_deadline(order) - time.time()) / 60))
    return (
        f"{ui.e('card')} <b>Счёт на {rub(amount)} ₽</b>\n\n"
        f"{html.escape(order_title(order))}\n"
        f"Способ: {method_title(order)}\n"
        f"{to_pay}\n"
        f"Ссылка действует {minutes} мин.\n\n"
        "<blockquote>После оплаты тариф включится сам за несколько секунд.</blockquote>"
    )


async def _send_invoice(bot: Bot, order: dict):
    try:
        sent = await bot.send_message(order["user_id"], _invoice_text(order), reply_markup=_invoice_markup(order))
    except Exception as exc:
        logger.warning("Platega invoice delivery failed: order=%s error=%s", order["id"], type(exc).__name__)
        return
    await billing.set_rub_message(order["id"], sent.chat.id, sent.message_id)


async def start_checkout(bot: Bot, user, plan: str, months: int = 1, method: str = "sbp") -> str | None:
    """Creates (or reuses) an order and sends the payment link. Returns an error text for the user, or None."""
    if method not in METHODS:
        return "Такого способа оплаты нет. Откройте тариф заново."
    async with _lock(_user_locks, user.id):  # two quick taps must not open two payments
        try:
            order = await billing.create_rub_order(user.id, plan, months, method,
                                                   username=user.username, name=user.full_name)
        except billing.PaymentValidationError as exc:
            return str(exc)
        if order.get("reused"):
            await _send_invoice(bot, order)
            watch(bot, order["id"])
            return None
        me = await bot.me()
        try:
            created = await platega.create_payment(
                amount=order["amount"], description=f"Avito Radar · {order_title(order)} · заказ R{order['id']}",
                order_id=order["id"], user_id=user.id, return_url=f"https://t.me/{me.username}", method=method,
            )
            transaction_id = str(created.get("transactionId") or "")
            pay_url = str(created.get("url") or created.get("redirect") or "")
            if not transaction_id or not pay_url.startswith("https://"):
                raise platega.PlategaError(f"unexpected reply, keys={sorted(created)}")
        except platega.PlategaError as exc:
            await billing.set_rub_status(order["id"], "failed", ("new",))
            logger.error("Platega create failed: order=%s error=%s", order["id"], exc)
            return "Касса не ответила. Деньги не списаны: попробуйте через минуту или напишите в /paysupport."
        await billing.attach_rub_payment(order["id"], transaction_id, pay_url,
                                         time.time() + link_seconds(created.get("expiresIn")))
        # Platega adds its fee on top of the price: show the buyer the exact sum before paying.
        try:
            info = await platega.get_status(transaction_id)
            total = _num((info.get("paymentDetails") or {}).get("amount"))
            if total:
                await billing.set_rub_pay_total(order["id"], total)
        except platega.PlategaError as exc:
            logger.info("Platega total unknown: order=%s error=%s", order["id"], exc)
        order = await billing.get_rub_order(order["id"])
        await _send_invoice(bot, order)
    logger.info("Platega order created: order=%s user=%s plan=%s method=%s amount=%s tx=%s",
                order["id"], user.id, plan, method, order["amount"], transaction_id)
    watch(bot, order["id"])
    return None


# ---------- ожидание оплаты ----------

def watch(bot: Bot, order_id: int) -> asyncio.Task:
    task = _watchers.get(order_id)
    if task is None or task.done():
        task = asyncio.create_task(_watch(bot, order_id), name=f"rub-watch-{order_id}")
        _watchers[order_id] = task
        task.add_done_callback(lambda _task, oid=order_id: _watchers.pop(oid, None))
    return task


def watching(order_id: int) -> bool:
    task = _watchers.get(order_id)
    return task is not None and not task.done()


async def _watch(bot: Bot, order_id: int):
    order = await billing.get_rub_order(order_id)
    if not order:
        return
    created = _epoch(order["created_at"]) or time.time()
    deadline = link_deadline(order) + WATCH_GRACE_SECONDS
    while time.time() < deadline:
        await asyncio.sleep(poll_interval(time.time() - created))
        try:
            status = await check_order(bot, order_id)
        except Exception as exc:
            logger.info("Platega watch: order=%s error=%s", order_id, exc)
            continue
        if status != "PENDING":
            return
    await expire(bot, order_id)


async def expire(bot: Bot, order_id: int):
    """The link is dead and nothing came: close the order, keep later checks for a late payment."""
    async with _lock(_order_locks, order_id):
        order = await billing.get_rub_order(order_id)
        if not order or not await billing.set_rub_status(order_id, "expired", ("pending",)):
            return
        await billing.schedule_rub_check(order_id, next_check_at({**order, "status": "expired"}))
    await _edit_invoice(bot, order, (
        f"⌛ <b>Ссылка на оплату устарела</b>\n\n{html.escape(order_title(order))}, {rub(order['amount'])} ₽.\n"
        "Если вы уже заплатили, нажмите «Проверить оплату». Новый счёт — в /tariffs."
    ), with_pay=False)


async def resume(bot: Bot) -> int:
    """After a restart: watchers for live links, the sweep for everything else. Returns watchers started."""
    started = 0
    now = time.time()
    for order in await billing.open_rub_orders():
        if link_deadline(order) + WATCH_GRACE_SECONDS > now:
            watch(bot, order["id"])
            started += 1
        else:
            await billing.schedule_rub_check(order["id"], now)
    for order in await billing.unscheduled_rub_orders():
        if order["status"] != "pending":
            await billing.schedule_rub_check(order["id"], next_check_at(order, now))
    return started


async def sweep_forever(bot: Bot):
    while True:
        try:
            await sweep_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Platega sweep failed", exc_info=True)
        await asyncio.sleep(SWEEP_SECONDS)


async def sweep_once(bot: Bot) -> int:
    """Due status checks; a few requests a day for the whole shop."""
    done = 0
    for order in await billing.due_rub_checks(time.time(), SWEEP_BATCH):
        if watching(order["id"]):
            continue
        try:
            status = await check_order(bot, order["id"])
        except platega.PlategaError as exc:
            logger.info("Platega sweep: order=%s error=%s", order["id"], exc)
            await billing.schedule_rub_check(order["id"], time.time() + SWEEP_RETRY_SECONDS)
            continue
        if status == "PENDING" and order["status"] == "pending" and time.time() >= link_deadline(order) + WATCH_GRACE_SECONDS:
            await expire(bot, order["id"])
        done += 1
        await asyncio.sleep(0.5)
    return done


# ---------- статус ----------

def _payment_problem(order: dict, info: dict) -> str | None:
    """Why a CONFIRMED answer must not grant the tariff, or None when it matches the order."""
    tx = str(info.get("id") or "")
    if tx and tx.lower() != str(order["transaction_id"]).lower():
        return "номер платежа в ответе не совпадает с заказом"
    payload = info.get("payload")
    if payload not in (None, "", f"order:{order['id']}"):
        return "платёж создан не для этого заказа"
    details = info.get("paymentDetails") if isinstance(info.get("paymentDetails"), dict) else {}
    currency = str(details.get("currency") or "RUB").upper()
    if currency != "RUB":
        return f"валюта {html.escape(currency)} вместо RUB"
    paid = _num(info.get("paidAmount"))
    if paid is None or paid <= 0:
        paid = _num(details.get("amount"))
    if paid is None:
        return "Platega не передала сумму"
    if paid + 0.004 < order["amount"]:
        return f"оплачено {rub(paid)} ₽ из {rub(order['amount'])} ₽"
    return None


def _paid_amount(info: dict) -> float | None:
    paid = _num(info.get("paidAmount"))
    if paid is None or paid <= 0:
        paid = _num((info.get("paymentDetails") or {}).get("amount")) if isinstance(info.get("paymentDetails"), dict) else None
    return paid


async def check_order(bot: Bot, order_id: int, reported: dict | None = None) -> str:
    """Asks Platega once and applies the answer. `reported` is a verified webhook body.

    Returns PENDING, CONFIRMED, CANCELED, DISPUTED, CHARGEBACKED, PARTIALLY_REFUNDED, MISMATCH or UNKNOWN."""
    async with _lock(_order_locks, order_id):
        order = await billing.get_rub_order(order_id)
        if not order or not order["transaction_id"]:
            return "UNKNOWN"
        info = await platega.get_status(order["transaction_id"])
        status = str(info.get("status") or "").upper() or "PENDING"
        reported = reported or {}
        said = str(reported.get("status") or "").upper()
        # The status request may lag behind (or not show at all) a dispute or a refund the webhook reports.
        if said in ("DISPUTED", "PARTIALLY_REFUNDED", "CHARGEBACKED") and status == "CONFIRMED":
            status = said
            info = {**info, **{k: v for k, v in reported.items() if k != "status"}}
        result, notes = await _apply(order, status, info)
        fresh = await billing.get_rub_order(order_id)
        # While a link waits for payment nothing changes between polls: no write every few seconds.
        unchanged = (fresh is not None and fresh["status"] == order["status"] == "pending"
                     and order.get("provider_status") == status and order.get("next_check_at") is not None)
        if fresh is not None and not unchanged:
            await billing.note_rub_check(order_id, status, next_check_at(fresh))
    # Telegram messages go out after the lock: a slow Telegram must not hold the order.
    for note in notes:
        try:
            await note(bot)
        except Exception:
            logger.warning("Platega notice failed: order=%s", order_id, exc_info=True)
    return result


async def _apply(order: dict, status: str, info: dict):
    """(result, [notices]) — the database is changed here, the messages are sent by the caller."""
    order_id = order["id"]
    if status == "CONFIRMED":
        if not order["paid_at"] and order["status"] == "review":
            return "MISMATCH", []  # an admin reconciles it by hand; the bot never grants it on its own
        if not order["paid_at"]:
            problem = _payment_problem(order, info)
            if problem:
                logger.error("Platega payment needs review: order=%s problem=%s", order_id, problem)
                if await billing.set_rub_status(order_id, "review", billing.RUB_OPEN_STATUSES):
                    return "MISMATCH", [lambda bot: _alert(bot, order, f"⚠️ <b>Platega: {problem}</b>\n"
                                                           "Тариф не выдан. Сверьте платёж в кабинете Platega.")]
                return "MISMATCH", []
            paid = await billing.complete_rub_order(order_id, order["transaction_id"], _paid_amount(info))
            return "CONFIRMED", ([lambda bot: _on_paid(bot, paid)] if paid["applied"] else [])
        if order["status"] == "disputed" and await billing.set_rub_status(order_id, "paid", ("disputed",)):
            return "CONFIRMED", [lambda bot: _alert(bot, order, "✅ <b>Проверка снята</b>: деньги по заказу остаются у вас.")]
        return "CONFIRMED", []
    if status == "CANCELED":
        if not order["paid_at"] and await billing.set_rub_status(order_id, "canceled", ("new", "pending", "expired")):
            return "CANCELED", [lambda bot: _edit_invoice(bot, order, (
                f"{ui.e('cancel')} <b>Платёж не состоялся</b>\n\nДеньги не списаны. "
                "Если банк всё же списал деньги, нажмите «Проверить оплату» или напишите в /paysupport."
            ), with_pay=False)]
        return "CANCELED", []
    if status == "DISPUTED":
        if order["paid_at"] and await billing.mark_rub_disputed(order_id):
            return "DISPUTED", [lambda bot: _alert(bot, order, (
                "⚠️ <b>Спор по платежу</b>: Platega поставила его на проверку, деньги удержаны.\n"
                "Не возвращайте деньги сами. Отправьте в Platega доказательства (кнопка ниже) до срока из их письма."
            ))]
        return "DISPUTED", []
    if status == "CHARGEBACKED":
        if str(info.get("refundStatus") or "").lower() == "declined":
            return "CHARGEBACKED", [lambda bot: _alert(bot, order, "ℹ️ Platega: возврат по заказу отклонён, деньги не двигались.")]
        if order["paid_at"] and order["status"] != "refunded":
            revoked = await billing.revoke_rub_order(order_id)
            return "CHARGEBACKED", ([lambda bot: _on_refunded(bot, revoked)] if revoked["applied"] else [])
        return "CHARGEBACKED", []
    if status == "PARTIALLY_REFUNDED":
        refunded = _num(info.get("refundedAmount"))
        if order["paid_at"] and refunded and await billing.note_rub_partial_refund(order_id, refunded):
            return status, [lambda bot: _alert(bot, order, (
                f"↩️ <b>Частичный возврат</b>: покупателю вернули {rub(refunded)} ₽. Тариф не менялся — решите вручную."
            ))]
        return status, []
    return status, []


# ---------- уведомления ----------

async def _edit_invoice(bot: Bot, order: dict, text: str, *, with_pay: bool = False):
    if not order.get("message_id"):
        return
    try:
        await bot.edit_message_text(text, chat_id=order["chat_id"], message_id=order["message_id"],
                                    reply_markup=None if with_pay is None else _invoice_markup(order, with_pay=with_pay))
    except Exception:
        pass  # deleted by the user, too old, or not modified


def _admin_markup(order: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📄 Доказательства R{order['id']}", callback_data=f"admin_rubproof:{order['id']}")],
        [InlineKeyboardButton(text="👤 Пользователь", callback_data=f"admin_user:{order['user_id']}:0")],
    ])


def _buyer(order: dict) -> str:
    name = html.escape(order.get("buyer_name") or "Без имени")
    tag = f" @{html.escape(order['buyer_username'])}" if order.get("buyer_username") else ""
    return f"{name}{tag} · <code>{order['user_id']}</code>"


async def _alert(bot: Bot, order: dict, text: str):
    body = (f"{text}\n\nЗаказ <code>R{order['id']}</code> · {html.escape(order_title(order))} · {rub(order['amount'])} ₽ · "
            f"{method_title(order)}\n{_buyer(order)}")
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, body, reply_markup=_admin_markup(order), parse_mode="HTML")
        except Exception as exc:
            logger.warning("Platega admin notice failed: %s", type(exc).__name__)


async def _on_paid(bot: Bot, order: dict):
    await _edit_invoice(bot, order, f"{ui.e('ok')} <b>Счёт на {rub(order['amount'])} ₽ оплачен</b>", with_pay=None)
    plan = "standart" if order["tariff_plan"] == "test" else order["tariff_plan"]
    paused = await database.count_limit_paused(order["user_id"])
    notes = f"\nНе вошли в лимит тарифа и стоят на паузе радаров: {paused}." if paused else ""
    try:
        await bot.send_message(
            order["user_id"],
            f"{ui.e('ok')} <b>Оплата {rub(order['amount'])} ₽ прошла</b>\n\n"
            f"Тариф <b>«{config.plan_title(plan)}»</b>, радаров до {await database.get_user_max_radars(order['user_id'])}.\n"
            f"+{config.plural(order['days'], 'день', 'дня', 'дней')}, "
            f"действует до {database.format_subscription_date(order['subscription_after'])}.\n"
            f"Заказ <code>R{order['id']}</code>, без автосписания. Вопросы: /paysupport.{notes}",
            reply_markup=ui.kb(
                [ui.btn("Добавить радар", "radar", callback_data="add_search_prompt", style="primary")],
                [ui.btn("Мои радары", "radars", callback_data="list_searches"),
                 ui.btn("Профиль", "profile", callback_data="refresh_profile")],
            ),
        )
    except Exception as exc:
        logger.warning("Platega paid notice to user failed: order=%s error=%s", order["id"], type(exc).__name__)
    logger.info("Platega payment fulfilled: order=%s user=%s plan=%s amount=%s paid=%s",
                order["id"], order["user_id"], order["tariff_plan"], order["amount"], order.get("paid_amount"))
    count = await database.count_paid_orders(order["user_id"])
    kind = "первая оплата" if count <= 1 else f"продление, {count}-я оплата"
    paid = _num(order.get("paid_amount"))
    with_fee = f" (покупатель заплатил {rub(paid)} ₽ с комиссией)" if paid and paid > order["amount"] + 0.004 else ""
    await _alert(bot, order, (
        f"💰 <b>Оплата {rub(order['amount'])} ₽ · {method_title(order)}</b>{with_fee}\n"
        f"{kind.capitalize()}, доступ до {database.format_subscription_date(order['subscription_after'])}"
    ))


async def _on_refunded(bot: Bot, order: dict):
    review = bool(order.get("refund_needs_review"))
    await _alert(bot, order, (
        "↩️ <b>Деньги возвращены покупателю</b> (CHARGEBACKED: возврат, чарджбэк или решение по спору).\n"
        + ("⚠️ После оплаты подписка менялась — сверьте срок в карточке пользователя вручную."
           if review else "Доступ, оплаченный этим заказом, отменён.")
    ))
    try:
        await bot.send_message(
            order["user_id"],
            f"↩️ Платёж по заказу <code>R{order['id']}</code> возвращён"
            + ("." if review else ", оплаченный им доступ отменён.") + " Вопросы: /paysupport.",
        )
    except Exception as exc:
        logger.warning("Platega refund notice to user failed: order=%s error=%s", order["id"], type(exc).__name__)


# ---------- доказательства для спора ----------

async def build_evidence(order_id: int, bot_username: str = "") -> tuple[str, bytes] | None:
    """A plain-text file for Platega's dispute form: who paid, what was delivered and how it was used.
    Only the bot's own logs: Platega does not accept screenshots of the bot granting access."""
    order = await billing.get_rub_order(order_id)
    if not order:
        return None
    user = await database.get_user(order["user_id"]) or {}
    plan = "standart" if order["tariff_plan"] == "test" else order["tariff_plan"]
    paid_day = _msk_day(order.get("paid_at"))
    usage = await database.get_usage_by_day(order["user_id"], paid_day) if paid_day else []
    paid_epoch = _epoch(order.get("paid_at"))
    history = [row for row in await database.get_radar_history(order["user_id"], 500)
               if paid_epoch and (_epoch(row.get("at")) or 0) >= paid_epoch]
    searches = await database.get_user_searches(order["user_id"])
    rub_orders = await billing.list_rub_orders(order["user_id"], 20)
    stars_orders = await billing.list_orders(order["user_id"], 20)
    username = user.get("username")
    total_lots = sum(lots for _day, lots, _opens in usage)
    lot_days = sum(1 for _day, lots, _opens in usage if lots)
    open_days = sum(1 for _day, _lots, opens in usage if opens)
    lines = [
        "ДОКАЗАТЕЛЬСТВА ОКАЗАНИЯ УСЛУГИ",
        f"Заказ R{order['id']} · сервис Avito Radar (Telegram-бот{' @' + bot_username if bot_username else ''})",
        f"Сформировано: {time.strftime('%d.%m.%Y %H:%M', time.gmtime(time.time() + 3 * 3600))} МСК",
        "",
        "1. ПЛАТЁЖ",
        f"Номер заказа в боте: R{order['id']}",
        f"Транзакция Platega: {order.get('transaction_id') or '—'}",
        f"Способ оплаты: {method_title(order)}",
        f"Цена услуги: {rub(order['amount'])} ₽"
        + (f"; покупатель заплатил {rub(order['paid_amount'])} ₽ с комиссией платёжной системы"
           if _num(order.get("paid_amount")) else ""),
        f"Статус в Platega: {order.get('provider_status') or '—'} (проверен {msk(order.get('last_checked_at'))})",
        f"Статус заказа в боте: {STATUS_TITLES.get(order['status'], order['status'])}",
        f"Счёт создан: {msk(order.get('created_at'))}; оплата подтверждена: {msk(order.get('paid_at'))}",
        "",
        "2. ПОКУПАТЕЛЬ",
        f"Telegram ID: {order['user_id']}",
        f"Username при оплате: {'@' + order['buyer_username'] if order.get('buyer_username') else 'нет'}"
        + (f"; сейчас: @{username}" if username and username != order.get("buyer_username") else ""),
        f"Имя в Telegram при оплате: {order.get('buyer_name') or '—'}",
        f"Связь с покупателем: {'https://t.me/' + username if username else 'нет username'} · tg://user?id={order['user_id']}",
        f"Пользуется ботом с: {msk(user.get('created_at'))}",
        "IP-адрес: бот работает внутри Telegram, Telegram не передаёт ботам IP-адреса пользователей.",
        "",
        "3. ЧТО КУПЛЕНО И КАК ВЫДАНО",
        f"Услуга: доступ к сервису мониторинга объявлений Avito Radar, тариф «{config.plan_title(plan)}» "
        f"на {config.plural(order['days'], 'день', 'дня', 'дней')}: до {config.plan_radars(plan)} радаров, "
        f"проверка каждого раз в {config.plan_interval(plan)} с.",
        f"Доступ выдан автоматически сразу после подтверждения оплаты: {msk(order.get('paid_at'))}.",
        f"Подписка до оплаты: {'до ' + msk(order['subscription_before']) if order.get('subscription_before') else 'не было'}; "
        f"после оплаты: до {msk(order.get('subscription_after'))}.",
        f"Условия, принятые при оплате: редакция от {legal_docs.DOCS_DATE}, код проверки {legal_docs.VERIFICATION_CODE} "
        "(команда /terms в боте). Разовая оплата без автосписаний.",
        "",
        "4. ИСПОЛЬЗОВАНИЕ ПОСЛЕ ОПЛАТЫ",
        "День (МСК) — доставлено объявлений — заходов в бота",
        *[f"{day} — {lots} — {opens}" for day, lots, opens in usage],
        *([] if usage else ["нет данных за период после оплаты"]),
        f"Итого после оплаты: {config.plural(total_lots, 'объявление', 'объявления', 'объявлений')} "
        f"за {lot_days} дн.; заходил в бота {open_days} дн.",
        f"Последний заход в бота: {msk(user.get('last_seen_at'))}",
        f"Радаров сейчас: {sum(1 for s in searches if s.get('is_active'))} работают из {len(searches)}.",
        "",
        "5. ДЕЙСТВИЯ С РАДАРАМИ ПОСЛЕ ОПЛАТЫ (журнал бота)",
        *[f"{msk(row.get('at'))} · {database.RADAR_HISTORY_LABELS.get(row['action'], row['action'])} · "
          f"радар #{row['search_id']} «{row.get('title') or ''}» {row.get('url') or ''}".rstrip()
          for row in reversed(history[:200])],
        *([] if history else ["изменений не было"]),
        "",
        "6. ДРУГИЕ ОПЛАТЫ ПОКУПАТЕЛЯ",
        *[f"R{o['id']} · {msk(o.get('paid_at'))} · {rub(o['amount'])} ₽ · {method_title(o)} · "
          f"{STATUS_TITLES.get(o['status'], o['status'])}" for o in rub_orders if o["id"] != order["id"]],
        *[f"Stars #{o['id']} · {msk(o.get('paid_at'))} · {o['total_amount']} ⭐ · {o['status']}" for o in stars_orders],
        *([] if len(rub_orders) > 1 or stars_orders else ["нет"]),
    ]
    return f"proof-R{order['id']}.txt", ("\n".join(lines) + "\n").encode("utf-8")


async def send_evidence(bot: Bot, chat_id: int, order_id: int) -> bool:
    try:  # the file shows Platega's current status, not the one from the last scheduled check
        await check_order(bot, order_id)
    except Exception as exc:
        logger.info("Platega status before evidence failed: order=%s error=%s", order_id, type(exc).__name__)
    me = await bot.me()
    built = await build_evidence(order_id, me.username or "")
    if not built:
        return False
    name, data = built
    await bot.send_document(chat_id, BufferedInputFile(data, filename=name), caption=(
        f"📄 Доказательства по заказу R{order_id}. Перешлите файл в Platega вместе с перепиской с покупателем, "
        "если она была. Скриншоты выдачи подписки Platega не принимает."
    ))
    return True
