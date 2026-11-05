import os
import asyncio
import logging
import subprocess
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand
from aiogram.fsm.storage.memory import MemoryStorage

import config
import database
import ui
from handlers import main_router
from middlewares import AccessControlMiddleware, ActionLoggingMiddleware
from proxy_pool import close_proxy_pool, get_proxy_pool
from scheduler import start_background_services, close_image_session

# Принудительная поддержка UTF-8 в консоли Windows (для эмодзи)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Настройка логирования: описание файлов и уровней в logger_setup.py
from logger_setup import log_event, register_secret, setup_logging
setup_logging()
register_secret(config.PLATEGA_SECRET)
logger = logging.getLogger("bot")


def release_version() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5, cwd=config.BASE_DIR
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


async def start_health_server():
    """
    Легковесный HTTP-сервер для хостингов вроде Render (Web Service).
    Render требует привязки к порту $PORT, иначе считает сервис упавшим.
    Потребляет < 1 МБ RAM.
    """
    port = os.getenv("PORT")
    if not port:
        return None
    try:
        app = web.Application()
        async def handle_ping(request):
            return web.Response(text="Avito Bot OK (512MB RAM Mode)")
        app.router.add_get("/", handle_ping)
        app.router.add_get("/health", handle_ping)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(port))
        await site.start()
        logger.info(f"Health-check HTTP сервер запущен на 0.0.0.0:{port} (Render-совместимость)")
        return runner
    except Exception as e:
        logger.warning(f"Не удалось запустить health-check сервер на порту {port}: {e}")
        return None


async def setup_bot_profile(bot: Bot):
    """Sets the command menu. The bot descriptions are edited by hand in BotFather."""
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="tariffs", description="Тарифы и оплата Telegram Stars"),
            BotCommand(command="profile", description="Моя подписка и радары"),
            BotCommand(command="help", description="Инструкция"),
            BotCommand(command="terms", description="Условия подписки и возврата"),
            BotCommand(command="paysupport", description="Помощь с оплатой / возврат Stars"),
        ])
        logger.info("Команды бота обновлены в Telegram API")
    except Exception as e:
        logger.warning(f"Не удалось обновить команды бота в Telegram API: {e}")


async def main():
    if not config.BOT_TOKEN or config.BOT_TOKEN == "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ":
        logger.error(
            "ОШИБКА: Не указан BOT_TOKEN в файле .env!\n"
            "Пожалуйста, создайте бота через @BotFather в Telegram и укажите токен в файле .env."
        )
        return

    config.check_deprecated_env_vars()
    config.validate_runtime_config()

    # Запуск легковесного сервера для Render Web Service (если передан PORT)
    web_runner = await start_health_server()
    bot = None
    background_tasks = []
    platega_runner = None

    try:
        await database.init_db()
        await database.enforce_radar_limits()
        pool = get_proxy_pool()
        await pool.reload_async(allow_direct=config.ALLOW_DIRECT_AVITO)
        bot = Bot(
            token=config.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        await ui.load()
        bot.session.middleware(ui.PremiumEmojiFallback())
        dp = Dispatcher(storage=MemoryStorage())
        dp.message.middleware(ActionLoggingMiddleware())
        dp.callback_query.middleware(ActionLoggingMiddleware())
        access_middleware = AccessControlMiddleware()
        dp.message.middleware(access_middleware)
        dp.callback_query.middleware(access_middleware)
        dp.include_router(main_router)

        # A deploy must not discard unread Telegram updates.
        for _attempt in range(3):
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                break
            except TelegramNetworkError as exc:
                if _attempt == 2:
                    raise
                logger.warning("Временная ошибка сети Telegram при delete_webhook (попытка %s/3): %s", _attempt + 1, exc)
                await asyncio.sleep(1.5)
        await setup_bot_profile(bot)
        recovery = await database.recover_outbox_on_startup()
        if any(recovery.values()):
            logger.info("Восстановление очереди уведомлений при старте: %s", recovery)
        logger.info(
            "Stars checkout: enabled=%s lite=%s standart=%s pro=%s days=%s",
            config.STARS_PAYMENTS_ENABLED,
            config.STARS_PRICE_LITE,
            config.STARS_PRICE_STANDART,
            config.STARS_PRICE_PRO,
            config.STARS_SUBSCRIPTION_DAYS,
        )
        background_tasks = start_background_services(bot)
        if config.PLATEGA_ENABLED:
            import platega_webhook
            import rub_payments
            resumed = await rub_payments.resume(bot)
            background_tasks.append(asyncio.create_task(rub_payments.sweep_forever(bot), name="platega-sweep"))
            try:
                platega_runner = await platega_webhook.start(bot)
            except OSError as exc:
                logger.error("Platega callback server did not start: %s", exc)
            logger.info("Platega checkout: enabled, watchers resumed=%s, webhook=%s",
                        resumed, "on" if platega_runner else "off")
        version = release_version()
        log_event(
            logger,
            logging.INFO,
            "app.start",
            f"Бот запущен · версия {version} · сеть {config.NET_MODE} · python {sys.version.split()[0]}",
            version=version,
            net_mode=config.NET_MODE,
            pid=os.getpid(),
        )
        await dp.start_polling(bot)
    finally:
        logger.info("Завершение работы бота...")
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await close_image_session()
        await close_proxy_pool()
        await database.close_db()
        if bot is not None:
            await bot.session.close()
        if web_runner:
            await web_runner.cleanup()
        if platega_runner:
            await platega_runner.cleanup()
        log_event(logger, logging.INFO, "app.stop", "Бот остановлен", clean=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен.")
