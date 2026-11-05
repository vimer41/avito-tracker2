"""Admin tools: why a lot did not come, payment notices, broadcasts, money, user activity and notes, stall reports."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiogram.types import PreCheckoutQuery, SuccessfulPayment, Update, User

from tests import _environment
from tests._dispatcher import DispatcherFixture
import billing
import config
import database
import log_report
import scan_journal
import scheduler
from collector import SourceCollection
from handlers import admin
from parser import AvitoItem

USER_ID = 77001
NEVER_ID = 77002


def fresh_lot(item_id, title="PSP Vita"):
    now = int(time.time())
    return AvitoItem(item_id=item_id, title=title, price="1 000 ₽", url=f"https://www.avito.ru/x_{item_id}",
                     date="только что", timestamp=now, t_pub_reliable=True, raw_price=1000)


class AdminToolsBase(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "tools.db")
        self.db_patch.start()
        self.enabled = patch.object(config, "STARS_PAYMENTS_ENABLED", True)
        self.enabled.start()
        await database.init_db()
        await database.get_or_create_user(config.ADMIN_ID, "admin", "Admin")
        await database.get_or_create_user(USER_ID, "buyer", "Buyer")
        await database.activate_trial(USER_ID)
        await database.get_or_create_user(NEVER_ID, "never", "Never")
        self.setup_dispatcher()

    async def asyncTearDown(self):
        await self.close_dispatcher()
        self.enabled.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def texts(self, chat_id=None):
        return [m.text for m in self.session.calls if type(m).__name__ in ("SendMessage", "EditMessageText")
                and (chat_id is None or int(m.chat_id) == chat_id)]


class TestWhyLotDidNotCome(AdminToolsBase):
    async def fanout(self, radar_id, items):
        now = time.time()
        collection = SourceCollection(
            title="Fixture", items=tuple(items), pages=1, coverage_gap=False, elapsed_ms=10, captured_at=now,
            discovery_times={item.item_id: now for item in items}, top_ids=tuple(i.item_id for i in items[:3]),
            fast_skipped=False,
        )
        radar = await database.get_search(radar_id)
        self.assertTrue(await scheduler.process_collected_group_fanout(radar["url"], [radar], collection))

    async def test_each_radar_decision_is_explained(self):
        radar_id = await database.add_search(USER_ID, "PSP", "https://www.avito.ru/moskva?q=psp", initial_items=[])
        await database.add_stop_word_to_search(radar_id, "коробка", USER_ID)
        await self.fanout(radar_id, [fresh_lot("9100000001", "PSP коробка"), fresh_lot("9100000002", "PSP Street")])

        stopped = await admin.explain_lot("9100000001")
        self.assertIn("стоп-слово «коробка»", stopped)
        self.assertIn(f"#{radar_id}", stopped)
        queued = await admin.explain_lot("9100000002")
        self.assertIn("отправлен в очередь", queued)
        self.assertIn("ждёт отправки с", queued)
        self.assertIn(f"#{radar_id}: место 2 из 2", queued)

        await self.dispatch_message(user_id=config.ADMIN_ID, text="/lot https://www.avito.ru/moskva/igry/psp_9100000001?slocation=1")
        self.assertIn("стоп-слово", self.texts(config.ADMIN_ID)[-1])

    async def test_unknown_lot_says_the_bot_never_saw_it(self):
        text = await admin.explain_lot("9199999999")
        self.assertIn("Бот этот лот не видел", text)

    async def test_button_asks_for_a_link_and_a_link_does_not_make_a_radar(self):
        await self.dispatch_callback("admin_lot_prompt", config.ADMIN_ID)
        before = len(await database.get_user_searches(config.ADMIN_ID))
        await self.dispatch_message(user_id=config.ADMIN_ID, text="https://www.avito.ru/moskva/igry/psp_9199999998")
        self.assertIn("Лот 9199999998", self.texts(config.ADMIN_ID)[-1])
        self.assertEqual(len(await database.get_user_searches(config.ADMIN_ID)), before)

    def test_item_id_comes_from_any_link_form(self):
        self.assertEqual(admin.parse_item_id("https://www.avito.ru/spb/igry/sony_psp_4123456789?context=x"), "4123456789")
        self.assertEqual(admin.parse_item_id("avito.ru/moskva/telefony/iphone_13_mini_3000000001"), "3000000001")
        self.assertEqual(admin.parse_item_id("4123456789"), "4123456789")
        self.assertIsNone(admin.parse_item_id("привет"))


class TestPaymentNoticeAndMoney(AdminToolsBase):
    async def pay(self, plan="standart", charge="charge-1"):
        order = await billing.create_order(USER_ID, plan)
        self.update_id += 1
        query = PreCheckoutQuery(id=f"q-{order['id']}", from_user=User(id=USER_ID, is_bot=False, first_name="Buyer"),
                                 currency="XTR", total_amount=order["total_amount"], invoice_payload=order["payload"])
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, pre_checkout_query=query))
        await self.dispatch_message(user_id=USER_ID, successful_payment=SuccessfulPayment(
            currency="XTR", total_amount=order["total_amount"], invoice_payload=order["payload"],
            telegram_payment_charge_id=charge, provider_payment_charge_id=""))
        return order

    async def test_admins_hear_about_every_payment(self):
        order = await self.pay()
        notice = [t for t in self.texts(config.ADMIN_ID) if "Оплата" in t]
        self.assertTrue(notice, "admin got no payment notice")
        self.assertIn(f"{order['total_amount']} ⭐", notice[-1])
        self.assertIn("Первая оплата", notice[-1])
        self.assertIn(str(USER_ID), notice[-1])

        await self.pay(charge="charge-2")
        self.assertIn("Продление, 2-я оплата", [t for t in self.texts(config.ADMIN_ID) if "Оплата" in t][-1])

    async def test_money_screen_counts_paid_stars(self):
        order = await self.pay()
        await self.dispatch_callback("admin_money", config.ADMIN_ID)
        text = self.texts(config.ADMIN_ID)[-1]
        self.assertIn(f"Сутки: <b>0 ₽</b> (0 опл.) · <b>{order['total_amount']} ⭐</b> (1 опл.)", text)
        self.assertIn("Платили хоть раз: <b>1</b>", text)

        await self.dispatch_callback("admin_money", USER_ID)
        self.assertFalse([t for t in self.texts(USER_ID) if "Деньги" in (t or "")])


class TestBroadcast(AdminToolsBase):
    async def test_broadcast_reaches_only_the_filter_and_reports(self):
        await self.dispatch_callback("admin_bc:ever", config.ADMIN_ID)
        self.assertIn("Рассылка", self.texts(config.ADMIN_ID)[-1])
        await self.dispatch_message(user_id=config.ADMIN_ID, text="Скидка 20% до пятницы")
        self.assertIn("Отправить?", self.texts(config.ADMIN_ID)[-1])
        self.assertFalse(self.texts(USER_ID), "nothing goes before the confirmation")

        await self.dispatch_callback("admin_bc_go", config.ADMIN_ID)
        for _ in range(100):
            if any("Рассылка закончена" in (t or "") for t in self.texts(config.ADMIN_ID)):
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.texts(USER_ID), ["Скидка 20% до пятницы"])
        self.assertFalse(self.texts(NEVER_ID))
        self.assertIn("Доставлено: <b>1</b>", self.texts(config.ADMIN_ID)[-1])
        actions = await database.get_admin_actions(None)
        self.assertEqual(actions[0]["action"], "broadcast")

    async def test_non_admin_cannot_broadcast(self):
        await self.dispatch_callback("admin_bc:all", USER_ID)
        await self.dispatch_callback("admin_bc_go", USER_ID)
        self.assertFalse([t for t in self.texts(NEVER_ID)])


class TestUserCardAndNotes(AdminToolsBase):
    async def test_card_shows_activity_and_a_note_with_its_journal(self):
        await self.dispatch_callback(f"admin_user:{USER_ID}:0", config.ADMIN_ID)
        card = self.texts(config.ADMIN_ID)[-1]
        self.assertIn("Заходил:", card)
        self.assertIn("Лотов получил: <b>0</b>", card)
        self.assertIn("Оплат: <b>0</b>", card)

        await self.dispatch_callback(f"admin_note:{USER_ID}:0", config.ADMIN_ID)
        await self.dispatch_message(user_id=config.ADMIN_ID, text="просил скидку")
        self.assertIn("Заметка: <i>просил скидку</i>", self.texts(config.ADMIN_ID)[-1])

        await self.dispatch_callback(f"admin_ulog:{USER_ID}:0", config.ADMIN_ID)
        self.assertIn("заметка: просил скидку", self.texts(config.ADMIN_ID)[-1])
        await self.dispatch_callback("admin_alog", config.ADMIN_ID)
        self.assertIn(f"→ <code>{USER_ID}</code>", self.texts(config.ADMIN_ID)[-1])

    async def test_admin_actions_are_journaled(self):
        await self.dispatch_callback(f"admin_radar_limit:{USER_ID}:5:0", config.ADMIN_ID)
        actions = await database.get_admin_actions(USER_ID)
        self.assertEqual((actions[0]["action"], actions[0]["admin_id"]), ("limit", config.ADMIN_ID))

    async def test_radar_card_says_when_it_was_checked(self):
        radar_id = await database.add_search(USER_ID, "PSP", "https://www.avito.ru/moskva?q=psp")
        scan_journal.record_scan([radar_id], ok=True, at=time.time() - 7, ms=300)
        await self.dispatch_callback(f"open_search:{radar_id}", USER_ID)
        card = self.texts(USER_ID)[-1]
        self.assertIn("Проверен:</b> 7 с назад", card)
        self.assertIn("Прислано за сутки:</b> 0", card)
        scan_journal.forget(radar_id)


class TestStallReport(unittest.TestCase):
    def row(self, **fields):
        return {"ts": "11-05 09:59", "loop_max_ms": 8000, "cpu_pct": 5, "sys_cpu": 30, "iowait": 1, "steal": 0,
                "swap_in": 0, "mem_avail_mb": 120, **fields}

    def test_cause_is_named_from_server_numbers(self):
        self.assertIn("хостинг забирает процессор", log_report.stall_cause([self.row(steal=45)]))
        self.assertIn("не хватает памяти", log_report.stall_cause([self.row(iowait=60)]))
        self.assertIn("занят самим ботом", log_report.stall_cause([self.row(sys_cpu=98, cpu_pct=95)]))
        self.assertIn("код бота держит поток", log_report.stall_cause([self.row()]))
        self.assertIn("системных цифр нет", log_report.stall_cause([{"loop_max_ms": 8000}]))

    def test_crash_log_shows_only_dated_dumps_of_the_period(self):
        with tempfile.TemporaryDirectory() as folder:
            crash = Path(folder) / "crash.log"
            old_stack = "Timeout (0:00:04)!\nThread 0x1 (most recent call first):\n  File \"old.py\", line 1 in x\n"
            new_stack = ("Timeout (0:00:04)!\nThread 0x2 (most recent call first):\n  File \"pool.py\", line 9 in w\n\n"
                         "Thread 0x3 (most recent call first):\n  File \"/opt/bot/parser.py\", line 1194 in parse\n"
                         "  File \"/usr/lib/python3.12/asyncio/runners.py\", line 118 in run\n")
            now = int(time.time())
            crash.write_text(
                old_stack + f"=== зависание 11-04 10:00:00 МСК · 4.5 с · epoch {now - 90000} ===\n"
                + new_stack + f"=== зависание 11-05 09:59:12 МСК · 6.1 с · epoch {now - 60} ===\n"
                + old_stack,  # an undated dump after the last mark is skipped
                encoding="utf-8",
            )
            with patch.object(log_report, "CRASH_LOG", crash):
                lines = log_report._crash_tail(now - 3600)
        text = "\n".join(lines)
        self.assertIn("11-05 09:59:12 МСК · пауза 6.1 с", text)
        self.assertIn("parser.py\", line 1194", text)
        self.assertNotIn("old.py", text)
        self.assertNotIn("pool.py", text, "only the event loop thread is shown")


class TestQuietRoutineErrors(unittest.TestCase):
    def test_telegram_polling_timeout_is_not_an_error(self):
        import logging
        import logger_setup

        record = logging.LogRecord("aiogram.dispatcher", logging.ERROR, __file__, 1,
                                   "Failed to fetch updates - TelegramNetworkError: timeout", None, None)
        logger_setup._PollingNoise().filter(record)
        self.assertEqual(record.levelno, logging.INFO)
        other = logging.LogRecord("aiogram.dispatcher", logging.ERROR, __file__, 1, "Cause exception", None, None)
        logger_setup._PollingNoise().filter(other)
        self.assertEqual(other.levelno, logging.ERROR)


class TestServerBlockAlert(unittest.TestCase):
    def test_blocked_server_alerts_admins_once_an_hour(self):
        from proxy_pool import ProxyPool

        clock = [1000.0]
        with tempfile.TemporaryDirectory() as folder:
            pfile = Path(folder) / "proxies.txt"
            pfile.write_text("http://u:p@192.0.2.1:8001\n", encoding="utf-8")
            with patch.object(config, "NET_MODE", "smart"):
                pool = ProxyPool(proxy_pool_file=pfile, fallback_proxy_url="", allow_direct=True, monotonic_fn=lambda: clock[0])
                direct = next(c for c in pool.clients if c.is_direct)
                for status in [403, 200] * 10:
                    direct.record_raw_http_response(status, 0.1)
                pool.smart_status()
                alerts = pool.pop_alerts()
                self.assertEqual(len(alerts), 1)
                self.assertIn("Авито блокирует сервер", alerts[0])
                self.assertEqual(pool.pop_alerts(), [])


if __name__ == "__main__":
    unittest.main()
