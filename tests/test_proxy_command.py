"""Proxy management from the bot: add any number of proxies, drop same-IP duplicates, probe, persist, hot-swap."""

import asyncio
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests import _environment
from tests._dispatcher import DispatcherFixture
import config
import database
import proxy_check
import proxy_pool
from collector import SourceCollection
from handlers import admin
from parser import AvitoItem, FetchFailure
from proxy_check import ProbeAttempt, ProbeResult

PROXY_RAW = "10.1.2.3:8080:user:secret"
PROXY_URL = "http://user:secret@10.1.2.3:8080"
PROXY_B = "http://user:secret@10.9.9.9:3000"
PROXY_B_OTHER_PORT = "socks5://user:secret@10.9.9.9:3001"


def make_message(text: str):
    status = Mock(edit_text=AsyncMock())
    message = Mock(text=text, from_user=Mock(id=config.ADMIN_ID))
    message.answer = AsyncMock(return_value=status)
    message.delete = AsyncMock()
    return message, status


def probe(*oks):
    attempts = [ProbeAttempt(True, 400, items=50) if ok else ProbeAttempt(False, 3000, failure="forbidden", status=403) for ok in oks]
    return ProbeResult(proxy_id="p", masked_url="http://***@x:1", target_url="https://www.avito.ru/x", attempts=attempts)


def exit_ips(mapping):
    """Fake exit-IP lookup: proxy URL -> public IP; the server (None) exits from 203.0.113.1."""
    async def lookup(urls, concurrency=5):
        return {url: ("203.0.113.1" if url is None else mapping.get(url)) for url in urls}
    return lookup


class ProxyEnvMixin:
    async def setup_env(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "proxy.db"),
            patch.object(config, "PROXY_URL", None),
            patch.object(config, "PROXY_URLS", []),
            patch.object(config, "PROXY_POOL_FILE", ""),
            patch.object(config, "ALLOW_DIRECT_AVITO", True),
            patch.object(config, "NET_MODE", "auto"),
        ]
        for p in self.patches:
            p.start()
        await database.init_db()
        self.pool = proxy_pool.ProxyPool(proxy_pool_file="", fallback_proxy_url="", allow_direct=True)
        self.pool_patch = patch.object(proxy_pool, "get_proxy_pool", return_value=self.pool)
        self.pool_patch.start()

    async def teardown_env(self):
        self.pool_patch.stop()
        for client in self.pool.clients:
            await client.close()
        for p in reversed(self.patches):
            p.stop()
        self.temp_dir.cleanup()

    def channels(self):
        return sorted((c.proxy_url or "direct") for c in self.pool.clients)


class TestProxyCommand(ProxyEnvMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_env()

    async def asyncTearDown(self):
        await self.teardown_env()

    async def test_successful_probe_adds_persists_keeps_server_and_hides_password(self):
        message, status = make_message(f"/proxy {PROXY_RAW}")
        with patch.object(proxy_check, "exit_ips_for", new=exit_ips({PROXY_URL: "10.1.2.3"})), \
                patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe(True, True, False))) as probe_mock:
            await admin.cmd_admin_proxy(message)
        self.assertEqual(probe_mock.await_args.args[0], PROXY_URL)
        message.delete.assert_awaited_once()
        self.assertEqual(await database.get_proxy_urls(), [PROXY_URL])
        # Adding a proxy must add capacity, not switch the server IP off: smart keeps it scanning first.
        self.assertEqual(config.NET_MODE, "smart")
        self.assertEqual(self.channels(), ["direct", PROXY_URL])
        report = status.edit_text.await_args.args[0]
        self.assertIn("Добавлено: 1", report)
        self.assertIn("2/3", report)
        self.assertNotIn("secret", report)

    async def test_failed_probe_is_not_added(self):
        message, status = make_message(f"/proxy {PROXY_RAW}")
        with patch.object(proxy_check, "exit_ips_for", new=exit_ips({PROXY_URL: "10.1.2.3"})), \
                patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe(False, False, False))):
            await admin.cmd_admin_proxy(message)
        self.assertIn("не добавлен", status.edit_text.await_args.args[0])
        self.assertEqual(await database.get_proxy_urls(), [])
        self.assertEqual(self.channels(), ["direct"])

    async def test_bulk_add_rejects_same_exit_ip_and_already_listed(self):
        message, status = make_message(f"/proxy {PROXY_RAW}\n{PROXY_B}\n{PROXY_B_OTHER_PORT}\n\n{PROXY_RAW}")
        ips = {PROXY_URL: "10.1.2.3", PROXY_B: "10.9.9.9", PROXY_B_OTHER_PORT: "10.9.9.9"}
        with patch.object(proxy_check, "exit_ips_for", new=exit_ips(ips)), \
                patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe(True, True))) as probe_mock:
            await admin.cmd_admin_proxy(message)
        self.assertEqual(probe_mock.await_count, 2, "the same-IP duplicate must not even be probed")
        self.assertEqual(await database.get_proxy_urls(), [PROXY_URL, PROXY_B])
        report = status.edit_text.await_args.args[0]
        self.assertIn("тот же IP 10.9.9.9", report)
        self.assertIn("уже в списке", report)

    async def test_same_subnet_adds_one_and_asks_about_the_rest(self):
        neighbour = "http://user:secret@10.1.2.77:8080"
        third = "http://user:secret@10.1.2.99:8080"
        message, status = make_message(f"/proxy {PROXY_RAW}\n{neighbour}\n{third}\n{PROXY_B}")
        ips = {PROXY_URL: "10.1.2.3", neighbour: "10.1.2.77", third: "10.1.2.99", PROXY_B: "10.9.9.9"}
        with patch.object(proxy_check, "exit_ips_for", new=exit_ips(ips)), \
                patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe(True, True))):
            await admin.cmd_admin_proxy(message)
        self.assertEqual(await database.get_proxy_urls(), [PROXY_URL, PROXY_B])
        report = status.edit_text.await_args.args[0]
        self.assertIn("10.1.2.0/24", report)
        self.assertIn("Вы уверены", report)
        kb = status.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "admin_net_samenet_yes")

        call = Mock(data="admin_net_samenet_yes", from_user=Mock(id=config.ADMIN_ID), answer=AsyncMock())
        call.message = Mock(answer=AsyncMock())
        await admin.cb_admin_net_samenet(call)
        self.assertEqual(await database.get_proxy_urls(), [PROXY_URL, PROXY_B, neighbour, third])
        await admin.cb_admin_net_samenet(call)  # a second press must not add them twice
        self.assertEqual(len(await database.get_proxy_urls()), 4)

    async def test_same_subnet_refused_adds_nothing_more(self):
        neighbour = "http://user:secret@10.1.2.77:8080"
        message, _ = make_message(f"/proxy {PROXY_RAW}\n{neighbour}")
        with patch.object(proxy_check, "exit_ips_for", new=exit_ips({PROXY_URL: "10.1.2.3", neighbour: "10.1.2.77"})), \
                patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe(True, True))):
            await admin.cmd_admin_proxy(message)
        call = Mock(data="admin_net_samenet_no", from_user=Mock(id=config.ADMIN_ID), answer=AsyncMock())
        call.message = Mock(answer=AsyncMock())
        await admin.cb_admin_net_samenet(call)
        self.assertEqual(await database.get_proxy_urls(), [PROXY_URL])

    async def test_race_runs_through_a_pool_proxy_without_polling_telegram(self):
        await admin._save_proxy_list([PROXY_URL])
        message, _ = make_message("/race 15")
        message.chat = Mock(id=config.ADMIN_ID)
        proc = Mock(returncode=None, wait=AsyncMock(return_value=0))
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
            await admin.cmd_admin_race(message)
            await asyncio.sleep(0)
        env = spawn.await_args.kwargs["env"]
        self.assertEqual(env["RACE_PROXY"], PROXY_URL)
        self.assertEqual(env["RACE_MINUTES"], "15")
        self.assertEqual(env["RACE_NO_UPDATES"], "1")
        self.assertTrue(spawn.await_args.args[2].endswith("api_race.py"))
        admin._RACE.clear()

    async def test_proxy_exiting_from_the_server_ip_is_rejected(self):
        message, status = make_message(f"/proxy {PROXY_RAW}")
        with patch.object(proxy_check, "exit_ips_for", new=exit_ips({PROXY_URL: "203.0.113.1"})), \
                patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe(True))) as probe_mock:
            await admin.cmd_admin_proxy(message)
        probe_mock.assert_not_awaited()
        self.assertIn("IP сервера", status.edit_text.await_args.args[0])
        self.assertEqual(await database.get_proxy_urls(), [])

    async def test_invalid_proxy_is_rejected_without_probe(self):
        message, _ = make_message("/proxy ftp://10.1.2.3:21")
        with patch.object(proxy_check, "probe_channel", new=AsyncMock()) as probe_mock:
            await admin.cmd_admin_proxy(message)
        probe_mock.assert_not_awaited()
        self.assertIn("неподдерживаемая схема", message.answer.await_args.args[0])

    async def test_off_removes_all_and_legacy_single_proxy_migrates_into_list(self):
        await database.set_bot_setting("proxy_url", PROXY_URL)
        await database.init_db()
        self.assertEqual(config.PROXY_URLS, [PROXY_URL])
        self.assertEqual(await database.get_proxy_urls(), [PROXY_URL])
        self.assertEqual(await database.get_bot_setting("proxy_url"), "")
        await self.pool.reload_async()
        self.assertEqual(self.channels(), [PROXY_URL])

        message, _ = make_message("/proxy off")
        await admin.cmd_admin_proxy(message)
        self.assertEqual(await database.get_proxy_urls(), [])
        self.assertEqual(self.channels(), ["direct"])

    async def test_non_admin_is_refused(self):
        message, _ = make_message(f"/proxy {PROXY_RAW}")
        message.from_user.id = 1
        with patch.object(proxy_check, "probe_channel", new=AsyncMock()) as probe_mock:
            await admin.cmd_admin_proxy(message)
        probe_mock.assert_not_awaited()


class TestNetworkScreen(ProxyEnvMixin, DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_env()
        self.setup_dispatcher(names=("admin",))

    async def asyncTearDown(self):
        await self.close_dispatcher()
        await self.teardown_env()

    async def test_screen_lists_modes_and_refuses_proxy_mode_without_proxy(self):
        await self.dispatch_callback("admin_net", config.ADMIN_ID)
        self.assertTrue({"admin_net_mode:direct", "admin_net_mode:proxy", "admin_net_mode:both",
                         "admin_net_probe", "admin_net_add"} <= set(self.callback_values()))
        await self.dispatch_callback("admin_net_mode:proxy", config.ADMIN_ID)
        self.assertTrue(self.methods("AnswerCallbackQuery")[-1].show_alert)
        self.assertEqual(config.NET_MODE, "auto")
        self.assertEqual(self.channels(), ["direct"])

    async def test_button_flow_adds_many_proxies_then_lists_and_deletes_them(self):
        await self.dispatch_callback("admin_net_add", config.ADMIN_ID)
        ips = {PROXY_URL: "10.1.2.3", PROXY_B: "10.9.9.9"}
        with patch.object(proxy_check, "exit_ips_for", new=exit_ips(ips)), \
                patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe(True, True))):
            await self.dispatch_message(text=f"{PROXY_RAW}\n{PROXY_B}", user_id=config.ADMIN_ID)
        self.assertTrue(self.methods("DeleteMessage"), "the message with the passwords must be deleted")
        self.assertEqual(self.channels(), ["direct", PROXY_URL, PROXY_B])
        self.assertIn("admin_net_list:0", self.callback_values())

        await self.dispatch_callback("admin_net_mode:proxy", config.ADMIN_ID)
        self.assertIn("Точно переключить режим сети?", self.methods("EditMessageText")[-1].text)
        self.assertEqual(self.channels(), ["direct", PROXY_URL, PROXY_B], "the first tap only asks")
        self.assertIn("admin_net_mode_yes:proxy", self.callback_values())
        self.assertIn("admin_net", self.callback_values())
        await self.dispatch_callback("admin_net_mode_yes:proxy", config.ADMIN_ID)
        self.assertEqual(self.channels(), [PROXY_URL, PROXY_B])

        await self.dispatch_callback("admin_net_list:0", config.ADMIN_ID)
        delete_buttons = [v for v in self.callback_values() if v.startswith("admin_net_del:")]
        self.assertEqual(len(delete_buttons), 2)
        await self.dispatch_callback(delete_buttons[0], config.ADMIN_ID)
        self.assertEqual(len(await database.get_proxy_urls()), 1)

        # Removing the last proxy in proxy-only mode must not silently stop scanning.
        await self.dispatch_callback("admin_net_delall", config.ADMIN_ID)
        await self.dispatch_callback("admin_net_delall_yes", config.ADMIN_ID)
        self.assertEqual(config.NET_MODE, "direct")
        self.assertEqual(self.channels(), ["direct"])

    async def test_a_resting_proxy_that_passes_the_check_turns_green(self):
        await self.dispatch_callback("admin_net_add", config.ADMIN_ID)
        with patch.object(proxy_check, "exit_ips_for", new=exit_ips({PROXY_URL: "10.1.2.3"})), \
                patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe(True, True))):
            await self.dispatch_message(text=PROXY_RAW, user_id=config.ADMIN_ID)
        client = next(c for c in proxy_pool.get_proxy_pool().clients if c.proxy_url == PROXY_URL)
        client._state = "half_open"  # rested after a 403 and no scan has gone through it since
        await self.dispatch_callback("admin_net", config.ADMIN_ID)
        self.assertIn("позеленеет после первого удачного запроса", self.methods("EditMessageText")[-1].text)
        with patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe(True, True))):
            await self.dispatch_callback("admin_net_probe", config.ADMIN_ID)
        self.assertEqual(client.state, "healthy")
        self.assertIn("Снова зелёные после проверки: 1", self.methods("EditMessageText")[-1].text)

    async def test_each_proxy_opens_its_own_statistics_card(self):
        await self.dispatch_callback("admin_net_add", config.ADMIN_ID)
        with patch.object(proxy_check, "exit_ips_for", new=exit_ips({PROXY_URL: "10.1.2.3"})), \
                patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe(True, True))):
            await self.dispatch_message(text=PROXY_RAW, user_id=config.ADMIN_ID)
        proxy_id = proxy_pool.compute_proxy_id(PROXY_URL)
        client = next(c for c in proxy_pool.get_proxy_pool().clients if c.proxy_id == proxy_id)
        for status in (200, 200, 200, 403):
            client.record_raw_http_response(status, 0.1)

        await self.dispatch_callback("admin_net_list:0", config.ADMIN_ID)
        values = self.callback_values()
        self.assertIn(f"admin_net_stat:{proxy_id}:0", values)
        self.assertIn("admin_net_stat:direct:0", values)

        day = {"http": 100, "ok": 84, "403": 10, "429": 0, "terr": 6, "coll": 80, "coll_ok": 78,
               "fail": Counter({"timeout": 6}), "minutes": 60, "paused": 5}
        with patch("log_report.channel_history", return_value=day) as history:
            await self.dispatch_callback(f"admin_net_stat:{proxy_id}:0", config.ADMIN_ID)
        history.assert_called_once_with("10.1.2.3:8080", 24, 0.0)  # 24 h, no «start over» mark
        text = self.methods("EditMessageText")[-1].text
        self.assertIn("10.1.2.3:8080", text)
        self.assertIn("Роль: в запасе", text, "smart mode keeps a proxy in reserve while the server copes")
        self.assertIn("75% успешных", text)
        self.assertIn("За 60 минут", text)
        self.assertIn("Запросов 100, успешных 84%", text)
        self.assertIn("На паузе 5 мин из 60", text)
        self.assertNotIn("secret", text)
        self.assertIn(f"admin_net_del:{proxy_id}:0", self.callback_values())

        with patch("log_report.channel_history", return_value=None):
            await self.dispatch_callback("admin_net_stat:direct:0", config.ADMIN_ID)
            self.assertIn("Сервер (без прокси)", self.methods("EditMessageText")[-1].text)
            await self.dispatch_callback("admin_net_stat:p-missing:0", config.ADMIN_ID)
            self.assertIn("уже нет в списке", self.methods("EditMessageText")[-1].text)

    async def test_non_admin_cannot_open_a_proxy_card(self):
        await self.dispatch_callback("admin_net_stat:direct:0", 123456)
        self.assertFalse(self.methods("EditMessageText"))
        self.assertTrue(self.methods("AnswerCallbackQuery")[-1].show_alert)

    async def test_saved_mode_is_restored_on_startup(self):
        await database.set_bot_setting("net_mode", "both")
        await database.init_db()
        self.assertEqual(config.NET_MODE, "both", "a choice made after the one-time switch stays")

    async def test_old_both_mode_becomes_smart_once(self):
        await database.set_bot_setting("net_mode", "both")
        async with database.writer_connection() as db:
            await db.execute("DELETE FROM bot_settings WHERE key = 'net_mode_smart_offered'")
            await db.commit()
        await database.init_db()
        self.assertEqual(config.NET_MODE, "smart")
        self.assertEqual(await database.get_bot_setting("net_mode"), "smart")

    async def test_smart_screen_says_who_scans_now(self):
        await self.dispatch_callback("admin_net_add", config.ADMIN_ID)
        with patch.object(proxy_check, "exit_ips_for", new=exit_ips({PROXY_URL: "10.1.2.3"})), \
                patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe(True, True))):
            await self.dispatch_message(text=PROXY_RAW, user_id=config.ADMIN_ID)
        self.assertEqual(config.NET_MODE, "smart")
        await self.dispatch_callback("admin_net", config.ADMIN_ID)
        text = self.methods("EditMessageText")[-1].text
        self.assertIn("Режим: <b>🧠 Умный</b>", text)
        self.assertIn("Сейчас: <b>сканирует сервер</b>, прокси в запасе", text)

        proxy_pool.get_proxy_pool().note_demand(config.SMART_DIRECT_MAX_RPS * 2)
        await self.dispatch_callback("admin_net", config.ADMIN_ID)
        text = self.methods("EditMessageText")[-1].text
        self.assertIn("Сейчас: <b>прокси в работе</b> — нагрузка", text)
        proxy_pool.get_proxy_pool().note_demand(0.0)

    async def test_non_admin_cannot_switch_modes(self):
        await self.dispatch_callback("admin_net_mode:direct", 123456)
        await self.dispatch_callback("admin_net_mode_yes:direct", 123456)
        self.assertEqual(config.NET_MODE, "auto")


class TestChannelSelectionAndLimiter(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(config, "PROXY_URLS", []), patch.object(config, "NET_MODE", "both"),
                        patch.object(config, "PROXY_URL", None), patch.object(config, "PROXY_POOL_FILE", "")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_background_403s_do_not_throttle_a_proxy(self):
        client = proxy_pool.ProxyClient(PROXY_URL)
        for index in range(40):  # ~12% blocks, like Avito's background rate for any IP
            client.record_raw_http_response(403 if index % 8 == 0 else 200, 0.5)
            if index % 8 == 0:
                client.record_final_block("forbidden")
        self.assertEqual(client.effective_rate, client.configured_rate)

    def test_heavy_blocking_cuts_rate_but_not_below_floor_and_recovers(self):
        client = proxy_pool.ProxyClient(PROXY_URL)
        for _ in range(60):  # each cut needs a fresh half window of blocks: 1.5 → 1.05 → 0.74 → 0.51 → 0.5
            client.record_raw_http_response(403, 0.5)
            client.record_final_block("forbidden")
        self.assertEqual(client.effective_rate, config.PROXY_MIN_RATE)
        for _ in range(40):
            client.record_raw_http_response(200, 0.5)
        self.assertGreater(client.effective_rate, config.PROXY_MIN_RATE)

    def test_busy_fast_channel_beats_idle_throttled_one(self):
        with patch.object(config, "PROXY_URLS", [PROXY_URL]):
            pool = proxy_pool.ProxyPool(proxy_pool_file="", fallback_proxy_url="", allow_direct=True)
        server = next(c for c in pool.clients if c.is_direct)
        slow = next(c for c in pool.clients if not c.is_direct)
        server.in_flight = 1  # the server is mid-request
        slow._next_request_at = time.monotonic() + 10  # throttled proxy is free but 10 s from its next slot
        lease = pool.acquire_client()
        self.assertIs(lease.client, server)
        lease.release()


class TestProbeChannel(unittest.IsolatedAsyncioTestCase):
    async def test_probe_reports_successes_failures_and_median(self):
        now = time.time()
        ok = SourceCollection(title="t", items=(AvitoItem(item_id="1", title="t", price="1", url="u"),),
                              captured_at=now, pages=1, coverage_gap=False, elapsed_ms=1)
        outcomes = iter([ok, None, ok])

        async def fake_collect(parser, url, *args, **kwargs):
            result = next(outcomes)
            if result is None:
                parser.last_failure = FetchFailure("forbidden", status=403)
            return result

        with patch.object(proxy_check, "collect_source", new=fake_collect):
            result = await proxy_check.probe_channel(PROXY_URL, "https://www.avito.ru/moskva?q=phone")
        self.assertEqual(result.successes, 2)
        self.assertEqual([a.failure for a in result.attempts], ["", "forbidden", ""])
        self.assertEqual(result.attempts[1].status, 403)
        self.assertIsNotNone(result.median_ok_ms)
        self.assertNotIn("secret", result.masked_url)


if __name__ == "__main__":
    unittest.main()
