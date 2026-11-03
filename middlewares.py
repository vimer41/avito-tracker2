import html
import logging
import time
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import TelegramObject, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
import trial_gate
import ui
import database

from logger_setup import log_user_event
from runtime_metrics import sanitize_proxy_credentials

logger = logging.getLogger(__name__)

EXPIRED_ALERT_INTERVAL = 3600.0  # one person pressing buttons must not flood the admin chat
_expired_alert_at: dict[int, float] = {}


def expired_alert_due(user_id: int) -> bool:
    now = time.monotonic()
    if now - _expired_alert_at.get(user_id, -EXPIRED_ALERT_INTERVAL) < EXPIRED_ALERT_INTERVAL:
        return False
    if len(_expired_alert_at) > 5000:
        _expired_alert_at.clear()
    _expired_alert_at[user_id] = now
    return True


class ActionLoggingMiddleware(BaseMiddleware):
    """
    Детально логирует каждое действие пользователя в logs/user_actions.log:
    - Текстовые сообщения и команды
    - Нажатия на инлайн-кнопки
    - Текущее состояние FSM-машины (на каком этапе находился пользователь)
    - Ошибки выполнения хэндлеров для данного пользователя
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        state = data.get("state")
        current_state = None
        if state:
            try:
                current_state = await state.get_state()
            except Exception:
                pass

        if isinstance(event, Message) and event.text:
            if current_state and current_state.endswith("waiting_for_proxy"):
                logged_text = "[прокси скрыты]"
            else:
                # Mask before truncating: a cut URL would no longer match the credential pattern.
                logged_text = sanitize_proxy_credentials(event.text)[:100]
            log_user_event(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                action=f"Сообщение: «{logged_text}»",
                status="MSG",
                fsm_state=current_state
            )
        elif isinstance(event, CallbackQuery) and event.data:
            log_user_event(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                action=f"Нажата кнопка: «{event.data}»",
                status="CLICK",
                fsm_state=current_state
            )

        try:
            return await handler(event, data)
        except Exception as e:
            if isinstance(e, TelegramBadRequest) and isinstance(event, CallbackQuery) and "query is too old" in str(e):
                # The button press outlived Telegram's answer window (bot was busy); nothing to repair.
                logging.getLogger(__name__).info("Пропущен устаревший callback %r от %s", event.data, user.id)
                return None
            log_user_event(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                action="СБОЙ ПРИ ОБРАБОТКЕ СОБЫТИЯ",
                details=f"Ошибка: {str(e)[:150]}",
                status="ERROR",
                fsm_state=current_state
            )
            raise


class AccessControlMiddleware(BaseMiddleware):

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        user_id = user.id
        bot: Bot = data.get("bot")

        # В группах бот только копирует лоты: там свой обработчик, без меню и предложений триала.
        if isinstance(event, Message) and event.chat.type != "private":
            return await handler(event, data)

        # Финансовые служебные события обязательны даже при истёкшем доступе
        # и выключенном checkout. Их проверяет отдельный платёжный обработчик.
        if isinstance(event, Message) and (event.successful_payment or event.refunded_payment):
            return await handler(event, data)

        # Административные действия никогда не обходят проверку роли.
        if isinstance(event, CallbackQuery) and event.data and event.data.startswith(("admin_", "support_reply:")):
            if not config.is_admin(user_id):
                await event.answer("У вас нет прав администратора", show_alert=True)
                return

        # Публичные меню доступны и после истечения подписки.
        if isinstance(event, CallbackQuery) and event.data:
            allowed_callbacks = (
                "buy_tariff:", "pay_request:", "demo_tariff:", "stars_buy:", "pay_method:", "doc_page:",
            )
            allowed_exact = {
                "open_subscription_menu", "activate_trial_btn", "trial_check", "support_start_ticket", "support_cancel",
                "start_approved", "refresh_profile", "open_help_menu", "cancel_fsm",
                "payment_terms", "payment_support", "other_payment_methods",
                "open_docs_menu", "doc_agreement", "doc_privacy",
            }
            if event.data in allowed_exact or any(event.data.startswith(prefix) for prefix in allowed_callbacks):
                return await handler(event, data)

        if isinstance(event, Message) and event.text:
            public_commands = {
                "/start", "/help", "/profile", "/subscribe", "/tariffs", "/cancel",
                "/paysupport", "/terms", "/agreement", "/privacy", "/policy", "/offer", "/docs",
            }
            command = (event.text.split(maxsplit=1) or [""])[0].split("@", 1)[0]
            public_menu = ui.menu_texts("tariffs", "support", "profile", "help", "docs") | ui.CANCEL_TEXTS
            if command in public_commands or event.text.strip() in public_menu:
                state = data.get("state")
                if state is not None:
                    await state.clear()
                return await handler(event, data)

        # Разрешаем отправку сообщений, если пользователь пишет обращение в поддержку
        state = data.get("state")
        if state:
            current_st = await state.get_state()
            if current_st == "SupportStates:waiting_for_user_message":
                return await handler(event, data)

        # 2. Проверяем наличие пользователя в БД
        user_record = await database.get_or_create_user(user_id, user.username, user.first_name)

        # 3. Проверяем, есть ли доступ
        if await database.is_user_allowed(user_id):
            # Автоматический сброс FSM-состояния при нажатии любой кнопки главного меню или команд
            if isinstance(event, Message) and event.text:
                txt = event.text.strip()
                menu_keywords = ui.ALL_MENU_TEXTS
                if txt.startswith("/") or txt in menu_keywords:
                    fsm_state = data.get("state")
                    if fsm_state:
                        current_st = await fsm_state.get_state()
                        if current_st:
                            await fsm_state.clear()

            return await handler(event, data)

        # 4. Триал ещё не брали: предлагаем его, админов это не касается
        if not user_record.get("trial_used"):
            offer_text, offer_kb = trial_gate.offer()
            await ui.show(event, offer_text, offer_kb)
            return

        # 5. Если доступ НЕ одобрен (пробный период истёк или доступ закрыт):
        text_for_user = (
            f"{ui.e('lock')} <b>Доступ закончился</b>\n\n"
            "Чтобы бот снова присылал новые лоты, выберите тариф. "
            "Радары и настройки сохранены и заработают сразу после оплаты."
        )

        user_kb = ui.kb(
            [ui.btn("Выбрать тариф", "tariffs", callback_data="open_subscription_menu", style="primary")],
            [ui.btn("Написать в поддержку", "support", callback_data="support_start_ticket")],
        )

        if isinstance(event, Message):
            await event.answer(text_for_user, reply_markup=user_kb, parse_mode="HTML")
        elif isinstance(event, CallbackQuery):
            await event.answer("Срок действия доступа истёк. Выберите тариф или напишите в поддержку.", show_alert=True)

        # 6. Отправляем запрос администраторам
        if not expired_alert_due(user_id):
            return

        user_msg = f"\n💬 <b>Сообщение:</b> <i>«{html.escape(event.text[:250])}»</i>" if (isinstance(event, Message) and event.text) else ""
        admin_text = (
            "🔔 <b>Пользователь с истёкшим доступом пишет в бот:</b>\n\n"
            f"👤 <b>Имя:</b> {html.escape(user.full_name)}\n"
            f"🔗 <b>Username:</b> @{user.username if user.username else 'нет'}\n"
            f"🆔 <b>ID:</b> <code>{user_id}</code>{user_msg}"
        )
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="💳 Выдать / продлить подписку", callback_data=f"admin_user:{user_id}:0"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject:{user_id}")
            ],
            [
                InlineKeyboardButton(text="✍️ Написать пользователю", callback_data=f"support_reply:{user_id}")
            ]
        ])

        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=admin_text,
                    reply_markup=admin_kb,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

        # Прерываем выполнение дальнейших обработчиков
        return
