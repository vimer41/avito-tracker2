"""Free trial gate: the plaque offering it and the channel subscription check behind it.

The bot must be an admin of config.TRIAL_CHANNEL, otherwise Telegram refuses getChatMember. In that
case the trial is not blocked: admins get an alert and the user gets the trial anyway.
"""

import logging
import time

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup

import config
import ui

logger = logging.getLogger(__name__)

SUBSCRIBED_STATUSES = (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER)
ALERT_INTERVAL = 3600
_alerted_at = 0.0


def offer() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{ui.e('trial')} <b>{config.TRIAL_HOURS} часов бесплатно</b>\n"
        "Новые объявления Авито приходят в Telegram, как только появляются в поиске.\n"
        f"Один радар, остальное как на тарифе «Полный»: проверка раз в {config.plan_interval('pro')} с.\n\n"
        "Подпишитесь на наш канал, и пробный период включится сразу:\n"
        "1. Нажмите «Подписаться на канал».\n"
        "2. Вернитесь сюда и нажмите «Я подписался, проверить»."
    )
    channel_url = f"https://t.me/{config.TRIAL_CHANNEL.lstrip('@')}"
    return text, ui.kb(
        [ui.btn("Подписаться на канал", "link", url=channel_url, style="primary")],
        [ui.btn("Я подписался, проверить", "ok", callback_data="trial_check", style="success")],
        [ui.btn("Тарифы", "tariffs", callback_data="open_subscription_menu"),
         ui.btn("Инструкция", "help", callback_data="open_help_menu")],
    )


async def _alert_admins(bot: Bot, exc: Exception) -> None:
    global _alerted_at
    logger.error("Не удалось проверить подписку на %s: %s", config.TRIAL_CHANNEL, exc)
    if time.monotonic() - _alerted_at < ALERT_INTERVAL and _alerted_at:
        return
    _alerted_at = time.monotonic()
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"Бот не может проверить подписку на {config.TRIAL_CHANNEL}: сделайте его администратором канала. "
                "Пока триал выдаётся без проверки.",
            )
        except Exception as send_error:
            logger.error("Не удалось отправить уведомление админу %s: %s", admin_id, send_error)


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """Network failures are raised: the user should retry, not lose the trial."""
    try:
        member = await bot.get_chat_member(config.TRIAL_CHANNEL, user_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await _alert_admins(bot, exc)
        return True
    return member.status in SUBSCRIBED_STATUSES or bool(getattr(member, "is_member", False))
