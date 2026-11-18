import asyncio
import datetime
import json
import time
import unittest
from threading import Event
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from tests import _environment
from freshness import freshness_status, publication_age_seconds
from parser import AvitoParser, FetchFailure
from curl_cffi import requests as curl_requests
from request_budget import NetworkBlockState, RequestDeferred, RequestBudgetExceeded, parse_retry_after
from scan_planner import DeadlineScheduler, calculate_interval_plan
from tools.simulate_scheduler import simulate
from collector import collect_source
from source_cache import SourceSnapshotCache, SourceSnapshot
from tests.test_anti_bot import FakeResponse
import config
import monitoring
import parser as parser_module
import scheduler
import source_cache


class TestDeadlineScheduling(unittest.TestCase):
    def make_planner(self, count=3):
        planner = DeadlineScheduler(retry_base_delay=5, max_retries=2)
        groups = {str(index): [{"id": index}] for index in range(count)}
        planner.sync(groups, {key: 15 for key in groups}, 0)
        return planner

    def test_no_duplicate_reservations_and_start_anchored_interval(self):
        planner = self.make_planner(1)
        task = planner.take_due(0)
        self.assertIsNone(planner.take_due(100))
        planner.complete(task, 2, True)
        self.assertIsNone(planner.take_due(14.99))
        self.assertIsNotNone(planner.take_due(15))

    def test_slow_scans_do_not_replay_missed_ticks(self):
        planner = self.make_planner(1)
        task = planner.take_due(0)
        planner.complete(task, 40, True)
        next_task = planner.take_due(40)
        self.assertIsNotNone(next_task)
        planner.complete(next_task, 41, True)
        self.assertIsNone(planner.take_due(41))

    def test_retries_are_delayed_bounded_and_fair(self):
        planner = self.make_planner()
        first, second = planner.take_due(0), planner.take_due(0)
        planner.complete(first, 1, False)
        planner.complete(second, 1, False)
        regular = planner.take_due(6)
        self.assertEqual(regular.retry_attempt, 0)
        retry = planner.take_due(6)
        self.assertEqual(retry.retry_attempt, 1)
        planner.complete(retry, 7, False, retry_after=30)
        self.assertGreaterEqual(planner.sources[retry.key].due_at, 37)

    def test_retry_burst_cannot_starve_later_regular_deadline(self):
        planner = self.make_planner(3)
        tasks = [planner.take_due(0) for _ in range(3)]
        for task in tasks[:2]:
            planner.complete(task, 0, False)
        planner.complete(tasks[2], 0, True)
        self.assertEqual(planner.take_due(15).retry_attempt, 1)
        self.assertEqual(planner.take_due(15).retry_attempt, 0)

    def test_retry_exhaustion_and_nonretryable_failures(self):
        planner = self.make_planner(1)
        for now in (0, 5, 15):
            task = planner.take_due(now)
            planner.complete(task, now, False)
        self.assertEqual(planner.sources["0"].retry_attempt, 0)
        self.assertEqual(planner.sources["0"].due_at, 30)
        task = planner.take_due(30)
        planner.complete(task, 30, False, retryable=False)
        self.assertEqual(planner.sources["0"].due_at, 45)

    def test_new_sources_immediate_existing_sources_staggered(self):
        planner = DeadlineScheduler()
        groups = {str(index): [] for index in range(10)}
        planner.sync(groups, {key: 25 for key in groups}, 100, startup=True)
        self.assertEqual(planner.sources["0"].due_at, 100)
        self.assertGreater(planner.sources["9"].due_at, 110)
        groups["new"] = []
        planner.sync(groups, {key: 25 for key in groups}, 101)
        self.assertEqual(planner.sources["new"].due_at, 101)

    def test_removed_generation_cannot_overwrite_readded_source(self):
        planner = self.make_planner(1)
        task = planner.take_due(0)
        planner.sync({}, {}, 1)
        planner.sync({"0": [{"id": 99}]}, {"0": 25}, 2)
        self.assertIsNone(planner.take_due(2))
        planner.complete(task, 3, False)
        new_task = planner.take_due(3)
        self.assertEqual(new_task.radars[0]["id"], 99)
        self.assertEqual(new_task.retry_attempt, 0)

    def test_interval_budget_reports_unachievable_maximum(self):
        plan = calculate_interval_plan({str(i): 25 for i in range(350)}, 0.5)
        self.assertTrue(plan.overloaded)
        self.assertTrue(all(interval <= 120 for interval in plan.intervals.values()))
        small = calculate_interval_plan({"pro": 15, "regular": 25}, 0.5)
        self.assertFalse(small.overloaded)
        self.assertEqual(small.intervals, {"pro": 15, "regular": 25})

    def test_model_350_subscriptions_uses_unique_source_count(self):
        shared = simulate(sources=10, seconds=1800)
        distinct = simulate(sources=350, seconds=1800)
        self.assertEqual(shared["sources_visited"], 10)
        self.assertEqual(distinct["sources_visited"], 350)
        self.assertGreater(distinct["revisit_p95_seconds"], shared["revisit_p95_seconds"])
        self.assertLessEqual(distinct["requests_started"], 901)


class TestPublicationAges(unittest.TestCase):
    def test_abbreviated_relative_dates(self):
        for text, seconds in (("12 сек. назад", 12), ("2 мин. назад", 120), ("7 мин назад", 420),
                              ("1 ч. назад", 3600), ("3 дня назад", 259200), ("минуту назад", 60)):
            with self.subTest(text=text):
                self.assertEqual(publication_age_seconds(text), seconds)
        self.assertTrue(freshness_status("2 мин. назад", max_age_minutes=5))
        self.assertFalse(freshness_status("7 мин. назад", max_age_minutes=5))

    def test_unknown_invalid_and_future_dates_are_not_old(self):
        for text in (None, "Дата не указана", "минуты", "сегодня", "сегодня в 29:90"):
            self.assertIsNone(freshness_status(text))
        self.assertIsNone(freshness_status(None, time.time() + 3600))
        self.assertIsNone(freshness_status(None, float("nan")))

    def test_timestamp_seconds_milliseconds_and_moscow_clock(self):
        now = datetime.datetime(2026, 10, 27, 12, 10, tzinfo=datetime.timezone(datetime.timedelta(hours=3))).timestamp()
        self.assertTrue(freshness_status(None, (now - 120) * 1000, now=now))
        self.assertFalse(freshness_status(None, now - 600, now=now))
        self.assertEqual(publication_age_seconds("сегодня в 12:08", now=now), 120)
        self.assertIsNone(publication_age_seconds("сегодня в 22:00", now=now))

    def test_nonpositive_age_limit_preserves_default_and_metadata_types(self):
        self.assertTrue(freshness_status("2 мин. назад", max_age_minutes=-1))
        self.assertIsNone(freshness_status({"unexpected": "date"}))


class TestSharedRequestBudget(unittest.IsolatedAsyncioTestCase):
    async def test_spacing_concurrency_and_fifo(self):
        state = NetworkBlockState(requests_per_second=50, max_in_flight=1)
        started = []
        running = peak = 0
        async def perform(index):
            nonlocal running, peak
            async with state.request_slot_async() as granted_at:
                started.append((index, granted_at))
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.025)
                running -= 1
        await asyncio.gather(*(perform(index) for index in range(5)))
        self.assertEqual(peak, 1)
        self.assertEqual([index for index, _ in started], list(range(5)))
        self.assertTrue(all(started[i + 1][1] - started[i][1] >= 0.019 for i in range(4)))
        self.assertEqual(state.snapshot()["requests"], 5)

    async def test_cooldown_cancellation_and_deadline_do_not_leak_slots(self):
        state = NetworkBlockState(requests_per_second=100, max_in_flight=1)
        state.acquire()
        async def waiting():
            async with state.request_slot_async(timeout=0.05):
                self.fail("occupied slot was acquired")
        with self.assertRaises(RequestBudgetExceeded):
            await waiting()
        task = asyncio.create_task(waiting())
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        state.release()
        state.register_block(120)
        with self.assertRaises(RequestDeferred):
            state.acquire()
        self.assertEqual(state.snapshot()["waiting"], 0)
        self.assertEqual(state.snapshot()["in_flight"], 0)

    def test_retry_after_is_case_insensitive_and_finite(self):
        self.assertEqual(parse_retry_after({"rEtRy-AfTeR": "120"}), 120)
        self.assertEqual(parse_retry_after({"Retry-After": "nan"}), 0)
        self.assertEqual(parse_retry_after({"Retry-After": "inf"}), 0)

    def test_success_cannot_clear_other_worker_cooldown(self):
        state = NetworkBlockState()
        state.register_block(120)
        before = state.blocked_until
        for _ in range(20):
            state.register_success()
        self.assertEqual(state.blocked_until, before)
        self.assertGreater(state.cooldown_remaining(), 119)

    def test_response_backoff_and_cautious_recovery(self):
        state = NetworkBlockState()
        with patch("request_budget.time.time", return_value=1000):
            state.register_throttle_response(429, {"Retry-After": "120"})
            self.assertEqual(state.blocked_until, 1120)
            state.register_success()
            self.assertEqual(state.snapshot()["current_rps"], 0.25)
        with patch("request_budget.time.time", return_value=1121):
            for _ in range(10):
                state.register_success()
            self.assertGreater(state.snapshot()["current_rps"], 0.25)
            self.assertLessEqual(state.snapshot()["current_rps"], 0.5)
            self.assertEqual(state.block_streak, 0)


class TestCatalogCoverage(unittest.TestCase):
    def test_ssr_is_not_truncated_and_markup_without_ssr_is_not_a_catalog(self):
        cards = [{"id": str(index), "title": "Fixture", "urlPath": f"/fixture_{index}"} for index in range(60)]
        ssr = '<html><script>' + json.dumps({"loaderData": {"data": {"catalog": {"items": cards}}}}) + '</script></html>'
        self.assertEqual(len(AvitoParser.parse_items(ssr)), 60)
        # The HTML-markup fallback is gone: cards without the SSR catalog mean a broken/foreign page.
        html = '<html>' + ''.join(f'<div data-marker="item" data-item-id="{index}"><a data-marker="item-title" href="/fixture_{index}">Fixture</a></div>' for index in range(60)) + '</html>'
        self.assertIsNone(AvitoParser.parse_catalog(html))
        self.assertEqual(AvitoParser.parse_items(html), [])
        self.assertFalse(AvitoParser.has_catalog(html))

    def test_empty_catalog_is_valid_but_login_page_is_not(self):
        html = '<html><script>' + json.dumps({"loaderData": {"data": {"catalog": {"items": []}}}}) + '</script></html>'
        self.assertTrue(AvitoParser.has_catalog(html))
        self.assertEqual(AvitoParser.parse_items(html), [])
        self.assertFalse(AvitoParser.has_catalog('<html><body>Sign in</body></html>'))

    def test_canonical_key_resets_page_but_preserves_search_filters(self):
        first = AvitoParser.normalize_url('https://m.avito.ru/moskva?q=phone&p=9&bt=1&searchRadius=10&priceMax=20000&utm_source=test')
        second = AvitoParser.normalize_url('https://www.avito.ru/moskva?priceMax=20000&searchRadius=10&bt=1&q=phone')
        self.assertEqual(first, second)
        self.assertNotEqual(first, AvitoParser.normalize_url('https://www.avito.ru/moskva?q=phone&bt=1'))

    def test_pagination_only_accepts_exact_next_page_same_search(self):
        current = 'https://www.avito.ru/moskva?q=phone&s=104'
        valid = '<a data-marker="pagination-button/nextPage" href="?q=phone&amp;s=104&amp;p=2">Next</a>'
        self.assertIn('p=2', AvitoParser.next_page_url(valid, current))
        for href in ('https://evil.test/?p=2', '?q=other&p=2', '?q=phone&p=20'):
            self.assertIsNone(AvitoParser.next_page_url(f'<a rel="next" href="{href}">Next</a>', current))

    def test_pagination_regex_handles_attribute_order_quotes_and_noise(self):
        current = 'https://www.avito.ru/moskva?q=phone&s=104'
        html = (
            '<a href="?q=phone&amp;s=104&amp;p=7" data-marker="pagination-button/page">7</a>'
            "<A class='x' HREF='?q=phone&amp;s=104&amp;p=2' data-marker='pagination-button/nextPage'>›</A>"
        )
        self.assertIn('p=2', AvitoParser.next_page_url(html, current))
        self.assertIsNone(AvitoParser.next_page_url('<a data-marker="pagination-button/nextPage">no href</a>', current))

    def test_search_title_without_dom_parser(self):
        self.assertEqual(AvitoParser.extract_search_title('<h1 class="t">iPhone&nbsp;15 <span>Pro</span></h1>', 'https://www.avito.ru/x'), 'iPhone 15 Pro')
        self.assertEqual(AvitoParser.extract_search_title('<title>Ноутбуки — Авито</title>', 'https://www.avito.ru/x'), 'Ноутбуки')
        self.assertEqual(AvitoParser.extract_search_title('', 'https://www.avito.ru/moskva/noutbuki'), 'noutbuki')


class TestBudgetedCurlPath(unittest.IsolatedAsyncioTestCase):
    def create_parser(self, state):
        with patch.object(AvitoParser, "_load_cookies"):
            parser = AvitoParser(block_state=state)
        parser._save_cookies = Mock()
        self.addCleanup(parser.close)
        return parser

    async def test_real_session_counts_nested_and_redirect_requests_off_loop(self):
        state = NetworkBlockState(requests_per_second=1000, max_in_flight=1)
        responses = [FakeResponse(439, payload={"pow_challenge": "test"}), FakeResponse(200, payload={"accepted": True}),
                     FakeResponse(302, headers={"Location": "/canonical"}), FakeResponse(200, "<html>catalog</html>")]
        session_seen = []
        def solve(session, response, url, **kwargs):
            session_seen.append(session)
            session.post("https://www.avito.ru/fixture-check", timeout=5)
            return True
        with patch.object(curl_requests.Session, "request", side_effect=responses) as request, patch.object(parser_module, "handle_anti_bot_challenge", side_effect=solve):
            parser = self.create_parser(state)
            original_session = parser.session
            self.assertIn("catalog", await parser.fetch_page_async("https://www.avito.ru/search"))
        self.assertIs(parser.session, original_session)
        self.assertEqual(session_seen, [original_session])
        self.assertEqual(request.call_count, 4)
        self.assertEqual(state.snapshot()["requests"], 4)
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in request.call_args_list))
        self.assertEqual(state.snapshot()["in_flight"], 0)

    async def test_429_never_failovers_or_calls_challenge_solver(self):
        state = NetworkBlockState(requests_per_second=1000)
        with patch.object(curl_requests.Session, "request", return_value=FakeResponse(429, headers={"Retry-After": "120"})) as request, patch.object(parser_module, "handle_anti_bot_challenge") as solve:
            first, second = self.create_parser(state), self.create_parser(state)
            self.assertIsNone(await first.fetch_page_async("https://www.avito.ru/search"))
            self.assertIsNone(await second.fetch_page_async("https://www.avito.ru/search"))
        self.assertEqual(request.call_count, 1)
        solve.assert_not_called()
        self.assertEqual(state.block_streak, 1)
        self.assertEqual(first.last_failure.kind, "rate_limit")
        self.assertGreaterEqual(state.cooldown_remaining(), 119)

    async def test_network_failure_has_one_attempt_and_preserves_event_loop(self):
        state = NetworkBlockState(requests_per_second=1000)
        started = Event()
        def slow_failure(*args, **kwargs):
            started.set()
            time.sleep(0.1)
            raise TimeoutError("fixture timeout")
        with patch.object(curl_requests.Session, "request", side_effect=slow_failure) as request:
            parser = self.create_parser(state)
            task = asyncio.create_task(parser.fetch_page_async("https://www.avito.ru/search"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            self.assertIsNone(await task)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(parser.last_failure.kind, "timeout")
        self.assertEqual(state.snapshot()["in_flight"], 0)

    async def test_request_and_time_budgets_prevent_unbounded_fetch(self):
        state = NetworkBlockState(requests_per_second=1000)
        response = FakeResponse(302, headers={"Location": "/again"})
        with patch.object(curl_requests.Session, "request", return_value=response) as request, patch.object(config, "AVITO_MAX_REQUESTS_PER_FETCH", 2):
            parser = self.create_parser(state)
            self.assertIsNone(await parser.fetch_page_async("https://www.avito.ru/search"))
            self.assertEqual(request.call_count, 2)
            self.assertEqual(parser.last_failure.kind, "request_budget")
        with patch.object(curl_requests.Session, "request") as request:
            parser = self.create_parser(NetworkBlockState())
            self.assertIsNone(await parser.fetch_page_async("https://www.avito.ru/search", deadline=time.monotonic() - 1))
            request.assert_not_called()


class TestBoundedCollection(unittest.IsolatedAsyncioTestCase):
    def catalog(self, cards, next_page=None):
        payload = {"loaderData": {"data": {"catalog": {"items": cards}}}}
        link = f'<a rel="next" href="?s=104&amp;p={next_page}">Next</a>' if next_page else ''
        return '<html><script>' + json.dumps(payload) + '</script>' + link + '</html>'

    def fake_parser(self, pages):
        parser = Mock(spec=AvitoParser)
        parser.fetch_page_async = AsyncMock(side_effect=pages)
        parser.parse_items = AvitoParser.parse_items
        parser.has_catalog = AvitoParser.has_catalog
        parser.next_page_url = AvitoParser.next_page_url
        parser.extract_search_title = lambda *_: "Fixture"
        return parser

    async def test_bounded_pages_deduplicate_and_report_gap(self):
        cards = [{"id": "1", "title": "Fixture"}, {"id": "2", "title": "Fixture"}]
        parser = self.fake_parser([self.catalog(cards, 2), self.catalog(cards + [{"id": "3", "title": "Fixture"}], 3)])
        with patch.object(config, "AVITO_MAX_PAGES_PER_SCAN", 2):
            collection = await collect_source(parser, "https://www.avito.ru/search?s=104")
        self.assertEqual(len(collection.items), 3)
        self.assertEqual(collection.pages, 2)
        self.assertTrue(collection.coverage_gap)
        self.assertEqual(parser.fetch_page_async.await_count, 2)
        self.assertEqual(len({call.kwargs["deadline"] for call in parser.fetch_page_async.call_args_list}), 1)

    async def test_pagination_failure_retains_successful_first_page(self):
        parser = self.fake_parser([self.catalog([{"id": "1", "title": "Fixture"}], 2), None])
        collection = await collect_source(parser, "https://www.avito.ru/search?s=104")
        self.assertEqual(len(collection.items), 1)
        self.assertTrue(collection.coverage_gap)

    async def test_sorted_old_boundary_stops_without_extra_requests(self):
        now = int(time.time())
        cards = [{"id": "1", "title": "Fixture", "sortTimeStamp": (now - 120) * 1000},
                 {"id": "2", "title": "Fixture", "sortTimeStamp": (now - 900) * 1000}]
        parser = self.fake_parser([self.catalog(cards, 2)])
        collection = await collect_source(parser, "https://www.avito.ru/search?s=104")
        self.assertEqual(collection.pages, 1)
        self.assertFalse(collection.coverage_gap)

    async def test_login_page_is_retryable_not_empty_baseline(self):
        parser = self.fake_parser(['<html><body>Login</body></html>'])
        self.assertIsNone(await collect_source(parser, "https://www.avito.ru/search?s=104"))
        self.assertEqual(parser.last_failure.kind, "catalog_missing")


class TestSharedCalibration(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_cache_single_flight_and_expiry(self):
        cache = SourceSnapshotCache(ttl=0.01, maxsize=1)
        calls = 0
        async def fetch(key):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            result = SourceSnapshot("Fixture", (), time.time())
            cache.put(key, result)
            return result
        with patch.object(cache, "_fetch", side_effect=fetch):
            first, second = await asyncio.gather(cache.load("same"), cache.load("same"))
            self.assertIs(first, second)
            self.assertEqual(calls, 1)
        cache.put("other", SourceSnapshot("Other", (), time.time()))
        self.assertIsNone(cache.get("same"))
        await asyncio.sleep(0.02)
        self.assertIsNone(cache.get("other"))

    async def _fetch_with_failures(self, kinds):
        """Each channel of the fake pool fails with its own kind; returns what load() gave."""
        clients = [SimpleNamespace(proxy_id=str(i), _bound_parser=Mock(last_failure=FetchFailure(kind))) for i, kind in enumerate(kinds)]
        pool = Mock(clients=clients)
        pool.acquire_client.side_effect = lambda exclude_ids=(): next(
            (Mock(client=client) for client in clients if client.proxy_id not in exclude_ids), None)
        fallback = Mock(last_failure=None)
        with (
            patch.object(source_cache, "get_proxy_pool", return_value=pool),
            patch.object(source_cache, "collect_source", new=AsyncMock(return_value=None)),
            patch.object(source_cache, "AvitoParser", return_value=fallback),
        ):
            return await SourceSnapshotCache().load("https://www.avito.ru/moskva/x")

    async def test_link_is_bad_only_when_every_channel_saw_no_catalog(self):
        with self.assertRaises(source_cache.BadSourceLink):
            await self._fetch_with_failures(["catalog_missing", "catalog_missing", "http_client"])

    async def test_a_block_on_any_channel_proves_nothing_about_the_link(self):
        self.assertIsNone(await self._fetch_with_failures(["catalog_missing", "forbidden", "catalog_missing"]))

    async def test_scheduler_runtime_groups_sources_and_closes_owned_parsers(self):
        radar = {"id": 1, "user_id": 123, "url": "https://www.avito.ru/search", "tariff_plan": "standart"}
        parser = Mock()
        parser.last_failure = None
        parser.last_coverage_gap = False
        called = asyncio.Event()
        async def process(*args, **kwargs):
            called.set()
            await asyncio.sleep(0.01)
            return True
        with patch.object(config, "MAX_CONCURRENT_WORKERS", 1), patch.object(monitoring, "AvitoParser", wraps=AvitoParser) as constructor, patch.object(monitoring.database, "get_all_active_searches", new=AsyncMock(return_value=[radar, dict(radar, id=2)])), patch.object(scheduler, "process_url_group", new=process):
            constructor.return_value = parser
            task = asyncio.create_task(monitoring.run_monitoring(Mock(send_message=AsyncMock())))
            await asyncio.wait_for(called.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(monitoring.MONITORING_STATS["sources"], 1)
        self.assertEqual(monitoring.MONITORING_STATS["subscriptions"], 2)
        parser.close.assert_called_once()

    def _crash_fixture(self, process, *radars):
        parser = Mock(last_failure=None, last_coverage_gap=False)
        return (
            patch.object(config, "MAX_CONCURRENT_WORKERS", 1),
            patch.object(config, "ADMIN_IDS", [7]),
            patch.object(config, "SCAN_STARTUP_SPREAD", 0),
            patch.object(config, "SOURCE_BACKOFF_MAX", 0.05),
            patch.object(monitoring, "WORKER_CRASH_PAUSE", 0.01),
            patch.object(monitoring, "AvitoParser", Mock(return_value=parser, normalize_url=staticmethod(lambda url: url))),
            patch.object(monitoring.database, "get_all_active_searches", new=AsyncMock(return_value=list(radars))),
            patch.object(scheduler, "process_url_group", new=process),
            patch.object(monitoring.METRICS, "record_scan_attempt", side_effect=lambda **kw: self._maybe_crash(kw)),
        )

    def _maybe_crash(self, fields):
        if not fields["success"]:
            raise OverflowError("bad link")

    async def test_one_crashing_link_leaves_the_rest_scanning_and_alerts_once(self):
        bad = {"id": 1, "user_id": 1, "url": "https://www.avito.ru/bad", "tariff_plan": "standart"}
        good = {"id": 2, "user_id": 2, "url": "https://www.avito.ru/good", "tariff_plan": "standart"}
        bad_runs, good_runs = 0, 0
        done = asyncio.Event()

        async def process(_bot, _parser, key, *args, **kwargs):
            nonlocal bad_runs, good_runs
            if "bad" in key:
                bad_runs += 1
                if bad_runs >= 3:
                    done.set()
                return False
            good_runs += 1
            return True

        bot = Mock(send_message=AsyncMock())
        patches = self._crash_fixture(process, bad, good)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        task = asyncio.create_task(monitoring.run_monitoring(bot))
        await asyncio.wait_for(done.wait(), 5)
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertGreaterEqual(good_runs, 1)
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertIn("OverflowError", bot.send_message.await_args.kwargs["text"])

    async def test_crashes_with_no_healthy_scans_reach_the_supervisor(self):
        bad = {"id": 1, "user_id": 1, "url": "https://www.avito.ru/bad", "tariff_plan": "standart"}

        async def process(*args, **kwargs):
            return False

        patches = (
            *self._crash_fixture(process, bad),
            patch.object(config, "HEALTH_ALERT_AFTER_SECONDS", 0),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with self.assertRaises(OverflowError):
            await asyncio.wait_for(monitoring.run_monitoring(Mock(send_message=AsyncMock())), 5)


class TestRuntimeMetricsAndCapacity(unittest.TestCase):
    def test_percentiles_capacity_reserve_and_credential_sanitization(self):
        import runtime_metrics

        m = runtime_metrics.RuntimeMetricsRegistry()
        # Cold-start capacity uses 1.3 req/scan and 0.8 reserve
        cap = runtime_metrics.calculate_capacity_report(
            unique_sources=300,
            proxy_endpoints=[{"proxy_id": f"p{i}", "state": "healthy", "current_rps": 1.0} for i in range(2)],
            rolling_mean_cost=None,
            p95_cost=None,
            completed_scans_count=0,
        )
        self.assertEqual(cap["capacity_mode"], "deficit")
        self.assertEqual(cap["healthy_proxies"], 2)
        self.assertTrue(cap["needs_more_proxies"])

        # Record deliveries for pro, standart, base and check separate deliver_at -> t_sent vs t_queued -> t_sent
        now = time.time()
        m.record_delivery(
            tariff="pro",
            t_pub=now - 5.0,
            t_pub_reliable=True,
            t_seen=now - 1.2,
            t_queued=now - 1.0,
            deliver_at=now - 1.0,
            t_sent=now,
        )
        m.record_delivery(
            tariff="standart",
            t_pub=None,
            t_pub_reliable=False,
            t_seen=now - 2.2,
            t_queued=now - 2.0,
            deliver_at=now - 1.0,
            t_sent=now,
        )
        m.record_delivery(
            tariff="base",
            t_pub=None,
            t_pub_reliable=False,
            t_seen=now - 2.7,
            t_queued=now - 2.5,
            deliver_at=now - 1.0,
            t_sent=now,
        )
        m.record_http_request("p0", "http://secret_user:top_secret_pass@10.20.30.40:8080", 200, 0.35)
        snap = m.snapshot(
            active_unique_urls=20,
            proxy_snapshots=[
                {
                    "proxy_id": "p0",
                    "masked_url": runtime_metrics.mask_proxy_url("http://secret_user:top_secret_pass@10.20.30.40:8080"),
                    "state": "healthy",
                    "current_rps": 1.0,
                }
            ],
        )
        by_t = snap["by_tariff"]
        self.assertLessEqual(by_t["pro"]["deliver_at_to_sent_seconds"]["p95"], 2.0)
        self.assertAlmostEqual(by_t["standart"]["queued_to_sent_seconds"]["p95"], 2.0, delta=0.5)
        self.assertAlmostEqual(by_t["base"]["queued_to_sent_seconds"]["p95"], 2.5, delta=0.5)

        report = m.format_stats_report()
        self.assertIn("Мониторинг", report)
        self.assertNotIn("top_secret_pass", report)
        self.assertNotIn("secret_user", report)


class TestProxyPoolLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_validation_quarantine_half_open_and_hot_reload(self):
        import tempfile
        from pathlib import Path
        import runtime_metrics
        from proxy_pool import ProxyPool, parse_and_validate_proxy_line

        # Percent-encoded special chars in user/pass are valid; unencoded '@' or invalid schemes never leak credentials
        parsed_url = parse_and_validate_proxy_line("http://user%40name:p%3Aa%40ss@192.0.2.10:8080", 1)
        self.assertIn("192.0.2.10:8080", parsed_url)
        self.assertEqual(runtime_metrics.mask_proxy_url(parsed_url), "http://***@192.0.2.10:8080")

        with self.assertRaises(ValueError) as ctx1:
            parse_and_validate_proxy_line("http://user@name:secret_pass@192.0.2.10:8080", 1)
        self.assertNotIn("secret_pass", str(ctx1.exception))

        with self.assertRaises(ValueError) as ctx2:
            parse_and_validate_proxy_line("ftp://secret_login:secret_pass@192.0.2.10:8080", 2)
        self.assertNotIn("secret_pass", str(ctx2.exception))

        with tempfile.TemporaryDirectory(prefix="avito-proxies-") as tmp:
            pfile = Path(tmp) / "proxies.txt"
            pfile.write_text(
                "# Primary proxies\nhttp://u1:p1@192.0.2.1:8001\nhttp://u2:p2@192.0.2.2:8002\n",
                encoding="utf-8",
            )
            pool = ProxyPool(proxy_pool_file=pfile, fallback_proxy_url="", allow_direct=False)
            self.assertEqual(len(pool.clients), 2)
            self.assertEqual(pool.healthy_count(), 2)

            # Quarantine first proxy via 3 transport errors, and also test block rate reduction
            c1 = pool.clients[0]
            for _ in range(3):
                c1.record_transport_error()
            self.assertEqual(c1.state, "quarantined")
            self.assertEqual(pool.healthy_count(), 1)
            # The rate is cut only when blocks dominate the recent window, not on Avito's background 403s.
            for _ in range(10):
                c1.record_raw_http_response(403, 0.2)
            c1.record_final_block("test_403")
            self.assertLess(c1.effective_rate, c1.configured_rate)

            # Half-open recovery after quarantine expires
            c1.quarantined_until = time.monotonic() - 0.01
            lease = pool.acquire_client(exclude_ids={pool.clients[1].proxy_id})
            self.assertIsNotNone(lease)
            self.assertIs(lease.client, c1)
            self.assertEqual(c1.state, "half_open")
            # Second concurrent acquire while half-open probe is in flight is rejected
            self.assertIsNone(pool.acquire_client(exclude_ids={pool.clients[1].proxy_id}))
            c1.record_success()
            lease.release()
            self.assertEqual(c1.state, "healthy")

            # Successes push the block share back under the cut ratio and restore effective_rate by *1.25
            start_rps = c1.effective_rate
            for _ in range(50):
                c1.record_raw_http_response(200, 0.2)
            self.assertGreater(c1.effective_rate, start_rps)

            # Hot reload adds a 3rd proxy and removes the 2nd without dropping c1
            pfile.write_text(
                "http://u1:p1@192.0.2.1:8001\nhttp://u3:p3@192.0.2.3:8003\n",
                encoding="utf-8",
            )
            reload_info = await pool.reload_async(proxy_pool_file=pfile, fallback_proxy_url="", allow_direct=False)
            self.assertEqual(reload_info["total"], 2)
            self.assertEqual(reload_info["added"], 1)
            self.assertEqual(reload_info["removed"], 1)
            await pool.close()


class TestFastSkipCatchupAndHysteresis(unittest.IsolatedAsyncioTestCase):
    async def test_fast_skip_skips_page_2_when_top_3_match(self):
        cards1 = [
            {"id": "10", "title": "Item 10", "sortTimeStamp": int(time.time() * 1000)},
            {"id": "20", "title": "Item 20", "sortTimeStamp": int(time.time() * 1000)},
            {"id": "30", "title": "Item 30", "sortTimeStamp": int(time.time() * 1000)},
        ]
        page1 = (
            '<html><script>'
            + json.dumps({"loaderData": {"data": {"catalog": {"items": cards1}}}})
            + '</script><a rel="next" href="?s=104&amp;p=2">Next</a></html>'
        )
        parser = Mock(spec=AvitoParser)
        parser.fetch_page_async = AsyncMock(side_effect=[page1, "<html>should not fetch</html>"])
        parser.parse_items = AvitoParser.parse_items
        parser.has_catalog = AvitoParser.has_catalog
        parser.next_page_url = AvitoParser.next_page_url

        collection = await collect_source(
            parser,
            "https://www.avito.ru/search?s=104",
            can_fast_skip=True,
            last_top_ids=("10", "20", "30"),
        )
        self.assertIsNotNone(collection)
        self.assertTrue(collection.fast_skipped)
        self.assertEqual(collection.pages, 1)
        self.assertEqual(parser.fetch_page_async.await_count, 1)

    def test_source_backoff_is_short_and_clears_after_one_success(self):
        planner = DeadlineScheduler(use_source_backoff=True)
        planner.sync({"u": [{"id": 1}]}, None, 0.0)
        task = planner.take_due(0.0)

        # Single proxy failure (exhausted_proxies=False) does NOT increment source_failure_streak
        planner.complete(task, 1.0, False, exhausted_proxies=False)
        self.assertEqual(planner.sources["u"].source_failure_streak, 0)

        # Every channel failed (exhausted_proxies=True) twice -> streak 2 -> 10s backoff
        t2 = planner.take_due(10.0)
        planner.complete(t2, 11.0, False, exhausted_proxies=True)
        t3 = planner.take_due(50.0)
        planner.complete(t3, 51.0, False, exhausted_proxies=True)
        self.assertEqual(planner.sources["u"].source_failure_streak, 2)
        self.assertAlmostEqual(planner.sources["u"].due_at - 51.0, 10.0, delta=0.01)
        for at in (100.0, 200.0, 300.0, 400.0):
            tk = planner.take_due(at)
            planner.complete(tk, at + 1.0, False, exhausted_proxies=True)
        state = planner.sources["u"]
        self.assertAlmostEqual(state.due_at - 401.0, max(20.0, state.interval * config.SOURCE_SLOW_MAX_FACTOR), delta=0.01)

        # One healthy scan clears the streak
        due = planner.sources["u"].due_at
        tk = planner.take_due(due + 0.1)
        planner.complete(tk, due + 0.2, True)
        self.assertEqual(planner.sources["u"].source_failure_streak, 0)

    def test_source_backoff_survives_huge_persisted_streak(self):
        # A link failing for hours reloads its streak after a restart: 2 ** 1024 must not overflow float
        planner = DeadlineScheduler(use_source_backoff=True)
        planner.sync({"u": [{"id": 1}]}, None, 0.0, persisted_states={"u": {"failure_streak": 1024}})
        task = planner.take_due(0.0)

        planner.complete(task, 1.0, False, exhausted_proxies=True)

        state = planner.sources["u"]
        self.assertEqual(state.source_failure_streak, 1025)
        self.assertAlmostEqual(state.due_at - 1.0, max(20.0, state.interval * config.SOURCE_SLOW_MAX_FACTOR), delta=0.01)

    def test_failing_link_slows_down_past_its_tariff_interval_and_recovers(self):
        planner = DeadlineScheduler(use_source_backoff=True)
        planner.sync({"u": [{"id": 1}]}, {"u": 40.0}, 0.0)
        delays = []
        now = 0.0
        for _ in range(7):
            task = planner.take_due(now)
            planner.complete(task, now, False, exhausted_proxies=True)
            delays.append(planner.sources["u"].due_at - now)
            now = planner.sources["u"].due_at

        self.assertEqual(delays[:2], [5.0, 10.0])  # one bad minute stays fast
        self.assertEqual(delays[2:], [80.0, 120.0, 160.0, 200.0, 200.0])

        task = planner.take_due(now)
        planner.complete(task, now, True)
        self.assertEqual(planner.sources["u"].source_failure_streak, 0)
        self.assertEqual(planner.sources["u"].due_at - now, 40.0)

    def test_abandon_returns_a_crashed_scan_to_the_schedule(self):
        planner = DeadlineScheduler()
        planner.sync({"u": [{"id": 1}]}, None, 0.0)
        task = planner.take_due(0.0)

        planner.abandon(task, 1.0)

        self.assertNotIn("u", planner.running)
        self.assertIsNone(planner.take_due(1.0 + config.SOURCE_BACKOFF_MAX - 0.1))
        self.assertIsNotNone(planner.take_due(1.0 + config.SOURCE_BACKOFF_MAX + 0.1))

    def test_crash_inside_complete_keeps_the_source_recoverable(self):
        planner = DeadlineScheduler()
        planner.sync({"u": [{"id": 1}]}, None, 0.0)
        task = planner.take_due(0.0)

        with patch.object(planner, "_apply_failure", side_effect=OverflowError("boom")):
            with self.assertRaises(OverflowError):
                planner.complete(task, 1.0, False)
        planner.abandon(task, 1.0)

        self.assertIsNotNone(planner.take_due(1.0 + config.SOURCE_BACKOFF_MAX + 0.1))

    def test_abandon_ignores_a_finished_or_foreign_task(self):
        planner = DeadlineScheduler()
        planner.sync({"u": [{"id": 1}]}, None, 0.0)
        finished = planner.take_due(0.0)
        planner.complete(finished, 1.0, True)
        due = planner.sources["u"].due_at
        current = planner.take_due(due + 0.1)

        planner.abandon(finished, due + 0.2)

        self.assertIs(planner.running["u"], current)
        self.assertTrue(planner.sources["u"].http_in_flight)


if __name__ == "__main__":
    unittest.main()
