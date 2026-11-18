"""Regression tests for critical security, concurrency, billing, parser, UI, and logging fixes."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests import _environment  # noqa: F401

from aiogram.fsm.storage.base import StorageKey

import billing
import config
import database
import logger_setup
import referrals
import scheduler
import ui
from anti_bot import challenge_worker
from anti_bot import manager as anti_bot_manager
from fsm_storage import SQLiteFSMStorage
from handlers import group_delivery, payments as payment_handlers
from parser import AvitoItem, AvitoParser


def _parse_sql_dt(raw: str) -> datetime:
    return datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


class AuditDBRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="avito-audit-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.tmp.name) / "audit.db")
        self.stars_patch = patch.object(config, "STARS_PAYMENTS_ENABLED", True)
        self.platega_patch = patch.object(config, "PLATEGA_ENABLED", True)
        self.db_patch.start()
        self.stars_patch.start()
        self.platega_patch.start()
        await database.init_db()
        self.user_id = 77001
        await database.get_or_create_user(self.user_id, "audit_user", "Audit")

    async def asyncTearDown(self):
        await database.close_db()
        self.platega_patch.stop()
        self.stars_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_billing_uses_pooled_connections_only(self):
        source = Path(billing.__file__).read_text(encoding="utf-8")
        self.assertNotIn("aiosqlite.connect(", source)

    async def test_successful_payment_alerts_admins_on_unexpected_db_error(self):
        payment = SimpleNamespace(
            invoice_payload="p-err",
            currency="XTR",
            total_amount=499,
            telegram_payment_charge_id="chg-err-1",
            provider_payment_charge_id="prv-err-1",
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.user_id),
            successful_payment=payment,
            answer=AsyncMock(),
        )
        bot = Mock()
        with (
            patch.object(billing, "complete_payment", new=AsyncMock(side_effect=RuntimeError("db locked"))),
            patch.object(payment_handlers, "notify_admins", new=AsyncMock()) as notify_mock,
            patch.object(payment_handlers.logger, "critical") as crit_mock,
        ):
            await payment_handlers.successful_payment(message, bot)
        crit_mock.assert_called_once()
        notify_mock.assert_awaited_once()
        message.answer.assert_awaited_once()

    async def test_cross_provider_refund_review_prevents_rollback_to_free(self):
        order = await billing.create_order(self.user_id, "pro")
        await billing.approve_checkout(
            "q-1", self.user_id, order["payload"], "XTR", order["total_amount"]
        )
        await billing.complete_payment(
            self.user_id, order["payload"], "XTR", order["total_amount"], "chg-stars-1", ""
        )
        rub = await billing.create_rub_order(self.user_id, "lite")
        async with database.writer_connection() as db:
            await db.execute("UPDATE rub_orders SET refund_needs_review = 1 WHERE id = ?", (rub["id"],))
            await db.commit()

        await billing.complete_refund("chg-stars-1")

        self.assertTrue(await database.is_user_allowed(self.user_id))
        updated_order = await billing.get_order(order_id=order["id"])
        self.assertEqual(int(updated_order["refund_needs_review"]), 1)

    async def test_trial_hours_not_carried_into_short_test_order_but_kept_for_30d_paid(self):
        await database.activate_trial(self.user_id)
        before_trial = await database.get_user(self.user_id)
        trial_until = _parse_sql_dt(before_trial["subscription_until"])

        async with database.writer_connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            short_until = await database.grant_subscription_in_transaction(
                db, self.user_id, days=1, tariff_plan="test", seconds=3600
            )
            await db.commit()
        short_dt = _parse_sql_dt(short_until)
        self.assertLess((short_dt - datetime.now(timezone.utc)).total_seconds(), 4200)
        self.assertLess(short_dt, trial_until)

        uid2 = 77002
        await database.get_or_create_user(uid2, "audit2", "Audit2")
        await database.activate_trial(uid2)
        t2_before = _parse_sql_dt((await database.get_user(uid2))["subscription_until"])
        paid_until = await database.grant_subscription(uid2, days=30, tariff_plan="pro")
        t2_after = _parse_sql_dt(paid_until)
        self.assertAlmostEqual((t2_after - t2_before).total_seconds(), timedelta(days=30).total_seconds(), delta=10)

    async def test_extend_active_subscriptions_preserves_chargeback_revocation(self):
        order = await billing.create_order(self.user_id, "pro")
        await billing.approve_checkout(
            "q-2", self.user_id, order["payload"], "XTR", order["total_amount"]
        )
        await billing.complete_payment(
            self.user_id, order["payload"], "XTR", order["total_amount"], "chg-ext-1", ""
        )
        extended = await database.extend_active_subscriptions(5)
        self.assertIn(self.user_id, [uid for uid, _ in extended])

        await billing.complete_refund("chg-ext-1")
        self.assertFalse(await database.is_user_allowed(self.user_id))

    async def test_free_trial_user_is_blocked_from_group_delivery(self):
        await database.activate_trial(self.user_id)
        self.assertFalse(await group_delivery.group_delivery_allowed(self.user_id))
        await database.set_mirror_chat(self.user_id, -1001234567890)
        msg = SimpleNamespace(message_id=99)
        bot = SimpleNamespace(copy_message=AsyncMock())
        self.assertFalse(await scheduler.mirror_to_group(bot, self.user_id, msg, None, False))
        bot.copy_message.assert_not_awaited()

    async def test_purged_user_keeps_trial_tombstone_and_cannot_bind_referral(self):
        owner_id = 77010
        await database.get_or_create_user(owner_id, "refowner", "Owner")
        link = await referrals.create_link(owner_id, config.ADMIN_ID, days=30, percent=10)

        purge_res = await database.purge_user_by_pattern(str(self.user_id))
        self.assertGreaterEqual(purge_res["deleted"], 1)

        recreated = await database.get_or_create_user(self.user_id, "audit_user", "Audit")
        self.assertEqual(int(recreated["trial_used"]), 1)

        purged2 = 77011
        await database.get_or_create_user(purged2, "purged2", "P2")
        await database.purge_user_by_pattern(str(purged2))
        self.assertIsNone(
            await referrals.bind_from_start(purged2, "purged2", "P2", f"/start ref_{link['code']}")
        )

    async def test_sqlite_fsm_storage_persists_across_instances(self):
        s1 = SQLiteFSMStorage()
        key = StorageKey(bot_id=1, chat_id=self.user_id, user_id=self.user_id)
        await s1.set_state(key, "SearchStates:waiting_for_price_range")
        await s1.set_data(key, {"search_id": 42, "step": "price"})

        s2 = SQLiteFSMStorage()
        self.assertEqual(await s2.get_state(key), "SearchStates:waiting_for_price_range")
        self.assertEqual(await s2.get_data(key), {"search_id": 42, "step": "price"})

        await s2.set_state(key, None)
        await s2.set_data(key, {})
        self.assertIsNone(await s1.get_state(key))
        self.assertEqual(await s1.get_data(key), {})


class AuditLogicRegressionTests(unittest.TestCase):
    def test_challenge_worker_returns_warming_immediately_when_thread_alive(self):
        fake_thread = SimpleNamespace(is_alive=lambda: True)
        with patch.object(challenge_worker, "_warm_thread", fake_thread):
            self.assertEqual(challenge_worker._task_geetest("cid", "ua", "chrome120"), {"_warming": True})

        fake_session = SimpleNamespace(headers={"User-Agent": "ua"})
        with (
            patch.object(anti_bot_manager.challenge_process, "enabled", return_value=True),
            patch.object(anti_bot_manager.challenge_process, "run", return_value={"_warming": True}),
            patch.object(anti_bot_manager.geetest_solve, "request_seccode") as req_mock,
        ):
            self.assertIsNone(
                anti_bot_manager._geetest_seccode(fake_session, "cid", cancel_event=None, deadline=None)
            )
            req_mock.assert_not_called()

    def test_parser_preserves_local_priority_handles_zero_maincount_and_skips_widgets(self):
        normalized = AvitoParser.normalize_url("https://www.avito.ru/moskva?q=iphone&localPriority=1&from=search")
        self.assertIn("localPriority=1", normalized)
        self.assertNotIn("from=", normalized)

        zero_payload = {
            "loaderData": {
                "data": {
                    "mainCount": 0,
                    "catalog": {
                        "items": [
                            {"id": 1001, "title": "Рекомендация", "urlPath": "/item/1001", "priceDetailed": {"value": 1000}},
                        ]
                    },
                }
            }
        }
        zero_html = f'<html><script>{{"i18n":{{}}, "loaderData": {json.dumps(zero_payload["loaderData"])}}}</script></html>'
        self.assertEqual(AvitoParser._parse_items_from_ssr_json(zero_html), [])

        mixed_payload = {
            "loaderData": {
                "data": {
                    "mainCount": 2,
                    "catalog": {
                        "items": [
                            {"id": 2001, "type": "vip", "title": "Промо", "urlPath": "/item/2001"},
                            {"id": 2002, "type": "banner", "title": "Баннер", "urlPath": "/item/2002"},
                            {"id": 2003, "type": "item", "title": "Реальный лот", "urlPath": "/item/2003", "priceDetailed": {"value": 15000}},
                            {"id": 2004, "title": "Лот без type", "urlPath": "/item/2004", "priceDetailed": {"value": 16000}},
                        ]
                    },
                }
            }
        }
        mixed_html = f'<html><script>{{"i18n":{{}}, "loaderData": {json.dumps(mixed_payload["loaderData"])}}}</script></html>'
        parsed = AvitoParser._parse_items_from_ssr_json(mixed_html)
        self.assertEqual([it.item_id for it in parsed], ["2003", "2004"])

    def test_smart_russian_stop_words(self):
        def _item(title: str, desc: str = "") -> AvitoItem:
            return AvitoItem(item_id="1", title=title, price="1000 ₽", url="https://www.avito.ru/1", description=desc)

        self.assertEqual(scheduler.is_stop_word_matched(_item("iPhone 13 бу"), ["бу"]), "бу")
        self.assertEqual(scheduler.is_stop_word_matched(_item("iPhone 13 б/у"), ["бу"]), "бу")
        self.assertIsNone(scheduler.is_stop_word_matched(_item("Ноутбук Lenovo"), ["бу"]))
        self.assertIsNone(scheduler.is_stop_word_matched(_item("Торг будет уместен"), ["бу"]))

        self.assertEqual(scheduler.is_stop_word_matched(_item("Экран", "есть сколы"), ["скол"]), "скол")
        self.assertIsNone(scheduler.is_stop_word_matched(_item("Экран", "есть несколько царапин"), ["скол"]))

    def test_drop_colour_emoji_preserves_href_attributes_and_blockquotes(self):
        raw = '🔥 <a href="https://example.com/🔥">Лот</a>\n<blockquote>Текст 🔥</blockquote>'
        cleaned = ui.drop_colour_emoji(raw)
        self.assertIn('href="https://example.com/🔥"', cleaned)
        self.assertIn("<blockquote>Текст 🔥</blockquote>", cleaned)
        self.assertFalse(cleaned.startswith("🔥"))

    def test_sanitize_logs_for_bot_handle_does_not_wipe_system_lines(self):
        with tempfile.TemporaryDirectory(prefix="avito-logscrub-") as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            log_file = logs_dir / "errors.log"
            combined = Path(tmpdir) / "bot.log"
            log_file.write_text(
                "2026-01-01 INFO bot.log started\n"
                "2026-01-01 INFO antibot solved\n"
                "2026-01-01 INFO [ID:123 @bot (User)] msg\n",
                encoding="utf-8",
            )
            combined.write_text("2026-01-01 INFO bot running\n", encoding="utf-8")
            with (
                patch.object(logger_setup, "LOGS_DIR", logs_dir),
                patch.object(logger_setup, "COMBINED_LOG", combined),
                patch.object(logger_setup, "_listener", None),
            ):
                logger_setup.sanitize_logs_for_user("@bot")
            remaining = log_file.read_text(encoding="utf-8")
            self.assertIn("bot.log started", remaining)
            self.assertIn("antibot solved", remaining)
            self.assertNotIn("@bot", remaining)
            self.assertIn("bot running", combined.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
