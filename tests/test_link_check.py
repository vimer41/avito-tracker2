import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import _environment  # noqa: F401

import database
import link_check
from handlers import searches

USER_ID = 7101


class TestLinkProblem(unittest.TestCase):
    def test_home_page_is_not_a_search(self):
        for url in ("https://www.avito.ru/?bt=1&s=104", "https://www.avito.ru", "avito.ru/", "https://m.avito.ru/?q=iphone"):
            self.assertEqual(link_check.link_problem(url), link_check.HOME, url)

    def test_service_pages_are_not_a_search(self):
        for url in ("https://www.avito.ru/profile", "https://www.avito.ru/favorites?s=104", "https://www.avito.ru/Messenger/x"):
            self.assertEqual(link_check.link_problem(url), link_check.SERVICE, url)

    def test_searches_pass(self):
        for url in (
            "https://www.avito.ru/moskva?q=iphone",
            "https://www.avito.ru/all?q=iphone&s=104",
            "https://www.avito.ru/rossiya/telefony",
            "https://www.avito.ru/sankt-peterburg/kvartiry/prodam-ASgBAgICAUSSA8YQ",
            "https://www.avito.ru/user/abc123/profile",
            "https://www.avito.ru/brands/shop1",
        ):
            self.assertIsNone(link_check.link_problem(url), url)

    def test_bare_region_is_refused_for_new_radars_only(self):
        for url in ("https://www.avito.ru/all?radius=0&s=104", "https://www.avito.ru/moskva", "avito.ru/rossiya?s=104&q="):
            self.assertEqual(link_check.link_problem(url), link_check.NO_SEARCH, url)
            self.assertTrue(link_check.keeps_running(url), url)   # a running radar is not paused
            self.assertEqual(link_check.card_warning(url), "", url)
        self.assertIsNone(link_check.link_problem("https://www.avito.ru/all?categoryId=9&s=104"))
        self.assertIn("ни раздела, ни запроса", searches.search_url_error("https://www.avito.ru/all?radius=0&s=104") or "")

    def test_running_home_page_with_a_query_is_left_alone(self):
        self.assertTrue(link_check.keeps_running("https://www.avito.ru/?q=iphone&s=104"))
        self.assertFalse(link_check.keeps_running("https://www.avito.ru/?bt=1&s=104"))
        self.assertFalse(link_check.keeps_running("https://www.avito.ru/profile"))
        self.assertTrue(link_check.keeps_running("https://www.avito.ru/moskva?q=iphone"))

    def test_add_flow_refuses_the_home_page(self):
        self.assertIn("главная", searches.search_url_error("https://www.avito.ru/?bt=1&s=104"))
        self.assertIsNone(searches.search_url_error("https://www.avito.ru/moskva?q=iphone"))


class TestBadLinkRadars(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-links-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        await database.get_or_create_user(USER_ID, "u", "U")
        await database.grant_subscription(USER_ID, 30, "pro")
        self.good = await database.add_search(USER_ID, "good", "https://www.avito.ru/moskva?q=a&s=104", initial_items=[])
        self.bad = await database.add_search(USER_ID, "bad", "https://www.avito.ru/?bt=1&s=104", initial_items=[])

    async def asyncTearDown(self):
        await database.close_db()
        self.db_patch.stop()
        self.directory.cleanup()

    async def test_startup_pass_pauses_and_tells_the_owner_once(self):
        bot = AsyncMock()
        self.assertEqual(await link_check.warn_bad_links(bot, pause_between=0), 1)
        bad = await database.get_search(self.bad)
        self.assertEqual((bad["is_active"], bad["pause_reason"]), (0, database.PAUSE_BAD_LINK))
        self.assertEqual((await database.get_search(self.good))["is_active"], 1)
        owner_calls = [c for c in bot.send_message.await_args_list if c.kwargs["chat_id"] == USER_ID]
        self.assertEqual(len(owner_calls), 1)
        self.assertIn(f"change_url:{self.bad}", str(owner_calls[0].kwargs["reply_markup"]))

        bot.reset_mock()
        self.assertEqual(await link_check.warn_bad_links(bot, pause_between=0), 0)
        bot.send_message.assert_not_awaited()

    async def test_new_link_resumes_the_radar(self):
        await link_check.warn_bad_links(AsyncMock(), pause_between=0)
        await database.replace_search_url(self.bad, USER_ID, "https://www.avito.ru/moskva?q=b&s=104", 0, initial_items=[])
        radar = await database.get_search(self.bad)
        self.assertEqual((radar["is_active"], radar["pause_reason"]), (1, ""))

    async def test_card_warns_about_a_bad_link(self):
        await link_check.warn_bad_links(AsyncMock(), pause_between=0)
        text, _ = searches.build_radar_card(await database.get_search(self.bad))
        self.assertIn("ссылка не на поиск", text)
        self.assertIn("главная страница Авито", text)
        good_text, _ = searches.build_radar_card(await database.get_search(self.good))
        self.assertNotIn("главная страница", good_text)


if __name__ == "__main__":
    unittest.main()
