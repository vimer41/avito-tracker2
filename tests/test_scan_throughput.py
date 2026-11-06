import asyncio
import time
import unittest
from unittest.mock import patch

from tests import _environment  # noqa: F401  (isolated data dir)
import config
import parser as parser_module
import proxy_pool
from curl_cffi import CurlOpt
from parser import AvitoParser, channel_parsers, lane_parser
from runtime_metrics import RuntimeMetricsRegistry, calculate_capacity_report
from scan_planner import stretch_factor

PROXY_URL = "http://user:pass@1.2.3.4:5500"


class TestLanes(unittest.IsolatedAsyncioTestCase):
    def pool(self, mode: str = "proxy"):
        patcher = patch.object(config, "NET_MODE", mode)
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch.object(config, "PROXY_URLS", [PROXY_URL]), patch.object(config, "PROXY_MAX_IN_FLIGHT", 2):
            return proxy_pool.ProxyPool(proxy_pool_file="", fallback_proxy_url="", allow_direct=False)

    def test_a_proxy_serves_two_scans_on_two_lanes_and_the_server_one(self):
        with patch.object(config, "PROXY_MAX_IN_FLIGHT", 2):
            proxy, server = proxy_pool.ProxyClient(PROXY_URL), proxy_pool.ProxyClient(None)
        self.assertEqual((proxy.max_in_flight, server.max_in_flight), (2, 1))
        pool = self.pool()
        client = pool.clients[0]
        first, second = pool.acquire_client(), pool.acquire_client()
        self.assertEqual((first.client, second.client), (client, client))
        self.assertEqual({first.lane, second.lane}, {0, 1})
        self.assertFalse(pool.has_free_lane(), "both lanes are taken")
        first.release()
        self.assertTrue(pool.has_free_lane())
        third = pool.acquire_client()
        self.assertEqual(third.lane, first.lane, "the freed lane is reused")
        second.release()
        third.release()
        self.assertEqual((client.in_flight, client._lane_users), (0, {}))

    async def test_a_freed_lane_wakes_a_waiting_worker(self):
        pool = self.pool()
        leases = [pool.acquire_client(), pool.acquire_client()]
        waiter = asyncio.create_task(pool.wait_for_free_lane(5.0))
        await asyncio.sleep(0)
        started = time.monotonic()
        leases[0].release()
        await waiter
        self.assertLess(time.monotonic() - started, 1.0)
        leases[1].release()

    async def test_lanes_have_their_own_sessions_and_parsers(self):
        client = proxy_pool.ProxyClient(PROXY_URL, max_in_flight=2)
        made = []

        def factory():
            session = type("Session", (), {"cookies": None, "close": lambda self: made.append("closed")})()
            made.append(session)
            return session

        first = await client.get_or_create_async_session(factory, lane=0)
        second = await client.get_or_create_async_session(factory, lane=1)
        self.assertIsNot(first, second)
        await client.reset_async_session(1)
        self.assertIs(await client.get_or_create_async_session(factory, lane=0), first, "a reset of lane 1 leaves lane 0")
        built = []

        def parser_factory(**kwargs):
            built.append(kwargs)
            return type("FakeParser", (), {"close": lambda self: None})()

        zero, one = lane_parser(client, 0, parser_factory), lane_parser(client, 1, parser_factory)
        self.assertIsNot(zero, one)
        self.assertIs(client._bound_parser, zero)
        self.assertEqual(built, [{"proxy_client": client}, {"proxy_client": client, "lane": 1}])
        self.assertIs(lane_parser(client, 1, parser_factory), one)
        self.assertEqual(len(channel_parsers(client)), 2)
        await client.close()

    def test_smart_mode_still_lets_one_scan_queue_behind_the_server(self):
        with patch.object(config, "PROXY_URLS", [PROXY_URL]), patch.object(config, "NET_MODE", "smart"):
            pool = proxy_pool.ProxyPool(proxy_pool_file="", fallback_proxy_url="", allow_direct=True)
            server = next(c for c in pool.clients if c.is_direct)
            reserve = next(c for c in pool.clients if not c.is_direct)
            reserve._outcomes.extend([True] * 10)  # a reserve proxy with a good record
            server.in_flight = 1
            self.assertTrue(pool.has_free_lane(), "one more scan may wait behind the server")
            server.in_flight = 2
            lease = pool.acquire_client()
            self.assertFalse(lease.client.is_direct, "the next one goes to a reserve proxy")
            lease.release()
            server.in_flight = 0

    def test_lanes_switch_live_for_proxies_only(self):
        with patch.object(config, "PROXY_URLS", [PROXY_URL]), patch.object(config, "PROXY_MAX_IN_FLIGHT", 2), \
                patch.object(config, "NET_MODE", "both"):
            pool = proxy_pool.ProxyPool(proxy_pool_file="", fallback_proxy_url="", allow_direct=True)
        pool.set_lanes(1)
        self.assertEqual(sorted(c.max_in_flight for c in pool.clients), [1, 1])
        pool.set_lanes(2)
        self.assertEqual({c.is_direct: c.max_in_flight for c in pool.clients}, {True: 1, False: 2})
        self.assertEqual(pool.total_lanes(), 3)


class TestHonestCapacity(unittest.TestCase):
    def test_capacity_is_lanes_over_service_time_not_the_rate_limit(self):
        clock = [1000.0]
        metrics = RuntimeMetricsRegistry(monotonic_fn=lambda: clock[0], wall_clock_fn=lambda: 1_700_000_000.0)
        url = "http://***@1.2.3.4:5500"
        metrics.update_proxy_state("p1", masked_url=url, state="healthy", configured_rate=1.5, effective_rate=1.5, lanes=2)
        for _ in range(12):
            metrics.record_http_request("p1", url, 200, 2.9)
            metrics.record_channel_use("p1", url, wait_seconds=0.0, busy_seconds=3.0)
        metrics.update_proxy_state("p2", masked_url="http://***@5.6.7.8:5500", state="healthy", configured_rate=1.5,
                                   effective_rate=1.5, lanes=2)
        summaries = {p["proxy_id"]: p for p in metrics.get_proxy_summaries()}
        self.assertEqual(summaries["p1"]["service_time_seconds"], 3.0)
        self.assertAlmostEqual(summaries["p1"]["throughput_rps"], 0.6667, places=3)
        self.assertIsNone(summaries["p2"]["throughput_rps"], "an idle channel is not measured")
        report = calculate_capacity_report(
            unique_sources=100, proxy_endpoints=list(summaries.values()), rolling_mean_cost=1.2, p95_cost=1.3,
            completed_scans_count=100, interval_min=20.0, interval_max=20.0,
        )
        self.assertAlmostEqual(report["available_rate_now"], 1.3334, places=3, msg="the idle one counts as the measured one")
        self.assertEqual(report["measured_channels"], 1)

    def test_stretch_keeps_tariff_proportions_and_has_a_ceiling(self):
        self.assertEqual(stretch_factor(3.0, 4.0, utilization=0.9, max_factor=3.0), 1.0)
        self.assertEqual(stretch_factor(7.0, 4.0, utilization=0.9, max_factor=3.0), 1.94)
        self.assertEqual(stretch_factor(70.0, 4.0, utilization=0.9, max_factor=3.0), 3.0)
        self.assertEqual(stretch_factor(7.0, 0.0, utilization=0.9, max_factor=3.0), 1.0, "nothing measured: no stretch")


class TestFetchDetails(unittest.TestCase):
    def test_canonical_addresses_are_shared_by_all_channels(self):
        first = AvitoParser(proxy_client=proxy_pool.ProxyClient(PROXY_URL))
        second = AvitoParser(proxy_client=proxy_pool.ProxyClient("http://user:pass@5.6.7.8:5500"))
        try:
            first._canonical_cache["https://www.avito.ru/kazan?q=x&s=104"] = "https://www.avito.ru/kazan/hobbi?q=x&s=104"
            self.assertEqual(second._canonical_cache.get("https://www.avito.ru/kazan?q=x&s=104"),
                             "https://www.avito.ru/kazan/hobbi?q=x&s=104")
        finally:
            parser_module.CANONICAL_URLS.clear()
            first.close()
            second.close()

    def test_a_silent_request_is_dropped_after_the_stall_timeout(self):
        with patch.object(config, "AVITO_STALL_TIMEOUT", 5.0):
            self.assertEqual(parser_module.stall_curl_options(), {CurlOpt.LOW_SPEED_LIMIT: 1, CurlOpt.LOW_SPEED_TIME: 5})


if __name__ == "__main__":
    unittest.main()
