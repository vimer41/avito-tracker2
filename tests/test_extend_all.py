import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment  # noqa: F401
from tests._dispatcher import DispatcherFixture

import config
import database
import reports
from handlers import admin


class TestExtendDuration(unittest.TestCase):
    def test_parses_units_and_plain_hours(self):
        self.assertEqual(admin.parse_extend_duration("2ч"), 7200)
        self.assertEqual(admin.parse_extend_duration("30 мин"), 1800)
        self.assertEqual(admin.parse_extend_duration("1д 6ч"), 86400 + 6 * 3600)
        self.assertEqual(admin.parse_extend_duration("3"), 3 * 3600)
        for bad in ("", "abc", "2ч ерунда", "0", "31д", "5 лет"):
            with self.assertRaises(ValueError, msg=bad):
                admin.parse_extend_duration(bad)

    def test_formats_duration(self):
        self.assertEqual(admin.format_extend_duration(86400 + 2 * 3600 + 60), "1 день 2 часа 1 минуту")
        self.assertEqual(admin.format_extend_duration(1800), "30 минут")


class TestExtendAll(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-extend-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        self.paid, self.trial, self.expired, self.fresh = 900001, 900002, 900003, 900004
        for uid in (self.paid, self.trial, self.expired, self.fresh):
            await database.get_or_create_user(uid)
        await database.grant_subscription(self.paid, 10, "pro")
        await database.activate_trial(self.trial)
        await database.grant_subscription(self.expired, 1, "lite")
        async with database.writer_connection() as db:
            await db.execute("UPDATE users SET subscription_until = datetime('now', '-1 hour') WHERE user_id = ?", (self.expired,))
            await db.commit()
        self.setup_dispatcher()

    async def asyncTearDown(self):
        await self.close_dispatcher()
        self.db_patch.stop()
        self.directory.cleanup()

    async def until(self, uid):
        return (await database.get_user(uid))["subscription_until"]

    async def test_only_running_access_is_extended(self):
        before = {uid: await self.until(uid) for uid in (self.paid, self.trial, self.expired)}
        granted = await database.extend_active_subscriptions(7200)
        self.assertEqual({uid for uid, _ in granted}, {self.paid, self.trial})
        for uid in (self.paid, self.trial):
            added = reports._until_epoch(await self.until(uid)) - reports._until_epoch(before[uid])
            self.assertEqual(added, 7200)
        self.assertEqual(await self.until(self.expired), before[self.expired])
        self.assertIsNone(await self.until(self.fresh))
        trial_user = await database.get_user(self.trial)
        self.assertEqual(trial_user["is_allowed"], 0)  # a trial stays a trial

    async def test_admin_flow_extends_and_notifies_with_own_text(self):
        admin_id = config.ADMIN_ID
        before = await self.until(self.paid)
        await self.dispatch_callback("admin_extend_all", admin_id)
        await self.dispatch_message(text="2ч", user_id=admin_id)
        await self.dispatch_message(text="Техработы с 14 до 16 <b>", user_id=admin_id)
        self.assertIn("admin_extend_go", self.callback_values())
        with patch("asyncio.sleep", new=_no_sleep):
            await self.dispatch_callback("admin_extend_go", admin_id)
        self.assertNotEqual(await self.until(self.paid), before)
        notices = [m for m in self.methods("SendMessage") if m.chat_id in (self.paid, self.trial)]
        self.assertEqual({m.chat_id for m in notices}, {self.paid, self.trial})
        self.assertTrue(all("Техработы с 14 до 16 &lt;b&gt;" in m.text and "2 часа" in m.text for m in notices))
        self.assertFalse([m for m in self.methods("SendMessage") if m.chat_id == self.expired])

        # A second press after the state was cleared must not extend again.
        after = await self.until(self.paid)
        await self.dispatch_callback("admin_extend_go", admin_id)
        self.assertEqual(await self.until(self.paid), after)

    async def test_non_admin_cannot_extend(self):
        before = await self.until(self.paid)
        await self.dispatch_callback("admin_extend_go", self.paid)
        self.assertEqual(await self.until(self.paid), before)


async def _no_sleep(*_args, **_kwargs):
    return None


if __name__ == "__main__":
    unittest.main()


class TestAccessReturn(unittest.IsolatedAsyncioTestCase):
    """A user who comes back after the access ran out gets no catch-up of old lots."""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-return-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        self.uid = 900010
        await database.get_or_create_user(self.uid)
        await database.grant_subscription(self.uid, 1, "lite")
        self.radar = await database.add_search(self.uid, "Радар", "https://www.avito.ru/moskva?q=x&s=104")
        async with database.writer_connection() as db:
            await db.execute("UPDATE searches SET initialized_at = datetime('now') WHERE id = ?", (self.radar,))
            await db.commit()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    async def expire(self):
        async with database.writer_connection() as db:
            await db.execute("UPDATE users SET subscription_until = datetime('now', '-1 hour') WHERE user_id = ?", (self.uid,))
            await db.commit()

    async def initialized(self):
        return (await database.get_search(self.radar))["initialized_at"]

    async def test_renewal_while_active_keeps_radar_state(self):
        await database.grant_subscription(self.uid, 30, "lite")
        self.assertIsNotNone(await self.initialized())

    async def test_payment_after_expiry_recalibrates(self):
        await self.expire()
        await database.grant_subscription(self.uid, 30, "lite")
        self.assertIsNone(await self.initialized())

    async def test_vip_after_expiry_recalibrates(self):
        await self.expire()
        await database.set_user_allowed(self.uid, 1)
        self.assertIsNone(await self.initialized())

    async def test_expired_radars_are_not_counted_as_active(self):
        self.assertEqual((await database.get_system_stats())["active_searches"], 1)
        await self.expire()
        self.assertEqual((await database.get_system_stats())["active_searches"], 0)
