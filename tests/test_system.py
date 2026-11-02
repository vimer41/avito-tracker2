import asyncio
import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests import _environment

import config
import database
from handlers.common import get_main_keyboard
from scheduler import TTLCache, get_item_lock


class TestAvitoRadarSystem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Используем отдельную изолированную тестовую базу в tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_bot.db"
        self.db_patch = patch.object(database, "DB_PATH", self.test_db_path)
        self.db_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def test_admin_and_trial_limits(self):
        """Проверка выдачи 5h триала новым юзерам и лимитов радаров."""
        admin_id = 986832959
        trial_user_id = 11223344

        # 1. Администратор
        admin_user = await database.get_or_create_user(admin_id, "admin_user", "Admin")
        self.assertEqual(admin_user["is_allowed"], 1)
        self.assertEqual(admin_user["tariff_plan"], "pro")
        admin_radars = await database.get_user_max_radars(admin_id)
        self.assertEqual(admin_radars, config.MAX_SEARCHES_PER_USER)

        # 2. Новый пользователь: доступа нет, пока не возьмёт триал через подписку на канал
        new_user = await database.get_or_create_user(trial_user_id, "new_buyer", "Ivan")
        self.assertEqual(new_user["trial_used"], 0)
        self.assertIsNone(new_user["subscription_until"])
        self.assertFalse(await database.is_user_allowed(trial_user_id))
        self.assertTrue((await database.activate_trial(trial_user_id))["success"])

        # Доступ активен в течение 6 часов
        is_allowed = await database.is_user_allowed(trial_user_id)
        self.assertTrue(is_allowed)

        # Лимит для триала — ровно 1 радар
        trial_radars = await database.get_user_max_radars(trial_user_id)
        self.assertEqual(trial_radars, 1)

        # Проверяем сводную статистику системы: и админ, и триал-пользователь должны быть учтены в allowed_users
        stats = await database.get_system_stats()
        self.assertEqual(stats["total_users"], 2)
        self.assertEqual(stats["allowed_users"], 2)

    async def test_subscription_upgrade(self):
        """Проверка апгрейда до Standart (4 радара) и PRO (9 радаров)."""
        user_id = 55667788
        await database.get_or_create_user(user_id, "upgrader", "Alex")

        # Апгрейд до Standart
        await database.grant_subscription(user_id, days=30, tariff_plan="standart")
        standart_radars = await database.get_user_max_radars(user_id)
        self.assertEqual(standart_radars, 2)

        # Апгрейд до PRO
        await database.grant_subscription(user_id, days=30, tariff_plan="pro")
        pro_radars = await database.get_user_max_radars(user_id)
        self.assertEqual(pro_radars, 4)

    def test_main_keyboard_tariffs(self):
        """Проверка наличия кнопки тарифов в главном меню."""
        kb = get_main_keyboard()
        buttons = [btn.text for row in kb.keyboard for btn in row]
        self.assertIn("💳 Тарифы", buttons)
        self.assertIn("🎯 Добавить радар", buttons)
        self.assertIn("📁 Мои радары", buttons)

    def test_ttl_cache_logic(self):
        """Проверка кэша file_id Telegram."""
        cache = TTLCache(ttl_seconds=2)
        cache.set("key1", "val1")
        self.assertEqual(cache.get("key1"), "val1")
        self.assertIsNone(cache.get("nonexistent"))

    async def test_item_lock(self):
        """Проверка блокировок против гонки состояний (Cache Stampede)."""
        lock1 = await get_item_lock("item_100")
        lock2 = await get_item_lock("item_100")
        self.assertIs(lock1, lock2)
        lock3 = await get_item_lock("item_200")
        self.assertIsNot(lock1, lock3)

    def test_format_subscription_date_msk(self):
        """Проверка корректной конвертации даты подписки из UTC в MSK (UTC+3) и расчета остатка."""
        from datetime import datetime, timezone, timedelta
        # Допустим, подписка действует до момента через 24 часа
        target_utc = datetime.now(timezone.utc) + timedelta(hours=24)
        raw_str = target_utc.strftime("%Y-%m-%d %H:%M:%S")
        formatted = database.format_subscription_date(raw_str)
        self.assertIn("(МСК)", formatted)
        self.assertIn("осталось", formatted)

    async def test_purge_user_by_pattern(self):
        """Проверка полного удаления пользователя, радаров и данных из БД."""
        u_id = 999888777
        await database.get_or_create_user(u_id, "johnpeterson92", "John")
        await database.activate_trial(u_id)
        await database.add_search(u_id, "iPhone 15", "https://avito.ru/search", 0)
        searches_before = await database.get_user_searches(u_id)
        self.assertEqual(len(searches_before), 1)

        # Вызываем удаление по username
        res = await database.purge_user_by_pattern("johnpeterson92")
        self.assertTrue(res["success"])
        self.assertGreaterEqual(res["deleted"], 1)

        # Проверяем, что пользователь и его радары полностью исчезли
        user_check = await database.get_all_users()
        self.assertFalse(any(u["user_id"] == u_id for u in user_check))
        searches_after = await database.get_user_searches(u_id)
        self.assertEqual(len(searches_after), 0)


    async def test_radar_deletion_atomicity_and_active_check(self):
        """Проверка атомарности delete_search и метода is_search_active."""
        user1 = 1001
        user2 = 1002
        await database.get_or_create_user(user1, "u1", "User One")
        await database.get_or_create_user(user2, "u2", "User Two")
        await database.activate_trial(user1)
        await database.activate_trial(user2)

        search1_id = await database.add_search(user1, "PlayStation 5", "https://avito.ru/ps5", 0)
        self.assertTrue(await database.is_search_active(search1_id))

        # Добавляем лоты в seen_items для search1
        await database.mark_items_seen_bulk(search1_id, [("item_1", 30000), ("item_2", 35000)])
        seen_before = await database.get_seen_items_with_prices(search1_id)
        self.assertEqual(len(seen_before), 2)

        # Попытка чужого пользователя (user2) удалить радар user1: должно вернуть False и НЕ трогать seen_items
        deleted_by_other = await database.delete_search(search1_id, user2)
        self.assertFalse(deleted_by_other)
        seen_after_attempt = await database.get_seen_items_with_prices(search1_id)
        self.assertEqual(len(seen_after_attempt), 2)
        self.assertTrue(await database.is_search_active(search1_id))

        # Удаление владельцем (user1): должно вернуть True, стереть радар и его seen_items
        deleted_by_owner = await database.delete_search(search1_id, user1)
        self.assertTrue(deleted_by_owner)
        self.assertFalse(await database.is_search_active(search1_id))
        seen_after_delete = await database.get_seen_items_with_prices(search1_id)
        self.assertEqual(len(seen_after_delete), 0)

    async def test_cleanup_orphaned_seen_items(self):
        """Проверка очистки сиротских записей seen_items при отсутствии радара."""
        # Вставляем искусственные сиротские записи для несуществующего search_id = 99999
        await database.mark_items_seen_bulk(99999, [("orphan_1", 1000), ("orphan_2", 2000)])
        orphans_before = await database.get_seen_items_with_prices(99999)
        self.assertEqual(len(orphans_before), 2)

        # Вызываем регулярную очистку БД
        deleted_total = await database.cleanup_old_seen_items(days=5)
        self.assertGreaterEqual(deleted_total, 2)

        # Сироты должны быть удалены
        orphans_after = await database.get_seen_items_with_prices(99999)
        self.assertEqual(len(orphans_after), 0)

    async def test_item_lock_rotation_preserves_locked(self):
        """Проверка, что ротация ITEM_LOCKS не удаляет активные (захваченные) мьютексы."""
        from scheduler import ITEM_LOCKS, get_item_lock

        # Создаем лок и захватываем его
        held_lock = await get_item_lock("active_item_in_progress")
        await held_lock.acquire()
        self.assertTrue(held_lock.locked())

        try:
            # Заполняем словарь фейковыми локами до лимита 3000
            for i in range(3005):
                await get_item_lock(f"idle_item_{i}")

            # Проверяем, что активный лок остался на месте и не был перезаписан
            same_held_lock = await get_item_lock("active_item_in_progress")
            self.assertIs(same_held_lock, held_lock)
            self.assertTrue(same_held_lock.locked())
        finally:
            held_lock.release()


    async def test_radar_custom_freshness_and_discount_defaults(self):
        """Проверка дефолтных значений скидок (0 = выключено) и настройки свежести радара."""
        user_id = 77665544
        await database.get_or_create_user(user_id, "fresh_user", "Freshman")
        await database.activate_trial(user_id)

        # 1. Создаем радар и проверяем дефолты
        s_id = await database.add_search(user_id, "MacBook Pro", "https://avito.ru/macbook", 0)
        search_data = await database.get_search(s_id)
        
        # Скидки должны быть отключены по дефолту (0)
        self.assertEqual(search_data.get("min_discount_percent"), 0)
        # Свежесть по умолчанию 5 минут
        self.assertEqual(search_data.get("max_age_minutes"), 5)

        # 2. Настраиваем индивидуальную свежесть (например 15 минут)
        updated = await database.update_search_max_age(s_id, user_id, 15)
        self.assertTrue(updated)
        search_data2 = await database.get_search(s_id)
        self.assertEqual(search_data2.get("max_age_minutes"), 15)

        # 3. Пользователь сам включает скидки (например 10%)
        updated_disc = await database.update_search_discount_percent(s_id, user_id, 10)
        self.assertTrue(updated_disc)
        search_data3 = await database.get_search(s_id)
        self.assertEqual(search_data3.get("min_discount_percent"), 10)

    def test_is_ad_fresh_enough_configurable(self):
        """Проверка работы функции is_ad_fresh_enough с кастомными порогами max_age_minutes (1-30 мин, дефолт 5)."""
        import time
        from scheduler import is_ad_fresh_enough
        from handlers.searches import format_max_age_label

        now = time.time()

        # 1. Тест по точным timestamp: лот выложен 20 минут назад (1200 сек)
        ts_20m_ago = int(now - 20 * 60)
        # При лимите 30 минут — свежий
        self.assertTrue(is_ad_fresh_enough(None, timestamp=ts_20m_ago, max_age_minutes=30))
        # При лимите 10 минут — отсекается как старый
        self.assertFalse(is_ad_fresh_enough(None, timestamp=ts_20m_ago, max_age_minutes=10))
        # По дефолту (5 минут) — отсекается как старый
        self.assertFalse(is_ad_fresh_enough(None, timestamp=ts_20m_ago))

        # 2. Тест по текстовой дате "25 минут назад"
        self.assertTrue(is_ad_fresh_enough("25 минут назад", max_age_minutes=30))
        self.assertFalse(is_ad_fresh_enough("25 минут назад", max_age_minutes=15))
        self.assertFalse(is_ad_fresh_enough("25 минут назад")) # дефолт 5 мин

        # 3. "только что" или "50 секунд назад" — всегда свежие
        self.assertTrue(is_ad_fresh_enough("только что", max_age_minutes=5))
        self.assertTrue(is_ad_fresh_enough("45 секунд назад", max_age_minutes=5))

        # 4. "1 час назад" (от 45 до 60 мин) — при лимите до 30 минут ГАРАНТИРОВАННО отсекается
        self.assertFalse(is_ad_fresh_enough("1 час назад", max_age_minutes=30))
        self.assertFalse(is_ad_fresh_enough("2 часа назад", max_age_minutes=30))

        # 5. Проверка форматирования меток (максимум 30 минут)
        self.assertEqual(format_max_age_label(0), "до 5 мин. ⚡️")
        self.assertEqual(format_max_age_label(5), "до 5 мин. ⚡️")
        self.assertEqual(format_max_age_label(10), "до 10 мин. ⚡️")
        self.assertEqual(format_max_age_label(30), "до 30 мин. ⚡️")
        self.assertEqual(format_max_age_label(60), "до 30 мин. ⚡️")

        # 6. Объявления без даты или с неизвестной датой НЕ считаются свежими
        self.assertFalse(is_ad_fresh_enough(None, None))
        self.assertFalse(is_ad_fresh_enough("Дата не указана", None))
        self.assertFalse(is_ad_fresh_enough("возраст не указан", None))

    def test_normalize_url_radius_and_mobile_domain(self):
        """Проверка сохранения searchRadius и нормализации m.avito.ru в www.avito.ru."""
        from parser import AvitoParser
        url1 = "https://www.avito.ru/sankt-peterburg/odezhda?searchRadius=50&context=xyz&s=1"
        norm1 = AvitoParser.normalize_url(url1)
        self.assertIn("searchRadius=50", norm1)
        self.assertIn("s=104", norm1)
        self.assertNotIn("context", norm1)

        url2 = "https://m.avito.ru/all/telefony/apple/iphone?s=104"
        norm2 = AvitoParser.normalize_url(url2)
        self.assertTrue(norm2.startswith("https://www.avito.ru/all/telefony/apple/iphone"))

    def test_ssr_parser_extracts_raw_price(self):
        """Проверка, что SSR JSON парсер отдаёт числовое значение raw_price."""
        import json
        from parser import AvitoParser
        catalog = {"loaderData": {"data": {"catalog": {"items": [
            {"id": 12345, "title": "Тестовый товар", "urlPath": "/item/12345",
             "priceDetailed": {"value": 15500, "fullString": "15 500 ₽"}},
            {"id": 67890, "title": "Бесплатный товар", "urlPath": "/item/67890",
             "priceDetailed": {"value": 0, "fullString": "Бесплатно"}},
        ]}}}}
        sample_html = '<html><script>' + json.dumps(catalog, ensure_ascii=False) + '</script></html>'
        items = AvitoParser.parse_items(sample_html)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].raw_price, 15500)
        self.assertEqual(items[0].price, "15 500 ₽")
        self.assertEqual(items[1].raw_price, 0)

    async def test_batch_seen_items_query(self):
        """Проверка точечной выборки seen_items по списку item_ids."""
        await database.get_or_create_user(123456)
        await database.activate_trial(123456)
        radar_id = await database.add_search(123456, "Тест", "https://www.avito.ru/test")
        await database.mark_items_seen_bulk(radar_id, [("item1", 100), ("item2", 200), ("item3", 300)])

        # Запрос только для item1 и item3
        subset = await database.get_seen_items_with_prices(radar_id, item_ids=["item1", "item3", "item99"])
        self.assertEqual(subset, {"item1": 100, "item3": 300})

        # Запрос без фильтра возвращает все
        all_items = await database.get_seen_items_with_prices(radar_id)
        self.assertEqual(all_items, {"item1": 100, "item2": 200, "item3": 300})


if __name__ == "__main__":
    unittest.main()


