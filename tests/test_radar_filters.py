import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment
import database
import radar_filters
import scheduler
from collector import SourceCollection
from parser import AvitoItem


def lot(item_id="1", title="iPhone 13", price=30000, **fields) -> AvitoItem:
    return AvitoItem(item_id=item_id, title=title, price=f"{price} ₽", url=f"https://www.avito.ru/{item_id}",
                     raw_price=price, **fields)


class PriceRangeTests(unittest.TestCase):
    def test_accepted_forms(self):
        cases = {
            "10000-50000": (10000, 50000),
            "10 000 – 50 000 ₽": (10000, 50000),
            "50000 10000": (10000, 50000),
            "от 10 000": (10000, 0),
            "до 50000": (0, 50000),
            "15000": (15000, 0),
            "нет": (0, 0),
        }
        for text, expected in cases.items():
            self.assertEqual(radar_filters.parse_price_range(text), expected, text)

    def test_unclear_input_is_rejected(self):
        for text in ("дёшево", "1 2 3", "1-2-3", "99999999999"):
            self.assertIsNone(radar_filters.parse_price_range(text), text)


class FilterReasonTests(unittest.TestCase):
    def test_empty_radar_passes_everything(self):
        self.assertIsNone(radar_filters.filter_reason(lot(), {}))

    def test_price_bounds(self):
        radar = {"price_min": 10000, "price_max": 20000}
        self.assertEqual(radar_filters.filter_reason(lot(price=9999), radar), "price")
        self.assertEqual(radar_filters.filter_reason(lot(price=20001), radar), "price")
        self.assertIsNone(radar_filters.filter_reason(lot(price=20000), radar))

    def test_lot_without_a_price_passes_the_price_filter(self):
        item = lot()
        item.raw_price = None
        self.assertIsNone(radar_filters.filter_reason(item, {"price_min": 10000}))

    def test_delivery_and_promotion(self):
        self.assertEqual(radar_filters.filter_reason(lot(), {"delivery_only": 1}), "delivery")
        self.assertIsNone(radar_filters.filter_reason(lot(delivery="Авито Доставка"), {"delivery_only": 1}))
        self.assertEqual(radar_filters.filter_reason(lot(is_promotion=True), {"hide_promoted": 1}), "promoted")

    def test_must_words_need_one_match_in_title_or_description(self):
        radar = {"must_words": json.dumps(["256", "pro max"])}
        self.assertEqual(radar_filters.filter_reason(lot(), radar), "must_words")
        self.assertIsNone(radar_filters.filter_reason(lot(title="iPhone 13 PRO MAX"), radar))
        self.assertIsNone(radar_filters.filter_reason(lot(description="память 256 ГБ"), radar))

    def test_broken_must_words_do_not_drop_lots(self):
        self.assertIsNone(radar_filters.filter_reason(lot(), {"must_words": "not json"}))


class MarketTests(unittest.TestCase):
    def _page(self, *extra):
        return [lot(str(i), price=30000 + i * 100) for i in range(14)] + list(extra)

    def test_cheap_lot_gets_the_estimate(self):
        cheap = lot("cheap", price=20000)
        radar_filters.mark_market(self._page(cheap))
        self.assertEqual(cheap.market_percent, 35)
        self.assertGreater(cheap.market_median, 30000)

    def test_ordinary_lot_and_suspicious_lot_stay_unmarked(self):
        ordinary, accessory = lot("ok", price=29000), lot("case", price=500)
        radar_filters.mark_market(self._page(ordinary, accessory))
        self.assertIsNone(ordinary.market_percent)
        self.assertIsNone(accessory.market_percent)

    def test_small_page_has_no_market(self):
        cheap = lot("cheap", price=1000)
        radar_filters.mark_market([lot(str(i)) for i in range(5)] + [cheap])
        self.assertIsNone(cheap.market_percent)

    def test_broad_radar_with_a_wide_spread_has_no_market(self):
        items = [lot(str(i), price=1000 * (2 ** (i % 8))) for i in range(24)]
        radar_filters.mark_market(items)
        self.assertTrue(all(item.market_percent is None for item in items))

    def test_stale_estimate_is_cleared(self):
        item = lot(market_percent=30, market_median=40000)
        radar_filters.mark_market([item])
        self.assertIsNone(item.market_percent)

    def test_notification_shows_the_estimate(self):
        item = lot(market_percent=25, market_median=40000)
        self.assertIn("На 25 % дешевле медианы выдачи (40 000 ₽)", scheduler.build_notification_text(item, "Радар"))
        self.assertNotIn("медианы", scheduler.build_notification_text(lot(), "Радар"))


class ReplyTemplateTests(unittest.TestCase):
    def test_placeholders(self):
        text = radar_filters.render_reply("{название} за {цена} продаётся?", lot())
        self.assertEqual(text, "iPhone 13 за 30000 ₽ продаётся?")

    def test_copy_limit(self):
        self.assertEqual(len(radar_filters.render_reply("{название}", lot(title="я" * 400))), 256)

    def test_no_template_no_button(self):
        self.assertEqual(radar_filters.render_reply("  ", lot()), "")
        keyboard = scheduler.build_notification_keyboard("https://www.avito.ru/1", 5)
        self.assertEqual(len(keyboard.inline_keyboard), 2)

    def test_button_copies_the_message(self):
        keyboard = scheduler.build_notification_keyboard("https://www.avito.ru/1", 5, reply_text="Здравствуйте")
        self.assertEqual(keyboard.inline_keyboard[1][0].copy_text.text, "Здравствуйте")


class SavedFiltersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-filters-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        self.user_id = 5001
        await database.get_or_create_user(self.user_id, "fixture", "Fixture")
        await database.activate_trial(self.user_id)
        self.search_id = await database.add_search(self.user_id, "Радар", "https://www.avito.ru/moskva?q=x")

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    def _fresh(self, item_id, price, **fields):
        now = int(time.time())
        return lot(item_id, price=price, date="только что", timestamp=now, t_pub_reliable=True, **fields)

    async def _fanout(self, items):
        now = time.time()
        collection = SourceCollection(
            title="Fixture", items=tuple(items), pages=1, coverage_gap=False, elapsed_ms=10, captured_at=now,
            discovery_times={item.item_id: now for item in items}, top_ids=tuple(i.item_id for i in items[:3]),
            fast_skipped=False,
        )
        radar = await database.get_search(self.search_id)
        self.assertTrue(await scheduler.process_collected_group_fanout(radar["url"], [radar], collection))

    async def test_columns_round_trip_and_owner_is_checked(self):
        self.assertTrue(await database.update_search_filters(
            self.search_id, self.user_id, price_min=100, must_words=json.dumps(["256"]), reply_template="Привет",
        ))
        self.assertFalse(await database.update_search_filters(self.search_id, 5002, price_min=999))
        saved = await database.get_search(self.search_id)
        self.assertEqual(saved["price_min"], 100)
        self.assertEqual(radar_filters.parse_words(saved["must_words"]), ["256"])
        self.assertEqual(saved["reply_template"], "Привет")

    async def test_unknown_column_is_refused(self):
        with self.assertRaises(ValueError):
            await database.update_search_filters(self.search_id, self.user_id, title="x")

    async def test_trial_user_gets_the_lot(self):
        await self._fanout([self._fresh("trial", 15000)])
        claimed = await database.claim_notification_event()
        self.assertTrue(claimed["_user_allowed"])
        self.assertIsNotNone(await scheduler._prepare_delivery(claimed))

    async def test_subscription_counts_in_utc_until_its_last_hour(self):
        async with database.writer_connection() as db:
            await db.execute(
                "UPDATE users SET is_allowed = 1, subscription_until = datetime('now', '+1 hours') WHERE user_id = ?",
                (self.user_id,),
            )
            await db.commit()
        await self._fanout([self._fresh("paid", 15000)])
        self.assertTrue((await database.claim_notification_event())["_user_allowed"])

    async def test_expired_and_revoked_users_get_nothing(self):
        await self._fanout([self._fresh("gone", 15000)])
        await database.set_user_allowed(self.user_id, 0)
        self.assertFalse((await database.claim_notification_event())["_user_allowed"])

    async def test_filtered_lot_is_remembered_and_never_queued(self):
        await database.update_search_filters(self.search_id, self.user_id, price_max=20000)
        await self._fanout([self._fresh("cheap", 15000), self._fresh("dear", 25000)])
        claimed = await database.claim_notification_event()
        self.assertIn("cheap", claimed["payload_json"])
        self.assertIsNone(await database.claim_notification_event())
        self.assertIn("dear", await database.get_seen_items_with_prices(self.search_id))

    async def test_delivery_rechecks_filters_set_after_the_lot_was_queued(self):
        await self._fanout([self._fresh("late", 15000)])
        await database.update_search_filters(self.search_id, self.user_id, price_max=10000)
        claimed = await database.claim_notification_event()
        self.assertTrue(claimed["_fast_claimed"])
        self.assertIsNone(await scheduler._prepare_delivery(claimed))

    async def test_delivery_context_carries_the_template_and_market_survives_the_outbox(self):
        await database.update_search_filters(self.search_id, self.user_id, reply_template="Ещё продаётся?")
        page = [self._fresh(str(i), 30000 + i * 100) for i in range(14)]
        await self._fanout([*page, self._fresh("bargain", 20000)])
        for _ in range(20):
            claimed = await database.claim_notification_event()
            self.assertIsNotNone(claimed)
            if '"bargain"' in claimed["payload_json"]:
                break
        ctx = await scheduler._prepare_delivery(claimed)
        self.assertEqual(ctx["reply_template"], "Ещё продаётся?")
        self.assertEqual(ctx["item"].market_percent, 35)
