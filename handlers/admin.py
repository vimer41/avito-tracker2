import asyncio
import html
import json
import logging
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database
import logger_setup
import ui
from runtime_metrics import FAILURE_LABELS

logger = logging.getLogger(__name__)
router = Router()
RADARS_DELETE_PAGE = 20


class AdminStates(StatesGroup):
    waiting_for_user_query = State()
    waiting_for_proxy = State()
    waiting_for_emoji_pack = State()
    waiting_for_emoji_slot = State()
    waiting_for_stars_prices = State()
    waiting_for_extend_time = State()
    waiting_for_extend_reason = State()
    waiting_for_lot = State()
    waiting_for_broadcast = State()
    waiting_for_note = State()


@router.callback_query(F.data == "admin_probe")
async def cb_admin_probe(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await call.answer("✅ Запрос получен, бот отвечает", show_alert=True)
    await call.message.answer(
        "✅ <b>Проверка реального запроса Telegram пройдена.</b>\n"
        f"Stars: {'включены' if config.STARS_PAYMENTS_ENABLED else 'выключены'}, "
        f"Базовый {config.STARS_PRICE_LITE} ⭐ / Средний {config.STARS_PRICE_STANDART} ⭐ / Полный {config.STARS_PRICE_PRO} ⭐.\n"
        "Для проверки покупки нужен неадминистративный аккаунт: у администратора доступ бесплатный.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_refresh")],
            [InlineKeyboardButton(text="💳 Тарифы", callback_data="open_subscription_menu")],
        ]),
    )
    logger.info("Live Telegram probe completed by admin=%s", call.from_user.id)


# ── Одобрение / Отклонение доступа администратором ─────────────────────

@router.callback_query(F.data.startswith("admin_approve:"))
async def cb_admin_approve(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    parts = call.data.split(":")
    target_user_id = int(parts[1])
    is_panel = len(parts) > 2
    page = int(parts[2]) if is_panel else 0

    await database.set_user_allowed(target_user_id, 1)
    max_radars = await database.get_user_max_radars(target_user_id)

    user = await database.get_or_create_user(target_user_id)
    username_str = f"@{user['username']}" if user.get("username") else f"ID {target_user_id}"

    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} одобрил доступ пользователю ID:{target_user_id} ({username_str})")
    await database.log_admin_action(call.from_user.id, target_user_id, "approve")

    # Уведомляем пользователя
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "🎉 <b>Ваш доступ к AVITO RADAR успешно одобрен!</b>\n\n"
                f"Доступен мониторинг Авито 24/7: до {max_radars} радаров.\n"
                "Нажмите кнопку ниже или отправьте ссылку на поиск, чтобы запустить первый радар!"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Запустить радар", callback_data="start_approved")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление пользователю {target_user_id}: {e}")

    if is_panel:
        await call.answer(f"Доступ для {username_str} одобрен!")
        await cmd_admin_panel(call, page=page)
    else:
        await call.message.edit_text(
            f"✅ <b>Доступ успешно одобрен!</b>\n\n"
            f"Пользователь: <b>{html.escape(user.get('first_name') or '')}</b> ({username_str}) теперь имеет полный доступ к мониторингу.",
            parse_mode="HTML"
        )
        await call.answer("Доступ одобрен")


@router.callback_query(F.data.startswith("admin_reject:"))
async def cb_admin_reject(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    target_user_id = int(call.data.split(":")[1])
    await database.set_user_allowed(target_user_id, 0)
    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} отклонил доступ пользователю ID:{target_user_id}")
    await database.log_admin_action(call.from_user.id, target_user_id, "reject")

    await call.message.edit_text(
        f"❌ <b>Заявка пользователя {target_user_id} отклонена.</b>",
        parse_mode="HTML"
    )
    await call.answer("Доступ отклонён")


    try:
        await bot.send_message(
            chat_id=target_user_id,
            text="❌ <b>К сожалению, доступ к боту был отклонён администратором.</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin_grant_sub:"))
async def cb_admin_grant_sub(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    # Формат: admin_grant_sub:{user_id}:{plan}:{days}
    try:
        parts = call.data.split(":")
        target_user_id, plan, days = int(parts[1]), parts[2], int(parts[3])
        page = int(parts[4]) if len(parts) > 4 else 0
        token = await database.create_admin_subscription_action(call.from_user.id, target_user_id, plan, days, page)
    except (ValueError, IndexError, PermissionError) as exc:
        await call.answer(str(exc) or "Неверная кнопка подписки", show_alert=True)
        return
    await call.message.edit_text(
        f"💳 <b>ПОДТВЕРЖДЕНИЕ ВЫДАЧИ ПОДПИСКИ</b>\n\n"
        f"Пользователь: <code>{target_user_id}</code>\nТариф: <b>{config.plan_title(plan)}</b>\nДобавить: <b>{days} дн.</b>\n\n"
        "Срок добавится к текущей подписке или триалу; выбранный тариф применяется сразу. "
        "Это ручная выдача доступа, а не подтверждение оплаты Stars.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить выдачу / продление", callback_data=f"admin_confirm_sub:{token}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user:{target_user_id}:{page}")],
        ]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_confirm_sub:"))
async def cb_admin_confirm_sub(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        result = await database.apply_admin_subscription_action(call.data.split(":")[1], call.from_user.id)
    except (ValueError, IndexError, PermissionError) as exc:
        await call.answer(str(exc), show_alert=True)
        return
    if not result["applied"]:
        await call.answer("Эта выдача уже выполнена. Повторное нажатие не добавляет срок.", show_alert=True)
        return
    target_user_id, plan, days = result["user_id"], result["tariff_plan"], result["days"]
    until = result["subscription_until"]
    max_radars = await database.get_user_max_radars(target_user_id)
    formatted_until = database.format_subscription_date(until)

    plan_title = f"«{config.plan_title(plan)}»"
    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} выдал подписку {plan_title} на {days} дн. пользователю ID:{target_user_id}")
    await database.log_admin_action(call.from_user.id, target_user_id, "grant", f"{plan_title}, {days} дн.")

    await call.message.edit_text(
        f"✅ <b>Подписка {plan_title} успешно выдана!</b>\n\n"
        f"👤 ID пользователя: <code>{target_user_id}</code>\n"
        f"📦 Тариф: <b>{plan_title}</b> (до {max_radars} радаров)\n"
        f"📅 Срок действия: <b>{formatted_until}</b> ({days} дн.)",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Карточка пользователя", callback_data=f"admin_user:{target_user_id}:{result['page']}")],
            [InlineKeyboardButton(text="« Список пользователей", callback_data=f"admin_page:{result['page']}")],
        ]),
    )
    await call.answer(f"Подписка {plan_title} активирована!")

    # Ручная выдача не является подтверждением финансовой операции.
    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="refresh_profile")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")]
        ])
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                f"🎉 <b>Администратор активировал подписку {plan_title}!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Доступно активных радаров: <b>{max_radars} шт.</b>\n"
                f"📅 Срок действия: <b>{formatted_until}</b> ({days} дн.)\n\n"
                f"Радары доступны в пределах тарифа. Доставка новых лотов зависит от доступности Авито. "
                f"Удачных и прибыльных покупок!"
            ),
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {target_user_id} об активации подписки: {e}")


async def render_user_card_data(target_id: int, page: int = 0):
    user = await database.get_user(target_id)
    if not user:
        return None, None
    has_access = await database.is_user_allowed(target_id)
    until = database.format_subscription_date(user.get("subscription_until"))
    plan_name = config.plan_title(user.get("tariff_plan"))
    trial_used = bool(user.get("trial_used"))
    is_allowed = bool(user.get("is_allowed"))

    user_radars = await database.get_user_searches(target_id)
    active_radars = sum(1 for r in user_radars if r.get("is_active", 1))
    max_radars = await database.get_user_max_radars(target_id)
    extra_radars = int(user.get("extra_radars") or 0)
    extra_label = f", из них +{extra_radars} от админа" if extra_radars else ""
    blocked_count = len(await database.get_user_blocked_sellers_list(target_id))

    if has_access and not is_allowed and trial_used:
        status_line = f"🎁 <b>Бесплатный триал ({config.TRIAL_HOURS} ч)</b>"
    elif has_access and is_allowed and user.get("subscription_until"):
        status_line = f"🟢 <b>{plan_name} Подписка</b>"
    elif has_access and is_allowed:
        status_line = f"🟢 <b>Бессрочный VIP ({plan_name})</b>"
    else:
        status_line = "🔴 <b>Доступ истёк / отозван</b>"

    uname = f"@{user['username']}" if user.get("username") else "—"
    fname = html.escape(user.get("first_name") or "Без имени")

    text = (
        "👤 <b>КАРТОЧКА ПОЛЬЗОВАТЕЛЯ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• Имя: <b>{fname}</b>\n"
        f"• Username: <b>{uname}</b>\n"
        f"• Telegram ID: <code>{target_id}</code>\n"
        f"• Статус: {status_line}\n"
        f"• Тариф: <b>{plan_name}</b> (лимит: до {max_radars} радаров{extra_label})\n"
        f"• Срок подписки: <i>{until}</i>\n"
        f"• Радары: <b>{active_radars}</b> акт. / <b>{len(user_radars)}</b> всего"
        + (" (не проверяются: нет доступа)" if active_radars and not has_access else "") + "\n"
        f"• Чёрный список: <b>{blocked_count}</b> продавцов\n"
        f"• Триал использован: <b>{'Да' if trial_used else 'Нет'}</b>\n"
        + await _activity_lines(target_id, user)
        + "━━━━━━━━━━━━━━━━━━━━\n"
        "Управление пользователем:"
    )

    limit_row = [
        InlineKeyboardButton(text="➖5 радаров", callback_data=f"admin_radar_limit:{target_id}:-5:{page}"),
        InlineKeyboardButton(text="➕5 радаров", callback_data=f"admin_radar_limit:{target_id}:5:{page}"),
        InlineKeyboardButton(text="➕20 радаров", callback_data=f"admin_radar_limit:{target_id}:20:{page}"),
    ]
    back_row = [
        InlineKeyboardButton(text="🔍 Найти другого", callback_data="admin_search_user_prompt"),
        InlineKeyboardButton(text="« Список пользователей", callback_data=f"admin_page:{page}"),
    ]
    delete_row = [InlineKeyboardButton(text="🗑 Удалить радары", callback_data=f"admin_rdel:new:{target_id}:{page}")] if user_radars else []
    note_row = [
        InlineKeyboardButton(text="📝 Заметка", callback_data=f"admin_note:{target_id}:{page}"),
        InlineKeyboardButton(text="🧾 Действия админов", callback_data=f"admin_ulog:{target_id}:{page}"),
    ]
    if config.is_admin(target_id):
        # Admins have no subscription: only radars and the radar limit are managed here.
        text = text.replace(f"• Статус: {status_line}\n", "• Статус: 👑 <b>Администратор</b>\n")
        return text, InlineKeyboardMarkup(inline_keyboard=[row for row in (
            [InlineKeyboardButton(text=f"📁 Радары ({len(user_radars)})", callback_data=f"admin_user_radars:{target_id}:{page}")],
            note_row,
            delete_row,
            limit_row,
            back_row,
        ) if row])

    buttons = [
        [InlineKeyboardButton(text="💳 Выдать / продлить → выбрать тариф", callback_data=f"admin_sub_menu:{target_id}:{page}")],
        [InlineKeyboardButton(text=f"🎁 Выдать / обновить триал ({config.TRIAL_HOURS} ч)", callback_data=f"admin_reset_trial_prompt:{target_id}:{page}")],
        [InlineKeyboardButton(text=f"📁 Радары ({len(user_radars)})", callback_data=f"admin_user_radars:{target_id}:{page}"),
         InlineKeyboardButton(text="⭐ Платежи / возврат", callback_data=f"admin_payments:{target_id}")],
        limit_row,
        note_row,
    ]
    if delete_row:
        buttons.insert(3, delete_row)
    if has_access:
        buttons.append([InlineKeyboardButton(text="🚫 Отозвать доступ", callback_data=f"admin_revoke_prompt:{target_id}:{page}")])
    buttons.append([
        InlineKeyboardButton(text="🗑 Удалить пользователя", callback_data=f"admin_purge_user:{target_id}:{page}"),
        InlineKeyboardButton(text="✍️ Написать пользователю", callback_data=f"support_reply:{target_id}")
    ])
    buttons.append(back_row)
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def _ago(value) -> str:
    """'2 ч назад' from an epoch or a SQLite UTC timestamp."""
    import time
    from datetime import datetime, timezone

    if value in (None, ""):
        return "—"
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        try:
            epoch = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return "—"
    return f"{_dur(time.time() - epoch)} назад"


async def _activity_lines(target_id: int, user: dict) -> str:
    activity = await database.get_user_activity(target_id)
    lines = [
        f"• Заходил: <b>{_ago(user.get('last_seen_at'))}</b>",
        f"• Лотов получил: <b>{activity['lots_day']}</b> за сутки, <b>{activity['lots_week']}</b> за неделю, "
        f"всего {activity['lots_total']} · последний {_ago(activity['last_sent'])}",
        f"• Оплат: <b>{activity['paid_count']}</b> на <b>{activity['paid_total']} ⭐</b>"
        + (f" · последняя {_ago(activity['last_paid'])}" if activity["paid_count"] else "")
        + (f"; рублями <b>{activity['rub_count']}</b> на <b>{activity['rub_total']} ₽</b> · последняя {_ago(activity['last_rub'])}"
           if activity["rub_count"] else ""),
    ]
    if activity["blocked_bot"]:
        lines.append("• ⛔️ <b>Заблокировал бота</b>: радары на паузе, сообщения не доходят")
    if user.get("admin_note"):
        lines.append(f"• 📝 Заметка: <i>{html.escape(user['admin_note'])}</i>")
    return "\n".join(lines) + "\n"


@router.callback_query(F.data.startswith("admin_note:"))
async def cb_admin_note(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        _, target_raw, page_raw = call.data.split(":")
        target_id, page = int(target_raw), int(page_raw)
    except ValueError:
        await call.answer("Некорректная кнопка", show_alert=True)
        return
    user = await database.get_user(target_id) or {}
    await state.set_state(AdminStates.waiting_for_note)
    await state.update_data(note_user=target_id, note_page=page)
    current = f"\n\nСейчас: <i>{html.escape(user['admin_note'])}</i>" if user.get("admin_note") else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Стереть заметку", callback_data=f"admin_note_clear:{target_id}:{page}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user:{target_id}:{page}")],
    ])
    await ui.show(call, f"📝 <b>Заметка о пользователе</b> <code>{target_id}</code>\n\n"
                        f"Пришлите текст. Её видят только админы.{current}", kb)


@router.callback_query(F.data.startswith("admin_note_clear:"))
async def cb_admin_note_clear(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    _, target_raw, page_raw = call.data.split(":")
    await state.clear()
    await database.set_admin_note(int(target_raw), "")
    await database.log_admin_action(call.from_user.id, int(target_raw), "note", "заметка стёрта")
    text, kb = await render_user_card_data(int(target_raw), int(page_raw))
    await ui.show(call, text, kb)


@router.message(AdminStates.waiting_for_note)
async def msg_admin_note(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    from handlers.searches import route_menu_text

    if await route_menu_text(message, state):
        return
    data = await state.get_data()
    await state.clear()
    target_id, page = int(data.get("note_user") or 0), int(data.get("note_page") or 0)
    note = (message.text or "").strip()[:500]
    await database.set_admin_note(target_id, note)
    await database.log_admin_action(message.from_user.id, target_id, "note", note[:100])
    text, kb = await render_user_card_data(target_id, page)
    if text:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


ADMIN_ACTION_LABELS = {
    "grant": "выдал подписку", "vip": "выдал VIP", "trial": "выдал триал", "revoke": "отозвал доступ",
    "limit": "изменил лимит радаров", "radars_delete": "удалил радары", "purge": "удалил пользователя",
    "refund": "вернул платёж", "note": "заметка", "broadcast": "рассылка", "extend_all": "продлил всем",
    "net_mode": "режим сети", "prices": "цены Stars", "approve": "одобрил доступ", "reject": "отклонил доступ",
}


def _admin_action_lines(rows: list[dict], with_user: bool) -> list[str]:
    lines = []
    for row in rows:
        target = f" → <code>{row['user_id']}</code>" if with_user and row.get("user_id") else ""
        detail = f": {html.escape(row['detail'])}" if row.get("detail") else ""
        label = ADMIN_ACTION_LABELS.get(row["action"], row["action"])
        lines.append(f"<code>{_msk_sql(row['at'])}</code> админ {row['admin_id']}{target} — {label}{detail}")
    return lines or ["Пока пусто."]


@router.callback_query(F.data.startswith("admin_ulog:"))
async def cb_admin_user_log(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    _, target_raw, page_raw = call.data.split(":")
    rows = await database.get_admin_actions(int(target_raw), limit=25)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Карточка пользователя", callback_data=f"admin_user:{target_raw}:{page_raw}")],
    ])
    text = f"🧾 <b>Действия админов</b> · <code>{target_raw}</code>\n\n" + "\n".join(_admin_action_lines(rows, False))
    await ui.show(call, text + "\n\n<blockquote>Время МСК.</blockquote>", kb)


@router.callback_query(F.data == "admin_alog")
async def cb_admin_log_all(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    rows = await database.get_admin_actions(None, limit=30)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data="admin_refresh")]])
    text = "🧾 <b>Журнал админов</b> (последние 30)\n\n" + "\n".join(_admin_action_lines(rows, True))
    await ui.show(call, text + "\n\n<blockquote>Время МСК.</blockquote>", kb)


@router.callback_query(F.data.startswith("admin_radar_limit:"))
async def cb_admin_radar_limit(call: CallbackQuery):
    """admin_radar_limit:<user_id|self>:<delta>:<page> — меняет бонус к лимиту радаров."""
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        _, target_raw, delta_raw, page_raw = call.data.split(":")
        target_id = call.from_user.id if target_raw == "self" else int(target_raw)
        delta, page = int(delta_raw), max(0, int(page_raw))
    except ValueError:
        await call.answer("Некорректная команда", show_alert=True)
        return
    extra = await database.change_user_extra_radars(target_id, delta)
    max_radars = await database.get_user_max_radars(target_id)
    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} изменил лимит радаров ID:{target_id} на {delta:+d} (бонус {extra}, лимит {max_radars})")
    await database.log_admin_action(call.from_user.id, target_id, "limit", f"{delta:+d}, лимит {max_radars}")
    await call.answer(f"Лимит радаров: {max_radars} (бонус +{extra})")
    if target_raw == "self":
        return
    text, kb = await render_user_card_data(target_id, page)
    if text:
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass


@router.callback_query(F.data.startswith(("admin_user:", "admin_sub_menu:", "admin_sub_days:")))
async def cb_admin_user(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        parts = call.data.split(":")
        target_id = int(parts[1])
        plan = parts[2] if parts[0] == "admin_sub_days" else None
        page = max(0, int(parts[3] if plan is not None else parts[2]))
        if plan is not None and plan not in config.PAID_PLANS:
            raise ValueError("Неизвестный тариф")
    except (ValueError, IndexError):
        await call.answer("Некорректная кнопка пользователя", show_alert=True)
        return
    user = await database.get_user(target_id)
    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
        return
    if config.is_admin(target_id) and parts[0] != "admin_user":
        await call.answer("Доступ администратора не требует подписки и не меняется здесь.", show_alert=True)
        return

    if parts[0] == "admin_user":
        text, kb = await render_user_card_data(target_id, page)
        if not text:
            await call.answer("Пользователь не найден", show_alert=True)
            return
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await call.answer()
        return

    until = database.format_subscription_date(user["subscription_until"])
    has_access = await database.is_user_allowed(target_id)
    text = (f"👤 <b>{html.escape(user.get('first_name') or 'Пользователь')}</b>\n"
            f"🆔 ID: <code>{target_id}</code>\n"
            f"📦 Тариф: <b>{config.plan_title(user.get('tariff_plan'))}</b>\n"
            f"🔑 Доступ: {'активен' if has_access else 'истёк / отозван'}\n📅 Срок: {until}\n\n")

    if parts[0] == "admin_sub_menu":
        text += "<b>Выберите тариф для выдачи / продления:</b>"
        buttons = [
            *[[InlineKeyboardButton(
                text=f"{config.plan_title(p)} · {config.plan_radars(p)} рад. · раз в {config.plan_interval(p)} с",
                callback_data=f"admin_sub_days:{target_id}:{p}:{page}",
            )] for p in config.PAID_PLANS],
            *[[InlineKeyboardButton(
                text=f"👑 Бессрочный «{config.plan_title(p)}» (VIP)",
                callback_data=f"admin_grant_vip_prompt:{target_id}:{p}:{page}",
            )] for p in config.PAID_PLANS],
            [InlineKeyboardButton(text="« Назад в карточку", callback_data=f"admin_user:{target_id}:{page}")]
        ]
    elif parts[0] == "admin_sub_days":
        text += f"<b>Добавить срок на тарифе «{config.plan_title(plan)}»:</b>"
        buttons = [
            [InlineKeyboardButton(text=f"➕ {days} дн.", callback_data=f"admin_grant_sub:{target_id}:{plan}:{days}:{page}") for days in (1, 7, 14)],
            [InlineKeyboardButton(text=f"➕ {days} дн.", callback_data=f"admin_grant_sub:{target_id}:{plan}:{days}:{page}") for days in (30, 90, 365)],
            [InlineKeyboardButton(text="« Назад к выбору тарифа", callback_data=f"admin_sub_menu:{target_id}:{page}")]
        ]
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("admin_grant_vip_prompt:"))
async def cb_admin_grant_vip_prompt(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    target_id, plan, page = int(parts[1]), parts[2], int(parts[3])
    plan_title = f"«{config.plan_title(plan)}»"
    text = (
        f"👑 <b>ВЫДАЧА БЕССРОЧНОГО VIP-ДОСТУПА</b>\n\n"
        f"Пользователь: <code>{target_id}</code>\n"
        f"Тариф: <b>{plan_title}</b>\n\n"
        "Пользователю будет выдан постоянный бессрочный доступ к боту без ограничения по времени. "
        "Пользователь получит мгновенное уведомление в чат."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить выдачу VIP", callback_data=f"admin_confirm_vip:{target_id}:{plan}:{page}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user:{target_id}:{page}")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("admin_confirm_vip:"))
async def cb_admin_confirm_vip(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    target_id, plan, page = int(parts[1]), parts[2], int(parts[3])
    await database.grant_vip_access(target_id, plan)
    max_radars = await database.get_user_max_radars(target_id)
    plan_title = f"«{config.plan_title(plan)}»"

    logger.info("👑 [АДМИН] Администратор ID:%s выдал бессрочный VIP (%s) пользователю ID:%s", call.from_user.id, plan_title, target_id)
    await database.log_admin_action(call.from_user.id, target_id, "vip", plan_title)

    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="refresh_profile")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")]
        ])
        await bot.send_message(
            chat_id=target_id,
            text=(
                f"👑 <b>Администратор предоставил вам постоянный VIP-доступ к AVITO RADAR!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Тариф: <b>{plan_title}</b>\n"
                f"🎯 Лимит активных радаров: <b>{max_radars} шт.</b>\n"
                f"📅 Срок действия: <b>Бессрочно</b> ♾️\n\n"
                f"Вам открыт доступ ко всем возможностям тарифа без ограничений по времени. "
                f"Удачных и выгодных находок!"
            ),
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning("Не удалось уведомить пользователя %s о выдаче VIP: %s", target_id, e)

    await call.answer(f"Бессрочный {plan_title} выдан!", show_alert=True)
    text, kb = await render_user_card_data(target_id, page)
    if text:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_reset_trial_prompt:"))
async def cb_admin_reset_trial_prompt(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    target_id, page = int(parts[1]), int(parts[2])
    text = (
        f"🎁 <b>Активация триала ({config.TRIAL_TEXT})</b>\n\n"
        f"Пользователь: <code>{target_id}</code>\n\n"
        f"Пользователю будет выдан бесплатный пробный доступ на <b>{config.TRIAL_TEXT}</b> "
        f"({config.TRIAL_RADARS} радар, проверка раз в {config.plan_interval('pro')} с, как на «Полном»). "
        "Пользователь получит мгновенное уведомление в чат."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Активировать триал {config.TRIAL_HOURS} ч", callback_data=f"admin_confirm_trial:{target_id}:{page}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user:{target_id}:{page}")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("admin_confirm_trial:"))
async def cb_admin_confirm_trial(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    target_id, page = int(parts[1]), int(parts[2])
    user = await database.reset_user_trial(target_id)
    until = database.format_subscription_date(user.get("subscription_until"))

    logger.info("👑 [АДМИН] Администратор ID:%s активировал триал %sч для пользователя ID:%s", call.from_user.id, config.TRIAL_HOURS, target_id)
    await database.log_admin_action(call.from_user.id, target_id, "trial", f"{config.TRIAL_HOURS} ч")

    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустить радар", callback_data="start_approved")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="refresh_profile")]
        ])
        await bot.send_message(
            chat_id=target_id,
            text=(
                f"🎁 <b>Администратор активировал вам бесплатный пробный период на {config.TRIAL_TEXT}!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Доступен: <b>{config.TRIAL_RADARS} радар</b>\n"
                f"📅 Срок действия: <b>{until}</b> ({config.TRIAL_HOURS} ч.)\n\n"
                f"Отправьте ссылку на поиск с Авито прямо в чат или нажмите кнопку ниже:"
            ),
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning("Не удалось уведомить пользователя %s об активации триала: %s", target_id, e)

    await call.answer(f"Триал {config.TRIAL_TEXT} активирован!", show_alert=True)
    text, kb = await render_user_card_data(target_id, page)
    if text:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_user_radars:"))
async def cb_admin_user_radars(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    target_id, page = int(parts[1]), int(parts[2])
    radars = await database.get_user_searches(target_id)

    text_lines = [
        f"📁 <b>РАДАРЫ ПОЛЬЗОВАТЕЛЯ <code>{target_id}</code></b> (всего: {len(radars)})\n"
        "━━━━━━━━━━━━━━━━━━━━"
    ]
    if not radars:
        text_lines.append("<i>У пользователя пока нет созданных радаров.</i>")
    else:
        for idx, r in enumerate(radars, start=1):
            is_active = bool(r.get("is_active", 1))
            status_emoji = "🟢" if is_active else "⏸"
            scope = "🏷 В заголовках" if r.get("search_in_title_only") else "📄 Заголовок+текст"
            title = html.escape(r.get("title") or "Без названия")
            url = html.escape(r.get("url") or "")
            text_lines.append(
                f"{idx}. {status_emoji} <b>{title}</b> · #{r['id']}\n"
                f"   ├ Выборка: <i>{scope}</i>\n"
                f"   └ <a href=\"{url}\">Ссылка на Авито</a>"
            )

    scan_buttons = [
        InlineKeyboardButton(text=f"📊 Сканы {idx}. {(r.get('title') or 'Радар')[:22]}",
                             callback_data=f"admin_scans:{r['id']}:{target_id}:{page}")
        for idx, r in enumerate(radars[:20], start=1)
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *[[button] for button in scan_buttons],
        [InlineKeyboardButton(text="📜 История радаров", callback_data=f"admin_rhist:{target_id}:{page}:0")],
        [InlineKeyboardButton(text="« Назад в карточку пользователя", callback_data=f"admin_user:{target_id}:{page}")]
    ])
    text = "\n".join(text_lines)
    if len(text) > 3800:
        text = text[:3800].rsplit("\n", 1)[0] + "\n…"
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    await call.answer()


RADAR_HISTORY_PAGE = 15


def _msk(epoch: float, fmt: str = "%H:%M:%S") -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch + 3 * 3600, timezone.utc).strftime(fmt)


def _msk_sql(value) -> str:
    """'2026-11-05 12:00:00' (UTC, SQLite) → '05.11 15:00' MSK."""
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return str(value or "?")[:16]
    return _msk(dt.timestamp(), "%d.%m %H:%M")


def _dur(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} м {seconds % 60:02d} с"
    return f"{seconds // 3600} ч {seconds % 3600 // 60:02d} м"


@router.callback_query(F.data.startswith("admin_rhist:"))
async def cb_admin_radar_history(call: CallbackQuery):
    """admin_rhist:<user_id>:<card_page>:<page> — all radar changes, deleted radars included."""
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        _, target_raw, card_raw, page_raw = call.data.split(":")
        target_id, card_page, page = int(target_raw), int(card_raw), max(0, int(page_raw))
    except ValueError:
        await call.answer("Некорректная кнопка", show_alert=True)
        return
    rows = await database.get_radar_history(target_id)
    pages = max(1, -(-len(rows) // RADAR_HISTORY_PAGE))
    page = min(page, pages - 1)
    live_ids = {r["id"] for r in await database.get_user_searches(target_id)}
    created = sum(1 for r in rows if r["action"] == "create")
    deleted = sum(1 for r in rows if r["action"] == "delete")
    lines = [
        f"{ui.e('time')} <b>История радаров</b> · ID <code>{target_id}</code>",
        f"Создано: <b>{created}</b> · удалено: <b>{deleted}</b> · сейчас: <b>{len(live_ids)}</b>"
        + (f" · стр. {page + 1}/{pages}" if pages > 1 else ""),
        "",
    ]
    if not rows:
        lines.append("Пока пусто.")
    for row in rows[page * RADAR_HISTORY_PAGE:(page + 1) * RADAR_HISTORY_PAGE]:
        action = database.RADAR_HISTORY_LABELS.get(row["action"], row["action"])
        title = html.escape((row.get("title") or "Радар")[:40])
        mark = "" if row["search_id"] in live_ids else " ✖"
        line = f"<code>{_msk_sql(row['at'])}</code> #{row['search_id']}{mark} <b>{title}</b> — {action}"
        if row.get("detail"):
            line += f" ({html.escape(row['detail'])})"
        if row["action"] in ("create", "url", "delete") and row.get("url"):
            line += f' · <a href="{html.escape(row["url"])}">ссылка</a>'
        lines.append(line)
    lines.append("\n<blockquote>✖ — радара уже нет. Время МСК.</blockquote>")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="«", callback_data=f"admin_rhist:{target_id}:{card_page}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="»", callback_data=f"admin_rhist:{target_id}:{card_page}:{page + 1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[row for row in (
        nav,
        [InlineKeyboardButton(text="« Радары", callback_data=f"admin_user_radars:{target_id}:{card_page}")],
    ) if row])
    await ui.show(call, "\n".join(lines), kb)


def render_scan_journal(radar: dict, now: float = None) -> str:
    """Last scans and new lots of one radar: how often it is really checked, and who held each lot."""
    import time
    import scan_journal

    now = now or time.time()
    rid = int(radar["id"])
    plan_interval = None
    try:
        tariff, _, _ = database.classify_user_tariff(radar)
        plan_interval = config.plan_interval(tariff)
    except Exception:
        pass
    hour = scan_journal.summary(rid, 3600, now)
    ten = scan_journal.summary(rid, 600, now)
    scans = scan_journal.scans(rid)
    lots = scan_journal.lots(rid)

    lines = [
        f"{ui.e('radar')} <b>Сканы радара #{rid}</b>",
        f"«{html.escape((radar.get('title') or 'Радар')[:50])}» · {'🟢 работает' if radar.get('is_active', 1) else '⏸ на паузе'}",
        "",
    ]
    if not scans:
        lines.append("Сканов в памяти нет: бот перезапускался или радар не проверяется.")
    else:
        lines += [
            f"<b>Частота</b> (тариф: раз в {plan_interval or '?'} с)",
            f"10 мин: сканов {ten['scans']}, удачных {ten['ok']} · интервал медиана {_dur(ten['gap_median'])}, макс {_dur(ten['gap_max'])}",
            f"1 час: сканов {hour['scans']}, удачных {hour['ok']} · интервал медиана {_dur(hour['gap_median'])}, макс {_dur(hour['gap_max'])}",
            f"Последний удачный: {_dur(hour['since_last_ok'])} назад · запрос {int(hour['ms_median'] or 0)} мс",
        ]
        if hour["kinds"]:
            fails = ", ".join(f"{FAILURE_LABELS.get(k, k)} ×{n}" for k, n in sorted(hour["kinds"].items(), key=lambda kv: -kv[1]))
            lines.append(f"Сбои за час: {html.escape(fails)}")
        lines += ["", "<b>Последние сканы</b> (МСК · интервал · результат)"]
        recent = scans[-15:]
        previous_at = scans[-16]["at"] if len(scans) > 15 else None
        out = []
        for entry in recent:
            gap = f"+{_dur(entry['at'] - previous_at)}" if previous_at else ""
            previous_at = entry["at"]
            if entry["ok"]:
                new = entry["new"].get(rid, 0)
                res = f"✓ {entry['items']} лот." + (f" <b>+{new} нов.</b>" if new else "") + (" =" if entry["fast"] else "")
                res += f" · {entry['ms']} мс · {html.escape(entry['ch'] or '?')}"
            else:
                res = f"✗ {html.escape(FAILURE_LABELS.get(entry['kind'], entry['kind']))} · {entry['ms']} мс"
            if entry["due_lag"] >= 3:
                res += f" · опоздал {_dur(entry['due_lag'])}"
            out.append(f"<code>{_msk(entry['at'])}</code> {gap} {res}")
        lines += list(reversed(out))

    lines += ["", "<b>Новые лоты</b> (возраст = Авито прятал + бот не смотрел)"]
    if not lots:
        lines.append("Новых лотов в памяти нет.")
    for lot in reversed(lots[-10:]):
        split = scan_journal.lot_split(lot)
        title = html.escape(lot["title"][:30] or lot["item"])
        head = f'<code>{_msk(lot["seen"])}</code> <a href="{html.escape(lot["url"])}">{title}</a>'
        if split["age"] is None:
            lines.append(f"{head}: нет времени публикации")
            continue
        verdict = (
            "Авито" if (split["avito"] or 0) > max(60.0, 2 * (split["bot"] or 0))
            else "бот" if (split["bot"] or 0) > max(60.0, 2 * (split["avito"] or 0))
            else "норма" if split["age"] < 60 else "оба"
        )
        tail = f" · отправлен через {_dur(split['send'])}" if split["send"] is not None else ""
        lines.append(
            f"{head}\n   опубл. {_msk(lot['pub'])}, возраст <b>{_dur(split['age'])}</b>: "
            f"Авито {_dur(split['avito'])} + бот {_dur(split['bot'])} → <b>{verdict}</b>{tail}"
            + ("" if lot["reliable"] else " · время неточное")
            + (" · продвиж." if lot["promo"] else "")
            + (f" · поз. {lot['pos'] + 1}" if lot.get("pos") is not None else "")
        )
    lines.append(
        "\n<blockquote>«Авито» — лота не было в выдаче на прошлом удачном скане, хотя он уже был опубликован: "
        "Авито показал его в поиске с задержкой (модерация, индексация, поднятие). "
        "«Бот» — между сканами прошло слишком долго. «=» — выдача не изменилась. Журнал в памяти, сбрасывается при перезапуске.</blockquote>"
    )
    text = "\n".join(lines)
    while ui.visible_len(text) > 4000 and len(lines) > 10:
        lines.pop(-2)
        text = "\n".join(lines)
    return text


@router.callback_query(F.data.startswith("admin_scans:"))
async def cb_admin_scans(call: CallbackQuery):
    """admin_scans:<radar_id>:<user_id>:<card_page>"""
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        _, rid_raw, target_raw, card_raw = call.data.split(":")
        rid, target_id, card_page = int(rid_raw), int(target_raw), int(card_raw)
    except ValueError:
        await call.answer("Некорректная кнопка", show_alert=True)
        return
    radar = await database.get_search(rid)
    if not radar:
        await call.answer("Радар удалён", show_alert=True)
        return
    owner = await database.get_user(int(radar["user_id"])) or {}
    radar = {**radar, **{k: owner.get(k) for k in ("tariff_plan", "is_allowed", "trial_used", "subscription_until")}}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=call.data)],
        [InlineKeyboardButton(text="« Радары", callback_data=f"admin_user_radars:{target_id}:{card_page}")],
    ])
    await ui.show(call, render_scan_journal(radar), kb, disable_web_page_preview=True)


def parse_item_id(text: str) -> str | None:
    """Avito lot number from a link (…/iphone_13_1234567890?…) or the bare number."""
    import re
    from urllib.parse import urlsplit

    raw = (text or "").strip()
    if raw.isdigit() and len(raw) >= 5:
        return raw
    path = urlsplit(raw if "://" in raw else f"https://{raw}").path
    found = re.findall(r"(\d{6,})", path)
    return found[-1] if found else None


async def explain_lot(item_id: str) -> str:
    """Everything the bot knows about one lot: what each radar decided, where it sits now, what waits to be sent."""
    import time
    import scan_journal
    from parser import AvitoParser
    from source_cache import SOURCE_SNAPSHOTS

    now = time.time()
    lines = [f"{ui.e('scope')} <b>Лот {html.escape(item_id)}</b>"]
    radar_titles: dict[int, str] = {}

    async def radar_name(rid: int) -> str:
        if rid not in radar_titles:
            radar = await database.get_search(rid)
            radar_titles[rid] = (
                f"#{rid} «{html.escape((radar.get('title') or 'Радар')[:30])}» (польз. {radar.get('user_id')})"
                if radar else f"#{rid} (удалён)"
            )
        return radar_titles[rid]

    decided = scan_journal.decisions(item_id)
    title = next((d["title"] for d in decided if d["title"]), "")
    published = next((d["pub"] for d in decided if d["pub"]), None)

    # Where the lot sits in the latest page of each link, if the bot still holds that page.
    placed = []
    try:
        url_radars: dict[str, list[dict]] = {}
        for radar in await database.get_all_active_searches():
            keys = {radar["url"]}
            try:
                keys.add(AvitoParser.normalize_url(radar["url"]))
            except Exception:
                pass
            for key in keys:
                url_radars.setdefault(key, []).append(radar)
        for key, (snapshot, _) in list(SOURCE_SNAPSHOTS._data.items()):
            for position, item in enumerate(snapshot.items, 1):
                if str(item.item_id) == item_id:
                    title = title or item.title
                    published = published or item.timestamp
                    names = ", ".join(f"#{r['id']}" for r in url_radars.get(key, [])) or "радар не найден"
                    placed.append(f"{names}: место {position} из {len(snapshot.items)}, скан {_msk(snapshot.captured_at)}")
    except Exception:
        logger.debug("Не удалось просмотреть кэш выдачи", exc_info=True)

    if title:
        lines.append(f"«{html.escape(title[:60])}»")
    if published:
        try:
            lines.append(f"Опубликован: {_msk(float(published), '%d.%m %H:%M:%S')} МСК")
        except (TypeError, ValueError):
            pass
    lines.append("")

    if decided:
        lines.append("<b>Что решил бот, когда лот был новым</b>")
        for entry in decided:
            verdict = scan_journal.VERDICTS.get(entry["verdict"], entry["verdict"])
            detail = f" «{html.escape(entry['detail'])}»" if entry["detail"] else ""
            line = f"{await radar_name(entry['rid'])}: {_msk(entry['at'])} — {verdict}{detail}"
            if entry["verdict"] == "queued":
                line += f", отправлен через {_dur(entry['sent'] - entry['at'])}" if entry["sent"] else ", отправки ещё не было"
            if entry["pub"]:
                try:
                    line += f" · возраст при находке {_dur(entry['at'] - float(entry['pub']))}"
                except (TypeError, ValueError):
                    pass
            lines.append(line)
        lines.append("")

    traces = await database.find_lot_traces(item_id)
    # The journal of decisions lives in memory; after a restart or for an old lot the logs still know it.
    history = [] if decided else await _lot_history(item_id)
    if traces["deliveries"]:
        lines.append("<b>Доставка</b>")
        for row in traces["deliveries"]:
            who = await radar_name(int(row["search_id"])) if row.get("search_id") else f"польз. {row['user_id']}"
            status = row["status"]
            if status == "sent" and row.get("sent_at"):
                state = f"отправлен {_msk(row['sent_at'])}, через {_dur(row['sent_at'] - row['queued_at'])} после находки"
            elif status in ("expired", "failed"):
                when = row.get("expired_at") or row.get("failed_at")
                state = (f"не отправлен ({'устарел' if status == 'expired' else 'ошибка'}"
                         f"{': ' + html.escape(str(row['last_error'])[:60]) if row.get('last_error') else ''})"
                         + (f" в {_msk(when)}" if when else ""))
            else:
                state = f"ждёт отправки с {_msk(row['queued_at'])}, попыток {row['attempt_count']}"
            lines.append(f"{who}: {state}")
        lines.append("")
    if placed:
        lines.append("<b>В последней выдаче</b>")
        lines += placed
        lines.append("")
    if history:
        lines.append(f"<b>Из логов за {logger_setup.LOG_KEEP_DAYS} дн.</b>")
        for row in history[-LOT_HISTORY_LINES:]:
            fields = row.get("f") or {}
            when = _msk(float(row.get("t") or 0), "%d.%m %H:%M:%S")
            if row.get("ev") == "lot.new":
                rid = fields.get("rid")
                line = f"{await radar_name(int(rid)) if rid is not None else 'радар ?'}: найден {when}"
                if fields.get("age") is not None:
                    line += f", через {_dur(fields['age'])} после публикации"
                if fields.get("pos") is not None:
                    line += f", место {int(fields['pos']) + 1} в выдаче"
            else:
                line = f"польз. {fields.get('chat')}: отправлен {when}"
                if fields.get("seen_to_sent_s") is not None:
                    line += f", через {_dur(fields['seen_to_sent_s'])} после находки"
            lines.append(line)
        lines.append("")
    if traces["seen"] and not decided:
        lines.append("<b>Помечен как просмотренный</b>")
        for row in traces["seen"][:8]:
            lines.append(f"{await radar_name(int(row['search_id']))} с {_msk_sql(row['created_at'])}")
        lines.append(
            "<blockquote>Бот видел лот раньше, чем хранит журнал (до перезапуска) или при первом скане радара: "
            "лоты, которые уже были в выдаче при создании радара, не присылаются.</blockquote>"
        )

    if not decided and not traces["seen"] and not traces["deliveries"] and not placed and not history:
        lines.append(
            "Бот этот лот не видел: его нет ни в журнале, ни в базе, ни в последних выдачах радаров.\n\n"
            "<blockquote>Значит, он не подходит под ссылку радара (город, категория, цена или фильтры в самой ссылке) "
            "или Авито ещё не показал его в поиске. Откройте ссылку радара в браузере и поищите лот там.</blockquote>"
        )
    elif decided:
        lines.append("<blockquote>Журнал решений в памяти, сбрасывается при перезапуске бота. Время МСК.</blockquote>")
    elif history:
        lines.append(
            "<blockquote>Журнал решений пуст: бот перезапускался или лот старый. Логи хранят находки и отправки, "
            "но не причину отказа (стоп-слово, фильтр). Время МСК.</blockquote>"
        )
    return "\n".join(lines).rstrip()


LOT_HISTORY_LINES = 12


async def _lot_history(item_id: str) -> list[dict]:
    """The lot's finds and deliveries from the kept logs, read by a separate process."""
    import log_report

    try:
        rows = json.loads(await _log_report("--lot", item_id) or "[]")
    except (OSError, RuntimeError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("История лота отдельным процессом не прочитана (%s), читаю в боте", str(exc)[:200])
        rows = await asyncio.to_thread(log_report.lot_history, item_id)
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


@router.message(Command("lot"))
async def cmd_lot(message: Message, state: FSMContext):
    """/lot <ссылка или номер> — почему лот пришёл или не пришёл."""
    if not config.is_admin(message.from_user.id):
        return
    arg = (message.text or "").partition(" ")[2].strip()
    if not arg:
        await state.set_state(AdminStates.waiting_for_lot)
        await message.answer("Пришлите ссылку на объявление Авито или его номер.")
        return
    await _answer_lot(message, arg)


@router.callback_query(F.data == "admin_lot_prompt")
async def cb_admin_lot_prompt(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_lot)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Логи", callback_data="admin_logs_menu")]])
    await ui.show(call, f"{ui.e('scope')} <b>Почему не пришёл лот</b>\n\nПришлите ссылку на объявление Авито или его номер.", kb)


@router.message(AdminStates.waiting_for_lot)
async def msg_admin_lot(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    from handlers.searches import route_menu_text

    if await route_menu_text(message, state):
        return
    await state.clear()
    await _answer_lot(message, message.text or "")


async def _answer_lot(message: Message, text: str) -> None:
    item_id = parse_item_id(text)
    if item_id is None:
        await message.answer("Не нашёл номер объявления. Пришлите ссылку вида avito.ru/…_1234567890 или сам номер.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Ещё раз", callback_data=f"admin_lot:{item_id}")],
    ])
    await message.answer(await explain_lot(item_id), reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("admin_lot:"))
async def cb_admin_lot(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    item_id = call.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Ещё раз", callback_data=call.data)]])
    await ui.show(call, await explain_lot(item_id), kb, disable_web_page_preview=True)


SCANS_PICK_PAGE = 12


@router.callback_query(F.data.startswith("admin_scans_pick:"))
async def cb_admin_scans_pick(call: CallbackQuery):
    """All running radars, newest first: one tap opens the scan journal."""
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        page = max(0, int(call.data.split(":")[1]))
    except (ValueError, IndexError):
        page = 0
    radars = sorted(await database.get_all_active_searches(), key=lambda r: -int(r["id"]))
    pages = max(1, -(-len(radars) // SCANS_PICK_PAGE))
    page = min(page, pages - 1)
    rows = [
        [InlineKeyboardButton(
            text=f"📊 #{r['id']} {(r.get('title') or 'Радар')[:24]} · {r.get('username') or r['user_id']}"[:60],
            callback_data=f"admin_scans:{r['id']}:{r['user_id']}:0",
        )]
        for r in radars[page * SCANS_PICK_PAGE:(page + 1) * SCANS_PICK_PAGE]
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="«", callback_data=f"admin_scans_pick:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="»", callback_data=f"admin_scans_pick:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="« Логи", callback_data="admin_logs_menu")])
    text = (
        f"{ui.e('radar')} <b>Сканы радаров</b>\n\n"
        f"Работающих радаров: <b>{len(radars)}</b>" + (f" · стр. {page + 1}/{pages}" if pages > 1 else "")
        + "\nВыберите радар. Или командой: <code>/scans 12</code>."
    )
    await ui.show(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("scans"))
async def cmd_scans(message: Message):
    """/scans <радар> — журнал сканов; без номера — кнопки радаров администратора."""
    if not config.is_admin(message.from_user.id):
        return
    arg = (message.text or "").partition(" ")[2].strip().lstrip("#")
    if arg.isdigit():
        radar = await database.get_search(int(arg))
        if not radar:
            await message.answer("Радар не найден.")
            return
        owner = await database.get_user(int(radar["user_id"])) or {}
        radar = {**radar, **{k: owner.get(k) for k in ("tariff_plan", "is_allowed", "trial_used", "subscription_until")}}
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin_scans:{radar['id']}:{radar['user_id']}:0")],
        ])
        await ui.show(message, render_scan_journal(radar), kb, disable_web_page_preview=True)
        return
    radars = await database.get_user_searches(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📊 #{r['id']} {(r.get('title') or 'Радар')[:28]}",
                              callback_data=f"admin_scans:{r['id']}:{message.from_user.id}:0")]
        for r in radars[:20]
    ])
    await message.answer(
        "Журнал сканов: <code>/scans 12</code> — номер радара (есть в карточке пользователя → Радары)."
        + ("\n\nВаши радары:" if radars else ""),
        reply_markup=kb if radars else None,
        parse_mode="HTML",
    )


async def _selected_radars(state: FSMContext, radars: list[dict]) -> set[int]:
    """Only this user's radars stay selected, whatever a stale button carries."""
    return set((await state.get_data()).get("admin_rsel") or []) & {r["id"] for r in radars}


async def _radars_delete_screen(call: CallbackQuery, state: FSMContext, target_id: int, page: int, card_page: int) -> None:
    radars = await database.get_user_searches(target_id)
    selected = await _selected_radars(state, radars)
    await state.update_data(admin_rsel=sorted(selected))
    pages = max(1, -(-len(radars) // RADARS_DELETE_PAGE))
    page = max(0, min(page, pages - 1))
    suffix = f"{target_id}:{card_page}"
    rows = []
    first = page * RADARS_DELETE_PAGE
    for i, radar in enumerate(radars[first:first + RADARS_DELETE_PAGE], first + 1):
        mark = "☑" if radar["id"] in selected else "☐"
        title = (radar.get("title") or "Без названия").strip()
        rows.append([ui.btn(f"{mark} {i}. {title[:30]}", callback_data=f"admin_rtog:{radar['id']}:{page}:{suffix}")])
    nav = []
    if page > 0:
        nav.append(ui.btn("«", callback_data=f"admin_rdel:{page - 1}:{suffix}"))
    if page < pages - 1:
        nav.append(ui.btn("»", callback_data=f"admin_rdel:{page + 1}:{suffix}"))
    rows.append(nav)
    rows.append([ui.btn("Выбрать все", callback_data=f"admin_rall:{page}:{suffix}"),
                 ui.btn("Снять все", callback_data=f"admin_rnone:{page}:{suffix}")])
    if selected:
        rows.append([ui.btn(f"Удалить выбранные ({len(selected)})", "trash", callback_data=f"admin_rgo:{suffix}", style="danger")])
    rows.append([ui.btn("« Карточка пользователя", callback_data=f"admin_user:{suffix}")])
    text = (
        f"{ui.e('trash')} <b>Удалить радары пользователя</b>\n"
        f"ID: <code>{target_id}</code>\n"
        f"Выбрано: <b>{len(selected)}</b> из {len(radars)}" + (f" · стр. {page + 1}/{pages}" if pages > 1 else "")
        + "\n\nНажимайте на радары, чтобы отметить их."
    )
    await ui.show(call, text, ui.kb(*rows))


@router.callback_query(F.data.startswith(("admin_rdel:", "admin_rtog:", "admin_rall:", "admin_rnone:")))
async def cb_admin_radars_select(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    action, *args = call.data.split(":")
    try:
        page_arg, target_id, card_page = args[-3], int(args[-2]), int(args[-1])
        radar_id = int(args[0]) if action == "admin_rtog" else None
    except (ValueError, IndexError):
        await call.answer("Некорректная кнопка", show_alert=True)
        return
    selected = set((await state.get_data()).get("admin_rsel") or [])
    if action == "admin_rdel" and page_arg == "new":
        selected, page_arg = set(), "0"
    elif action == "admin_rtog":
        selected ^= {radar_id}
    elif action == "admin_rall":
        selected = {r["id"] for r in await database.get_user_searches(target_id)}
    elif action == "admin_rnone":
        selected = set()
    await state.update_data(admin_rsel=sorted(selected))
    await _radars_delete_screen(call, state, target_id, int(page_arg) if page_arg.isdigit() else 0, card_page)


@router.callback_query(F.data.startswith("admin_rgo:"))
async def cb_admin_radars_confirm(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    _, target_id, card_page = call.data.split(":")
    selected = await _selected_radars(state, await database.get_user_searches(int(target_id)))
    if not selected:
        await call.answer("Ничего не выбрано", show_alert=True)
        return
    await ui.show(call, (
        f"{ui.e('trash')} <b>Удалить {len(selected)} радаров пользователя <code>{target_id}</code>?</b>\n\n"
        "Их стоп-слова тоже удалятся. Отменить это нельзя."
    ), ui.kb([ui.btn("Да, удалить", "trash", callback_data=f"admin_ryes:{target_id}:{card_page}", style="danger"),
              ui.btn("Отмена", callback_data=f"admin_rdel:0:{target_id}:{card_page}")]))


@router.callback_query(F.data.startswith("admin_ryes:"))
async def cb_admin_radars_delete(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    _, target_id, card_page = call.data.split(":")
    target_id = int(target_id)
    selected = await _selected_radars(state, await database.get_user_searches(target_id))
    await state.update_data(admin_rsel=[])
    deleted = 0
    for radar_id in selected:
        deleted += int(await database.delete_search(radar_id, target_id))
    logger.info("👑 [АДМИН] Администратор ID:%s удалил радары пользователя ID:%s: %s из %s", call.from_user.id, target_id, deleted, len(selected))
    await database.log_admin_action(call.from_user.id, target_id, "radars_delete", f"{deleted} шт.")
    await call.answer(f"Удалено радаров: {deleted}")
    text, kb = await render_user_card_data(target_id, int(card_page))
    if text:
        await ui.show(call, text, kb, answer=False)


@router.callback_query(F.data.startswith("admin_revoke_prompt:"))
async def cb_admin_revoke_prompt(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        _, user_id, page = call.data.split(":")
        user_id, page = int(user_id), int(page)
    except (ValueError, IndexError):
        await call.answer("Некорректная кнопка пользователя", show_alert=True)
        return
    if config.is_admin(user_id) or not await database.get_user(user_id):
        await call.answer("Отзыв недоступен", show_alert=True)
        return
    await call.message.edit_text(f"🚫 Отозвать подписку / триал пользователя <code>{user_id}</code>?\n"
                                 "Возврат Stars не выполняется автоматически.", parse_mode="HTML",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [InlineKeyboardButton(text="✅ Отозвать доступ", callback_data=f"admin_revoke:{user_id}:{page}")],
                                     [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user:{user_id}:{page}")],
                                 ]))
    await call.answer()



# ── Панель администратора (/admin, /users, кнопка в меню) ─────────────

USERS_PER_PAGE = 8
USER_FILTERS = {
    "all": "Все",
    "ever": "Была подписка",
    "now": "Активные",
    "paid": "Платили",
    "lost": "Ушедшие",
}

NET_MODE_TITLES = {
    "smart": "🧠 Умный",
    "direct": "🖥 Только сервер",
    "proxy": "🛰 Только прокси",
    "both": "🔀 Сервер + прокси",
    "auto": "🤖 Авто",
}
NET_MODE_HINTS = {
    "smart": "Пока сервер справляется, сканирует он один. Прокси ждут в запасе: не тратят запросы на проверки "
             "и берут скан, только если сервер ошибся. Выросла нагрузка или Авито блокирует сервер — "
             "прокси сами включаются в работу, переключать ничего не нужно.",
    "direct": "Сканирует только IP сервера. Бесплатно, но один IP — ниже скорость и выше риск блокировки.",
    "proxy": "Сканируют только прокси, IP сервера Авито не видит.",
    "both": "Сервер и прокси всегда работают вместе и делят нагрузку, даже когда её мало.",
    "auto": "Прокси, если подключён, иначе IP сервера (режим по умолчанию).",
}
CHANNEL_STATE_ICONS = {"healthy": "🟢", "degraded": "🟡", "half_open": "🟡", "quarantined": "🔴", "draining": "⚪"}


def _admin_event(event):
    """(message, user_id) for both a command message and a button press."""
    if isinstance(event, CallbackQuery):
        return event.message, event.from_user.id
    return event, event.from_user.id


async def _show(event, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edit the screen in place for button presses, send a new message for commands."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception as e:
            if "message is not modified" in str(e).lower():
                return
    message = event.message if isinstance(event, CallbackQuery) else event
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


def _short_until(dt_val) -> str:
    """'до 28.11' in MSK for compact list rows."""
    if not dt_val:
        return ""
    try:
        from datetime import datetime, timedelta
        raw = str(dt_val).strip()
        fmt = "%Y-%m-%d %H:%M:%S" if len(raw) >= 19 else "%Y-%m-%d %H:%M"
        msk = datetime.strptime(raw[:19] if len(raw) >= 19 else raw[:16], fmt) + timedelta(hours=3)
        return f"до {msk:%d.%m}"
    except ValueError:
        return ""


def _channel_label(snap: dict) -> str:
    icon = CHANNEL_STATE_ICONS.get(str(snap.get("state")), "⚪")
    url = str(snap.get("masked_url") or "")
    name = "IP сервера" if url.startswith("direct://") else html.escape(url)
    rate = float(snap.get("effective_rate") or 0.0)
    extra = ""
    if snap.get("state") == "quarantined":
        extra = f" · пауза {int(float(snap.get('quarantine_remaining') or 0))} с"
    return f"{icon} {name} · <code>{rate:.2f}</code> req/s{extra}"


def _net_summary(pool) -> str:
    clients = pool.clients
    if not clients:
        return "⏸ <b>на паузе</b> (нет каналов)"
    return f"{NET_MODE_TITLES.get(config.NET_MODE, config.NET_MODE)} · каналов: <b>{len(clients)}</b>"


@router.message(F.text.in_(ui.menu_texts("admin")))
@router.message(Command("admin"))
@router.callback_query(F.data == "admin_refresh")
async def cmd_admin_home(event):
    message, user_id = _admin_event(event)
    if not config.is_admin(user_id):
        return
    if isinstance(event, CallbackQuery):
        await event.answer()

    from proxy_pool import get_proxy_pool
    from scheduler import get_last_scan_info

    stats = await database.get_system_stats()
    text = (
        f"{ui.e('admin')} <b>Админ-панель</b>\n\n"
        f"{ui.e('users')} Пользователи: <b>{stats['total_users']}</b> · с доступом <b>{stats['allowed_users']}</b>\n"
        f"{ui.e('radar')} Радары: <b>{stats['active_searches']}</b> активных из <b>{stats['total_searches']}</b>\n"
        f"{ui.e('box')} Обработано лотов: <b>{stats['total_seen']}</b>\n"
        f"{ui.e('net')} Сеть: {_net_summary(get_proxy_pool())}\n"
        f"{ui.e('time')} Последняя проверка: <b>{get_last_scan_info()}</b>\n"
        f"{ui.e('palette')} Премиум-эмодзи: <b>{len(ui.IDS)}</b> из {len(ui.SLOTS)}"
        + ("" if ui.ENABLED else " (выключены)")
    )
    kb = ui.kb(
        [ui.btn("Пользователи", "users", callback_data="admin_page:0"),
         ui.btn("Сеть и прокси", "net", callback_data="admin_net")],
        [ui.btn("Мониторинг", "stats", callback_data="admin_runtime_stats"),
         ui.btn("Найти", "scope", callback_data="admin_search_user_prompt")],
        [ui.btn("База", "db", callback_data="admin_backup"),
         ui.btn("Логи", "logs", callback_data="admin_logs_menu")],
        [ui.btn("Отчёт за сутки", "note", callback_data="admin_daily_report"),
         ui.btn("Премиум-эмодзи", "palette", callback_data="admin_emoji")],
        [ui.btn("Цены в Stars", "star", callback_data="admin_prices"),
         ui.btn("Продлить всем", "time", callback_data="admin_extend_all")],
        [ui.btn("Деньги", "coin", callback_data="admin_money"),
         ui.btn("Журнал админов", "write", callback_data="admin_alog")],
        [ui.btn("Обновить", "refresh", callback_data="admin_refresh")],
    )
    await _show(event, text, kb)


# ── Продлить всем (компенсация после техработ) ─────────────────────────

EXTEND_MAX_SECONDS = 30 * 86400
EXTEND_REASON_MAX = 2000
_EXTEND_UNITS = (
    (("д", "дн", "дня", "дней", "день", "d"), 86400),
    (("ч", "час", "часа", "часов", "h"), 3600),
    (("м", "мин", "минут", "минуты", "минуту", "m"), 60),
)


def parse_extend_duration(text: str) -> int:
    """'2ч', '1д 6ч', '30 мин', '3' (часы) -> seconds. ValueError when it can't be read."""
    import re
    raw = (text or "").strip().lower().replace(",", " ")
    if re.fullmatch(r"\d+", raw):
        seconds = int(raw) * 3600
    else:
        parts = re.findall(r"(\d+)\s*([a-zа-яё]+)", raw)
        if not parts or re.sub(r"(\d+)\s*([a-zа-яё]+)", "", raw).strip():
            raise ValueError("bad duration")
        seconds = 0
        for number, unit in parts:
            size = next((s for names, s in _EXTEND_UNITS if unit in names), None)
            if size is None:
                raise ValueError("bad unit")
            seconds += int(number) * size
    if not 0 < seconds <= EXTEND_MAX_SECONDS:
        raise ValueError("out of range")
    return seconds


def format_extend_duration(seconds: int) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(config.plural(days, "день", "дня", "дней"))
    if hours:
        parts.append(config.hours_word(hours))
    if minutes:
        parts.append(config.plural(minutes, "минуту", "минуты", "минут"))
    return " ".join(parts) or "0 минут"


def extend_notice_text(seconds: int, reason: str, until: str) -> str:
    return (
        f"{ui.e('trial')} <b>Мы добавили вам {format_extend_duration(seconds)}</b>\n\n"
        f"{html.escape(reason)}\n\n"
        f"Доступ до: <b>{database.format_subscription_date(until)}</b>"
    )


def _extend_cancel_kb() -> InlineKeyboardMarkup:
    return ui.kb([ui.btn("Отмена", "cancel", callback_data="admin_extend_cancel")])


@router.callback_query(F.data == "admin_extend_all")
async def cb_admin_extend_all(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_for_extend_time)
    await call.answer()
    count = await database.count_active_subscriptions()
    await _show(
        call,
        f"{ui.e('time')} <b>Продлить всем</b>\n\n"
        f"Время добавится всем, у кого сейчас идёт подписка или пробный период: <b>{count}</b>.\n\n"
        "Сколько добавить? Пришлите, например: <code>2ч</code>, <code>30м</code>, <code>1д 6ч</code>. "
        "Просто число — это часы. Не больше 30 дней.",
        _extend_cancel_kb(),
    )


@router.callback_query(F.data == "admin_extend_cancel")
async def cb_admin_extend_cancel(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await state.clear()
    await call.answer("Отменено")
    await cmd_admin_home(call)


@router.message(AdminStates.waiting_for_extend_time)
async def msg_admin_extend_time(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    from handlers.searches import route_menu_text
    if await route_menu_text(message, state):
        return
    try:
        seconds = parse_extend_duration(message.text or "")
    except ValueError:
        await message.answer(
            "Не понял срок. Примеры: <code>2ч</code>, <code>30м</code>, <code>1д 6ч</code>, <code>3</code> (часы). "
            "Не больше 30 дней.",
            parse_mode="HTML", reply_markup=_extend_cancel_kb(),
        )
        return
    await state.update_data(extend_seconds=seconds)
    await state.set_state(AdminStates.waiting_for_extend_reason)
    await message.answer(
        f"{ui.e('write')} <b>Добавим {format_extend_duration(seconds)}</b>\n\n"
        "Напишите, за что добавили время. Этот текст получат все пользователи.\n"
        "<blockquote>Например: «Извините за технические работы 3 ноября с 14:00 до 16:00, "
        "бот не присылал лоты. Добавили время, которое вы потеряли.»</blockquote>",
        parse_mode="HTML", reply_markup=_extend_cancel_kb(),
    )


@router.message(AdminStates.waiting_for_extend_reason)
async def msg_admin_extend_reason(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    from handlers.searches import route_menu_text
    if await route_menu_text(message, state):
        return
    reason = (message.text or "").strip()
    if not reason or len(reason) > EXTEND_REASON_MAX:
        await message.answer(
            f"Пришлите текст сообщения, до {EXTEND_REASON_MAX} символов.", reply_markup=_extend_cancel_kb()
        )
        return
    data = await state.get_data()
    seconds = int(data.get("extend_seconds") or 0)
    if seconds <= 0:
        await state.clear()
        await message.answer("Срок потерялся, начните заново.", reply_markup=ui.kb(
            [ui.btn("Продлить всем", "time", callback_data="admin_extend_all")]))
        return
    await state.update_data(extend_reason=reason)
    count = await database.count_active_subscriptions()
    from datetime import datetime, timedelta, timezone
    sample_until = (datetime.now(timezone.utc) + timedelta(days=7, seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    await message.answer(
        f"{ui.e('time')} <b>Проверьте перед отправкой</b>\n\n"
        f"Получат: <b>{count}</b> · добавим <b>{format_extend_duration(seconds)}</b>\n\n"
        "Так будет выглядеть сообщение (дата у каждого своя):\n"
        f"<blockquote>{extend_notice_text(seconds, reason, sample_until)}</blockquote>",
        parse_mode="HTML",
        reply_markup=ui.kb(
            [ui.btn("Продлить и отправить", "ok", callback_data="admin_extend_go", style="success")],
            [ui.btn("Отмена", "cancel", callback_data="admin_extend_cancel")],
        ),
    )


@router.callback_query(F.data == "admin_extend_go")
async def cb_admin_extend_go(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    data = await state.get_data()
    seconds = int(data.get("extend_seconds") or 0)
    reason = str(data.get("extend_reason") or "")
    await state.clear()  # a second press must not extend twice
    if seconds <= 0 or not reason:
        await call.answer("Данные устарели, начните заново", show_alert=True)
        return
    await call.answer("Продлеваю…")
    granted = await database.extend_active_subscriptions(seconds)
    logger.info("Админ %s продлил всем %s с: пользователей %s", call.from_user.id, seconds, len(granted))
    await database.log_admin_action(call.from_user.id, None, "extend_all", f"{seconds} с, пользователей {len(granted)}")
    await _show(call, f"{ui.e('wait')} Время добавлено: <b>{len(granted)}</b>. Рассылаю сообщения…", ui.kb())

    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
    delivered = failed = 0
    for user_id, until in granted:
        text = extend_notice_text(seconds, reason, until)
        for attempt in range(2):
            try:
                await bot.send_message(user_id, text, parse_mode="HTML")
                delivered += 1
                break
            except TelegramRetryAfter as exc:
                if attempt:
                    failed += 1
                else:
                    await asyncio.sleep(float(exc.retry_after) + 0.5)
            except TelegramForbiddenError:
                failed += 1
                break
            except Exception as exc:
                logger.warning("Сообщение о продлении не отправлено %s: %s", user_id, exc)
                failed += 1
                break
        await asyncio.sleep(0.05)  # ~20 messages per second, under Telegram's limit

    await _show(
        call,
        f"{ui.e('ok')} <b>Готово</b>\n\n"
        f"Добавили {format_extend_duration(seconds)}: <b>{len(granted)}</b>\n"
        f"Сообщение получили: <b>{delivered}</b>"
        + (f" · не доставлено: <b>{failed}</b> (бот заблокирован или ошибка)" if failed else ""),
        ui.kb([ui.btn("В админку", "admin", callback_data="admin_refresh")]),
    )


# ── Цены в Telegram Stars ──────────────────────────────────────────────

def _stars_prices_screen() -> tuple[str, InlineKeyboardMarkup]:
    prices = "\n".join(
        f"{config.plan_title(plan)}: <b>{config.stars_price(plan)}</b> ⭐ · 3 мес: {config.stars_price(plan, 3)} ⭐"
        for plan in config.PAID_PLANS
    )
    text = (
        f"{ui.e('star')} <b>Цены в Stars</b> за 30 дней\n\n{prices}\n\n"
        f"<i>Цена за 3 месяца считается сама: месяц × 3 − {config.PERIOD_DISCOUNT_PERCENT[3]}%, округлённо до …9 или …99.</i>\n\n"
        "<blockquote>Новые цены действуют сразу. Счета со старой ценой оплатить нельзя: клиент откроет тариф заново. "
        "Цены в рублях на экране тарифов не меняются.</blockquote>"
    )
    return text, ui.kb(
        [ui.btn("Изменить цены", "write", callback_data="admin_prices_edit", style="primary")],
        [ui.btn("В админку", "admin", callback_data="admin_refresh")],
    )


@router.callback_query(F.data == "admin_prices")
async def cb_admin_prices(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await _show(call, *_stars_prices_screen())


@router.callback_query(F.data == "admin_prices_edit")
async def cb_admin_prices_edit(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_stars_prices)
    await call.answer()
    current = " ".join(str(config.stars_price(plan)) for plan in config.PAID_PLANS)
    await _show(
        call,
        f"{ui.e('write')} <b>Новые цены в Stars</b>\n\n"
        "Пришлите три числа через пробел: Базовый, Средний, Полный. Каждый следующий тариф дороже предыдущего.\n\n"
        f"Сейчас: <code>{current}</code>",
        ui.kb([ui.btn("Отмена", "cancel", callback_data="admin_prices")]),
    )


@router.message(AdminStates.waiting_for_stars_prices)
async def msg_admin_stars_prices(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    from handlers.searches import route_menu_text
    if await route_menu_text(message, state):
        return
    parts = (message.text or "").replace(",", " ").split()
    try:
        prices = [int(part) for part in parts]
        if len(prices) != 3:
            raise ValueError
        config.apply_stars_prices(*prices)
    except ValueError:
        await message.answer(
            f"Нужны три целых числа по возрастанию, не больше {config.STARS_PRICE_MAX}. Например: <code>199 449 899</code>",
            parse_mode="HTML", reply_markup=ui.kb([ui.btn("Отмена", "cancel", callback_data="admin_prices")]),
        )
        return
    await database.set_bot_setting("stars_prices", json.dumps(prices))
    await state.clear()
    logger.info("Цены Stars изменены: admin=%s prices=%s", message.from_user.id, prices)
    await database.log_admin_action(message.from_user.id, None, "prices", str(prices))
    await _show(message, *_stars_prices_screen())


@router.message(Command("users"))
@router.callback_query(F.data.startswith("admin_page:"))
async def cmd_admin_panel(event, page: int = 0):
    """Список пользователей: одна строка и одна кнопка на человека, все действия — в карточке."""
    message, user_id = _admin_event(event)
    user_filter = "all"
    if isinstance(event, CallbackQuery):
        if event.data.startswith("admin_page:"):
            parts = event.data.split(":")
            try:
                page = int(parts[1])
            except (ValueError, IndexError):
                page = 0
            if len(parts) > 2 and parts[2] in USER_FILTERS:
                user_filter = parts[2]
        await event.answer()
    if not config.is_admin(user_id):
        return

    all_users, ever_ids, paid_ids, allowed = await _filtered_users(user_filter)
    total_pages = max(1, (len(all_users) + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = min(max(0, page), total_pages - 1)
    start_idx = page * USERS_PER_PAGE
    page_users = all_users[start_idx:start_idx + USERS_PER_PAGE]
    suffix = "" if user_filter == "all" else f":{user_filter}"

    page_info = f" · стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    lines = [f"👥 <b>Пользователи</b> · {USER_FILTERS[user_filter]} ({len(all_users)}){page_info}", ""]
    buttons = []
    for idx, u in enumerate(page_users, start=start_idx + 1):
        u_id = u["user_id"]
        fname = u.get("first_name") or "Без имени"
        tag = f"@{u['username']}" if u.get("username") else str(u_id)
        if config.is_admin(u_id):
            icon, status = "👑", "админ"
        elif allowed.get(u_id):
            trial = not u.get("is_allowed") and u.get("trial_used")
            icon = "🎁" if trial else "🟢"
            status = "триал" if trial else config.plan_title(u.get("tariff_plan"))
            until = _short_until(u.get("subscription_until"))
            status = f"{status} {until}".strip()
        elif u_id in paid_ids:
            icon, status = "🔴", "нет доступа · платил"
        elif u_id in ever_ids:
            icon, status = "🔴", "нет доступа · был триал"
        else:
            icon, status = "⚪️", "не пробовал"
        lines.append(f"{idx}. {icon} <b>{html.escape(fname)}</b> {html.escape(tag)} — {status}")
        label = f"{icon} {fname[:18]} · {tag[:16]}"
        callback = "admin_noop" if config.is_admin(u_id) else f"admin_user:{u_id}:{page}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=callback)])

    if not page_users:
        lines.append("Никого нет.")
    lines.append("\nНажмите на пользователя: подписка, радары, платежи, блок и удаление — в карточке.")

    if total_pages > 1:
        buttons.append([
            InlineKeyboardButton(text="◀️", callback_data=f"admin_page:{page - 1}{suffix}" if page > 0 else "admin_noop"),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="admin_noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"admin_page:{page + 1}{suffix}" if page < total_pages - 1 else "admin_noop"),
        ])
    filter_buttons = [
        InlineKeyboardButton(text=("• " if key == user_filter else "") + title, callback_data=f"admin_page:0:{key}")
        for key, title in USER_FILTERS.items()
    ]
    buttons.append(filter_buttons[:3])
    buttons.append(filter_buttons[3:])
    buttons.append([InlineKeyboardButton(text=f"📣 Рассылка: {USER_FILTERS[user_filter]}", callback_data=f"admin_bc:{user_filter}")])
    buttons.append([
        InlineKeyboardButton(text="🔍 Найти", callback_data="admin_search_user_prompt"),
        InlineKeyboardButton(text="⬅️ Меню", callback_data="admin_refresh"),
    ])
    await _show(event, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


async def _filtered_users(user_filter: str):
    """(users of the filter, ever-subscribed ids, paying ids, {user_id: has access now}). Admins only in «Все»."""
    all_users = await database.get_all_users()
    ever_ids = await database.get_ever_subscribed_user_ids()
    paid_ids = await database.get_paid_user_ids()
    allowed = {u["user_id"]: await database.is_user_allowed(u["user_id"]) for u in all_users}
    keep = {
        "ever": lambda uid: uid in ever_ids,
        "paid": lambda uid: uid in paid_ids,
        "now": lambda uid: allowed.get(uid),
        "lost": lambda uid: uid in ever_ids and not allowed.get(uid),
    }.get(user_filter)
    if keep is not None:
        all_users = [u for u in all_users if keep(u["user_id"]) and not config.is_admin(u["user_id"])]
    return all_users, ever_ids, paid_ids, allowed


# ── Рассылка по фильтру пользователей ──────────────────────────────────

BROADCAST_PAUSE = 0.05  # 20 messages a second, under Telegram's 30/s for bots
BROADCAST_TASKS: set = set()


@router.callback_query(F.data.startswith("admin_bc:"))
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    """admin_bc:<filter> — ask for the text of a broadcast to this user group."""
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    user_filter = call.data.split(":", 1)[1]
    if user_filter not in USER_FILTERS:
        await call.answer("Неизвестный фильтр", show_alert=True)
        return
    users = [u for u in (await _filtered_users(user_filter))[0] if not config.is_admin(u["user_id"])]
    await state.set_state(AdminStates.waiting_for_broadcast)
    await state.update_data(bc_filter=user_filter)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_page:0:{user_filter}")]])
    await ui.show(
        call,
        f"📣 <b>Рассылка</b> · {USER_FILTERS[user_filter]} ({len(users)} чел.)\n\n"
        "Пришлите текст сообщения. Форматирование (жирный, ссылки) сохранится.\n"
        "<blockquote>Перед отправкой покажу, как оно выглядит, и спрошу подтверждение. Админам рассылка не уходит.</blockquote>",
        kb,
    )


@router.message(AdminStates.waiting_for_broadcast)
async def msg_admin_broadcast(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    from handlers.searches import route_menu_text

    if await route_menu_text(message, state):
        return
    text = (message.html_text or "").strip()
    if not text:
        await message.answer("Нужен текст. Пришлите сообщение словами.")
        return
    data = await state.get_data()
    user_filter = data.get("bc_filter") or "all"
    users = [u for u in (await _filtered_users(user_filter))[0] if not config.is_admin(u["user_id"])]
    await state.update_data(bc_text=text[:4000])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Отправить {len(users)} чел.", callback_data="admin_bc_go")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_page:0:{user_filter}")],
    ])
    await message.answer(text[:4000], parse_mode="HTML", disable_web_page_preview=True)
    await message.answer(
        f"☝️ Так увидят сообщение <b>{len(users)}</b> чел. ({USER_FILTERS[user_filter]}). Отправить?",
        reply_markup=kb, parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_bc_go")
async def cb_admin_broadcast_go(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    text, user_filter = data.get("bc_text"), data.get("bc_filter") or "all"
    if not text:
        await call.answer("Текст рассылки потерян, начните заново", show_alert=True)
        return
    users = [u["user_id"] for u in (await _filtered_users(user_filter))[0] if not config.is_admin(u["user_id"])]
    await call.answer("Рассылка началась")
    await ui.show(call, f"📣 Рассылка {len(users)} чел. идёт. Пришлю итог, когда закончу.", None)
    await database.log_admin_action(call.from_user.id, None, "broadcast", f"{USER_FILTERS[user_filter]}, {len(users)} чел.: {text[:80]}")
    task = asyncio.create_task(_run_broadcast(bot, call.from_user.id, users, text))
    BROADCAST_TASKS.add(task)
    task.add_done_callback(BROADCAST_TASKS.discard)


async def _run_broadcast(bot: Bot, admin_id: int, user_ids: list[int], text: str) -> dict:
    """Sends one by one at BROADCAST_PAUSE; waits out flood limits; reports the totals to the admin."""
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

    sent = blocked = failed = 0
    for user_id in user_ids:
        for _ in range(3):
            try:
                await bot.send_message(user_id, text, parse_mode="HTML", disable_web_page_preview=True)
                sent += 1
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1)
            except TelegramForbiddenError:
                blocked += 1
                break
            except Exception as exc:
                failed += 1
                logger.warning("Рассылка: не отправлено %s: %s", user_id, type(exc).__name__)
                break
        await asyncio.sleep(BROADCAST_PAUSE)
    result = {"sent": sent, "blocked": blocked, "failed": failed}
    logger.info("Рассылка от админа %s: %s", admin_id, result)
    try:
        await bot.send_message(
            admin_id,
            f"📣 <b>Рассылка закончена</b>\n\nДоставлено: <b>{sent}</b>\nЗаблокировали бота: {blocked}\nОшибки: {failed}",
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("Не удалось отправить итог рассылки", exc_info=True)
    return result


# ── Деньги ───────────────────────────────────────────────────────────

def _share(part: int, total: int) -> str:
    return f"{100 * part / total:.0f}%" if total else "—"


@router.message(Command("money"))
@router.callback_query(F.data == "admin_money")
async def cb_admin_money(event):
    message, user_id = _admin_event(event)
    if not config.is_admin(user_id):
        return
    stats = await database.get_money_stats()
    revenue, funnel = stats["revenue"], stats["funnel"]

    revenue_rub = stats["revenue_rub"]

    def period(key: str, title: str) -> str:
        orders, stars, payers = revenue[key]
        rub_orders, rubles, rub_payers = revenue_rub[key]
        return (f"{title}: <b>{rubles} ₽</b> ({rub_orders} опл.) · <b>{stars} ⭐</b> ({orders} опл.) · "
                f"платили {payers + rub_payers} чел.")

    plans = ", ".join(
        f"«{config.plan_title(p)}» {n} шт. на {s} {'₽' if kind == 'rub' else '⭐'}" for p, kind, n, s in stats["by_plan"]
    ) or "нет"
    trouble = []
    if stats["rub_disputed"]:
        trouble.append(f"⚠️ Споров по оплатам: <b>{stats['rub_disputed']}</b> — доказательства в карточке пользователя")
    if stats["rub_review"]:
        trouble.append(f"⚠️ На сверке: <b>{stats['rub_review']}</b> (сумма не сошлась или срок менялся после возврата)")
    if stats["rub_refunded"]:
        trouble.append(f"Возвращено рублёвых оплат: {stats['rub_refunded']}")
    lines = [
        f"{ui.e('coin')} <b>Деньги</b>",
        "",
        period("day", "Сутки"),
        period("week", "Неделя"),
        period("month", "30 дней"),
        period("all", "Всё время"),
        f"По тарифам за 30 дней: {plans}",
        *([""] + trouble if trouble else []),
        "",
        "<b>Воронка за 30 дней</b>",
        f"Новых: <b>{funnel['new']}</b> → попробовали (триал или выдача): <b>{funnel['tried']}</b> "
        f"({_share(funnel['tried'], funnel['new'])}) → оплатили: <b>{funnel['paid']}</b> ({_share(funnel['paid'], funnel['new'])})",
        "",
        "<b>Сейчас</b>",
        f"С подпиской: <b>{stats['active_paid']}</b> · на триале: <b>{stats['active_trial']}</b>",
        f"Платили хоть раз: <b>{stats['payers']}</b>, из них продлевали: <b>{stats['renewed']}</b> ({_share(stats['renewed'], stats['payers'])})",
        f"Доступ закончился за 30 дней: <b>{stats['lapsed']}</b>, из них платившие: <b>{stats['lapsed_paid']}</b>",
        "",
        "<blockquote>Суммы — оплаты без возвращённых; рубли — цена тарифа, без комиссии платёжной системы. "
        "«С подпиской» включает выданные админом.</blockquote>",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Написать ушедшим", callback_data="admin_bc:lost")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_money"),
         InlineKeyboardButton(text="⬅️ Меню", callback_data="admin_refresh")],
    ])
    if isinstance(event, CallbackQuery):
        await ui.show(event, "\n".join(lines), kb)
    else:
        await message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_revoke:"))
async def cb_admin_revoke(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав", show_alert=True)
        return

    parts = call.data.split(":")
    target_user_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0

    if config.is_admin(target_user_id):
        await call.answer("Нельзя отозвать доступ администратора", show_alert=True)
        return
    await database.set_user_allowed(target_user_id, 0)
    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} отозвал доступ у пользователя ID:{target_user_id}")
    await database.log_admin_action(call.from_user.id, target_user_id, "revoke")
    await call.answer("Доступ отозван")

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "⚠️ <b>Ваш доступ к AVITO RADAR был приостановлен администратором.</b>\n\n"
                "Если у вас есть вопросы или возникла ошибка, свяжитесь со службой поддержки:"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="support_start_ticket")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning("Не удалось уведомить пользователя %s об отзыве доступа: %s", target_user_id, e)

    await cmd_admin_panel(call, page=page)


@router.callback_query(F.data.startswith("admin_purge_user:"))
async def cb_admin_purge_user(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    parts = call.data.split(":")
    target_user_id = parts[1]
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    # The button only asks; the user is erased by «Да, удалить» (admin_purge_yes).
    user = await database.get_user(int(target_user_id)) if target_user_id.isdigit() else None
    who = f"<code>{target_user_id}</code>"
    if user:
        name = html.escape(user.get("first_name") or "")
        username = f" @{html.escape(user['username'])}" if user.get("username") else ""
        who = f"{name}{username} · {who}".strip(" ·")
    await _show(call, (
        f"{ui.e('warn')} <b>Точно удалить пользователя?</b>\n\n"
        f"{who}\n\n"
        "Сотрутся пользователь, его радары, история лотов и логи. Отменить нельзя."
    ), InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"admin_purge_yes:{target_user_id}:{page}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user:{target_user_id}:{page}")],
    ]))
    await call.answer()


@router.callback_query(F.data.startswith("admin_purge_yes:"))
async def cb_admin_purge_user_yes(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    parts = call.data.split(":")
    target_user_id = parts[1]
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    res = await database.purge_user_by_pattern(target_user_id)
    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} стер пользователя ID:{target_user_id}: {res['message']}")
    await database.log_admin_action(call.from_user.id, target_user_id, "purge")
    await call.answer(res["message"][:200], show_alert=True)
    await cmd_admin_panel(call, page=page)


@router.message(Command("delete_user"))
@router.message(Command("purge_user"))
async def cmd_purge_user(message: Message):
    if not config.is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "⚠️ <b>Использование команды:</b>\n"
            "<code>/delete_user @username</code> или <code>/delete_user 12345678</code>\n\n"
            "<i>Полностью стирает пользователя, его радары, историю лотов и логи из системы.</i>",
            parse_mode="HTML"
        )
        return

    pattern = parts[1].strip()
    res = await database.purge_user_by_pattern(pattern)
    if res["success"]:
        await message.answer(f"✅ <b>Пользователь успешно удален из системы!</b>\n\n{html.escape(res['message'])}", parse_mode="HTML")
    else:
        await message.answer(f"⚠️ {html.escape(res['message'])}", parse_mode="HTML")



# ── Выгрузка базы данных SQLite ────────────────────────────────────────

@router.message(Command("backup"))
@router.message(Command("db"))
@router.callback_query(F.data == "admin_backup")
async def cmd_admin_backup(event):
    user_id = event.from_user.id
    if not config.is_admin(user_id):
        return

    if isinstance(event, CallbackQuery):
        message = event.message
        await event.answer("Формирую бэкап базы данных...")
    else:
        message = event

    import os
    from aiogram.types import BufferedInputFile

    backup_path = database.create_database_backup_file()
    if not backup_path or not os.path.exists(backup_path):
        await message.answer("❌ Не удалось создать резервную копию базы данных.")
        return

    try:
        with open(backup_path, "rb") as f:
            data = f.read()

        filename = os.path.basename(backup_path)
        file_to_send = BufferedInputFile(data, filename=filename)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Скачать логи", callback_data="admin_logs_menu"),
                InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_refresh")
            ]
        ])

        await message.answer_document(
            document=file_to_send,
            caption=(
                "💾 <b>Резервная копия базы данных SQLite (WAL-безопасная)</b>\n\n"
                f"📦 Размер: <b>{len(data):,} байт</b> ({len(data) / 1024:.1f} KB)\n"
                "Файл полностью консистентен и готов к восстановлению или анализу."
            ),
            reply_markup=kb,
            parse_mode="HTML"
        )
        logger.info(f"👑 [АДМИН] Администратор ID:{user_id} выгрузил бэкап базы данных ({filename})")
    finally:
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except Exception:
                pass


# ── Выгрузка системных логов ──────────────────────────────────────────

@router.message(Command("logs"))
@router.message(Command("log"))
@router.callback_query(F.data == "admin_logs_menu")
async def cmd_admin_logs_menu(event):
    user_id = event.from_user.id
    if not config.is_admin(user_id):
        return

    if isinstance(event, CallbackQuery):
        message = event.message
        await event.answer()
    else:
        message = event

    debug_left = logger_setup.debug_mode_remaining()
    menu_text = (
        f"{ui.e('logs')} <b>Логи</b>\n\n"
        "<b>Отчёт для отладки</b> — главное за час одним файлом: причины сбоев, каналы, ошибки. "
        "Его достаточно прислать разработчику.\n"
        "<b>Готовность к продаже</b> — вердикт за сутки: скорость, надёжность, прокси, ссылки. "
        f"За другой срок: /ready 168 (часы, до {READY_MAX_HOURS}).\n"
        "<b>Сканы радаров</b> — как часто проверяется каждый радар и кто задержал лот: Авито или бот.\n"
        "<b>Почему не пришёл лот</b> — пришлите ссылку на объявление: видел ли его бот и что решил.\n"
        "<b>Подробно на 10 мин</b> — в events.jsonl пишутся все шаги сканов.\n\n"
        "• bot.log — что делал бот, по строке на событие\n"
        "• errors.log — только то, что требует внимания\n"
        "• user_actions.log — действия пользователей\n"
        "• events.jsonl — все события для разбора\n"
        "• metrics.jsonl — показатели по минутам"
    )
    if debug_left > 0:
        menu_text += f"\n\n{ui.e('time')} Подробный режим: ещё {int(debug_left // 60) + 1} мин."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ui.btn("Отчёт для отладки", "stats", callback_data="admin_log_get:report", style="primary")],
        [ui.btn("Готовность к продаже", "ok", callback_data="admin_log_get:ready", style="success")],
        [ui.btn("Сканы радаров", "radar", callback_data="admin_scans_pick:0")],
        [ui.btn("Почему не пришёл лот", "scope", callback_data="admin_lot_prompt")],
        [ui.btn("Выключить подробный режим" if debug_left > 0 else "Подробно на 10 мин", "scope",
                callback_data="admin_log_debug")],
        [ui.btn("Все логи (ZIP)", "box", callback_data="admin_log_get:all_zip")],
        [
            InlineKeyboardButton(text="bot.log", callback_data="admin_log_get:bot"),
            InlineKeyboardButton(text="errors.log", callback_data="admin_log_get:errors")
        ],
        [
            InlineKeyboardButton(text="user_actions.log", callback_data="admin_log_get:user_actions"),
            InlineKeyboardButton(text="events.jsonl", callback_data="admin_log_get:events")
        ],
        [InlineKeyboardButton(text="metrics.jsonl", callback_data="admin_log_get:metrics")],
        [
            ui.btn("Скачать БД", "db", callback_data="admin_backup"),
            ui.btn("В админку", "admin", callback_data="admin_refresh")
        ]
    ])

    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(menu_text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass

    await message.answer(menu_text, reply_markup=kb, parse_mode="HTML")


def _debug_live_state() -> dict:
    """The current picture for the debug report: no user data and no proxy passwords."""
    from monitoring import MONITORING_STATS

    keys = ("requested_rps", "planned_rps", "available_rps", "overloaded", "healthy_proxies", "fanout_queue_depth",
            "outbox", "successful_scans", "failed_scans", "fast_skipped_scans", "coverage_gaps")
    state = {key: MONITORING_STATS.get(key) for key in keys}
    state["net_mode"] = config.NET_MODE
    state["debug_left_s"] = int(logger_setup.debug_mode_remaining())
    state["channels"] = [
        {key: proxy.get(key) for key in ("masked_url", "state", "effective_rate", "quarantine_reason", "quarantine_remaining")}
        for proxy in MONITORING_STATS.get("proxies") or []
    ]
    return state


LOG_REPORT_TIMEOUT = 600.0
READY_MAX_HOURS = 168  # a week; the logs keep 30 days
ZIP_LOG_HOURS = 48
ZIP_LOG_MAX_BYTES = 45 * 1024 * 1024  # Telegram takes a bot's document up to 50 MB


async def _log_report(*args: str, live_state: dict | None = None) -> str:
    """log_report.py in a process of its own: reading weeks of logs must not hold the bot's GIL."""
    import sys

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", str(config.BASE_DIR / "log_report.py"), *args,
        *(["--live-stdin"] if live_state is not None else []),
        stdin=asyncio.subprocess.PIPE if live_state is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(config.BASE_DIR),
    )
    payload = json.dumps(live_state, ensure_ascii=False, default=str).encode("utf-8") if live_state is not None else None
    try:
        out, err = await asyncio.wait_for(process.communicate(payload), LOG_REPORT_TIMEOUT)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "replace")[-500:] or f"exit code {process.returncode}")
    return out.decode("utf-8").removesuffix("\n")


async def _report_text(args: tuple, build, *build_args) -> str:
    """The report from a separate process; in this process if that one failed."""
    try:
        return await _log_report(*args, live_state=_debug_live_state())
    except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
        logger.warning("Отчёт отдельным процессом не построен (%s), строю в боте", str(exc)[:200])
        return await asyncio.to_thread(build, *build_args, _debug_live_state())


async def _send_debug_report(message: Message, minutes: int) -> None:
    from datetime import datetime
    from aiogram.types import BufferedInputFile
    import log_report

    report = await _report_text(("--minutes", str(minutes)), log_report.build_report, minutes)
    name = f"avito_report_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    await message.answer_document(
        BufferedInputFile(report.encode("utf-8"), filename=name),
        caption=f"{ui.e('stats')} Отчёт за {minutes} мин.",
        parse_mode="HTML",
    )


async def _send_readiness_report(message: Message, hours: int) -> None:
    from datetime import datetime
    from aiogram.types import BufferedInputFile
    import log_report

    report = await _report_text(("--ready", str(hours)), log_report.build_readiness, hours)
    verdict = report.split("## Вердикт\n", 1)[-1].split("\n", 1)[0]
    name = f"avito_ready_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    await message.answer_document(
        BufferedInputFile(report.encode("utf-8"), filename=name),
        caption=f"{ui.e('stats')} Готовность за {hours} ч.\n{html.escape(verdict)}",
        parse_mode="HTML",
    )


@router.message(Command("ready"))
async def cmd_admin_ready(message: Message):
    if not config.is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    hours = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 24
    await _send_readiness_report(message, max(1, min(hours, READY_MAX_HOURS)))


@router.message(Command("report"))
async def cmd_admin_report(message: Message):
    if not config.is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 60
    await _send_debug_report(message, max(1, min(minutes, 2880)))


@router.callback_query(F.data == "admin_log_debug")
async def cb_admin_log_debug(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    logger_setup.set_debug_mode(0 if logger_setup.debug_mode_remaining() > 0 else 10)
    await cmd_admin_logs_menu(call)


@router.callback_query(F.data.startswith("admin_log_get:"))
async def cb_admin_log_get(call: CallbackQuery):
    user_id = call.from_user.id
    if not config.is_admin(user_id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    target = call.data.split(":")[1]
    await call.answer("Формирую файл логов...")

    if target == "report":
        await _send_debug_report(call.message, 60)
        return
    if target == "ready":
        await _send_readiness_report(call.message, 24)
        return

    import io
    import os
    import zipfile
    from datetime import datetime
    from pathlib import Path
    from aiogram.types import BufferedInputFile

    base_dir = config.BASE_DIR
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1. Выгрузка ВСЕХ логов единым ZIP-архивом
    if target == "all_zip":
        def build_zip() -> tuple[bytes, int, int, int]:
            zip_buffer = io.BytesIO()
            total, count, skipped_files = 0, 0, 0
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                # Общий bot.log
                bot_log = base_dir / "bot.log"
                if bot_log.exists() and bot_log.is_file():
                    sz = bot_log.stat().st_size
                    if sz > 0:
                        zf.write(bot_log, arcname="bot.log")
                        total += sz
                        count += 1

                # Файлы из папки logs/: свежие первыми, за ZIP_LOG_HOURS и не больше ZIP_LOG_MAX_BYTES —
                # архив должен пройти в Telegram; вся история (30 дней) остаётся на сервере.
                logs_dir = base_dir / "logs"
                if logs_dir.exists() and logs_dir.is_dir():
                    cutoff = datetime.now().timestamp() - ZIP_LOG_HOURS * 3600
                    candidates = sorted(
                        (p for p in logs_dir.iterdir() if p.is_file() and p.stat().st_size > 0),
                        key=lambda p: p.stat().st_mtime, reverse=True,
                    )
                    for p in candidates:
                        sz = p.stat().st_size
                        if p.stat().st_mtime < cutoff or total + sz > ZIP_LOG_MAX_BYTES:
                            skipped_files += 1
                            continue
                        # .gz is already packed: stored as is instead of deflating it again.
                        zf.write(p, arcname=f"logs/{p.name}",
                                 compress_type=zipfile.ZIP_STORED if p.suffix == ".gz" else zipfile.ZIP_DEFLATED)
                        total += sz
                        count += 1
            return zip_buffer.getvalue(), total, count, skipped_files

        # Off the event loop: packing tens of megabytes takes seconds.
        zip_bytes, total_raw_size, files_count, skipped = await asyncio.to_thread(build_zip)
        if files_count == 0:
            await call.message.answer("⚠️ Файлы логов в системе пока отсутствуют или пусты.")
            return

        zip_filename = f"avito_radar_logs_{timestamp}.zip"
        file_to_send = BufferedInputFile(zip_bytes, filename=zip_filename)

        compression_pct = round((1 - len(zip_bytes) / max(total_raw_size, 1)) * 100)

        await call.message.answer_document(
            document=file_to_send,
            caption=(
                f"📦 <b>Архив логов за {ZIP_LOG_HOURS} ч</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🗂 Файлов в архиве: <b>{files_count}</b>\n"
                f"📊 Исходный размер: <b>{total_raw_size / 1024:.1f} KB</b>\n"
                f"🗜 Размер архива: <b>{len(zip_bytes) / 1024:.1f} KB</b> (сжатие {compression_pct}%)\n"
                f"📅 Дата выгрузки: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
                + (f"\n🕰 Не вошли {skipped} старых файлов: история за {logger_setup.LOG_KEEP_DAYS} дн. "
                   "лежит на сервере в logs/" if skipped else "")
            ),
            parse_mode="HTML"
        )
        logger.info(f"👑 [АДМИН] Администратор ID:{user_id} выгрузил полный ZIP-архив логов ({zip_filename})")
        return

    # 2. Выгрузка конкретного файла логов
    log_map = {
        "bot": (base_dir / "bot.log", "📄 bot.log (общий консольный лог)"),
        "user_actions": (base_dir / "logs" / "user_actions.log", "👥 user_actions.log (активность пользователей)"),
        "errors": (base_dir / "logs" / "errors.log", "⚠️ errors.log (журнал ошибок и сбоев)"),
        "events": (logger_setup.EVENTS_LOG, "🧾 events.jsonl (все события для разбора)"),
        "metrics": (logger_setup.METRICS_LOG, "📈 metrics.jsonl (показатели по минутам)"),
    }

    if target not in log_map:
        await call.answer("Неизвестный тип лога", show_alert=True)
        return

    log_path, desc = log_map[target]
    if not log_path.exists() or log_path.stat().st_size == 0:
        await call.message.answer(f"ℹ️ Файл <b>{log_path.name}</b> пока пуст или ещё не создан системой.", parse_mode="HTML")
        return

    raw_size = log_path.stat().st_size

    # Если файл больше 40 МБ, сжимаем его в ZIP во избежание ограничений Telegram (50 MB)
    if raw_size > 40 * 1024 * 1024:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(log_path, arcname=log_path.name)
        zip_buf.seek(0)
        file_bytes = zip_buf.getvalue()
        send_name = f"{log_path.stem}_{timestamp}.zip"
        file_to_send = BufferedInputFile(file_bytes, filename=send_name)
        caption = (
            f"{desc}\n\n"
            f"📦 <i>Файл сжат в ZIP из-за большого размера (>40 МБ)</i>\n"
            f"📊 Исходный: <b>{raw_size / (1024*1024):.1f} MB</b> | Архив: <b>{len(file_bytes) / (1024*1024):.1f} MB</b>"
        )
    else:
        with open(log_path, "rb") as f:
            file_bytes = f.read()
        send_name = f"{log_path.stem}_{timestamp}{log_path.suffix}"
        file_to_send = BufferedInputFile(file_bytes, filename=send_name)
        caption = (
            f"{desc}\n\n"
            f"📦 Размер: <b>{raw_size / 1024:.1f} KB</b>\n"
            f"📅 Выгружен: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
        )

    await call.message.answer_document(
        document=file_to_send,
        caption=caption,
        parse_mode="HTML"
    )
    logger.info(f"👑 [АДМИН] Администратор ID:{user_id} выгрузил лог {send_name}")


# ── Поиск пользователей администратором ───────────────────────────────

@router.callback_query(F.data == "admin_search_user_prompt")
async def cb_admin_search_user_prompt(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_user_query)
    text = (
        "🔍 <b>ПОИСК ПОЛЬЗОВАТЕЛЯ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите <b>@username</b>, <b>Telegram ID</b> или имя пользователя:\n\n"
        "<i>Примеры:</i>\n"
        "• <code>@username</code>\n"
        "• <code>986832959</code>\n"
        "• <code>Алексей</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_search_cancel")]
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_search_cancel")
async def cb_admin_search_cancel(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await state.clear()
    await call.answer("Поиск отменён")
    await cmd_admin_panel(call, page=0)


@router.message(Command("find"))
@router.message(Command("user"))
@router.message(Command("search_user"))
async def cmd_admin_find_user(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await state.set_state(AdminStates.waiting_for_user_query)
        await message.answer(
            "🔍 Введите <b>@username</b>, <b>Telegram ID</b> или имя пользователя для поиска:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_search_cancel")]
            ])
        )
        return
    await do_admin_user_search(message, args[1].strip(), state)


@router.message(AdminStates.waiting_for_user_query)
async def msg_admin_user_query(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        await state.clear()
        return
    query = (message.text or "").strip()
    await do_admin_user_search(message, query, state)


async def do_admin_user_search(message: Message, query: str, state: FSMContext):
    await state.clear()
    if not query:
        await message.answer("Запрос пуст. Попробуйте снова.")
        return

    users = await database.find_users_by_query(query)
    if not users:
        await message.answer(
            f"❌ <b>Пользователь не найден</b>\n\n"
            f"По запросу <code>{html.escape(query)}</code> в базе данных бота ничего не найдено.\n\n"
            f"Убедитесь, что пользователь хотя бы раз отправлял <code>/start</code> в бот.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Искать снова", callback_data="admin_search_user_prompt")],
                [InlineKeyboardButton(text="👑 В админ-панель", callback_data="admin_refresh")]
            ])
        )
        return

    if len(users) == 1:
        u_id = users[0]["user_id"]
        text, kb = await render_user_card_data(u_id, page=0)
        if text:
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
            return

    text = (
        f"🔍 <b>РЕЗУЛЬТАТЫ ПОИСКА</b> (найдено: {len(users)})\n"
        f"Запрос: <code>{html.escape(query)}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    )
    buttons = []
    for idx, u in enumerate(users[:10], start=1):
        u_id = u["user_id"]
        uname = f"@{u['username']}" if u.get("username") else f"ID: {u_id}"
        fname = html.escape(u.get("first_name") or "Без имени")
        plan = config.plan_title(u.get("tariff_plan"))
        has_access = await database.is_user_allowed(u_id)
        status_icon = "🟢" if has_access else "🔴"
        text += f"{idx}. {status_icon} <b>{fname}</b> ({uname}) — <i>[{plan}]</i> [ID: <code>{u_id}</code>]\n"
        buttons.append([
            InlineKeyboardButton(text=f"👤 Карточка {uname}", callback_data=f"admin_user:{u_id}:0"),
            InlineKeyboardButton(text="💳 Выдать", callback_data=f"admin_sub_menu:{u_id}:0")
        ])

    buttons.append([
        InlineKeyboardButton(text="🔍 Новый поиск", callback_data="admin_search_user_prompt"),
        InlineKeyboardButton(text="👑 В админ-панель", callback_data="admin_refresh")
    ])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")





@router.message(Command("stats"))
@router.callback_query(F.data == "admin_runtime_stats")
async def cmd_admin_stats(event):
    if isinstance(event, CallbackQuery):
        message = event.message
        user_id = event.from_user.id
        if not config.is_admin(user_id):
            await event.answer("У вас нет прав администратора", show_alert=True)
            return
        await event.answer()
    else:
        message = event
        user_id = message.from_user.id
        if not config.is_admin(user_id):
            await message.answer("⛔ У вас нет прав администратора.")
            return

    import reports
    from runtime_metrics import METRICS
    await reports.refresh_live_metrics()

    stats_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_runtime_stats"),
            InlineKeyboardButton(text="🌐 Сеть и прокси", callback_data="admin_net"),
        ],
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="admin_refresh")],
    ])

    chunks = METRICS.format_stats_messages()
    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        await message.answer(chunk, reply_markup=stats_kb if is_last else None, parse_mode="HTML")


@router.message(Command("stats_at"))
async def cmd_admin_stats_at(message: Message):
    if not config.is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет прав администратора.")
        return
    import time
    import reports
    from datetime import datetime, timezone

    arg = (message.text or "").partition(" ")[2].strip()
    if arg.lower() in ("off", "нет", "отмена"):
        await database.set_bot_setting(reports.STATS_AT_KEY, "")
        await message.answer("🗓 Отчёт по расписанию отменён.")
        return
    due = reports.next_msk_epoch(arg)
    if due is None:
        await message.answer(
            "🗓 Пришлю статистику в указанное время по Москве.\n\n"
            "Пример: <code>/stats_at 5:00</code>, отмена: <code>/stats_at off</code>",
            parse_mode="HTML",
        )
        return
    await database.set_bot_setting(reports.STATS_AT_KEY, str(due))
    msk = datetime.fromtimestamp(due + 3 * 3600, timezone.utc).strftime("%H:%M")
    left_min = int((due - time.time()) // 60)
    await message.answer(
        f"🗓 Пришлю статистику в <b>{msk}</b> МСК (через {left_min // 60} ч {left_min % 60} мин) — всем админам.",
        parse_mode="HTML",
    )


async def _set_direct_legacy(enabled: bool) -> dict:
    """Old on/off switch for the server IP, mapped onto network modes."""
    from proxy_pool import get_proxy_pool

    pool = get_proxy_pool()
    config.ALLOW_DIRECT_AVITO = bool(enabled)
    await database.set_bot_setting("allow_direct_avito", "1" if enabled else "0")
    mode = ("smart" if pool.has_configured_proxy() else "direct") if enabled else "proxy"
    await database.set_bot_setting("net_mode", mode)
    return await pool.set_net_mode_async(mode)


def _net_mode_confirm(mode: str, yes_data: str) -> tuple[str, InlineKeyboardMarkup]:
    """Every network switch asks first: a wrong tap can stop scanning or burn the server IP."""
    text = (
        "🌐 <b>Точно переключить режим сети?</b>\n\n"
        f"Сейчас: <b>{NET_MODE_TITLES.get(config.NET_MODE, config.NET_MODE)}</b>\n"
        f"Станет: <b>{NET_MODE_TITLES.get(mode, mode)}</b>\n\n"
        f"<i>{NET_MODE_HINTS.get(mode, '')}</i>"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, переключить", callback_data=yes_data)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_net")],
    ])


@router.callback_query(F.data.startswith("admin_toggle_direct_avito"))
async def cb_admin_toggle_direct_avito(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    from proxy_pool import get_proxy_pool

    new_state = not bool(config.ALLOW_DIRECT_AVITO)
    target = ("smart" if get_proxy_pool().has_configured_proxy() else "direct") if new_state else "proxy"
    text, kb = _net_mode_confirm(target, f"admin_direct_yes:{int(new_state)}")
    await _show(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("admin_direct_yes:"))
async def cb_admin_direct_yes(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    new_state = call.data.split(":", 1)[1] == "1"
    res = await _set_direct_legacy(new_state)
    await database.log_admin_action(call.from_user.id, None, "net_mode", NET_MODE_TITLES.get(config.NET_MODE, config.NET_MODE))
    await call.answer(
        f"{'✅ IP сервера подключён к сканированию' if new_state else '🛑 IP сервера отключён от сканирования'} "
        f"(каналов: {res['total']})",
        show_alert=True,
    )
    text, kb = await _render_net()
    await _show(call, text, kb)


@router.message(Command("direct_mode"))
@router.message(Command("direct"))
async def cmd_admin_direct_mode(message: Message):
    if not config.is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет прав администратора.")
        return
    parts = (message.text or "").strip().split()
    if len(parts) > 1 and parts[1].lower() in ("1", "on", "true", "yes", "вкл"):
        new_state = True
    elif len(parts) > 1 and parts[1].lower() in ("0", "off", "false", "no", "выкл"):
        new_state = False
    else:
        new_state = not bool(config.ALLOW_DIRECT_AVITO)
    await _set_direct_legacy(new_state)
    text, kb = await _render_net()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("reload_proxies"))
async def cmd_admin_reload_proxies(message: Message):
    if not config.is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет прав администратора.")
        return

    from proxy_pool import get_proxy_pool
    from runtime_metrics import sanitize_proxy_credentials

    try:
        pool = get_proxy_pool()
        res = await pool.reload_async()
        await message.answer(
            "✅ <b>Пул прокси успешно перезагружен:</b>\n"
            f"• Активных прокси: <code>{res['total']}</code>\n"
            f"• Добавлено: <code>{res['added']}</code>\n"
            f"• Сохранено: <code>{res['kept']}</code>\n"
            f"• Выведено в дренаж: <code>{res['removed']}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        safe_err = sanitize_proxy_credentials(str(exc))
        await message.answer(
            f"❌ <b>Ошибка перезагрузки пула прокси:</b> {html.escape(safe_err)}\n"
            "Текущий активный пул оставлен без изменений.",
            parse_mode="HTML",
        )


# ── Сеть и прокси: режимы, проверка каналов, подключение прокси без перезапуска ──

NET_MODE_BUTTONS = (("smart", "🧠 Умный"), ("both", "🔀 Оба"), ("direct", "🖥 Сервер"), ("proxy", "🛰 Прокси"))

PROXY_FORMAT_HELP = (
    "Формат: <code>http://логин:пароль@IP:порт</code>\n"
    "или <code>IP:порт:логин:пароль</code>, поддерживается <code>socks5://</code>.\n"
    "Несколько прокси — каждый с новой строки."
)


NET_BACK_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🌐 К настройкам сети", callback_data="admin_net")],
])


NET_CHANNELS_SHOWN = 12
PROXIES_PER_PAGE = 8
PROBE_CONCURRENCY = 4


def _proxy_host(url: str | None) -> str:
    """'IP:порт' without credentials, for compact lists."""
    if not url:
        return "IP сервера"
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port}" if parts.hostname else "прокси"


def _probe_summary(result) -> str:
    """'2/3 · 640 мс' or '0/3 · Авито заблокировал IP (403)'."""
    score = f"{result.successes}/{len(result.attempts)}"
    if result.median_ok_ms is not None:
        return f"{score} · {result.median_ok_ms} мс"
    last = result.attempts[-1] if result.attempts else None
    reason = FAILURE_LABELS.get(last.failure, last.failure) if last else "нет ответа"
    status = f" (HTTP {last.status})" if last and last.status else ""
    return f"{score} · {html.escape(reason)}{status}"


async def _probe_target_url() -> str | None:
    """Probe on a real radar link when there is one: that is what the channel will be scanning."""
    try:
        searches = await database.get_all_active_searches()
    except Exception:
        return None
    return searches[0]["url"] if searches else None


async def _probe_many(urls: list, attempts: int) -> dict:
    from proxy_check import probe_channel

    target = await _probe_target_url()
    gate = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def one(url):
        async with gate:
            return await probe_channel(url, target, attempts=attempts)

    results = await asyncio.gather(*(one(url) for url in urls))
    return dict(zip(urls, results))


async def _render_net(note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    from proxy_pool import get_proxy_pool

    pool = get_proxy_pool()
    mode = config.NET_MODE
    snaps = pool.snapshots()
    working = [s for s in snaps if s.get("state") in ("healthy", "degraded", "half_open")]
    lines = [
        "🌐 <b>Сеть и прокси</b>",
        "",
        f"Режим: <b>{NET_MODE_TITLES.get(mode, mode)}</b>",
        f"<i>{NET_MODE_HINTS.get(mode, '')}</i>",
        *_smart_lines(pool),
        "",
        f"<b>Каналы сканирования</b> · работают {len(working)} из {len(snaps)}, "
        f"суммарно <code>{sum(float(s.get('effective_rate') or 0) for s in working):.2f}</code> req/s:",
    ]
    lines += [_channel_label(snap) for snap in snaps[:NET_CHANNELS_SHOWN]] or ["⏸ нет активных каналов — сканирование стоит"]
    if len(snaps) > NET_CHANNELS_SHOWN:
        lines.append(f"… и ещё {len(snaps) - NET_CHANNELS_SHOWN} — полный список в «🗂 Мои прокси»")
    if mode in ("proxy", "both", "smart") and not pool.has_configured_proxy():
        lines += ["", "⚠️ Прокси не подключены — нажмите «➕ Добавить прокси»."]
    if pool.pool_file_overrides_single_proxy():
        lines += ["", "📄 Прокси берутся из файла PROXY_POOL_FILE на сервере, добавленные в боте не используются."]
    if note:
        lines += ["", note]

    add_row = [InlineKeyboardButton(text="➕ Добавить прокси", callback_data="admin_net_add")]
    if config.PROXY_URLS:
        add_row.append(InlineKeyboardButton(text=f"🗂 Мои прокси ({len(config.PROXY_URLS)})", callback_data="admin_net_list:0"))
    mode_buttons = [
        InlineKeyboardButton(text=("✅ " if key == mode else "") + title, callback_data=f"admin_net_mode:{key}")
        for key, title in NET_MODE_BUTTONS
    ]
    buttons = [
        mode_buttons[:2],
        mode_buttons[2:],
        [InlineKeyboardButton(text="🧪 Проверить каналы", callback_data="admin_net_probe")],
        add_row,
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_net"),
            InlineKeyboardButton(text="⬅️ Меню", callback_data="admin_refresh"),
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def _smart_lines(pool) -> list[str]:
    """What smart mode does right now and why, so nobody has to guess from the channel list."""
    if config.NET_MODE != "smart" or not hasattr(pool, "smart_status"):
        return []
    status = pool.smart_status()
    load = f"нагрузка <code>{status['demand_rps']:.2f}</code> из <code>{status['limit_rps']:.2f}</code> запр/с"
    blocks = (
        f"блокировок у сервера {status['server_blocks']:.0%}" if status["server_answers"]
        else "у сервера пока нет ответов"
    )
    if not status["has_proxies"]:
        return ["", f"Сейчас: сканирует сервер, прокси нет · {load}"]
    if status["server_first"]:
        return ["", f"Сейчас: <b>сканирует сервер</b>, прокси в запасе · {load} · {blocks}"]
    left = int(status["hold_left"] // 60) + 1
    return [
        "",
        f"Сейчас: <b>прокси в работе</b> — {html.escape(status['reason'])}",
        f"Вернусь к одному серверу, если всё спокойно ещё ~{left} мин · {blocks}",
    ]


async def _set_net_mode(mode: str) -> None:
    from proxy_pool import get_proxy_pool

    await database.set_bot_setting("net_mode", mode)
    await get_proxy_pool().set_net_mode_async(mode)


async def _save_proxy_list(urls: list[str]) -> None:
    from proxy_pool import get_proxy_pool

    await get_proxy_pool().set_proxy_list_async(urls)
    await database.save_proxy_urls(config.PROXY_URLS)


async def _probe_all_channels() -> str:
    from proxy_check import DEFAULT_PROBE_URL
    from proxy_pool import get_proxy_pool

    urls = [client.proxy_url for client in get_proxy_pool().clients]
    if not urls:
        return "⏸ Нет активных каналов — проверять нечего."
    attempts = 3 if len(urls) <= 3 else 2
    results = await _probe_many(urls, attempts)
    target = (await _probe_target_url() or DEFAULT_PROBE_URL)[:80]
    lines = [f"🧪 <b>Проверка каналов</b> · по {attempts} запроса подряд", f"Поиск: <code>{html.escape(target)}</code>", ""]
    for url in urls:
        result = results[url]
        icon = "🟢" if result.successes == len(result.attempts) else ("🟡" if result.successes else "🔴")
        lines.append(f"{icon} {html.escape(_proxy_host(url))} — {_probe_summary(result)}")
    return "\n".join(lines[:60])


async def _add_proxies(message: Message, raw: str) -> None:
    """
    Add any number of proxies (one per line): validate, drop duplicates by exit IP, live-probe the rest
    and keep only the ones Avito answers through. The message with the passwords is deleted.
    """
    from proxy_check import exit_ips_for
    from proxy_pool import get_proxy_pool, parse_and_validate_proxy_line
    from runtime_metrics import sanitize_proxy_credentials

    try:
        await message.delete()
    except Exception:
        pass

    report: list[str] = []
    candidates: list[str] = []
    entries = [line.strip() for line in raw.splitlines() if line.strip() and not line.strip().startswith("#")]
    for line_number, line in enumerate(entries, start=1):
        try:
            normalized = parse_and_validate_proxy_line(line, line_number=line_number)
        except ValueError as exc:
            report.append(f"❌ {html.escape(sanitize_proxy_credentials(str(exc)))}")
            continue
        if normalized in config.PROXY_URLS or normalized in candidates:
            report.append(f"↩️ {html.escape(_proxy_host(normalized))} — уже в списке")
            continue
        candidates.append(normalized)

    if not candidates:
        text, kb = await _render_net("\n".join(report) or f"Не найдено ни одного прокси.\n\n{PROXY_FORMAT_HELP}")
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    status = await message.answer(f"⏳ Проверяю прокси: {len(candidates)} шт. Это займёт до минуты…")
    pool = get_proxy_pool()
    existing = [client.proxy_url for client in pool.clients if client.proxy_url]
    exit_ips = await exit_ips_for([None, *existing, *candidates])

    # One public IP is one IP for Avito, whatever the port or protocol.
    taken = {ip: _proxy_host(url) for url, ip in exit_ips.items() if ip and (url is None or url in existing)}
    unique: list[str] = []
    for url in candidates:
        ip = exit_ips.get(url)
        if ip and ip in taken:
            report.append(f"⚠️ {html.escape(_proxy_host(url))} — тот же IP {ip}, что у {html.escape(taken[ip])}: для Авито это один адрес, пропущен")
            continue
        if ip:
            taken[ip] = _proxy_host(url)
        unique.append(url)

    results = await _probe_many(unique, attempts=3 if len(unique) == 1 else 2) if unique else {}
    accepted = []
    for url in unique:
        result = results[url]
        ip = exit_ips.get(url)
        ip_note = f" · IP {ip}" if ip else ""
        if result.successes:
            accepted.append(url)
            report.append(f"✅ {html.escape(_proxy_host(url))}{ip_note} — {_probe_summary(result)}")
        else:
            report.append(f"🚫 {html.escape(_proxy_host(url))}{ip_note} — {_probe_summary(result)}, не добавлен")

    if accepted:
        await _save_proxy_list([*config.PROXY_URLS, *accepted])
        report.append(f"\n<b>Добавлено: {len(accepted)}</b>, всего прокси: {len(config.PROXY_URLS)}. Применено сразу.")
        if config.NET_MODE in ("direct", "auto"):
            # Keep the server IP scanning too: a fresh proxy must add capacity, not replace it.
            await _set_net_mode("smart")
            report.append("Режим переключён на «🧠 Умный»: сервер сканирует, прокси в запасе и включаются сами, когда нужны.")
        logger.info("Admin %s added %s proxies", message.from_user.id, len(accepted))
    text, kb = await _render_net("\n".join(report))
    await status.edit_text(text, reply_markup=kb, parse_mode="HTML")


async def _remove_proxies(proxy_ids: set[str] | None) -> str:
    """Remove the given bot-managed proxies (None = all) without stalling scanning."""
    from proxy_pool import compute_proxy_id, get_proxy_pool

    before = len(config.PROXY_URLS)
    keep = [] if proxy_ids is None else [u for u in config.PROXY_URLS if compute_proxy_id(u) not in proxy_ids]
    await _save_proxy_list(keep)
    note = f"🗑 Удалено прокси: {before - len(keep)}, осталось: {len(keep)}."
    if config.NET_MODE == "proxy" and not get_proxy_pool().has_configured_proxy():
        await _set_net_mode("direct")
        note += " Режим переключён на «🖥 Только сервер», чтобы сканирование не встало."
    return note


async def _render_proxy_list(page: int) -> tuple[str, InlineKeyboardMarkup]:
    from proxy_check import EXIT_IPS
    from proxy_pool import compute_proxy_id, get_proxy_pool
    from runtime_metrics import METRICS

    pool = get_proxy_pool()
    states = {snap["proxy_id"]: snap for snap in pool.snapshots()}
    summaries = {summary["proxy_id"]: summary for summary in METRICS.get_proxy_summaries()}
    urls = list(config.PROXY_URLS)
    pages = max(1, (len(urls) + PROXIES_PER_PAGE - 1) // PROXIES_PER_PAGE)
    page = min(max(0, page), pages - 1)
    start = page * PROXIES_PER_PAGE
    lines = [f"🗂 <b>Мои прокси</b> ({len(urls)})" + (f" · стр. {page + 1}/{pages}" if pages > 1 else ""), ""]
    buttons = []
    for index, url in enumerate(urls[start:start + PROXIES_PER_PAGE], start=start + 1):
        pid = compute_proxy_id(url)
        snap = states.get(pid, {})
        icon = CHANNEL_STATE_ICONS.get(str(snap.get("state")), "⚪")
        ip = EXIT_IPS.get(pid)
        rate = float(snap.get("effective_rate") or 0)
        ratio = summaries.get(pid, {}).get("raw_http_success_ratio_60m")
        lines.append(
            f"{index}. {icon} {html.escape(_proxy_host(url))}" + (f" · IP {ip}" if ip else "")
            + f" · <code>{rate:.2f}</code> req/s" + (f" · ok <code>{ratio:.0%}</code>" if ratio is not None else "")
        )
        buttons.append([
            InlineKeyboardButton(text=f"📊 {index}. {_proxy_host(url)}", callback_data=f"admin_net_stat:{pid}:{page}"),
            InlineKeyboardButton(text="🗑", callback_data=f"admin_net_del:{pid}:{page}"),
        ])
    if not urls:
        lines.append("Список пуст.")
    if any(client.is_direct for client in pool.clients):
        buttons.append([InlineKeyboardButton(text="📊 Сервер", callback_data=f"admin_net_stat:direct:{page}")])
    if pages > 1:
        buttons.append([
            InlineKeyboardButton(text="◀️", callback_data=f"admin_net_list:{page - 1}" if page > 0 else "admin_noop"),
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="admin_noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"admin_net_list:{page + 1}" if page < pages - 1 else "admin_noop"),
        ])
    if urls:
        buttons.append([InlineKeyboardButton(text="🗑 Удалить все", callback_data="admin_net_delall")])
    buttons.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="admin_net_add"),
        InlineKeyboardButton(text="🌐 К сети", callback_data="admin_net"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def _channel_day_lines(stats: dict) -> list[str]:
    """The channel's last 24 hours from logs/metrics.jsonl."""
    http, blocks, scans = stats.get("http", 0), stats.get("403", 0) + stats.get("429", 0), stats.get("coll", 0)
    lines = ["<b>За 24 часа</b>"]
    if http:
        lines.append(f"Запросов {http}, успешных {stats.get('ok', 0) / http:.0%}")
        lines.append(f"Блоки 403/429: {blocks} ({blocks / http:.0%}), обрывы связи: {stats.get('terr', 0)}")
    if scans:
        lines.append(f"Сканов {scans}, удачных {stats.get('coll_ok', 0) / scans:.0%}")
    lines.append(f"На паузе {stats.get('paused', 0)} мин из {stats.get('minutes', 0)}")
    if stats["fail"]:
        lines.append("Ошибки: " + ", ".join(f"{FAILURE_LABELS.get(k, k)} — {v}" for k, v in stats["fail"].most_common(3)))
    return lines


async def _render_proxy_card(proxy_id: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    import log_report
    from proxy_check import EXIT_IPS
    from proxy_pool import compute_proxy_id, get_proxy_pool
    from runtime_metrics import METRICS, channel_card

    back = [InlineKeyboardButton(text="« К списку", callback_data=f"admin_net_list:{page}")]
    is_direct = proxy_id == "direct"
    url = next((u for u in config.PROXY_URLS if compute_proxy_id(u) == proxy_id), None)
    if url is None and not is_direct:
        return "Этого прокси уже нет в списке.", InlineKeyboardMarkup(inline_keyboard=[back])

    lines = [f"📊 <b>{'Сервер (без прокси)' if is_direct else html.escape(_proxy_host(url))}</b>"]
    if EXIT_IPS.get(proxy_id):
        lines.append(f"Внешний IP: {EXIT_IPS[proxy_id]}")
    client = next((c for c in get_proxy_pool().clients if c.proxy_id == proxy_id), None)
    if client is None:
        lines.append("Сейчас не в работе: режим сети без этого канала.")
    else:
        pool = get_proxy_pool()
        server_first = config.NET_MODE == "smart" and pool.smart_status()["server_first"]
        lines.append(f"Роль: {client.routing_role(server_first=server_first)}")
        lines.append(f"Последние {config.PROXY_HEALTH_WINDOW} запросов: {client.recent_health():.0%} успешных")
        if server_first and not client.is_direct and len(client._outcomes) >= 3:
            lines.append(f"За последние {len(client._outcomes)} ответов: {client.long_health():.0%} успешных")

    summary = next((s for s in METRICS.get_proxy_summaries() if s["proxy_id"] == proxy_id), None)
    if summary is not None:
        lines += ["", "<b>За 60 минут</b>", *channel_card(summary)]
    stats = await asyncio.to_thread(log_report.channel_history, "direct" if is_direct else _proxy_host(url))
    if stats is not None:
        lines += ["", *_channel_day_lines(stats)]
    if summary is None and stats is None:
        lines += ["", "Статистики пока нет: канал ещё не делал запросов."]

    buttons = [[InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin_net_stat:{proxy_id}:{page}")]]
    if not is_direct:
        buttons.append([InlineKeyboardButton(text="🗑 Удалить прокси", callback_data=f"admin_net_del:{proxy_id}:{page}")])
    buttons.append(back)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_net")
async def cb_admin_net(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await state.clear()
    await call.answer()
    text, kb = await _render_net()
    await _show(call, text, kb)


async def _net_mode_refusal(call: CallbackQuery, mode: str) -> bool:
    """Answers and returns True when this mode cannot be switched on now."""
    from proxy_pool import get_proxy_pool

    if mode not in config.NET_MODES:
        await call.answer("Неизвестный режим", show_alert=True)
        return True
    if mode in ("proxy", "both", "smart") and not get_proxy_pool().has_configured_proxy():
        await call.answer("Сначала добавьте прокси: кнопка «➕ Добавить прокси».", show_alert=True)
        return True
    return False


@router.callback_query(F.data.startswith("admin_net_mode:"))
async def cb_admin_net_mode(call: CallbackQuery):
    """A mode button only asks; the switch happens on «Да, переключить» (admin_net_mode_yes)."""
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    mode = call.data.split(":", 1)[1]
    if await _net_mode_refusal(call, mode):
        return
    if mode == config.NET_MODE:
        await call.answer(f"Уже включён: {NET_MODE_TITLES.get(mode, mode)}")
        return
    text, kb = _net_mode_confirm(mode, f"admin_net_mode_yes:{mode}")
    await _show(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("admin_net_mode_yes:"))
async def cb_admin_net_mode_yes(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    mode = call.data.split(":", 1)[1]
    if await _net_mode_refusal(call, mode):
        return
    await _set_net_mode(mode)
    await database.log_admin_action(call.from_user.id, None, "net_mode", NET_MODE_TITLES.get(mode, mode))
    await call.answer(f"Режим: {NET_MODE_TITLES[mode]}")
    text, kb = await _render_net()
    await _show(call, text, kb)


@router.callback_query(F.data == "admin_net_probe")
async def cb_admin_net_probe(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await call.answer("Проверяю, это займёт до минуты…")
    try:
        await call.message.edit_text("⏳ Проверяю каналы…")
    except Exception:
        pass
    report = await _probe_all_channels()
    await _show(call, report, NET_BACK_KB)


@router.callback_query(F.data == "admin_net_add")
async def cb_admin_net_add(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_proxy)
    await call.answer()
    await _show(
        call,
        "➕ <b>Добавление прокси</b>\n\n"
        "Пришлите один или сколько угодно прокси — каждый с новой строки. Бот удалит сообщение из чата, "
        "отсеет прокси с одинаковым IP, проверит каждый на Авито и добавит только рабочие.\n\n" + PROXY_FORMAT_HELP,
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="admin_net")]]),
    )


@router.message(AdminStates.waiting_for_proxy, F.text, ~F.text.startswith("/"))
async def msg_admin_proxy_input(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    await state.clear()
    await _add_proxies(message, message.text)


@router.callback_query(F.data.startswith("admin_net_list:"))
async def cb_admin_net_list(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0
    await call.answer()
    text, kb = await _render_proxy_list(page)
    await _show(call, text, kb)


@router.callback_query(F.data.startswith("admin_net_stat:"))
async def cb_admin_net_stat(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    proxy_id = parts[1] if len(parts) > 1 else ""
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    await call.answer()
    text, kb = await _render_proxy_card(proxy_id, page)
    await _show(call, text, kb)


@router.callback_query(F.data.startswith("admin_net_del:"))
async def cb_admin_net_del(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    proxy_id = parts[1] if len(parts) > 1 else ""
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    note = await _remove_proxies({proxy_id})
    await call.answer(note[:190])
    text, kb = await _render_proxy_list(page)
    await _show(call, text, kb)


@router.callback_query(F.data == "admin_net_delall")
async def cb_admin_net_delall(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await call.answer()
    await _show(
        call,
        f"🗑 Удалить все прокси ({len(config.PROXY_URLS)} шт.)?",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да, удалить все", callback_data="admin_net_delall_yes"),
            InlineKeyboardButton(text="Отмена", callback_data="admin_net_list:0"),
        ]]),
    )


@router.callback_query(F.data == "admin_net_delall_yes")
async def cb_admin_net_delall_yes(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    note = await _remove_proxies(None)
    await call.answer("Прокси удалены")
    text, kb = await _render_net(note)
    await _show(call, text, kb)


@router.message(Command("proxy"))
async def cmd_admin_proxy(message: Message):
    """/proxy — экран сети; /proxy <прокси, можно списком> — добавить; /proxy test — проверить; /proxy off — удалить все."""
    if not config.is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет прав администратора.")
        return
    parts = (message.text or "").strip().split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if not arg:
        text, kb = await _render_net()
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    elif arg.lower() in ("off", "выкл", "0"):
        text, kb = await _render_net(await _remove_proxies(None))
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    elif arg.lower() in ("test", "тест", "check"):
        status = await message.answer("⏳ Проверяю каналы…")
        await status.edit_text(await _probe_all_channels(), reply_markup=NET_BACK_KB, parse_mode="HTML")
    else:
        await _add_proxies(message, arg)


@router.callback_query(F.data == "admin_daily_report")
async def cb_admin_daily_report(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    await call.answer()
    from reports import build_daily_report
    text = await build_daily_report(reset=False)
    text += f"\n\n<i>Предпросмотр: сам отчёт приходит каждый день в {config.DAILY_REPORT_HOUR:02d}:00 МСК.</i>"
    await _show(call, text, ui.kb([ui.btn("« Админ-панель", callback_data="admin_refresh")]))


# ── Премиум-эмодзи интерфейса ──────────────────────────────────────────

EMOJI_SLOTS_PER_PAGE = 24


def _emoji_home() -> tuple[str, InlineKeyboardMarkup]:
    status = f"{ui.e('on')} включены" if ui.ENABLED else f"{ui.e('pause')} выключены"
    lines = [
        f"{ui.e('palette')} <b>Премиум-эмодзи</b>",
        "",
        f"Статус: {status} · анимированных <b>{len(ui.IDS)}</b> из {len(ui.SLOTS)} · своих замен {len(ui.OVERRIDES)}",
        "",
        "По умолчанию стоит официальный анимированный пак Telegram. "
        "Хотите другой стиль: «Загрузить пак» → пришлите любой эмодзи из нужного пака, "
        "бот заменит все совпадающие места. Отдельные места меняются по одному.",
        "",
    ]
    lines += [f"{ui.e(name)} {desc}" + ("" if name in ui.IDS else " <i>(обычный)</i>")
              + (" <i>· своя</i>" if name in ui.OVERRIDES else "")
              for name, (_char, desc) in ui.SLOTS.items()]
    markup = ui.kb(
        [ui.btn("Загрузить пак", "box", callback_data="admin_emoji_pack", style="primary")],
        [ui.btn("Заменить по одному", "write", callback_data="admin_emoji_slots:0")],
        [ui.btn("Выключить" if ui.ENABLED else "Включить", "pause" if ui.ENABLED else "play",
                callback_data="admin_emoji_toggle"),
         ui.btn("К стандартным", "refresh", callback_data="admin_emoji_reset")],
        [ui.btn("« Админ-панель", callback_data="admin_refresh")],
    )
    return "\n".join(lines), markup


@router.callback_query(F.data == "admin_emoji")
async def cb_admin_emoji(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    await state.clear()
    await call.answer()
    text, markup = _emoji_home()
    await _show(call, text, markup)


@router.callback_query(F.data == "admin_emoji_toggle")
async def cb_admin_emoji_toggle(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    await ui.save(enabled=not ui.ENABLED)
    await call.answer("Премиум-эмодзи включены" if ui.ENABLED else "Премиум-эмодзи выключены")
    text, markup = _emoji_home()
    await _show(call, text, markup)


@router.callback_query(F.data == "admin_emoji_reset")
async def cb_admin_emoji_reset(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    await call.answer()
    await _show(call, f"{ui.e('refresh')} <b>Убрать все свои замены?</b>\n\n"
                      "Везде вернётся стандартный анимированный пак Telegram.", ui.kb([
        ui.btn("Да, вернуть", callback_data="admin_emoji_reset_yes", style="danger"),
        ui.btn("Отмена", callback_data="admin_emoji"),
    ]))


@router.callback_query(F.data == "admin_emoji_reset_yes")
async def cb_admin_emoji_reset_yes(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    await ui.save(overrides={})
    await call.answer("Вернул стандартные")
    text, markup = _emoji_home()
    await _show(call, text, markup)


@router.callback_query(F.data == "admin_emoji_pack")
async def cb_admin_emoji_pack(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.waiting_for_emoji_pack)
    await call.answer()
    await _show(call, (
        f"{ui.e('box')} <b>Загрузка пака</b>\n\n"
        "Пришлите одно или несколько премиум-эмодзи. Бот загрузит их паки целиком "
        "и заполнит все подходящие места. Эмодзи из самого сообщения важнее остальных: "
        "так можно точно выбрать, например, свой 🎯 для радара."
    ), ui.kb([ui.btn("Отмена", "cancel", callback_data="admin_emoji")]))


@router.callback_query(F.data.startswith("admin_emoji_slots:"))
async def cb_admin_emoji_slots(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    await state.clear()
    await call.answer()
    page = int(call.data.split(":")[1] or 0)
    names = list(ui.SLOTS)
    pages = max(1, -(-len(names) // EMOJI_SLOTS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = names[page * EMOJI_SLOTS_PER_PAGE:(page + 1) * EMOJI_SLOTS_PER_PAGE]
    rows, row = [], []
    for name in chunk:
        row.append(ui.btn(ui.SLOTS[name][1].split(",")[0][:18], name, callback_data=f"admin_emoji_slot:{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(ui.btn("«", callback_data=f"admin_emoji_slots:{page - 1}"))
    if page < pages - 1:
        nav.append(ui.btn("»", callback_data=f"admin_emoji_slots:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([ui.btn("« Эмодзи", callback_data="admin_emoji")])
    await _show(call, f"{ui.e('write')} <b>Какое место заменить?</b> · стр. {page + 1}/{pages}", ui.kb(*rows))


@router.callback_query(F.data.startswith("admin_emoji_slot:"))
async def cb_admin_emoji_slot(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    name = call.data.split(":", 1)[1]
    if name not in ui.SLOTS:
        await call.answer("Такого места больше нет", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_emoji_slot)
    await state.update_data(emoji_slot=name)
    await call.answer()
    rows = [[ui.btn("Отмена", "cancel", callback_data="admin_emoji_slots:0")]]
    if name in ui.IDS:
        rows.insert(0, [ui.btn(f"Обычный {ui.plain(name)}", callback_data=f"admin_emoji_plain:{name}")])
    if name in ui.OVERRIDES:
        rows.insert(0, [ui.btn("Вернуть стандартный", "refresh", callback_data=f"admin_emoji_clear:{name}")])
    await _show(call, (
        f"{ui.e(name)} <b>{ui.SLOTS[name][1]}</b>\n\n"
        f"Обычный: {ui.plain(name)}. Пришлите один премиум-эмодзи для этого места."
    ), ui.kb(*rows))


@router.callback_query(F.data.startswith(("admin_emoji_clear:", "admin_emoji_plain:")))
async def cb_admin_emoji_clear(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    await state.clear()
    action, name = call.data.split(":", 1)
    overrides = {k: v for k, v in ui.OVERRIDES.items() if k != name}
    if action == "admin_emoji_plain":
        overrides[name] = "0"
    await ui.save(overrides=overrides)
    await call.answer("Поставил обычный" if action == "admin_emoji_plain" else "Вернул стандартный")
    text, markup = _emoji_home()
    await _show(call, text, markup)


async def _pack_pairs(bot: Bot, emoji_ids: list[str]) -> list[tuple[str, str]]:
    """(plain emoji, custom_emoji_id) for every emoji of the packs these custom emoji belong to."""
    pairs: list[tuple[str, str]] = []
    try:
        stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=emoji_ids[:200])
    except Exception as exc:
        logger.warning("getCustomEmojiStickers не удался: %s", exc)
        return pairs
    for set_name in dict.fromkeys(s.set_name for s in stickers if s.set_name):
        try:
            sticker_set = await bot.get_sticker_set(name=set_name)
        except Exception as exc:
            logger.warning("getStickerSet %s не удался: %s", set_name, exc)
            continue
        pairs += [(s.emoji or "", s.custom_emoji_id) for s in sticker_set.stickers if s.custom_emoji_id]
    return pairs


@router.message(AdminStates.waiting_for_emoji_pack)
async def msg_admin_emoji_pack(message: Message, state: FSMContext, bot: Bot):
    if not config.is_admin(message.from_user.id):
        return
    own = ui.custom_emoji_in(message)
    if not own:
        await message.answer("В сообщении нет премиум-эмодзи. Пришлите эмодзи из пака (не обычный смайлик).",
                             reply_markup=ui.kb([ui.btn("Отмена", "cancel", callback_data="admin_emoji")]))
        return
    await state.clear()
    status = await message.answer(f"{ui.e('wait')} Загружаю пак…", parse_mode="HTML")
    found = ui.match_slots(await _pack_pairs(bot, [cid for _c, cid in own]))
    found.update(ui.match_slots(own))
    await ui.save(overrides={**ui.OVERRIDES, **found})
    missing = [ui.SLOTS[n][0] for n in ui.SLOTS if n not in ui.IDS]
    text = f"{ui.e('ok')} <b>Заполнено мест: {len(found)}</b>\n" + " ".join(ui.e(n) for n in found)
    if missing:
        text += f"\n\nНе нашлось в паке ({len(missing)}): {' '.join(missing)}\nИх можно назначить по одному или загрузить ещё пак."
    await status.edit_text(text, reply_markup=ui.kb(
        [ui.btn("Загрузить ещё пак", "box", callback_data="admin_emoji_pack")],
        [ui.btn("« Эмодзи", callback_data="admin_emoji")],
    ), parse_mode="HTML")


@router.message(AdminStates.waiting_for_emoji_slot)
async def msg_admin_emoji_slot(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    own = ui.custom_emoji_in(message)
    name = (await state.get_data()).get("emoji_slot")
    if not own or name not in ui.SLOTS:
        await message.answer("Нужен премиум-эмодзи (из пака), не обычный смайлик.",
                             reply_markup=ui.kb([ui.btn("Отмена", "cancel", callback_data="admin_emoji_slots:0")]))
        return
    await state.clear()
    await ui.save(overrides={**ui.OVERRIDES, name: own[0][1]})
    await message.answer(f"{ui.e(name)} <b>Готово:</b> {ui.SLOTS[name][1]}", parse_mode="HTML", reply_markup=ui.kb(
        [ui.btn("Ещё одно место", "write", callback_data="admin_emoji_slots:0")],
        [ui.btn("« Эмодзи", callback_data="admin_emoji")],
    ))


@router.message(Command("emoji_id"))
async def cmd_emoji_id(message: Message):
    """/emoji_id с премиум-эмодзи в сообщении (или ответом на сообщение с ними) — их ID."""
    if not config.is_admin(message.from_user.id):
        return
    found = ui.custom_emoji_in(message)
    if not found and message.reply_to_message:
        found = ui.custom_emoji_in(message.reply_to_message)
    if not found:
        await message.answer("Пришлите /emoji_id вместе с премиум-эмодзи или ответом на сообщение с ними.")
        return
    await message.answer("\n".join(f'<tg-emoji emoji-id="{cid}">{char}</tg-emoji> <code>{cid}</code>'
                                   for char, cid in found), parse_mode="HTML")
