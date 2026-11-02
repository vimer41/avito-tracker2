import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests import _environment
import logger_setup

with patch.object(logger_setup, "setup_logging"):
    bot_module = importlib.import_module("bot")


class TestStartupCleanup(unittest.IsolatedAsyncioTestCase):
    async def test_database_failure_closes_health_runner(self):
        runner = SimpleNamespace(cleanup=AsyncMock())
        with patch.object(bot_module, "start_health_server", new=AsyncMock(return_value=runner)), patch.object(bot_module.database, "init_db", new=AsyncMock(side_effect=RuntimeError("test database failure"))), patch.object(bot_module, "close_image_session", new=AsyncMock()) as close_images, patch.object(bot_module, "Bot") as create_bot:
            with self.assertRaisesRegex(RuntimeError, "test database failure"):
                await bot_module.main()
            create_bot.assert_not_called()
            runner.cleanup.assert_awaited_once()
            close_images.assert_awaited_once()

    async def test_telegram_failure_closes_session_without_starting_workers(self):
        runner = SimpleNamespace(cleanup=AsyncMock())
        bot = SimpleNamespace(delete_webhook=AsyncMock(side_effect=RuntimeError("test Telegram failure")), session=SimpleNamespace(close=AsyncMock(), middleware=Mock()))
        with patch.object(bot_module, "start_health_server", new=AsyncMock(return_value=runner)), patch.object(bot_module.database, "init_db", new=AsyncMock()), patch.object(bot_module.database, "enforce_radar_limits", new=AsyncMock()), patch.object(bot_module, "close_image_session", new=AsyncMock()), patch.object(bot_module, "Bot", return_value=bot), patch.object(bot_module, "Dispatcher", return_value=Mock()), patch.object(bot_module, "start_background_services") as start_workers:
            with self.assertRaisesRegex(RuntimeError, "test Telegram failure"):
                await bot_module.main()
            bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
            bot.session.close.assert_awaited_once()
            runner.cleanup.assert_awaited_once()
            start_workers.assert_not_called()


if __name__ == "__main__":
    unittest.main()