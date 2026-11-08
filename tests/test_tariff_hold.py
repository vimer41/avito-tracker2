"""A slower tariff on a link shared with a faster one waits the gap between their scan intervals."""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment  # noqa: F401
import config
import database
import scheduler
from collector import SourceCollection
from parser import AvitoItem

URL = "https://www.avito.ru/moskva?q=hold"
INTERVALS = {"pro": 20.0, "standart": 30.0, "lite": 40.0, "base": 30.0}


class TariffHoldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-hold-", ignore_cleanup_errors=True)
        self.db_path = Path(self.directory.name) / "test.db"
        self.patches = [patch.object(database, "DB_PATH", self.db_path),
                        patch.object(config, "TARIFF_SCAN_INTERVAL", dict(INTERVALS))]
        for p in self.patches:
            p.start()
        await database.init_db()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.directory.cleanup()

    async def _radar(self, user_id: int, plan: str) -> dict:
        await database.get_or_create_user(user_id, f"u{user_id}", "Fixture")
        await database.grant_subscription(user_id, days=30, tariff_plan=plan)
        search_id = await database.add_search(user_id, f"Радар {plan}", URL)
        return await database.get_search(search_id)

    async def _fanout(self, radars: list[dict]) -> dict[int, float]:
        """Run one scan with a fresh lot; return user_id -> seconds the lot waits after it was found."""
        now = time.time()
        item = AvitoItem(item_id="777", title="iPhone", price="1 ₽", url="https://www.avito.ru/777", raw_price=1,
                         date="только что", timestamp=int(now), t_pub_reliable=True)
        collection = SourceCollection(
            title="Fixture", items=(item,), pages=1, coverage_gap=False, elapsed_ms=10, captured_at=now,
            discovery_times={item.item_id: now}, top_ids=(item.item_id,), fast_skipped=False,
        )
        self.assertTrue(await scheduler.process_collected_group_fanout(URL, radars, collection))
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT user_id, deliver_at - queued_at FROM notification_events").fetchall()
        finally:
            conn.close()
        return {int(user): round(float(wait), 1) for user, wait in rows}

    async def test_slower_tariffs_wait_the_interval_gap_next_to_polny(self):
        radars = [await self._radar(1, "pro"), await self._radar(2, "standart"), await self._radar(3, "lite")]
        waits = await self._fanout(radars)
        self.assertEqual(waits, {1: 0.0, 2: 10.0, 3: 20.0})

    async def test_bazovy_next_to_sredniy_waits_ten_seconds(self):
        waits = await self._fanout([await self._radar(2, "standart"), await self._radar(3, "lite")])
        self.assertEqual(waits, {2: 0.0, 3: 10.0})

    async def test_alone_on_the_link_there_is_no_wait(self):
        waits = await self._fanout([await self._radar(3, "lite")])
        self.assertEqual(waits, {3: 0.0})


if __name__ == "__main__":
    unittest.main()
