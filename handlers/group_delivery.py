"""Copies of lots in a group chat (Средний and Полный): linking from the group and the profile screen."""

import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

import config
import database
import ui
from logger_setup import log_user_event

logger = logging.getLogger(__name__)
router = Router()

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
LINK_PAYLOAD = "radar"


async def group_delivery_allowed(user_id: int) -> bool:
    if config.is_admin(user_id):
        return True
    user = await database.get_user(user_id)
    if not user or not await database.is_user_allowed(user_id):
        return False
    return database.classify_user_tariff(user)[0] in config.GROUP_DELIVERY_PLANS


@router.message(CommandStart(), F.chat.type.in_(GROUP_TYPES))
async def link_group(message: Message, bot: Bot):
    """/start in a group (the «add to group» link sends it) makes it the sender's group for lots."""
    user = message.from_user
    if not user or user.is_bot:
        return
    if not await group_delivery_allowed(user.id):
        await message.answer(
            f"{ui.e('lock')} Копии лотов в группу есть на тарифах «Средний» и «Полный». "
            "Выбрать тариф можно в личном чате с ботом."
        )
        return
    await database.set_mirror_chat(user.id, message.chat.id)
    log_user_event(user.id, user.username, user.first_name, "Группа для лотов", f"chat {message.chat.id}", status="OK")
    await message.answer(
        f"{ui.e('ok')} <b>Группа подключена</b>\n\n"
        f"Сюда будут приходить копии лотов {html.escape(user.first_name or 'владельца')}. "
        "Отключить можно в профиле бота."
    )
    try:
        await bot.send_message(user.id, f"{ui.e('users')} Группа «{html.escape(message.chat.title or '')}» подключена: "
                                        "копии новых лотов будут приходить и туда.")
    except Exception:
        pass


@router.message(F.chat.type.in_(GROUP_TYPES))
async def ignore_group_messages(message: Message):
    """Everything else in groups is ignored: menus and radars live in the private chat."""
    return


async def group_screen(user_id: int, bot: Bot) -> tuple[str, object]:
    me = await bot.me()
    add_url = f"https://t.me/{me.username}?startgroup={LINK_PAYLOAD}"
    header = f"{ui.e('users')} <b>Лоты в группу</b>\n\n"
    if not await group_delivery_allowed(user_id):
        text = header + (
            "Бот присылает копию каждого лота в вашу группу: удобно работать вдвоём или с командой.\n\n"
            "<blockquote>Есть на тарифах «Средний» и «Полный».</blockquote>"
        )
        return text, ui.kb(
            [ui.btn("Тарифы", "tariffs", callback_data="open_subscription_menu", style="primary")],
            [ui.btn("« Профиль", callback_data="refresh_profile"), ui.home_btn()],
        )
    chat_id = await database.get_mirror_chat(user_id)
    if chat_id:
        title = ""
        try:
            title = (await bot.get_chat(chat_id)).title or ""
        except Exception:
            pass
        text = header + (
            f"Подключена группа <b>{html.escape(title) or chat_id}</b>. Копия каждого лота приходит туда.\n\n"
            "<blockquote>В группе только ссылки на объявление; скрыть продавца и настройки — здесь, в личном чате. "
            "Если лотов очень много, Telegram ограничивает группу 20 сообщениями в минуту, и часть копий пропадёт.</blockquote>"
        )
        return text, ui.kb(
            [ui.btn("Отключить группу", "cancel", callback_data="group_off", style="danger")],
            [ui.btn("Подключить другую", "users", url=add_url)],
            [ui.btn("« Профиль", callback_data="refresh_profile"), ui.home_btn()],
        )
    text = header + (
        "Бот будет присылать копию каждого лота в вашу группу: удобно работать вдвоём или с командой.\n\n"
        "1. Нажмите «Добавить в группу» и выберите группу.\n"
        "2. Бот напишет в группе, что она подключена.\n\n"
        "<blockquote>Уже добавили бота? Отправьте в группе /start.</blockquote>"
    )
    return text, ui.kb(
        [ui.btn("Добавить в группу", "users", url=add_url, style="primary")],
        [ui.btn("« Профиль", callback_data="refresh_profile"), ui.home_btn()],
    )


@router.callback_query(F.data == "group_delivery")
async def cb_group_delivery(call: CallbackQuery, bot: Bot):
    await ui.show(call, *await group_screen(call.from_user.id, bot))


@router.callback_query(F.data == "group_off")
async def cb_group_off(call: CallbackQuery, bot: Bot):
    await database.set_mirror_chat(call.from_user.id, None)
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Группа для лотов", "отключена", status="OK")
    await call.answer("Группа отключена")
    await ui.show(call, *await group_screen(call.from_user.id, bot))
