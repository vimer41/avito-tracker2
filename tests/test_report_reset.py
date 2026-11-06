"""«Начать замер заново»: reports count from the mark, live history starts empty, log files stay."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment  # noqa: F401
from tests.test_admin_tools import USER_ID, AdminToolsBase
from tests.test_load_report import _good, _minute
import config
import database
import log_report
import scan_journal
from runtime_metrics import METRICS


class TestStartOverButton(AdminToolsBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        patcher = patch.object(config, "REPORT_SINCE", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_start_over_marks_reports_and_clears_live_history(self):
        scan_journal.record_scan([5], ok=True, at=time.time(), ms=100, channel="direct")
        scan_journal.record_decisions(5, time.time(), {"4000000001": ("queued", "")})
        METRICS.record_scan_attempt(success=True, elapsed_ms=1000)

        await self.dispatch_callback("admin_hist_reset", config.ADMIN_ID)
        self.assertIn("Начать замер заново?", self.texts()[-1])
        self.assertIn("Файлы логов не удаляются", self.texts()[-1])
        await self.dispatch_callback("admin_hist_reset_yes", config.ADMIN_ID)

        self.assertGreater(config.REPORT_SINCE, time.time() - 10)
        self.assertEqual(await database.get_bot_setting("report_since"), f"{config.REPORT_SINCE:.0f}")
        self.assertEqual(scan_journal.scans(5), [])
        self.assertTrue(scan_journal.decisions("4000000001"), "«why did the lot not come» keeps its answers")
        self.assertEqual(METRICS.snapshot()["scan_pipeline"]["scans_completed"], 0)
        self.assertIn("Замер идёт с", self.texts()[-1])

        await self.dispatch_callback("admin_hist_all", config.ADMIN_ID)
        self.assertEqual(config.REPORT_SINCE, 0.0)
        self.assertEqual(await database.get_bot_setting("report_since"), "0")
        self.assertNotIn("Замер идёт с", self.texts()[-1])

    async def test_only_admins_start_over(self):
        await self.dispatch_callback("admin_hist_reset_yes", USER_ID)
        self.assertEqual(config.REPORT_SINCE, 0.0)


class TestReportsFromTheMark(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="avito-mark-")
        self.addCleanup(directory.cleanup)
        self.metrics_log = Path(directory.name) / "metrics.jsonl"
        for name, value in (("METRICS_LOG", self.metrics_log), ("EVENTS_LOG", Path(directory.name) / "events.jsonl"),
                            ("CRASH_LOG", Path(directory.name) / "crash.log")):
            patcher = patch.object(log_report, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_load_and_readiness_count_from_the_mark(self):
        now = time.time()
        since = now - 600
        channels = {"1.1.1.1:5500": _good()}
        old = [_minute(now - 3000 + 60 * i, channels, scans_ok=10, busy=15.0, queue=40.0) for i in range(10)]
        new = [_minute(since + 10 + 60 * i, channels, scans_ok=60, busy=3.0, queue=0.0) for i in range(10)]
        self.metrics_log.write_text("".join(json.dumps(row) + "\n" for row in old + new), encoding="utf-8")

        load = log_report.build_load(60, since=since)
        self.assertIn("Минут с замером нагрузки: 10 из 10", load)
        self.assertIn("Отсчёт с", load)
        self.assertIn("Минут с замером нагрузки: 20 из 20", log_report.build_load(60))

        ready = log_report.build_readiness(24, since=since)
        self.assertIn("✅ Работа без перерывов: 100.0% времени", ready, "uptime is measured from the mark")
        self.assertIn("Отсчёт с", ready)
        self.assertIn("Минутных строк: 10", log_report.build_report(60, since=since))


if __name__ == "__main__":
    unittest.main()
