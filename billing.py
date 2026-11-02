"""Telegram Stars orders and transactional, replay-safe subscription changes."""

import secrets
from typing import Any

import aiosqlite

import config
import database


class PaymentValidationError(ValueError):
    pass


async def create_order(user_id: int, plan: str) -> dict[str, Any]:
    if not config.STARS_PAYMENTS_ENABLED:
        raise PaymentValidationError("Приём платежей временно выключен.")
    if plan == "test" and not config.STARS_TEST_ENABLED:
        raise PaymentValidationError("Этот тариф недоступен. Откройте список тарифов заново.")
    amount = config.stars_price(plan)
    days = 1 if plan == "test" else config.STARS_SUBSCRIPTION_DAYS
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
        if amount != config.stars_price(order["tariff_plan"]) or not order["valid"]:
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