"""Admin-set Telegram Stars prices: live, persistent, and unable to break the tariff order."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment
from tests._dispatcher import DispatcherFixture

import billing
import config
import database


class TestStarsPrices(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-prices-")
        self.patches = [
            patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db"),
            patch.object(config, "STARS_PAYMENTS_ENABLED", True),
            patch.object(config, "STARS_PRICE_LITE", 199),
            patch.object(config, "STARS_PRICE_STANDART", 449),
            patch.object(config, "STARS_PRICE_PRO", 899),
        ]
        for active in self.patches:
            active.start()
        await database.init_db()
        self.user_id = 123456
        await database.get_or_create_user(self.user_id, username="buyer", first_name="Buyer")
        self.setup_dispatcher()

    async def asyncTearDown(self):
        await self.close_dispatcher()
        for active in reversed(self.patches):
            active.stop()
        self.directory.cleanup()

    def prices(self):
        return [config.stars_price(plan) for plan in config.PAID_PLANS]

    async def send_prices(self, text):
        await self.dispatch_callback("admin_prices_edit", config.ADMIN_ID)
        await self.dispatch_message(text=text, user_id=config.ADMIN_ID)

    async def test_admin_panel_leads_to_the_price_screen(self):
        await self.dispatch_message(text="/admin", user_id=config.ADMIN_ID)
        self.assertIn("admin_prices", self.callback_values())
        await self.dispatch_callback("admin_prices", config.ADMIN_ID)
        text = self.methods("EditMessageText")[-1].text
        self.assertIn("Базовый: <b>199</b>", text)
        self.assertIn("Полный: <b>899</b>", text)
        self.assertIn("admin_prices_edit", self.callback_values())

    async def test_new_prices_apply_at_once_and_survive_restart(self):
        await self.send_prices("250 500 1000")
        self.assertEqual(self.prices(), [250, 500, 1000])
        self.assertIn("Базовый: <b>250</b>", self.methods("SendMessage")[-1].text)
        self.assertEqual(await database.get_bot_setting("stars_prices"), "[250, 500, 1000]")
        self.assertEqual((await billing.create_order(self.user_id, "standart"))["total_amount"], 500)

        config.STARS_PRICE_LITE, config.STARS_PRICE_STANDART, config.STARS_PRICE_PRO = 199, 449, 899
        await database.init_db()
        self.assertEqual(self.prices(), [250, 500, 1000])

    async def test_invoice_with_the_old_price_cannot_be_paid(self):
        order = await billing.create_order(self.user_id, "standart")
        await self.send_prices("250 500 1000")
        with self.assertRaises(billing.PaymentValidationError):
            await billing.approve_checkout("q", self.user_id, order["payload"], "XTR", 449)

    async def test_tariff_card_offers_the_new_price(self):
        await self.send_prices("250 500 1000")
        await self.dispatch_callback("buy_tariff:pro", self.user_id)
        self.assertIn("stars_buy:pro:1:1000", self.callback_values())

    async def test_bad_input_changes_nothing_and_keeps_asking(self):
        for text in ("100", "abc", "500 400 900", "0 1 2", "1 2 1000000", "1.5 2 3"):
            await self.send_prices(text)
            self.assertIn("три целых числа", self.methods("SendMessage")[-1].text)
            self.assertEqual(self.prices(), [199, 449, 899])
        self.assertIsNone(await database.get_bot_setting("stars_prices"))

        await self.dispatch_message(text="300, 600, 900", user_id=config.ADMIN_ID)
        self.assertEqual(self.prices(), [300, 600, 900])

    async def test_broken_saved_prices_are_ignored(self):
        for value in ("[900, 500, 100]", "oops", "[1, 2]", '["1", "2", "3"]'):
            await database.set_bot_setting("stars_prices", value)
            await database.init_db()
            self.assertEqual(self.prices(), [199, 449, 899])

    async def test_non_admin_cannot_change_prices(self):
        await self.dispatch_callback("admin_prices_edit", self.user_id)
        self.assertTrue(self.methods("AnswerCallbackQuery")[-1].show_alert)
        await self.dispatch_message(text="1 2 3", user_id=self.user_id)
        self.assertEqual(self.prices(), [199, 449, 899])


if __name__ == "__main__":
    unittest.main()
