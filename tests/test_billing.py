import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import _environment
import aiosqlite

import billing
import config
import database


class TestStarsBilling(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-billing-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        self.enabled_patch = patch.object(config, "STARS_PAYMENTS_ENABLED", True)
        self.enabled_patch.start()
        await database.init_db()
        self.user_id = 123456
        await database.get_or_create_user(self.user_id)
        await database.activate_trial(self.user_id)

    async def asyncTearDown(self):
        self.enabled_patch.stop()
        self.db_patch.stop()
        self.directory.cleanup()

    async def execute(self, statement, parameters=()):
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(statement, parameters)
            await db.commit()

    async def approved_order(self, plan="standart"):
        order = await billing.create_order(self.user_id, plan)
        await billing.approve_checkout(f"q-{order['id']}", self.user_id, order["payload"], "XTR", order["total_amount"])
        return order

    async def pay(self, order, charge="charge-1"):
        return await billing.complete_payment(self.user_id, order["payload"], "XTR", order["total_amount"], charge)

    async def test_default_prices_and_explicit_disable(self):
        self.assertEqual(config.stars_price("standart"), 449)
        self.assertEqual(config.stars_price("pro"), 899)
        self.assertEqual(config.stars_price("test"), 10)
        with self.assertRaises(ValueError):
            config.stars_price("fake")
        with patch.object(config, "STARS_PAYMENTS_ENABLED", False):
            with self.assertRaises(billing.PaymentValidationError):
                await billing.create_order(self.user_id, "pro")
        async with aiosqlite.connect(database.DB_PATH) as db:
            self.assertEqual((await (await db.execute("SELECT COUNT(*) FROM stars_orders")).fetchone())[0], 0)

    async def test_test_tariff_is_closed_by_default(self):
        with self.assertRaises(billing.PaymentValidationError):
            await billing.create_order(self.user_id, "test")

    @patch.object(config, "STARS_TEST_ENABLED", True)
    async def test_test_tariff_flow(self):
        order = await billing.create_order(self.user_id, "test")
        self.assertEqual(order["total_amount"], 10)
        self.assertEqual(order["tariff_plan"], "test")
        await billing.approve_checkout(f"q-{order['id']}", self.user_id, order["payload"], "XTR", 10)
        grant = await self.pay(order, charge="charge-test")
        self.assertTrue(grant["applied"])
        user = await database.get_user(self.user_id)
        self.assertEqual(user["tariff_plan"], "standart")
        self.assertTrue(await database.is_user_allowed(self.user_id))

    async def test_checkout_validates_owner_amount_currency_and_payload(self):
        order = await billing.create_order(self.user_id, "standart")
        before = await database.get_user(self.user_id)
        for user_id, payload, currency, amount in (
            (654321, order["payload"], "XTR", 449), (self.user_id, "fake", "XTR", 449),
            (self.user_id, order["payload"], "RUB", 449), (self.user_id, order["payload"], "XTR", 1),
        ):
            with self.subTest(user_id=user_id, payload=payload, currency=currency, amount=amount):
                with self.assertRaises(billing.PaymentValidationError):
                    await billing.approve_checkout("q", user_id, payload, currency, amount)
        self.assertEqual(await database.get_user(self.user_id), before)
        self.assertEqual((await billing.get_order(payload=order["payload"]))["status"], "pending")

    async def test_checkout_never_grants_and_reserves_invoice_once(self):
        before = await database.get_user(self.user_id)
        order = await self.approved_order()
        await billing.approve_checkout(f"q-{order['id']}", self.user_id, order["payload"], "XTR", 449)
        self.assertEqual(await database.get_user(self.user_id), before)
        with self.assertRaises(billing.PaymentValidationError):
            await billing.approve_checkout("another-q", self.user_id, order["payload"], "XTR", 449)

    async def test_checkout_rejects_stale_or_changed_price(self):
        order = await billing.create_order(self.user_id, "standart")
        with patch.object(config, "STARS_PRICE_STANDART", 450):
            with self.assertRaises(billing.PaymentValidationError):
                await billing.approve_checkout("q", self.user_id, order["payload"], "XTR", 449)
        await self.execute("UPDATE stars_orders SET expires_at = datetime('now', '-1 second')")
        with self.assertRaises(billing.PaymentValidationError):
            await billing.approve_checkout("q", self.user_id, order["payload"], "XTR", 449)

    async def test_permanent_or_admin_access_is_not_sold(self):
        await database.set_user_allowed(self.user_id, 1)
        for user_id in (self.user_id, config.ADMIN_ID):
            await database.get_or_create_user(user_id)
            with self.assertRaises(billing.PaymentValidationError):
                await billing.create_order(user_id, "pro")

    async def test_paid_upgrade_preserves_trial_and_grants_correct_limit(self):
        before = await database.get_user(self.user_id)
        order = await self.approved_order("pro")
        result = await self.pay(order)
        after = await database.get_user(self.user_id)
        self.assertTrue(result["applied"])
        self.assertEqual(result["telegram_payment_charge_id"], "charge-1")
        self.assertEqual(after["tariff_plan"], "pro")
        self.assertEqual(after["is_allowed"], 1)
        self.assertEqual(await database.get_user_max_radars(self.user_id), 4)
        self.assertEqual(datetime.fromisoformat(after["subscription_until"]),
                         datetime.fromisoformat(before["subscription_until"]) + timedelta(days=30))

    async def test_concurrent_replay_only_grants_once(self):
        before = await database.get_user(self.user_id)
        order = await self.approved_order()
        results = await asyncio.gather(self.pay(order), self.pay(order), self.pay(order))
        self.assertEqual(sum(r["applied"] for r in results), 1)
        after = await database.get_user(self.user_id)
        self.assertEqual(after["subscription_revision"], before["subscription_revision"] + 1)
        self.assertEqual(datetime.fromisoformat(after["subscription_until"]),
                         datetime.fromisoformat(before["subscription_until"]) + timedelta(days=30))

    async def test_distinct_payments_extend_but_charge_ids_cannot_be_reused(self):
        first = await self.approved_order()
        await self.pay(first)
        before = await database.get_user(self.user_id)
        second = await self.approved_order()
        with self.assertRaises(billing.PaymentValidationError):
            await self.pay(second)
        self.assertEqual(await database.get_user(self.user_id), before)
        await self.pay(second, "charge-2")
        after = await database.get_user(self.user_id)
        self.assertEqual(datetime.fromisoformat(after["subscription_until"]),
                         datetime.fromisoformat(before["subscription_until"]) + timedelta(days=30))

    async def test_successful_payment_requires_matching_approved_order(self):
        before = await database.get_user(self.user_id)
        order = await billing.create_order(self.user_id, "standart")
        with self.assertRaises(billing.PaymentValidationError):
            await self.pay(order)
        await billing.approve_checkout("q", self.user_id, order["payload"], "XTR", 449)
        for user, payload, currency, amount, charge in (
            (self.user_id + 1, order["payload"], "XTR", 449, "charge"),
            (self.user_id, "fake", "XTR", 449, "charge"),
            (self.user_id, order["payload"], "RUB", 449, "charge"),
            (self.user_id, order["payload"], "XTR", 899, "charge"),
            (self.user_id, order["payload"], "XTR", 449, ""),
        ):
            with self.assertRaises(billing.PaymentValidationError):
                await billing.complete_payment(user, payload, currency, amount, charge)
        self.assertEqual(await database.get_user(self.user_id), before)

    async def test_confirmed_payment_is_fulfilled_after_disable_and_expiry_of_invoice(self):
        order = await self.approved_order()
        await self.execute("UPDATE stars_orders SET expires_at = datetime('now', '-1 day')")
        with patch.object(config, "STARS_PAYMENTS_ENABLED", False), patch.object(config, "STARS_PRICE_STANDART", 460):
            result = await self.pay(order)
            self.assertTrue(result["applied"])
            with self.assertRaises(billing.PaymentValidationError):
                await billing.approve_checkout("q", self.user_id, order["payload"], "XTR", 449)

    async def test_failed_transaction_leaves_payment_retryable(self):
        order = await self.approved_order()
        before = await database.get_user(self.user_id)
        with patch.object(database, "grant_subscription_in_transaction", new=AsyncMock(side_effect=RuntimeError("offline failure"))):
            with self.assertRaises(RuntimeError):
                await self.pay(order)
        self.assertEqual(await database.get_user(self.user_id), before)
        saved = await billing.get_order(payload=order["payload"])
        self.assertEqual(saved["status"], "approved")
        self.assertIsNone(saved["telegram_payment_charge_id"])
        self.assertTrue((await self.pay(order))["applied"])

    async def test_delete_between_checkout_and_payment_restores_only_paid_access(self):
        order = await self.approved_order()
        await database.purge_user_by_pattern(str(self.user_id))
        self.assertTrue((await self.pay(order))["applied"])
        self.assertEqual((await database.get_user(self.user_id))["trial_used"], 1)
        await database.purge_user_by_pattern(str(self.user_id))
        self.assertFalse((await self.pay(order))["applied"])
        self.assertIsNone(await database.get_user(self.user_id))
        self.assertEqual(len(await billing.list_orders(self.user_id)), 1)

    async def test_latest_refund_restores_before_state_once(self):
        await database.set_user_allowed(self.user_id, 0)
        before = await database.get_user(self.user_id)
        order = await self.approved_order("pro")
        await self.pay(order)
        refund = await billing.begin_refund(order["id"], config.ADMIN_ID)
        self.assertTrue(refund["request"])
        with self.assertRaises(billing.PaymentValidationError):
            await billing.begin_refund(order["id"], config.ADMIN_ID)
        result = await billing.complete_refund("charge-1")
        self.assertFalse(result["refund_needs_review"])
        after = await database.get_user(self.user_id)
        self.assertEqual(after["subscription_until"], before["subscription_until"])
        self.assertEqual(after["is_allowed"], before["is_allowed"])
        self.assertEqual(after["tariff_plan"], before["tariff_plan"])
        self.assertFalse(await database.is_user_allowed(self.user_id))
        self.assertFalse((await billing.complete_refund("charge-1"))["applied"])
        self.assertEqual(await database.get_user(self.user_id), after)
        self.assertFalse((await self.pay(order))["applied"])

    async def test_refund_never_overwrites_later_paid_or_admin_changes(self):
        order = await self.approved_order()
        await self.pay(order)
        await database.grant_subscription(self.user_id, 7, "pro")
        before_refund = await database.get_user(self.user_id)
        result = await billing.complete_refund("charge-1")
        self.assertTrue(result["refund_needs_review"])
        self.assertEqual(await database.get_user(self.user_id), before_refund)
        with self.assertRaises(PermissionError):
            await billing.begin_refund(order["id"], self.user_id)

    async def test_migrations_are_additive_and_idempotent_with_paid_history(self):
        order = await self.approved_order()
        await self.pay(order)
        before = await database.get_user(self.user_id)
        await database.init_db()
        await database.init_db()
        self.assertEqual(await database.get_user(self.user_id), before)
        self.assertEqual((await billing.get_order(order_id=order["id"]))["status"], "paid")