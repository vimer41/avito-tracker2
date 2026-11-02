import sqlite3
import tempfile
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment

import config
import database
import monitoring


def radar(user_id, plan, *, allowed=1):
    return {"id": user_id, "user_id": user_id, "tariff_plan": plan, "is_allowed": allowed}


class TestTariffScanIntervals(unittest.TestCase):
    def test_each_tariff_scans_at_its_own_interval(self):
        for plan, expected in (("lite", 40), ("standart", 30), ("pro", 20)):
            interval = monitoring.source_interval([radar(5, plan)], "https://www.avito.ru/moskva?q=x")
            self.assertLessEqual(interval, expected)
            self.assertGreaterEqual(interval, expected * 0.9)

    def test_shared_url_uses_fastest_tariff_and_trial_speed(self):
        key = "https://www.avito.ru/moskva?q=shared"
        shared = monitoring.source_interval([radar(5, "lite"), radar(6, "pro")], key)
        self.assertEqual(shared, monitoring.source_interval([radar(6, "pro")], key))
        trial = monitoring.source_interval([radar(7, "standart", allowed=0)], key)
        self.assertEqual(trial, shared)

    def test_jitter_is_stable_per_url(self):
        radars = [radar(5, "standart")]
        first = monitoring.source_interval(radars, "https://www.avito.ru/a")
        self.assertEqual(first, monitoring.source_interval(radars, "https://www.avito.ru/a"))

    def test_prices_and_limits(self):
        self.assertEqual([config.stars_price(p) for p in config.PAID_PLANS], [199, 449, 899])
        self.assertEqual([config.plan_radars(p) for p in config.PAID_PLANS], [1, 2, 4])
        self.assertEqual(database.classify_user_tariff(radar(5, "lite"))[0], "lite")


class TestLitePlanStorage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tariffs.db"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def test_old_orders_table_accepts_lite_after_migration(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("""
                CREATE TABLE stars_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL,
                    tariff_plan TEXT NOT NULL CHECK(tariff_plan IN ('standart', 'pro', 'test')),
                    total_amount INTEGER NOT NULL CHECK(total_amount > 0), days INTEGER NOT NULL CHECK(days > 0),
                    status TEXT NOT NULL DEFAULT 'pending', pre_checkout_query_id TEXT, approved_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP NOT NULL,
                    telegram_payment_charge_id TEXT UNIQUE, provider_payment_charge_id TEXT, paid_at TIMESTAMP,
                    subscription_before TIMESTAMP, tariff_before TEXT, allowed_before INTEGER, trial_before INTEGER,
                    subscription_after TIMESTAMP, tariff_after TEXT, revision_after INTEGER,
                    refund_requested_at TIMESTAMP, refund_admin_id INTEGER, refunded_at TIMESTAMP,
                    refund_needs_review INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("INSERT INTO stars_orders(payload, user_id, tariff_plan, total_amount, days, expires_at) "
                         "VALUES ('old', 1, 'pro', 470, 30, datetime('now'))")
        await database.init_db()
        await database.get_or_create_user(4242, "u", "U")
        until = await database.grant_subscription(4242, 30, "lite")
        self.assertTrue(until)
        self.assertEqual(await database.get_user_max_radars(4242), 1)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT tariff_plan FROM stars_orders WHERE payload = 'old'").fetchone()[0], "pro")
            conn.execute("INSERT INTO stars_orders(payload, user_id, tariff_plan, total_amount, days, expires_at) "
                         "VALUES ('new', 4242, 'lite', 199, 30, datetime('now'))")


if __name__ == "__main__":
    unittest.main()
