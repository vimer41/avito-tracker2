"""Telegram Stars and Platega (rubles) orders; transactional, replay-safe subscription changes."""

import secrets
import time
from typing import Any

import aiosqlite

import config
import database


class PaymentValidationError(ValueError):
    pass


async def create_order(user_id: int, plan: str, months: int = 1) -> dict[str, Any]:
    if not config.STARS_PAYMENTS_ENABLED:
        raise PaymentValidationError("Приём платежей временно выключен.")
    if plan == "test" and not config.STARS_TEST_ENABLED:
        raise PaymentValidationError("Этот тариф недоступен. Откройте список тарифов заново.")
    if months not in config.PERIOD_MONTHS or (plan == "test" and months != 1):
        raise PaymentValidationError("Такого срока нет. Откройте тариф заново.")
    amount = config.stars_price(plan, months)
    days = 1 if plan == "test" else config.period_days(months)
    user = await database.get_user(user_id)
    if not user:
        raise PaymentValidationError("Сначала откройте /start.")
    if config.is_admin(user_id) or (user["is_allowed"] and not user["subscription_until"]):
        raise PaymentValidationError("У вас уже есть бессрочный доступ. Оплата не требуется.")
    payload = "stars:v1:" + secrets.token_hex(24)
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute("""
            INSERT INTO stars_orders(payload, user_id, tariff_plan, total_amount, days, expires_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', '+1 hour'))
        """, (payload, user_id, plan, amount, days))
        await db.commit()
    return await get_order(payload=payload)


async def get_order(*, payload: str | None = None, order_id: int | None = None,
                    charge_id: str | None = None) -> dict[str, Any] | None:
    field, value = ("payload", payload) if payload is not None else (
        ("id", order_id) if order_id is not None else ("telegram_payment_charge_id", charge_id)
    )
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"SELECT * FROM stars_orders WHERE {field} = ?", (value,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_orders(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM stars_orders WHERE user_id = ? AND telegram_payment_charge_id IS NOT NULL
            ORDER BY id DESC LIMIT ?
        """, (user_id, min(50, max(1, limit)))) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def approve_checkout(query_id: str, user_id: int, payload: str, currency: str, amount: int):
    """Reserve one invoice before answering Telegram's 10-second pre-checkout."""
    if not config.STARS_PAYMENTS_ENABLED:
        raise PaymentValidationError("Приём платежей временно выключен. Списание не выполнено.")
    async with aiosqlite.connect(database.DB_PATH, timeout=3) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT *, expires_at > datetime('now') AS valid FROM stars_orders WHERE payload = ?", (payload,)) as cursor:
            order = await cursor.fetchone()
        if not order or order["user_id"] != user_id:
            raise PaymentValidationError("Счёт не найден или принадлежит другому пользователю.")
        if currency != "XTR" or amount != order["total_amount"]:
            raise PaymentValidationError("Валюта или сумма счёта не совпадает. Откройте тариф заново.")
        if amount != config.stars_price(order["tariff_plan"], config.period_months(order["days"])) or not order["valid"]:
            raise PaymentValidationError("Счёт устарел. Откройте тариф и получите новый счёт.")
        if order["status"] == "approved" and order["pre_checkout_query_id"] == query_id:
            return
        if order["status"] != "pending":
            raise PaymentValidationError("Этот счёт уже обрабатывается или оплачен. Получите новый счёт.")
        async with db.execute("SELECT is_allowed, subscription_until FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()
        if not user or config.is_admin(user_id) or (user["is_allowed"] and not user["subscription_until"]):
            raise PaymentValidationError("Покупка недоступна. Обратитесь в /paysupport.")
        await db.execute("""
            UPDATE stars_orders SET status = 'approved', pre_checkout_query_id = ?, approved_at = datetime('now')
            WHERE id = ?
        """, (query_id, order["id"]))
        await db.commit()


async def complete_payment(user_id: int, payload: str, currency: str, amount: int,
                           charge_id: str, provider_charge_id: str = "") -> dict[str, Any]:
    """Only a Telegram successful_payment grants access, including after shutdown of sales."""
    if not charge_id:
        raise PaymentValidationError("Telegram не передал идентификатор платежа.")
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT * FROM stars_orders WHERE payload = ?", (payload,)) as cursor:
            order = await cursor.fetchone()
        if not order or order["user_id"] != user_id or currency != "XTR" or amount != order["total_amount"]:
            raise PaymentValidationError("Подтверждённый платёж не совпадает со счётом. Нужна сверка поддержки.")
        if order["telegram_payment_charge_id"]:
            if order["telegram_payment_charge_id"] != charge_id:
                raise PaymentValidationError("Для счёта уже записан другой платёж. Нужна сверка поддержки.")
            return dict(order, applied=False)
        if order["status"] != "approved" or not order["approved_at"]:
            raise PaymentValidationError("Нет сохранённого подтверждения счёта. Нужна сверка поддержки.")
        async with db.execute("SELECT id FROM stars_orders WHERE telegram_payment_charge_id = ?", (charge_id,)) as cursor:
            if await cursor.fetchone():
                raise PaymentValidationError("Идентификатор платежа уже использован другим счётом.")
        # A user could have been deleted between pre-checkout and confirmation.
        # Restore only paid access, never a new free trial.
        await db.execute("INSERT OR IGNORE INTO users(user_id, trial_used) VALUES (?, 1)", (user_id,))
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            before = await cursor.fetchone()
        until = await database.grant_subscription_in_transaction(db, user_id, order["days"], order["tariff_plan"])
        async with db.execute("SELECT subscription_revision FROM users WHERE user_id = ?", (user_id,)) as cursor:
            revision = (await cursor.fetchone())[0]
        await db.execute("""
            UPDATE stars_orders SET status = 'paid', telegram_payment_charge_id = ?, provider_payment_charge_id = ?,
                paid_at = datetime('now'), subscription_before = ?, tariff_before = ?, allowed_before = ?,
                trial_before = ?, subscription_after = ?, tariff_after = ?, revision_after = ? WHERE id = ?
        """, (charge_id, provider_charge_id, before["subscription_until"], before["tariff_plan"],
              before["is_allowed"], before["trial_used"], until, order["tariff_plan"], revision, order["id"]))
        await db.commit()
        async with db.execute("SELECT * FROM stars_orders WHERE id = ?", (order["id"],)) as cursor:
            return dict(await cursor.fetchone(), applied=True)


# ---------- Рубли через Platega ----------
# Заказ живёт в rub_orders. Тариф выдаёт только complete_rub_order — один раз, отметка paid_at;
# отменяет его только revoke_rub_order, когда деньги вернулись покупателю.

RUB_REUSE_MINUTES = 10   # повторное нажатие той же кнопки отдаёт ту же ссылку, а не новый платёж
RUB_ORDERS_PER_HOUR = 6  # больше новых счетов за час не выдаём: брошенные платежи портят репутацию у кассы
RUB_OPEN_STATUSES = ("new", "pending", "expired", "canceled")


async def _rub_row(db, order_id: int) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM rub_orders WHERE id = ?", (order_id,)) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def create_rub_order(user_id: int, plan: str, months: int = 1, method: str | None = None, *,
                           username: str | None = None, name: str | None = None) -> dict[str, Any]:
    """A new order, or this user's still-open order for the same purchase (reused=True): one link per purchase."""
    if not config.PLATEGA_ENABLED:
        raise PaymentValidationError("Оплата рублями временно недоступна.")
    is_test = plan == config.RUB_TEST_PLAN
    if not is_test and plan not in config.PAID_PLANS:
        raise PaymentValidationError("Такого тарифа нет. Откройте список тарифов заново.")
    if months not in config.PERIOD_MONTHS or (is_test and months != 1):
        raise PaymentValidationError("Такого срока нет. Откройте тариф заново.")
    user = await database.get_user(user_id)
    if not user:
        raise PaymentValidationError("Сначала откройте /start.")
    if is_test:
        # The test checkout is open to admins too: they check the cashbox. It must not cut an endless access
        # down to one hour or convert someone's paid days of another tariff.
        if not config.is_admin(user_id):
            if user["is_allowed"] and not user["subscription_until"]:
                raise PaymentValidationError("У вас уже есть бессрочный доступ. Оплата не требуется.")
            if (user["is_allowed"] and database.subscription_seconds_left(user)
                    and (user["tariff_plan"] or "").lower() != config.RUB_TEST_GRANTS):
                raise PaymentValidationError("Тест доступен без активной подписки или на тарифе «Полный».")
    elif config.is_admin(user_id) or (user["is_allowed"] and not user["subscription_until"]):
        raise PaymentValidationError("У вас уже есть бессрочный доступ. Оплата не требуется.")
    amount = config.rub_price(plan, months)
    days = 1 if is_test else config.period_days(months)
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("""
            SELECT * FROM rub_orders
            WHERE user_id = ? AND tariff_plan = ? AND days = ? AND amount = ? AND payment_method IS ?
              AND status = 'pending' AND pay_url IS NOT NULL AND COALESCE(expires_at, 0) > ?
              AND created_at > datetime('now', ?)
            ORDER BY id DESC LIMIT 1
        """, (user_id, plan, days, amount, method, time.time() + 120, f"-{RUB_REUSE_MINUTES} minutes")) as cursor:
            open_order = await cursor.fetchone()
        if open_order:
            await db.rollback()
            return dict(open_order, reused=True)
        async with db.execute(
            "SELECT COUNT(*) FROM rub_orders WHERE user_id = ? AND created_at > datetime('now', '-1 hour')", (user_id,)
        ) as cursor:
            if (await cursor.fetchone())[0] >= RUB_ORDERS_PER_HOUR:
                raise PaymentValidationError("Слишком много счетов за час. Оплатите уже выданный счёт или попробуйте позже.")
        cursor = await db.execute("""
            INSERT INTO rub_orders(user_id, tariff_plan, amount, days, payment_method, buyer_username, buyer_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, plan, amount, days, method, (username or "")[:64] or None, (name or "")[:128] or None))
        order_id = cursor.lastrowid
        await db.commit()
        return dict(await _rub_row(db, order_id), reused=False)


async def get_rub_order(order_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await _rub_row(db, order_id)


async def find_rub_order(*, transaction_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM rub_orders WHERE lower(transaction_id) = lower(?)", (str(transaction_id),)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_rub_orders(user_id: int, limit: int = 10, *, paid_only: bool = True) -> list[dict[str, Any]]:
    """Newest first; paid_only keeps orders that ever brought money (paid, disputed, refunded)."""
    where = "AND paid_at IS NOT NULL" if paid_only else ""
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM rub_orders WHERE user_id = ? {where} ORDER BY id DESC LIMIT ?",
            (user_id, min(50, max(1, limit))),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def attach_rub_payment(order_id: int, transaction_id: str, pay_url: str, expires_at: float,
                             pay_total: float | None = None) -> bool:
    """Saved before the link is shown: after a crash at any later step the bot still finds the payment."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE rub_orders SET transaction_id = ?, pay_url = ?, expires_at = ?, pay_total = ?, status = 'pending'
            WHERE id = ? AND status = 'new'
        """, (transaction_id, pay_url, expires_at, pay_total, order_id))
        await db.commit()
        return cursor.rowcount == 1


async def set_rub_pay_total(order_id: int, pay_total: float):
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute("UPDATE rub_orders SET pay_total = ? WHERE id = ?", (pay_total, order_id))
        await db.commit()


async def set_rub_message(order_id: int, chat_id: int, message_id: int):
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute("UPDATE rub_orders SET chat_id = ?, message_id = ? WHERE id = ?", (chat_id, message_id, order_id))
        await db.commit()


async def set_rub_status(order_id: int, status: str, from_statuses: tuple[str, ...] = ("new", "pending")) -> bool:
    """Moves the order only from the given statuses; True when it moved. A granted order is never reopened here."""
    marks = ",".join("?" for _ in from_statuses)
    async with aiosqlite.connect(database.DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE rub_orders SET status = ? WHERE id = ? AND status IN ({marks})", (status, order_id, *from_statuses)
        )
        await db.commit()
        return cursor.rowcount == 1


async def note_rub_check(order_id: int, provider_status: str, next_check_at: float | None):
    """What Platega answered, when, and when to ask next — one write per status check."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute(
            "UPDATE rub_orders SET provider_status = ?, last_checked_at = datetime('now'), next_check_at = ? WHERE id = ?",
            (provider_status or None, next_check_at, order_id),
        )
        await db.commit()


async def schedule_rub_check(order_id: int, at: float | None):
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute("UPDATE rub_orders SET next_check_at = ? WHERE id = ?", (at, order_id))
        await db.commit()


async def due_rub_checks(now: float, limit: int = 20) -> list[dict[str, Any]]:
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM rub_orders WHERE next_check_at IS NOT NULL AND next_check_at <= ? ORDER BY next_check_at LIMIT ?",
            (now, limit),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def unscheduled_rub_orders() -> list[dict[str, Any]]:
    """Orders that still need status checks but have no time for the next one (a crash between steps)."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM rub_orders
            WHERE next_check_at IS NULL AND transaction_id IS NOT NULL
              AND (status IN ('pending', 'paid', 'disputed', 'canceled', 'expired')
                   OR (status = 'refunded' AND refund_requested_at IS NOT NULL AND refund_result IS NULL))
        """) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def open_rub_orders() -> list[dict[str, Any]]:
    """Orders still waiting for payment; their watchers resume after a restart."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM rub_orders WHERE status = 'pending'") as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def complete_rub_order(order_id: int, transaction_id: str, paid_amount: float | None = None) -> dict[str, Any]:
    """Grants the tariff once per order. A repeated CONFIRMED (re-delivery, a closed dispute) returns applied=False."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        order = await _rub_row(db, order_id)
        if not order or (order["transaction_id"] or "").lower() != (transaction_id or "").lower():
            raise PaymentValidationError("Платёж не совпадает с заказом. Нужна сверка поддержки.")
        if order["paid_at"]:
            return dict(order, applied=False)
        if order["status"] not in RUB_OPEN_STATUSES:
            raise PaymentValidationError(f"Заказ в статусе {order['status']}: тариф выдаётся только после сверки.")
        user_id = order["user_id"]
        # A user could have been deleted after creating the order. Restore only paid access, never a new trial.
        await db.execute("INSERT OR IGNORE INTO users(user_id, trial_used) VALUES (?, 1)", (user_id,))
        async with db.execute(
            "SELECT subscription_until, tariff_plan, is_allowed, trial_used FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            before = await cursor.fetchone()
        if order["tariff_plan"] == config.RUB_TEST_PLAN:
            until = await database.grant_subscription_in_transaction(
                db, user_id, 1, config.RUB_TEST_GRANTS, seconds=config.RUB_TEST_SECONDS)
        else:
            until = await database.grant_subscription_in_transaction(db, user_id, order["days"], order["tariff_plan"])
        async with db.execute("SELECT tariff_plan, subscription_revision FROM users WHERE user_id = ?", (user_id,)) as cursor:
            after = await cursor.fetchone()
        await db.execute("""
            UPDATE rub_orders SET status = 'paid', paid_at = datetime('now'), paid_amount = ?,
                subscription_before = ?, tariff_before = ?, allowed_before = ?, trial_before = ?,
                subscription_after = ?, tariff_after = ?, revision_after = ?
            WHERE id = ?
        """, (paid_amount, before["subscription_until"], before["tariff_plan"], before["is_allowed"], before["trial_used"],
              until, after["tariff_plan"], after["subscription_revision"], order_id))
        await db.commit()
        return dict(await _rub_row(db, order_id), applied=True)


async def mark_rub_disputed(order_id: int) -> bool:
    """Platega put a paid order on review: money is held. True the first time (and again after a closed dispute)."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE rub_orders SET status = 'disputed', disputed_at = datetime('now')
            WHERE id = ? AND status = 'paid' AND paid_at IS NOT NULL
        """, (order_id,))
        await db.commit()
        return cursor.rowcount == 1


async def note_rub_partial_refund(order_id: int, refunded: float) -> bool:
    """True when the refunded total changed; the tariff stays, an admin decides."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE rub_orders SET refunded_amount = ?
            WHERE id = ? AND paid_at IS NOT NULL AND ABS(COALESCE(refunded_amount, 0) - ?) > 0.004
        """, (refunded, order_id, refunded))
        await db.commit()
        return cursor.rowcount == 1


async def _rub_revoke_review(db, row) -> tuple[Any, bool]:
    """(user, review): review = the subscription changed after this payment, so undoing it would erase
    a later payment or an admin's change. Then nothing is overwritten and an admin fixes the term by hand."""
    async with db.execute("SELECT * FROM users WHERE user_id = ?", (row["user_id"],)) as cursor:
        user = await cursor.fetchone()
    async with db.execute("""
        SELECT 1 FROM rub_orders WHERE user_id = ? AND refund_needs_review = 1
        UNION ALL SELECT 1 FROM stars_orders WHERE user_id = ? AND refund_needs_review = 1 LIMIT 1
    """, (row["user_id"], row["user_id"])) as cursor:
        prior_review = await cursor.fetchone()
    review = bool(user) and (
        bool(prior_review) or row["allowed_before"] is None or row["revision_after"] is None
        or user["subscription_revision"] != row["revision_after"]
        or user["subscription_until"] != row["subscription_after"] or user["tariff_plan"] != row["tariff_after"]
    )
    return user, review


async def rub_revoke_needs_review(order_id: int) -> bool:
    """For the refund confirmation: True when a refund will not take the access back by itself."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await _rub_row(db, order_id)
        if not row or not row["paid_at"]:
            return True
        _user, review = await _rub_revoke_review(db, row)
        return review


async def mark_rub_refund_requested(order_id: int, admin_id: int, operation_id: str | None):
    """Platega took an admin's refund: the bot now waits for CHARGEBACKED (refund_result stays NULL until then)."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute("""
            UPDATE rub_orders SET refund_requested_at = datetime('now'), refund_admin_id = ?, refund_operation_id = ?,
                refund_result = NULL
            WHERE id = ?
        """, (admin_id, operation_id or None, order_id))
        await db.commit()


async def set_rub_refund_result(order_id: int, result: str) -> bool:
    """done / declined / overdue, once per refund; True the first time."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE rub_orders SET refund_result = ? WHERE id = ? AND refund_requested_at IS NOT NULL AND refund_result IS NULL",
            (result, order_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def revoke_rub_order(order_id: int) -> dict[str, Any]:
    """Money went back to the buyer (CHARGEBACKED, or an admin's refund from the bot): undo this order's grant once.
    If the subscription changed after the payment (another payment, an admin), nothing is overwritten: the order is flagged."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        row = await _rub_row(db, order_id)
        if not row or not row["paid_at"]:
            raise PaymentValidationError("Оплаченный заказ не найден.")
        if row["status"] == "refunded":
            return dict(row, applied=False)
        user, review = await _rub_revoke_review(db, row)
        if user and not review:
            await db.execute("""
                UPDATE users SET subscription_until = ?, tariff_plan = ?, is_allowed = ?, trial_used = ?,
                    subscription_revision = subscription_revision + 1 WHERE user_id = ?
            """, (row["subscription_before"], row["tariff_before"], row["allowed_before"], row["trial_before"], row["user_id"]))
            await database.sync_radar_limit_in_transaction(db, row["user_id"])
        await db.execute("""
            UPDATE rub_orders SET status = 'refunded', refunded_at = datetime('now'), refund_needs_review = ?,
                refunded_amount = COALESCE(paid_amount, amount)
            WHERE id = ?
        """, (int(review), order_id))
        await db.commit()
        return dict(await _rub_row(db, order_id), applied=True)


async def begin_refund(order_id: int, admin_id: int) -> dict[str, Any]:
    if not config.is_admin(admin_id):
        raise PermissionError("У вас нет прав администратора")
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("""
            SELECT *, refund_requested_at > datetime('now', '-5 minutes') AS refund_busy
            FROM stars_orders WHERE id = ?
        """, (order_id,)) as cursor:
            row = await cursor.fetchone()
        if not row or not row["telegram_payment_charge_id"]:
            raise PaymentValidationError("Оплаченный счёт не найден.")
        if row["status"] == "refunded":
            return dict(row, request=False)
        if row["status"] == "refunding" and row["refund_busy"]:
            raise PaymentValidationError("Возврат обрабатывается. При сбое повторите через 5 минут.")
        if row["status"] not in ("paid", "refunding"):
            raise PaymentValidationError("Возврат этого счёта недоступен.")
        await db.execute("""
            UPDATE stars_orders SET status = 'refunding', refund_requested_at = datetime('now'), refund_admin_id = ?
            WHERE id = ?
        """, (admin_id, order_id))
        await db.commit()
        return dict(row, request=True)


async def cancel_refund(order_id: int):
    """Definite Telegram rejection permits retry; ambiguous network errors keep the lease."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute("UPDATE stars_orders SET status = 'paid', refund_requested_at = NULL WHERE id = ? AND status = 'refunding'", (order_id,))
        await db.commit()


async def complete_refund(charge_id: str) -> dict[str, Any]:
    """Undo the last unchanged grant once; never erase a later payment or admin change."""
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT * FROM stars_orders WHERE telegram_payment_charge_id = ?", (charge_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            raise PaymentValidationError("Возвращённый платёж не найден в базе. Нужна ручная сверка.")
        if row["status"] == "refunded":
            return dict(row, applied=False)
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (row["user_id"],)) as cursor:
            user = await cursor.fetchone()
        async with db.execute("SELECT 1 FROM stars_orders WHERE user_id = ? AND refund_needs_review = 1 LIMIT 1", (row["user_id"],)) as cursor:
            prior_review = await cursor.fetchone()
        review = bool(user) and (
            bool(prior_review) or user["subscription_revision"] != row["revision_after"]
            or user["subscription_until"] != row["subscription_after"] or user["tariff_plan"] != row["tariff_after"]
        )
        if user and not review:
            await db.execute("""
                UPDATE users SET subscription_until = ?, tariff_plan = ?, is_allowed = ?, trial_used = ?,
                    subscription_revision = subscription_revision + 1 WHERE user_id = ?
            """, (row["subscription_before"], row["tariff_before"], row["allowed_before"], row["trial_before"], row["user_id"]))
            await database.sync_radar_limit_in_transaction(db, row["user_id"])
        await db.execute("""
            UPDATE stars_orders SET status = 'refunded', refunded_at = datetime('now'), refund_needs_review = ? WHERE id = ?
        """, (int(review), row["id"]))
        await db.commit()
        return dict(row, status="refunded", refund_needs_review=int(review), applied=True)