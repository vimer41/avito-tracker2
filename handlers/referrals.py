"""Referral program screens: the owner's page in the profile and the admin section (issue, term, percent,
switch off, payouts, star rate). Logic and money rules live in referrals.py."""
import html
import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import config
import database
import referrals
import ui

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 8
TERMS = ((7, "7 дней"), (30, "30 дней"), (90, "90 дней"), (365, "Год"), (0, "Бессрочно"))
PERCENTS = (5, 10, 15, 20, 25, 30)


class RefAdminStates(StatesGroup):
    waiting_for_payout = State()
    waiting_for_rate = State()


def _contact_url() -> str:
    return "https://t.me/" + config.REF_PAYOUT_CONTACT.lstrip("@")


async def _url(bot, code: str) -> str:
    me = await bot.me()
    return referrals.link_url(me.username, code)


def _money_lines(s: dict) -> str:
    lines = [
        f"<b>Пришло по ссылке:</b> {s['came']} · оплатили: {s['buyers']}",
        f"<b>Начислено всего:</b> {referrals.fmt_rub(s['earned'])}",
        f"<b>Ожидает ({config.REF_HOLD_DAYS} дн. после оплаты):</b> {referrals.fmt_rub(s['pending'])}",
    ]
    if s["frozen"]:
        lines.append(f"<b>Заморожено (спор по оплате):</b> {referrals.fmt_rub(s['frozen'])}")
    lines.append(f"<b>Выплачено:</b> {referrals.fmt_rub(s['paid_out'])}")
    lines.append(f"<b>Доступно к выводу:</b> {referrals.fmt_rub(max(s['balance'], 0))}")
    if s["balance"] < 0:
        lines.append(f"<i>Долг после возврата оплаты: {referrals.fmt_rub(-s['balance'])}, покроется следующими начислениями.</i>")
    return "\n".join(lines)


# ---------- owner ----------

@router.callback_query(F.data == "ref_me")
async def cb_ref_me(call: CallbackQuery):
    s = await referrals.summary(call.from_user.id)
    link = s["link"]
    if not link:
        await call.answer("У вас нет партнёрской ссылки.", show_alert=True)
        return
    active = referrals.is_active(link)
    text = (
        f"{ui.e('users')} <b>Партнёрская программа</b>\n\n"
        f"<b>Ваша ссылка:</b>\n<code>{await _url(call.bot, link['code'])}</code>\n"
        f"<b>Статус:</b> {referrals.link_status(link)}\n"
        f"<b>Вознаграждение:</b> {link['percent']} % с каждой оплаты подписки\n\n"
        + _money_lines(s)
    )
    if s["recent"]:
        text += "\n\n<b>Последние начисления:</b>\n" + "\n".join(referrals.reward_line(r) for r in s["recent"][:5])
    hint = (f"Для вывода напишите {html.escape(config.REF_PAYOUT_CONTACT)}. "
            f"Начисление можно вывести через {config.REF_HOLD_DAYS} дней после оплаты; "
            "если оплату вернут, начисление отменяется.")
    if not active:
        hint = ("Ссылка не действует: за новые оплаты ничего не начисляется. "
                f"Чтобы продлить, напишите {html.escape(config.REF_PAYOUT_CONTACT)}.\n" + hint)
    text += f"\n\n<blockquote>{hint}</blockquote>"
    markup = ui.kb(
        [ui.btn("Написать для вывода", "write", url=_contact_url())],
        [ui.btn("Обновить", "refresh", callback_data="ref_me"),
         ui.btn("« Профиль", callback_data="refresh_profile")],
    )
    await ui.show(call, text, markup)


# ---------- admin ----------

async def _link_card(bot, link_id: int):
    link = await referrals.get_link(link_id)
    if not link:
        return "Ссылка не найдена.", ui.kb([ui.btn("« Все ссылки", callback_data="ref_adm:0")])
    owner = await database.get_user(link["owner_id"]) or {"owner_id": link["owner_id"]}
    owner.setdefault("owner_id", link["owner_id"])
    s = await referrals.summary(link["owner_id"])
    text = (
        f"{ui.e('link')} <b>Реферальная ссылка #{link['id']}</b>\n\n"
        f"<b>Владелец:</b> {referrals.owner_name(owner)} · <code>{link['owner_id']}</code>\n"
        f"<b>Ссылка:</b> <code>{await _url(bot, link['code'])}</code>\n"
        f"<b>Статус:</b> {referrals.link_status(link)}\n"
        f"<b>Процент:</b> {link['percent']} %\n"
        f"<b>Выдана:</b> {referrals.format_date(link['created_at'])}, админ <code>{link['created_by']}</code>\n\n"
        + _money_lines(s)
    )
    if s["balance"] > 0:
        text += f"\n\n<b>К выплате сейчас: {referrals.fmt_rub(s['balance'])}</b>"
    active = referrals.is_active(link)
    rows = [
        [ui.btn("Продлить" if active else "Включить снова", "time", callback_data=f"ref_adm_ext:{link['id']}",
                style=None if active else "success"),
         ui.btn("Процент", "price", callback_data=f"ref_adm_pct:{link['id']}")],
        [ui.btn("Отметить выплату", "card", callback_data=f"ref_adm_pay:{link['id']}"),
         ui.btn("Журнал", "note", callback_data=f"ref_adm_log:{link['id']}")],
    ]
    if active:
        rows.append([ui.btn("Выключить ссылку", "cancel", callback_data=f"ref_adm_off:{link['id']}", style="danger")])
    rows.append([ui.btn("Пользователь", "profile", callback_data=f"admin_user:{link['owner_id']}:0"),
                 ui.btn("« Все ссылки", callback_data="ref_adm:0")])
    return text, ui.kb(*rows)


async def _show_card(event, link_id: int):
    text, markup = await _link_card(event.bot, link_id)
    await ui.show(event, text, markup)


def _term_rows(prefix: str):
    buttons = [ui.btn(title, callback_data=f"{prefix}:{days}") for days, title in TERMS]
    return [buttons[:3], buttons[3:]]


@router.callback_query(F.data.startswith("ref_adm:"))
async def cb_ref_list(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    page = max(0, int(call.data.split(":")[1] or 0))
    links, total = await referrals.list_links(page * PAGE_SIZE, PAGE_SIZE)
    rate = await referrals.star_rate()
    text = (
        f"{ui.e('link')} <b>Реферальные ссылки</b>\n\n"
        f"Всего: <b>{total}</b>. По умолчанию {config.REF_PERCENT} %, ожидание {config.REF_HOLD_DAYS} дней.\n"
        f"Курс звезды: <b>{rate:g} ₽</b>.\n"
        "<blockquote>Выдать ссылку: карточка пользователя → «Реферальная ссылка».</blockquote>"
    )
    rows = []
    for link in links:
        mark = "🟢" if referrals.is_active(link) else "⚪️"
        rows.append([ui.btn(f"{mark} {referrals.owner_name(link)} · {link['percent']}% · пришло {link['came']}",
                            callback_data=f"ref_adm_link:{link['id']}")])
    total_pages = max(1, -(-total // PAGE_SIZE))
    rows.append(ui.page_nav(page, total_pages, "ref_adm:{page}"))
    rows.append([ui.btn("Курс звезды", "star", callback_data="ref_adm_rate"),
                 ui.btn("« Админ-панель", callback_data="admin_refresh")])
    await ui.show(call, text, ui.kb(*rows))


@router.callback_query(F.data.startswith("ref_adm_user:"))
async def cb_ref_user(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    target = int(call.data.split(":")[1])
    link = await referrals.get_owner_link(target)
    if link:
        await _show_card(call, link["id"])
        return
    if config.is_admin(target):
        await call.answer("Администратору ссылка не нужна.", show_alert=True)
        return
    text = (
        f"{ui.e('link')} <b>Выдать реферальную ссылку</b>\n\n"
        f"Пользователь <code>{target}</code>. Процент {config.REF_PERCENT} % (можно сменить после выдачи).\n"
        "На какой срок?"
    )
    await ui.show(call, text, ui.kb(*_term_rows(f"ref_adm_new:{target}"),
                                    [ui.btn("Отмена", "cancel", callback_data=f"admin_user:{target}:0")]))


@router.callback_query(F.data.startswith("ref_adm_new:"))
async def cb_ref_new(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    _, target, days = call.data.split(":")
    try:
        link = await referrals.create_link(int(target), call.from_user.id, int(days) or None)
    except referrals.ReferralError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await database.log_admin_action(call.from_user.id, int(target), "ref_issue",
                                    f"#{link['id']} {referrals.link_status(link)}")
    await call.answer("Ссылка выдана")
    await _show_card(call, link["id"])
    try:
        await call.bot.send_message(int(target), (
            f"{ui.e('users')} <b>Вам открыта партнёрская программа</b>\n\n"
            f"С каждой оплаты подписки приглашённым вами пользователем вы получаете {link['percent']} %.\n"
            f"Ваша ссылка: <code>{await _url(call.bot, link['code'])}</code>\n"
            f"Статус: {referrals.link_status(link)}"
        ), parse_mode="HTML", reply_markup=ui.kb([ui.btn("Партнёрская программа", "users", callback_data="ref_me")]))
    except Exception as exc:
        logger.info("Referral issue notice failed: user=%s error=%s", target, type(exc).__name__)


@router.callback_query(F.data.startswith("ref_adm_link:"))
async def cb_ref_link(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    await _show_card(call, int(call.data.split(":")[1]))


@router.callback_query(F.data.startswith("ref_adm_ext:"))
async def cb_ref_extend(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    parts = call.data.split(":")
    link_id = int(parts[1])
    if len(parts) == 2:
        link = await referrals.get_link(link_id)
        if not link:
            await call.answer("Ссылка не найдена.", show_alert=True)
            return
        active = referrals.is_active(link)
        text = (
            f"{ui.e('time')} <b>{'Продлить' if active else 'Включить снова'} ссылку #{link_id}</b>\n\n"
            f"Сейчас: {referrals.link_status(link)}.\n"
            + ("Срок добавится к текущему." if active and link.get("expires_at") else
               "Срок считается с сегодняшнего дня. Код ссылки и приведённые пользователи остаются прежними.")
        )
        await ui.show(call, text, ui.kb(*_term_rows(f"ref_adm_ext:{link_id}"),
                                        [ui.btn("Отмена", "cancel", callback_data=f"ref_adm_link:{link_id}")]))
        return
    try:
        link = await referrals.extend_link(link_id, int(parts[2]) or None)
    except referrals.ReferralError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await database.log_admin_action(call.from_user.id, link["owner_id"], "ref_extend",
                                    f"#{link_id} {referrals.link_status(link)}")
    await call.answer("Готово")
    await _show_card(call, link_id)


@router.callback_query(F.data.startswith("ref_adm_pct:"))
async def cb_ref_percent(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    parts = call.data.split(":")
    link_id = int(parts[1])
    if len(parts) == 2:
        buttons = [ui.btn(f"{p} %", callback_data=f"ref_adm_pct:{link_id}:{p}") for p in PERCENTS]
        await ui.show(call, f"{ui.e('price')} <b>Процент для ссылки #{link_id}</b>\n\n"
                            "Новый процент действует для следующих оплат, прошлые начисления не меняются.",
                      ui.kb(buttons[:3], buttons[3:], [ui.btn("Отмена", "cancel", callback_data=f"ref_adm_link:{link_id}")]))
        return
    link = await referrals.set_percent(link_id, int(parts[2]))
    await database.log_admin_action(call.from_user.id, link["owner_id"], "ref_pct", f"#{link_id} {link['percent']}%")
    await call.answer("Готово")
    await _show_card(call, link_id)


@router.callback_query(F.data.startswith("ref_adm_off:"))
async def cb_ref_off(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    link_id = int(call.data.split(":")[1])
    await ui.show(call, (
        f"{ui.e('warn')} <b>Выключить ссылку #{link_id}?</b>\n\n"
        "Новые пользователи по ней не привяжутся, за новые оплаты ничего не начислится. "
        "Уже начисленное остаётся. Включить снова можно в карточке ссылки."
    ), ui.kb([ui.btn("Выключить", "cancel", callback_data=f"ref_adm_offy:{link_id}", style="danger"),
              ui.btn("Отмена", callback_data=f"ref_adm_link:{link_id}")]))


@router.callback_query(F.data.startswith("ref_adm_offy:"))
async def cb_ref_off_yes(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    link_id = int(call.data.split(":")[1])
    link = await referrals.revoke_link(link_id, call.from_user.id)
    if link:
        await database.log_admin_action(call.from_user.id, link["owner_id"], "ref_off", f"#{link_id}")
    await call.answer("Ссылка выключена")
    await _show_card(call, link_id)


@router.callback_query(F.data.startswith("ref_adm_log:"))
async def cb_ref_log(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    link_id = int(call.data.split(":")[1])
    link = await referrals.get_link(link_id)
    if not link:
        await call.answer("Ссылка не найдена.", show_alert=True)
        return
    rows = await referrals.rewards(link["owner_id"], limit=20)
    payouts = await referrals.payouts(link["owner_id"])
    text = f"{ui.e('note')} <b>Журнал ссылки #{link_id}</b>\n\n<b>Начисления</b> (последние 20):\n"
    text += "\n".join(f"{referrals.reward_line(r)} · покупатель <code>{r['buyer_id']}</code>" for r in rows) or "пока нет"
    text += "\n\n<b>Выплаты:</b>\n"
    text += "\n".join(
        f"{referrals.format_date(p['created_at'])[:10]} · <b>{referrals.fmt_rub(p['amount_kop'])}</b>"
        + (f" · {html.escape(p['note'])}" if p["note"] else "") for p in payouts
    ) or "пока нет"
    await ui.show(call, text, ui.kb([ui.btn("« Ссылка", callback_data=f"ref_adm_link:{link_id}")]))


@router.callback_query(F.data.startswith("ref_adm_pay:"))
async def cb_ref_pay(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    link_id = int(call.data.split(":")[1])
    link = await referrals.get_link(link_id)
    if not link:
        await call.answer("Ссылка не найдена.", show_alert=True)
        return
    s = await referrals.summary(link["owner_id"])
    await state.set_state(RefAdminStates.waiting_for_payout)
    await state.update_data(ref_link=link_id)
    await ui.show(call, (
        f"{ui.e('card')} <b>Отметить выплату по ссылке #{link_id}</b>\n\n"
        f"Доступно к выводу: <b>{referrals.fmt_rub(max(s['balance'], 0))}</b>\n"
        "Пришлите сумму, которую вы перевели, в рублях. Можно добавить комментарий через пробел: "
        "<code>1500 СБП Тинькофф</code>"
    ), ui.kb([ui.btn("Отмена", "cancel", callback_data=f"ref_adm_link:{link_id}")]))


@router.message(RefAdminStates.waiting_for_payout)
async def msg_ref_pay(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    from handlers.searches import route_menu_text

    if await route_menu_text(message, state):
        return
    # "1500", "1 500,50 ₽ СБП Тинькофф": the amount may contain spaces, the rest is a comment.
    match = re.match(r"^\s*(\d[\d  ]*(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?|р\.?)?\s*(.*)$", message.text or "", re.S)
    amount = referrals.parse_rub(match.group(1)) if match else None
    parts = [match.group(1), match.group(2).strip()] if match and match.group(2).strip() else [""]
    if not amount:
        await message.answer("Не понял сумму. Пришлите число, например <code>1500</code>.", parse_mode="HTML")
        return
    data = await state.get_data()
    await state.clear()
    link = await referrals.get_link(int(data.get("ref_link") or 0))
    if not link:
        await message.answer("Ссылка не найдена.")
        return
    note = parts[1] if len(parts) > 1 else ""
    await referrals.record_payout(link["owner_id"], amount, message.from_user.id, note)
    await database.log_admin_action(message.from_user.id, link["owner_id"], "ref_payout",
                                    f"#{link['id']} {referrals.fmt_rub(amount)} {note}")
    text, markup = await _link_card(message.bot, link["id"])
    await message.answer(f"{ui.e('ok')} Выплата {referrals.fmt_rub(amount)} записана.\n\n" + text,
                         reply_markup=markup, parse_mode="HTML")
    try:
        await message.bot.send_message(link["owner_id"], (
            f"{ui.e('ok')} <b>Выплата {referrals.fmt_rub(amount)} отправлена</b>\n\n"
            "Вознаграждение по партнёрской программе. Баланс — в «Профиль» → «Партнёрская программа»."
        ), parse_mode="HTML")
    except Exception as exc:
        logger.info("Referral payout notice failed: owner=%s error=%s", link["owner_id"], type(exc).__name__)


@router.callback_query(F.data == "ref_adm_rate")
async def cb_ref_rate(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    await state.set_state(RefAdminStates.waiting_for_rate)
    await ui.show(call, (
        f"{ui.e('star')} <b>Курс звезды для начислений</b>\n\n"
        f"Сейчас: <b>{await referrals.star_rate():g} ₽</b> за 1 ⭐.\n"
        "Пришлите новый курс, например <code>1.2</code>. Действует для следующих оплат."
    ), ui.kb([ui.btn("Отмена", "cancel", callback_data="ref_adm:0")]))


@router.message(RefAdminStates.waiting_for_rate)
async def msg_ref_rate(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    from handlers.searches import route_menu_text

    if await route_menu_text(message, state):
        return
    try:
        rate = float((message.text or "").strip().replace(",", "."))
        await referrals.set_star_rate(rate)
    except (ValueError, referrals.ReferralError):
        await message.answer("Нужно число больше 0, например <code>1.2</code>.", parse_mode="HTML")
        return
    await state.clear()
    await database.log_admin_action(message.from_user.id, None, "ref_rate", f"{rate:g} ₽")
    await message.answer(f"{ui.e('ok')} Курс звезды: {rate:g} ₽.",
                         reply_markup=ui.kb([ui.btn("« Ссылки", callback_data="ref_adm:0")]))
