"""The clean theme: no emoji on buttons or in texts, colour carries the meaning, old menu labels still work."""

import unittest
from unittest.mock import patch

from tests import _environment  # noqa: F401
import config
import ui
from aiogram.methods import SendMessage


class CleanThemeTests(unittest.TestCase):
    def test_buttons_get_the_monochrome_icon_and_keep_colour(self):
        with patch.object(config, "UI_THEME", "clean"), patch.object(ui, "ENABLED", True):
            button = ui.btn("Удалить радар", "trash", callback_data="x", style="danger")
        self.assertEqual(button.text, "Удалить радар")
        self.assertEqual(button.icon_custom_emoji_id, ui.CLEAN_IDS["trash"])
        self.assertEqual(button.style, "danger")

    def test_premium_off_leaves_plain_text_without_colour_emoji(self):
        with patch.object(config, "UI_THEME", "clean"), patch.object(ui, "ENABLED", False):
            button = ui.btn("Удалить радар", "trash", callback_data="x")
            self.assertEqual(ui.e("radar"), "")
        self.assertEqual(button.text, "Удалить радар")
        self.assertIsNone(button.icon_custom_emoji_id)

    def test_emoji_theme_is_unchanged(self):
        with patch.object(config, "UI_THEME", "emoji"):
            self.assertTrue(ui.btn("Радары", "radars", callback_data="x").text.endswith("Радары"))
            self.assertNotEqual(ui.e("radar"), "")

    def test_texts_lose_emoji_and_the_space_it_leaves(self):
        with patch.object(config, "UI_THEME", "clean"), patch.object(ui, "ENABLED", False):
            text = f"{ui.e('radar')} <b>Новый радар</b>\n\n{ui.e('admin')} Можно прислать до 50 ссылок"
            method = ui.clean_method(SendMessage(chat_id=1, text=text))
        self.assertEqual(method.text, "<b>Новый радар</b>\n\nМожно прислать до 50 ссылок")

    def test_pre_blocks_keep_their_indentation(self):
        text = "<pre>Traceback\n  File x</pre>"
        self.assertEqual(ui.clean_method(SendMessage(chat_id=1, text=text)).text, text)

    def test_both_menu_label_sets_are_understood(self):
        texts = ui.menu_texts("help")
        self.assertIn("Инструкция", texts)
        self.assertIn(ui.MENU["help"][0], texts)
        with patch.object(config, "UI_THEME", "clean"):
            self.assertEqual(ui.menu_label("admin"), "Админка")

    def test_colour_emoji_go_status_marks_stay(self):
        self.assertEqual(ui.drop_colour_emoji("🧠 Умный · 🔥 <b>Полный</b>"), "Умный · <b>Полный</b>")
        self.assertEqual(ui.drop_colour_emoji("🟢 Сервер, ✓ ваш, ☑ выбран"), "🟢 Сервер, ✓ ваш, ☑ выбран")

    def test_old_screens_get_icons_for_their_emoji(self):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Умный", callback_data="a"),
            InlineKeyboardButton(text="🗑", callback_data="b"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="c"),
        ]])
        with patch.object(config, "UI_THEME", "clean"), patch.object(ui, "ENABLED", True):
            sent = ui.clean_method(SendMessage(chat_id=1, text="🌐 <b>Сеть</b>", reply_markup=markup))
        self.assertEqual(sent.text, f'<tg-emoji emoji-id="{ui.CLEAN_IDS["net"]}">🌐</tg-emoji> <b>Сеть</b>')
        row = sent.reply_markup.inline_keyboard[0]
        self.assertEqual([b.text for b in row], ["✓ Умный", "Удалить", "Меню"])
        self.assertEqual(row[1].icon_custom_emoji_id, ui.CLEAN_IDS["trash"])
        self.assertEqual(row[2].icon_custom_emoji_id, ui.CLEAN_IDS["home"])
        tag = '<tg-emoji emoji-id="1">🔥</tg-emoji> Полный'
        self.assertEqual(ui.drop_colour_emoji(tag), tag)

    def test_admin_sits_next_to_support_in_the_clean_menu(self):
        from handlers import common
        with patch.object(config, "UI_THEME", "clean"):
            rows = common.get_main_keyboard(config.ADMIN_ID).keyboard
        self.assertEqual([b.text for b in rows[0]], ["Добавить радар"])
        self.assertEqual(rows[0][0].style, "primary")
        self.assertEqual([b.text for b in rows[-1]], ["Поддержка", "Админка"])


if __name__ == "__main__":
    unittest.main()
