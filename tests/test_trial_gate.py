import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import _environment
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetChatMember
from aiogram.types import CallbackQuery, Chat, Message, User

import config
import database
import legal_docs
import trial_gate
from handlers import common
from middlewares import AccessControlMiddleware


def member(status, **extra):
    return SimpleNamespace(status=status, **extra)


class TestChannelCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        trial_gate._alerted_at = 0.0
        self.bot = SimpleNamespace(get_chat_member=AsyncMock(), send_message=AsyncMock())

    async def test_members_pass_and_others_do_not(self):
        cases = [
            (member(ChatMemberStatus.MEMBER), True),
            (member(ChatMemberStatus.ADMINISTRATOR), True),
            (member(ChatMemberStatus.CREATOR), True),
            (member(ChatMemberStatus.RESTRICTED, is_member=True), True),
            (member(ChatMemberStatus.RESTRICTED, is_member=False), False),
            (member(ChatMemberStatus.LEFT), False),
            (member(ChatMemberStatus.KICKED), False),
        ]
        for result, expected in cases:
            with self.subTest(status=result.status):
                self.bot.get_chat_member.return_value = result
                self.assertEqual(await trial_gate.is_subscribed(self.bot, 1), expected)
        self.bot.get_chat_member.assert_awaited_with(config.TRIAL_CHANNEL, 1)

    async def test_bot_without_channel_rights_does_not_block_trial_and_alerts_once(self):
        method = GetChatMember(chat_id=config.TRIAL_CHANNEL, user_id=1)
        self.bot.get_chat_member.side_effect = TelegramBadRequest(method, "member list is inaccessible")
        self.assertTrue(await trial_gate.is_subscribed(self.bot, 1))
        self.assertTrue(await trial_gate.is_subscribed(self.bot, 2))
        self.assertEqual(self.bot.send_message.await_count, len(config.ADMIN_IDS))

    async def test_network_failure_is_not_treated_as_subscribed(self):
        method = GetChatMember(chat_id=config.TRIAL_CHANNEL, user_id=1)
        self.bot.get_chat_member.side_effect = TelegramNetworkError(method, "timeout")
        with self.assertRaises(TelegramNetworkError):
            await trial_gate.is_subscribed(self.bot, 1)


class TestTrialButton(unittest.IsolatedAsyncioTestCase):
    def call(self):
        return SimpleNamespace(bot=object(), from_user=SimpleNamespace(id=5, username="u", first_name="U"), answer=AsyncMock())

    async def test_not_subscribed_gets_alert_and_no_trial(self):
        call = self.call()
        with patch.object(trial_gate, "is_subscribed", new=AsyncMock(return_value=False)), \
                patch.object(database, "activate_trial", new=AsyncMock()) as activate:
            await common.cb_activate_trial(call)
        activate.assert_not_called()
        self.assertTrue(call.answer.await_args.kwargs["show_alert"])

    async def test_telegram_failure_asks_to_retry(self):
        call = self.call()
        method = GetChatMember(chat_id=config.TRIAL_CHANNEL, user_id=5)
        with patch.object(trial_gate, "is_subscribed", new=AsyncMock(side_effect=TelegramNetworkError(method, "timeout"))), \
                patch.object(database, "activate_trial", new=AsyncMock()) as activate:
            await common.cb_activate_trial(call)
        activate.assert_not_called()
        self.assertIn("ещё раз", call.answer.await_args.args[0])

    async def test_subscribed_gets_trial(self):
        call = self.call()
        result = {"success": True, "message": "ok", "until": "2026-10-30 12:00:00"}
        with patch.object(trial_gate, "is_subscribed", new=AsyncMock(return_value=True)), \
                patch.object(database, "activate_trial", new=AsyncMock(return_value=result)) as activate, \
                patch.object(common.ui, "show", new=AsyncMock()) as show:
            await common.cb_activate_trial(call)
        activate.assert_awaited_once_with(5)
        self.assertIn("Пробный период включён", show.await_args.args[1])


class TestOfferInMiddleware(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = User(id=123456, is_bot=False, first_name="Fixture")
        self.handler = AsyncMock()
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.state = SimpleNamespace(get_state=AsyncMock(return_value=None), clear=AsyncMock())
        self.data = {"event_from_user": self.user, "bot": self.bot, "state": self.state}

    def message(self, text):
        return Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc), chat=Chat(id=self.user.id, type="private"), from_user=self.user, text=text)

    async def test_new_user_sees_offer_and_admins_are_not_bothered(self):
        with patch.object(database, "is_user_allowed", new=AsyncMock(return_value=False)), \
                patch.object(database, "get_or_create_user", new=AsyncMock(return_value={"trial_used": 0})), \
                patch("middlewares.ui.show", new=AsyncMock()) as show:
            await AccessControlMiddleware()(self.handler, self.message("https://avito.ru/x"), self.data)
        self.handler.assert_not_called()
        self.bot.send_message.assert_not_called()
        self.assertIn("бесплатно", show.await_args.args[1])

    async def test_check_button_works_without_access(self):
        call = CallbackQuery(id="t", from_user=self.user, chat_instance="t", data="trial_check")
        await AccessControlMiddleware()(self.handler, call, self.data)
        self.handler.assert_awaited_once()

    async def start(self, user_record, user_id=123456):
        message = SimpleNamespace(from_user=SimpleNamespace(id=user_id, username="u", first_name="U"), answer=AsyncMock())
        with patch.object(database, "get_or_create_user", new=AsyncMock(return_value=user_record)), \
                patch.object(database, "resume_blocked_searches", new=AsyncMock(return_value=0)):
            await common.cmd_start(message, self.state)
        return [call.args[0] for call in message.answer.await_args_list]

    async def test_new_user_gets_code_as_own_message_before_the_offer(self):
        texts = await self.start({"trial_used": 0})
        self.assertEqual(texts[1], legal_docs.VERIFICATION_CODE)
        self.assertIn("бесплатно", texts[-1])

    async def test_code_is_not_repeated_after_the_trial_or_for_admins(self):
        self.assertNotIn(legal_docs.VERIFICATION_CODE, await self.start({"trial_used": 1}))
        self.assertNotIn(legal_docs.VERIFICATION_CODE, await self.start({"trial_used": 0}, config.ADMIN_IDS[0]))

    def test_offer_has_channel_and_check_buttons(self):
        _text, markup = trial_gate.offer()
        buttons = [b for row in markup.inline_keyboard for b in row]
        self.assertEqual(buttons[0].url, "https://t.me/avitoradar1")
        self.assertEqual(buttons[1].callback_data, "trial_check")
        self.assertIn("Я подписался, проверить", buttons[1].text)


if __name__ == "__main__":
    unittest.main()
