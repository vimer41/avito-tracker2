import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import log_report
from runtime_metrics import RuntimeMetricsRegistry


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestLoadMetrics(unittest.TestCase):
    def test_minute_row_carries_channel_load_and_worker_use(self):
        clock = Clock()
        metrics = RuntimeMetricsRegistry(monotonic_fn=clock, wall_clock_fn=lambda: 1_700_000_000.0)
        url = "http://***@1.2.3.4:5500"
        metrics.record_http_request("p1", url, 200, 2.5, size_bytes=80 * 1024, ttfb_seconds=0.6)
        metrics.record_http_request("p1", url, 403, 0.4, size_bytes=2048)
        metrics.record_http_request("p1", url, None, 0.0, is_transport_error=True)
        metrics.record_transport_time("p1", url, 11.0)
        metrics.record_pacing("p1", url, 0.3)
        metrics.record_channel_use("p1", url, wait_seconds=1.2, busy_seconds=14.5)
        metrics.record_channel_use("p1", url, wait_seconds=0.0, busy_seconds=1.0)
        metrics.record_scan_attempt(success=True, elapsed_ms=3000, proxy_id="p1", masked_url=url, channels_tried=1)
        metrics.record_scan_attempt(success=False, elapsed_ms=12000, proxy_id="p1", masked_url=url, channels_tried=2)
        metrics.set_planned_scan_rate(5.0)
        for busy in (14, 15):
            metrics.sample_workers(total=15, busy=busy, queued=7, oldest_due=40.0)
        clock.now += 60
        row = metrics.pop_minute_rows()[-1]

        channel = row["ch"]["1.2.3.4:5500"]
        self.assertEqual((channel["http"], channel["terr"], channel["pages"]), (3, 1, 1))
        self.assertEqual((channel["fetch"], channel["waited"], channel["busy_s"], channel["wait_s"]), (2, 1, 15.5, 1.2))
        self.assertEqual((channel["kb"], channel["page_kb"], channel["ttfb_ms"]), (82, 80, 600))
        self.assertEqual((channel["terr_s"], channel["pace_s"], channel["http_s"]), (11.0, 0.3, 2.9))
        self.assertEqual(channel["lat_h"], {"3": 1})
        self.assertEqual(channel["ms_p50"], 500, "a broken request has no latency and must not pull p50 to zero")
        load = row["load"]
        self.assertEqual((load["plan_sps"], load["workers"], load["busy"], load["busy_max"]), (5.0, 15.0, 14.5, 15))
        self.assertEqual((load["queue"], load["queue_max"], load["oldest_s"]), (7.0, 7, 40.0))
        self.assertEqual((load["tries"], load["scan_busy_s"]), ({"1": 1, "2": 1}, 15.0))
        self.assertEqual(load["scan_h"], {"5": 1, "15": 1})

    def test_idle_channel_keeps_its_row_small(self):
        clock = Clock()
        metrics = RuntimeMetricsRegistry(monotonic_fn=clock, wall_clock_fn=lambda: 1_700_000_000.0)
        metrics.record_http_request("p1", "http://***@1.2.3.4:5500", None, 0.0, is_transport_error=True)
        metrics.sample_workers(total=1, busy=0, queued=0, oldest_due=0.0)
        clock.now += 60
        channel = metrics.pop_minute_rows()[-1]["ch"]["1.2.3.4:5500"]
        self.assertFalse({"busy_s", "kb", "lat_h", "ttfb_ms", "fetch"} & set(channel))


def _minute(ts: float, channels: dict, *, scans_ok: int, scans_fail: int = 0, busy: float, queue: float) -> dict:
    return {
        "t": round(ts), "ts": time.strftime("%m-%d %H:%M", time.gmtime(ts + 3 * 3600)),
        "scans": {"ok": scans_ok, "fail": scans_fail, "fast": 0, "new": 0, "gap": 0},
        "fail": {"forbidden": scans_fail} if scans_fail else {},
        "ch": channels,
        "urls": 100, "radars": 102, "rps_need": 6.5, "rps_have": 7.5, "channels": "4/4",
        "cpu_pct": 22.0, "sys_cpu": 30.0, "steal": 0.0, "iowait": 0.0, "rss_mb": 300.0, "mem_avail_mb": 2500,
        "loop_max_ms": 120, "stalls": 0,
        "load": {"plan_sps": 5.0, "scan_busy_s": 870.0, "scan_h": {"15": scans_ok}, "due_h": {"120": scans_ok},
                 "revisit_h": {"120": scans_ok}, "tries": {"1": scans_ok}, "workers": 15.0, "busy": busy,
                 "busy_max": 15, "queue": queue, "queue_max": 60, "oldest_s": 150.0},
    }


def _good(http: int = 20) -> dict:
    return {"http": http, "ok": http - 1, "403": 0, "429": 0, "5xx": 0, "terr": 1, "coll": http - 1, "coll_ok": http - 1,
            "fetch": http, "busy_s": 58.0, "wait_s": 20.0, "waited": 10, "http_s": 50.0, "terr_s": 8.0,
            "kb": 1500, "pages": http - 1, "page_kb": 1450, "ttfb_ms": 700, "lat_h": {"3": http - 1},
            "fail": {}, "state": "healthy", "rate": 1.5}


def _banned() -> dict:
    return {"http": 10, "ok": 3, "403": 6, "429": 1, "5xx": 0, "terr": 0, "coll": 8, "coll_ok": 2,
            "fetch": 8, "busy_s": 6.0, "http_s": 5.0, "pages": 3, "page_kb": 230, "ttfb_ms": 500, "lat_h": {"1": 3},
            "fail": {"forbidden": 6}, "state": "quarantined", "rate": 0.5}


class TestLoadReport(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="avito-load-")
        self.addCleanup(directory.cleanup)
        self.metrics_log = Path(directory.name) / "metrics.jsonl"
        patcher = patch.object(log_report, "METRICS_LOG", self.metrics_log)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, rows: list[dict]) -> None:
        self.metrics_log.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def test_saturated_channels_are_named_as_the_bottleneck_with_capacity(self):
        start = time.time() - 30 * 60
        channels = {"1.1.1.1:5500": _good(), "2.2.2.2:5500": _good(), "3.3.3.3:5500": _good(), "45.88.150.39:3000": _banned()}
        self.write([_minute(start + 60 * i, channels, scans_ok=60, busy=15.0, queue=40.0) for i in range(30)])

        report = log_report.build_load(60)

        self.assertIn("Минут с замером нагрузки: 30 из 30", report)
        self.assertIn("Сканов по плану: 20% (факт 1.00 из 5.00 в секунду)", report)
        self.assertIn("Упор в каналы: хорошие каналы заняты 97% времени", report)
        self.assertIn("Мощность: 3 хороших каналов дают 1.03 запр/с", report)
        # A scan costs 70/60 requests: the banned proxy's answers are spent too.
        self.assertIn("~18 ссылок при проверке раз в 20 с", report)
        self.assertIn("им нужно 5.83 запр/с, это ~17 таких каналов, с запасом 20% — 21", report)
        self.assertIn("Бот сам считает, что у него 7.5 запр/с", report)
        self.assertIn("45.88.150.39:3000: 10.0 · 10% · 0.0 с · 30% · 70% · 0% · ≤ 1000 мс/≤ 1000 мс · 500 мс · 77 · — · блок Авито", report)
        self.assertIn("1.1.1.1:5500: 20.0 · 97% · 1.0 с · 95% · 0% · 5% · ≤ 3000 мс/≤ 3000 мс · 700 мс · 76 · 0.34/с · хороший", report)
        self.assertIn("повторная проверка ссылки: p50 ≤ 120 с", report)
        rate_table = report.split("## Частота запросов на IP и блоки\n", 1)[1]
        self.assertIn("18–24 (до 0.40) · 18 · 1800 · 0% · 5%", rate_table)
        self.assertNotIn("45.88", rate_table.split("##", 1)[0], "a banned IP must not skew the rate curve")

    def test_old_rows_without_load_say_so(self):
        self.write([{"t": round(time.time() - 60), "ts": "11-06 20:00", "scans": {"ok": 1, "fail": 0}}])
        self.assertIn("Нет данных о нагрузке", log_report.build_load(60))


if __name__ == "__main__":
    unittest.main()
