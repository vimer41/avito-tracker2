import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests import _environment
import aiosqlite
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
import database
import scheduler
from parser import AvitoItem


class TestReliability(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-reliability-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        self.user_id = 123456
        await database.get_or_create_user(self.user_id, "fixture", "Fixture")
        await database.activate_trial(self.user_id)
        self.radar_id =await database.add_search(self.user_id, "Test", "https://www.avito.ru/search")
        self.item = AvitoItem(
            "101",
            "Test item",
            "10 000 ₽",
            "https://www.avito.ru/test_101",
            raw_price=10000,
            date="только что",
            timestamp=int(time.time()),
            t_pub_reliable=True,
        )
        self.bot = Mock()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    async def execute(self, statement, parameters=()):
        async with database.writer_connection() as db:
            await db.execute(statement, parameters)
            await db.commit()

    async def outbox_count(self):
        async with database.reader_connection() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM notification_events WHERE status IN ('pending', 'sending')"
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def enqueue(self, *, is_price_drop=False, old_price=None):
        return await scheduler.enqueue_notification_task(
            scheduler.NotificationTask(
                self.user_id,
                self.radar_id,
                "Test",
                self.item,
                is_price_drop=is_price_drop,
                old_price=old_price,
                drop_percent=20 if is_price_drop else None,
            )
        )

    async def test_init_creates_backup_and_preserves_users_across_restarts(self):
        await self.execute("UPDATE users SET username = 'johnpeterson92' WHERE user_id = ?", (self.user_id,))
        await database.init_db()
        await database.init_db()
        users = await database.get_all_users()
        self.assertEqual([u["user_id"] for u in users], [self.user_id])
        self.assertEqual((await database.get_search(self.radar_id))["min_discount_percent"], 0)

    async def test_pre_migration_backup_and_legacy_outbox_migration(self):
        # Create a legacy pre-300 database in a separate file, then run init_db()
        legacy_db_path = Path(self.directory.name) / "legacy.db"
        conn = sqlite3.connect(legacy_db_path)
        conn.execute(
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, is_allowed INTEGER DEFAULT 1, tariff_plan TEXT DEFAULT 'pro')"
        )
        conn.execute("INSERT INTO users (user_id, username, is_allowed, tariff_plan) VALUES (777, 'legacy', 1, 'pro')")
        conn.execute(
            "CREATE TABLE searches (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT, url TEXT, min_discount_percent INTEGER DEFAULT 15)"
        )
        conn.execute("INSERT INTO searches (id, user_id, title, url, min_discount_percent) VALUES (1, 777, 'R1', 'https://www.avito.ru/s', 15)")
        conn.execute("INSERT INTO searches (id, user_id, title, url, min_discount_percent) VALUES (2, 777, 'R2', 'https://www.avito.ru/s', 20)")
        conn.execute(
            "CREATE TABLE seen_items (search_id INTEGER, item_id TEXT, last_price INTEGER, PRIMARY KEY (search_id, item_id))"
        )
        conn.execute(
            """CREATE TABLE notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                event_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0,
                leased_until REAL,
                lease_token TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(search_id, item_id, event_key)
            )"""
        )
        payload1 = json.dumps({"user_id": 777, "radar_id": 1, "search_title": "R1", "item": self.item.__dict__, "discovered_at": time.time()})
        payload2 = json.dumps({"user_id": 777, "radar_id": 2, "search_title": "R2", "item": self.item.__dict__, "discovered_at": time.time()})
        conn.execute(
            "INSERT INTO notification_outbox (search_id, user_id, item_id, event_key, payload) VALUES (1, 777, '101', 'new', ?)",
            (payload1,),
        )
        conn.execute(
            "INSERT INTO notification_outbox (search_id, user_id, item_id, event_key, payload) VALUES (2, 777, '101', 'new', ?)",
            (payload2,),
        )
        conn.execute(
            "INSERT INTO notification_outbox (search_id, user_id, item_id, event_key, payload) VALUES (1, 777, '101', 'drop:15000:10000', ?)",
            (payload1,),
        )
        conn.commit()
        conn.close()

        with patch.object(database, "DB_PATH", legacy_db_path):
            await database.init_db()
            # Pre-migration backup file must exist
            backups = list(Path(self.directory.name).glob("legacy.pre_migration_*.bak"))
            self.assertEqual(len(backups), 1)
            # Only 1 deduplicated user event created for item '101', aggregating radars R1 and R2; 'drop:*' discarded
            claimed = await database.claim_notification_event()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["user_id"], 777)
            self.assertEqual(claimed["item_id"], "101")
            self.assertEqual(sorted(claimed["search_ids"]), [1, 2])
            self.assertEqual(sorted(claimed["radar_titles"]), ["R1", "R2"])
            self.assertEqual((await database.get_search(1))["min_discount_percent"], 0)

    async def test_paid_subscription_expires_even_with_legacy_allowed_flag(self):
        await database.grant_subscription(self.user_id, days=30, tariff_plan="pro")
        self.assertTrue(await database.is_user_allowed(self.user_id))
        await self.execute("UPDATE users SET subscription_until = datetime('now', '-1 second') WHERE user_id = ?", (self.user_id,))
        self.assertFalse(await database.is_user_allowed(self.user_id))
        self.assertEqual(await database.get_all_active_searches(), [])
        self.assertEqual((await database.get_system_stats())["allowed_users"], 0)
        with self.assertRaises(PermissionError):
            await database.add_search(self.user_id, "Forbidden", "https://www.avito.ru/forbidden")

    async def test_revoke_stops_trial_and_does_not_reactivate_it(self):
        self.assertTrue(await database.is_user_allowed(self.user_id))
        await database.set_user_allowed(self.user_id, 0)
        await database.get_or_create_user(self.user_id)
        self.assertFalse(await database.is_user_allowed(self.user_id))
        self.assertFalse((await database.activate_trial(self.user_id))["success"])
        await database.set_user_allowed(self.user_id, 1)
        self.assertTrue(await database.is_user_allowed(self.user_id))

    async def test_stop_words_require_ownership(self):
        self.assertFalse(await database.add_stop_word_to_search(self.radar_id, "foreign", self.user_id + 1))
        self.assertTrue(await database.add_stop_word_to_search(self.radar_id, "owned", self.user_id))
        self.assertFalse(await database.remove_stop_word_from_search(self.radar_id, "owned", self.user_id + 1))
        self.assertEqual((await database.get_search(self.radar_id))["stop_words"], ["owned"])

    async def test_concurrent_radar_creation_cannot_exceed_quota(self):
        await database.delete_search(self.radar_id, self.user_id)
        results = await asyncio.gather(
            *[
                database.add_search(self.user_id, f"Radar {i}", f"https://www.avito.ru/search?q={i}")
                for i in range(4)
            ],
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, int) for result in results), 1)
        self.assertEqual(sum(isinstance(result, database.RadarLimitError) for result in results), 3)
        self.assertEqual(await database.count_user_searches(self.user_id), 1)

    async def test_enqueue_is_persistent_deduplicated_and_not_seen(self):
        self.assertTrue(await self.enqueue())
        self.assertFalse(await self.enqueue())
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.assertEqual(await database.get_pending_new_item_ids(self.radar_id), {self.item.item_id})
        await database.init_db()
        self.assertEqual(await self.outbox_count(), 1)
        self.assertIsNotNone(await database.claim_notification(now=time.time() + 5))

    async def test_outbox_metrics_include_leased_and_queue_age(self):
        metrics0 = await database.get_outbox_metrics()
        self.assertEqual(metrics0["pending"], 0)
        self.assertEqual(metrics0["leased"], 0)
        await self.enqueue()
        ten_min_ago = time.time() - 600
        await self.execute("UPDATE notification_events SET queued_at = ?, deliver_at = ?", (ten_min_ago, ten_min_ago))
        await database.claim_notification()
        metrics = await database.get_outbox_metrics()
        self.assertEqual(metrics["pending"], 1)
        self.assertEqual(metrics["leased"], 1)
        self.assertGreaterEqual(metrics["oldest_seconds"], 600)

    async def test_claim_is_atomic_and_stale_lease_cannot_acknowledge(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await self.enqueue()
        results = await asyncio.gather(database.claim_notification(), database.claim_notification())
        leases = [result for result in results if result is not None]
        self.assertEqual(len(leases), 1)
        stale = leases[0]
        current = await database.claim_notification(lease_seconds=0)
        self.assertNotEqual(stale["lease_token"], current["lease_token"])
        self.assertFalse(await database.acknowledge_notification(stale["id"], stale["lease_token"], 10000))
        self.assertTrue(await database.acknowledge_notification(current["id"], current["lease_token"], 10000))
        self.assertEqual(await self.outbox_count(), 0)

    async def test_delivery_failure_keeps_item_pending_and_unseen(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await self.enqueue()
        row = await database.claim_notification()
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock(return_value=False)):
            self.assertFalse(await scheduler.deliver_notification(self.bot, None, row))
        self.assertEqual(await self.outbox_count(), 1)
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.assertIsNone(await database.claim_notification())

    async def test_success_marks_seen_after_telegram_acknowledgment(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await self.enqueue()
        row = await database.claim_notification()

        async def send(*args, **kwargs):
            self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
            return True

        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock(side_effect=send)):
            self.assertTrue(await scheduler.deliver_notification(self.bot, None, row))
        self.assertEqual(await self.outbox_count(), 0)
        self.assertEqual(await database.get_seen_items_with_prices(self.radar_id), {self.item.item_id: 10000})

    async def test_price_drop_tasks_are_ignored_and_never_enqueued(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await database.mark_item_seen(self.radar_id, self.item.item_id, 12500)
        self.assertFalse(await self.enqueue(is_price_drop=True, old_price=12500))
        self.assertEqual(await self.outbox_count(), 0)

    async def test_user_level_deduplication_and_radar_aggregation(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        second_radar = await database.add_search(self.user_id, "Second Radar", "https://www.avito.ru/search?q=2")
        r1 = await database.get_search(self.radar_id)
        r2 = await database.get_search(second_radar)
        parser = Mock()
        parser.fetch_page_async = AsyncMock(return_value="<html>fixture</html>")
        parser.parse_items.return_value = [self.item]

        self.assertTrue(await scheduler.process_url_group(self.bot, parser, r1["url"], [r1, r2]))
        self.assertEqual(await self.outbox_count(), 1)
        row = await database.claim_notification()
        self.assertIsNotNone(row)
        self.assertEqual(sorted(row["search_ids"]), sorted([self.radar_id, second_radar]))
        self.assertEqual(sorted(row["radar_titles"]), ["Second Radar", "Test"])

        # After delivery, both radars have the item marked seen and a subsequent scan never re-sends it
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock(return_value=True)) as send_mock:
            self.assertTrue(await scheduler.deliver_notification(self.bot, None, row))
            send_mock.assert_awaited_once()
        self.assertTrue(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.assertTrue(await database.is_item_seen(second_radar, self.item.item_id))
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, r1["url"], [r1, r2]))
        self.assertEqual(await self.outbox_count(), 0)

    async def test_startup_crash_recovery_flags_interrupted_send(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await self.enqueue()
        row = await database.claim_notification()
        await database.mark_event_send_started(row["id"], row["lease_token"])
        stats = await database.recover_outbox_on_startup()
        self.assertEqual(stats["requeued"], 1)
        self.assertEqual(stats["flagged_possible_duplicate"], 1)
        recovered = await database.claim_notification()
        self.assertTrue(recovered["possible_duplicate_after_crash"])

    async def test_purge_requires_exact_user_and_protects_admins(self):
        self.assertFalse((await database.purge_user_by_pattern("fix"))["success"])
        self.assertFalse((await database.purge_user_by_pattern("%"))["success"])
        await database.get_or_create_user(database.ADMIN_IDS[0])
        self.assertFalse((await database.purge_user_by_pattern(str(database.ADMIN_IDS[0])))["success"])
        self.assertTrue(await database.is_user_allowed(self.user_id))

    async def test_trial_activation_is_atomic(self):
        await self.execute("UPDATE users SET trial_used = 0, subscription_until = NULL WHERE user_id = ?", (self.user_id,))
        results = await asyncio.gather(*[database.activate_trial(self.user_id) for _ in range(4)])
        self.assertEqual(sum(result["success"] for result in results), 1)

    async def test_pipeline_priority_and_cancelled_waiters(self):
        urgent, pipeline = asyncio.Queue(), asyncio.Queue()
        await pipeline.put("normal")
        await urgent.put("urgent")
        self.assertEqual(await scheduler.get_next_pipeline_task(urgent, pipeline), ("urgent", True))
        self.assertEqual(await scheduler.get_next_pipeline_task(urgent, pipeline), ("normal", False))
        task = asyncio.create_task(scheduler.get_next_pipeline_task(urgent, pipeline))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await pipeline.put("not-stolen")
        await asyncio.sleep(0)
        self.assertEqual(pipeline.qsize(), 1)

    async def test_retry_after_preserves_notification(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await self.enqueue()
        row = await database.claim_notification()
        error = TelegramRetryAfter(method=SendMessage(chat_id=self.user_id, text="test"), message="rate limited", retry_after=30)
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock(side_effect=error)):
            self.assertFalse(await scheduler.deliver_notification(self.bot, None, row))
        self.assertEqual(await self.outbox_count(), 1)
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.assertIsNone(await database.claim_notification())

    async def test_cancelled_delivery_releases_lease(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await self.enqueue()
        row = await database.claim_notification()
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler.deliver_notification(self.bot, None, row)
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.assertIsNotNone(await database.claim_notification())

    async def test_revoked_or_deleted_radar_cannot_receive_pending_notifications(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await self.enqueue()
        row = await database.claim_notification()
        await database.set_user_allowed(self.user_id, 0)
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock()) as send:
            self.assertFalse(await scheduler.deliver_notification(self.bot, None, row))
            send.assert_not_called()
        self.assertEqual(await self.outbox_count(), 0)
        await database.set_user_allowed(self.user_id, 1)
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await self.enqueue()
        await database.delete_search(self.radar_id, self.user_id)
        self.assertEqual(await self.outbox_count(), 0)

    async def test_pipeline_enqueues_once_without_premarking_deliverable_items(self):
        radar = await database.get_search(self.radar_id)
        parser = Mock()
        parser.fetch_page_async = AsyncMock(return_value="<html>fixture</html>")
        parser.parse_items.return_value = [self.item]
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertEqual(await self.outbox_count(), 1)

    async def test_deleted_radar_cannot_send_after_rate_wait(self):
        self.item.image_url = "https://c80.img.avito.st/offline-fixture"
        self.bot.send_photo = AsyncMock()
        self.bot.send_message = AsyncMock()

        async def rate_wait():
            await database.delete_search(self.radar_id, self.user_id)

        with patch.object(scheduler, "get_cached_photo_file_id", return_value=None), patch.object(
            scheduler, "limit_notification_send_rate", new=rate_wait
        ):
            self.assertFalse(
                await scheduler.send_ad_to_user(self.bot, None, self.user_id, self.radar_id, "Test", self.item)
            )
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_lot_without_cached_photo_goes_out_with_photo_by_url(self):
        self.item.image_url = "https://c80.img.avito.st/test.jpg"
        self.bot.send_photo = AsyncMock()
        self.bot.send_message = AsyncMock()
        with patch.object(scheduler, "get_cached_photo_file_id", return_value=None), patch.object(
            scheduler, "download_image_bytes", new=AsyncMock()
        ) as download, patch.object(scheduler, "limit_notification_send_rate", new=AsyncMock()):
            self.assertTrue(
                await scheduler.send_ad_to_user(self.bot, None, self.user_id, self.radar_id, "Test", self.item)
            )
        download.assert_not_awaited()
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_verified_notification_survives_queue_age_and_changed_age_limit(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await self.enqueue()
        await database.update_search_max_age(self.radar_id, self.user_id, 1)
        row = await database.claim_notification()
        with patch.object(scheduler.time, "time", return_value=time.time() + 600), patch.object(
            scheduler, "send_ad_to_user", new=AsyncMock(return_value=True)
        ) as send:
            self.assertTrue(await scheduler.deliver_notification(self.bot, None, row))
        send.assert_awaited_once()
        self.assertTrue(await database.is_item_seen(self.radar_id, self.item.item_id))

    async def test_legacy_notification_is_validated_at_enqueue_time(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await self.enqueue()
        row = await database.claim_notification()
        payload = json.loads(row["payload"])
        payload.pop("freshness_verified", None)
        payload.pop("discovered_at", None)
        row["payload"] = json.dumps(payload)
        row["payload_json"] = row["payload"]
        with patch.object(scheduler.time, "time", return_value=time.time() + 600), patch.object(
            scheduler, "send_ad_to_user", new=AsyncMock(return_value=True)
        ):
            self.assertTrue(await scheduler.deliver_notification(self.bot, None, row))

    async def test_unknown_age_is_not_marked_seen_and_can_be_delivered_later(self):
        radar = await database.get_search(self.radar_id)
        self.item.date, self.item.timestamp = "Дата не указана", None
        parser = Mock()
        parser.fetch_page_async = AsyncMock(return_value="<html></html>")
        parser.parse_items.return_value = [self.item]
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertEqual(await self.outbox_count(), 0)
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.item.date = "2 мин. назад"
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertEqual(await self.outbox_count(), 1)

    async def test_atomic_initial_snapshot_preserves_unknown_price(self):
        await database.delete_search(self.radar_id, self.user_id)
        radar_id = await database.add_search(
            self.user_id, "Atomic", "https://www.avito.ru/search", initial_items=[("old", None), ("free", 0)]
        )
        radar = await database.get_search(radar_id)
        self.assertIsNotNone(radar["initialized_at"])
        async with database.reader_connection() as db:
            async with db.execute("SELECT item_id, last_price FROM seen_items WHERE search_id = ?", (radar_id,)) as cursor:
                self.assertEqual(dict(await cursor.fetchall()), {"old": None, "free": 0})

    async def test_empty_deferred_baseline_initializes_only_once(self):
        await self.execute("UPDATE searches SET initialized_at = NULL WHERE id = ?", (self.radar_id,))
        radar = await database.get_search(self.radar_id)
        parser = Mock()
        parser.fetch_page_async = AsyncMock(return_value="<html></html>")
        parser.parse_items.return_value = []
        parser.has_catalog.return_value = True
        parser.next_page_url.return_value = None
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        radar = await database.get_search(self.radar_id)
        self.assertIsNotNone(radar["initialized_at"])
        parser.parse_items.return_value = [self.item]
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertEqual(await self.outbox_count(), 1)

    async def test_pending_baseline_survives_repeated_migration(self):
        await self.execute("UPDATE searches SET initialized_at = NULL WHERE id = ?", (self.radar_id,))
        await database.init_db()
        self.assertIsNone((await database.get_search(self.radar_id))["initialized_at"])

    async def test_legacy_unknown_age_returns_to_discovery_without_seen_state(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        self.item.timestamp = None
        self.item.date = "Дата не указана"
        await self.enqueue()
        row = await database.claim_notification()
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock()) as send:
            self.assertFalse(await scheduler.deliver_notification(self.bot, None, row))
        send.assert_not_called()
        self.assertEqual(await self.outbox_count(), 0)
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.item.date = "только что"
        radar = await database.get_search(self.radar_id)
        parser = Mock()
        parser.fetch_page_async = AsyncMock(return_value="<html></html>")
        parser.parse_items.return_value = [self.item]
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertEqual(await self.outbox_count(), 1)

    async def test_send_message_floodwait_reaches_persistent_outbox(self):
        self.bot.send_message = AsyncMock(
            side_effect=TelegramRetryAfter(
                method=SendMessage(chat_id=self.user_id, text="test"), message="rate limited", retry_after=10
            )
        )
        with patch.object(scheduler, "get_cached_photo_file_id", return_value=None), patch.object(
            scheduler, "limit_notification_send_rate", new=AsyncMock()
        ):
            with self.assertRaises(TelegramRetryAfter):
                await scheduler.send_ad_to_user(self.bot, None, self.user_id, self.radar_id, "Test", self.item)
        self.bot.send_message.assert_awaited_once()

    async def test_pro_downtime_catchup_caps_at_10_items_and_marks_rest_seen(self):
        from collector import SourceCollection
        from parser import AvitoItem

        await database.grant_subscription(self.user_id, tariff_plan="pro")
        radar = await database.get_search(self.radar_id)
        now = time.time()
        # 15 items published 6..20 minutes ago (older than 5m normal limit, younger than 30m catch-up limit)
        items = [
            AvitoItem(
                item_id=f"catchup_{i}",
                title=f"Catchup Item {i}",
                price="10 000 ₽",
                url=f"https://www.avito.ru/item_{i}",
                date=f"{6 + i} мин. назад",
                timestamp=int(now - (6 + i) * 60),
                raw_price=10000,
                t_pub_reliable=True,
            )
            for i in range(15)
        ]
        collection = SourceCollection(
            title="Catchup Fixture",
            items=tuple(items),
            pages=2,
            coverage_gap=False,
            elapsed_ms=120,
            captured_at=now,
            discovery_times={it.item_id: now for it in items},
            top_ids=(items[0].item_id, items[1].item_id, items[2].item_id),
            fast_skipped=False,
        )
        # Source was last processed 25 minutes ago (> 5m max_age_minutes)
        ok = await scheduler.process_collected_group_fanout(
            radar["url"],
            [radar],
            collection,
            previous_processed_at=now - 1500,
        )
        self.assertTrue(ok)
        # Exactly 10 newest items enqueued; 5 oldest catch-up items immediately marked seen
        self.assertEqual(await self.outbox_count(), 10)
        seen_map = await database.get_seen_items_with_prices(self.radar_id)
        self.assertEqual(len(seen_map), 5)
        for i in range(10, 15):
            self.assertIn(f"catchup_{i}", seen_map)

        claimed = await database.claim_notification_event()
        self.assertEqual(claimed["is_catchup"], 1)

    async def test_50_chat_fanout_delivery_within_sla(self):
        from parser import AvitoItem

        now = time.time()
        candidates = []
        for idx in range(50):
            uid = 500000 + idx
            await database.get_or_create_user(uid, username=f"buyer_{idx}", first_name=f"Buyer {idx}")
            await database.grant_subscription(uid, 30, "pro")
            rid = await database.add_search(
                uid, f"Radar {idx}", "https://www.avito.ru/moskva/telefony?s=104", initial_items=[("seed", 1)]
            )
            it = AvitoItem(
                item_id="hot_lot_50",
                title="iPhone 16 Pro 256GB",
                price="85 000 ₽",
                url="https://www.avito.ru/hot_lot_50",
                date="только что",
                timestamp=int(now),
                raw_price=85000,
                t_pub_reliable=True,
            )
            task_dict = scheduler.asdict(
                scheduler.NotificationTask(
                    user_id=uid,
                    radar_id=rid,
                    search_title=f"Radar {idx}",
                    item=it,
                    discovered_at=now,
                    freshness_verified=True,
                )
            )
            candidates.append(
                {
                    "user_id": uid,
                    "radar_id": rid,
                    "radar_title": f"Radar {idx}",
                    "item_id": "hot_lot_50",
                    "raw_price": 85000,
                    "tariff": "pro",
                    "priority": 10,
                    "delay_seconds": 0.0,
                    "t_seen": now,
                    "t_pub": int(now),
                    "t_pub_reliable": True,
                    "is_catchup": False,
                    "payload_dict": task_dict,
                }
            )

        enqueued = await database.enqueue_group_events(candidates, [])
        self.assertEqual(len(enqueued), 50)

        sent_chats = []
        all_done = asyncio.Event()

        async def fast_send_message(chat_id, text, **kwargs):
            sent_chats.append((chat_id, time.monotonic()))
            if len(sent_chats) >= 50:
                all_done.set()
            return Mock(message_id=len(sent_chats))

        self.bot.send_message = AsyncMock(side_effect=fast_send_message)
        scheduler.reset_notification_rate_limiter()
        scheduler.notify_outbox_wakeup()

        t_start = time.monotonic()
        workers = [
            asyncio.create_task(scheduler.notification_worker(self.bot, None, worker_id=w))
            for w in range(1, 9)
        ]
        try:
            await asyncio.wait_for(all_done.wait(), timeout=4.0)
            elapsed = time.monotonic() - t_start
            # The last sends are acknowledged in the outbox right after Telegram returns: let them land
            # before cancelling the workers, or a cancelled ack leaves a row behind.
            for _ in range(40):
                if await self.outbox_count() == 0:
                    break
                await asyncio.sleep(0.05)
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        self.assertEqual(len(sent_chats), 50)
        self.assertEqual(len({cid for cid, _ in sent_chats}), 50)
        # 50 distinct chats at 25 calls/s global rate limit must complete in ~2.0-2.9s (<= 3.0s SLA)
        self.assertLessEqual(elapsed, 3.0)
        self.assertEqual(await self.outbox_count(), 0)


if __name__ == "__main__":
    unittest.main()
