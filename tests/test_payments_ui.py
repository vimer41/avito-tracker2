import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock

from tests import _environment
from tests._dispatcher import DispatcherFixture
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import RefundStarPayment, SendMessage
from aiogram.types import PreCheckoutQuery, RefundedPayment, SuccessfulPayment, Update, User

import billing
import config
import database
import legal_docs
import ui


class TestPaymentsUI(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-payments-ui-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        self.enabled = patch.object(config, "STARS_PAYMENTS_ENABLED", True)
        self.enabled.start()
        await database.init_db()
        self.user_id = 123456
        await database.get_or_create_user(self.user_id)
        await database.set_user_allowed(self.user_id, 0)
        self.setup_dispatcher()

    async def asyncTearDown(self):
        await self.close_dispatcher()
        self.enabled.stop()
        self.db_patch.stop()
        self.directory.cleanup()

    async def checkout(self, order, **overrides):
        self.update_id += 1
        values = dict(id=f"q-{order['id']}", from_user=User(id=self.user_id, is_bot=False, first_name="Fixture"),
                      currency="XTR", total_amount=order["total_amount"], invoice_payload=order["payload"])
        values.update(overrides)
        query = PreCheckoutQuery(**values)
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, pre_checkout_query=query))

    def successful(self, order, charge="fixture-charge"):
        return SuccessfulPayment(currency="XTR", total_amount=order["total_amount"], invoice_payload=order["payload"],
                                 telegram_payment_charge_id=charge, provider_payment_charge_id="")

    async def paid_order(self):
        order = await billing.create_order(self.user_id, "standart")
        await self.checkout(order)
        await self.dispatch_message(successful_payment=self.successful(order))
        return await billing.get_order(order_id=order["id"])

    async def test_prices_only_stars_and_disabled_screen_has_no_checkout_button(self):
        with patch.object(config, "STARS_PAYMENTS_ENABLED", False):
            await self.dispatch_message(text="/tariffs")
            text = self.methods("SendMessage")[-1].text
            self.assertIn("449 ₽", text)
            self.assertIn("899 ₽", text)
            await self.dispatch_callback("buy_tariff:pro")
            self.assertFalse(any(v and v.startswith("stars_buy:") for v in self.callback_values()))
            await self.dispatch_callback("stars_buy:pro:899")
            self.assertEqual(self.methods("SendInvoice"), [])
            self.assertIn("выключен", self.methods("AnswerCallbackQuery")[-1].text)

    async def test_invoice_is_one_xtr_price_without_provider_or_autobilling(self):
        before = await database.get_user(self.user_id)
        await self.dispatch_callback("buy_tariff:pro")
        self.assertIn("stars_buy:pro:1:899", self.callback_values())
        await self.dispatch_callback("stars_buy:pro:899")
        invoice = self.methods("SendInvoice")[-1]
        self.assertEqual(invoice.currency, "XTR")
        self.assertEqual(invoice.provider_token, "")
        self.assertEqual(invoice.chat_id, self.user_id)
        self.assertEqual([p.amount for p in invoice.prices], [899])
        self.assertLessEqual(len(invoice.payload.encode()), 128)
        self.assertTrue(invoice.start_parameter)
        self.assertNotIn("subscription_period", invoice.model_dump())
        self.assertEqual(await database.get_user(self.user_id), before)

    async def test_three_months_is_hidden_from_sale_for_now(self):
        await self.dispatch_callback("buy_tariff:pro:3")
        labels = [b.text for row in self.last_markup().inline_keyboard for b in row]
        self.assertFalse(any("месяц" in label for label in labels))
        self.assertIn("stars_buy:pro:1:899", self.callback_values())
        await self.dispatch_callback("stars_buy:pro:3:2399")
        self.assertEqual(self.methods("SendInvoice"), [])

    @patch.object(config, "SALE_PERIOD_MONTHS", (1, 3))
    async def test_three_months_is_ten_percent_cheaper_and_gives_90_days(self):
        await self.dispatch_callback("buy_tariff:pro:3")
        text = self.methods("EditMessageText")[-1].text
        self.assertIn("2399 ₽", text)
        labels = [b.text for row in self.last_markup().inline_keyboard for b in row]
        self.assertIn("✓ 3 месяца −10%", labels)
        self.assertIn("1 месяц", labels)
        self.assertIn("stars_buy:pro:3:2399", self.callback_values())
        await self.dispatch_callback("stars_buy:pro:3:899")
        self.assertEqual(self.methods("SendInvoice"), [])
        await self.dispatch_callback("stars_buy:pro:3:2399")
        invoice = self.methods("SendInvoice")[-1]
        self.assertEqual([p.amount for p in invoice.prices], [2399])
        order = await billing.get_order(payload=invoice.payload)
        self.assertEqual((order["total_amount"], order["days"]), (2399, 90))
        await billing.approve_checkout("q3", self.user_id, invoice.payload, "XTR", 2399)

    @patch.object(config, "STARS_TEST_ENABLED", True)
    async def test_test_tariff_ui_and_invoice(self):
        await self.dispatch_callback("buy_tariff:test")
        self.assertIn("stars_buy:test:1:10", self.callback_values())
        await self.dispatch_callback("stars_buy:test:10")
        invoice = self.methods("SendInvoice")[-1]
        self.assertEqual(invoice.currency, "XTR")
        self.assertEqual([p.amount for p in invoice.prices], [10])
        self.assertIn("ТЕСТ", invoice.title)

    async def test_stale_price_foreign_chat_and_invalid_plan_never_send_invoice(self):
        for value, chat_id in (("stars_buy:pro:1", self.user_id), ("stars_buy:fake:449", self.user_id),
                               ("stars_buy:pro", self.user_id), ("stars_buy:pro:899", 654321)):
            await self.dispatch_callback(value, chat_id=chat_id)
            self.assertTrue(self.methods("AnswerCallbackQuery")[-1].show_alert)
        self.assertEqual(self.methods("SendInvoice"), [])

    async def test_legacy_payment_buttons_show_stars_not_card_request(self):
        for value in ("demo_tariff:pro", "pay_request:standart"):
            await self.dispatch_callback(value)
            self.assertIn("stars_buy:", " ".join(v or "" for v in self.callback_values()))
            text = self.methods("EditMessageText")[-1].text
            self.assertNotIn("реквизиты", text)
        self.assertEqual(self.methods("SendInvoice"), [])
        self.assertEqual(self.methods("SendMessage"), [])

    async def test_tariff_list_has_three_plans_and_old_other_methods_button_opens_it(self):
        await self.dispatch_callback("open_subscription_menu")
        values = self.callback_values()
        for plan in ("lite", "standart", "pro"):
            self.assertIn(f"buy_tariff:{plan}", values)
        self.assertNotIn("other_payment_methods", values)
        text = self.methods("EditMessageText")[-1].text
        for title in ("Базовый", "Средний", "Полный", "199 ₽", "449 ₽", "899 ₽"):
            self.assertIn(title, text)
        await self.dispatch_callback("buy_tariff:standart")
        self.assertNotIn("other_payment_methods", self.callback_values())
        await self.dispatch_callback("other_payment_methods")
        self.assertIn("buy_tariff:pro", self.callback_values())

    async def platega_order(self, status_reply):
        import platega
        import rub_payments
        created = AsyncMock(return_value={"transactionId": "tx-1", "url": "https://pay.platega.io/?id=tx-1", "status": "PENDING"})
        with patch.object(config, "PLATEGA_ENABLED", True), patch.object(platega, "create_payment", created), \
                patch.object(platega, "get_status", AsyncMock(return_value=status_reply)):
            await self.dispatch_callback("pay_method:sbp:lite:1")
            watchers = list(rub_payments._watchers.values())
            for task in watchers:
                task.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)
            kwargs = created.await_args.kwargs
            self.assertEqual((kwargs["amount"], kwargs["user_id"]), (199, self.user_id))
            await self.dispatch_callback(f"rub_check:{kwargs['order_id']}")
            await self.dispatch_callback(f"rub_check:{kwargs['order_id']}")
        return await billing.get_rub_order(kwargs["order_id"])

    async def test_platega_confirmed_grants_tariff_once(self):
        order = await self.platega_order({"id": "tx-1", "status": "CONFIRMED", "paymentDetails": {"amount": 199, "currency": "RUB"}})
        self.assertEqual(order["status"], "paid")
        self.assertEqual((await database.get_user(self.user_id))["tariff_plan"], "lite")
        paid = [m for m in self.methods("SendMessage") if "Оплата 199 ₽ прошла" in (m.text or "")]
        self.assertEqual(len(paid), 1)

    async def test_platega_canceled_or_short_payment_grants_nothing(self):
        order = await self.platega_order({"id": "tx-1", "status": "CANCELED"})
        self.assertEqual(order["status"], "canceled")
        self.assertFalse((await database.get_user(self.user_id))["is_allowed"])

    async def test_tariff_card_offers_sbp_stars_and_crypto_but_only_stars_charges(self):
        await self.dispatch_callback("buy_tariff:lite")
        values = self.callback_values()
        for value in ("pay_method:sbp:lite:1", "pay_method:card:lite:1", "stars_buy:lite:1:199", "pay_method:crypto:lite:1"):
            self.assertIn(value, values)
        self.assertIn("199 ₽", self.methods("EditMessageText")[-1].text)
        for method in ("sbp", "crypto"):
            await self.dispatch_callback(f"pay_method:{method}:lite")
            answer = self.methods("AnswerCallbackQuery")[-1]
            self.assertTrue(answer.show_alert)
            self.assertIn("скоро", answer.text)
        self.assertEqual(self.methods("SendInvoice"), [])

    async def test_precheckout_can_reject_invalid_amount_then_approve_without_access(self):
        order = await billing.create_order(self.user_id, "standart")
        await self.checkout(order, total_amount=1)
        self.assertFalse(self.methods("AnswerPreCheckoutQuery")[-1].ok)
        await self.checkout(order)
        self.assertTrue(self.methods("AnswerPreCheckoutQuery")[-1].ok)
        self.assertFalse(await database.is_user_allowed(self.user_id))

    async def test_successful_payment_bypasses_expired_access_and_all_fsm_states(self):
        order = await billing.create_order(self.user_id, "standart")
        await self.checkout(order)
        state = self.dp.fsm.get_context(bot=self.bot, chat_id=self.user_id, user_id=self.user_id)
        await state.set_state("SupportStates:waiting_for_user_message")
        await self.dispatch_message(successful_payment=self.successful(order))
        self.assertTrue(await database.is_user_allowed(self.user_id))
        self.assertEqual(await database.get_user_max_radars(self.user_id), 2)
        after = await database.get_user(self.user_id)
        await self.dispatch_message(successful_payment=self.successful(order))
        self.assertEqual(await database.get_user(self.user_id), after)

    async def test_payment_ack_failure_does_not_repeat_grant(self):
        order = await billing.create_order(self.user_id, "standart")
        await self.checkout(order)
        self.session.failures["SendMessage"] = TelegramNetworkError(method=SendMessage(chat_id=self.user_id, text="fixture"), message="offline")
        with self.assertRaises(TelegramNetworkError):
            await self.dispatch_message(successful_payment=self.successful(order))
        after = await database.get_user(self.user_id)
        self.session.failures.clear()
        await self.dispatch_message(successful_payment=self.successful(order))
        self.assertEqual(await database.get_user(self.user_id), after)

    async def test_payment_support_is_public_and_creates_a_support_ticket(self):
        await self.dispatch_message(text="/paysupport@fixture_bot")
        state = self.dp.fsm.get_context(bot=self.bot, chat_id=self.user_id, user_id=self.user_id)
        self.assertEqual(await state.get_state(), "SupportStates:waiting_for_user_message")
        self.assertIn("Оплата и возврат", self.methods("SendMessage")[-1].text)
        await self.dispatch_message(text="Не пришла подписка, заказ 42")
        admin_notices = [m for m in self.methods("SendMessage") if m.chat_id in config.ADMIN_IDS]
        self.assertEqual(len(admin_notices), len(config.ADMIN_IDS))
        self.assertTrue(all("ОБРАЩЕНИЕ ПО ОПЛАТЕ" in m.text for m in admin_notices))

    async def test_refund_ui_calls_telegram_once_and_revokes_granted_access(self):
        order = await self.paid_order()
        await self.dispatch_callback(f"admin_payments:{self.user_id}", config.ADMIN_ID)
        self.assertIn(f"admin_refund_prompt:{order['id']}", self.callback_values())
        await self.dispatch_callback(f"admin_refund_prompt:{order['id']}", config.ADMIN_ID)
        self.assertEqual(self.methods("RefundStarPayment"), [])
        await self.dispatch_callback(f"admin_refund:{order['id']}", config.ADMIN_ID)
        self.assertEqual(self.methods("RefundStarPayment")[-1].telegram_payment_charge_id, "fixture-charge")
        self.assertFalse(await database.is_user_allowed(self.user_id))
        await self.dispatch_callback(f"admin_refund:{order['id']}", config.ADMIN_ID)
        self.assertEqual(len(self.methods("RefundStarPayment")), 1)

    async def test_definite_refund_failure_preserves_subscription_and_allows_retry(self):
        order = await self.paid_order()
        self.session.failures["RefundStarPayment"] = TelegramBadRequest(method=RefundStarPayment(user_id=self.user_id, telegram_payment_charge_id="fixture-charge"), message="BALANCE_TOO_LOW")
        before = await database.get_user(self.user_id)
        await self.dispatch_callback(f"admin_refund:{order['id']}", config.ADMIN_ID)
        self.assertEqual(await database.get_user(self.user_id), before)
        self.assertEqual((await billing.get_order(order_id=order["id"]))["status"], "paid")
        self.session.failures.clear()
        await self.dispatch_callback(f"admin_refund:{order['id']}", config.ADMIN_ID)
        self.assertEqual((await billing.get_order(order_id=order["id"]))["status"], "refunded")

    async def test_already_refunded_remote_charge_is_reconciled_without_second_grant(self):
        order = await self.paid_order()
        self.session.failures["RefundStarPayment"] = TelegramBadRequest(method=RefundStarPayment(user_id=self.user_id, telegram_payment_charge_id="fixture-charge"), message="CHARGE_ALREADY_REFUNDED")
        await self.dispatch_callback(f"admin_refund:{order['id']}", config.ADMIN_ID)
        self.assertEqual((await billing.get_order(order_id=order["id"]))["status"], "refunded")
        self.assertFalse(await database.is_user_allowed(self.user_id))

    async def test_refunded_message_is_handled_even_when_payments_disabled(self):
        order = await self.paid_order()
        refund = RefundedPayment(currency="XTR", total_amount=449, invoice_payload=order["payload"], telegram_payment_charge_id="fixture-charge")
        with patch.object(config, "STARS_PAYMENTS_ENABLED", False):
            await self.dispatch_message(refunded_payment=refund)
            self.assertFalse(await database.is_user_allowed(self.user_id))
            after = await database.get_user(self.user_id)
            await self.dispatch_message(refunded_payment=refund)
            self.assertEqual(await database.get_user(self.user_id), after)

    async def test_checkout_timeout_rejects_without_granting(self):
        order = await billing.create_order(self.user_id, "standart")
        with patch.object(billing, "approve_checkout", new=AsyncMock(side_effect=TimeoutError)):
            await self.checkout(order)
        self.assertFalse(self.methods("AnswerPreCheckoutQuery")[-1].ok)
        self.assertFalse(await database.is_user_allowed(self.user_id))

    async def test_ambiguous_refund_keeps_lease_and_can_be_completed_by_event(self):
        order = await self.paid_order()
        self.session.failures["RefundStarPayment"] = TelegramNetworkError(method=RefundStarPayment(user_id=self.user_id, telegram_payment_charge_id="fixture-charge"), message="offline")
        before = await database.get_user(self.user_id)
        await self.dispatch_callback(f"admin_refund:{order['id']}", config.ADMIN_ID)
        await self.dispatch_callback(f"admin_refund:{order['id']}", config.ADMIN_ID)
        self.assertEqual(len(self.methods("RefundStarPayment")), 1)
        self.assertEqual(await database.get_user(self.user_id), before)
        self.assertEqual((await billing.get_order(order_id=order["id"]))["status"], "refunding")
        self.session.failures.clear()
        refund = RefundedPayment(currency="XTR", total_amount=449, invoice_payload=order["payload"], telegram_payment_charge_id="fixture-charge")
        await self.dispatch_message(refunded_payment=refund)
        self.assertFalse(await database.is_user_allowed(self.user_id))

    async def test_foreign_or_wrong_refund_event_does_not_change_paid_access(self):
        order = await self.paid_order()
        before = await database.get_user(self.user_id)
        refund = RefundedPayment(currency="XTR", total_amount=1, invoice_payload=order["payload"], telegram_payment_charge_id="fixture-charge")
        await self.dispatch_message(refunded_payment=refund)
        self.assertEqual(await database.get_user(self.user_id), before)
        self.assertEqual((await billing.get_order(order_id=order["id"]))["status"], "paid")

    async def test_invoice_network_failure_cannot_grant_access(self):
        self.session.failures["SendInvoice"] = TelegramNetworkError(method=SendMessage(chat_id=self.user_id, text="fixture"), message="offline")
        await self.dispatch_callback("stars_buy:standart:449")
        self.assertFalse(await database.is_user_allowed(self.user_id))
        self.assertIn("Деньги не списаны", self.methods("SendMessage")[-1].text)

    async def test_legal_documents_show_edition_without_review_code(self):
        await self.dispatch_message(text="/agreement")
        text = self.methods("SendMessage")[-1].text
        self.assertIn("Пользовательское соглашение", text)
        self.assertNotIn("Код проверки", text)
        self.assertIn("03.11.2026", text)
        self.assertIn("doc_page:agreement:1", self.callback_values())

        await self.dispatch_callback("doc_page:agreement:2")
        text = self.methods("EditMessageText")[-1].text
        self.assertIn("10 000 ₽", text)
        self.assertIn("24 часов", text)
        self.assertIn("avitoradar.help.support@gmail.com", text)

        await self.dispatch_message(text="/privacy")
        text = self.methods("SendMessage")[-1].text
        self.assertIn("Политика конфиденциальности", text)
        self.assertNotIn("Код проверки", text)
        self.assertIn("avitoradar.help.support@gmail.com", text)

        await self.dispatch_message(text="/terms")
        text = self.methods("SendMessage")[-1].text
        self.assertIn("Условия подписки", text)
        self.assertNotIn("Код проверки", text)
        self.assertIn("10 000 ₽", text)
        self.assertIn("24 часов", text)
        self.assertIn(f"{config.rub_price('pro')} ₽", text)
        self.assertIn("doc_agreement", self.callback_values())
        self.assertIn("doc_privacy", self.callback_values())

        await self.dispatch_message(text="/docs")
        text = self.methods("SendMessage")[-1].text
        self.assertIn("Документы", text)
        self.assertIn("doc_agreement", self.callback_values())
        self.assertIn("doc_privacy", self.callback_values())
        self.assertIn("payment_terms", self.callback_values())

    def test_legal_documents_fit_telegram_and_name_no_legal_entity(self):
        for title, icon, pages in legal_docs.DOCS.values():
            for index, page in enumerate(pages):
                name = next(key for key, doc in legal_docs.DOCS.items() if doc[2] is pages)
                text = f"{ui.e(icon)} {legal_docs.doc_header(name, index)}{page}"
                self.assertLessEqual(ui.visible_len(text), 4096, title)
                for word in ("ИНН", "ОГРН", "ООО", " ИП "):
                    self.assertNotIn(word, text)

    def test_tariff_prices_stay_under_the_payment_limit(self):
        self.assertLessEqual(max(config.PRICE_RUB.values()), config.PAYMENT_LIMIT_RUB)

