import asyncio
import gzip
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import config
import database
import log_report
import logger_setup
import parser
from anti_bot import manager
from parser import FetchFailure
from logger_setup import log_event, new_trace, set_trace, setup_logging, stop_logging, write_metrics_row
from runtime_metrics import RuntimeMetricsRegistry, format_minute_line, sanitize_proxy_credentials, short_channel


class LogFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="avito-logs-")
        self.addCleanup(directory.cleanup)
        self.logs = Path(directory.name)
        saved_handlers = list(logging.getLogger().handlers)
        patches = [
            patch.object(logger_setup, "LOGS_DIR", self.logs),
            patch.object(logger_setup, "EVENTS_LOG", self.logs / "events.jsonl"),
            patch.object(logger_setup, "METRICS_LOG", self.logs / "metrics.jsonl"),
            patch.object(logger_setup, "ERRORS_LOG", self.logs / "errors.log"),
            patch.object(logger_setup, "USER_ACTIONS_LOG", self.logs / "user_actions.log"),
            patch.object(logger_setup, "COMBINED_LOG", self.logs / "bot.log"),
            patch.object(logger_setup, "_crash_file", object()),
            patch.object(logger_setup, "_stdout_is_combined_log", return_value=True),
            patch.object(log_report, "EVENTS_LOG", self.logs / "events.jsonl"),
            patch.object(log_report, "METRICS_LOG", self.logs / "metrics.jsonl"),
            patch.object(log_report, "CRASH_LOG", self.logs / "crash.log"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        def restore():
            stop_logging()
            root = logging.getLogger()
            root.handlers[:] = saved_handlers
            set_trace("")

        self.addCleanup(restore)
        setup_logging()

    def flush(self):
        stop_logging()

    def events(self):
        return [json.loads(line) for line in (self.logs / "events.jsonl").read_text(encoding="utf-8").splitlines()]


class TestLogFiles(LogFixture):
    def test_trace_follows_a_scan_and_routes_to_files(self):
        logger = logging.getLogger("monitoring")
        trace = new_trace()
        log_event(logger, logging.INFO, "scan.fail", "✗ «iphone» 100% forbidden", kind="forbidden")
        logger.warning("нужно внимание")
        logging.getLogger("user_action").info("клик")
        self.flush()

        fail, warning, click = self.events()
        self.assertEqual((fail["ev"], fail["id"], fail["f"]["kind"]), ("scan.fail", trace, "forbidden"))
        self.assertNotIn("msg", fail)  # the fields already say it
        self.assertEqual(warning["l"], "W")
        self.assertEqual(click["c"], "user_ac")

        bot_log = (self.logs / "bot.log").read_text(encoding="utf-8")
        self.assertIn(f"scan    {trace} ✗ «iphone» 100% forbidden", bot_log)
        self.assertNotIn("клик", bot_log)
        errors = (self.logs / "errors.log").read_text(encoding="utf-8")
        self.assertIn("нужно внимание", errors)
        self.assertNotIn("forbidden", errors)  # a routine failed scan is not an error
        self.assertIn("клик", (self.logs / "user_actions.log").read_text(encoding="utf-8"))

    def test_secrets_never_reach_the_files(self):
        logger = logging.getLogger("proxy_pool")
        logger.warning("сбой http://login:s3cret@1.2.3.4:3000 token 1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        log_event(logger, logging.INFO, "proxy.x", "ok", url="http://login:s3cret@1.2.3.4:3000")
        try:
            raise RuntimeError("ключ 1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        except RuntimeError:
            logger.exception("падение")
        self.flush()
        for name in ("bot.log", "errors.log", "events.jsonl"):
            text = (self.logs / name).read_text(encoding="utf-8")
            self.assertNotIn("s3cret", text, name)
            self.assertNotIn("AAAAAAAA", text, name)

    def test_debug_mode_writes_only_to_events_and_turns_off(self):
        logger = logging.getLogger("scheduler")
        logger.debug("скрыто")
        logger_setup.set_debug_mode(5)
        self.assertGreater(logger_setup.debug_mode_remaining(), 0)
        logger.debug("подробно")
        logger_setup.set_debug_mode(0)
        logger.debug("снова скрыто")
        self.assertEqual(logger_setup.debug_mode_remaining(), 0)
        self.flush()
        self.assertEqual([row.get("msg") for row in self.events()], ["подробно"])
        self.assertNotIn("подробно", (self.logs / "bot.log").read_text(encoding="utf-8"))

    def test_events_rotate_to_gzip_and_the_report_reads_both(self):
        self.flush()
        with patch.object(logger_setup, "EVENTS_MAX_MB", 0.002):
            setup_logging()
        logger = logging.getLogger("monitoring")
        for index in range(60):
            log_event(logger, logging.INFO, "scan.fail", "x", src=f"источник {index}", kind="timeout", padding="y" * 40)
        self.flush()
        self.assertTrue((self.logs / "events.jsonl.1.gz").is_file())
        rows = log_report.read_rows(self.logs / "events.jsonl", time.time() - 60)
        self.assertEqual([row["f"]["src"] for row in rows][-1], "источник 59")
        self.assertGreater(len(rows), 40)

    def test_full_queue_drops_instead_of_blocking(self):
        self.flush()
        handler = logging.getLogger().handlers[0]
        before = logger_setup.dropped_records
        for _ in range(logger_setup.QUEUE_SIZE + 5):
            logging.getLogger("monitoring").info("x")
        self.assertEqual(logger_setup.dropped_records - before, 5)
        self.assertEqual(handler.queue.qsize(), logger_setup.QUEUE_SIZE)

    def test_user_is_erased_from_rotated_archives_too(self):
        logging.getLogger("monitoring").info("чужая строка")
        logging.getLogger("user_action").info("[ID:1 @vasya_test] клик")
        self.flush()
        archive = self.logs / "events.jsonl.1.gz"
        with gzip.open(archive, "wt", encoding="utf-8") as packed:
            packed.write('{"t":1,"msg":"@vasya_test"}\n{"t":2,"msg":"чужая"}\n')
        logger_setup.sanitize_logs_for_user("@vasya_test")
        with gzip.open(archive, "rt", encoding="utf-8") as packed:
            self.assertEqual(packed.read(), '{"t":2,"msg":"чужая"}\n')
        self.assertNotIn("vasya_test", (self.logs / "events.jsonl").read_text(encoding="utf-8"))
        self.assertIn("чужая строка", (self.logs / "events.jsonl").read_text(encoding="utf-8"))


class TestMinuteRows(unittest.TestCase):
    def setUp(self):
        self.clock = [6000.0]
        self.registry = RuntimeMetricsRegistry(monotonic_fn=lambda: self.clock[0], wall_clock_fn=lambda: 1_760_000_000.0)

    def test_each_finished_minute_is_returned_once(self):
        registry = self.registry
        registry.record_scan_attempt(success=True, elapsed_ms=900, pages_fetched=1, due_lag_seconds=3.0, coverage_gap=True)
        registry.record_scan_attempt(success=False, failure_kind="forbidden", proxy_id="p1", masked_url="http://***@1.2.3.4:3000")
        registry.record_http_request("p1", "http://***@1.2.3.4:3000", 403, 0.4)
        registry.record_loop_lag(1.5, stall=True)
        registry.record_bot_api_retry_after(42)
        registry.record_challenge("geetest", solved=True, seconds=2.0)
        registry.record_challenge("geetest", solved=False, seconds=4.0)
        registry.record_fanout_items(new=5, queued=2, stop_word=1, too_old=2)
        self.assertEqual(registry.pop_minute_rows(), [])

        self.clock[0] += 60
        row = registry.pop_minute_rows()[0]
        self.assertEqual(row["scans"], {"ok": 1, "fail": 1, "fast": 0, "new": 1, "gap": 1})
        self.assertEqual(row["fail"], {"forbidden": 1})
        self.assertEqual(row["ch"]["1.2.3.4:3000"]["403"], 1)
        self.assertEqual(row["loop_max_ms"], 1500)
        self.assertEqual((row["flood"], row["flood_max_s"]), (1, 42.0))
        self.assertEqual(row["challenge"]["geetest"], {"ok": 1, "fail": 1, "avg_s": 3.0})
        self.assertEqual(row["lots"]["queued"], 2)
        self.assertEqual(row["due_s"]["p95"], 5.0)
        self.assertEqual(registry.pop_minute_rows(), [])

        line = format_minute_line(row)
        self.assertIn("forbidden×1", line)
        self.assertIn("flood 1", line)
        json.dumps(row)

    def test_short_channel_never_shows_credentials(self):
        self.assertEqual(short_channel("direct"), "direct")
        self.assertEqual(short_channel("x", sanitize_proxy_credentials("http://u:p@1.2.3.4:3000")), "1.2.3.4:3000")


class TestDiagnosis(unittest.TestCase):
    def test_stub_page_fingerprint_says_what_the_page_was(self):
        page = "<html><head><title>Вход\n в аккаунт</title></head><body>доступ ограничен geetest" + "x" * 5000
        self.assertEqual(parser.page_fingerprint(page), "«Вход в аккаунт» 4 КБ [geetest, доступ ограничен]")
        self.assertEqual(parser.page_fingerprint("<html>"), "«без title» 0 КБ")

    def test_challenge_outcome_is_measured_even_when_the_solver_raises(self):
        with patch.object(manager.METRICS, "record_challenge") as record:
            self.assertTrue(manager._timed_solve("pow", lambda: True))
            with self.assertRaises(ValueError):
                manager._timed_solve("pow", lambda: (_ for _ in ()).throw(ValueError()))
        self.assertEqual([call.kwargs["solved"] for call in record.call_args_list], [True, False])

    def test_stalled_loop_is_recorded(self):
        async def scenario():
            import monitoring
            with patch.object(monitoring, "LOOP_PROBE_SECONDS", 0.01), patch.object(monitoring, "LOOP_STALL_SECONDS", 0.02), \
                    patch.object(monitoring.METRICS, "record_loop_lag") as record:
                watcher = asyncio.create_task(monitoring.watch_event_loop())
                await asyncio.sleep(0.015)
                time.sleep(0.05)
                await asyncio.sleep(0.03)
                watcher.cancel()
            return record

        record = asyncio.run(scenario())
        self.assertTrue(any(call.kwargs["stall"] for call in record.call_args_list))


class TestReport(LogFixture):
    def minute(self, stamp, **overrides):
        row = {
            "t": stamp, "ts": "10-30 12:00", "scans": {"ok": 10, "fail": 30, "fast": 0, "new": 2, "gap": 0},
            "fail": {"catalog_missing": 30},
            "ch": {"direct": {"http": 40, "ok": 36, "403": 4, "429": 0, "5xx": 0, "terr": 0, "coll": 40, "coll_ok": 10,
                              "ms_p50": 500, "fail": {"catalog_missing": 30}, "state": "healthy", "rate": 1.5}},
            "scan_s": {"p50": 1.0, "p95": 2.0}, "due_s": {"p50": 1.0, "p95": 5.0}, "lots": {"new": 5, "queued": 2, "stop": 1,
            "seller": 0, "old": 2, "title": 0}, "pub_sent_s": {"n": 2, "p50": 30.0, "p95": 60.0}, "sent": 2, "flood": 1,
            "flood_max_s": 120.0, "challenge": {"geetest": {"ok": 3, "fail": 1, "avg_s": 2.0}}, "loop_max_ms": 2100, "stalls": 1,
            "rps_need": 3.5, "rps_have": 2.0, "outbox": 0, "rss_mb": 300.0, "log_drops": 0,
        }
        row.update(overrides)
        return row

    def test_report_points_at_the_cause_and_stays_small(self):
        logger = logging.getLogger("monitoring")
        log_event(logging.getLogger("bot"), logging.INFO, "app.start", "старт", version="abc", net_mode="both")
        log_event(logging.getLogger("bot"), logging.INFO, "app.start", "старт", version="abd", net_mode="both")
        for _ in range(3):
            log_event(logger, logging.INFO, "scan.fail", "✗", src="iphone", kind="catalog_missing", detail="«Вход» 12 КБ [login]", rids=[7])
        try:
            raise KeyError("boom")
        except KeyError:
            logger.exception("Сбой 123")
            logger.exception("Сбой 456")
        now = int(time.time())
        write_metrics_row(self.minute(now - 120))
        write_metrics_row(self.minute(now - 60))
        self.flush()

        report = log_report.build_report(60, {"net_mode": "both"})
        self.assertIn("HTTP ok 90%, но каталог получен лишь в 25%", report)
        self.assertIn("Event loop зависал 2 раз", report)
        self.assertIn("Telegram flood-wait 2 раз", report)
        self.assertIn("Мощности не хватает 2 мин", report)
        self.assertIn("ПРЕДЫДУЩИЙ ЗАПУСК УПАЛ", report)
        self.assertIn("3× catalog_missing: «Вход» N КБ [login]", report)
        self.assertIn("3× «iphone»: catalog_missing×3", report)
        self.assertIn("geetest: решено 6/8 (75%)", report)
        self.assertIn("[E] 2× ", report)
        self.assertIn("KeyError", report)
        self.assertLess(len(report), 40000)

    def test_channel_history_sums_one_channel_and_counts_paused_minutes(self):
        now = int(time.time())
        paused = {"direct": {"http": 5, "ok": 1, "403": 4, "429": 0, "5xx": 0, "terr": 0, "coll": 5, "coll_ok": 1,
                             "ms_p50": 500, "fail": {"forbidden": 2}, "state": "quarantined", "rate": 1.5}}
        write_metrics_row(self.minute(now - 90000))  # older than a day
        write_metrics_row(self.minute(now - 120))
        write_metrics_row(self.minute(now - 60, ch=paused))
        self.flush()

        stats = log_report.channel_history("direct")
        self.assertEqual((stats["http"], stats["ok"], stats["minutes"], stats["paused"]), (45, 37, 2, 1))
        self.assertEqual(stats["fail"]["forbidden"], 2)
        self.assertIsNone(log_report.channel_history("10.0.0.1:3000"))

    def healthy_minute(self, stamp, **overrides):
        channel = {"http": 15, "ok": 14, "403": 1, "429": 0, "5xx": 0, "terr": 0, "coll": 15, "coll_ok": 14,
                   "ms_p50": 500, "fail": {}, "state": "healthy", "rate": 1.5}
        row = self.minute(
            stamp, scans={"ok": 60, "fail": 0, "fast": 40, "new": 20, "gap": 0}, fail={}, flood=0, stalls=0, loop_max_ms=100,
            ch={f"10.0.0.{i}:3000": dict(channel) for i in range(5)}, rps_need=1.4, rps_have=7.0, coverage_s=25.0,
            sent=3, lost={"expired": 0, "failed": 0},
        )
        row.update(overrides)
        return row

    def write_day(self, make_row, minutes=1440):
        now = int(time.time())
        for index in range(minutes):
            write_metrics_row(make_row(now - 60 * (minutes - index)))

    def lots(self, count, **fields):
        logger = logging.getLogger("scheduler")
        for index in range(count):
            lot = {"rid": 7, "item": str(index), "age": 130.0, "gap": 20.0, "fresh": True, "promo": False, **fields}
            log_event(logger, logging.INFO, "lot.new", "лот", **lot)

    def test_healthy_day_can_be_sold_and_splits_avito_delay_from_the_bot(self):
        self.write_day(self.healthy_minute)
        self.lots(30)
        self.lots(4, fresh=False, age=900.0)
        self.lots(2, promo=True, age=5000.0, fresh=False)
        self.flush()

        report = log_report.build_readiness(24, {"net_mode": "both"})
        self.assertIn("🟢 Можно продавать", report)
        self.assertIn("✅ Бот замечает лот: p95 20 с по 30 лотам", report)
        self.assertIn("Свежие (30): от публикации до бота p50 130 с", report)
        self.assertIn("Авито не показывал: p50 110 с", report)
        self.assertIn("бот не смотрел: p50 20 с", report)
        self.assertIn("Старше лимита радара (4)", report)
        self.assertIn("реклама 2", report)

    def test_outage_fails_the_verdict_and_names_the_dead_channels(self):
        def row(stamp):
            offset = (int(time.time()) - stamp) // 60
            if not 100 <= offset < 220:
                return self.healthy_minute(stamp)
            dead = {"http": 6, "ok": 0, "403": 0, "429": 0, "5xx": 0, "terr": 6, "coll": 2, "coll_ok": 0,
                    "ms_p50": 50, "fail": {"timeout": 2}, "state": "half_open", "rate": 1.5}
            return self.healthy_minute(
                stamp, scans={"ok": 40, "fail": 20, "fast": 30, "new": 10, "gap": 0}, fail={"forbidden": 20}, coverage_s=900.0,
                ch={"31.12.95.90:3000": dead},
            )

        self.write_day(row)
        self.lots(30)
        self.flush()

        report = log_report.build_readiness(24)
        self.assertIn("🔴 Продавать рано", report)
        self.assertIn("120 мин · провалов 2400 из 7200", report)
        self.assertIn("не отвечали: 31.12.95.90:3000", report)
        self.assertIn("❌ Все ссылки под присмотром", report)
        hour_note = log_report.findings(log_report.read_rows(log_report.METRICS_LOG, 0), [])
        self.assertTrue(any("Провалов работы: 1, всего 120 мин" in note for note in hour_note))

    def test_capacity_is_judged_over_a_window_not_a_single_minute(self):
        channel = {"http": 2, "ok": 2, "403": 0, "429": 0, "5xx": 0, "terr": 0, "coll": 2, "coll_ok": 2,
                   "ms_p50": 500, "fail": {}, "state": "healthy", "rate": 1.5}
        self.write_day(lambda stamp: self.healthy_minute(stamp, ch={f"10.0.0.{i}:3000": dict(channel) for i in range(5)}))
        self.flush()
        self.assertIn("✅ Запас хороших каналов: хватает 100% минут", log_report.build_readiness(24))

    def test_server_alone_covers_a_small_load_in_smart_mode(self):
        server = {"http": 8, "ok": 8, "403": 0, "429": 0, "5xx": 0, "terr": 0, "coll": 8, "coll_ok": 8,
                  "ms_p50": 400, "fail": {}, "state": "healthy", "rate": 1.5}
        idle_proxy = {"http": 0, "ok": 0, "403": 0, "429": 0, "5xx": 0, "terr": 0, "coll": 0, "coll_ok": 0,
                      "fail": {}, "state": "healthy", "rate": 1.5}
        self.write_day(lambda stamp: self.healthy_minute(
            stamp, rps_need=0.3, ch={"direct": dict(server), "10.0.0.1:3000": dict(idle_proxy)}))
        self.flush()
        self.assertIn("✅ Запас хороших каналов: хватает 100% минут", log_report.build_readiness(24))

    def test_blind_zone_names_its_radar(self):
        def row(stamp):
            offset = (int(time.time()) - stamp) // 60
            return self.healthy_minute(stamp, coverage_s=700.0, coverage_rid=172) if offset < 5 else self.healthy_minute(stamp, coverage_rid=165)

        self.write_day(row)
        self.flush()
        report = log_report.build_readiness(24)
        self.assertIn("худшая слепая зона 700 с (#172)", report)
        self.assertIn("слепая зона до 700 с (#172)", report)

    def test_speed_by_radar_shows_the_rest_without_the_busiest(self):
        self.write_day(self.healthy_minute)
        self.lots(30, rid=212, age=180.0)
        self.lots(20, rid=212, age=480.0, fresh=False, pos=30, ch="10.0.0.1:3000")
        self.lots(10, rid=184, age=78.0, pos=2, ch="10.0.0.2:3000")
        self.flush()
        report = log_report.build_readiness(24)
        self.assertIn("#212: 50 · 40% · 180 с · 180 с", report)
        self.assertIn("#184: 10 · 0% · 78 с · 78 с", report)
        self.assertIn("Без #212 (у него 83% лотов): p50 78 с, p95 78 с, свежих 10", report)
        self.assertIn("Место в выдаче: старые p50 30, p95 30; свежие p50 2, p95 2", report)
        self.assertIn("Старые по каналам: 10.0.0.1:3000×20", report)

    def test_redirect_failure_goes_to_another_channel_and_says_where(self):
        import re
        from parser import redirect_failure
        failure = redirect_failure(re.search("(.+)", r"/moskva/audio_i_video?cd=1\u0026q=x"), 2)
        self.assertTrue(failure.retryable)
        self.assertEqual(failure.detail, "переход 3: /moskva/audio_i_video")
        self.assertEqual(redirect_failure(None, 0).detail, "переход 1: нет адреса")

    def test_short_history_gives_no_verdict_yet(self):
        self.write_day(self.healthy_minute, minutes=120)
        self.flush()
        report = log_report.build_readiness(24)
        self.assertIn("🟡 Данных мало", report)
        self.assertIn("⏳ Бот замечает лот", report)

    def test_link_table_merges_source_rows_worst_first(self):
        now = int(time.time())
        first = {"172": {"ok": 8, "fail": 2, "retry": 5, "blocked": 4, "q": "technics sl-1200", "to": "/moskva/audio_i_video +context"},
                 "165": {"ok": 10, "fail": 0, "retry": 0, "blocked": 0, "q": "hasselblad 500", "to": "/moskva/fototehnika"}}
        write_metrics_row(self.healthy_minute(now - 660, src=first))
        write_metrics_row(self.healthy_minute(now - 60, src={"172": {"ok": 0, "fail": 10, "retry": 9, "blocked": 9, "q": "technics sl-1200", "to": "/moskva/audio_i_video +context"}}))
        self.flush()
        table = log_report.build_readiness(1).split("## Ссылки\n", 1)[1].splitlines()
        self.assertEqual(table[1], "#172 «technics sl-1200» → /moskva/audio_i_video +context: 20 · 60% · 14 · 13")
        self.assertTrue(table[2].startswith("#165 «hasselblad 500» → /moskva/fototehnika: 10 · 0%"))

    def test_old_rows_are_skipped_and_empty_logs_do_not_fail(self):
        self.assertIn("Нет минутных метрик", log_report.build_report(5))
        self.assertIn("🟡 Данных мало", log_report.build_readiness(1))
        now = time.time()
        (self.logs / "metrics.jsonl").write_text(
            json.dumps(self.minute(int(now - 7200))) + "\n" + "обрезанная строка\n" + json.dumps(self.minute(int(now)))[:40] + "\n",
            encoding="utf-8",
        )
        self.assertEqual(log_report.read_rows(self.logs / "metrics.jsonl", now - 60), [])

    def test_select_finds_one_scan_or_radar(self):
        rows = [
            {"id": "a1f3", "ev": "scan.fail", "f": {"rids": [7]}},
            {"id": "a1f3", "ev": "fanout.found", "f": {"radar": 7, "chat": 5}},
            {"id": "b2c4", "ev": "send.ok", "f": {"chat": 5}},
            {"id": "c3d5", "ev": "lot.new", "f": {"rid": 7, "age": 200, "gap": 18}},
        ]
        self.assertEqual(len(log_report.select(rows, trace="a1f3")), 2)
        self.assertEqual(len(log_report.select(rows, radar=7)), 3)
        self.assertEqual(len(log_report.select(rows, chat=5)), 2)
        self.assertEqual(len(log_report.select(rows, text="FANOUT")), 1)


# The loop takes the production path only for a parser whose class is called AvitoParser.
_ScanParser = type("AvitoParser", (), {
    "__init__": lambda self, **kwargs: None,
    "close": lambda self: None,
    "normalize_url": staticmethod(parser.AvitoParser.normalize_url),
})


class _FakeClient:
    is_direct = False
    in_flight = 0

    def __init__(self):
        self.proxy_id = "p1"
        self.masked_url = "http://***@1.2.3.4:3000"
        self._bound_parser = Mock(last_failure=FetchFailure("forbidden", status=403), last_http_requests=2)

    def cooldown_remaining(self):
        return 0.0


class _FakePool:
    is_direct_only = False

    def __init__(self):
        self.clients = [_FakeClient()]

    def healthy_count(self):
        return 1

    def acquire_client(self, exclude_ids=()):
        if self.clients[0].proxy_id in exclude_ids:
            return None
        return Mock(client=self.clients[0])

    def snapshots(self):
        return []


class TestProductionScanLoop(unittest.IsolatedAsyncioTestCase):
    async def test_failed_and_good_scan_log_one_line_and_fanout_keeps_the_trace(self):
        import monitoring
        import scheduler
        from collector import SourceCollection

        radars = [
            {"id": 1, "user_id": 9, "url": "https://www.avito.ru/search?q=bad", "tariff_plan": "pro", "title": "плохой"},
            {"id": 2, "user_id": 9, "url": "https://www.avito.ru/search?q=good", "tariff_plan": "pro", "title": "хороший"},
        ]
        fanned_out = asyncio.Event()
        fanout_trace = []
        events = []

        async def collect(parser_, url, *args, **kwargs):
            if "bad" in url:
                return None
            return SourceCollection("Поиск", (), time.time(), 1, False, 120, top_ids=("1",))

        async def fanout(*args, **kwargs):
            fanout_trace.append(logger_setup.get_trace())
            fanned_out.set()
            return True

        def capture(_logger, level, event, message, **fields):
            events.append((event, logger_setup.get_trace(), level, message, fields))

        with (
            patch.object(monitoring, "get_proxy_pool", return_value=_FakePool()),
            patch.object(monitoring, "AvitoParser", _ScanParser),
            patch.object(monitoring, "collect_source", collect),
            patch.object(monitoring, "log_event", capture),
            patch.object(monitoring, "write_metrics_row") as write_row,
            patch.object(monitoring.METRICS, "pop_minute_rows", side_effect=lambda: [monitoring.METRICS._minute_row(monitoring.METRICS._bucket(), time.time())]),
            patch.object(scheduler, "process_collected_group_fanout", fanout),
            patch.object(database, "get_all_active_searches", AsyncMock(return_value=radars)),
            patch.object(database, "get_outbox_metrics", AsyncMock(return_value={"pending": 3, "oldest_seconds": 1.5})),
            patch.object(database, "load_all_source_scan_states", AsyncMock(return_value={})),
            patch.object(database, "upsert_source_scan_states_batch", AsyncMock()),
            patch.object(config, "METRICS_REPORT_INTERVAL", 0),
            patch.object(config, "SCAN_STARTUP_SPREAD", 0),
            patch.object(config, "MAX_CONCURRENT_WORKERS", 1),
        ):
            task = asyncio.create_task(monitoring.run_monitoring(Mock(send_message=AsyncMock())))
            await asyncio.wait_for(fanned_out.wait(), 5)
            await asyncio.sleep(1.2)  # one more dispatcher pass, with fresh outbox numbers
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        by_event = {event[0]: event for event in reversed(events)}
        failed, good = by_event["scan.fail"], by_event["scan.ok"]
        self.assertEqual(failed[4]["kind"], "forbidden")
        self.assertEqual(set(failed[4]["attempts"]), {"1.2.3.4:3000: forbidden"})
        self.assertEqual(failed[4]["rids"], [1])
        self.assertEqual((good[4]["src"], good[4]["ch"], good[2]), ("хороший", "1.2.3.4:3000", logging.DEBUG))
        self.assertEqual(fanout_trace, [good[1]])
        self.assertNotEqual(failed[1], good[1])

        sources = monitoring.METRICS.pop_source_stats()
        self.assertGreaterEqual(sources["1"]["fail"], 1)
        self.assertEqual(sources["1"]["blocked"], sources["1"]["retry"])
        self.assertEqual((sources["2"]["q"], sources["2"]["to"]), ("good", "/search"))
        row = write_row.call_args.args[0]
        self.assertEqual((row["urls"], row["radars"], row["outbox"]), (2, 2, 3))
        self.assertEqual(row["channels"], "0/0")
        self.assertIn("minute", by_event)

    async def test_failed_fanout_is_repeated_instead_of_fast_skipped(self):
        import monitoring
        import scheduler
        from collector import SourceCollection

        radar = {
            "id": 1, "user_id": 9, "url": "https://www.avito.ru/search?q=x", "tariff_plan": "pro",
            "title": "радар", "initialized_at": "2026-01-01 00:00:00",
        }
        scans = []
        fanouts = 0
        repeated = asyncio.Event()

        async def collect(parser_, url, *args, **kwargs):
            scans.append((kwargs["can_fast_skip"], kwargs["last_top_ids"]))
            return SourceCollection("Поиск", (), time.time(), 1, False, 120, top_ids=("1",))

        async def fanout(*args, **kwargs):
            nonlocal fanouts
            fanouts += 1
            if fanouts == 1:
                raise RuntimeError("database is locked")
            repeated.set()
            return True

        with (
            patch.object(monitoring, "get_proxy_pool", return_value=_FakePool()),
            patch.object(monitoring, "AvitoParser", _ScanParser),
            patch.object(monitoring, "collect_source", collect),
            patch.object(scheduler, "process_collected_group_fanout", fanout),
            patch.object(database, "get_all_active_searches", AsyncMock(return_value=[radar])),
            patch.object(database, "get_outbox_metrics", AsyncMock(return_value={"pending": 0, "oldest_seconds": 0})),
            patch.object(database, "load_all_source_scan_states", AsyncMock(return_value={})),
            patch.object(database, "upsert_source_scan_states_batch", AsyncMock()),
            patch.object(config, "SCAN_STARTUP_SPREAD", 0),
            patch.object(config, "MAX_CONCURRENT_WORKERS", 1),
            patch.dict(config.TARIFF_SCAN_INTERVAL, {key: 0.05 for key in config.TARIFF_SCAN_INTERVAL}),
        ):
            task = asyncio.create_task(monitoring.run_monitoring(Mock(send_message=AsyncMock())))
            await asyncio.wait_for(repeated.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(scans[1], (False, ()))

    async def _dead_link_alerts(self, kind, *, ok_window=120.0):
        """Scans one failing and one healthy link and returns the dead-link alerts sent to admins."""
        import monitoring
        import scheduler
        from collector import SourceCollection

        radars = [
            {"id": 7, "user_id": 42, "url": "https://www.avito.ru/moskva/bad", "tariff_plan": "pro", "title": "битая"},
            {"id": 8, "user_id": 43, "url": "https://www.avito.ru/moskva/good", "tariff_plan": "pro", "title": "живая"},
        ]
        enough = asyncio.Event()
        bad_scans = 0

        async def collect(parser_, url, *args, **kwargs):
            nonlocal bad_scans
            if "bad" not in url:
                return SourceCollection("Поиск", (), time.time(), 1, False, 120, top_ids=("1",))
            parser_.last_failure = FetchFailure(kind)
            bad_scans += 1
            if bad_scans >= 15:  # 3 channel attempts per scan: past the alert streak
                enough.set()
            return None

        bot = Mock(send_message=AsyncMock())
        with (
            patch.object(monitoring, "get_proxy_pool", return_value=_FakePool()),
            patch.object(monitoring, "AvitoParser", _ScanParser),
            patch.object(monitoring, "collect_source", collect),
            patch.object(monitoring, "DEAD_LINK_OK_WINDOW", ok_window),
            patch.object(scheduler, "process_collected_group_fanout", AsyncMock(return_value=True)),
            patch.object(database, "get_all_active_searches", AsyncMock(return_value=radars)),
            patch.object(database, "get_outbox_metrics", AsyncMock(return_value={"pending": 0, "oldest_seconds": 0})),
            patch.object(database, "load_all_source_scan_states", AsyncMock(return_value={})),
            patch.object(database, "upsert_source_scan_states_batch", AsyncMock()),
            patch.object(config, "ADMIN_IDS", [7001]),
            patch.object(config, "SCAN_STARTUP_SPREAD", 0),
            patch.object(config, "MAX_CONCURRENT_WORKERS", 1),
            patch.object(config, "SOURCE_BACKOFF_BASE", 0.01),
            patch.object(config, "SOURCE_BACKOFF_MAX", 0.02),
            patch.object(config, "SOURCE_SLOW_AFTER", 1000),
            patch.object(config, "SOURCE_DEAD_ALERT_STREAK", 3),
            patch.dict(config.TARIFF_SCAN_INTERVAL, {key: 0.05 for key in config.TARIFF_SCAN_INTERVAL}),
        ):
            task = asyncio.create_task(monitoring.run_monitoring(bot))
            await asyncio.wait_for(enough.wait(), 8)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.sent = [call.kwargs for call in bot.send_message.await_args_list]
        return [sent["text"] for sent in self.sent if "Ссылка не работает" in sent["text"]]

    async def test_dead_link_alerts_admins_once_with_its_owner(self):
        alerts = await self._dead_link_alerts("catalog_missing")
        self.assertEqual(len(alerts), 1)
        self.assertIn("#7 «битая», пользователь 42", alerts[0])
        self.assertIn("https://www.avito.ru/moskva/bad", alerts[0])

    async def test_dead_link_tells_the_owner_once_with_a_way_to_fix_it(self):
        await self._dead_link_alerts("catalog_missing")
        to_owner = [sent for sent in self.sent if sent["chat_id"] == 42]
        self.assertEqual(len(to_owner), 1)
        self.assertIn("Радар не получает лоты", to_owner[0]["text"])
        self.assertEqual(to_owner[0]["reply_markup"].inline_keyboard[0][0].callback_data, "change_url:7")

    async def test_blocks_are_not_reported_as_a_dead_link(self):
        self.assertEqual(await self._dead_link_alerts("forbidden"), [])

    async def test_a_general_outage_is_not_reported_as_a_dead_link(self):
        self.assertEqual(await self._dead_link_alerts("catalog_missing", ok_window=0.0), [])


class TestMonthOfHistory(LogFixture):
    def test_rotations_are_kept_by_age_not_by_count(self):
        self.flush()
        with patch.object(logger_setup, "EVENTS_MAX_MB", 0.002):
            setup_logging()
        logger = logging.getLogger("monitoring")
        for index in range(300):
            log_event(logger, logging.INFO, "scan.fail", "x", src=f"источник {index}", kind="timeout", padding="y" * 40)
        self.flush()
        self.assertGreater(len(list(self.logs.glob("events.jsonl.*.gz"))), 6, "the old scheme kept only six")
        rows = log_report.read_rows(self.logs / "events.jsonl", time.time() - 60)
        self.assertEqual([row["f"]["src"] for row in rows if row.get("ev") == "scan.fail"],
                         [f"источник {index}" for index in range(300)])

    def test_prune_drops_old_copies_then_the_oldest_over_the_size_cap(self):
        import os

        base, now = self.logs / "x.log", time.time()
        for index, age_days in enumerate((1, 5, 31, 40), 1):
            path = self.logs / f"x.log.{index}.gz"
            path.write_bytes(b"z" * 1000)
            os.utime(path, (now - age_days * 86400, now - age_days * 86400))
        self.assertEqual(logger_setup.prune_rotations(base, 30, 10**6, now=now), 2)
        self.assertEqual(sorted(p.name for p in self.logs.glob("x.log.*")), ["x.log.1.gz", "x.log.2.gz"])
        self.assertEqual(logger_setup.prune_rotations(base, 30, 1500, now=now), 1)
        self.assertEqual([p.name for p in self.logs.glob("x.log.*")], ["x.log.1.gz"])

    def test_past_the_row_cap_the_newest_rows_are_kept(self):
        logger = logging.getLogger("monitoring")
        for index in range(10):
            log_event(logger, logging.INFO, "scan.fail", "x", n=index)
        self.flush()
        with patch.object(log_report, "MAX_LINES", 4):
            rows = log_report.read_rows(self.logs / "events.jsonl", time.time() - 60)
        self.assertEqual([row["f"]["n"] for row in rows], [6, 7, 8, 9])

    def test_readiness_reads_only_the_events_it_uses(self):
        logger = logging.getLogger("scheduler")
        log_event(logger, logging.INFO, "send.ok", "x", chat=1, item="1")
        log_event(logger, logging.INFO, "lot.new", "x", rid=1, item="2", age=3.0)
        log_event(logger, logging.WARNING, "send.flood", "x", chat=1)
        self.flush()
        rows = log_report.read_rows(self.logs / "events.jsonl", time.time() - 60, log_report.READINESS_TAGS)
        self.assertEqual([row["ev"] for row in rows], ["lot.new", "send.flood"])

    def test_lot_history_has_the_finds_and_deliveries_of_that_lot_only(self):
        logger = logging.getLogger("scheduler")
        log_event(logger, logging.INFO, "lot.new", "x", rid=7, item="4123456789", age=12.0, pos=0)
        log_event(logger, logging.INFO, "lot.new", "x", rid=7, item="4123456780", age=1.0, pos=1)
        log_event(logger, logging.INFO, "send.ok", "x", chat=55, item="4123456789", seen_to_sent_s=1.5)
        log_event(logging.getLogger("monitoring"), logging.INFO, "scan.fail", "лот 4123456789", src="x")
        self.flush()
        rows = log_report.lot_history("4123456789")
        self.assertEqual([(row["ev"], (row["f"].get("rid") or row["f"].get("chat"))) for row in rows],
                         [("lot.new", 7), ("send.ok", 55)])

    def test_the_bot_reads_lot_history_and_reports_in_a_separate_process(self):
        import os
        from handlers import admin

        log_event(logging.getLogger("scheduler"), logging.INFO, "lot.new", "x", rid=7, item="4123456789", age=3.0,
                  gap=2.0, fresh=True, promo=False, pos=0, ch="direct")
        self.flush()
        with patch.dict(os.environ, {"LOGS_DIR": str(self.logs)}), \
                patch.object(log_report, "lot_history", side_effect=AssertionError("read in the bot's process")), \
                patch.object(log_report, "build_readiness", side_effect=AssertionError("built in the bot's process")):
            rows = asyncio.run(admin._lot_history("4123456789"))
            report = asyncio.run(admin._log_report("--ready", "1", live_state={"net_mode": "smart"}))
        self.assertEqual([(row["ev"], row["f"]["rid"]) for row in rows], [("lot.new", 7)])
        self.assertIn("готовность к продаже за 1 ч", report)
        self.assertIn('"net_mode": "smart"', report)


if __name__ == "__main__":
    unittest.main()
