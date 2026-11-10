import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import _environment
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
import aiosqlite

import billing
import config
import database
import middlewares
import radar_filters
import reports
import scheduler
from handlers import common, support
from parser import AvitoItem, seller_key_from

USER_ID = 7001


def item(item_id="1", **fields):
    defaults = dict(item_id=item_id, title="Лот", price="1 000 ₽", url=f"https://www.avito.ru/{item_id}", image_url=None)
    return AvitoItem(**{**defaults, **fields})


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-tariff-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        await database.get_or_create_user(USER_ID, "u", "U")

    async def asyncTearDown(self):
        await database.close_db()
        self.db_patch.stop()
        self.directory.cleanup()

    async def add_radars(self, count):
        return [
            await database.add_search(USER_ID, f"r{n}", f"https://www.avito.ru/moskva?q=t{n}", initial_items=[])
            for n in range(count)
        ]

    async def running(self):
        return sorted(s["id"] for s in await database.get_all_active_searches() if s["user_id"] == USER_ID)

    async def days_left(self):
        return database.subscription_seconds_left(await database.get_user(USER_ID)) / 86400


class TestPlanSwitch(DatabaseCase):
    async def test_cheap_days_do_not_become_expensive_ones(self):
        for _ in range(3):
            await database.grant_subscription(USER_ID, 30, "lite")
        await database.grant_subscription(USER_ID, 30, "pro")
        fair = 90 * config.rub_price("lite") / config.rub_price("pro") + 30
        self.assertAlmostEqual(await self.days_left(), fair, delta=0.01)
        self.assertEqual((await database.get_user(USER_ID))["tariff_plan"], "pro")

    async def test_moving_down_keeps_the_value_of_paid_days(self):
        await database.grant_subscription(USER_ID, 30, "pro")
        await database.grant_subscription(USER_ID, 30, "lite")
        fair = 30 * config.rub_price("pro") / config.rub_price("lite") + 30
        self.assertAlmostEqual(await self.days_left(), fair, delta=0.01)

    async def test_same_tariff_just_adds_days(self):
        await database.grant_subscription(USER_ID, 30, "standart")
        await database.grant_subscription(USER_ID, 30, "standart")
        self.assertAlmostEqual(await self.days_left(), 60, delta=0.01)

    async def test_trial_hours_are_not_converted(self):
        await database.activate_trial(USER_ID)
        await database.grant_subscription(USER_ID, 30, "pro")
        self.assertAlmostEqual(await self.days_left(), 30 + config.TRIAL_HOURS / 24, delta=0.01)

    async def test_expired_tariff_starts_from_today(self):
        await database.grant_subscription(USER_ID, 30, "lite")
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute("UPDATE users SET subscription_until = datetime('now', '-1 day')")
            await db.commit()
        await database.grant_subscription(USER_ID, 30, "pro")
        self.assertAlmostEqual(await self.days_left(), 30, delta=0.01)

    async def test_card_warns_about_conversion_and_paused_radars(self):
        await database.grant_subscription(USER_ID, 30, "pro")
        await self.add_radars(4)
        note = await common.plan_change_note(USER_ID, "lite")
        self.assertIn("пересчитается", note)
        self.assertIn("1 радар", note)
        self.assertEqual(await common.plan_change_note(USER_ID, "pro"), "")


class TestTrialIsLikeFull(DatabaseCase):
    async def test_trial_has_one_radar_with_the_speed_and_priority_of_the_full_tariff(self):
        await database.activate_trial(USER_ID)
        self.assertEqual(await database.get_user_max_radars(USER_ID), 1)
        user = await database.get_user(USER_ID)
        self.assertEqual(database.classify_user_tariff(user)[:2], database.classify_user_tariff({**user, "tariff_plan": "pro", "is_allowed": 1})[:2])
        self.assertEqual(config.plan_interval(database.classify_user_tariff(user)[0]), config.plan_interval("pro"))

    async def test_the_trial_radar_keeps_working_on_any_tariff(self):
        await database.activate_trial(USER_ID)
        ids = await self.add_radars(1)
        self.assertEqual(await common.plan_change_note(USER_ID, "lite"), "")
        await database.grant_subscription(USER_ID, 30, "lite")
        self.assertEqual(await self.running(), ids)

    async def test_test_order_stays_on_its_own_class(self):
        self.assertEqual(database.classify_user_tariff({"user_id": USER_ID, "tariff_plan": "standart", "is_test_order": 1})[0], "base")


class TestRadarLimit(DatabaseCase):
    async def test_extra_radars_pause_after_moving_down_and_return_after_moving_up(self):
        await database.grant_subscription(USER_ID, 30, "pro")
        ids = await self.add_radars(4)
        await database.grant_subscription(USER_ID, 30, "lite")
        self.assertEqual(await self.running(), ids[:1])
        self.assertEqual(await database.count_limit_paused(USER_ID), 3)
        await database.grant_subscription(USER_ID, 30, "pro")
        self.assertEqual(await self.running(), ids)
        self.assertEqual(await database.count_limit_paused(USER_ID), 0)

    async def test_a_radar_paused_by_hand_stays_paused_after_moving_up(self):
        await database.grant_subscription(USER_ID, 30, "pro")
        ids = await self.add_radars(2)
        await database.toggle_search_active(ids[1], USER_ID)
        await database.grant_subscription(USER_ID, 30, "lite")
        await database.grant_subscription(USER_ID, 30, "pro")
        self.assertEqual(await self.running(), ids[:1])

    async def test_starting_a_radar_cannot_pass_the_limit(self):
        await database.grant_subscription(USER_ID, 30, "pro")
        ids = await self.add_radars(2)
        await database.grant_subscription(USER_ID, 30, "lite")
        with self.assertRaises(database.RadarLimitError):
            await database.toggle_search_active(ids[1], USER_ID)
        await database.toggle_search_active(ids[0], USER_ID)
        self.assertTrue(await database.toggle_search_active(ids[1], USER_ID))
        self.assertEqual(await self.running(), ids[1:])

    async def test_refund_gives_the_extra_radars_back_to_the_limit(self):
        await database.grant_subscription(USER_ID, 30, "standart")
        ids = await self.add_radars(2)
        with patch.object(config, "STARS_PAYMENTS_ENABLED", True):
            order = await billing.create_order(USER_ID, "lite")
            await billing.approve_checkout("q", USER_ID, order["payload"], "XTR", order["total_amount"])
            await billing.complete_payment(USER_ID, order["payload"], "XTR", order["total_amount"], "charge")
        self.assertEqual(await self.running(), ids[:1])
        await billing.complete_refund("charge")
        self.assertEqual(await self.running(), ids)

    async def test_startup_pass_catches_radars_already_over_the_limit(self):
        await database.grant_subscription(USER_ID, 30, "pro")
        ids = await self.add_radars(4)
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute("UPDATE users SET tariff_plan = 'lite'")
            await db.commit()
        await database.enforce_radar_limits()
        self.assertEqual(await self.running(), ids[:1])

    async def test_admin_bonus_brings_paused_radars_back(self):
        await database.grant_subscription(USER_ID, 30, "pro")
        ids = await self.add_radars(2)
        await database.grant_subscription(USER_ID, 30, "lite")
        await database.change_user_extra_radars(USER_ID, 1)
        self.assertEqual(await self.running(), ids)


class TestBlockedBot(DatabaseCase):
    async def test_radars_return_after_unblocking_except_those_paused_by_hand(self):
        await database.grant_subscription(USER_ID, 30, "pro")
        ids = await self.add_radars(3)
        await database.toggle_search_active(ids[2], USER_ID)
        await database.deactivate_user_searches(USER_ID)
        self.assertEqual(await self.running(), [])
        update = SimpleNamespace(
            chat=SimpleNamespace(type="private"), new_chat_member=SimpleNamespace(status="member"),
            from_user=SimpleNamespace(id=USER_ID),
        )
        await common.on_bot_unblocked(update)
        self.assertEqual(await self.running(), ids[:2])


class TestBlacklistByKey(DatabaseCase):
    async def test_second_seller_with_the_same_name_stays_visible(self):
        await database.add_blocked_seller(USER_ID, "Александр", "/user/a")
        blocked = await database.get_blocked_sellers(USER_ID)
        self.assertTrue(radar_filters.seller_is_blocked(item(seller_name="Александр", seller_key="/user/a"), blocked))
        self.assertFalse(radar_filters.seller_is_blocked(item(seller_name="Александр", seller_key="/user/b"), blocked))

    async def test_two_namesakes_can_both_be_hidden(self):
        await database.add_blocked_seller(USER_ID, "Александр", "/user/a")
        await database.add_blocked_seller(USER_ID, "Александр", "/user/b")
        self.assertEqual(len(await database.get_user_blocked_sellers_list(USER_ID)), 2)

    async def test_entry_without_key_still_works_by_name(self):
        await database.add_blocked_seller(USER_ID, "Магазин")
        blocked = await database.get_blocked_sellers(USER_ID)
        self.assertTrue(radar_filters.seller_is_blocked(item(seller_name="магазин ", seller_key="/user/z"), blocked))

    async def test_old_table_keeps_its_rows_after_the_migration(self):
        await database.close_db()
        db = sqlite3.connect(database.DB_PATH)
        db.execute("DROP TABLE blocked_sellers")
        db.execute("""
            CREATE TABLE blocked_sellers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, seller_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, seller_name) ON CONFLICT IGNORE
            )
        """)
        db.execute("INSERT INTO blocked_sellers (user_id, seller_name) VALUES (?, 'Старый')", (USER_ID,))
        db.commit()
        db.close()
        await database.init_db()
        await database.add_blocked_seller(USER_ID, "Старый", "/user/a")
        names = [row["seller_name"] for row in await database.get_user_blocked_sellers_list(USER_ID)]
        self.assertEqual(names, ["Старый", "Старый"])


class TestSellerKey(unittest.TestCase):
    def test_profile_link_gives_the_key(self):
        step = {"profile": {"title": "И", "link": "/user/abc123/profile?src=item"}}
        self.assertEqual(seller_key_from({}, step), "/user/abc123")
        self.assertEqual(seller_key_from({}, {"profile": {"link": "https://www.avito.ru/brands/shop1?x=1"}}), "/brands/shop1")

    def test_a_link_common_to_all_sellers_is_not_a_key(self):
        self.assertIsNone(seller_key_from({}, {"profile": {"link": "/profile/items"}}))

    def test_item_id_is_not_mistaken_for_a_seller(self):
        self.assertIsNone(seller_key_from({"id": 123}, {}))

    def test_explicit_id_field(self):
        self.assertEqual(seller_key_from({"sellerId": 55}, {}), "id:55")


class TestPriceAndTitle(unittest.TestCase):
    def test_short_price_forms(self):
        cases = {
            "до 50к": (0, 50_000), "от 10к до 50к": (10_000, 50_000), "до 50 тыс": (0, 50_000),
            "1.5 млн": (1_500_000, 0), "до 1,5 млн": (0, 1_500_000), "10-50 тыс": (10_000, 50_000),
            "500 - 1 тыс": (500, 1000), "от 300т": (300_000, 0), "50 000": (50_000, 0),
            "10 000-50 000": (10_000, 50_000),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(radar_filters.parse_price_range(text), expected)

    def test_title_needs_every_word(self):
        self.assertFalse(radar_filters.title_matches("Xiaomi 13 Pro", "iphone 13 pro"))
        self.assertTrue(radar_filters.title_matches("iPhone 13 Pro 256", "iphone 13 pro"))
        self.assertTrue(radar_filters.title_matches("Кроссовок Nike", "кроссовки nike"))
        self.assertTrue(radar_filters.title_matches("Что угодно", ""))


class TestFlood(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        scheduler.CHATS_IN_FLOOD.clear()

    async def test_flooded_chat_leaves_the_queue_until_telegram_allows_it(self):
        scheduler.pause_flooded_chat(5, 60)
        scheduler.pause_flooded_chat(6, -1)
        self.assertEqual(scheduler.flooded_chats(), [5])
        self.assertGreater(scheduler.flood_seconds_left(5), 50)

    async def test_retry_after_pauses_the_chat(self):
        row = {"id": 1, "lease_token": "t", "user_id": 5}
        ctx = {"user_id": 5, "radar_ids": [1], "radar_titles": ["r"], "item": item(), "user_label": "", "is_catchup": False,
               "possible_dup": False, "silent": False, "reply_template": ""}
        error = TelegramRetryAfter(method=SendMessage(chat_id=5, text="x"), message="flood", retry_after=30)
        with patch.object(scheduler, "_prepare_delivery", new=AsyncMock(return_value=ctx)), \
                patch.object(scheduler, "send_ad_to_user", new=AsyncMock(side_effect=error)), \
                patch.object(scheduler.database, "mark_event_send_started", new=AsyncMock()), \
                patch.object(scheduler.database, "release_notification", new=AsyncMock()) as release:
            await scheduler.deliver_notification(SimpleNamespace(), None, row)
        release.assert_awaited_once()
        self.assertIn(5, scheduler.flooded_chats())


class TestDigestButtons(unittest.TestCase):
    def test_each_lot_gets_numbered_buttons(self):
        entries = [
            {"id": 10, "item": item("1", seller_name="А"), "reply_template": "Здравствуйте, {название}", "radar_titles": ["r"]},
            {"id": 11, "item": item("2"), "reply_template": "", "radar_titles": ["r"]},
        ]
        rows = scheduler.build_digest_keyboard(entries).inline_keyboard
        self.assertEqual(rows[0][0].copy_text.text, "Здравствуйте, Лот")
        self.assertEqual(rows[1][0].callback_data, "ban_seller:10")
        self.assertIn("номера лотов", scheduler.build_digest_text(entries))

    def test_no_buttons_when_there_is_nothing_to_do(self):
        self.assertIsNone(scheduler.build_digest_keyboard([{"id": 1, "item": item(), "reply_template": "", "radar_titles": ["r"]}]))


class TestExpiredAlerts(unittest.TestCase):
    def test_one_person_alerts_admins_once_per_hour(self):
        middlewares._expired_alert_at.clear()
        self.assertTrue(middlewares.expired_alert_due(1))
        self.assertFalse(middlewares.expired_alert_due(1))
        self.assertTrue(middlewares.expired_alert_due(2))


class TestSupportOutbox(DatabaseCase):
    async def saved(self, text="x" * 4090, media_type="", file_id=""):
        ticket, message_id, _new = await database.add_user_ticket_message(USER_ID, "U <b>", "", text, media_type, file_id)
        return {**ticket, "message_id": message_id, "text": text, "media_type": media_type, "file_id": file_id,
                "message_epoch": ticket["last_user_epoch"]}

    async def test_ticket_waits_in_the_outbox_until_an_admin_gets_it(self):
        ticket = await self.saved(text="помогите")
        broken = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("flood")))
        self.assertFalse(await support.deliver_ticket(broken, ticket))
        self.assertEqual(len(await database.get_undelivered_ticket_messages()), 1)

        working = SimpleNamespace(send_message=AsyncMock())
        self.assertEqual(await support.retry_undelivered_tickets(working), 1)
        self.assertEqual(await database.get_undelivered_ticket_messages(), [])
        self.assertEqual(len(await database.get_ticket_messages(ticket["id"])), 1)  # delivered, still in history

    async def test_long_ticket_is_split_into_allowed_sizes(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        await support.send_ticket_to_admin(bot, 1, await self.saved())
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertTrue(all(len(call.kwargs["text"]) < 4096 for call in bot.send_message.await_args_list))

    async def test_photo_with_a_long_caption_keeps_its_text(self):
        bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        await support.send_ticket_to_admin(bot, 1, await self.saved(text="y" * 1000, media_type="photo", file_id="f"))
        self.assertEqual(bot.send_photo.await_args.kwargs["photo"], "f")
        self.assertLess(len(bot.send_photo.await_args.kwargs["caption"]), 1024)
        bot.send_message.assert_awaited_once()

    async def test_quote_reply_to_an_old_notice_finds_the_user_in_plain_text(self):
        replied = SimpleNamespace(text=f"📩 ОБРАЩЕНИЕ\n🆔 ID: {USER_ID}\n🕒 Время: 01.01", caption=None, message_id=7)
        message = SimpleNamespace(from_user=SimpleNamespace(id=config.ADMIN_ID), reply_to_message=replied,
                                  chat=SimpleNamespace(id=config.ADMIN_ID))
        found = await support._admin_ref(message)
        self.assertEqual(found["ref_ticket"]["user_id"], USER_ID)


class TestDailyBackup(DatabaseCase):
    async def test_admin_gets_a_packed_copy_and_no_file_stays_behind(self):
        bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
        await reports.send_daily_backup(bot)
        document = bot.send_document.await_args.args[1]
        self.assertTrue(document.path.endswith(".db.gz"))
        self.assertFalse(Path(document.path).exists())

    async def test_a_dated_copy_stays_on_the_server_for_two_weeks(self):
        import gzip

        backups = Path(config.BACKUP_DIR)
        backups.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: [path.unlink() for path in backups.glob("daily-*.db.gz")])
        for day in ("20260101", "20260102", "20260103"):
            (backups / f"daily-{day}.db.gz").write_bytes(b"old")
        bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
        with patch.object(config, "DAILY_BACKUP_KEEP", 2):
            await reports.send_daily_backup(bot)
        kept = sorted(path.name for path in backups.glob("daily-*.db.gz"))
        self.assertEqual(kept[0], "daily-20260103.db.gz")
        self.assertEqual(len(kept), 2)
        with gzip.open(backups / kept[1]) as packed:
            self.assertEqual(packed.read(15), b"SQLite format 3")
        bot.send_document.assert_awaited_once()
        self.assertEqual(config.DAILY_BACKUP_KEEP, 14)

    async def test_admins_hear_about_the_release_once(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        self.assertEqual(await reports.announce_release(bot, "abc1234", delay=0), len(config.ADMIN_IDS))
        self.assertEqual({call.args[0] for call in bot.send_message.await_args_list}, set(config.ADMIN_IDS))
        text = bot.send_message.await_args.args[1]
        self.assertIn("Обновлено 06.11.2026", text)
        self.assertIn("версия abc1234", text)
        self.assertEqual(await reports.announce_release(bot, "abc1234", delay=0), 0, "a restart sends nothing")
        self.assertEqual(bot.send_message.await_count, len(config.ADMIN_IDS))

    async def test_an_undelivered_release_note_waits_for_the_next_start(self):
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("telegram down")))
        self.assertEqual(await reports.announce_release(bot, "abc1234", delay=0), 0)
        bot.send_message = AsyncMock()
        self.assertEqual(await reports.announce_release(bot, "abc1234", delay=0), len(config.ADMIN_IDS))

    async def test_copy_over_the_telegram_limit_is_reported(self):
        bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
        with patch.object(reports, "BACKUP_MAX_BYTES", 1):
            await reports.send_daily_backup(bot)
        bot.send_document.assert_not_awaited()
        self.assertIn("больше лимита", bot.send_message.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
