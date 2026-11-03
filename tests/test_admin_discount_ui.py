import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment
from tests._dispatcher import DispatcherFixture

import config
import database
import scheduler
from parser import AvitoItem


class TestAdminDiscountUI(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-ui-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        # Network-mode buttons mutate process-wide config; keep it from leaking into other tests.
        self.net_patches = [patch.object(config, "NET_MODE", "auto"), patch.object(config, "PROXY_URL", None), patch.object(config, "PROXY_URLS", [])]
        for net_patch in self.net_patches:
            net_patch.start()
        await database.init_db()
        self.user_id = 123456
        await database.get_or_create_user(self.user_id, username="fixture_user", first_name="Fixture")
        await database.grant_subscription(self.user_id, 30, "pro")
        self.radar_id = await database.add_search(self.user_id, "Fixture radar", "https://www.avito.ru/search")
        self.setup_dispatcher()

    async def asyncTearDown(self):
        await self.close_dispatcher()
        for net_patch in reversed(self.net_patches):
            net_patch.stop()
        self.db_patch.stop()
        self.directory.cleanup()

    async def test_price_drop_controls_removed_from_tariffs_and_radar_cards(self):
        await self.dispatch_message(text="/tariffs", user_id=self.user_id)
        tariff_text = self.methods("SendMessage")[-1].text
        self.assertNotIn("Price Drop", tariff_text)
        self.assertIn("раз в 20 с", tariff_text)

        await self.dispatch_callback(f"view_search:{self.radar_id}", self.user_id)
        callbacks = self.callback_values()
        self.assertFalse(any(cb.startswith(("set_discount:", "save_discount:", "custom_discount:")) for cb in callbacks))
        self.assertNotIn("all_discounts_menu", callbacks)

    async def test_event_stop_word_single_and_multi_radar_selection(self):
        second = await database.add_search(self.user_id, "Second radar", "https://www.avito.ru/search")
        item = AvitoItem(
            "777",
            "MacBook Pro",
            "100 000 ₽",
            "https://www.avito.ru/mac_777",
            seller_name="AppleStore",
            raw_price=100000,
            timestamp=int(time.time()),
            t_pub_reliable=True,
        )
        # Single radar event
        await database.enqueue_group_events(
            [
                {
                    "user_id": self.user_id,
                    "radar_id": self.radar_id,
                    "radar_title": "Fixture radar",
                    "item_id": "777",
                    "raw_price": 100000,
                    "tariff": "pro",
                    "priority": 10,
                    "delay_seconds": 0.0,
                    "t_seen": time.time(),
                    "t_pub": item.timestamp,
                    "t_pub_reliable": True,
                    "is_catchup": False,
                    "payload_dict": {
                        "user_id": self.user_id,
                        "radar_id": self.radar_id,
                        "search_title": "Fixture radar",
                        "item": item.__dict__,
                    },
                }
            ],
            [],
        )
        row = await database.claim_notification_event()
        event_id = row["id"]

        # Single active radar -> opens FSM directly
        await self.dispatch_callback(f"event_stop:{event_id}", self.user_id)
        state = self.dp.fsm.get_context(bot=self.bot, chat_id=self.user_id, user_id=self.user_id)
        self.assertIsNotNone(await state.get_state())
        await state.clear()

        # Aggregate second radar onto another item event -> shows inline selection
        await database.enqueue_group_events(
            [
                {
                    "user_id": self.user_id,
                    "radar_id": self.radar_id,
                    "radar_title": "Fixture radar",
                    "item_id": "888",
                    "raw_price": 100000,
                    "tariff": "pro",
                    "priority": 10,
                    "delay_seconds": 0.0,
                    "t_seen": time.time(),
                    "t_pub": item.timestamp,
                    "t_pub_reliable": True,
                    "is_catchup": False,
                    "payload_dict": {
                        "user_id": self.user_id,
                        "radar_id": self.radar_id,
                        "search_title": "Fixture radar",
                        "item": {**item.__dict__, "item_id": "888"},
                    },
                },
                {
                    "user_id": self.user_id,
                    "radar_id": second,
                    "radar_title": "Second radar",
                    "item_id": "888",
                    "raw_price": 100000,
                    "tariff": "pro",
                    "priority": 10,
                    "delay_seconds": 0.0,
                    "t_seen": time.time(),
                    "t_pub": item.timestamp,
                    "t_pub_reliable": True,
                    "is_catchup": False,
                    "payload_dict": {
                        "user_id": self.user_id,
                        "radar_id": second,
                        "search_title": "Second radar",
                        "item": {**item.__dict__, "item_id": "888"},
                    },
                },
            ],
            [],
        )
        row2 = await database.claim_notification_event()
        event2_id = row2["id"]
        await self.dispatch_callback(f"event_stop:{event2_id}", self.user_id)
        self.assertIn(f"add_stop:{self.radar_id}", self.callback_values())
        self.assertIn(f"add_stop:{second}", self.callback_values())

        # Ban seller by event_id
        await self.dispatch_callback(f"ban_seller:{event2_id}", self.user_id)
        blocked = await database.get_blocked_sellers(self.user_id)
        self.assertIn("applestore", blocked)

        # Missing/cleaned event_id returns friendly alert
        await self.dispatch_callback("event_stop:999999", self.user_id)
        self.assertIn("Уведомление устарело", self.methods("AnswerCallbackQuery")[-1].text)

    async def test_foreign_user_cannot_use_event_stop_or_ban_seller(self):
        foreign_user = 654321
        await database.get_or_create_user(foreign_user)
        await database.grant_subscription(foreign_user, 30, "pro")
        item = AvitoItem("999", "Phone", "10 000 ₽", "https://www.avito.ru/p_999", seller_name="Shop")
        await scheduler.enqueue_notification_task(
            scheduler.NotificationTask(self.user_id, self.radar_id, "Fixture radar", item, freshness_verified=True)
        )
        row = await database.claim_notification_event()
        event_id = row["id"]

        await self.dispatch_callback(f"event_stop:{event_id}", foreign_user)
        self.assertTrue(self.methods("AnswerCallbackQuery")[-1].show_alert)
        await self.dispatch_callback(f"ban_seller:{event_id}", foreign_user)
        self.assertEqual(await database.get_blocked_sellers(self.user_id), set())

    async def test_admin_stats_and_reload_proxies_commands(self):
        # Non-admin denied
        await self.dispatch_message(text="/stats", user_id=self.user_id)
        self.assertIn("прав", self.methods("SendMessage")[-1].text)

        # Admin gets stats report
        await self.dispatch_message(text="/stats", user_id=config.ADMIN_ID)
        stats_text = " ".join(m.text for m in self.methods("SendMessage")[-2:])
        self.assertIn("Мониторинг", stats_text)
        self.assertIn("Каналы", stats_text)

        await self.dispatch_message(text="/reload_proxies", user_id=config.ADMIN_ID)
        reload_text = self.methods("SendMessage")[-1].text
        self.assertIn("Пул прокси", reload_text)

        # Toggle direct mode via command and callback
        await self.dispatch_message(text="/direct_mode off", user_id=config.ADMIN_ID)
        self.assertFalse(config.ALLOW_DIRECT_AVITO)
        self.assertEqual(await database.get_bot_setting("allow_direct_avito"), "0")
        self.assertEqual(config.NET_MODE, "proxy")

        await self.dispatch_callback("admin_toggle_direct_avito:alert", config.ADMIN_ID)
        self.assertTrue(config.ALLOW_DIRECT_AVITO)
        self.assertEqual(await database.get_bot_setting("allow_direct_avito"), "1")
        self.assertEqual(config.NET_MODE, "direct")

    async def test_admin_list_exposes_tariff_duration_confirmation_and_replay_safety(self):
        await self.dispatch_message(text="/admin", user_id=config.ADMIN_ID)
        self.assertTrue({"admin_page:0", "admin_net", "admin_runtime_stats"} <= set(self.callback_values()))
        await self.dispatch_callback("admin_page:0", config.ADMIN_ID)
        self.assertIn(f"admin_user:{self.user_id}:0", self.callback_values())
        await self.dispatch_callback(f"admin_user:{self.user_id}:0", config.ADMIN_ID)
        self.assertIn(f"admin_sub_menu:{self.user_id}:0", self.callback_values())
        self.assertIn(f"admin_payments:{self.user_id}", self.callback_values())
        await self.dispatch_callback(f"admin_sub_menu:{self.user_id}:0", config.ADMIN_ID)
        self.assertIn(f"admin_sub_days:{self.user_id}:standart:0", self.callback_values())
        self.assertIn(f"admin_sub_days:{self.user_id}:pro:0", self.callback_values())
        await self.dispatch_callback(f"admin_sub_days:{self.user_id}:standart:0", config.ADMIN_ID)
        for days in (1, 7, 30, 90):
            self.assertIn(f"admin_grant_sub:{self.user_id}:standart:{days}:0", self.callback_values())
        before = await database.get_user(self.user_id)
        await self.dispatch_callback(f"admin_grant_sub:{self.user_id}:standart:30:0", config.ADMIN_ID)
        self.assertEqual(await database.get_user(self.user_id), before)
        confirm = next(value for value in self.callback_values() if value.startswith("admin_confirm_sub:"))
        await self.dispatch_callback(confirm, config.ADMIN_ID)
        after = await database.get_user(self.user_id)
        self.assertEqual(after["tariff_plan"], "standart")
        self.assertEqual(after["subscription_revision"], before["subscription_revision"] + 1)
        await self.dispatch_callback(confirm, config.ADMIN_ID)
        self.assertEqual(await database.get_user(self.user_id), after)
        notices = [m.text for m in self.methods("SendMessage") if m.chat_id == self.user_id]
        self.assertEqual(len(notices), 1)
        self.assertNotIn("Оплата подтверждена", notices[0])

    async def test_admin_token_cannot_be_applied_by_another_admin_or_user(self):
        token = await database.create_admin_subscription_action(config.ADMIN_ID, self.user_id, "pro", 7)
        before = await database.get_user(self.user_id)
        await self.dispatch_callback(f"admin_confirm_sub:{token}", config.ADMIN_IDS[1])
        await self.dispatch_callback(f"admin_confirm_sub:{token}", self.user_id)
        self.assertEqual(await database.get_user(self.user_id), before)

    async def test_non_admin_cannot_use_any_subscription_or_refund_controls(self):
        before = await database.get_user(self.user_id)
        for value in (
            f"admin_user:{self.user_id}:0",
            f"admin_sub_menu:{self.user_id}:0",
            f"admin_grant_sub:{self.user_id}:pro:30:0",
            f"admin_revoke:{self.user_id}:0",
            f"admin_payments:{self.user_id}",
            "admin_refund:1",
        ):
            await self.dispatch_callback(value)
            self.assertIn("прав", self.methods("AnswerCallbackQuery")[-1].text)
        self.assertEqual(await database.get_user(self.user_id), before)

    async def test_admin_card_does_not_register_unknown_targets_or_revoke_admin(self):
        await self.dispatch_callback("admin_user:999999:0", config.ADMIN_ID)
        self.assertIsNone(await database.get_user(999999))
        await self.dispatch_callback(f"admin_grant_sub:{config.ADMIN_ID}:standart:30", config.ADMIN_ID)
        await self.dispatch_callback(f"admin_revoke:{config.ADMIN_ID}:0", config.ADMIN_ID)
        self.assertTrue(await database.is_user_allowed(config.ADMIN_ID))

    async def test_admin_revoke_requires_visible_confirmation_path(self):
        await self.dispatch_callback(f"admin_revoke_prompt:{self.user_id}:0", config.ADMIN_ID)
        self.assertTrue(await database.is_user_allowed(self.user_id))
        self.assertIn(f"admin_revoke:{self.user_id}:0", self.callback_values())
        await self.dispatch_callback(f"admin_revoke:{self.user_id}:0", config.ADMIN_ID)
        self.assertFalse(await database.is_user_allowed(self.user_id))

    async def test_real_diagnostic_button_requires_admin_and_replies(self):
        await self.dispatch_callback("admin_probe")
        self.assertIn("прав", self.methods("AnswerCallbackQuery")[-1].text)
        await self.dispatch_callback("admin_probe", config.ADMIN_ID)
        self.assertIn("Запрос получен", self.methods("AnswerCallbackQuery")[-1].text)
        self.assertIn("реального запроса Telegram", self.methods("SendMessage")[-1].text)

    async def test_admin_search_user_by_id_and_username(self):
        await self.dispatch_message(text=f"/find {self.user_id}", user_id=config.ADMIN_ID)
        self.assertIn("КАРТОЧКА ПОЛЬЗОВАТЕЛЯ", self.methods("SendMessage")[-1].text)
        self.assertIn(str(self.user_id), self.methods("SendMessage")[-1].text)

        await self.dispatch_callback("admin_search_user_prompt", config.ADMIN_ID)
        self.assertIn("ПОИСК ПОЛЬЗОВАТЕЛЯ", self.methods("EditMessageText")[-1].text)
        await self.dispatch_message(text="@fixture_user", user_id=config.ADMIN_ID)
        self.assertIn("КАРТОЧКА ПОЛЬЗОВАТЕЛЯ", self.methods("SendMessage")[-1].text)

        await self.dispatch_message(text="/find non_existent_user_99999", user_id=config.ADMIN_ID)
        self.assertIn("не найден", self.methods("SendMessage")[-1].text)

    async def test_admin_grant_vip_and_reset_trial_notifications(self):
        await self.dispatch_callback(f"admin_grant_vip_prompt:{self.user_id}:pro:0", config.ADMIN_ID)
        self.assertIn(f"admin_confirm_vip:{self.user_id}:pro:0", self.callback_values())
        await self.dispatch_callback(f"admin_confirm_vip:{self.user_id}:pro:0", config.ADMIN_ID)
        self.assertTrue(await database.is_user_allowed(self.user_id))
        user = await database.get_user(self.user_id)
        self.assertIsNone(user["subscription_until"])
        self.assertEqual(user["tariff_plan"], "pro")
        notices = [m for m in self.methods("SendMessage") if m.chat_id == self.user_id]
        self.assertTrue(any("VIP-доступ" in m.text for m in notices))

        await self.dispatch_callback(f"admin_reset_trial_prompt:{self.user_id}:0", config.ADMIN_ID)
        self.assertIn(f"admin_confirm_trial:{self.user_id}:0", self.callback_values())
        await self.dispatch_callback(f"admin_confirm_trial:{self.user_id}:0", config.ADMIN_ID)
        user_trial = await database.get_user(self.user_id)
        self.assertIsNotNone(user_trial["subscription_until"])
        notices2 = [m for m in self.methods("SendMessage") if m.chat_id == self.user_id]
        self.assertTrue(any(config.TRIAL_TEXT in m.text for m in notices2))

    async def test_admin_view_user_radars(self):
        await self.dispatch_callback(f"admin_user_radars:{self.user_id}:0", config.ADMIN_ID)
        text = self.methods("EditMessageText")[-1].text
        self.assertIn("РАДАРЫ ПОЛЬЗОВАТЕЛЯ", text)
        self.assertIn("Fixture radar", text)


if __name__ == "__main__":
    unittest.main()
