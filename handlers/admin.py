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
        "━━━━━━━━━━━━━━━━━━━━\n"
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
    if config.is_admin(target_id):
        # Admins have no subscription: only radars and the radar limit are managed here.
        text = text.replace(f"• Статус: {status_line}\n", "• Статус: 👑 <b>Администратор</b>\n")
        return text, InlineKeyboardMarkup(inline_keyboard=[row for row in (
            [InlineKeyboardButton(text=f"📁 Радары ({len(user_radars)})", callback_data=f"admin_user_radars:{target_id}:{page}")],
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
                f"{idx}. {status_emoji} <b>{title}</b>\n"
                f"   ├ Выборка: <i>{scope}</i>\n"
                f"   └ <a href=\"{url}\">Ссылка на Авито</a>"
            )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Назад в карточку пользователя", callback_data=f"admin_user:{target_id}:{page}")]
    ])
    await call.message.edit_text("\n".join(text_lines), reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    await call.answer()


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

NET_MODE_TITLES = {
    "direct": "🖥 Только сервер",
    "proxy": "🛰 Только прокси",
    "both": "🔀 Сервер + прокси",
    "auto": "🤖 Авто",
}
NET_MODE_HINTS = {
    "direct": "Сканирует только IP сервера. Бесплатно, но один IP — ниже скорость и выше риск блокировки.",
    "proxy": "Сканируют только прокси, IP сервера Авито не видит.",
    "both": "Сервер и прокси работают вместе и делят нагрузку — больше проверок в секунду.",
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
    await _show(message, *_stars_prices_screen())


@router.message(Command("users"))
@router.callback_query(F.data.startswith("admin_page:"))
async def cmd_admin_panel(event, page: int = 0):
    """Список пользователей: одна строка и одна кнопка на человека, все действия — в карточке."""
    message, user_id = _admin_event(event)
    if isinstance(event, CallbackQuery):
        if event.data.startswith("admin_page:"):
            try:
                page = int(event.data.split(":")[1])
            except (ValueError, IndexError):
                page = 0
        await event.answer()
    if not config.is_admin(user_id):
        return

    all_users = await database.get_all_users()
    total_pages = max(1, (len(all_users) + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = min(max(0, page), total_pages - 1)
    start_idx = page * USERS_PER_PAGE
    page_users = all_users[start_idx:start_idx + USERS_PER_PAGE]

    page_info = f" · стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    lines = [f"👥 <b>Пользователи</b> ({len(all_users)}){page_info}", ""]
    buttons = []
    for idx, u in enumerate(page_users, start=start_idx + 1):
        u_id = u["user_id"]
        fname = u.get("first_name") or "Без имени"
        tag = f"@{u['username']}" if u.get("username") else str(u_id)
        if config.is_admin(u_id):
            icon, status = "👑", "админ"
        elif await database.is_user_allowed(u_id):
            trial = not u.get("is_allowed") and u.get("trial_used")
            icon = "🎁" if trial else "🟢"
            status = "триал" if trial else config.plan_title(u.get("tariff_plan"))
            until = _short_until(u.get("subscription_until"))
            status = f"{status} {until}".strip()
        else:
            icon, status = "🔴", "нет доступа"
        lines.append(f"{idx}. {icon} <b>{html.escape(fname)}</b> {html.escape(tag)} — {status}")
        label = f"{icon} {fname[:18]} · {tag[:16]}"
        callback = "admin_noop" if config.is_admin(u_id) else f"admin_user:{u_id}:{page}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=callback)])

    if not page_users:
        lines.append("Пока никого нет.")
    lines.append("\nНажмите на пользователя: подписка, радары, платежи, блок и удаление — в карточке.")

    if total_pages > 1:
        buttons.append([
            InlineKeyboardButton(text="◀️", callback_data=f"admin_page:{page - 1}" if page > 0 else "admin_noop"),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="admin_noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"admin_page:{page + 1}" if page < total_pages - 1 else "admin_noop"),
        ])
    buttons.append([
        InlineKeyboardButton(text="🔍 Найти", callback_data="admin_search_user_prompt"),
        InlineKeyboardButton(text="⬅️ Меню", callback_data="admin_refresh"),
    ])
    await _show(event, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


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
    page = int(parts[2]) if len(parts) > 2 else 0

    res = await database.purge_user_by_pattern(target_user_id)
    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} стер пользователя ID:{target_user_id}: {res['message']}")
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
        "<b>Готовность к продаже</b> — вердикт за сутки: скорость, надёжность, прокси, ссылки.\n"
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


async def _send_debug_report(message: Message, minutes: int) -> None:
    from datetime import datetime
    from aiogram.types import BufferedInputFile
    import log_report

    report = await asyncio.to_thread(log_report.build_report, minutes, _debug_live_state())
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

    report = await asyncio.to_thread(log_report.build_readiness, hours, _debug_live_state())
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
    await _send_readiness_report(message, max(1, min(hours, 72)))


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
        zip_buffer = io.BytesIO()
        total_raw_size = 0
        files_count = 0

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            # Общий bot.log
            bot_log = base_dir / "bot.log"
            if bot_log.exists() and bot_log.is_file():
                sz = bot_log.stat().st_size
                if sz > 0:
                    zf.write(bot_log, arcname="bot.log")
                    total_raw_size += sz
                    files_count += 1

            # Файлы из папки logs/
            logs_dir = base_dir / "logs"
            if logs_dir.exists() and logs_dir.is_dir():
                for p in sorted(logs_dir.iterdir()):
                    if p.is_file():
                        sz = p.stat().st_size
                        if sz > 0:
                            zf.write(p, arcname=f"logs/{p.name}")
                            total_raw_size += sz
                            files_count += 1

        if files_count == 0:
            await call.message.answer("⚠️ Файлы логов в системе пока отсутствуют или пусты.")
            return

        zip_buffer.seek(0)
        zip_bytes = zip_buffer.getvalue()
        zip_filename = f"avito_radar_logs_{timestamp}.zip"
        file_to_send = BufferedInputFile(zip_bytes, filename=zip_filename)

        compression_pct = round((1 - len(zip_bytes) / max(total_raw_size, 1)) * 100)

        await call.message.answer_document(
            document=file_to_send,
            caption=(
                "📦 <b>Полный архив системных логов AVITO RADAR</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🗂 Файлов в архиве: <b>{files_count}</b>\n"
                f"📊 Исходный размер: <b>{total_raw_size / 1024:.1f} KB</b>\n"
                f"🗜 Размер архива: <b>{len(zip_bytes) / 1024:.1f} KB</b> (сжатие {compression_pct}%)\n"
                f"📅 Дата выгрузки: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
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
    mode = ("both" if pool.has_configured_proxy() else "direct") if enabled else "proxy"
    await database.set_bot_setting("net_mode", mode)
    return await pool.set_net_mode_async(mode)


@router.callback_query(F.data.startswith("admin_toggle_direct_avito"))
async def cb_admin_toggle_direct_avito(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    new_state = not bool(config.ALLOW_DIRECT_AVITO)
    res = await _set_direct_legacy(new_state)
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

NET_MODE_BUTTONS = (("direct", "🖥 Сервер"), ("proxy", "🛰 Прокси"), ("both", "🔀 Оба"))

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
        "",
        f"<b>Каналы сканирования</b> · работают {len(working)} из {len(snaps)}, "
        f"суммарно <code>{sum(float(s.get('effective_rate') or 0) for s in working):.2f}</code> req/s:",
    ]
    lines += [_channel_label(snap) for snap in snaps[:NET_CHANNELS_SHOWN]] or ["⏸ нет активных каналов — сканирование стоит"]
    if len(snaps) > NET_CHANNELS_SHOWN:
        lines.append(f"… и ещё {len(snaps) - NET_CHANNELS_SHOWN} — полный список в «🗂 Мои прокси»")
    if mode in ("proxy", "both") and not pool.has_configured_proxy():
        lines += ["", "⚠️ Прокси не подключены — нажмите «➕ Добавить прокси»."]
    if pool.pool_file_overrides_single_proxy():
        lines += ["", "📄 Прокси берутся из файла PROXY_POOL_FILE на сервере, добавленные в боте не используются."]
    if note:
        lines += ["", note]

    add_row = [InlineKeyboardButton(text="➕ Добавить прокси", callback_data="admin_net_add")]
    if config.PROXY_URLS:
        add_row.append(InlineKeyboardButton(text=f"🗂 Мои прокси ({len(config.PROXY_URLS)})", callback_data="admin_net_list:0"))
    buttons = [
        [
            InlineKeyboardButton(text=("✅ " if key == mode else "") + title, callback_data=f"admin_net_mode:{key}")
            for key, title in NET_MODE_BUTTONS
        ],
        [InlineKeyboardButton(text="🧪 Проверить каналы", callback_data="admin_net_probe")],
        add_row,
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_net"),
            InlineKeyboardButton(text="⬅️ Меню", callback_data="admin_refresh"),
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


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
            await _set_net_mode("both")
            report.append("Режим переключён на «🔀 Оба»: сервер и прокси делят нагрузку.")
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
        lines.append(f"Роль: {client.routing_role()}")
        lines.append(f"Последние {config.PROXY_HEALTH_WINDOW} запросов: {client.recent_health():.0%} успешных")

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


@router.callback_query(F.data.startswith("admin_net_mode:"))
async def cb_admin_net_mode(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    from proxy_pool import get_proxy_pool

    mode = call.data.split(":", 1)[1]
    if mode not in config.NET_MODES:
        await call.answer("Неизвестный режим", show_alert=True)
        return
    if mode in ("proxy", "both") and not get_proxy_pool().has_configured_proxy():
        await call.answer("Сначала добавьте прокси: кнопка «➕ Добавить прокси».", show_alert=True)
        return
    await _set_net_mode(mode)
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
