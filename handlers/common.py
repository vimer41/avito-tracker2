import html
import logging
import re
from typing import Optional
from aiogram import Bot, Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart, Command
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.fsm.context import FSMContext

import config
import rub_payments
import database
import legal_docs
import referrals
import trial_gate
import ui
from logger_setup import log_user_event

logger = logging.getLogger(__name__)
router = Router()


def get_docs_keyboard() -> InlineKeyboardMarkup:
    return ui.kb(
        [ui.btn("Пользовательское соглашение", "docs", callback_data="doc_agreement")],
        [ui.btn("Политика конфиденциальности", "lock", callback_data="doc_privacy")],
        [ui.btn("Условия подписки и возврата", "card", callback_data="payment_terms")],
        [ui.home_btn()],
    )


def get_doc_keyboard(name: str, page: int) -> InlineKeyboardMarkup:
    pages = legal_docs.DOCS[name][2]
    nav = []
    if page > 0:
        nav.append(ui.btn("« Назад", callback_data=f"doc_page:{name}:{page - 1}"))
    if page < len(pages) - 1:
        nav.append(ui.btn("Далее »", callback_data=f"doc_page:{name}:{page + 1}"))
    return ui.kb(
        nav,
        [ui.btn("К списку документов", "docs", callback_data="open_docs_menu")],
        [ui.btn("Тарифы", "tariffs", callback_data="open_subscription_menu"),
         ui.btn("Поддержка", "support", callback_data="open_support_stub")],
        [ui.home_btn()],
    )


def get_main_keyboard(user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    """Нижняя клавиатура: кнопки присылают свой текст, поэтому эмодзи в них обычные."""
    label = {key: ui.menu_label(key) for key in ui.MENU}
    if ui.clean():
        # The main action alone on top in blue, the rest two to a row; monochrome icons, the label stays plain
        # text because a reply button sends its label back.
        def key(name: str, icon: str, style: Optional[str] = None) -> KeyboardButton:
            return KeyboardButton(text=label[name], icon_custom_emoji_id=ui.custom_id(icon), style=style)

        kb = [
            [key("add", "add", "primary")],
            [key("radars", "radars"), key("profile", "profile")],
            [key("tariffs", "tariffs"), key("help", "help")],
            [key("support", "support")] + ([key("admin", "admin")] if user_id and config.is_admin(user_id) else []),
        ]
    else:
        kb = [
            [KeyboardButton(text=label["add"]), KeyboardButton(text=label["radars"])],
            [KeyboardButton(text=label["tariffs"]), KeyboardButton(text=label["profile"])],
            [KeyboardButton(text=label["help"]), KeyboardButton(text=label["support"])],
        ]
    if user_id and config.is_admin(user_id) and not ui.clean():
        kb.append([KeyboardButton(text=label["admin"])])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, input_field_placeholder="Пришлите ссылку на поиск Авито")


def main_menu_kb() -> InlineKeyboardMarkup:
    return ui.kb(
        [ui.btn("Добавить радар", "radar", callback_data="add_search_prompt", style="primary")],
        [ui.btn("Мои радары", "radars", callback_data="list_searches"),
         ui.btn("Профиль", "profile", callback_data="refresh_profile")],
        [ui.btn("Тарифы", "tariffs", callback_data="open_subscription_menu"),
         ui.btn("Инструкция", "help", callback_data="open_help_menu")],
        [ui.btn("Поддержка", "support", callback_data="open_support_stub"),
         ui.btn("Документы", "docs", callback_data="open_docs_menu")],
    )


def radars_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} радар"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} радара"
    return f"{count} радаров"


def plan_icon(plan: str) -> str:
    return ui.e(plan if plan in ("lite", "standart", "pro") else "standart")


def plan_summary(plan: str) -> str:
    """«🚀 Средний — 449 ₽ / мес · 1212 ₽ / 3 мес» plus its limits, for tariff lists."""
    return (
        f"{plan_icon(plan)} <b>{config.plan_title(plan)}</b> — {config.rub_price(plan)} ₽ / мес · "
        f"{config.rub_price(plan, 3)} ₽ / 3 мес\n"
        f"{radars_word(config.plan_radars(plan))} · проверка раз в {config.plan_interval(plan)} с"
        + (" · лоты в группу или канал" if plan in config.GROUP_DELIVERY_PLANS else "")
    )


PLAN_AUDIENCE = {
    "lite": "Для поиска вещи для себя",
    "pro": "Для перепродажи и больших городов",
}


def plan_card(plan: str) -> str:
    """A tariff on the tariff screen: what it gives over the cheaper one and the per-month price of 3 months."""
    fastest = min(config.PAID_PLANS, key=config.plan_interval)
    long_price = config.rub_price(plan, 3)
    lines = [
        f"{plan_icon(plan)} <b>{config.plan_title(plan)}</b> — {config.rub_price(plan)} ₽ / мес"
        + (" · <b>Популярный</b>" if plan == "standart" else ""),
        f"{radars_word(config.plan_radars(plan))} · проверка раз в {config.plan_interval(plan)} с"
        + (" — самая быстрая" if plan == fastest else ""),
    ]
    if plan in config.GROUP_DELIVERY_PLANS:
        lines.append("+ лоты в вашу группу или канал")
    if plan in PLAN_AUDIENCE:
        lines.append(f"<i>{PLAN_AUDIENCE[plan]}</i>")
    lines.append(f"3 мес: {long_price} ₽ (≈{round(long_price / 3)} ₽/мес)")
    return "\n".join(lines)


def period_discount_text(months: int) -> str:
    discount = config.PERIOD_DISCOUNT_PERCENT.get(months, 0)
    return f" −{discount}%" if discount else ""


def access_status(user: dict, user_id: int) -> str:
    """One-line access status shared by the start screen, the profile and the tariff menu."""
    if config.is_admin(user_id):
        return "Администратор"
    sub_until = user.get("subscription_until")
    is_allowed = bool(user.get("is_allowed"))
    if sub_until and bool(user.get("trial_used")) and not is_allowed:
        return f"Пробный период до {database.format_subscription_date(sub_until)}"
    plan = config.plan_title(user.get("tariff_plan"))
    if sub_until and is_allowed:
        return f"Тариф «{plan}» до {database.format_subscription_date(sub_until)}"
    if is_allowed:
        return f"Тариф «{plan}», бессрочно"
    return "Подписка не активна"


def short_access(user: dict, user_id: int) -> str:
    """«сейчас Полный до 28.11» for the clean tariff screen header."""
    if config.is_admin(user_id):
        return "администратор"
    sub_until = user.get("subscription_until")
    until = re.search(r"\d{2}\.\d{2}", database.format_subscription_date(sub_until) or "") if sub_until else None
    till = f" до {until.group(0)}" if until else ""
    if sub_until and user.get("trial_used") and not user.get("is_allowed"):
        return f"сейчас пробный период{till}"
    if user.get("is_allowed"):
        return f"сейчас {config.plan_title(user.get('tariff_plan'))}{till}"
    return "подписка не активна"


def can_take_trial(user: dict, user_id: int) -> bool:
    return not config.is_admin(user_id) and not user.get("subscription_until") and not user.get("trial_used")


@router.message(CommandStart())
@router.callback_query(F.data == "start_approved")
async def cmd_start(event, state: FSMContext):
    await state.clear()
    user = event.from_user
    first_name = user.first_name or "Пользователь"
    if not isinstance(event, CallbackQuery):
        # /start ref_<code>: only a user the bot has never seen is bound to the link's owner.
        try:
            bound = await referrals.bind_from_start(user.id, user.username, first_name, event.text)
        except Exception:
            logger.exception("Referral binding failed: user=%s", user.id)
            bound = None
        if bound:
            await referrals.notify_bound(event.bot, bound)
    user_record = await database.get_or_create_user(user.id, user.username, first_name)
    await database.resume_blocked_searches(user.id)

    text = (
        f"{ui.e('logo')} <b>Avito Radar</b>\n"
        "Новые объявления Авито приходят в Telegram, как только появляются в поиске.\n\n"
        f"{ui.e('hello')} <b>{html.escape(first_name)}</b> · {access_status(user_record, user.id)}\n\n"
        "<b>Как начать</b>\n"
        "1. Откройте <code>avito.ru</code>, задайте поиск и фильтры.\n"
        "2. Скопируйте ссылку из адресной строки и пришлите сюда.\n"
        "Бот запомнит выдачу и будет присылать каждый новый лот."
    )
    markup = main_menu_kb()
    is_new = can_take_trial(user_record, user.id)
    if is_new:
        text, markup = trial_gate.offer()

    if isinstance(event, CallbackQuery):
        await ui.show(event, text, markup)
        return
    # A typed /start also refreshes the bottom keyboard (new labels, admin button).
    await event.answer("Меню закреплено внизу экрана.", reply_markup=get_main_keyboard(user.id))
    await event.answer(text, reply_markup=markup, parse_mode="HTML")


@router.my_chat_member()
async def on_bot_unblocked(update: ChatMemberUpdated):
    """Unblocking sends no message, so this update is the only sign that the person is back."""
    if update.chat.type == ChatType.PRIVATE and update.new_chat_member.status == ChatMemberStatus.MEMBER:
        await database.resume_blocked_searches(update.from_user.id)


@router.message(F.text.in_(ui.menu_texts("help")))
@router.callback_query(F.data == "open_help_menu")
@router.message(Command("help"))
async def cmd_help(event):
    if ui.clean():
        tariffs = "\n".join(
            f"{plan_icon(plan)} {config.plan_title(plan)} — {config.rub_price(plan)} ₽ · "
            f"{radars_word(config.plan_radars(plan))} · {config.plan_interval(plan)} с"
            + (" · лоты в группу" if plan in config.GROUP_DELIVERY_PLANS else "")
            for plan in config.PAID_PLANS
        )
        text = (
            f"{ui.e('help')} <b>Как пользоваться</b>\n\n"
            f"{ui.e('radar')} <b>Радар</b>\n"
            "Пришлите ссылку на поиск Авито с нужными фильтрами — бот будет присылать каждое новое объявление. "
            "Уже выложенные лоты не приходят, только новые. "
            f"После паузы радар пришлёт лоты за последние {config.RESUME_MAX_AGE_MINUTES} минут.\n\n"
            f"{ui.e('shield')} <b>Фильтры</b>\n"
            "Стоп-слова — лоты с этими словами не придут\n"
            "Чёрный список — скрыть все объявления продавца\n"
            "Область поиска — только заголовок или вместе с описанием\n"
            "Цена, обязательные слова, только с доставкой, без продвинутых\n"
            "Сообщение продавцу — кнопка под лотом копирует ваш текст\n\n"
            f"{ui.e('tariffs')} <b>Тарифы</b>\n"
            f"{tariffs}\n"
            f"Пробный период — {radars_word(config.TRIAL_RADARS)} на {config.TRIAL_TEXT} за подписку на канал\n\n"
            f"{ui.e('time')} <b>Когда приходит лот</b>\n"
            "<blockquote expandable>Бот проверяет ссылку с частотой вашего тарифа и присылает лот сразу, как найдёт. "
            "Авито сначала проверяет объявление на модерации, поэтому в лоте может стоять «3 минуты назад» — "
            "это время публикации, а не появления в поиске. При сбоях на Авито или в сети бывает дольше. "
            "Лоты старше лимита радара не приходят, по умолчанию 5 минут.</blockquote>"
        )
    else:
      text = (
        f"{ui.e('help')} <b>Как пользоваться</b>\n\n"
        f"{ui.e('radar')} <b>1. Добавьте радар</b>\n"
        "Откройте <code>avito.ru</code>, задайте запрос и фильтры: город, цену, состояние. "
        "Скопируйте ссылку из адресной строки и пришлите боту. "
        "Сортировку по дате бот включит сам; уже выложенные лоты не придут, только новые.\n"
        f"После паузы радар пришлёт лоты за последние {config.RESUME_MAX_AGE_MINUTES} минут.\n\n"
        f"{ui.e('shield')} <b>2. Отсекайте лишнее</b>\n"
        "• <b>Стоп-слова</b>: лоты с этими словами не придут («запчасти», «реплика»).\n"
        "• <b>Чёрный список</b>: кнопка под лотом скрывает все объявления продавца.\n"
        "• <b>Область поиска</b>: только заголовок или заголовок с описанием.\n"
        "• <b>Фильтры радара</b>: цена, обязательные слова, только с доставкой, без продвинутых.\n"
        "• <b>Сообщение продавцу</b>: кнопка под лотом копирует ваш текст.\n\n"
        f"{ui.e('tariffs')} <b>3. Тарифы</b>\n"
        f"Пробный период за подписку на канал: {radars_word(config.TRIAL_RADARS)} на {config.TRIAL_TEXT}, "
        "остальное как на тарифе «Полный».\n"
        f"{plan_summary('lite')}\n"
        f"{plan_summary('standart')}\n"
        f"{plan_summary('pro')}\n\n"
        f"{ui.e('time')} <b>4. Когда приходит лот</b>\n"
        "Бот проверяет ссылку с частотой вашего тарифа и присылает лот, как только найдёт его. "
        "Новое объявление Авито сначала проверяет на модерации и только потом показывает в поиске. "
        "Бот пришлёт лот сразу, как только тот пройдёт модерацию. При этом в лоте может стоять время "
        "«3 минуты назад» или больше: Авито считает его от публикации, а не от появления в поиске. "
        "При сбоях на Авито или в сети бывает дольше. "
        "Лоты старше лимита радара не приходят, по умолчанию лимит 5 минут."
    )
    markup = ui.kb(
        [ui.btn("Добавить радар", "radar", callback_data="add_search_prompt", style="primary")],
        [ui.btn("Тарифы", "tariffs", callback_data="open_subscription_menu"),
         ui.btn("Поддержка", "support", callback_data="open_support_stub")],
        [ui.home_btn()],
    )
    await ui.show(event, text, markup)


@router.message(F.text.in_(ui.menu_texts("profile")))
@router.callback_query(F.data == "refresh_profile")
@router.message(Command("profile"))
async def cmd_profile(event):
    user_id = event.from_user.id
    if isinstance(event, CallbackQuery):
        await event.answer("Обновлено")

    user = await database.get_or_create_user(user_id)
    searches = await database.get_user_searches(user_id)
    searches_count = len(searches)
    has_access = await database.is_user_allowed(user_id)
    active_count = sum(1 for s in searches if s.get("is_active")) if has_access else 0
    is_adm = config.is_admin(user_id)
    max_radars = await database.get_user_max_radars(user_id)
    scan_interval = config.plan_interval(database.classify_user_tariff(user)[0])

    from scheduler import get_last_scan_info
    blocked_count = len(await database.get_user_blocked_sellers_list(user_id))

    if ui.clean():
        text = (
            f"{ui.e('profile')} <b>Профиль</b>\n\n"
            f"Доступ: {access_status(user, user_id)}\n"
            f"Радары: {searches_count} из {max_radars}, работают {active_count}\n"
            f"Проверка: раз в {scan_interval} с"
        )
    else:
        text = (
            f"{ui.e('profile')} <b>Профиль</b>\n\n"
            f"<b>Доступ:</b> {access_status(user, user_id)}\n"
            f"<b>Радары:</b> {searches_count} из {max_radars}, работают {active_count}\n"
            f"<b>Проверка:</b> раз в {scan_interval} с\n"
            f"<b>Чёрный список:</b> {blocked_count}\n"
            f"<b>Последний скан:</b> {get_last_scan_info()}\n"
            f"<b>ID:</b> <code>{user_id}</code>"
        )

    rows = []
    if can_take_trial(user, user_id):
        rows.append([ui.btn(f"Пробный период {config.TRIAL_TEXT}", "trial", callback_data="activate_trial_btn", style="success")])
    if not is_adm:
        has_plan = user.get("subscription_until") or user.get("is_allowed")
        rows.append([ui.btn("Продлить или сменить тариф" if has_plan else "Выбрать тариф", "tariffs",
                            callback_data="open_subscription_menu", style=None if has_plan else "primary")])
    rows.append([
        ui.btn(f"Радары ({searches_count})", "radars", callback_data="list_searches"),
        ui.btn(f"Чёрный список ({blocked_count})", "ban", callback_data="manage_blacklist"),
    ])
    night_mode, night_from, night_to = await database.get_night_settings(user_id)
    night = "выкл" if night_mode == database.NIGHT_OFF else f"{night_from:02d}–{night_to:02d}"
    rows.append([ui.btn(f"Ночной режим: {night}", "moon", callback_data="night_mode")])
    group = "вкл" if await database.get_mirror_chat(user_id) else "выкл"
    rows.append([ui.btn(f"Лоты в группу или канал: {group}", "users", callback_data="group_delivery")])
    if await referrals.get_owner_link(user_id):
        rows.append([ui.btn("Партнёрская программа", "link", callback_data="ref_me")])
    rows.append([ui.btn("Обновить", "refresh", callback_data="refresh_profile"), ui.home_btn()])
    await ui.show(event, text, ui.kb(*rows), answer=False)


# mode -> (short label, explanation)
NIGHT_MODES = {
    database.NIGHT_OFF: ("выкл", "лоты приходят круглосуточно со звуком"),
    database.NIGHT_SILENT: ("без звука", "ночью лоты приходят без звука и вибрации"),
    database.NIGHT_HOLD: ("не присылать", "ночью лоты не приходят совсем"),
}


@router.callback_query(F.data == "night_mode")
@router.callback_query(F.data.startswith(("night_mode:", "night_from:", "night_to:")))
async def cb_night_mode(call: CallbackQuery):
    user_id = call.from_user.id
    if ":" in call.data:
        field, value = call.data.split(":", 1)
        value = int(value) if value.isdigit() else -1
        if field == "night_mode" and value in NIGHT_MODES:
            await database.set_night_settings(user_id, mode=value)
            log_user_event(user_id, call.from_user.username, call.from_user.first_name, "Ночной режим", NIGHT_MODES[value][0], status="OK")
            await call.answer(f"Ночной режим: {NIGHT_MODES[value][0]}")
        elif field in ("night_from", "night_to") and 0 <= value < 24:
            await database.set_night_settings(user_id, **{"from_hour" if field == "night_from" else "to_hour": value})
            await call.answer("Время сохранено")
    mode, from_hour, to_hour = await database.get_night_settings(user_id)
    status = (
        "Выключен: лоты приходят круглосуточно со звуком." if mode == database.NIGHT_OFF
        else f"С <b>{from_hour:02d}:00</b> до <b>{to_hour:02d}:00</b> (МСК) {NIGHT_MODES[mode][1].split('ночью ')[-1]}."
    )
    text = (
        f"{ui.e('pause')} <b>Ночной режим</b>\n\n{status}\n\n"
        + "\n".join(f"{'●' if m == mode else '○'} <b>{label.capitalize()}</b> — {hint}"
                    for m, (label, hint) in NIGHT_MODES.items())
    )
    rows = [[ui.btn(("✓ " if m == mode else "") + label.capitalize(), callback_data=f"night_mode:{m}")
             for m, (label, _hint) in NIGHT_MODES.items()]]
    if mode != database.NIGHT_OFF:
        rows.append([ui.btn(f"Начало: {from_hour:02d}:00", "time", callback_data="night_pick:from"),
                     ui.btn(f"Конец: {to_hour:02d}:00", "time", callback_data="night_pick:to")])
    rows.append([ui.btn("« Профиль", callback_data="refresh_profile"), ui.home_btn()])
    await ui.show(call, text, ui.kb(*rows))


@router.callback_query(F.data.startswith("night_pick:"))
async def cb_night_pick(call: CallbackQuery):
    which = "from" if call.data.endswith(":from") else "to"
    _mode, from_hour, to_hour = await database.get_night_settings(call.from_user.id)
    current = from_hour if which == "from" else to_hour
    rows = []
    for start in range(0, 24, 6):
        rows.append([ui.btn(("✓" if h == current else "") + f"{h:02d}", callback_data=f"night_{which}:{h}")
                     for h in range(start, start + 6)])
    rows.append([ui.btn("« Назад", callback_data="night_mode")])
    title = "С какого часа" if which == "from" else "До какого часа"
    await ui.show(call, f"{ui.e('time')} <b>{title} включать ночной режим?</b>\nВремя московское.", ui.kb(*rows))


@router.callback_query(F.data == "activate_trial_btn")
async def cb_trial_offer(call: CallbackQuery):
    text, markup = trial_gate.offer()
    await ui.show(call, text, markup)


@router.callback_query(F.data == "trial_check")
async def cb_activate_trial(call: CallbackQuery):
    try:
        subscribed = await trial_gate.is_subscribed(call.bot, call.from_user.id)
    except TelegramAPIError:
        await call.answer("Telegram не ответил. Нажмите кнопку ещё раз через минуту.", show_alert=True)
        return
    if not subscribed:
        await call.answer("Подписка не найдена. Подпишитесь на канал и нажмите кнопку ещё раз.", show_alert=True)
        return
    res = await database.activate_trial(call.from_user.id)
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Активация триала", res["message"], status="OK" if res["success"] else "WARN")
    if not res["success"]:
        await call.answer(res["message"], show_alert=True)
        return
    until = database.format_subscription_date(res.get("until"))
    text = (
        f"{ui.e('trial')} <b>Пробный период включён</b>\n\n"
        f"Действует до {until}: {radars_word(config.TRIAL_RADARS)}, остальное как на тарифе «Полный», "
        f"проверка раз в {config.plan_interval('pro')} с.\n"
        "Пришлите ссылку на поиск Авито, и первый радар заработает."
    )
    await ui.show(call, text, ui.kb(
        [ui.btn("Добавить радар", "radar", callback_data="add_search_prompt", style="primary")],
        [ui.btn("Инструкция", "help", callback_data="open_help_menu"), ui.home_btn()],
    ))


@router.message(F.text.in_(ui.menu_texts("tariffs")))
@router.message(Command("subscribe"))
@router.message(Command("tariffs"))
@router.callback_query(F.data == "open_subscription_menu")
async def cmd_subscription_menu(event, state: Optional[FSMContext] = None):
    if state:
        await state.clear()
    user_id = event.from_user.id
    user = await database.get_or_create_user(user_id)
    current = (user.get("tariff_plan") or "").lower() if user.get("is_allowed") else ""

    if ui.clean():
        # Two lines per tariff: name and price, then what it gives; one shared line for the rest.
        cards = "\n\n".join(
            f"{plan_icon(plan)} <b>{config.plan_title(plan)}</b> — {config.rub_price(plan)} ₽/мес\n"
            f"{radars_word(config.plan_radars(plan))} · проверка раз в {config.plan_interval(plan)} с"
            + (" · лоты в группу" if plan in config.GROUP_DELIVERY_PLANS else "")
            for plan in config.PAID_PLANS
        )
        text = (
            f"{ui.e('tariffs')} <b>Тарифы</b> · {short_access(user, user_id)}\n\n"
            f"{cards}\n\n"
            "<blockquote>Во всех тарифах: стоп-слова, фильтры, чёрный список, ночной режим. "
            "Оплата разовая, без автосписаний.</blockquote>"
        )
    else:
        text = (
            f"{ui.e('tariffs')} <b>Тарифы</b>\n"
            f"Сейчас: <b>{access_status(user, user_id)}</b>\n\n"
            f"{plan_card('lite')}\n\n"
            f"{plan_card('standart')}\n\n"
            f"{plan_card('pro')}\n\n"
            "На всех тарифах: стоп-слова, фильтр цены, чёрный список продавцов, ночной режим.\n\n"
            "<blockquote>Чем чаще проверка, тем раньше вы увидите лот и успеете написать первым.</blockquote>\n"
            + (f"Оплата разовая на 1 или 3 месяца, без автосписаний. За 3 месяца{period_discount_text(3)}. "
               if 3 in config.SALE_PERIOD_MONTHS else "Оплата разовая на месяц, без автосписаний. ")
            + "Оплаченные дни добавляются к текущему сроку."
        )

    rows = []
    if can_take_trial(user, user_id):
        rows.append([ui.btn(f"Пробный период {config.TRIAL_TEXT}", "trial", callback_data="activate_trial_btn")])
    for plan in config.PAID_PLANS:
        if ui.clean():
            # Your tariff is the blue one with a tick; without a tariff the middle one is suggested.
            mine = plan == current
            label = f"{'✓ ' if mine else ''}{config.plan_title(plan)} · {config.rub_price(plan)} ₽"
            style = "primary" if mine or (plan == "standart" and not current) else None
        else:
            mark = " · ваш" if plan == current else ""
            label = f"{config.plan_title(plan)} — {config.rub_price(plan)} ₽{mark}"
            style = "primary" if plan == "standart" and not current else None
        rows.append([ui.btn(label, plan, callback_data=f"buy_tariff:{plan}", style=style)])
    rows.append([
        ui.btn("Условия", "docs", callback_data="payment_terms"),
        ui.btn("Вопрос по оплате", "support", callback_data="payment_support"),
    ])
    rows.append([ui.home_btn()])
    await ui.show(event, text, ui.kb(*rows))


PLAN_PERKS = {
    "lite": "Для личного поиска: квартира, техника, вещь для себя.",
    "standart": "Для активного поиска и перепродажи в небольших городах.",
    "pro": "Для перепродажи в больших городах и конкурентных категориях.",
}


async def plan_change_note(user_id: int, plan: str, months: int = 1) -> str:
    """What a purchase does to the current tariff; empty when nothing changes."""
    user = await database.get_user(user_id)
    if not user:
        return ""
    lines = []
    tariff_changes = user.get("is_allowed") and plan != user.get("tariff_plan")
    left = database.subscription_seconds_left(user) if tariff_changes else 0
    if left:
        days_left = left // 86400
        new_days = config.plan_switch_seconds(user.get("tariff_plan"), plan, left) // 86400
        lines.append(
            f"Сейчас «{config.plan_title(user.get('tariff_plan'))}», осталось {days_left} дн. "
            f"Новый тариф начнёт действовать сразу, остаток пересчитается по цене: {new_days} дн. "
            f"и ещё {config.period_days(months)} оплаченных."
        )
    if config.plan_radars(plan) < await database.count_user_searches(user_id):
        lines.append(f"Работать будут {radars_word(config.plan_radars(plan))}, остальные встанут на паузу.")
    return "".join(f"{ui.e('warn')} {line}\n" for line in lines) + ("\n" if lines else "")


@router.callback_query(F.data.startswith("buy_tariff:"))
async def cb_select_tariff(call: CallbackQuery):
    # buy_tariff:<plan>[:<months>]; old buttons have no months and open one month.
    parts = call.data.split(":")
    tariff_key = parts[1] if len(parts) > 1 else ""
    months = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    test_enabled = config.STARS_TEST_ENABLED and tariff_key == "test"
    if tariff_key not in config.PAID_PLANS and not test_enabled:
        # Old buttons (removed tariffs) lead back to the current tariff list.
        await cmd_subscription_menu(call)
        return
    is_test = tariff_key == "test"
    if is_test or months not in config.SALE_PERIOD_MONTHS:
        months = 1
    title = config.plan_title(tariff_key)
    stars = config.stars_price(tariff_key, months)
    price_label = f"{stars} ⭐" if is_test else f"{config.rub_price(tariff_key, months)} ₽"
    period_label = "1 день" if is_test else config.period_title(months)
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Выбор тарифа",
                   f"Тариф {title} ({price_label} / {period_label})", status="INFO")

    # "test" is a hidden 1-day plan for checking Stars checkout end to end; it grants the Средний tariff.
    plan = "standart" if is_test else tariff_key
    saving = ""
    if not is_test and months > 1:
        saved = config.rub_price(tariff_key) * months - config.rub_price(tariff_key, months)
        saving = f" <i>(выгода {saved} ₽)</i>" if saved > 0 else ""
    if not is_test and config.STARS_PAYMENTS_ENABLED:
        price_label += f" или {stars} ⭐"
    text = (
        f"{plan_icon(plan)} <b>Тариф «{title}»</b> — {price_label} за {period_label}{saving}\n\n"
        f"• До <b>{radars_word(config.plan_radars(plan))}</b>\n"
        f"• Проверка каждого радара раз в <b>{config.plan_interval(plan)} с</b>\n"
        "• Стоп-слова, чёрный список продавцов, поиск по заголовку\n"
        "• Фильтры по цене и словам, сообщение продавцу в одно касание\n"
        + ("• Копии лотов в вашу группу или канал — для работы вдвоём или командой\n" if plan in config.GROUP_DELIVERY_PLANS else "")
        + f"\n<i>{PLAN_PERKS[plan]}</i>\n\n"
        f"{await plan_change_note(call.from_user.id, plan, months)}"
        "Разовая оплата без автосписаний, доступ включается сразу. Оплачивая, вы соглашаетесь с /terms.\n"
        + ("При оплате рублями платёжная система добавляет свою комиссию, точная сумма будет в счёте.\n"
           if config.PLATEGA_ENABLED and not is_test else "")
        + "\n"
        + ("<b>Способ оплаты:</b>" if is_test else "<b>Срок и способ оплаты:</b>")
    )

    rows = []
    if not is_test and len(config.SALE_PERIOD_MONTHS) > 1:
        rows.append([
            ui.btn(("✓ " if m == months else "") + config.period_title(m) + period_discount_text(m),
                   callback_data=f"buy_tariff:{tariff_key}:{m}", style="primary" if m == months else None)
            for m in config.SALE_PERIOD_MONTHS
        ])
    # Payment methods: plain buttons in a 2×2 grid (СБП · Карта / Крипта · Stars).
    pay_buttons = [] if is_test else rub_payments.method_buttons(tariff_key, months)
    if config.STARS_PAYMENTS_ENABLED:
        pay_buttons.append(ui.btn(f"Telegram Stars — {stars} ⭐" if is_test else "Stars", "star",
                                  callback_data=f"stars_buy:{tariff_key}:{months}:{stars}"))
    rows.extend(pay_buttons[i:i + 2] for i in range(0, len(pay_buttons), 2))
    if not config.STARS_PAYMENTS_ENABLED:
        text += "\n\n⏸ Оплата Telegram Stars временно выключена."
    rows.append([
        ui.btn("« Все тарифы", callback_data="open_subscription_menu"),
        ui.btn("Вопрос по оплате", "support", callback_data="payment_support"),
    ])
    await ui.show(call, text, ui.kb(*rows))


PAY_METHOD_TITLES = {"sbp": "СБП", "card": "картой", "crypto": "криптовалютой"}


@router.callback_query(F.data.startswith("pay_method:"))
async def cb_pay_method(call: CallbackQuery, bot: Bot):
    # pay_method:<sbp|card|crypto>:<plan>[:<months>]. With Platega keys each opens its own Platega payment page.
    parts = call.data.split(":")
    method = parts[1] if len(parts) > 1 else ""
    if config.PLATEGA_ENABLED and method in rub_payments.METHODS and len(parts) > 2:
        months = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        if months not in config.SALE_PERIOD_MONTHS:
            await call.answer("Такого срока сейчас нет. Откройте тариф заново.", show_alert=True)
            return
        if not call.message or call.message.chat.type != "private":
            await call.answer("Оплата доступна только в личном чате с ботом.", show_alert=True)
            return
        await call.answer("Создаю счёт")
        error = await rub_payments.start_checkout(bot, call.from_user, parts[2], months, method)
        if error:
            await call.message.answer(error)
        return
    title = PAY_METHOD_TITLES.get(method, "этим способом")
    stars_hint = " Сейчас можно оплатить через Telegram Stars." if config.STARS_PAYMENTS_ENABLED else ""
    await call.answer(f"Оплата {title} скоро появится.{stars_hint}", show_alert=True)


@router.callback_query(F.data == "other_payment_methods")
async def cb_other_payment_methods(call: CallbackQuery):
    # The separate "other payment methods" screen is gone; old buttons open the tariff list.
    await cmd_subscription_menu(call)


# ── Документы и регламенты ─────────────────────────────────────────────

@router.message(F.text.in_(ui.menu_texts("docs")))
@router.message(Command("docs"))
@router.callback_query(F.data == "open_docs_menu")
async def cmd_docs_hub(event):
    await ui.show(event, f"{ui.e('docs')} {legal_docs.HUB_TEXT}", get_docs_keyboard())


async def show_doc(event, name: str, page: int = 0) -> None:
    _, icon, pages = legal_docs.DOCS[name]
    page = max(0, min(page, len(pages) - 1))
    await ui.show(event, f"{ui.e(icon)} {legal_docs.doc_header(name, page)}{pages[page]}", get_doc_keyboard(name, page))


@router.message(Command("agreement", "offer", "contract"))
@router.callback_query(F.data == "doc_agreement")
async def cmd_doc_agreement(event):
    await show_doc(event, "agreement")


@router.message(Command("privacy", "policy"))
@router.callback_query(F.data == "doc_privacy")
async def cmd_doc_privacy(event):
    await show_doc(event, "privacy")


@router.callback_query(F.data.startswith("doc_page:"))
async def cb_doc_page(call: CallbackQuery):
    _, name, page = call.data.split(":")
    if name not in legal_docs.DOCS or not page.isdigit():
        await call.answer("Документ не найден", show_alert=True)
        return
    await show_doc(call, name, int(page))


# ── Служба поддержки и контакты ───────────────────────────────────────

@router.message(Command("support", "contacts"))
@router.message(F.text.in_(ui.menu_texts("support")))
@router.callback_query(F.data == "open_support_stub")
async def cmd_support(event, state: Optional[FSMContext] = None):
    if state:
        await state.clear()
    from handlers.support import support_screen
    text, markup = await support_screen(event.from_user.id)
    await ui.show(event, text, markup)


@router.callback_query(F.data == "support_stub_click")
async def cb_support_stub(call: CallbackQuery):
    await cmd_support(call)


# ── Чёрный список продавцов (Бан в 1 клик) ─────────────────────────────

@router.callback_query(F.data == "manage_blacklist")
async def cb_manage_blacklist(call: CallbackQuery):
    blocked = await database.get_user_blocked_sellers_list(call.from_user.id)
    back_row = [ui.btn("« Профиль", callback_data="refresh_profile"), ui.home_btn()]
    if not blocked:
        text = (
            f"{ui.e('ban')} <b>Чёрный список пуст</b>\n\n"
            "Нажмите «Скрыть продавца» под лотом: его объявления перестанут приходить по всем радарам."
        )
        await ui.show(call, text, ui.kb(back_row))
        return
    text = (
        f"{ui.e('ban')} <b>Чёрный список</b> · {len(blocked)}\n\n"
        "Объявления этих продавцов не приходят. Нажмите на продавца, чтобы вернуть его."
    )
    if len(blocked) > 12:
        text += f"\n<i>Показаны первые 12 из {len(blocked)}.</i>"
    rows = [
        [ui.btn(b["seller_name"][:30] if (ui.clean() and ui.custom_id("cancel")) else f"✖ {b['seller_name'][:30]}",
                "cancel", callback_data=f"unban_seller:{b['id']}")]
        for b in blocked[:12]
    ]
    rows.append(back_row)
    await ui.show(call, text, ui.kb(*rows))


@router.callback_query(F.data.startswith("unban_seller:"))
async def cb_unban_seller(call: CallbackQuery):
    block_id = int(call.data.split(":")[1])
    await database.remove_blocked_seller_by_id(call.from_user.id, block_id)
    await call.answer("Продавец убран из чёрного списка")
    await cb_manage_blacklist(call)


@router.callback_query(F.data.startswith("ban_seller:"))
async def cb_ban_seller(call: CallbackQuery):
    user_id = call.from_user.id
    parts = call.data.split(":", 1)
    raw_id = parts[1].strip() if len(parts) > 1 else ""

    seller_name = None
    seller_key = ""
    if raw_id.isdigit() and int(raw_id) > 0:
        event_id = int(raw_id)
        event = await database.get_event_for_callback(event_id)
        if event is None or not event.get("active_radars"):
            await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
            return
        if int(event["user_id"]) != int(user_id):
            await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
            return
        item_obj = event.get("item") or {}
        if isinstance(item_obj.get("item"), dict):
            item_obj = item_obj["item"]
        seller_name = (item_obj.get("seller_name") or "").strip()
        seller_key = (item_obj.get("seller_key") or "").strip()

    if not seller_name and call.message:
        # Old notifications: the seller line is "👤 Продавец: Имя" in the plain message text.
        msg_text = call.message.caption or call.message.text or ""
        m = re.search(r"👤\s*Продавец:\s*([^·\n]+)", msg_text)
        if m:
            seller_name = m.group(1).strip()

    if not seller_name or seller_name.lower() in {"продавец", "компания", "частное лицо"}:
        await call.answer("Не удалось определить имя продавца", show_alert=True)
        return

    await database.add_blocked_seller(user_id, seller_name, seller_key)
    log_user_event(user_id, call.from_user.username, call.from_user.first_name, "Блокировка продавца", f"Продавец «{seller_name}» добавлен в ЧС", status="OK")
    logger.info(f"🚷 [ЧЕРНЫЙ СПИСОК] Пользователь ID:{user_id} заблокировал продавца «{seller_name}»")

    await call.answer(
        f"🚫 «{seller_name}» в чёрном списке.\nЕго лоты больше не придут. Вернуть: Профиль → Чёрный список.",
        show_alert=True
    )
