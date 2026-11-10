"""Copies of lots in a group chat (Средний and Полный): linking from the group and the profile screen."""

import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

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
            f"{ui.e('lock')} Копии лотов в группу или канал есть на тарифах «Средний» и «Полный». "
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


@router.my_chat_member(F.chat.type == ChatType.CHANNEL)
async def link_channel(update: ChatMemberUpdated, bot: Bot):
    """Nobody can send /start in a channel: the admin who made the bot an admin there owns the link."""
    user = update.from_user
    if not user or user.is_bot:
        return
    status = update.new_chat_member.status
    if status != ChatMemberStatus.ADMINISTRATOR:
        if status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and await database.get_mirror_chat(user.id) == update.chat.id:
            await database.set_mirror_chat(user.id, None)
        return
    title = html.escape(update.chat.title or "")
    if not await group_delivery_allowed(user.id):
        text = (f"{ui.e('lock')} Канал «{title}»: копии лотов в группу или канал есть на тарифах «Средний» и «Полный».")
    elif not getattr(update.new_chat_member, "can_post_messages", False):
        text = (f"{ui.e('warn')} Канал «{title}»: дайте боту право публиковать сообщения, "
                "иначе он не сможет присылать туда лоты.")
    else:
        await database.set_mirror_chat(user.id, update.chat.id)
        log_user_event(user.id, user.username, user.first_name, "Канал для лотов", f"chat {update.chat.id}", status="OK")
        text = f"{ui.e('ok')} Канал «{title}» подключён: копии новых лотов будут приходить и туда."
    try:
        await bot.send_message(user.id, text)
    except Exception:
        pass


@router.message(F.chat.type.in_(GROUP_TYPES))
async def ignore_group_messages(message: Message):
    """Everything else in groups is ignored: menus and radars live in the private chat."""
    return


async def group_screen(user_id: int, bot: Bot) -> tuple[str, object]:
    me = await bot.me()
    add_url = f"https://t.me/{me.username}?startgroup={LINK_PAYLOAD}"
    channel_url = f"https://t.me/{me.username}?startchannel&admin=post_messages"
    header = f"{ui.e('users')} <b>Лоты в группу или канал</b>\n\n"
    if not await group_delivery_allowed(user_id):
        text = header + (
            "Бот присылает копию каждого лота в вашу группу или канал: удобно работать вдвоём или с командой.\n\n"
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
            f"Подключено: <b>{html.escape(title) or chat_id}</b>. Копия каждого лота приходит туда.\n\n"
            "<blockquote>Там только ссылки на объявление; скрыть продавца и настройки — здесь, в личном чате. "
            "Если лотов очень много, Telegram ограничивает группу или канал примерно 20 сообщениями в минуту, "
            "и часть копий пропадёт.</blockquote>"
        )
        return text, ui.kb(
            [ui.btn("Отключить", "cancel", callback_data="group_off", style="danger")],
            [ui.btn("Другая группа", "users", url=add_url), ui.btn("Другой канал", "megaphone", url=channel_url)],
            [ui.btn("« Профиль", callback_data="refresh_profile"), ui.home_btn()],
        )
    text = header + (
        "Бот будет присылать копию каждого лота в вашу группу или канал: удобно работать вдвоём или с командой.\n\n"
        "<b>Группа:</b> нажмите «Добавить в группу» и выберите её. Бот напишет там, что она подключена.\n"
        "<b>Канал:</b> нажмите «Добавить в канал», выберите его и разрешите публиковать сообщения. "
        "Бот напишет здесь, что канал подключён.\n\n"
        "<blockquote>Бот уже в группе? Отправьте там /start. Работает одна группа или один канал.</blockquote>"
    )
    return text, ui.kb(
        [ui.btn("Добавить в группу", "users", url=add_url, style="primary")],
        [ui.btn("Добавить в канал", "megaphone", url=channel_url, style="primary")],
        [ui.btn("« Профиль", callback_data="refresh_profile"), ui.home_btn()],
    )


@router.callback_query(F.data == "group_delivery")
async def cb_group_delivery(call: CallbackQuery, bot: Bot):
    await ui.show(call, *await group_screen(call.from_user.id, bot))


@router.callback_query(F.data == "group_off")
async def cb_group_off_prompt(call: CallbackQuery):
    """The button only asks; the group is unlinked by «Да, отключить» (group_off_yes)."""
    await ui.show(call, (
        f"{ui.e('warn')} <b>Точно отключить группу или канал?</b>\n\n"
        "Копии новых лотов перестанут туда приходить. Подключить снова можно в любой момент."
    ), ui.kb(
        [ui.btn("Да, отключить", "cancel", callback_data="group_off_yes", style="danger")],
        [ui.btn("Отмена", "cancel", callback_data="group_delivery")],
    ))


@router.callback_query(F.data == "group_off_yes")
async def cb_group_off(call: CallbackQuery, bot: Bot):
    await database.set_mirror_chat(call.from_user.id, None)
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Группа для лотов", "отключена", status="OK")
    await call.answer("Группа отключена")
    await ui.show(call, *await group_screen(call.from_user.id, bot))
