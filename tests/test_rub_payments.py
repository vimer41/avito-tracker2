import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import _environment  # noqa: F401  (isolated env before project imports)
from tests._dispatcher import DispatcherFixture

import aiosqlite

import billing
import config
import database
import logger_setup
import platega
import platega_webhook
import rub_payments

USER = 123456
TX = "11111111-2222-4333-8444-555555555555"
OTHER_TX = "66666666-7777-4888-9999-000000000000"
PRICE = config.rub_price("lite")


def reply(status, amount=PRICE, order_id=1, tx=TX, **extra):
    return {"id": tx, "status": status, "paymentDetails": {"amount": amount, "currency": "RUB"},
            "paidAmount": amount if status != "PENDING" else 0, "payload": f"order:{order_id}", **extra}


class FakeRequest:
    def __init__(self, body: str, merchant="merchant-1", secret="secret-key-123"):
        self.headers = {"X-MerchantId": merchant, "X-Secret": secret}
        self._body = body
        self.remote = "127.0.0.1"

    async def text(self):
        return self._body


class TestRubPayments(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-rub-")
        self.patches = [
            patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db"),
            patch.object(config, "PLATEGA_MERCHANT_ID", "merchant-1"),
            patch.object(config, "PLATEGA_SECRET", "secret-key-123"),
            patch.object(config, "PLATEGA_ENABLED", True),
            patch.object(config, "PLATEGA_CALLBACK_PORT", 0),
        ]
        for item in self.patches:
            item.start()
        await database.init_db()
        await database.get_or_create_user(USER, "buyer", "Buyer")
        await database.set_user_allowed(USER, 0)
        self.setup_dispatcher()

    async def asyncTearDown(self):
        watchers = list(rub_payments._watchers.values())
        for task in watchers:
            task.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        await self.close_dispatcher()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    async def new_order(self, plan="lite", method="sbp", tx=TX):
        order = await billing.create_rub_order(USER, plan, 1, method, username="buyer", name="Buyer Name")
        await billing.attach_rub_payment(order["id"], tx, "https://pay.platega.io/p/x", time.time() + 900, 487.17)
        return await billing.get_rub_order(order["id"])

    async def check(self, order_id, answer, reported=None):
        with patch.object(platega, "get_status", AsyncMock(return_value=answer)):
            return await rub_payments.check_order(self.bot, order_id, reported)

    def admin_messages(self):
        return [m for m in self.methods("SendMessage") if int(m.chat_id) in config.ADMIN_IDS]

    def user_texts(self):
        return [m.text or "" for m in self.methods("SendMessage") if int(m.chat_id) == USER]

    async def test_confirmed_grants_once_and_remembers_the_state_before(self):
        order = await self.new_order()
        self.assertEqual(await self.check(order["id"], reply("CONFIRMED", 487.17, order["id"])), "CONFIRMED")
        self.assertEqual(await self.check(order["id"], reply("CONFIRMED", 487.17, order["id"])), "CONFIRMED")
        row = await billing.get_rub_order(order["id"])
        user = await database.get_user(USER)
        self.assertEqual(row["status"], "paid")
        self.assertAlmostEqual(row["paid_amount"], 487.17)
        self.assertEqual((row["allowed_before"], row["tariff_after"]), (0, "lite"))
        self.assertEqual(user["subscription_until"], row["subscription_after"])
        self.assertEqual(sum(f"Оплата {PRICE} ₽ прошла" in text for text in self.user_texts()), 1)
        self.assertEqual(len([m for m in self.admin_messages() if "💰" in m.text]), len(config.ADMIN_IDS))
        self.assertIsNotNone(row["next_check_at"], "paid orders are re-checked for refunds and chargebacks")

    async def test_short_or_foreign_payment_is_held_for_review_with_one_alert(self):
        order = await self.new_order()
        for _ in range(2):
            self.assertEqual(await self.check(order["id"], reply("CONFIRMED", 100, order["id"])), "MISMATCH")
        row = await billing.get_rub_order(order["id"])
        self.assertEqual((row["status"], row["paid_at"]), ("review", None))
        self.assertFalse((await database.get_user(USER))["is_allowed"])
        self.assertEqual(len(self.admin_messages()), len(config.ADMIN_IDS))
        self.assertIsNone(row["next_check_at"])

        other = await self.new_order(plan="pro", tx=OTHER_TX)
        foreign = reply("CONFIRMED", 899, order_id=999, tx=OTHER_TX)
        self.assertEqual(await self.check(other["id"], foreign), "MISMATCH")
        self.assertIsNone((await billing.get_rub_order(other["id"]))["paid_at"])

    async def test_dispute_close_and_chargeback(self):
        order = await self.new_order()
        await self.check(order["id"], reply("CONFIRMED", PRICE, order["id"]))
        granted = (await database.get_user(USER))["subscription_until"]

        # The webhook reports a dispute while the status request still answers CONFIRMED.
        confirmed = reply("CONFIRMED", PRICE, order["id"])
        self.assertEqual(await self.check(order["id"], confirmed, {"status": "DISPUTED"}), "DISPUTED")
        self.assertEqual(await self.check(order["id"], confirmed, {"status": "DISPUTED"}), "DISPUTED")
        disputes = [m for m in self.admin_messages() if "Спор по платежу" in m.text]
        self.assertEqual(len(disputes), len(config.ADMIN_IDS), "one alert per admin, not per delivery")
        buttons = [b.callback_data for row in disputes[0].reply_markup.inline_keyboard for b in row]
        self.assertIn(f"admin_rubproof:{order['id']}", buttons)
        self.assertEqual((await billing.get_rub_order(order["id"]))["status"], "disputed")

        # Dispute closed in our favour: CONFIRMED again must not add days.
        self.assertEqual(await self.check(order["id"], confirmed), "CONFIRMED")
        self.assertEqual((await billing.get_rub_order(order["id"]))["status"], "paid")
        self.assertEqual((await database.get_user(USER))["subscription_until"], granted)

        self.assertEqual(await self.check(order["id"], reply("CHARGEBACKED", PRICE, order["id"])), "CHARGEBACKED")
        row = await billing.get_rub_order(order["id"])
        user = await database.get_user(USER)
        self.assertEqual((row["status"], row["refund_needs_review"]), ("refunded", 0))
        self.assertEqual((user["is_allowed"], user["subscription_until"]), (0, None))
        self.assertTrue(any("возвращён" in text for text in self.user_texts()))
        self.assertIsNone(row["next_check_at"])

    async def test_chargeback_after_a_later_change_is_flagged_not_overwritten(self):
        order = await self.new_order()
        await self.check(order["id"], reply("CONFIRMED", PRICE, order["id"]))
        await database.grant_subscription(USER, 5, "pro")
        changed = await database.get_user(USER)
        await self.check(order["id"], reply("CHARGEBACKED", PRICE, order["id"]))
        row = await billing.get_rub_order(order["id"])
        self.assertEqual((row["status"], row["refund_needs_review"]), ("refunded", 1))
        self.assertEqual((await database.get_user(USER))["subscription_until"], changed["subscription_until"])
        self.assertTrue(any("сверьте срок" in m.text for m in self.admin_messages()))

    async def test_declined_refund_changes_nothing(self):
        order = await self.new_order()
        await self.check(order["id"], reply("CONFIRMED", PRICE, order["id"]))
        await self.check(order["id"], reply("CHARGEBACKED", PRICE, order["id"], refundStatus="Declined"))
        self.assertEqual((await billing.get_rub_order(order["id"]))["status"], "paid")
        self.assertTrue((await database.get_user(USER))["is_allowed"])

    async def test_canceled_payment_confirmed_later_is_still_granted(self):
        order = await self.new_order()
        self.assertEqual(await self.check(order["id"], reply("CANCELED", PRICE, order["id"])), "CANCELED")
        self.assertEqual((await billing.get_rub_order(order["id"]))["status"], "canceled")
        self.assertEqual(await self.check(order["id"], reply("CONFIRMED", PRICE, order["id"])), "CONFIRMED")
        self.assertEqual((await billing.get_rub_order(order["id"]))["status"], "paid")
        self.assertTrue((await database.get_user(USER))["is_allowed"])

    async def test_same_purchase_reuses_the_link_and_new_links_are_capped(self):
        first = await self.new_order()
        again = await billing.create_rub_order(USER, "lite", 1, "sbp")
        self.assertEqual((again["id"], again["reused"]), (first["id"], True))
        self.assertFalse((await billing.create_rub_order(USER, "lite", 1, "card"))["reused"])
        for _ in range(billing.RUB_ORDERS_PER_HOUR - 2):
            await billing.create_rub_order(USER, "pro", 1, "sbp")
        with self.assertRaises(billing.PaymentValidationError):
            await billing.create_rub_order(USER, "standart", 1, "sbp")

    async def test_buttons_open_the_chosen_method_and_show_the_fee(self):
        created = AsyncMock(return_value={"transactionId": TX, "redirect": "https://pay.platega.io/p/abc",
                                          "status": "PENDING", "expiresIn": "00:30:00"})
        status = AsyncMock(return_value=reply("PENDING", 380.41, 1))
        with patch.object(platega, "create_payment", created), patch.object(platega, "get_status", status):
            await self.dispatch_callback("pay_method:card:lite:1")
            await self.dispatch_callback("pay_method:card:lite:1")
        self.assertEqual(created.await_count, 1, "the second tap reuses the open payment")
        self.assertEqual(created.await_args.kwargs["method"], "card")
        invoices = [text for text in self.user_texts() if f"Счёт на {PRICE} ₽" in text]
        self.assertEqual(len(invoices), 2)
        self.assertIn("К оплате: <b>380,41 ₽</b>", invoices[0])
        self.assertIn("Ссылка действует 30 мин.", invoices[0])
        order = await billing.get_rub_order(1)
        self.assertEqual((order["payment_method"], order["buyer_name"], order["status"]), ("card", "Fixture", "pending"))

    async def test_hidden_one_ruble_test_gives_one_hour_of_full(self):
        await self.dispatch_message(text=f"/{config.RUB_TEST_COMMAND}")
        self.assertIn("Тест оплаты — 1 ₽", self.user_texts()[-1])
        self.assertIn(f"pay_method:sbp:{config.RUB_TEST_PLAN}:1", self.callback_values())
        created = AsyncMock(return_value={"transactionId": TX, "redirect": "https://pay.platega.io/p/abc",
                                          "status": "PENDING", "expiresIn": "00:30:00"})
        with patch.object(platega, "create_payment", created), \
                patch.object(platega, "get_status", AsyncMock(return_value=reply("PENDING", 1.09, 1))):
            await self.dispatch_callback(f"pay_method:sbp:{config.RUB_TEST_PLAN}:1")
        self.assertEqual(created.await_args.kwargs["amount"], 1)
        self.assertEqual(await self.check(1, reply("CONFIRMED", 1.09, 1)), "CONFIRMED")
        user = await database.get_user(USER)
        self.assertEqual((user["tariff_plan"], user["is_allowed"]), ("pro", 1))
        left = database.subscription_seconds_left(user)
        self.assertTrue(3500 < left <= 3600, left)
        self.assertTrue(any("+1 час" in text and "«Полный»" in text for text in self.user_texts()))

    async def test_one_ruble_test_does_not_touch_other_paid_tariffs(self):
        await database.grant_subscription(USER, 20, "lite")
        with self.assertRaises(billing.PaymentValidationError):
            await billing.create_rub_order(USER, config.RUB_TEST_PLAN, 1, "sbp")

    async def paid_order(self):
        order = await self.new_order()
        self.assertEqual(await self.check(order["id"], reply("CONFIRMED", 487.17, order["id"])), "CONFIRMED")
        self.assertTrue(await database.is_user_allowed(USER))
        return order

    def admin_screen(self):
        return self.methods("EditMessageText")[-1].text

    async def test_admin_refund_asks_first_then_refunds_and_takes_access_back(self):
        order = await self.paid_order()
        await self.dispatch_callback(f"admin_payments:{USER}", config.ADMIN_ID)
        self.assertIn(f"admin_rubrefund:{order['id']}", self.callback_values())

        quote = AsyncMock(return_value={"supported": True, "totalDeductUsdt": 5.6, "penaltyNativeAmount": 50,
                                        "penaltyNativeCurrency": "RUB", "blockReason": None})
        refund = AsyncMock(return_value={"transactionId": TX, "accepted": True, "manualControlRequired": False,
                                         "message": None, "refundOperationId": "op-1"})
        with patch.object(platega, "refund_check", quote), patch.object(platega, "refund", refund):
            await self.dispatch_callback(f"admin_rubrefund:{order['id']}", config.ADMIN_ID)
            screen = self.admin_screen()
            self.assertIn("Точно вернуть деньги?", screen)
            self.assertIn("487,17 ₽", screen)
            self.assertIn("Комиссия провайдера за возврат: <b>50 ₽</b>", screen)
            self.assertIn("5.6 USDT", screen)
            self.assertIn("отзовётся сразу", screen)
            self.assertEqual(refund.await_count, 0, "the first tap only asks")
            self.assertTrue(await database.is_user_allowed(USER))
            self.assertIn(f"admin_rubrefund_yes:{order['id']}", self.callback_values())
            self.assertIn(f"admin_payments:{USER}", self.callback_values())

            await self.dispatch_callback(f"admin_rubrefund_yes:{order['id']}", config.ADMIN_ID)
            await self.dispatch_callback(f"admin_rubrefund_yes:{order['id']}", config.ADMIN_ID)
        self.assertEqual(refund.await_count, 1, "a second tap must not refund twice")
        self.assertIn("уже возвращены", self.admin_screen())
        row = await billing.get_rub_order(order["id"])
        self.assertEqual((row["status"], row["refund_operation_id"], row["refund_admin_id"]), ("refunded", "op-1", config.ADMIN_ID))
        self.assertIsNone(row["refund_result"])
        self.assertIsNotNone(row["next_check_at"], "the bot waits for Platega to finish the refund")
        self.assertFalse(await database.is_user_allowed(USER), "the access this order paid for is gone at once")
        self.assertTrue(any(f"Возврат по заказу <code>R{order['id']}</code> оформлен" in t for t in self.user_texts()))
        self.assertTrue(any(a["action"] == "refund" for a in await database.get_admin_actions(USER)))

        self.assertEqual(await self.check(order["id"], reply("CHARGEBACKED", 487.17, order["id"])), "CHARGEBACKED")
        row = await billing.get_rub_order(order["id"])
        self.assertEqual(row["refund_result"], "done")
        self.assertIsNone(row["next_check_at"])
        self.assertTrue(any("Platega подтвердила возврат" in m.text for m in self.admin_messages()))

    async def test_refund_platega_did_not_confirm_in_three_days_is_flagged_once(self):
        order = await self.paid_order()
        with patch.object(platega, "refund", AsyncMock(return_value={"accepted": True, "manualControlRequired": False})):
            result = await rub_payments.refund_order(self.bot, order["id"], config.ADMIN_ID)
        self.assertTrue(result["ok"])
        self.assertEqual(await self.check(order["id"], reply("CONFIRMED", 487.17, order["id"])), "CONFIRMED")
        self.assertIsNone((await billing.get_rub_order(order["id"]))["refund_result"], "still waiting")
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute("UPDATE rub_orders SET refund_requested_at = datetime('now', '-4 days') WHERE id = ?", (order["id"],))
            await db.commit()
        await self.check(order["id"], reply("CONFIRMED", 487.17, order["id"]))
        await self.check(order["id"], reply("CONFIRMED", 487.17, order["id"]))
        self.assertEqual((await billing.get_rub_order(order["id"]))["refund_result"], "overdue")
        self.assertEqual(sum("не подтвердила возврат" in m.text for m in self.admin_messages()), len(config.ADMIN_IDS))

    async def test_refund_is_refused_without_balance_or_during_a_dispute(self):
        order = await self.paid_order()
        blocked = AsyncMock(return_value={"supported": False, "blockReason": "INSUFFICIENT_FUNDS"})
        refund = AsyncMock()
        with patch.object(platega, "refund_check", blocked), patch.object(platega, "refund", refund):
            await self.dispatch_callback(f"admin_rubrefund:{order['id']}", config.ADMIN_ID)
        self.assertIn("не хватает денег", self.admin_screen())
        self.assertFalse(any(v.startswith("admin_rubrefund_yes:") for v in self.callback_values()))

        await self.check(order["id"], reply("CONFIRMED", 487.17, order["id"]), {"status": "DISPUTED"})
        await self.dispatch_callback(f"admin_payments:{USER}", config.ADMIN_ID)
        self.assertNotIn(f"admin_rubrefund:{order['id']}", self.callback_values(), "no refund button during a dispute")
        with patch.object(platega, "refund", refund):
            await self.dispatch_callback(f"admin_rubrefund_yes:{order['id']}", config.ADMIN_ID)
        self.assertIn("открыт спор", self.admin_screen())
        refund.assert_not_awaited()
        self.assertTrue(await database.is_user_allowed(USER))

    async def test_failed_kassa_answer_creates_no_payment_link(self):
        with patch.object(platega, "create_payment", AsyncMock(side_effect=platega.PlategaError("HTTP 500"))):
            await self.dispatch_callback("pay_method:sbp:lite:1")
        self.assertEqual((await billing.get_rub_order(1))["status"], "failed")
        self.assertTrue(any("Касса не ответила" in (m.text or "") for m in self.methods("SendMessage")))

    async def test_webhook_checks_keys_and_trusts_only_platega_status(self):
        handler = next(route.handler for route in platega_webhook.make_app(self.bot).router.routes()
                       if route.method == "POST")
        body = json.dumps({"id": TX, "status": "CONFIRMED", "amount": 1})
        self.assertEqual((await handler(FakeRequest(body, secret="wrong"))).status, 401)
        self.assertEqual((await handler(FakeRequest(body, merchant="other"))).status, 401)
        self.assertEqual((await handler(FakeRequest("not json"))).status, 400)
        self.assertEqual((await handler(FakeRequest(json.dumps({"id": "../../etc"})))).status, 400)

        status = AsyncMock(return_value=reply("PENDING", PRICE, 1))
        with patch.object(platega, "get_status", status):
            self.assertEqual((await handler(FakeRequest(body))).status, 200, "unknown payment: accepted and ignored")
            status.assert_not_awaited()
            order = await self.new_order()
            self.assertEqual((await handler(FakeRequest(body))).status, 200)
            self.assertIsNone((await billing.get_rub_order(order["id"]))["paid_at"], "a CONFIRMED body alone grants nothing")
            status.return_value = reply("CONFIRMED", PRICE, order["id"])
            self.assertEqual((await handler(FakeRequest(body))).status, 200)
        self.assertEqual((await billing.get_rub_order(order["id"]))["status"], "paid")

        with patch.object(platega, "get_status", AsyncMock(side_effect=platega.PlategaError("down"))):
            self.assertEqual((await handler(FakeRequest(body))).status, 503, "Platega retries later")

    def test_schedule_of_status_checks(self):
        now = 1_800_000_000.0
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 10))
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 200 * 86400))
        nxt = rub_payments.next_check_at
        self.assertEqual(nxt({"status": "pending", "expires_at": now + 900, "created_at": stamp}, now), now + 900 + 300)
        self.assertEqual(nxt({"status": "expired", "created_at": stamp}, now), now - 10 + 1800)
        self.assertEqual(nxt({"status": "paid", "paid_at": stamp}, now), now - 10 + 3600)
        self.assertIsNone(nxt({"status": "paid", "paid_at": old}, now))
        self.assertEqual(nxt({"status": "disputed", "disputed_at": stamp}, now), now + 6 * 3600)
        self.assertIsNone(nxt({"status": "refunded"}, now))
        self.assertIsNone(nxt({"status": "review"}, now))
        self.assertEqual(rub_payments.link_seconds("00:30:00"), 1800)
        self.assertEqual(rub_payments.link_seconds("junk"), rub_payments.LINK_SECONDS_DEFAULT)

    async def test_sweep_finds_a_chargeback_without_the_webhook(self):
        order = await self.new_order()
        await self.check(order["id"], reply("CONFIRMED", PRICE, order["id"]))
        await billing.schedule_rub_check(order["id"], time.time() - 1)
        with patch.object(platega, "get_status", AsyncMock(return_value=reply("CHARGEBACKED", PRICE, order["id"]))), \
                patch.object(rub_payments.asyncio, "sleep", AsyncMock()):
            self.assertEqual(await rub_payments.sweep_once(self.bot), 1)
        self.assertEqual((await billing.get_rub_order(order["id"]))["status"], "refunded")

    async def test_usage_outlives_the_event_log(self):
        now = time.time()
        async with aiosqlite.connect(database.DB_PATH) as db:
            for index, sent_at in enumerate((now - 2 * 86400, now - 2 * 86400 + 60, now - 2 * 86400 + 120, now - 60)):
                await db.execute("""
                    INSERT INTO notification_events (user_id, item_id, status, queued_at, deliver_at, next_attempt_at,
                                                     item_json, t_seen, sent_at)
                    VALUES (?, ?, 'sent', ?, ?, ?, '{}', ?, ?)
                """, (USER, f"item-{index}", sent_at, sent_at, sent_at, sent_at, sent_at))
            await db.commit()
        await database.cleanup_terminal_notification_events(24, now_epoch=now)
        await database.touch_user(USER)
        usage = await database.get_usage_by_day(USER, "2000-01-01")
        self.assertEqual(sum(lots for _day, lots, _opens in usage), 4)
        self.assertEqual(usage[0][1], 3, "archived day keeps its three lots")
        self.assertEqual(sum(opens for _day, _lots, opens in usage), 1)

    async def test_evidence_file_names_the_buyer_the_service_and_its_use(self):
        order = await self.new_order()
        await self.check(order["id"], reply("CONFIRMED", 487.17, order["id"]))
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute("""
                INSERT INTO radar_history (user_id, search_id, at, action, title, url)
                VALUES (?, 7, datetime('now', '+1 minute'), 'create', 'iphone', 'https://www.avito.ru/moskva?q=iphone')
            """, (USER,))
            await db.commit()
        name, data = await rub_payments.build_evidence(order["id"], "radar_bot")
        text = data.decode("utf-8")
        self.assertEqual(name, f"proof-R{order['id']}.txt")
        for part in (f"R{order['id']}", TX, f"Telegram ID: {USER}", "@buyer", "«Базовый»", "487,17",
                     "IP-адрес", "радар #7 «iphone»", "@radar_bot"):
            self.assertIn(part, text)
        await self.dispatch_callback(f"admin_rubproof:{order['id']}", user_id=config.ADMIN_IDS[0])
        self.assertEqual(len(self.methods("SendDocument")), 1)

    async def test_rubles_count_in_money_activity_and_filters(self):
        order = await self.new_order()
        await self.check(order["id"], reply("CONFIRMED", PRICE, order["id"]))
        stats = await database.get_money_stats()
        self.assertEqual(stats["revenue_rub"]["day"], (1, PRICE, 1))
        self.assertEqual(stats["funnel"]["paid"], 1)
        self.assertIn(("lite", "rub", 1, PRICE), stats["by_plan"])
        self.assertIn(USER, await database.get_paid_user_ids())
        self.assertIn(USER, await database.get_ever_subscribed_user_ids())
        self.assertEqual(await database.count_paid_orders(USER), 1)
        self.assertEqual((await database.get_user_activity(USER))["rub_total"], PRICE)
        self.assertEqual((await database.get_daily_report_stats(24))["payments_rub"], [("lite", 1, PRICE)])

    def test_secret_is_masked_in_logs(self):
        logger_setup.register_secret("secret-key-123")
        try:
            self.assertEqual(logger_setup._redact("headers secret-key-123 sent"), "headers <secret> sent")
        finally:
            logger_setup._SECRETS.discard("secret-key-123")


if __name__ == "__main__":
    unittest.main()
