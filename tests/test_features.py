import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import _environment
from tests._dispatcher import DispatcherFixture

import config
import database
import reports
import source_cache
from runtime_metrics import RuntimeMetricsRegistry as RuntimeMetrics


def _utc(seconds_from_now: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).strftime("%Y-%m-%d %H:%M:%S")


class TestReminderRules(unittest.TestCase):
    def user(self, left, *, trial=False, user_id=555):
        return {"user_id": user_id, "subscription_until": _utc(left), "trial_used": 1, "is_allowed": 0 if trial else 1,
                "tariff_plan": "pro"}

    def test_paid_subscription_stages(self):
        now = time.time()
        self.assertIsNone(reports.reminder_kind(self.user(5 * 86400), now))
        self.assertEqual(reports.reminder_kind(self.user(2.5 * 86400), now), "3d")
        self.assertEqual(reports.reminder_kind(self.user(3600 * 20), now), "1d")
        self.assertEqual(reports.reminder_kind(self.user(-600), now), "end")
        self.assertIsNone(reports.reminder_kind(self.user(-2 * 86400), now))

    def test_trial_gets_last_hour_and_end_only(self):
        now = time.time()
        self.assertIsNone(reports.reminder_kind(self.user(3 * 3600, trial=True), now))
        self.assertIsNone(reports.reminder_kind(self.user(3000, trial=True), now))
        self.assertEqual(reports.reminder_kind(self.user(1700, trial=True), now), "30m")
        self.assertIn("через 30 минут", reports.reminder_text(self.user(1700, trial=True), "30m"))
        ended = reports.reminder_text(self.user(-60, trial=True), "end")
        self.assertIn("Пробный период закончился", ended)
        self.assertNotIn("прислал", ended)
        self.assertIn("22 новых лота", reports.reminder_text(self.user(-60, trial=True), "end", 22))

    def test_admins_get_no_reminders(self):
        admin = next(iter(config.ADMIN_IDS))
        self.assertIsNone(reports.reminder_kind(self.user(3600, user_id=admin), time.time()))

    def test_report_time_is_next_morning_msk(self):
        # 2026-10-29 05:00 UTC = 08:00 MSK -> one hour to 09:00 MSK
        now = datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(reports.seconds_until_report(now), 3600)
        self.assertEqual(reports.seconds_until_report(now + 7200), 86400 - 3600)

    def test_stats_at_picks_next_msk_moment(self):
        # 00:18 UTC = 03:18 MSK -> 05:00 MSK is 02:00 UTC the same day, tomorrow once it has passed
        now = datetime(2026, 10, 30, 0, 18, tzinfo=timezone.utc).timestamp()
        self.assertEqual(reports.next_msk_epoch("5:00", now), datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(reports.next_msk_epoch("03:00", now), datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(reports.next_msk_epoch("25:00", now))
        self.assertIsNone(reports.next_msk_epoch("завтра", now))


class TestNightAndDaily(unittest.TestCase):
    def test_night_hours_in_moscow(self):
        def at(hour_msk):
            return datetime(2026, 10, 29, hour_msk, 30, tzinfo=timezone(timedelta(hours=3))).timestamp()
        quiet = config.is_quiet_hour_msk
        self.assertTrue(quiet(23, 8, at(23)))
        self.assertTrue(quiet(23, 8, at(3)))
        self.assertFalse(quiet(23, 8, at(8)))
        self.assertFalse(quiet(23, 8, at(15)))
        self.assertTrue(quiet(1, 6, at(3)))
        self.assertFalse(quiet(1, 6, at(23)))
        self.assertFalse(quiet(5, 5, at(5)))

    def test_daily_summary_counts_scans_and_real_latency(self):
        metrics = RuntimeMetrics()
        metrics.record_scan_attempt(success=True)
        metrics.record_scan_attempt(success=False)
        now = time.time()
        metrics.record_delivery("pro", t_pub=now - 40, t_pub_reliable=True, t_seen=now - 30, t_queued=now - 30,
                                deliver_at=now - 30, t_sent=now)
        peek = metrics.pop_daily_summary(reset=False)
        self.assertEqual((peek["scans_ok"], peek["scans_failed"], peek["sent"]), (1, 1, 1))
        self.assertEqual(peek["pub_to_sent"]["p50"], 45.0)
        metrics.pop_daily_summary()
        self.assertEqual(metrics.pop_daily_summary()["sent"], 0)


class TestRadarEditing(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-features-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        self.user_id = next(iter(config.ADMIN_IDS))
        await database.get_or_create_user(self.user_id)
        self.setup_dispatcher()

    async def asyncTearDown(self):
        await self.close_dispatcher()
        await database.close_db()
        self.db_patch.stop()
        self.directory.cleanup()

    async def add(self, n):
        return await database.add_search(self.user_id, f"Радар {n}", f"https://www.avito.ru/moskva?q=r{n}&s=104",
                                         initial_items=[("1", 100)])

    async def test_rename_keeps_everything_else(self):
        search_id = await self.add(1)
        await self.dispatch_callback(f"rename_search:{search_id}", user_id=self.user_id)
        await self.dispatch_message(user_id=self.user_id, text="  Айфоны   до 50к ")
        self.assertEqual((await database.get_search(search_id))["title"], "Айфоны до 50к")

    async def test_new_link_replaces_url_and_baseline_but_not_a_new_radar(self):
        search_id = await self.add(1)
        await database.add_stop_word_to_search(search_id, "копия", self.user_id)
        await self.dispatch_callback(f"change_url:{search_id}", user_id=self.user_id)
        snapshot = type("Snap", (), {"title": "x", "items": [type("I", (), {"item_id": "9", "raw_price": 5})()]})()
        with patch("source_cache.SOURCE_SNAPSHOTS.load", new=AsyncMock(return_value=snapshot)):
            await self.dispatch_message(user_id=self.user_id, text="https://www.avito.ru/kazan?q=new")
        searches = await database.get_user_searches(self.user_id)
        self.assertEqual(len(searches), 1)
        self.assertIn("kazan", searches[0]["url"])
        self.assertEqual(searches[0]["stop_words"], ["копия"])
        self.assertEqual(await database.get_seen_item_ids_for_search(search_id), {"9"})

    def last_text(self):
        return [m for m in self.session.calls if type(m).__name__ in ("SendMessage", "EditMessageText")][-1].text

    async def test_bad_link_gets_an_error_and_creates_no_radar(self):
        await self.dispatch_callback("add_search_prompt", user_id=self.user_id)
        with patch("source_cache.SOURCE_SNAPSHOTS.load", new=AsyncMock(side_effect=source_cache.BadSourceLink("catalog_missing"))):
            await self.dispatch_message(user_id=self.user_id, text="https://www.avito.ru/moskva/muzykalnye_instr=104")
        self.assertEqual(await database.get_user_searches(self.user_id), [])
        self.assertIn("Радар не создан", self.last_text())

    async def test_bad_replacement_link_keeps_the_old_one(self):
        search_id = await self.add(1)
        await self.dispatch_callback(f"change_url:{search_id}", user_id=self.user_id)
        with patch("source_cache.SOURCE_SNAPSHOTS.load", new=AsyncMock(side_effect=source_cache.BadSourceLink("http_client"))):
            await self.dispatch_message(user_id=self.user_id, text="https://www.avito.ru/kazan/broken")
        self.assertIn("q=r1", (await database.get_search(search_id))["url"])
        self.assertIn("Прежняя ссылка радара осталась", self.last_text())

    async def test_blocked_scan_still_creates_the_radar(self):
        await self.dispatch_callback("add_search_prompt", user_id=self.user_id)
        with patch("source_cache.SOURCE_SNAPSHOTS.load", new=AsyncMock(return_value=None)):
            await self.dispatch_message(user_id=self.user_id, text="https://www.avito.ru/kazan?q=iphone")
        self.assertEqual(len(await database.get_user_searches(self.user_id)), 1)

    async def test_admin_bulk_delete_removes_only_selected(self):
        ids = [await self.add(n) for n in range(3)]
        await self.dispatch_callback("bulk_del:new", user_id=self.user_id)
        await self.dispatch_callback(f"bulk_tog:{ids[0]}:0", user_id=self.user_id)
        await self.dispatch_callback(f"bulk_tog:{ids[2]}:0", user_id=self.user_id)
        self.assertIn("bulk_go", self.callback_values())
        await self.dispatch_callback("bulk_go", user_id=self.user_id)
        await self.dispatch_callback("bulk_yes", user_id=self.user_id)
        self.assertEqual([s["id"] for s in await database.get_user_searches(self.user_id)], [ids[1]])

    async def test_bulk_delete_is_admin_only(self):
        user_id = 777001
        await database.get_or_create_user(user_id)
        await database.set_user_allowed(user_id, 1)
        await self.dispatch_callback("bulk_yes", user_id=user_id)
        await self.dispatch_callback("bulk_del:new", user_id=user_id)
        self.assertNotIn("bulk_go", self.callback_values() if self.methods("EditMessageText") else [])

    async def test_night_mode_is_off_by_default_and_user_picks_mode_and_hours(self):
        database._NIGHT_MODE_CACHE.clear()
        self.assertEqual(await database.get_night_settings(self.user_id), (database.NIGHT_OFF, 23, 8))
        await self.dispatch_callback("night_mode", user_id=self.user_id)
        self.assertNotIn("night_pick:from", self.callback_values())  # no hours while it is off
        await self.dispatch_callback(f"night_mode:{database.NIGHT_HOLD}", user_id=self.user_id)
        self.assertIn("night_pick:from", self.callback_values())
        await self.dispatch_callback("night_pick:from", user_id=self.user_id)
        await self.dispatch_callback("night_from:1", user_id=self.user_id)
        await self.dispatch_callback("night_to:7", user_id=self.user_id)
        database._NIGHT_MODE_CACHE.clear()
        self.assertEqual(await database.get_night_settings(self.user_id), (database.NIGHT_HOLD, 1, 7))

    async def test_reminder_is_sent_once_per_stage(self):
        user_id = 777002
        await database.get_or_create_user(user_id)
        await database.grant_subscription(user_id, days=2, tariff_plan="pro")
        bot = AsyncMock()
        self.assertEqual(await reports.send_due_reminders(bot), 1)
        self.assertEqual(await reports.send_due_reminders(bot), 0)
        self.assertIn("через 3 дня", bot.send_message.await_args.args[1])

    async def test_trial_end_counts_sent_lots(self):
        user_id = 777003
        await database.get_or_create_user(user_id)
        await database.activate_trial(user_id)
        until = reports._until_epoch((await database.get_user(user_id))["subscription_until"])
        async with database.writer_connection() as db:
            for i, sent_at in enumerate((until - 3600, until - 7200, until - (config.TRIAL_HOURS + 1) * 3600)):
                await db.execute(
                    "INSERT INTO notification_dedupe (user_id, item_id, status, first_queued_at, sent_at, updated_at) "
                    "VALUES (?, ?, 'sent', ?, ?, ?)",
                    (user_id, f"lot{i}", sent_at, sent_at, sent_at),
                )
            await db.commit()
        self.assertEqual(await database.count_sent_lots(user_id, until - config.TRIAL_HOURS * 3600, until), 2)
        bot = AsyncMock()
        await reports.send_due_reminders(bot, now=until + 60)
        self.assertIn("2 новых лота", bot.send_message.await_args.args[1])

    async def test_daily_report_renders(self):
        text = await reports.build_daily_report(reset=False)
        self.assertIn("Отчёт за сутки", text)
        self.assertIn("Удачных сканов", text)


if __name__ == "__main__":
    unittest.main()
