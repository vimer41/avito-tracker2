import html
import logging
import re
import time
from datetime import datetime
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

import config
import database
import legal_docs
import ui
from logger_setup import log_user_event

logger = logging.getLogger(__name__)
router = Router()


class SupportStates(StatesGroup):
    waiting_for_user_message = State()
    waiting_for_admin_reply = State()


# ── 1. Пользователь: начало создания обращения ──────────────────────────

@router.callback_query(F.data == "support_start_ticket")
async def cb_support_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(SupportStates.waiting_for_user_message)
    text = (
        f"{ui.e('write')} <b>Сообщение в поддержку</b>\n\n"
        "Опишите вопрос одним сообщением. Можно приложить скриншот, фото или голосовое.\n"
        "Оператор ответит прямо в этом чате.\n\n"
        f"{ui.e('mail')} Или на почту: {legal_docs.SUPPORT_EMAIL}"
    )
    await ui.show(call, text, ui.kb([ui.btn("Отмена", "cancel", callback_data="support_cancel")]))


@router.callback_query(F.data == "support_cancel")
async def cb_support_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.show(call, f"{ui.e('cancel')} <b>Обращение отменено</b>", ui.kb([ui.home_btn()]))



# ── 2. Пользователь: отправка сообщения в поддержку ─────────────────────

# Telegram counts 1024 characters of a caption and 4096 of a message, and the ticket header takes its share.
TICKET_CAPTION_TEXT = 700
TICKET_MESSAGE_TEXT = 3500
TICKET_MEDIA_FIELDS = ("document", "voice", "video")


def ticket_media(message: Message) -> tuple[str, str]:
    if message.photo:
        return "photo", message.photo[-1].file_id
    for kind in TICKET_MEDIA_FIELDS:
        media = getattr(message, kind)
        if media:
            return kind, media.file_id
    return "", ""


async def send_ticket_to_admin(bot: Bot, admin_id: int, ticket: dict) -> None:
    created = datetime.fromtimestamp(ticket["created_epoch"], database.MSK_TZ).strftime("%d.%m.%Y %H:%M")
    username = f"@{ticket['username']}" if ticket["username"] else "без username"
    title = "ОБРАЩЕНИЕ ПО ОПЛАТЕ / ВОЗВРАТУ" if ticket["is_payment"] else "НОВОЕ ОБРАЩЕНИЕ В ПОДДЕРЖКУ!"
    header = (
        f"📩 <b>{title}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>От:</b> {html.escape(ticket['full_name'])} ({username})\n"
        f"🆔 <b>ID:</b> <code>{ticket['user_id']}</code>\n"
        f"🕒 <b>Время:</b> {created} МСК\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📝 <b>Текст:</b>\n"
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Ответить пользователю", callback_data=f"support_reply:{ticket['user_id']}")]
    ])
    text, kind = ticket["text"], ticket["media_type"]
    limit = TICKET_CAPTION_TEXT if kind else TICKET_MESSAGE_TEXT
    body = header + html.escape(text[:limit])
    if kind:
        send = getattr(bot, f"send_{kind}")
        await send(chat_id=admin_id, caption=body, reply_markup=admin_kb, parse_mode="HTML", **{kind: ticket["file_id"]})
    else:
        await bot.send_message(chat_id=admin_id, text=body, reply_markup=admin_kb, parse_mode="HTML")
    if text[limit:]:
        await bot.send_message(chat_id=admin_id, text=f"📝 Продолжение:\n{html.escape(text[limit:])}", parse_mode="HTML")


async def deliver_ticket(bot: Bot, ticket: dict) -> bool:
    """One admin is enough. A saved ticket stays in support_outbox until that happens."""
    delivered = False
    for admin_id in config.ADMIN_IDS:
        try:
            await send_ticket_to_admin(bot, admin_id, ticket)
            delivered = True
        except Exception as err:
            logger.error(f"Не удалось отправить обращение в поддержку администратору {admin_id}: {err}")
    if delivered:
        await database.delete_support_ticket(ticket["id"])
    return delivered


async def retry_undelivered_tickets(bot: Bot) -> int:
    delivered = 0
    for ticket in await database.get_undelivered_support_tickets():
        delivered += int(await deliver_ticket(bot, ticket))
    return delivered


@router.message(SupportStates.waiting_for_user_message)
async def handle_user_support_message(message: Message, state: FSMContext, bot: Bot):
    user = message.from_user
    user_id = user.id

    # A bottom-menu button or a command is navigation, not the ticket text.
    from handlers.searches import route_menu_text
    if await route_menu_text(message, state):
        return

    text_content = (message.text or message.caption or "(без текста, вложение)").strip()
    state_data = await state.get_data()
    media_type, file_id = ticket_media(message)
    ticket = {
        "user_id": user_id,
        "full_name": user.full_name,
        "username": user.username or "",
        "text": text_content,
        "media_type": media_type,
        "file_id": file_id,
        "is_payment": bool(state_data.get("payment_support")),
        "created_epoch": time.time(),
    }
    ticket["id"] = await database.save_support_ticket(**ticket)
    if not await deliver_ticket(bot, ticket):
        logger.warning("Обращение #%s пока не доставлено, повторю позже", ticket["id"])

    await state.clear()

    log_user_event(
        user_id=user_id,
        username=user.username,
        first_name=user.first_name,
        action=f"Обращение в поддержку: «{text_content[:80]}»",
        status="SUPPORT"
    )

    await message.answer(
        f"{ui.e('ok')} <b>Обращение отправлено</b>\n\n"
        "Ответ придёт в этот чат. Поддержка работает с 09:00 до 23:00 (МСК).",
        reply_markup=ui.kb([ui.home_btn()]),
        parse_mode="HTML"
    )


# ── 3. Администратор: ответ через кнопку ────────────────────────────────

@router.callback_query(F.data.startswith("support_reply:"))
async def cb_admin_reply_start(call: CallbackQuery, state: FSMContext):
    admin_id = call.from_user.id
    if not config.is_admin(admin_id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    target_user_id = int(call.data.split(":")[1])
    target_user = await database.get_or_create_user(target_user_id)
    target_name = html.escape(target_user.get("first_name") or "Пользователь")
    username_str = f"@{target_user.get('username')}" if target_user.get("username") else "без username"

    await state.set_state(SupportStates.waiting_for_admin_reply)
    await state.update_data(target_user_id=target_user_id, target_name=target_name)

    text = (
        f"✍️ <b>Ответ пользователю:</b> <b>{target_name}</b> ({username_str})\n"
        f"🆔 <b>ID:</b> <code>{target_user_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Введите текст ответа (или отправьте фото с описанием).\n"
        "Сообщение будет доставлено пользователю прямо в чат с ботом.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_reply_cancel")]
    ])

    await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_reply_cancel")
async def cb_admin_reply_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ <b>Ответ пользователю отменён.</b>", parse_mode="HTML")
    await call.answer()


@router.message(SupportStates.waiting_for_admin_reply)
async def handle_admin_reply_message(message: Message, state: FSMContext, bot: Bot):
    admin_id = message.from_user.id
    if not config.is_admin(admin_id):
        return

    # Проверка на отмену
    if message.text and message.text.strip().lower() in {"отмена", "❌ отмена", "/cancel"}:
        await state.clear()
        await message.answer("❌ <b>Ответ пользователю отменён.</b>", parse_mode="HTML")
        return

    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    target_name = data.get("target_name") or f"ID {target_user_id}"

    if not target_user_id:
        await state.clear()
        await message.answer("❌ <b>Ошибка:</b> адресат сообщения не найден.", parse_mode="HTML")
        return

    success, err_msg = await send_reply_to_user(bot, target_user_id, message)
    await state.clear()

    if success:
        await message.answer(
            f"✅ <b>Ответ успешно доставлен пользователю {target_name}</b> (ID: <code>{target_user_id}</code>)!",
            parse_mode="HTML"
        )
        logger.info(f"👑 [АДМИН] Администратор ID:{admin_id} отправил ответ пользователю ID:{target_user_id}")
    else:
        await message.answer(
            f"⚠️ <b>Не удалось доставить ответ пользователю:</b> {err_msg}",
            parse_mode="HTML"
        )


# ── 4. Администратор: быстрый ответ через Reply (свайп/цитирование) ─────

@router.message(F.reply_to_message)
async def handle_admin_quote_reply(message: Message, state: FSMContext, bot: Bot):
    admin_id = message.from_user.id
    if not config.is_admin(admin_id):
        return

    # Если администратор уже находится в каком-то состоянии FSM, пропускаем
    current_state = await state.get_state()
    if current_state:
        return

    replied = message.reply_to_message
    replied_text = replied.text or replied.caption or ""

    # Извлекаем ID пользователя: Telegram отдаёт текст уже без HTML-тегов
    m = re.search(r"🆔\s*ID:\s*(\d+)", replied_text)
    if not m:
        return

    target_user_id = int(m.group(1))
    if target_user_id == admin_id:
        return

    success, err_msg = await send_reply_to_user(bot, target_user_id, message)
    if success:
        await message.reply(
            f"✅ <b>Ответ доставлен пользователю ID: <code>{target_user_id}</code></b>",
            parse_mode="HTML"
        )
        logger.info(f"👑 [АДМИН] Быстрый ответ (Quote Reply) от ID:{admin_id} для ID:{target_user_id}")
    else:
        await message.reply(
            f"⚠️ <b>Не удалось доставить:</b> {err_msg}",
            parse_mode="HTML"
        )


# ── Вспомогательная функция отправки ответа пользователю ────────────────

async def send_reply_to_user(bot: Bot, target_user_id: int, message: Message) -> tuple[bool, str]:
    """Отправляет ответ администратора пользователю от имени бота."""
    reply_text = message.text or message.caption or ""
    clean_text = html.escape(reply_text.strip())

    user_header = f"{ui.e('support')} <b>Ответ поддержки</b>\n\n{clean_text}"
    reply_kb = ui.kb([ui.btn("Ответить", "write", callback_data="support_start_ticket")])

    try:
        if message.photo:
            photo_id = message.photo[-1].file_id
            await bot.send_photo(
                chat_id=target_user_id,
                photo=photo_id,
                caption=user_header,
                reply_markup=reply_kb,
                parse_mode="HTML"
            )
        elif message.document:
            await bot.send_document(
                chat_id=target_user_id,
                document=message.document.file_id,
                caption=user_header,
                reply_markup=reply_kb,
                parse_mode="HTML"
            )
        elif message.voice:
            await bot.send_voice(
                chat_id=target_user_id,
                voice=message.voice.file_id,
                caption=user_header,
                reply_markup=reply_kb,
                parse_mode="HTML"
            )
        elif message.video:
            await bot.send_video(
                chat_id=target_user_id,
                video=message.video.file_id,
                caption=user_header,
                reply_markup=reply_kb,
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                chat_id=target_user_id,
                text=user_header,
                reply_markup=reply_kb,
                parse_mode="HTML"
            )
        return True, ""
    except TelegramForbiddenError:
        return False, "пользователь заблокировал бота"
    except Exception as e:
        return False, str(e)
