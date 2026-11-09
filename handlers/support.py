"""Support tickets.

A user writes → the message joins their live ticket (or starts one) → every admin gets it with buttons.
An admin answers with a button or a quote-reply; the user answers the same way. Either side closes the ticket;
an answered ticket closes itself after SUPPORT_AUTOCLOSE_HOURS of the user's silence. Nothing is ever deleted:
the card shows the whole conversation page by page.

Callbacks: tk:<id>:<page> card (page -1 = last), tk_my:<page>, tk_write:<id>, tk_close:<id> → tk_close_y:<id>,
tk_reopen:<id>, tk_full:<id>:<page>, tk_files:<id>:<page>; admin: admin_tks, admin_tkl:<status>:<page>,
admin_tku:<user>:<page>, admin_tkr:<id>, admin_tkc:<id> → admin_tkc_y:<id>, admin_tkx:<id>; legacy support_reply:<user>.
"""
import html
import logging
import re
import time
from datetime import datetime
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.exceptions import TelegramForbiddenError

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
    waiting_for_admin_close = State()


# Telegram counts 1024 characters of a caption and 4096 of a message, and the header takes its share.
TICKET_CAPTION_TEXT = 700
TICKET_MESSAGE_TEXT = 3500
TICKET_MEDIA_FIELDS = ("document", "voice", "video")
PAGE_MESSAGES = 5          # messages on one page of the ticket card
SNIPPET = 300              # a longer message is cut on the card; «Показать полностью» sends it whole
USER_LIST_PAGE = 6
ADMIN_LIST_PAGE = 8
TOPICS = {"general": "Вопрос", "payment": "Оплата"}
MEDIA_TITLES = {"photo": "фото", "document": "файл", "voice": "голосовое", "video": "видео"}
USER_STATUS = {"open": "ждёт ответа", "answered": "есть ответ", "closed": "закрыто"}
ADMIN_STATUS = {"open": "ждёт ответа", "answered": "отвечено, ждём пользователя", "closed": "закрыто"}
STATUS_ICONS = {"open": "wait", "answered": "support", "closed": "ok"}
ADMIN_LISTS = {"open": "Ждут ответа", "answered": "Отвечены", "closed": "Закрытые"}
# Admin notices made before tickets carried the user id in the text; a quote-reply to them still works.
LEGACY_ID_RE = re.compile(r"🆔\s*ID:\s*(\d+)")
HOURS_TEXT = "Поддержка работает с 09:00 до 23:00 (МСК)."


# ── Formatting ──────────────────────────────────────────────────────────

def _when(epoch, with_year: bool = False) -> str:
    if not epoch:
        return "—"
    return datetime.fromtimestamp(float(epoch), database.MSK_TZ).strftime("%d.%m.%Y %H:%M" if with_year else "%d.%m %H:%M")


def _ago(epoch) -> str:
    seconds = max(0.0, time.time() - float(epoch or 0))
    if seconds < 3600:
        return f"{int(seconds // 60)} мин"
    if seconds < 86400:
        return f"{int(seconds // 3600)} ч"
    return f"{int(seconds // 86400)} дн"


def ticket_title(ticket: dict) -> str:
    return f"#{ticket['id']} · {TOPICS.get(ticket['topic'], 'Вопрос')}"


def _person(ticket: dict) -> str:
    name = html.escape(ticket.get("full_name") or "Пользователь")
    tag = f" (@{html.escape(ticket['username'])})" if ticket.get("username") else ""
    gone = " · аккаунт удалён" if ticket.get("user_deleted_epoch") else ""
    return f"{name}{tag} · <code>{ticket['user_id']}</code>{gone}"


def ticket_media(message: Message) -> tuple[str, str]:
    if message.photo:
        return "photo", message.photo[-1].file_id
    for kind in TICKET_MEDIA_FIELDS:
        media = getattr(message, kind)
        if media:
            return kind, media.file_id
    return "", ""


def _message_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


def _author(row: dict, ticket: dict, as_admin: bool) -> str:
    if row["author"] == "user":
        return html.escape(ticket.get("full_name") or "Пользователь") if as_admin else "Вы"
    if as_admin and row.get("author_name"):
        return f"Поддержка · {html.escape(row['author_name'])}"
    return "Поддержка"


def page_slice(messages: list, page: int) -> tuple[list, int, int]:
    """PAGE_MESSAGES real messages per page. A system line («Закрыто…», «Открыто снова…») rides with the message
    before it, so a page never holds only such lines and the last page always shows the conversation."""
    groups: list[list] = []
    for row in messages:
        if row["author"] == "system" and groups:
            groups[-1].append(row)
        elif groups and all(r["author"] == "system" for r in groups[-1]):
            groups[-1].append(row)  # leading lines join the first real message
        else:
            groups.append([row])
    pages = max(1, -(-len(groups) // PAGE_MESSAGES))
    if page < 0 or page >= pages:
        page = pages - 1
    return [row for group in groups[page * PAGE_MESSAGES:(page + 1) * PAGE_MESSAGES] for row in group], page, pages


def _message_block(row: dict, ticket: dict, as_admin: bool) -> str:
    if row["author"] == "system":
        return f"<i>{html.escape(row['text'])} · {_when(row['created_epoch'])}</i>"
    text = row["text"] or ""
    body = html.escape(text[:SNIPPET]) + ("…" if len(text) > SNIPPET else "")
    if row["media_type"]:
        media = f"<i>Вложение: {MEDIA_TITLES.get(row['media_type'], 'файл')}</i>"
        body = f"{media}\n{body}" if body else media
    pending = " · <i>не доставлено</i>" if row["author"] == "admin" and not row.get("delivered_epoch") else ""
    return f"<b>{_author(row, ticket, as_admin)}</b> · {_when(row['created_epoch'])}{pending}\n<blockquote>{body or '—'}</blockquote>"


def render_ticket(ticket: dict, messages: list, page: int, as_admin: bool):
    rows, page, pages = page_slice(messages, page)
    statuses = ADMIN_STATUS if as_admin else USER_STATUS
    head = [
        f"{ui.e('support')} <b>Обращение {ticket_title(ticket)}</b>",
        f"{statuses[ticket['status']].capitalize()} · создано {_when(ticket['created_epoch'])}"
        + (f" · стр. {page + 1}/{pages}" if pages > 1 else ""),
    ]
    if as_admin:
        head.append(_person(ticket))
    if ticket.get("parent_id"):
        head.append(f"Продолжение #{ticket['parent_id']}")
    text = "\n".join(head) + "\n\n" + "\n\n".join(_message_block(r, ticket, as_admin) for r in rows)

    tid, live = ticket["id"], ticket["status"] != "closed"
    buttons = []
    if as_admin:
        buttons.append([ui.btn("Ответить", "write", callback_data=f"admin_tkr:{tid}", style="primary")]
                       + ([ui.btn("Закрыть", "ok", callback_data=f"admin_tkc:{tid}")] if live else []))
    elif live:
        buttons.append([ui.btn("Ответить", "write", callback_data=f"tk_write:{tid}", style="primary"),
                        ui.btn("Вопрос решён", "ok", callback_data=f"tk_close:{tid}", style="success")])
    elif database.ticket_can_reopen(ticket):
        buttons.append([ui.btn("Открыть снова", "refresh", callback_data=f"tk_reopen:{tid}")])
    else:
        buttons.append([ui.btn("Новое обращение", "add", callback_data="support_start_ticket")])
    nav = []
    if page > 0:
        nav.append(ui.btn("«", callback_data=f"tk:{tid}:{page - 1}"))
    if page < pages - 1:
        nav.append(ui.btn("»", callback_data=f"tk:{tid}:{page + 1}"))
    buttons.append(nav)
    extras = []
    if any(r["author"] != "system" and len(r["text"] or "") > SNIPPET for r in rows):
        extras.append(ui.btn("Показать полностью", "views", callback_data=f"tk_full:{tid}:{page}"))
    if any(r["media_type"] for r in rows):
        extras.append(ui.btn("Вложения", "box", callback_data=f"tk_files:{tid}:{page}"))
    buttons.append(extras)
    if as_admin:
        buttons.append([ui.btn("Пользователь", "profile", callback_data=f"admin_user:{ticket['user_id']}:0"),
                        ui.btn("Выгрузить .txt", "docs", callback_data=f"admin_tkx:{tid}")])
        buttons.append([ui.btn("Поддержка", "support", callback_data="admin_tks")])
    else:
        buttons.append([ui.btn("Мои обращения", "note", callback_data="tk_my:0"), ui.home_btn()])
    return text, ui.kb(*buttons)


def export_ticket(ticket: dict, messages: list) -> str:
    lines = [
        f"Обращение #{ticket['id']} · {TOPICS.get(ticket['topic'], 'Вопрос')} · {ADMIN_STATUS[ticket['status']]}",
        f"Пользователь: {ticket.get('full_name') or ''} @{ticket.get('username') or '-'} id {ticket['user_id']}"
        + (" (аккаунт удалён)" if ticket.get("user_deleted_epoch") else ""),
        f"Создано: {_when(ticket['created_epoch'], True)} МСК",
        "",
    ]
    for row in messages:
        if row["author"] == "system":
            lines.append(f"[{_when(row['created_epoch'], True)}] — {row['text']}")
            continue
        who = "Пользователь" if row["author"] == "user" else f"Поддержка ({row.get('author_name') or row.get('admin_id')})"
        media = f" [вложение: {MEDIA_TITLES.get(row['media_type'], 'файл')}, file_id {row['file_id']}]" if row["media_type"] else ""
        lines.append(f"[{_when(row['created_epoch'], True)}] {who}:{media}\n{row['text']}")
        lines.append("")
    return "\n".join(lines)


# ── Sending ─────────────────────────────────────────────────────────────

async def _send(bot: Bot, chat_id: int, text: str, markup, media_type: str = "", file_id: str = ""):
    """One message, or a media message with the text as caption. Returns the sent messages."""
    if media_type:
        send = getattr(bot, f"send_{media_type}")
        return [await send(chat_id=chat_id, caption=text, reply_markup=markup, parse_mode="HTML", **{media_type: file_id})]
    return [await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode="HTML")]


def _refs(sent) -> list[tuple[int, int]]:
    out = []
    for message in sent:
        chat = getattr(message, "chat", None)
        if chat is not None and isinstance(getattr(message, "message_id", None), int):
            out.append((chat.id, message.message_id))
    return out


def _admin_notice_kb(ticket: dict):
    tid = ticket["id"]
    return ui.kb(
        [ui.btn("Ответить", "write", callback_data=f"admin_tkr:{tid}", style="primary"),
         ui.btn("История", "note", callback_data=f"tk:{tid}:-1")],
        [ui.btn("Закрыть", "ok", callback_data=f"admin_tkc:{tid}"),
         ui.btn("Пользователь", "profile", callback_data=f"admin_user:{ticket['user_id']}:0")],
    )


async def send_ticket_to_admin(bot: Bot, admin_id: int, item: dict) -> list:
    """item: the ticket's fields plus text / media_type / file_id / message_epoch / is_new of the new user message."""
    title = (f"Новое обращение {ticket_title(item)}" if item.get("is_new")
             else f"Обращение {ticket_title(item)} · новое сообщение")
    header = f"{ui.e('support')} <b>{title}</b>\n{_person(item)} · {_when(item['message_epoch'])}\n\n"
    text, kind = item["text"] or "", item["media_type"]
    limit = TICKET_CAPTION_TEXT if kind else TICKET_MESSAGE_TEXT
    body = header + (f"<blockquote>{html.escape(text[:limit])}</blockquote>" if text else "")
    sent = await _send(bot, admin_id, body, _admin_notice_kb(item), kind, item["file_id"])
    rest = text[limit:]
    while rest:
        sent += await _send(bot, admin_id, f"Продолжение {ticket_title(item)}:\n{html.escape(rest[:TICKET_MESSAGE_TEXT])}", None)
        rest = rest[TICKET_MESSAGE_TEXT:]
    return sent


async def deliver_ticket(bot: Bot, item: dict) -> bool:
    """One admin is enough. An undelivered message is retried by reports.reminder_worker."""
    refs, delivered = [], False
    for admin_id in config.ADMIN_IDS:
        try:
            refs += _refs(await send_ticket_to_admin(bot, admin_id, item))
            delivered = True
        except Exception as err:
            logger.error("Не удалось отправить обращение #%s администратору %s: %s", item["id"], admin_id, err)
    if delivered:
        await database.mark_ticket_message_delivered(item["message_id"])
        await database.save_ticket_refs(item["id"], refs)
    return delivered


async def retry_undelivered_tickets(bot: Bot) -> int:
    delivered = 0
    for item in await database.get_undelivered_ticket_messages():
        delivered += int(await deliver_ticket(bot, item))
    return delivered


async def tell_admins(bot: Bot, ticket: dict, text: str, except_id: Optional[int] = None) -> None:
    """A short note about a ticket to the other admins, so two of them do not answer the same person."""
    markup = ui.kb([ui.btn("История", "note", callback_data=f"tk:{ticket['id']}:-1")])
    for admin_id in config.ADMIN_IDS:
        if admin_id == except_id:
            continue
        try:
            sent = await bot.send_message(admin_id, text, reply_markup=markup, parse_mode="HTML")
            await database.save_ticket_refs(ticket["id"], _refs([sent]))
        except Exception as err:
            logger.warning("Не удалось уведомить администратора %s по обращению #%s: %s", admin_id, ticket["id"], err)


def _user_answer_kb(ticket: dict):
    tid = ticket["id"]
    return ui.kb(
        [ui.btn("Ответить", "write", callback_data=f"tk_write:{tid}", style="primary"),
         ui.btn("Вопрос решён", "ok", callback_data=f"tk_close:{tid}", style="success")],
        [ui.btn("Вся переписка", "note", callback_data=f"tk:{tid}:-1")],
    )


async def send_reply_to_user(bot: Bot, ticket: dict, message: Message, final: bool = False) -> tuple[bool, str, list]:
    """Support's message to the user from the bot. Returns (delivered, error, sent messages)."""
    text = _message_text(message)
    kind, file_id = ticket_media(message)
    title = f"Обращение {ticket_title(ticket)} закрыто поддержкой" if final else f"Ответ по обращению {ticket_title(ticket)}"
    limit = TICKET_CAPTION_TEXT if kind else TICKET_MESSAGE_TEXT
    body = f"{ui.e('support')} <b>{title}</b>\n\n{html.escape(text[:limit])}"
    markup = _closed_user_kb(ticket) if final else _user_answer_kb(ticket)
    try:
        sent = await _send(bot, ticket["user_id"], body, markup, kind, file_id)
        rest = text[limit:]
        while rest:
            sent += await _send(bot, ticket["user_id"], html.escape(rest[:TICKET_MESSAGE_TEXT]), None)
            rest = rest[TICKET_MESSAGE_TEXT:]
        return True, "", sent
    except TelegramForbiddenError:
        return False, "пользователь заблокировал бота", []
    except Exception as e:
        return False, str(e), []


def _closed_user_kb(ticket: dict):
    return ui.kb(
        [ui.btn("Открыть снова", "refresh", callback_data=f"tk_reopen:{ticket['id']}"),
         ui.btn("Новое обращение", "add", callback_data="support_start_ticket")],
        [ui.btn("Вся переписка", "note", callback_data=f"tk:{ticket['id']}:-1")],
    )


async def notify_user_closed(bot: Bot, ticket: dict, by_admin: bool) -> None:
    reason = ("закрыто поддержкой" if by_admin
              else f"закрыто: ответа не было {config.SUPPORT_AUTOCLOSE_HOURS // 24} дня")
    text = (f"{ui.e('ok')} <b>Обращение {ticket_title(ticket)} {reason}</b>\n\n"
            f"Если вопрос остался, откройте его снова в течение {config.SUPPORT_REOPEN_DAYS} дней.")
    try:
        sent = await bot.send_message(ticket["user_id"], text, reply_markup=_closed_user_kb(ticket), parse_mode="HTML")
        await database.save_ticket_refs(ticket["id"], _refs([sent]))
    except Exception as err:
        logger.info("Пользователь %s не получил закрытие обращения #%s: %s", ticket["user_id"], ticket["id"], err)


async def close_stale_tickets(bot: Bot) -> int:
    closed = await database.close_stale_tickets()
    for ticket in closed:
        await notify_user_closed(bot, ticket, by_admin=False)
    return len(closed)


# ── User: support screen and writing ────────────────────────────────────

async def support_screen(user_id: int):
    live = await database.get_live_ticket(user_id)
    _rows, total = await database.list_user_tickets(user_id, 0, 1)
    text = (
        f"{ui.e('support')} <b>Поддержка</b>\n\n"
        "Вопросы о работе бота, оплате и возвратах.\n"
        "• Ежедневно с 09:00 до 23:00 (МСК)\n"
        "• Возвраты рассматриваем в течение 24 часов\n"
        f"• {ui.e('mail')} {legal_docs.SUPPORT_EMAIL}\n\n"
    )
    if live:
        text += f"<blockquote>У вас открыто обращение {ticket_title(live)} — {USER_STATUS[live['status']]}.</blockquote>"
        first = [ui.btn(f"Дописать в #{live['id']}", "write", callback_data="support_start_ticket", style="primary"),
                 ui.btn(f"Открыть #{live['id']}", "note", callback_data=f"tk:{live['id']}:-1")]
    else:
        text += "Нажмите «Новое обращение» и опишите вопрос: оператор ответит в этом чате."
        first = [ui.btn("Новое обращение", "add", callback_data="support_start_ticket", style="primary")]
    return text, ui.kb(
        first,
        [ui.btn("Мои обращения", "note", callback_data="tk_my:0")] if total else [],
        [ui.btn("Документы", "docs", callback_data="open_docs_menu"),
         ui.btn("Тарифы", "tariffs", callback_data="open_subscription_menu")],
        [ui.home_btn()],
    )


def write_prompt(ticket: Optional[dict]) -> str:
    if ticket and ticket["status"] != "closed":
        return (f"{ui.e('write')} <b>Сообщение в обращение {ticket_title(ticket)}</b>\n\n"
                "Напишите одним сообщением, можно приложить скриншот, фото или голосовое. "
                "Оно добавится к переписке.")
    if ticket and database.ticket_can_reopen(ticket):
        return (f"{ui.e('write')} <b>Сообщение в обращение {ticket_title(ticket)}</b>\n\n"
                "Обращение закрыто: ваше сообщение откроет его снова.")
    return (
        f"{ui.e('write')} <b>Сообщение в поддержку</b>\n\n"
        "Опишите вопрос одним сообщением. Можно приложить скриншот, фото или голосовое.\n"
        "Оператор ответит прямо в этом чате.\n\n"
        f"{ui.e('mail')} Или на почту: {legal_docs.SUPPORT_EMAIL}"
    )


@router.callback_query(F.data == "support_start_ticket")
async def cb_support_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(SupportStates.waiting_for_user_message)
    live = await database.get_live_ticket(call.from_user.id)
    await ui.show(call, write_prompt(live), ui.kb([ui.btn("Отмена", "cancel", callback_data="support_cancel")]))


@router.callback_query(F.data.startswith("tk_write:"))
async def cb_ticket_write(call: CallbackQuery, state: FSMContext):
    ticket = await _ticket_for(call, int(call.data.split(":")[1]), owner_only=True)
    if ticket is None:
        return
    await state.clear()
    await state.set_state(SupportStates.waiting_for_user_message)
    await state.update_data(ticket_id=ticket["id"])
    await ui.show(call, write_prompt(ticket), ui.kb([ui.btn("Отмена", "cancel", callback_data="support_cancel")]))


@router.callback_query(F.data == "support_cancel")
async def cb_support_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.show(call, f"{ui.e('cancel')} <b>Сообщение не отправлено</b>", ui.kb([ui.home_btn()]))


async def accept_user_message(message: Message, bot: Bot, ticket_id: Optional[int] = None, is_payment: bool = False):
    user = message.from_user
    text = _message_text(message) or ("" if ticket_media(message)[0] else "(пустое сообщение)")
    media_type, file_id = ticket_media(message)
    ticket, message_id, is_new = await database.add_user_ticket_message(
        user.id, user.full_name, user.username or "", text, media_type, file_id, is_payment, ticket_id,
    )
    item = {**ticket, "message_id": message_id, "text": text, "media_type": media_type, "file_id": file_id,
            "message_epoch": ticket["last_user_epoch"], "is_new": is_new}
    if not await deliver_ticket(bot, item):
        logger.warning("Сообщение в обращение #%s пока не доставлено, повторю позже", ticket["id"])
    log_user_event(user_id=user.id, username=user.username, first_name=user.first_name,
                   action=f"Поддержка #{ticket['id']}: «{text[:80]}»", status="SUPPORT")
    title = f"Обращение {ticket_title(ticket)} создано" if is_new else f"Сообщение добавлено в обращение {ticket_title(ticket)}"
    await message.answer(
        f"{ui.e('ok')} <b>{title}</b>\n\nОтвет придёт в этот чат. {HOURS_TEXT}",
        reply_markup=ui.kb([ui.btn(f"Открыть #{ticket['id']}", "note", callback_data=f"tk:{ticket['id']}:-1"),
                            ui.home_btn()]),
        parse_mode="HTML",
    )
    return ticket


@router.message(SupportStates.waiting_for_user_message)
async def handle_user_support_message(message: Message, state: FSMContext, bot: Bot):
    # A bottom-menu button or a command is navigation, not the ticket text.
    from handlers.searches import route_menu_text
    if await route_menu_text(message, state):
        return
    data = await state.get_data()
    await state.clear()
    await accept_user_message(message, bot, data.get("ticket_id"), bool(data.get("payment_support")))


async def _user_ref(message: Message):
    """A user's quote-reply to a support message of their own ticket."""
    replied = message.reply_to_message
    if replied is None or message.chat.type != "private":
        return False
    ticket = await database.get_ticket_by_ref(message.chat.id, replied.message_id)
    if ticket is None or ticket["user_id"] != message.from_user.id or ticket.get("user_deleted_epoch"):
        return False
    return {"ref_ticket": ticket}


# ── Ticket card, list, close, reopen (user and admin) ───────────────────

async def _ticket_for(call: CallbackQuery, ticket_id: int, owner_only: bool = False) -> Optional[dict]:
    ticket = await database.get_ticket(ticket_id)
    uid = call.from_user.id
    allowed = ticket is not None and (
        (ticket["user_id"] == uid and not ticket.get("user_deleted_epoch"))
        or (not owner_only and config.is_admin(uid))
    )
    if not allowed:
        await call.answer("Обращение не найдено", show_alert=True)
        return None
    return ticket


def _as_admin(call: CallbackQuery, ticket: dict) -> bool:
    return config.is_admin(call.from_user.id) and ticket["user_id"] != call.from_user.id


async def show_ticket(event, ticket: dict, page: int = -1, as_admin: bool = False) -> None:
    messages = await database.get_ticket_messages(ticket["id"])
    text, markup = render_ticket(ticket, messages, page, as_admin)
    await ui.show(event, text, markup)


@router.callback_query(F.data.startswith("tk:"))
async def cb_ticket(call: CallbackQuery, state: FSMContext):
    _, tid, page = call.data.split(":")
    ticket = await _ticket_for(call, int(tid))
    if ticket:
        await state.clear()
        await show_ticket(call, ticket, int(page), _as_admin(call, ticket))


def ticket_row_label(ticket: dict, as_admin: bool) -> str:
    if as_admin:
        name = (ticket.get("full_name") or str(ticket["user_id"]))[:18]
        return f"#{ticket['id']} · {name} · {TOPICS.get(ticket['topic'], 'Вопрос')} · {_ago(ticket['updated_epoch'])}"
    return f"#{ticket['id']} · {TOPICS.get(ticket['topic'], 'Вопрос')} · {USER_STATUS[ticket['status']]} · {_when(ticket['created_epoch'])[:5]}"


def _list_kb(rows: list, page: int, pages: int, nav_prefix: str, as_admin: bool, back):
    buttons = [[ui.btn(ticket_row_label(t, as_admin), STATUS_ICONS[t["status"]], callback_data=f"tk:{t['id']}:-1")]
               for t in rows]
    nav = []
    if page > 0:
        nav.append(ui.btn("«", callback_data=f"{nav_prefix}:{page - 1}"))
    if page < pages - 1:
        nav.append(ui.btn("»", callback_data=f"{nav_prefix}:{page + 1}"))
    return ui.kb(*buttons, nav, back)


@router.callback_query(F.data.startswith("tk_my:"))
async def cb_my_tickets(call: CallbackQuery, state: FSMContext):
    await state.clear()
    page = max(0, int(call.data.split(":")[1]))
    rows, total = await database.list_user_tickets(call.from_user.id, page * USER_LIST_PAGE, USER_LIST_PAGE)
    pages = max(1, -(-total // USER_LIST_PAGE))
    text = (f"{ui.e('note')} <b>Мои обращения</b>" + (f" · стр. {page + 1}/{pages}" if pages > 1 else "") + "\n\n"
            + ("Выберите обращение, чтобы увидеть всю переписку." if total else "Обращений пока нет."))
    await ui.show(call, text, _list_kb(rows, page, pages, "tk_my", False,
                                       [ui.btn("Поддержка", "support", callback_data="open_support_stub"), ui.home_btn()]))


@router.callback_query(F.data.startswith("tk_close:"))
async def cb_ticket_close(call: CallbackQuery):
    ticket = await _ticket_for(call, int(call.data.split(":")[1]), owner_only=True)
    if ticket is None:
        return
    if ticket["status"] == "closed":
        await show_ticket(call, ticket)
        return
    await ui.show(call, f"{ui.e('ok')} <b>Закрыть обращение {ticket_title(ticket)}?</b>\n\n"
                        f"Переписка сохранится. Открыть снова можно в течение {config.SUPPORT_REOPEN_DAYS} дней.",
                  ui.kb([ui.btn("Да, закрыть", "ok", callback_data=f"tk_close_y:{ticket['id']}", style="success"),
                         ui.btn("Нет", "cancel", callback_data=f"tk:{ticket['id']}:-1")]))


@router.callback_query(F.data.startswith("tk_close_y:"))
async def cb_ticket_close_yes(call: CallbackQuery, bot: Bot):
    ticket = await _ticket_for(call, int(call.data.split(":")[1]), owner_only=True)
    if ticket is None:
        return
    closed = await database.close_ticket(ticket["id"], "user")
    if closed:
        await tell_admins(bot, closed, f"{ui.e('ok')} <b>{ticket_title(closed)}</b> закрыл пользователь\n{_person(closed)}")
    await show_ticket(call, closed or await database.get_ticket(ticket["id"]))


@router.callback_query(F.data.startswith("tk_reopen:"))
async def cb_ticket_reopen(call: CallbackQuery, bot: Bot):
    ticket = await _ticket_for(call, int(call.data.split(":")[1]), owner_only=True)
    if ticket is None:
        return
    reopened = await database.reopen_ticket(ticket["id"], call.from_user.id)
    if reopened is None:
        live = await database.get_live_ticket(call.from_user.id)
        await call.answer(f"Сначала закройте открытое обращение #{live['id']}" if live and live["id"] != ticket["id"]
                          else f"Прошло больше {config.SUPPORT_REOPEN_DAYS} дней: создайте новое обращение",
                          show_alert=True)
        return
    await tell_admins(bot, reopened, f"{ui.e('refresh')} <b>{ticket_title(reopened)}</b> пользователь открыл снова\n{_person(reopened)}")
    await show_ticket(call, reopened)


async def _page_rows(call: CallbackQuery):
    _, tid, page = call.data.split(":")
    ticket = await _ticket_for(call, int(tid))
    if ticket is None:
        return None, []
    rows, _page, _pages = page_slice(await database.get_ticket_messages(ticket["id"]), int(page))
    return ticket, rows


@router.callback_query(F.data.startswith("tk_full:"))
async def cb_ticket_full(call: CallbackQuery):
    ticket, rows = await _page_rows(call)
    if ticket is None:
        return
    await call.answer()
    as_admin = _as_admin(call, ticket)
    for row in rows:
        text = row["text"] or ""
        if row["author"] == "system" or len(text) <= SNIPPET:
            continue
        head = f"<b>#{ticket['id']} · {_author(row, ticket, as_admin)} · {_when(row['created_epoch'])}</b>\n\n"
        for start in range(0, len(text), TICKET_MESSAGE_TEXT):
            await call.message.answer(head + html.escape(text[start:start + TICKET_MESSAGE_TEXT]), parse_mode="HTML")
            head = ""


@router.callback_query(F.data.startswith("tk_files:"))
async def cb_ticket_files(call: CallbackQuery, bot: Bot):
    ticket, rows = await _page_rows(call)
    if ticket is None:
        return
    await call.answer()
    as_admin = _as_admin(call, ticket)
    for row in rows:
        if not row["media_type"]:
            continue
        caption = f"#{ticket['id']} · {_author(row, ticket, as_admin)} · {_when(row['created_epoch'])}"
        try:
            await _send(bot, call.from_user.id, caption, None, row["media_type"], row["file_id"])
        except Exception as err:
            await call.message.answer(f"Вложение от {_when(row['created_epoch'])} недоступно: {html.escape(str(err)[:200])}")


# ── Admin: support panel ────────────────────────────────────────────────

async def admin_panel_text() -> tuple[str, dict]:
    counts = await database.count_tickets()
    text = (
        f"{ui.e('support')} <b>Поддержка</b>\n\n"
        f"Ждут ответа: <b>{counts['open']}</b>\n"
        f"Отвечены, ждём пользователя: <b>{counts['answered']}</b>\n"
        f"Закрытые: <b>{counts['closed']}</b>\n\n"
        f"<blockquote>Отвеченное обращение закрывается само через {config.SUPPORT_AUTOCLOSE_HOURS} ч молчания "
        "пользователя. История не удаляется, даже если удалить пользователя.</blockquote>"
    )
    return text, counts


@router.callback_query(F.data == "admin_tks")
async def cb_admin_support(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    await state.clear()
    text, counts = await admin_panel_text()
    await ui.show(call, text, ui.kb(
        [ui.btn(f"Ждут ответа ({counts['open']})", "wait", callback_data="admin_tkl:open:0",
                style="primary" if counts["open"] else None)],
        [ui.btn(f"Отвечены ({counts['answered']})", "support", callback_data="admin_tkl:answered:0"),
         ui.btn(f"Закрытые ({counts['closed']})", "ok", callback_data="admin_tkl:closed:0")],
        [ui.btn("Админка", "admin", callback_data="admin_refresh")],
    ))


@router.callback_query(F.data.startswith("admin_tkl:"))
async def cb_admin_ticket_list(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    _, status, page_raw = call.data.split(":")
    if status not in ADMIN_LISTS:
        return
    page = max(0, int(page_raw))
    rows, total = await database.list_tickets(status, page * ADMIN_LIST_PAGE, ADMIN_LIST_PAGE)
    pages = max(1, -(-total // ADMIN_LIST_PAGE))
    hint = "Сверху те, кто ждёт дольше всех." if status == "open" else "Сверху последние изменения."
    text = (f"{ui.e(STATUS_ICONS[status])} <b>{ADMIN_LISTS[status]}</b> · {total}"
            + (f" · стр. {page + 1}/{pages}" if pages > 1 else "") + f"\n\n{hint if total else 'Пусто.'}")
    await ui.show(call, text, _list_kb(rows, page, pages, f"admin_tkl:{status}", True,
                                       [ui.btn("Поддержка", "support", callback_data="admin_tks")]))


@router.callback_query(F.data.startswith("admin_tku:"))
async def cb_admin_user_tickets(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    _, uid_raw, page_raw = call.data.split(":")
    uid, page = int(uid_raw), max(0, int(page_raw))
    rows, total = await database.list_user_tickets(uid, page * ADMIN_LIST_PAGE, ADMIN_LIST_PAGE, include_deleted=True)
    pages = max(1, -(-total // ADMIN_LIST_PAGE))
    text = (f"{ui.e('support')} <b>Обращения пользователя</b> <code>{uid}</code> · {total}"
            + (f" · стр. {page + 1}/{pages}" if pages > 1 else "") + ("" if total else "\n\nОбращений нет."))
    await ui.show(call, text, _list_kb(rows, page, pages, f"admin_tku:{uid}", True, [
        ui.btn("Написать", "write", callback_data=f"support_reply:{uid}"),
        ui.btn("Карточка", "profile", callback_data=f"admin_user:{uid}:0"),
    ]))


@router.callback_query(F.data.startswith("admin_tkx:"))
async def cb_admin_ticket_export(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    ticket = await database.get_ticket(int(call.data.split(":")[1]))
    if ticket is None:
        await call.answer("Обращение не найдено", show_alert=True)
        return
    await call.answer()
    body = export_ticket(ticket, await database.get_ticket_messages(ticket["id"]))
    await call.message.answer_document(BufferedInputFile(body.encode("utf-8"), filename=f"ticket_{ticket['id']}.txt"),
                                       caption=f"Обращение {ticket_title(ticket)}")


# ── Admin: answering ────────────────────────────────────────────────────

def _cancel_kb(back: str):
    return ui.kb([ui.btn("Отмена", "cancel", callback_data=back)])


@router.callback_query(F.data.startswith("admin_tkr:"))
async def cb_admin_ticket_reply(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    ticket = await database.get_ticket(int(call.data.split(":")[1]))
    if ticket is None:
        await call.answer("Обращение не найдено", show_alert=True)
        return
    await state.set_state(SupportStates.waiting_for_admin_reply)
    await state.update_data(ticket_id=ticket["id"], target_user_id=ticket["user_id"])
    reopen = "\n\nОбращение закрыто: ответ откроет его снова." if ticket["status"] == "closed" else ""
    await call.message.answer(
        f"{ui.e('write')} <b>Ответ по обращению {ticket_title(ticket)}</b>\n{_person(ticket)}\n\n"
        f"Напишите ответ, можно с фото, файлом или голосовым.{reopen}",
        reply_markup=_cancel_kb("admin_reply_cancel"), parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("support_reply:"))
async def cb_admin_reply_start(call: CallbackQuery, state: FSMContext):
    """«Написать пользователю» from the user card and alerts: lands in the user's live ticket or starts one."""
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    target_user_id = int(call.data.split(":")[1])
    live = await database.get_live_ticket(target_user_id)
    user = await database.get_user(target_user_id) or {}
    await state.set_state(SupportStates.waiting_for_admin_reply)
    await state.update_data(ticket_id=live["id"] if live else None, target_user_id=target_user_id)
    name = html.escape(user.get("first_name") or "Пользователь")
    where = f"в обращение {ticket_title(live)}" if live else "новым обращением от поддержки"
    await call.message.answer(
        f"{ui.e('write')} <b>Сообщение пользователю</b> {name} · <code>{target_user_id}</code>\n\n"
        f"Уйдёт {where}. Напишите текст, можно с фото, файлом или голосовым.",
        reply_markup=_cancel_kb("admin_reply_cancel"), parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "admin_reply_cancel")
async def cb_admin_reply_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.show(call, f"{ui.e('cancel')} <b>Ответ не отправлен</b>")


async def answer_ticket(bot: Bot, admin_message: Message, ticket: dict, final: bool = False) -> tuple[bool, dict]:
    """Delivers support's message, stores it (delivered or not) and tells the other admins."""
    admin = admin_message.from_user
    delivered, error, sent = await send_reply_to_user(bot, ticket, admin_message, final)
    kind, file_id = ticket_media(admin_message)
    updated = await database.add_admin_ticket_message(
        ticket["id"], admin.id, admin.first_name or str(admin.id), _message_text(admin_message), kind, file_id, delivered,
    ) or ticket
    await database.save_ticket_refs(ticket["id"], _refs(sent))
    if delivered:
        logger.info("Администратор %s ответил по обращению #%s", admin.id, ticket["id"])
        if not final:
            await tell_admins(bot, updated, f"{ui.e('write')} <b>{ticket_title(updated)}</b> — ответил "
                                            f"{html.escape(admin.first_name or str(admin.id))}", except_id=admin.id)
        confirm = f"{ui.e('ok')} <b>Ответ по {ticket_title(updated)} доставлен</b>"
    else:
        confirm = (f"{ui.e('warn')} <b>Не доставлено:</b> {html.escape(error)}\n"
                   f"Сообщение сохранено в истории {ticket_title(updated)}.")
    reply = await admin_message.answer(confirm, parse_mode="HTML", reply_markup=ui.kb(
        [ui.btn("История", "note", callback_data=f"tk:{updated['id']}:-1")]))
    await database.save_ticket_refs(updated["id"], _refs([reply]))
    return delivered, updated


@router.message(SupportStates.waiting_for_admin_reply)
async def handle_admin_reply_message(message: Message, state: FSMContext, bot: Bot):
    if not config.is_admin(message.from_user.id):
        return
    if message.text and message.text.strip().lower() in {"отмена", "❌ отмена", "/cancel"}:
        await state.clear()
        await message.answer(f"{ui.e('cancel')} <b>Ответ не отправлен</b>", parse_mode="HTML")
        return
    data = await state.get_data()
    await state.clear()
    ticket = await database.get_ticket(data["ticket_id"]) if data.get("ticket_id") else None
    if ticket is None:
        if not data.get("target_user_id"):
            await message.answer(f"{ui.e('warn')} Адресат не найден.", parse_mode="HTML")
            return
        ticket = await database.ticket_for_admin_message(int(data["target_user_id"]))
    await answer_ticket(bot, message, ticket)


async def _admin_ref(message: Message):
    """An admin's quote-reply to a ticket notice (or to an old pre-ticket notice with «🆔 ID:»)."""
    replied = message.reply_to_message
    if replied is None or message.from_user is None or not config.is_admin(message.from_user.id):
        return False
    ticket = await database.get_ticket_by_ref(message.chat.id, replied.message_id)
    if ticket is not None:
        return {"ref_ticket": ticket}
    match = LEGACY_ID_RE.search(replied.text or replied.caption or "")
    if match and int(match.group(1)) != message.from_user.id:
        return {"ref_ticket": await database.ticket_for_admin_message(int(match.group(1)))}
    return False


@router.message(StateFilter(None), F.reply_to_message, _admin_ref)
async def handle_admin_quote_reply(message: Message, bot: Bot, ref_ticket: dict):
    await answer_ticket(bot, message, ref_ticket)


@router.message(StateFilter(None), F.reply_to_message, _user_ref)
async def handle_user_quote_reply(message: Message, bot: Bot, ref_ticket: dict):
    await accept_user_message(message, bot, ref_ticket["id"])


# ── Admin: closing ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_tkc:"))
async def cb_admin_ticket_close(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    ticket = await database.get_ticket(int(call.data.split(":")[1]))
    if ticket is None or ticket["status"] == "closed":
        await call.answer("Обращение уже закрыто", show_alert=True)
        return
    await state.set_state(SupportStates.waiting_for_admin_close)
    await state.update_data(ticket_id=ticket["id"])
    await call.message.answer(
        f"{ui.e('ok')} <b>Закрыть обращение {ticket_title(ticket)}?</b>\n{_person(ticket)}\n\n"
        "Можно отправить пользователю последнее сообщение: напишите его. Или закройте без сообщения.",
        reply_markup=ui.kb(
            [ui.btn("Закрыть без сообщения", "ok", callback_data=f"admin_tkc_y:{ticket['id']}", style="success")],
            [ui.btn("Отмена", "cancel", callback_data="admin_reply_cancel")],
        ),
        parse_mode="HTML",
    )
    await call.answer()


async def _admin_close(bot: Bot, ticket_id: int, admin) -> Optional[dict]:
    name = admin.first_name or str(admin.id)
    closed = await database.close_ticket(ticket_id, "admin", admin.id, name)
    if closed:
        await tell_admins(bot, closed, f"{ui.e('ok')} <b>{ticket_title(closed)}</b> закрыл {html.escape(name)}",
                          except_id=admin.id)
    return closed


@router.callback_query(F.data.startswith("admin_tkc_y:"))
async def cb_admin_ticket_close_yes(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not config.is_admin(call.from_user.id):
        return
    await state.clear()
    tid = int(call.data.split(":")[1])
    closed = await _admin_close(bot, tid, call.from_user)
    if closed:
        await notify_user_closed(bot, closed, by_admin=True)
    ticket = closed or await database.get_ticket(tid)
    if ticket:
        await show_ticket(call, ticket, as_admin=True)


@router.message(SupportStates.waiting_for_admin_close)
async def handle_admin_close_message(message: Message, state: FSMContext, bot: Bot):
    if not config.is_admin(message.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    ticket = await database.get_ticket(int(data.get("ticket_id") or 0))
    if ticket is None:
        return
    # The last word goes out with «закрыто» in its title and the reopen buttons, then the ticket closes.
    await answer_ticket(bot, message, ticket, final=True)
    await _admin_close(bot, ticket["id"], message.from_user)
