import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import _environment
from tests._dispatcher import DispatcherFixture
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage, SendPhoto
from aiogram.types import MessageEntity

import config
import database
import ui


class TestPremiumEmoji(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved = (dict(ui.OVERRIDES), ui.ENABLED, dict(ui._defaults))
        ui._apply({"radar": "111", "home": "222"}, True, {})

    def tearDown(self):
        ui._apply(*self.saved)

    def test_defaults_cover_every_slot_and_admin_overrides_win(self):
        self.assertEqual(set(ui.DEFAULT_IDS), set(ui.SLOTS))
        ui._apply({"radar": "111", "trash": "0"}, True, ui.DEFAULT_IDS)
        self.assertEqual(ui.custom_id("radar"), "111")
        self.assertEqual(ui.custom_id("home"), ui.DEFAULT_IDS["home"])
        self.assertIsNone(ui.custom_id("trash"))
        self.assertEqual(ui.e("trash"), ui.plain("trash"))

    def test_filled_slot_renders_animated_emoji_and_empty_slot_stays_plain(self):
        self.assertEqual(ui.e("radar"), '<tg-emoji emoji-id="111">🎯</tg-emoji>')
        self.assertEqual(ui.e("trash"), "✖️")
        button = ui.btn("Радар", "radar", callback_data="x")
        self.assertEqual((button.text, button.icon_custom_emoji_id), ("Радар", "111"))
        plain = ui.btn("Удалить", "trash", callback_data="x")
        self.assertEqual((plain.text, plain.icon_custom_emoji_id), ("✖️ Удалить", None))

    def test_disabled_emoji_fall_back_to_plain_everywhere(self):
        ui._apply(ui.IDS, False)
        self.assertEqual(ui.e("radar"), "🎯")
        self.assertIsNone(ui.btn("Радар", "radar", callback_data="x").icon_custom_emoji_id)

    def test_caption_budget_ignores_tags(self):
        self.assertEqual(ui.visible_len(f"{ui.e('radar')} <b>a&amp;b</b>"), 5)

    def test_pack_matching_ignores_variation_selector(self):
        found = ui.match_slots([("❗", "9"), ("🎯", "8"), ("🎯", "7")])
        self.assertEqual(found["warn"], "9")
        self.assertEqual(found["radar"], "8")
        self.assertNotIn("trash", found)

    def test_custom_emoji_are_read_from_message_entities(self):
        message = SimpleNamespace(text="x 🎯 y", caption=None, caption_entities=None, entities=[
            MessageEntity(type="custom_emoji", offset=2, length=2, custom_emoji_id="555"),
            MessageEntity(type="bold", offset=0, length=1),
        ])
        self.assertEqual(ui.custom_emoji_in(message), [("🎯", "555")])

    async def test_rejected_custom_emoji_are_resent_plain_and_switched_off(self):
        method = SendMessage(chat_id=1, text=f"{ui.e('radar')} hi",
                             reply_markup=ui.kb([ui.btn("Меню", "home", callback_data="m")]))
        sent = []

        async def make_request(bot, m):
            sent.append(m)
            if "<tg-emoji" in (m.text or ""):
                raise TelegramBadRequest(method=m, message="Bad Request: can't use custom emoji")
            return True

        self.assertTrue(await ui.PremiumEmojiFallback()(make_request, None, method))
        self.assertEqual(sent[-1].text, "🎯 hi")
        button = sent[-1].reply_markup.inline_keyboard[0][0]
        self.assertEqual((button.text, button.icon_custom_emoji_id), ("🏠 Меню", None))
        self.assertFalse(ui.ENABLED)

    async def test_unrelated_errors_are_not_retried(self):
        for method in (SendMessage(chat_id=1, text="plain"),
                       EditMessageText(chat_id=1, message_id=1, text=ui.e("radar"))):
            make_request = AsyncMock(side_effect=TelegramBadRequest(method=method, message="message is not modified"))
            with self.subTest(method=type(method).__name__), self.assertRaises(TelegramBadRequest):
                await ui.PremiumEmojiFallback()(make_request, None, method)
            make_request.assert_awaited_once()
        self.assertTrue(ui.ENABLED)

    def test_photo_caption_is_stripped_too(self):
        method = SendPhoto(chat_id=1, photo="x", caption=f"{ui.e('radar')} лот")
        self.assertEqual(ui.strip_method(method).caption, "🎯 лот")
        self.assertIsNone(ui.strip_method(SendPhoto(chat_id=1, photo="x", caption="лот")))


class TestInterfaceFlows(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-ui-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        self.user_id = 123456
        await database.get_or_create_user(self.user_id)
        await database.set_user_allowed(self.user_id, 1)
        self.setup_dispatcher()

    async def asyncTearDown(self):
        await self.close_dispatcher()
        await database.close_db()
        self.db_patch.stop()
        self.directory.cleanup()

    async def test_menu_button_during_support_ticket_opens_menu_instead_of_sending_ticket(self):
        await self.dispatch_callback("support_start_ticket")
        await self.dispatch_message(text="👤 Мой профиль")
        state = self.dp.fsm.get_context(bot=self.bot, chat_id=self.user_id, user_id=self.user_id)
        self.assertIsNone(await state.get_state())
        self.assertFalse([m for m in self.methods("SendMessage") if m.chat_id in config.ADMIN_IDS])
        self.assertIn("Профиль", self.methods("SendMessage")[-1].text)

    async def test_long_stop_word_gets_a_working_remove_button(self):
        search_id = await database.add_search(self.user_id, "Радар", "https://www.avito.ru/moskva?q=x&s=104")
        long_word = "очень длинное стоп слово про запчасти и ремонт"
        await database.add_stop_word_to_search(search_id, long_word, self.user_id)
        await self.dispatch_callback(f"manage_stops:{search_id}")
        values = self.callback_values()
        self.assertTrue(all(len(v.encode()) <= 64 for v in values if v))
        self.assertIn(f"rm_stop_i:{search_id}:0", values)
        await self.dispatch_callback(f"rm_stop_i:{search_id}:0")
        self.assertEqual((await database.get_search(search_id))["stop_words"], [])

    def _last_screen(self):
        return [m for m in self.session.calls if type(m).__name__ in ("SendMessage", "EditMessageText")][-1]

    async def test_filters_screen_has_no_price_button_and_points_to_avito(self):
        search_id = await database.add_search(self.user_id, "Радар", "https://www.avito.ru/moskva?q=x&s=104")
        await self.dispatch_callback(f"flt_open:{search_id}")
        sent = self._last_screen()
        self.assertNotIn("<b>Цена:</b>", sent.text)
        self.assertNotIn("Без договорной", sent.text)
        self.assertNotIn("Без магазинов", sent.text)
        self.assertIn("avito.ru", sent.text)
        self.assertFalse(any(v.startswith("flt_price:") for v in self.callback_values() if v))
        labels = [b.text for row in sent.reply_markup.inline_keyboard for b in row]
        self.assertFalse(any("Цена" in label for label in labels))

    async def test_help_and_add_prompt_explain_avito_site_filters(self):
        for theme in ("emoji", "clean"):
            with patch.object(config, "UI_THEME", theme):
                await self.dispatch_callback("open_help_menu")
                help_text = self._last_screen().text
                self.assertIn("частные", help_text)
                self.assertIn("состояние", help_text)
                self.assertIn("avito.ru", help_text)
                await self.dispatch_callback("add_search_prompt")
                add_text = self._last_screen().text
                self.assertIn("частные", add_text)
                self.assertIn("состояние", add_text)


if __name__ == "__main__":
    unittest.main()
