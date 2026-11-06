"""Hot ingestion path: in-memory dedup, token-bucket pacing and batched radar eligibility."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment
import config
import database
from collector import SourceCollection, settled_item_ids
from parser import AvitoItem
from proxy_pool import ProxyClient, ProxyPool
from seen_cache import InMemorySeenStore


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestInMemorySeenStore(unittest.IsolatedAsyncioTestCase):
    async def test_known_only_after_remember_with_same_revision(self):
        store = InMemorySeenStore(ttl_seconds=60, max_per_source=100)
        self.assertFalse(await store.all_known("src", "r1", ["1", "2"]))
        await store.remember("src", "r1", ["1", "2", "3"])
        self.assertTrue(await store.all_known("src", "r1", ["1", "2"]))
        self.assertFalse(await store.all_known("src", "r1", ["1", "4"]))
        # New radar joined the group -> its fanout must run even for old IDs.
        self.assertFalse(await store.all_known("src", "r2", ["1", "2"]))
        self.assertFalse(await store.all_known("other", "r1", ["1"]))

    async def test_empty_page_is_never_known(self):
        store = InMemorySeenStore(ttl_seconds=60, max_per_source=100)
        await store.remember("src", "r1", ["1"])
        self.assertFalse(await store.all_known("src", "r1", []))

    async def test_revision_change_resets_source(self):
        store = InMemorySeenStore(ttl_seconds=60, max_per_source=100)
        await store.remember("src", "r1", ["1"])
        await store.remember("src", "r2", ["2"])
        self.assertTrue(await store.all_known("src", "r2", ["2"]))
        self.assertFalse(await store.all_known("src", "r2", ["1"]))

    async def test_ttl_and_lru_bounds(self):
        clock = FakeClock()
        store = InMemorySeenStore(ttl_seconds=10, max_per_source=3, monotonic_fn=clock)
        await store.remember("src", "r", ["1", "2", "3"])
        await store.remember("src", "r", ["4"])
        self.assertEqual(len(store), 3)
        self.assertFalse(await store.all_known("src", "r", ["1"]))
        self.assertTrue(await store.all_known("src", "r", ["2", "3", "4"]))
        clock.now += 11
        self.assertFalse(await store.all_known("src", "r", ["4"]))

    async def test_prune_and_forget(self):
        store = InMemorySeenStore(ttl_seconds=60, max_per_source=10)
        await store.remember("a", "r", ["1"])
        await store.remember("b", "r", ["2"])
        store.prune({"a": []})
        self.assertFalse(await store.all_known("b", "r", ["2"]))
        await store.forget("a")
        self.assertEqual(len(store), 0)


class TestFanoutSkipDecision(unittest.IsolatedAsyncioTestCase):
    def make(self, ids, *, fast_skipped=False):
        now = time.time()
        items = tuple(AvitoItem(item_id=i, title="t", price="1", url="u", timestamp=int(now - 10)) for i in ids)
        return SourceCollection(title="x", items=items, captured_at=now, pages=1, coverage_gap=False,
                                elapsed_ms=1, fast_skipped=fast_skipped)

    async def test_reshuffled_known_page_skips_but_any_new_id_or_guard_does_not(self):
        import monitoring
        from scan_planner import ScheduledScan

        store = InMemorySeenStore(ttl_seconds=60, max_per_source=100)
        task = ScheduledScan(key="src", radars=(), scheduled_at=0, started_at=0, retry_attempt=0,
                             generation=1, can_fast_skip=True, group_revision="rev")
        first = self.make(["1", "2", "3", "4"])
        self.assertFalse(await monitoring.nothing_new_for_fanout(store, task, first, False))
        await store.remember(task.key, task.group_revision, settled_item_ids(first))

        reshuffled = self.make(["4", "3", "1", "2"])
        self.assertTrue(await monitoring.nothing_new_for_fanout(store, task, reshuffled, False))
        self.assertFalse(await monitoring.nothing_new_for_fanout(store, task, self.make(["5", "1"]), False))
        # Planner guards win: catch-up, a required full scan, or an already fast-skipped page.
        self.assertFalse(await monitoring.nothing_new_for_fanout(store, task, reshuffled, True))
        full_scan_due = ScheduledScan(**{**task.__dict__, "can_fast_skip": False})
        self.assertFalse(await monitoring.nothing_new_for_fanout(store, full_scan_due, reshuffled, False))
        self.assertFalse(await monitoring.nothing_new_for_fanout(store, task, self.make(["1"], fast_skipped=True), False))


class TestSettledItems(unittest.TestCase):
    def test_items_without_reliable_age_stay_for_fanout_recheck(self):
        now = time.time()
        items = (
            AvitoItem(item_id="dated", title="t", price="1", url="u", timestamp=int(now - 30)),
            AvitoItem(item_id="relative", title="t", price="1", url="u", date="5 минут назад"),
            AvitoItem(item_id="unknown", title="t", price="1", url="u", date="Дата не указана"),
        )
        collection = SourceCollection(title="x", items=items, captured_at=now, pages=1, coverage_gap=False, elapsed_ms=1)
        self.assertEqual(settled_item_ids(collection), ["dated", "relative"])


class TestTokenBucketPacing(unittest.TestCase):
    def make_client(self, clock, rate=2.0):
        with patch.object(config, "PROXY_RATE_BURST", 3.0):
            client = ProxyClient("http://user:pass@127.0.0.1:8080", configured_rate=rate, monotonic_fn=clock)
        return client

    def test_burst_is_free_then_sustained_rate_is_enforced(self):
        clock = FakeClock()
        client = self.make_client(clock)
        with patch.object(config, "PROXY_RATE_BURST", 3.0):
            self.assertEqual([client._reserve_pacing_delay() for _ in range(3)], [0.0, 0.0, 0.0])
            # Bucket empty: next requests are spaced at 1/rate = 0.5 s.
            self.assertAlmostEqual(client._reserve_pacing_delay(), 0.5)
            self.assertAlmostEqual(client._reserve_pacing_delay(), 1.0)
            self.assertGreater(client.estimated_ready_at(clock.now), clock.now)

    def test_idle_client_refills_up_to_burst(self):
        clock = FakeClock()
        client = self.make_client(clock)
        with patch.object(config, "PROXY_RATE_BURST", 3.0):
            for _ in range(3):
                client._reserve_pacing_delay()
            clock.now += 60  # long idle: refill is capped at the burst size
            self.assertEqual([client._reserve_pacing_delay() for _ in range(3)], [0.0, 0.0, 0.0])
            self.assertAlmostEqual(client._reserve_pacing_delay(), 0.5)

    def test_rate_halving_after_block_slows_pacing(self):
        clock = FakeClock()
        client = self.make_client(clock, rate=2.0)
        with patch.object(config, "PROXY_RATE_BURST", 1.0):
            client._tokens = 1.0
            client.effective_rate = 1.0
            client._last_block_at = clock.now
            self.assertEqual(client._reserve_pacing_delay(), 0.0)
            self.assertAlmostEqual(client._reserve_pacing_delay(), 1.0)

    def test_cut_is_judged_by_fresh_answers_and_calm_restores_rate(self):
        clock = FakeClock()
        client = self.make_client(clock, rate=1.5)
        for _ in range(10):
            client.record_raw_http_response(403, 0.2)
        client.record_final_block("forbidden")
        self.assertAlmostEqual(client.effective_rate, 1.05)
        # The old 403s are still in the window: one more block right away must not cut again.
        client.record_raw_http_response(403, 0.2)
        client.record_final_block("forbidden")
        self.assertAlmostEqual(client.effective_rate, 1.05)
        # Timeouts do not count against recovery; after a calm stretch the rate steps up without traffic.
        client.record_transport_error()
        clock.now += config.PROXY_RATE_CALM_SECONDS
        client._reserve_pacing_delay()
        self.assertAlmostEqual(client.effective_rate, 1.3125)
        client._reserve_pacing_delay()  # one step per calm period
        self.assertAlmostEqual(client.effective_rate, 1.3125)


class TestChannelRouting(unittest.TestCase):
    """Load goes to proxies that actually answer; the server and weak proxies are a reserve."""

    def setUp(self):
        self.clock = FakeClock()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        pfile = Path(self.temp_dir.name) / "proxies.txt"
        pfile.write_text(
            "http://u1:p1@192.0.2.1:8001\nhttp://u2:p2@192.0.2.2:8002\nhttp://u3:p3@192.0.2.3:8003\n",
            encoding="utf-8",
        )
        net_mode = patch.object(config, "NET_MODE", "both")
        net_mode.start()
        self.addCleanup(net_mode.stop)
        self.pool = ProxyPool(proxy_pool_file=pfile, fallback_proxy_url="", allow_direct=True, monotonic_fn=self.clock)
        self.good, self.other, self.weak = self.pool.clients[:3]
        self.direct = next(client for client in self.pool.clients if client.is_direct)

    def answer(self, client, *statuses):
        for status in statuses:
            client.record_raw_http_response(status, 0.1)

    def pick(self, seconds_between=2.0):
        lease = self.pool.acquire_client()
        lease.release()
        self.clock.now += seconds_between
        return lease.client

    def settle_channels(self):
        """Everyone has a measured history and was used just now, so no probe is due."""
        self.answer(self.good, *[200] * 10)
        self.answer(self.other, *[200] * 10)
        self.answer(self.weak, *[200, 403] * 5)
        self.answer(self.direct, *[200, 403] * 5)
        for client in self.pool.clients:
            client.last_acquired_at = self.clock.now
        self.clock.now += 2.0

    def test_new_channels_count_as_good_but_server_stays_reserve(self):
        self.answer(self.direct, *[200] * 10)
        self.direct.last_acquired_at = self.clock.now
        picked = {self.pick(0.0) for _ in range(3)}
        self.assertEqual(picked, {self.good, self.other, self.weak})

    def test_proven_proxies_rotate_and_weak_ones_wait(self):
        self.settle_channels()
        picked = [self.pick(1.0) for _ in range(8)]
        self.assertEqual(set(picked), {self.good, self.other})
        self.assertLessEqual(abs(picked.count(self.good) - picked.count(self.other)), 1)

    def test_load_above_preferred_rate_spills_to_the_better_reserve(self):
        self.settle_channels()
        self.answer(self.direct, 200, 200)  # 60 % ok beats the weak proxy's 50 %
        self.clock.now += 2.0
        picked = [self.pick(0.1) for _ in range(3)]
        self.assertEqual(picked[:2].count(self.direct), 0)
        self.assertIs(picked[2], self.direct)

    def test_timeouts_drop_a_proxy_and_a_probe_brings_it_back(self):
        self.settle_channels()
        for _ in range(4):
            self.good.record_raw_http_response(None, 0.0, is_transport_error=True)
        self.good._state, self.good.quarantined_until = "healthy", 0.0
        self.assertLess(self.good.recent_health(), config.PROXY_GOOD_HEALTH)
        self.assertIs(self.pick(1.0), self.other)
        self.clock.now += config.PROXY_PROBE_INTERVAL
        self.weak.last_acquired_at = self.direct.last_acquired_at = self.clock.now
        self.assertIs(self.pick(1.0), self.good)

        self.good.transport_failure_streak = 0
        self.answer(self.good, 200)
        self.clock.now += config.PROXY_PROBE_RETRY
        self.assertIs(self.pick(1.0), self.good)  # a success brings the next probe forward
        self.answer(self.good, *[200] * 6)
        self.assertGreaterEqual(self.good.recent_health(), config.PROXY_GOOD_HEALTH)

    def test_only_available_channel_is_used_whatever_its_score(self):
        self.settle_channels()
        lease = self.pool.acquire_client(exclude_ids={self.good.proxy_id, self.other.proxy_id, self.direct.proxy_id})
        self.assertIs(lease.client, self.weak)
        lease.release()


class TestSmartRouting(TestChannelRouting):
    """Smart mode: the server scans alone while it copes; proxies wait in reserve without probes."""

    def setUp(self):
        super().setUp()
        smart = patch.object(config, "NET_MODE", "smart")
        smart.start()
        self.addCleanup(smart.stop)

    # The «both» routing tests do not apply to a server-first pool.
    test_new_channels_count_as_good_but_server_stays_reserve = None
    test_proven_proxies_rotate_and_weak_ones_wait = None
    test_load_above_preferred_rate_spills_to_the_better_reserve = None
    test_timeouts_drop_a_proxy_and_a_probe_brings_it_back = None

    def test_server_scans_alone_and_idle_proxies_are_not_probed(self):
        self.settle_channels()
        self.answer(self.direct, *[200] * 20)  # the server is fine
        picked = [self.pick(config.PROXY_PROBE_INTERVAL + 1) for _ in range(5)]
        self.assertEqual(set(picked), {self.direct}, "no proxy gets a probe while the server copes")

    def test_after_a_server_failure_the_best_reserve_proxy_takes_the_retry(self):
        self.answer(self.direct, *[200] * 10)
        self.answer(self.good, *[200] * 20)
        self.answer(self.other, *[200, None] * 10)
        self.answer(self.weak, *[403] * 20)
        self.weak._state, self.weak.quarantined_until = "healthy", 0.0
        first = self.pool.acquire_client()
        self.assertIs(first.client, self.direct)
        retry = self.pool.acquire_client(exclude_ids={self.direct.proxy_id})
        self.assertIs(retry.client, self.good)
        first.release()
        retry.release()

    def test_busy_server_lets_one_scan_wait_then_spills_to_a_good_proxy(self):
        self.answer(self.direct, *[200] * 10)
        self.answer(self.good, *[200] * 20)
        leases = [self.pool.acquire_client() for _ in range(3)]
        self.assertEqual([lease.client for lease in leases[:2]], [self.direct, self.direct])
        self.assertIs(leases[2].client, self.good)
        for lease in leases:
            lease.release()

    def test_high_load_brings_proxies_in_and_they_stay_for_the_hold(self):
        self.settle_channels()
        self.answer(self.direct, *[200] * 20)  # the server is fine: only the load decides here
        self.assertTrue(self.pool.smart_status()["server_first"])
        self.pool.note_demand(config.SMART_DIRECT_MAX_RPS * 2)
        self.assertIn(self.pick(1.0), {self.good, self.other}, "proven proxies lead as in «both»")
        self.pool.note_demand(0.05)
        self.assertNotEqual(self.pick(1.0), self.direct, "proxies stay in work for the hold")
        self.clock.now += config.SMART_HOLD_SECONDS
        self.assertIs(self.pick(1.0), self.direct)
        self.assertTrue(self.pool.smart_status()["server_first"])

    def test_blocked_server_brings_proxies_in(self):
        self.settle_channels()
        self.answer(self.direct, *[403, 200] * 10)
        status = self.pool.smart_status()
        self.assertFalse(status["server_first"])
        self.assertIn("блокирует сервер: 50%", status["reason"])
        self.assertIn(self.pick(1.0), {self.good, self.other})

    def test_other_modes_never_put_the_server_first(self):
        self.answer(self.direct, *[200] * 10)
        self.direct.last_acquired_at = self.clock.now  # no probe of the server is due
        with patch.object(config, "NET_MODE", "both"):
            self.assertFalse(self.pool.smart_status()["server_first"])
            self.assertNotEqual(self.pick(0.0), self.direct)


class TestBatchedRadarEligibility(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "hot.db")
        self.db_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def test_matches_per_radar_checks(self):
        allowed, blocked, admin = 501, 502, config.ADMIN_ID
        for uid in (allowed, blocked, admin):
            await database.get_or_create_user(uid, f"u{uid}", "U")
        await database.set_user_allowed(allowed, 1)
        await database.set_user_allowed(blocked, 1)
        ids = {
            "allowed": await database.add_search(allowed, "a", "https://www.avito.ru/moskva?q=a"),
            "blocked": await database.add_search(blocked, "b", "https://www.avito.ru/moskva?q=b"),
            "admin": await database.add_search(admin, "c", "https://www.avito.ru/moskva?q=c"),
        }
        paused = await database.add_search(allowed, "p", "https://www.avito.ru/moskva?q=p")
        await database.toggle_search_active(paused, allowed)
        await database.set_user_allowed(blocked, 0)

        batched = await database.get_deliverable_search_ids([*ids.values(), paused, 999999])
        expected = set()
        for sid, uid in ((ids["allowed"], allowed), (ids["blocked"], blocked), (ids["admin"], admin), (paused, allowed)):
            if await database.is_search_active(sid) and await database.is_user_allowed(uid):
                expected.add(sid)
        self.assertEqual(batched, expected)
        self.assertEqual(batched, {ids["allowed"], ids["admin"]})
        self.assertEqual(await database.get_deliverable_search_ids([]), set())


if __name__ == "__main__":
    unittest.main()


class TestScanAttribution(unittest.TestCase):
    def test_scan_is_counted_on_the_channel_that_ran_it(self):
        from runtime_metrics import RuntimeMetricsRegistry

        metrics = RuntimeMetricsRegistry()
        metrics.record_scan_attempt(success=True, elapsed_ms=500, proxy_id="p-abc", masked_url="http://***@10.0.0.1:3000")
        metrics.record_scan_attempt(success=False, elapsed_ms=300)
        proxy = metrics._proxy_slice("p-abc", "http://***@10.0.0.1:3000")
        direct = metrics._proxy_slice("direct", "direct")
        self.assertEqual((proxy.collection_total, proxy.collection_success), (1, 1))
        self.assertEqual((direct.collection_total, direct.collection_success), (1, 0))
