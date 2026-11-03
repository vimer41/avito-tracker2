import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests import _environment
from tests._dispatcher import DispatcherFixture
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import CopyMessage
from aiogram.types import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User

import config
import database
import scheduler

GROUP_ID = -100777


class TestGroupDelivery(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-group-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        database._MIRROR_CHAT_CACHE.clear()
        self.user_id = 880001
        await database.get_or_create_user(self.user_id)
        self.setup_dispatcher(("group_delivery", "payments", "admin", "support", "common", "searches"))

    async def asyncTearDown(self):
        database._MIRROR_CHAT_CACHE.clear()
        await self.close_dispatcher()
        await database.close_db()
        self.db_patch.stop()
        self.directory.cleanup()

    async def group_start(self, user_id):
        self.update_id += 1
        message = Message(message_id=self.update_id, date=datetime.now(timezone.utc),
                          chat=Chat(id=GROUP_ID, type="supergroup", title="Команда"),
                          from_user=User(id=user_id, is_bot=False, first_name="Fixture"), text="/start radar")
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, message=message))

    def sent_message(self):
        return Message(message_id=42, date=datetime.now(timezone.utc), chat=Chat(id=self.user_id, type="private"))

    async def test_lite_cannot_link_a_group(self):
        await database.grant_subscription(self.user_id, days=30, tariff_plan="lite")
        await self.group_start(self.user_id)
        self.assertIsNone(await database.get_mirror_chat(self.user_id))
        self.assertIn("«Средний»", self.methods("SendMessage")[-1].text)
        self.assertFalse(await scheduler.mirror_to_group(self.bot, self.user_id, self.sent_message(), None, False))

    async def test_full_links_group_and_lots_are_copied_with_link_buttons_only(self):
        await database.grant_subscription(self.user_id, days=30, tariff_plan="pro")
        await self.group_start(self.user_id)
        self.assertEqual(await database.get_mirror_chat(self.user_id), GROUP_ID)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Открыть", url="https://www.avito.ru/1")],
            [InlineKeyboardButton(text="Скрыть продавца", callback_data="ban_seller:1")],
        ])
        self.assertTrue(await scheduler.mirror_to_group(self.bot, self.user_id, self.sent_message(), keyboard, True))
        copy = self.methods("CopyMessage")[-1]
        self.assertEqual((copy.chat_id, copy.from_chat_id, copy.message_id), (GROUP_ID, self.user_id, 42))
        self.assertEqual([[b.url for b in row] for row in copy.reply_markup.inline_keyboard], [["https://www.avito.ru/1"]])
        self.assertTrue(copy.disable_notification)

    async def test_bot_removed_from_group_unlinks_it(self):
        await database.grant_subscription(self.user_id, days=30, tariff_plan="standart")
        await database.set_mirror_chat(self.user_id, GROUP_ID)
        self.session.failures["CopyMessage"] = TelegramForbiddenError(CopyMessage(chat_id=GROUP_ID, from_chat_id=1, message_id=1), "kicked")
        self.assertFalse(await scheduler.mirror_to_group(self.bot, self.user_id, self.sent_message(), None, False))
        database._MIRROR_CHAT_CACHE.clear()
        self.assertIsNone(await database.get_mirror_chat(self.user_id))

    async def test_profile_screen_and_unlink(self):
        await database.grant_subscription(self.user_id, days=30, tariff_plan="pro")
        await database.set_mirror_chat(self.user_id, GROUP_ID)
        await self.dispatch_callback("group_delivery", user_id=self.user_id)
        self.assertIn("group_off", self.callback_values())
        await self.dispatch_callback("group_off", user_id=self.user_id)
        self.assertIsNone(await database.get_mirror_chat(self.user_id))
        urls = [b.url for row in self.last_markup().inline_keyboard for b in row if b.url]
        self.assertEqual(urls, ["https://t.me/fixture_bot?startgroup=radar"])

    async def test_other_group_messages_get_no_menu(self):
        self.update_id += 1
        message = Message(message_id=self.update_id, date=datetime.now(timezone.utc),
                          chat=Chat(id=GROUP_ID, type="supergroup", title="Команда"),
                          from_user=User(id=880002, is_bot=False, first_name="Чужой"), text="/radars")
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, message=message))
        self.assertEqual(self.methods("SendMessage"), [])


if __name__ == "__main__":
    unittest.main()
