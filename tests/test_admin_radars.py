"""Admins add many radars from one message and raise radar limits with a button."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment
from tests._dispatcher import DispatcherFixture
import config
import database
from handlers.searches import extract_avito_urls

USER_ID = 55501


def links(count, start=0):
    return "\n".join(f"https://www.avito.ru/moskva?q=lot{i}" for i in range(start, start + count))


class TestExtractUrls(unittest.TestCase):
    def test_finds_every_link_once_in_order(self):
        text = "avito.ru/spb/odezhda?q=a\nhttps://m.avito.ru/moskva?q=b  https://www.avito.ru/spb/odezhda?q=a\nмусор"
        self.assertEqual(
            extract_avito_urls(text),
            ["https://avito.ru/spb/odezhda?q=a", "https://m.avito.ru/moskva?q=b", "https://www.avito.ru/spb/odezhda?q=a"],
        )


class TestAdminRadars(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "admin_radars.db")
        self.db_patch.start()
        await database.init_db()
        await database.get_or_create_user(config.ADMIN_ID, "admin", "Admin")
        await database.get_or_create_user(USER_ID, "user", "User")
        await database.grant_subscription(USER_ID, 30, "pro")
        self.setup_dispatcher(names=("admin", "searches"))

    async def asyncTearDown(self):
        await self.close_dispatcher()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def send_links(self, user_id, text):
        await self.dispatch_callback("add_search_prompt", user_id)
        await self.dispatch_message(user_id=user_id, text=text)

    def last_text(self):
        return [m for m in self.session.calls if type(m).__name__ in ("SendMessage", "EditMessageText")][-1].text

    async def test_admin_adds_many_radars_in_one_message(self):
        await self.send_links(config.ADMIN_ID, links(30) + "\n" + links(2))
        searches = await database.get_user_searches(config.ADMIN_ID)
        self.assertEqual(len(searches), 30, "duplicates inside the message are skipped")
        self.assertIn("Добавлено: <b>30</b>", self.last_text())

        await self.send_links(config.ADMIN_ID, links(3, start=28))
        self.assertEqual(len(await database.get_user_searches(config.ADMIN_ID)), 31)
        self.assertIn("Уже были: <b>2</b>", self.last_text())

    async def test_bulk_add_stops_at_the_limit(self):
        with patch.object(database, "MAX_SEARCHES_PER_USER", 5):
            await self.send_links(config.ADMIN_ID, links(8))
            self.assertEqual(len(await database.get_user_searches(config.ADMIN_ID)), 5)
            self.assertIn("Не влезли в лимит (5): <b>3</b>", self.last_text())

            await self.dispatch_callback("admin_radar_limit:self:10:0", config.ADMIN_ID)
            self.assertEqual(await database.get_user_max_radars(config.ADMIN_ID), 15)

    async def test_regular_user_cannot_bulk_add(self):
        await self.send_links(USER_ID, links(3))
        self.assertLessEqual(len(await database.get_user_searches(USER_ID)), 1)

    async def test_admin_raises_and_lowers_user_limit(self):
        self.assertEqual(await database.get_user_max_radars(USER_ID), 4)
        await self.dispatch_callback(f"admin_radar_limit:{USER_ID}:20:0", config.ADMIN_ID)
        self.assertEqual(await database.get_user_max_radars(USER_ID), 24)
        self.assertIn("из них +20 от админа", self.last_text())
        for _ in range(6):
            await self.dispatch_callback(f"admin_radar_limit:{USER_ID}:-5:0", config.ADMIN_ID)
        self.assertEqual(await database.get_user_max_radars(USER_ID), 4, "bonus never goes below zero")

        # The limit applies when adding radars, too.
        await database.change_user_extra_radars(USER_ID, 1)
        for i in range(5):
            await database.add_search(USER_ID, f"R{i}", f"https://www.avito.ru/moskva?q=u{i}")
        with self.assertRaises(database.RadarLimitError):
            await database.add_search(USER_ID, "R5", "https://www.avito.ru/moskva?q=u5")

    async def test_admin_card_of_an_admin_opens_with_limit_buttons(self):
        other_admin = next(a for a in config.ADMIN_IDS if a != config.ADMIN_ID)
        await database.get_or_create_user(other_admin, "admin2", "Admin2")
        await self.dispatch_callback(f"admin_user:{other_admin}:0", config.ADMIN_ID)
        edit = [m for m in self.session.calls if type(m).__name__ == "EditMessageText"][-1]
        self.assertIn("Администратор", edit.text)
        buttons = [b.callback_data for row in edit.reply_markup.inline_keyboard for b in row]
        self.assertIn(f"admin_radar_limit:{other_admin}:20:0", buttons)
        self.assertFalse(any(b.startswith("admin_sub_menu:") for b in buttons))

        await self.dispatch_callback(f"admin_radar_limit:{other_admin}:20:0", config.ADMIN_ID)
        self.assertEqual(await database.get_user_max_radars(other_admin), config.MAX_SEARCHES_PER_USER + 20)

    async def test_non_admin_cannot_change_limits(self):
        await self.dispatch_callback(f"admin_radar_limit:{USER_ID}:20:0", USER_ID)
        self.assertEqual(await database.get_user_max_radars(USER_ID), 4)

    async def add_user_radars(self, count):
        return [await database.add_search(USER_ID, f"R{i}", f"https://www.avito.ru/moskva?q=u{i}") for i in range(count)]

    async def test_admin_deletes_selected_radars_of_a_user(self):
        radars = await self.add_user_radars(3)
        await self.dispatch_callback(f"admin_user:{USER_ID}:0", config.ADMIN_ID)
        self.assertIn(f"admin_rdel:new:{USER_ID}:0", self.callback_values())

        await self.dispatch_callback(f"admin_rdel:new:{USER_ID}:0", config.ADMIN_ID)
        await self.dispatch_callback(f"admin_rtog:{radars[0]}:0:{USER_ID}:0", config.ADMIN_ID)
        await self.dispatch_callback(f"admin_rtog:{radars[1]}:0:{USER_ID}:0", config.ADMIN_ID)
        await self.dispatch_callback(f"admin_rtog:{radars[1]}:0:{USER_ID}:0", config.ADMIN_ID)
        await self.dispatch_callback(f"admin_rtog:{radars[2]}:0:{USER_ID}:0", config.ADMIN_ID)
        self.assertIn("Выбрано: <b>2</b> из 3", self.last_text())
        await self.dispatch_callback(f"admin_rgo:{USER_ID}:0", config.ADMIN_ID)
        self.assertIn("Удалить 2 радаров", self.last_text())
        self.assertEqual(len(await database.get_user_searches(USER_ID)), 3, "nothing goes before the confirmation")

        await self.dispatch_callback(f"admin_ryes:{USER_ID}:0", config.ADMIN_ID)
        self.assertEqual([s["id"] for s in await database.get_user_searches(USER_ID)], [radars[1]])

    async def test_admin_delete_screen_pages_a_long_radar_list(self):
        await database.change_user_extra_radars(USER_ID, 40)
        await self.add_user_radars(25)
        await self.dispatch_callback(f"admin_rdel:new:{USER_ID}:0", config.ADMIN_ID)
        self.assertIn(f"admin_rdel:1:{USER_ID}:0", self.callback_values())
        await self.dispatch_callback(f"admin_rall:0:{USER_ID}:0", config.ADMIN_ID)
        self.assertIn("Выбрано: <b>25</b> из 25", self.last_text())
        await self.dispatch_callback(f"admin_ryes:{USER_ID}:0", config.ADMIN_ID)
        self.assertEqual(await database.get_user_searches(USER_ID), [])

    async def test_selection_never_reaches_another_users_radar(self):
        own = await database.add_search(config.ADMIN_ID, "Свой", "https://www.avito.ru/moskva?q=own")
        await self.add_user_radars(1)
        await self.dispatch_callback(f"admin_rdel:new:{USER_ID}:0", config.ADMIN_ID)
        await self.dispatch_callback(f"admin_rtog:{own}:0:{USER_ID}:0", config.ADMIN_ID)
        await self.dispatch_callback(f"admin_ryes:{USER_ID}:0", config.ADMIN_ID)
        self.assertEqual(len(await database.get_user_searches(config.ADMIN_ID)), 1)
        self.assertEqual(len(await database.get_user_searches(USER_ID)), 1)

    async def test_non_admin_cannot_delete_radars_of_others(self):
        radars = await self.add_user_radars(1)
        for data in (f"admin_rall:0:{USER_ID}:0", f"admin_rtog:{radars[0]}:0:{USER_ID}:0", f"admin_ryes:{USER_ID}:0"):
            await self.dispatch_callback(data, USER_ID)
        self.assertEqual(len(await database.get_user_searches(USER_ID)), 1)

    async def test_radar_history_keeps_deleted_radars(self):
        first, second = await self.add_user_radars(2)
        await database.rename_search(first, USER_ID, "PSP")
        await database.toggle_search_active(first, USER_ID)
        await database.delete_search(second, USER_ID)
        actions = [(row["search_id"], row["action"]) for row in await database.get_radar_history(USER_ID)]
        self.assertEqual(actions[0], (second, "delete"))
        self.assertIn((first, "rename"), actions)
        self.assertIn((first, "pause"), actions)
        self.assertIn((second, "create"), actions)

        await self.dispatch_callback(f"admin_rhist:{USER_ID}:0:0", config.ADMIN_ID)
        text = self.last_text()
        self.assertIn("История радаров", text)
        self.assertIn("удалено: <b>1</b>", text)
        self.assertIn(f"#{second} ✖", text)

        await self.dispatch_callback(f"admin_rhist:{USER_ID}:0:0", USER_ID)
        self.assertNotIn(str(second) + " ✖", "".join(
            m.text for m in self.session.calls if type(m).__name__ == "SendMessage" and m.chat_id == USER_ID))

    async def test_user_filter_ever_subscribed(self):
        never = 55502
        await database.get_or_create_user(never, "never", "Never")
        await self.dispatch_callback("admin_page:0:ever", config.ADMIN_ID)
        text = self.last_text()
        self.assertIn("Была подписка", text)
        self.assertIn("@user", text)
        self.assertNotIn("@never", text)
        await self.dispatch_callback("admin_page:0", config.ADMIN_ID)
        self.assertIn("@never", self.last_text())

    async def test_scan_journal_splits_lot_age(self):
        import time
        import scan_journal

        radar_id = (await self.add_user_radars(1))[0]
        now = time.time()
        scan_journal.forget(radar_id)
        scan_journal.record_scan([radar_id], ok=True, at=now - 20, ms=300, channel="direct", items=50)
        scan_journal.record_scan([radar_id], ok=False, at=now - 12, ms=900, kind="forbidden")
        scan_journal.record_scan([radar_id], ok=True, at=now - 5, ms=310, channel="direct", items=50)
        scan_journal.record_lot(radar_id, item_id="1", title="PSP Vita", url="https://www.avito.ru/1",
                                published=now - 245, seen_at=now - 5, scan_at=now - 5, previous_scan_at=now - 20,
                                reliable=True)
        scan_journal.record_sent([radar_id], "1", now - 4)

        split = scan_journal.lot_split(scan_journal.lots(radar_id)[0])
        self.assertAlmostEqual(split["bot"], 15, delta=0.5)
        self.assertAlmostEqual(split["avito"], 225, delta=0.5)
        self.assertEqual(scan_journal.summary(radar_id, now=now)["fail"], 1)

        await self.dispatch_callback(f"admin_scans:{radar_id}:{USER_ID}:0", config.ADMIN_ID)
        text = self.last_text()
        self.assertIn("+1 нов.", text)
        self.assertIn("→ <b>Авито</b>", text)
        self.assertIn("PSP Vita", text)
        scan_journal.forget(radar_id)


if __name__ == "__main__":
    unittest.main()
