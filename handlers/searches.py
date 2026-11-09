import html
import json
import logging
import re
from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from typing import Optional, Tuple, List
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import config
import database
import link_check
import radar_filters
import ui
from parser import AvitoParser
from source_cache import SOURCE_SNAPSHOTS, BadSourceLink
from logger_setup import log_user_event

logger = logging.getLogger(__name__)
router = Router()

# Множество нормализованных URL и ID радаров, где радар был только что снят с паузы
RESUMED_SOURCE_URLS: set[str] = set()
RESUMED_RADAR_IDS: set[int] = set()


def pop_resumed_source_urls() -> set[str]:
    res = set(RESUMED_SOURCE_URLS)
    RESUMED_SOURCE_URLS.clear()
    return res


def consume_resumed_radar_id(radar_id: int) -> bool:
    if radar_id in RESUMED_RADAR_IDS:
        RESUMED_RADAR_IDS.discard(radar_id)
        return True
    return False


class SearchStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_stop_word = State()
    waiting_for_custom_age = State()
    waiting_for_rename = State()
    waiting_for_new_url = State()
    waiting_for_price = State()
    waiting_for_must_words = State()
    waiting_for_reply_template = State()


async def route_menu_text(message: Message, state: FSMContext) -> bool:
    """A bottom-menu button or a command pressed in the middle of an input step: leave the step
    and open what was pressed. Returns False when the text is ordinary input."""
    text = (message.text or "").strip()
    if text not in ui.ALL_MENU_TEXTS and not text.startswith("/"):
        return False
    await state.clear()
    from handlers import common
    if text in ui.menu_texts("add"):
        await btn_add_search(message, state)
    elif text in ui.menu_texts("radars"):
        await show_searches(message, state)
    elif text in ui.menu_texts("tariffs"):
        await common.cmd_subscription_menu(message)
    elif text in ui.menu_texts("profile"):
        await common.cmd_profile(message)
    elif text in ui.menu_texts("help"):
        await common.cmd_help(message)
    elif text in ui.menu_texts("docs"):
        await common.cmd_docs_hub(message)
    elif text in ui.menu_texts("support"):
        await common.cmd_support(message)
    elif text in ui.menu_texts("admin") and config.is_admin(message.from_user.id):
        from handlers.admin import cmd_admin_home
        await cmd_admin_home(message)
    elif text.startswith("/start"):
        await common.cmd_start(message, state)
    else:
        await message.answer(f"{ui.e('cancel')} Ввод отменён.", reply_markup=ui.kb([ui.home_btn()]), parse_mode="HTML")
    return True


# ── Добавление радара ──────────────────────────────────────────────────

_AVITO_URL_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)*avito\.ru/[^\s<>\"']+", re.IGNORECASE)


def extract_avito_urls(text: str) -> List[str]:
    """Все ссылки на Авито из текста, в порядке появления, без повторов."""
    seen: set[str] = set()
    urls = []
    for match in _AVITO_URL_RE.findall(text or ""):
        url = match if match.lower().startswith("http") else f"https://{match}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


async def bulk_add_radars(message: Message, state: FSMContext, urls: List[str]) -> None:
    """Админ присылает много ссылок одним сообщением: каждая становится радаром.
    Калибровка откладывается до первого скана, чтобы не делать десятки запросов к Авито разом."""
    user_id = message.from_user.id
    await state.clear()
    limit = int(config.BULK_RADAR_MAX)
    extra_urls = max(0, len(urls) - limit)
    urls = urls[:limit]

    existing = {s["url"] for s in await database.get_user_searches(user_id)}
    max_radars = await database.get_user_max_radars(user_id)
    current_count = await database.count_user_searches(user_id)

    added, duplicates, invalid, over_limit = [], 0, 0, 0
    for raw in urls:
        if "/item/" in raw or re.search(r"_\d{8,}", raw) or link_check.link_problem(raw) is not None:
            invalid += 1
            continue
        try:
            normalized_url = AvitoParser.normalize_url(raw)
        except Exception:
            invalid += 1
            continue
        if normalized_url in existing:
            duplicates += 1
            continue
        if current_count >= max_radars:
            over_limit += 1
            continue
        title = AvitoParser.extract_search_title("", normalized_url)
        try:
            search_id = await database.add_search(
                user_id, title, normalized_url,
                search_in_title_only=1 if "bt=1" in normalized_url else 0,
                initial_items=None,
            )
        except database.RadarLimitError:
            over_limit += 1
            continue
        RESUMED_SOURCE_URLS.add(normalized_url)
        existing.add(normalized_url)
        current_count += 1
        added.append((search_id, title))

    log_user_event(
        user_id, message.from_user.username, message.from_user.first_name, "Массовое добавление радаров",
        f"добавлено={len(added)} повторы={duplicates} ошибки={invalid} сверх лимита={over_limit + extra_urls}",
        status="OK" if added else "WARN",
    )
    lines = [
        f"{ui.e('box')} <b>Массовое добавление</b>",
        "",
        f"{ui.e('ok')} Добавлено: <b>{len(added)}</b>",
    ]
    if duplicates:
        lines.append(f"♻️ Уже были: <b>{duplicates}</b>")
    if invalid:
        lines.append(f"{ui.e('warn')} Не ссылки на поиск: <b>{invalid}</b>")
    if over_limit:
        lines.append(f"🚫 Не влезли в лимит ({max_radars}): <b>{over_limit}</b>")
    if extra_urls:
        lines.append(f"✂️ Сверх {limit} ссылок за раз: <b>{extra_urls}</b> (пришлите отдельным сообщением)")
    for _sid, title in added[:15]:
        lines.append(f"• {html.escape(title)}")
    if len(added) > 15:
        lines.append(f"• … и ещё {len(added) - 15}")
    lines.append("")
    lines.append("<i>Калибровка пройдёт при первом скане: уже выложенные лоты не придут.</i>")

    rows = [[ui.btn("Мои радары", "radars", callback_data="list_searches")]]
    if over_limit:
        rows.insert(0, [ui.btn("+10 к моему лимиту", "add", callback_data="admin_radar_limit:self:10:0")])
    await message.answer("\n".join(lines), reply_markup=ui.kb(*rows), parse_mode="HTML")


@router.message(F.text.in_(ui.menu_texts("add")))
@router.callback_query(F.data == "add_search_prompt")
async def btn_add_search(event, state: FSMContext):
    await state.set_state(SearchStates.waiting_for_url)
    if ui.clean():
        text = (
            f"{ui.e('radar')} <b>Новый радар</b>\n\n"
            "Пришлите ссылку на поиск Авито — бот будет проверять её и присылать каждое новое объявление.\n\n"
            "<b>Как получить ссылку</b>\n"
            "1. Найдите товар на avito.ru\n"
            "2. Задайте город, цену и другие фильтры\n"
            "3. Скопируйте адрес страницы и отправьте сюда\n\n"
            "<blockquote><code>https://www.avito.ru/moskva/telefony?q=iphone</code></blockquote>\n"
            "Фильтры сохраняются в ссылке. Сортировку менять не нужно: бот сам смотрит самые новые объявления."
        )
        if config.is_admin(event.from_user.id):
            text += (f"\n\n{ui.e('admin')} До {int(config.BULK_RADAR_MAX)} ссылок за раз, по одной на строку — "
                     "каждая станет отдельным радаром.")
    else:
        text = (
            f"{ui.e('radar')} <b>Новый радар</b>\n\n"
            "Пришлите ссылку на поиск Авито:\n"
            "1. Откройте <b>avito.ru</b> и найдите нужный товар.\n"
            "2. Выставьте фильтры: город, цена, состояние.\n"
            "3. Скопируйте ссылку из адресной строки и отправьте сюда.\n\n"
            "<blockquote>Пример: <code>https://www.avito.ru/moskva/telefony?q=iphone</code></blockquote>"
        )
    if config.is_admin(event.from_user.id) and not ui.clean():
        text += (
            f"\n{ui.e('admin')} Можно прислать до {int(config.BULK_RADAR_MAX)} ссылок, "
            "по одной на строку: каждая станет отдельным радаром."
        )
    markup = ui.kb(
        [ui.btn("Отмена", "cancel", callback_data="cancel_fsm"),
         ui.btn("Мои радары", "radars", callback_data="list_searches")],
    )
    await ui.show(event, text, markup)


@router.callback_query(F.data == "cancel_fsm")
async def cb_cancel_fsm(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.show(call, f"{ui.e('cancel')} <b>Отменено</b>", ui.kb(
        [ui.btn("Мои радары", "radars", callback_data="list_searches"), ui.home_btn()],
    ))


def _url_error_kb() -> InlineKeyboardMarkup:
    return ui.kb(
        [ui.btn("Отмена", "cancel", callback_data="cancel_fsm"),
         ui.btn("Инструкция", "help", callback_data="open_help_menu")],
    )


def search_url_error(raw_url: str) -> Optional[str]:
    """Why a text is not a usable Avito search link, or None."""
    if "avito.ru" not in raw_url.lower():
        return (f"{ui.e('warn')} <b>Нужна ссылка с avito.ru</b>\n\n"
                "Например: <code>https://www.avito.ru/moskva/telefony?q=iphone</code>")
    if "/item/" in raw_url or re.search(r"_\d{8,}", raw_url):
        return (f"{ui.e('warn')} <b>Это ссылка на одно объявление</b>\n\n"
                "Радару нужна ссылка на <b>страницу поиска</b>: со списком объявлений и фильтрами.")
    problem = link_check.link_problem(raw_url)
    if problem is not None:
        return link_check.problem_message(problem)
    return None


def bad_link_text(outcome: str) -> str:
    return (f"{ui.e('warn')} <b>По этой ссылке нет объявлений</b>\n\n"
            f"Авито не показал по ней страницу поиска. {outcome}\n\n"
            "<blockquote>Откройте поиск на avito.ru и скопируйте адрес из браузера целиком.</blockquote>")


# Registered before process_avito_url: a link sent while replacing a radar's link must not become a new radar.
@router.message(SearchStates.waiting_for_new_url)
async def process_new_url(message: Message, state: FSMContext):
    if await route_menu_text(message, state):
        return
    raw_url = (message.text or "").strip()
    search_id = (await state.get_data()).get("url_search_id")
    s = await database.get_search(int(search_id or 0))
    if not s or s.get("user_id") != message.from_user.id:
        await state.clear()
        await message.answer("Радар не найден.", reply_markup=ui.kb([ui.btn("Мои радары", "radars", callback_data="list_searches")]))
        return
    error = search_url_error(raw_url)
    if error is None:
        try:
            normalized_url = AvitoParser.normalize_url(raw_url)
        except Exception as e:
            error = f"{ui.e('warn')} Не получилось разобрать ссылку: {html.escape(str(e))}"
    if error is not None:
        await message.answer(error, parse_mode="HTML", reply_markup=ui.kb(
            [ui.btn("Отмена", "cancel", callback_data=f"open_search:{search_id}")]))
        return
    if any(r["url"] == normalized_url and r["id"] != s["id"] for r in await database.get_user_searches(message.from_user.id)):
        await message.answer(f"{ui.e('warn')} Эта ссылка уже есть в другом вашем радаре.", parse_mode="HTML")
        return

    await state.clear()
    status_msg = await message.answer(f"{ui.e('wait')} <b>Запоминаю новую выдачу…</b>", parse_mode="HTML")
    try:
        snapshot = await SOURCE_SNAPSHOTS.load(normalized_url)
    except BadSourceLink:
        log_user_event(message.from_user.id, message.from_user.username, message.from_user.first_name, "Замена ссылки радара",
                       f"Ссылка без каталога: «{normalized_url[:80]}»", status="WARN")
        await status_msg.edit_text(bad_link_text("Прежняя ссылка радара осталась."), parse_mode="HTML", reply_markup=ui.kb(
            [ui.btn("К радару", "radar", callback_data=f"open_search:{search_id}")]))
        return
    except Exception as e:
        logger.warning("Ошибка калибровки при замене ссылки: %s", e)
        snapshot = None
    initial_items = [(item.item_id, item.raw_price) for item in snapshot.items] if snapshot is not None else None
    if snapshot is None:
        RESUMED_SOURCE_URLS.add(normalized_url)
    await database.replace_search_url(
        s["id"], message.from_user.id, normalized_url, 1 if "bt=1" in normalized_url else 0,
        initial_items=initial_items,
    )
    log_user_event(message.from_user.id, message.from_user.username, message.from_user.first_name, "Замена ссылки радара",
                   f"Радар #{s['id']} -> {normalized_url[:80]}", status="OK")
    s = await database.get_search(s["id"])
    card_text, markup = build_radar_card(s)
    note = (f"{ui.e('ok')} <b>Ссылка заменена.</b> Уже выложенные лоты новой выдачи не придут; "
            "название и стоп-слова остались прежними.\n\n")
    await status_msg.edit_text(note + card_text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


# Only outside other steps: a seller message or a stop word that mentions avito.ru is not a new radar.
@router.message(StateFilter(None), F.text.regexp(r"(?i)avito\.ru"))
@router.message(SearchStates.waiting_for_url)
async def process_avito_url(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пришлите ссылку текстом.", reply_markup=_url_error_kb())
        return
    if await route_menu_text(message, state):
        return
    raw_url = message.text.strip()
    user_id = message.from_user.id

    if config.is_admin(user_id):
        bulk_urls = extract_avito_urls(raw_url)
        if len(bulk_urls) > 1:
            await bulk_add_radars(message, state, bulk_urls)
            return

    url_error = search_url_error(raw_url)
    if url_error is not None:
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", f"Ошибка ссылки: «{raw_url[:80]}»", status="WARN")
        await message.answer(url_error, reply_markup=_url_error_kb(), parse_mode="HTML")
        return

    current_count = await database.count_user_searches(user_id)
    max_radars = await database.get_user_max_radars(user_id)
    if current_count >= max_radars:
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", f"Ошибка: лимит {max_radars} исчерпан", status="WARN")
        limit_button = (
            ui.btn("+10 к моему лимиту", "add", callback_data="admin_radar_limit:self:10:0")
            if config.is_admin(user_id)
            else ui.btn("Тариф с бо́льшим лимитом", "tariffs", callback_data="open_subscription_menu", style="primary")
        )
        await message.answer(
            f"{ui.e('warn')} <b>Все радары заняты ({current_count} из {max_radars})</b>\n\n"
            "Удалите ненужный радар или перейдите на тариф с бо́льшим лимитом: "
            f"Средний — {config.plan_radars('standart')}, Полный — {config.plan_radars('pro')}.",
            reply_markup=ui.kb([limit_button], [ui.btn("Мои радары", "radars", callback_data="list_searches")]),
            parse_mode="HTML"
        )
        await state.clear()
        return

    try:
        normalized_url = AvitoParser.normalize_url(raw_url)
    except Exception as e:
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", f"Ошибка синтаксиса ссылки: {e}", status="WARN")
        await message.answer(
            f"{ui.e('warn')} Не получилось разобрать ссылку: {html.escape(str(e))}",
            reply_markup=_url_error_kb(), parse_mode="HTML",
        )
        return

    same_link = next((r for r in await database.get_user_searches(user_id) if r["url"] == normalized_url), None)
    if same_link is not None:
        await state.clear()
        await message.answer(
            f"{ui.e('warn')} <b>Такой радар уже есть:</b> «{html.escape(same_link['title'])}»",
            reply_markup=ui.kb([ui.btn("Открыть радар", "radar", callback_data=f"open_search:{same_link['id']}")]),
            parse_mode="HTML",
        )
        return

    status_msg = await message.answer(f"{ui.e('wait')} <b>Запоминаю текущую выдачу…</b>", parse_mode="HTML")

    try:
        snapshot = await SOURCE_SNAPSHOTS.load(normalized_url)
    except BadSourceLink:
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара",
                       f"Ссылка без каталога: «{normalized_url[:80]}»", status="WARN")
        await status_msg.edit_text(bad_link_text("Радар не создан."), reply_markup=_url_error_kb(), parse_mode="HTML")
        return
    except Exception as e:
        logger.warning("Ошибка калибровки: %s", e)
        snapshot = None

    if snapshot is None:
        search_title = AvitoParser.extract_search_title("", normalized_url)
        items = []
        initial_items_arg = None
        RESUMED_SOURCE_URLS.add(normalized_url)
        log_user_event(
            user_id,
            message.from_user.username,
            message.from_user.first_name,
            "Калибровка радара",
            f"Отложенная калибровка («{normalized_url[:80]}»)",
            status="WARN",
        )
    else:
        search_title = snapshot.title
        items = list(snapshot.items)
        initial_items_arg = [(item.item_id, item.raw_price) for item in items]

    is_title_only = 1 if "bt=1" in normalized_url else 0

    try:
        search_id = await database.add_search(
            user_id, search_title, normalized_url, search_in_title_only=is_title_only,
            initial_items=initial_items_arg,
        )
    except (database.RadarLimitError, PermissionError) as exc:
        await status_msg.edit_text(f"{ui.e('warn')} {html.escape(str(exc))}", parse_mode="HTML")
        await state.clear()
        return

    log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Радар создан", f"Радар #{search_id} «{search_title}» (лотов: {len(items)})", status="OK")
    logger.info(f"🎯 [РАДАР] Пользователь ID:{user_id} создал радар #{search_id}: «{search_title}» ({len(items)} базовых лотов проиндексировано)")

    await state.clear()

    owner = await database.get_user(user_id) or {"user_id": user_id}
    scan_interval = config.plan_interval(database.classify_user_tariff(owner)[0])
    baseline = (
        f"Уже выложенных лотов: <b>{len(items)}</b>, они не придут."
        if snapshot is not None
        else "Текущую выдачу бот запомнит при первой проверке."
    )
    text = (
        f"{ui.e('ok')} <b>Радар запущен</b>\n\n"
        f"{ui.e('radar')} <b>{html.escape(search_title)}</b>\n"
        f"{ui.e('scope')} Ищет: {_scope_label(bool(is_title_only))}\n"
        f"{ui.e('on')} Проверка раз в {scan_interval} с\n"
        f"{baseline}\n\n"
        "<i>Новый лот придёт сюда сразу после обнаружения.</i>"
    )
    markup = ui.kb(
        [_scope_button(search_id, bool(is_title_only))],
        [ui.btn("Стоп-слова", "shield", callback_data=f"manage_stops:{search_id}"),
         ui.btn("На Авито", "link", url=normalized_url)],
        [ui.btn("Мои радары", "radars", callback_data="list_searches")],
    )
    await status_msg.edit_text(text, reply_markup=markup, parse_mode="HTML")


# ── Список радаров и карточка ──────────────────────────────────────────

def format_max_age_label(max_age_minutes: Optional[int]) -> str:
    if not max_age_minutes or max_age_minutes <= 0:
        max_age_minutes = 5
    elif max_age_minutes > 30:
        max_age_minutes = 30
    return f"до {max_age_minutes} мин. ⚡️"


def _scope_label(title_only: bool) -> str:
    return "только в названиях" if title_only else "в названиях и описаниях"


def _scope_button(search_id: int, title_only: bool):
    label = "Ищет только в названиях" if title_only else "Ищет в названиях и описаниях"
    return ui.btn(label, "scope", callback_data=f"toggle_title_scope:{search_id}")


def _check_line(s: dict, lots_day: Optional[int]) -> str:
    """When the radar was last checked and how much it sent: the user sees that it is alive."""
    import time
    import scan_journal

    if not s["is_active"]:
        return ""
    ok_times = [entry["at"] for entry in scan_journal.scans(s["id"]) if entry["ok"]]
    if ok_times:
        ago = max(0, int(time.time() - ok_times[-1]))
        when = f"{ago} с назад" if ago < 120 else f"{ago // 60} мин назад"
        if ago > 600:
            when += " — Авито сейчас плохо отдаёт выдачу, бот продолжает пробовать"
    else:
        when = "скоро, бот только что запустился"
    line = f"<b>Проверен:</b> {when}\n"
    if lots_day is not None:
        line += f"<b>Прислано за сутки:</b> {lots_day}\n"
    return line


def build_radar_card(s: dict, lots_day: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
    search_id = s["id"]
    is_active = bool(s["is_active"])
    stops_count = len(s["stop_words"])
    title_only = bool(s.get("search_in_title_only", 0))

    status = f"{ui.e('on')} Работает" if is_active else f"{ui.e('pause')} На паузе"
    if s.get("pause_reason") == database.PAUSE_LIMIT:
        status += " (не вошёл в лимит тарифа)"
    elif s.get("pause_reason") == database.PAUSE_BAD_LINK:
        status += " (ссылка не на поиск)"
    card_text = (
        f"{ui.e('radar')} <b>{html.escape(s['title'])}</b>\n\n"
        + link_check.card_warning(s["url"]) +
        f"<b>Статус:</b> {status}\n"
        + _check_line(s, lots_day)
        + f"<b>Ищет:</b> {_scope_label(title_only)}\n"
        f"<b>Стоп-слова:</b> {stops_count or 'нет'}\n"
        f"<b>Фильтры:</b> {html.escape(radar_filters.summary(s))}\n"
        f"<a href=\"{html.escape(s['url'], quote=True)}\">Открыть выдачу на Авито</a>"
    )
    toggle = (
        ui.btn("Пауза", "pause", callback_data=f"toggle_search:{search_id}")
        if is_active else
        ui.btn("Запустить", "play", callback_data=f"toggle_search:{search_id}", style="success")
    )
    markup = ui.kb(
        [toggle, ui.btn(f"Стоп-слова ({stops_count})", "shield", callback_data=f"manage_stops:{search_id}")],
        [_scope_button(search_id, title_only)],
        [ui.btn("Фильтры и сообщение продавцу", "note", callback_data=f"flt_open:{search_id}")],
        [ui.btn("Переименовать", "write", callback_data=f"rename_search:{search_id}"),
         ui.btn("Сменить ссылку", "refresh", callback_data=f"change_url:{search_id}")],
        [ui.btn("На Авито", "link", url=s["url"]),
         ui.btn("Удалить", "trash", callback_data=f"delete_search:{search_id}", style="danger")],
        [ui.btn("« Все радары", callback_data="list_searches"), ui.home_btn()],
    )
    return card_text, markup


async def _show_card(event, s: dict) -> None:
    try:
        lots_day = await database.count_radar_lots(int(s["id"]), hours=24)
    except Exception:
        lots_day = None
    card_text, markup = build_radar_card(s, lots_day)
    await ui.show(event, card_text, markup, disable_web_page_preview=True)


@router.message(F.text.in_(ui.menu_texts("radars")))
@router.callback_query(F.data == "list_searches")
async def show_searches(event, state: Optional[FSMContext] = None):
    user_id = event.from_user.id
    if state:
        await state.clear()
    searches = await database.get_user_searches(user_id)

    if not searches:
        text = (
            f"{ui.e('radars')} <b>Радаров пока нет</b>\n\n"
            "Пришлите ссылку на поиск Авито прямо в чат или нажмите кнопку ниже."
        )
        await ui.show(event, text, ui.kb(
            [ui.btn("Добавить первый радар", "radar", callback_data="add_search_prompt", style="primary")],
            [ui.home_btn()],
        ))
        return

    from scheduler import get_last_scan_info
    max_radars = await database.get_user_max_radars(user_id)
    active_cnt = sum(1 for s in searches if s.get("is_active"))
    lines = [
        f"{ui.e('radars')} <b>Мои радары</b> · {len(searches)} из {max_radars}",
        f"Работают: {active_cnt} · последний скан {get_last_scan_info()}",
        "",
    ]
    for i, s in enumerate(searches, 1):
        dot = ui.e("on") if s.get("is_active") else ui.e("pause")
        stops_cnt = len(s["stop_words"])
        stops_str = f" · {ui.e('shield')} {stops_cnt}" if stops_cnt else ""
        lines.append(f"{dot} <b>{i}. {html.escape(s['title'])}</b>{stops_str}")
    lines.append("")
    lines.append("<i>Нажмите на радар, чтобы настроить его.</i>")

    rows = []
    for i, s in enumerate(searches, 1):
        raw_title = s["title"].strip()
        short_title = (raw_title[:27] + "…") if len(raw_title) > 28 else raw_title
        rows.append([ui.btn(f"{i}. {short_title}", "on" if s.get("is_active") else "pause",
                            callback_data=f"open_search:{s['id']}")])
    add_row = [ui.btn("Обновить", "refresh", callback_data="list_searches")]
    if len(searches) < max_radars:
        if ui.clean():
            # The main action of the screen: alone, blue, above the list.
            rows.insert(0, [ui.btn("Добавить радар", "add", callback_data="add_search_prompt", style="primary")])
        else:
            add_row.insert(0, ui.btn("Добавить", "add", callback_data="add_search_prompt"))
    rows.append(add_row)
    if config.is_admin(user_id) and len(searches) > 1:
        rows.append([ui.btn("Удалить несколько", "trash", callback_data="bulk_del:new"),
                     ui.btn("Удалить все", "trash", callback_data="bulk_wipe", style="danger")])
    rows.append([ui.home_btn()])
    await ui.show(event, "\n".join(lines), ui.kb(*rows))


async def _own_search(call: CallbackQuery, search_id: int) -> Optional[dict]:
    s = await database.get_search(search_id)
    if not s or s.get("user_id") != call.from_user.id:
        await call.answer("Радар не найден", show_alert=True)
        return None
    return s


@router.callback_query(F.data.startswith("open_search:"))
async def cb_open_search(call: CallbackQuery):
    try:
        search_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректная кнопка радара", show_alert=True)
        return
    s = await _own_search(call, search_id)
    if s:
        await _show_card(call, s)


# ── Пауза / Возобновление ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("toggle_search:"))
async def cb_toggle_search(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    current = await database.get_search(search_id)
    if (current and current.get("user_id") == call.from_user.id and not current.get("is_active")
            and not link_check.keeps_running(current.get("url") or "")):
        await call.answer("Эта ссылка не страница поиска, радар ничего не пришлёт. Нажмите «Сменить ссылку».", show_alert=True)
        return
    try:
        new_status = await database.toggle_search_active(search_id, call.from_user.id)
    except database.RadarLimitError as exc:
        await call.answer(f"{exc}. Поставьте другой радар на паузу или смените тариф.", show_alert=True)
        return

    if new_status is None:
        await call.answer("Радар не найден", show_alert=True)
        return

    status_str = "возобновлён 🟢" if new_status else "приостановлен ⏸"
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Статус радара", f"Радар #{search_id} -> {status_str}", status="OK")
    logger.info(f"⏯ [РАДАР] Пользователь ID:{call.from_user.id} переключил статус радара #{search_id} -> {status_str}")
    await call.answer(
        f"Радар запущен. Пришлю лоты за последние {config.RESUME_MAX_AGE_MINUTES} минут"
        if new_status else "Радар на паузе"
    )

    s = await database.get_search(search_id)
    if not s:
        return

    if new_status:
        RESUMED_RADAR_IDS.add(search_id)
        try:
            RESUMED_SOURCE_URLS.add(AvitoParser.normalize_url(s["url"]))
        except Exception:
            pass
    else:
        RESUMED_RADAR_IDS.discard(search_id)

    card_text, markup = build_radar_card(s)
    await ui.show(call, card_text, markup, answer=False, disable_web_page_preview=True)


# ── Переключение области выборки (Только в названиях / Название + описание) ──

@router.callback_query(F.data.startswith("toggle_title_scope:"))
async def cb_toggle_title_scope(call: CallbackQuery):
    user_id = call.from_user.id
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
        return

    current_val = int(s.get("search_in_title_only") or 0)
    new_val = 0 if current_val == 1 else 1
    old_url = s["url"]

    parsed = urlparse(old_url)
    params = parse_qs(parsed.query)

    if new_val == 1:
        params["bt"] = ["1"]
    else:
        params.pop("bt", None)

    new_query = urlencode(sorted(params.items()), doseq=True)
    new_url = urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path, parsed.params, new_query, ""))
    new_url = AvitoParser.normalize_url(new_url)

    await database.update_search_title_scope(search_id, user_id, new_val, new_url)

    await call.answer(
        "Теперь ищет только в названиях: меньше случайных лотов." if new_val == 1
        else "Теперь ищет в названиях и описаниях: больше лотов."
    )
    s["search_in_title_only"] = new_val
    s["url"] = new_url
    card_text, markup = build_radar_card(s)
    await ui.show(call, card_text, markup, answer=False, disable_web_page_preview=True)


# ── Настройка свежести лотов (max_age_minutes) ──────────────────────────

@router.callback_query(F.data.startswith("set_age:"))
async def cb_set_age_menu(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
        return

    cur_age = s.get("max_age_minutes")
    if not cur_age or cur_age <= 0:
        cur_age = 5
    elif cur_age > 30:
        cur_age = 30

    text = (
        f"{ui.e('time')} <b>Свежесть лотов</b>\n\n"
        f"Радар: <b>{html.escape(s['title'])}</b>\n"
        f"Сейчас: <b>{format_max_age_label(cur_age)}</b>\n\n"
        "Лоты старше выбранного времени не придут (максимум 30 минут)."
    )
    options = [3, 5, 10, 15, 20, 30]
    rows, row = [], []
    for val in options:
        mark = " ✓" if cur_age == val else ""
        row.append(ui.btn(f"{val} мин{mark}", callback_data=f"save_age:{search_id}:{val}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([ui.btn("Ввести вручную (1–30)", "write", callback_data=f"custom_age:{search_id}")])
    rows.append([ui.btn("« К радару", callback_data=f"open_search:{search_id}")])
    await ui.show(call, text, ui.kb(*rows))


@router.callback_query(F.data.startswith("save_age:"))
async def cb_save_age(call: CallbackQuery):
    parts = call.data.split(":")
    search_id = int(parts[1])
    val = min(30, max(1, int(parts[2])))
    s = await _own_search(call, search_id)
    if not s:
        return

    await database.update_search_max_age(search_id, call.from_user.id, val)
    s["max_age_minutes"] = val

    ans_text = f"Установлен фильтр: {format_max_age_label(val)}"
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Настройка свежести", f"Радар #{search_id} -> {ans_text}", status="OK")
    await call.answer(ans_text)
    card_text, markup = build_radar_card(s)
    await ui.show(call, card_text, markup, answer=False, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("custom_age:"))
async def cb_prompt_custom_age(call: CallbackQuery, state: FSMContext):
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
        return

    await state.set_state(SearchStates.waiting_for_custom_age)
    await state.update_data(age_search_id=search_id)
    text = (
        f"{ui.e('write')} <b>Свежесть лотов</b>\n\n"
        f"Радар: <b>{html.escape(s['title'])}</b>\n"
        "Пришлите число минут от <b>1</b> до <b>30</b>."
    )
    await ui.show(call, text, ui.kb([ui.btn("« Отмена", callback_data=f"set_age:{search_id}")]))


@router.message(SearchStates.waiting_for_custom_age)
async def process_custom_age_input(message: Message, state: FSMContext):
    if await route_menu_text(message, state):
        return

    text = (message.text or "").strip()
    if not text.isdigit() or not 1 <= int(text) <= 30:
        await message.answer(
            f"{ui.e('warn')} Нужно целое число от 1 до 30, например <code>5</code>.",
            parse_mode="HTML"
        )
        return

    val = int(text)
    data = await state.get_data()
    search_id = data.get("age_search_id")
    await state.clear()

    if search_id:
        await database.update_search_max_age(search_id, message.from_user.id, val)
        log_user_event(message.from_user.id, message.from_user.username, message.from_user.first_name, "Настройка свежести", f"Радар #{search_id} -> свой порог: {val} мин.", status="OK")
        s = await database.get_search(search_id)
        if s:
            await _show_card(message, s)
            return

    await show_searches(message, state)


# ── Переименование и замена ссылки ─────────────────────────────────────

@router.callback_query(F.data.startswith("rename_search:"))
async def cb_rename_search(call: CallbackQuery, state: FSMContext):
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
        return
    await state.set_state(SearchStates.waiting_for_rename)
    await state.update_data(rename_search_id=search_id)
    await ui.show(call, (
        f"{ui.e('write')} <b>Новое название радара</b>\n\n"
        f"Сейчас: <b>{html.escape(s['title'])}</b>\nПришлите новое название, до 100 символов."
    ), ui.kb([ui.btn("Отмена", "cancel", callback_data=f"open_search:{search_id}")]))


@router.message(SearchStates.waiting_for_rename)
async def process_rename(message: Message, state: FSMContext):
    if await route_menu_text(message, state):
        return
    title = " ".join((message.text or "").split())
    if not title:
        await message.answer("Пришлите название текстом.")
        return
    search_id = int((await state.get_data()).get("rename_search_id") or 0)
    await state.clear()
    if not await database.rename_search(search_id, message.from_user.id, title):
        await message.answer("Радар не найден.", reply_markup=ui.kb([ui.btn("Мои радары", "radars", callback_data="list_searches")]))
        return
    log_user_event(message.from_user.id, message.from_user.username, message.from_user.first_name, "Переименование радара",
                   f"Радар #{search_id} -> «{title[:100]}»", status="OK")
    await _show_card(message, await database.get_search(search_id))


@router.callback_query(F.data.startswith("change_url:"))
async def cb_change_url(call: CallbackQuery, state: FSMContext):
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
        return
    await state.set_state(SearchStates.waiting_for_new_url)
    await state.update_data(url_search_id=search_id)
    await ui.show(call, (
        f"{ui.e('refresh')} <b>Новая ссылка для «{html.escape(s['title'])}»</b>\n\n"
        "Пришлите ссылку на поиск Авито. Название, стоп-слова и настройки останутся, "
        "а уже выложенные лоты новой выдачи не придут."
    ), ui.kb([ui.btn("Отмена", "cancel", callback_data=f"open_search:{search_id}")]))


# ── Массовое удаление (только админы) ──────────────────────────────────

BULK_PAGE = 20


async def _bulk_screen(call: CallbackQuery, state: FSMContext, page: int) -> None:
    searches = await database.get_user_searches(call.from_user.id)
    selected = set((await state.get_data()).get("bulk_sel") or [])
    selected &= {s["id"] for s in searches}
    await state.update_data(bulk_sel=sorted(selected))
    pages = max(1, -(-len(searches) // BULK_PAGE))
    page = max(0, min(page, pages - 1))
    rows = []
    for i, s in enumerate(searches[page * BULK_PAGE:(page + 1) * BULK_PAGE], page * BULK_PAGE + 1):
        mark = "☑" if s["id"] in selected else "☐"
        title = s["title"].strip()
        rows.append([ui.btn(f"{mark} {i}. {title[:30]}", callback_data=f"bulk_tog:{s['id']}:{page}")])
    nav = []
    if page > 0:
        nav.append(ui.btn("«", callback_data=f"bulk_del:{page - 1}"))
    if page < pages - 1:
        nav.append(ui.btn("»", callback_data=f"bulk_del:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([ui.btn("Выбрать все", callback_data=f"bulk_all:{page}"),
                 ui.btn("Снять все", callback_data=f"bulk_none:{page}")])
    if selected:
        rows.append([ui.btn(f"Удалить выбранные ({len(selected)})", "trash", callback_data="bulk_go", style="danger")])
    rows.append([ui.btn("« Мои радары", callback_data="list_searches")])
    text = (
        f"{ui.e('trash')} <b>Удалить несколько радаров</b>\n"
        f"Выбрано: <b>{len(selected)}</b> из {len(searches)}" + (f" · стр. {page + 1}/{pages}" if pages > 1 else "")
        + "\n\nНажимайте на радары, чтобы отметить их."
    )
    await ui.show(call, text, ui.kb(*rows))


@router.callback_query(F.data.startswith(("bulk_del:", "bulk_tog:", "bulk_all:", "bulk_none:")))
async def cb_bulk_select(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("Доступно только администраторам", show_alert=True)
        return
    action, *args = call.data.split(":")
    selected = set((await state.get_data()).get("bulk_sel") or [])
    if action == "bulk_del" and args[0] == "new":
        selected, args = set(), ["0"]
    elif action == "bulk_tog":
        selected ^= {int(args[0])}
        args = args[1:]
    elif action == "bulk_all":
        selected = {s["id"] for s in await database.get_user_searches(call.from_user.id)}
    elif action == "bulk_none":
        selected = set()
    await state.update_data(bulk_sel=sorted(selected))
    await _bulk_screen(call, state, int(args[0]) if args and args[0].isdigit() else 0)


@router.callback_query(F.data == "bulk_go")
async def cb_bulk_confirm(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    selected = (await state.get_data()).get("bulk_sel") or []
    if not selected:
        await call.answer("Ничего не выбрано", show_alert=True)
        return
    await ui.show(call, (
        f"{ui.e('trash')} <b>Удалить {len(selected)} радаров?</b>\n\nИх стоп-слова тоже удалятся. Отменить это нельзя."
    ), ui.kb([ui.btn("Да, удалить", "trash", callback_data="bulk_yes", style="danger"),
              ui.btn("Отмена", callback_data="bulk_del:0")]))


@router.callback_query(F.data == "bulk_yes")
async def cb_bulk_delete(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        return
    selected = (await state.get_data()).get("bulk_sel") or []
    await state.update_data(bulk_sel=[])
    deleted = 0
    for search_id in selected:
        deleted += int(await database.delete_search(int(search_id), call.from_user.id))
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Массовое удаление радаров",
                   f"Удалено {deleted} из {len(selected)}", status="OK")
    await call.answer(f"Удалено радаров: {deleted}")
    await show_searches(call)


@router.callback_query(F.data == "bulk_wipe")
async def cb_bulk_wipe_confirm(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("Доступно только администраторам", show_alert=True)
        return
    count = len(await database.get_user_searches(call.from_user.id))
    if not count:
        await call.answer("Радаров нет", show_alert=True)
        return
    await ui.show(call, (
        f"{ui.e('trash')} <b>Вы уверены? Удалить все ваши радары ({count})?</b>\n\n"
        "Их стоп-слова тоже удалятся. Отменить это нельзя."
    ), ui.kb([ui.btn("Да, удалить все", "trash", callback_data="bulk_wipe_yes", style="danger"),
              ui.btn("Отмена", callback_data="list_searches")]))


@router.callback_query(F.data == "bulk_wipe_yes")
async def cb_bulk_wipe(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        return
    searches = await database.get_user_searches(call.from_user.id)
    deleted = 0
    for s in searches:
        deleted += int(await database.delete_search(int(s["id"]), call.from_user.id))
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Удаление всех радаров",
                   f"Удалено {deleted} из {len(searches)}", status="OK")
    await call.answer(f"Удалено радаров: {deleted}")
    await show_searches(call)


# ── Удаление поиска ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("delete_search:"))
async def cb_delete_search(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
        return
    text = (
        f"{ui.e('trash')} <b>Удалить радар «{html.escape(s['title'])}»?</b>\n\n"
        "Радар остановится, его стоп-слова удалятся. Отменить это нельзя."
    )
    await ui.show(call, text, ui.kb([
        ui.btn("Да, удалить", "trash", callback_data=f"confirm_delete:{search_id}", style="danger"),
        ui.btn("Отмена", callback_data=f"cancel_delete:{search_id}"),
    ]))


@router.callback_query(F.data.startswith("confirm_delete:"))
async def cb_confirm_delete(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    await database.delete_search(search_id, call.from_user.id)
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Удаление радара", f"Удален радар #{search_id}", status="OK")
    logger.info(f"🗑 [РАДАР] Пользователь ID:{call.from_user.id} удалил радар #{search_id}")
    await call.answer("Радар удалён")
    await show_searches(call)


@router.callback_query(F.data.startswith("cancel_delete:"))
async def cb_cancel_delete(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s or s.get("user_id") != call.from_user.id:
        await show_searches(call)
        return
    await _show_card(call, s)


# ── Управление стоп-словами ────────────────────────────────────────────

@router.callback_query(F.data.startswith("manage_stops:"))
async def cb_manage_stops(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
        return

    stop_words = s["stop_words"]
    lines = [
        f"{ui.e('shield')} <b>Стоп-слова</b> · {html.escape(s['title'])}",
        "",
    ]
    if stop_words:
        lines.append("Лоты с этими словами в названии или описании не придут:")
        lines.append(", ".join(f"<code>{html.escape(w)}</code>" for w in stop_words))
        lines.append("")
        lines.append("<i>Нажмите на слово, чтобы удалить его.</i>")
    else:
        lines.append("Пока пусто. Например: «запчасти», «реплика», «дефект», «копия».")

    rows, row = [], []
    # The button carries the word's position, not the word: callback data is limited to 64 bytes.
    for idx, w in enumerate(stop_words):
        row.append(ui.btn(f"✖ {w}", callback_data=f"rm_stop_i:{search_id}:{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([ui.btn("Добавить стоп-слово", "add", callback_data=f"add_stop:{search_id}", style="primary")])
    rows.append([ui.btn("« К радару", callback_data=f"open_search:{search_id}"),
                 ui.btn("Все радары", "radars", callback_data="list_searches")])
    await ui.show(call, "\n".join(lines), ui.kb(*rows))


async def _start_stop_word_fsm(call: CallbackQuery, state: FSMContext, search_id: int) -> None:
    await state.set_state(SearchStates.waiting_for_stop_word)
    await state.update_data(search_id=search_id)
    if call.message:
        await call.message.answer(
            f"{ui.e('write')} <b>Пришлите стоп-слово или фразу</b>\n\n"
            "Несколько — через запятую: <code>на запчасти, копия, без комплекта</code>",
            reply_markup=ui.kb([ui.btn("Отмена", "cancel", callback_data="cancel_fsm")]),
            parse_mode="HTML"
        )
    await call.answer()


@router.callback_query(F.data.startswith("event_stop:"))
async def cb_event_stop_word(call: CallbackQuery, state: FSMContext):
    """
    Обработчик кнопки «Стоп-слово» под агрегированным уведомлением (по event_id):
    - 1 активный радар -> сразу открывает ввод стоп-слова
    - >1 активных радаров -> показывает выбор радара (до 5 кнопок в ряд и Отмена)
    - удалено / очищено -> отвечает 'Уведомление устарело, откройте радар в меню'
    """
    parts = call.data.split(":", 1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
        return
    event_id = int(parts[1].strip())
    event = await database.get_event_for_callback(event_id)
    if event is None or int(event.get("user_id") or 0) != int(call.from_user.id):
        await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
        return

    active_radars = event.get("active_radars") or []
    if not active_radars:
        await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
        return

    if len(active_radars) == 1:
        await _start_stop_word_fsm(call, state, int(active_radars[0]["search_id"]))
        return

    # Несколько активных радаров — показываем inline-выбор (до 5 кнопок в ряд + Отмена)
    lines = [f"{ui.e('shield')} <b>В какой радар добавить стоп-слово?</b>\n"]
    rows: list = []
    current_row: list = []
    for idx, r in enumerate(active_radars, start=1):
        sid = int(r["search_id"])
        rtitle = html.escape(str(r.get("radar_title") or f"Радар #{sid}"))
        lines.append(f"<b>{idx}.</b> {rtitle}")
        short_label = str(r.get("radar_title") or f"#{sid}").strip()
        if len(short_label) > 16:
            short_label = short_label[:15] + "…"
        current_row.append(ui.btn(f"{idx}. {short_label}", callback_data=f"add_stop:{sid}"))
        if len(current_row) == 5:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([ui.btn("Отмена", "cancel", callback_data="cancel_fsm")])

    if call.message:
        await call.message.answer("\n".join(lines), reply_markup=ui.kb(*rows), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("add_stop:"))
@router.callback_query(F.data.startswith("add_stop_from_item:"))
async def cb_add_stop_word_prompt(call: CallbackQuery, state: FSMContext):
    try:
        search_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
        return
    search = await database.get_search(search_id)
    if not search or search["user_id"] != call.from_user.id:
        await call.answer("Радар не найден", show_alert=True)
        return
    await _start_stop_word_fsm(call, state, search_id)


# ── Фильтры радара и сообщение продавцу ────────────────────────────────

FILTER_TOGGLES = ("delivery_only", "hide_promoted")


def _yes_no(value) -> str:
    return "да" if value else "нет"


def build_filters_screen(s: dict) -> Tuple[str, InlineKeyboardMarkup]:
    search_id = s["id"]
    price_min, price_max = int(s.get("price_min") or 0), int(s.get("price_max") or 0)
    words = radar_filters.parse_words(s.get("must_words"))
    template = (s.get("reply_template") or "").strip()
    text = (
        f"{ui.e('note')} <b>Фильтры радара</b>\n«{html.escape(s['title'])}»\n\n"
        f"<b>Цена:</b> {radar_filters.price_label(price_min, price_max)}\n"
        f"<b>Обязательные слова:</b> {html.escape(', '.join(words)) or 'нет'}\n"
        f"<b>Только с Авито Доставкой:</b> {_yes_no(s.get('delivery_only'))}\n"
        f"<b>Скрывать продвинутые:</b> {_yes_no(s.get('hide_promoted'))}\n"
        f"<b>Сообщение продавцу:</b> {html.escape(template) or 'нет'}\n\n"
        "<blockquote>Лот придёт, если проходит все условия. Лот без цены проходит фильтр цены. "
        "Лот, отсеянный сейчас, не придёт и после смены фильтра.</blockquote>"
    )
    markup = ui.kb(
        [ui.btn("Цена", "price", callback_data=f"flt_price:{search_id}"),
         ui.btn("Обязательные слова", "scope", callback_data=f"flt_must:{search_id}")],
        [ui.btn(f"Только с доставкой: {_yes_no(s.get('delivery_only'))}", "box",
                callback_data=f"flt_tog:delivery_only:{search_id}")],
        [ui.btn(f"Скрывать продвинутые: {_yes_no(s.get('hide_promoted'))}", "ban",
                callback_data=f"flt_tog:hide_promoted:{search_id}")],
        [ui.btn("Сообщение продавцу", "write", callback_data=f"flt_tpl:{search_id}")],
        [ui.btn("« К радару", callback_data=f"open_search:{search_id}")],
    )
    return text, markup


async def _ask_filter_value(call: CallbackQuery, state: FSMContext, step: State, text: str) -> None:
    search_id = int(call.data.split(":")[1])
    if not await _own_search(call, search_id):
        return
    await state.set_state(step)
    await state.update_data(search_id=search_id)
    await ui.show(call, text, ui.kb([ui.btn("Отмена", "cancel", callback_data=f"flt_back:{search_id}")]))


async def _save_filter_value(message: Message, state: FSMContext, **fields) -> None:
    data = await state.get_data()
    search_id = int(data.get("search_id") or 0)
    await state.clear()
    saved = await database.update_search_filters(search_id, message.from_user.id, **fields)
    s = await database.get_search(search_id) if saved else None
    if not s:
        await message.answer(f"{ui.e('warn')} Радар не найден.", parse_mode="HTML")
        return
    log_user_event(message.from_user.id, message.from_user.username, message.from_user.first_name,
                   "Фильтры радара", f"Радар #{search_id}: {sorted(fields)}", status="OK")
    text, markup = build_filters_screen(s)
    await ui.show(message, text, markup)


@router.callback_query(F.data.startswith(("flt_open:", "flt_back:")))
async def cb_filters_open(call: CallbackQuery, state: FSMContext):
    await state.clear()
    s = await _own_search(call, int(call.data.split(":")[1]))
    if not s:
        return
    text, markup = build_filters_screen(s)
    await ui.show(call, text, markup)


@router.callback_query(F.data.startswith("flt_tog:"))
async def cb_filter_toggle(call: CallbackQuery):
    _, field, raw_id = call.data.split(":")
    if field not in FILTER_TOGGLES:
        await call.answer()
        return
    s = await _own_search(call, int(raw_id))
    if not s:
        return
    s[field] = 0 if s.get(field) else 1
    await database.update_search_filters(s["id"], call.from_user.id, **{field: s[field]})
    text, markup = build_filters_screen(s)
    await ui.show(call, text, markup)


@router.callback_query(F.data.startswith("flt_price:"))
async def cb_filter_price(call: CallbackQuery, state: FSMContext):
    await _ask_filter_value(
        call, state, SearchStates.waiting_for_price,
        f"{ui.e('price')} <b>Диапазон цены</b>\n\n"
        "Пришлите, например: <code>10000-50000</code>, <code>от 10к</code> или <code>до 1.5 млн</code>.\n"
        "Убрать фильтр: <code>нет</code>.",
    )


@router.message(SearchStates.waiting_for_price, F.text)
async def process_filter_price(message: Message, state: FSMContext):
    if await route_menu_text(message, state):
        return
    parsed = radar_filters.parse_price_range(message.text)
    if parsed is None:
        await message.answer(
            f"{ui.e('warn')} Не понял диапазон. Пример: <code>10000-50000</code>, "
            "<code>от 10к</code>, <code>до 50 тыс</code> или <code>нет</code>.",
            parse_mode="HTML",
        )
        return
    await _save_filter_value(message, state, price_min=parsed[0], price_max=parsed[1])


@router.callback_query(F.data.startswith("flt_must:"))
async def cb_filter_must_words(call: CallbackQuery, state: FSMContext):
    await _ask_filter_value(
        call, state, SearchStates.waiting_for_must_words,
        f"{ui.e('scope')} <b>Обязательные слова</b>\n\n"
        "Лот придёт, если в названии или описании есть хотя бы одно слово.\n"
        "Пришлите слова через запятую: <code>256, pro max</code>. Список заменится целиком.\n"
        "Убрать фильтр: <code>нет</code>.",
    )


@router.message(SearchStates.waiting_for_must_words, F.text)
async def process_filter_must_words(message: Message, state: FSMContext):
    if await route_menu_text(message, state):
        return
    text = message.text.strip().lower()
    words = [] if text in radar_filters.CLEAR_WORDS else radar_filters.split_words(text)
    await _save_filter_value(message, state, must_words=json.dumps(words, ensure_ascii=False))


@router.callback_query(F.data.startswith("flt_tpl:"))
async def cb_filter_reply_template(call: CallbackQuery, state: FSMContext):
    await _ask_filter_value(
        call, state, SearchStates.waiting_for_reply_template,
        f"{ui.e('write')} <b>Сообщение продавцу</b>\n\n"
        "Под каждым лотом появится кнопка: один тап копирует текст, останется вставить его в чат на Авито.\n"
        "В тексте можно использовать <code>{название}</code> и <code>{цена}</code>.\n"
        "Пример: <code>Здравствуйте! {название} ещё продаётся? Заберу сегодня.</code>\n"
        "Убрать: <code>нет</code>.",
    )


@router.message(SearchStates.waiting_for_reply_template, F.text)
async def process_filter_reply_template(message: Message, state: FSMContext):
    if await route_menu_text(message, state):
        return
    template = message.text.strip()
    if template.lower() in radar_filters.CLEAR_WORDS:
        template = ""
    if len(template) > radar_filters.REPLY_TEMPLATE_MAX_LEN:
        await message.answer(
            f"{ui.e('warn')} Слишком длинно: максимум {radar_filters.REPLY_TEMPLATE_MAX_LEN} символов.",
            parse_mode="HTML",
        )
        return
    await _save_filter_value(message, state, reply_template=template)


@router.message(SearchStates.waiting_for_stop_word, F.text)
async def process_stop_word_input(message: Message, state: FSMContext):
    if await route_menu_text(message, state):
        return

    data = await state.get_data()
    search_id = data.get("search_id")
    words = [w.strip() for w in message.text.split(",") if w.strip()]
    added = []
    for w in words:
        if await database.add_stop_word_to_search(search_id, w, message.from_user.id):
            added.append(w)

    await state.clear()
    log_user_event(message.from_user.id, message.from_user.username, message.from_user.first_name, "Добавление стоп-слов", f"Добавлены слова: {added} к радару #{search_id}", status="OK")
    logger.info(f"🛡 [СТОП-СЛОВА] Пользователь ID:{message.from_user.id} добавил к радару #{search_id}: {added}")

    if added:
        text = (
            f"{ui.e('ok')} <b>Добавлено:</b> {', '.join(f'«{html.escape(w)}»' for w in added)}\n"
            "Лоты с этими словами больше не придут."
        )
    else:
        text = f"{ui.e('warn')} Ничего не добавлено: эти слова уже есть в списке."
    await message.answer(
        text,
        reply_markup=ui.kb(
            [ui.btn("Стоп-слова радара", "shield", callback_data=f"manage_stops:{search_id}")],
            [ui.btn("Мои радары", "radars", callback_data="list_searches")],
        ),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("rm_stop_i:"))
@router.callback_query(F.data.startswith("rm_stop:"))
async def cb_remove_stop_word(call: CallbackQuery):
    by_index = call.data.startswith("rm_stop_i:")
    parts = call.data.split(":", 2)
    search_id = int(parts[1])
    word = parts[2]
    if by_index:
        s = await _own_search(call, search_id)
        if not s:
            return
        idx = int(word) if word.isdigit() else -1
        if not 0 <= idx < len(s["stop_words"]):
            await call.answer("Список изменился, откройте его заново", show_alert=True)
            return
        word = s["stop_words"][idx]

    if not await database.remove_stop_word_from_search(search_id, word, call.from_user.id):
        await call.answer("Радар или стоп-слово не найдено", show_alert=True)
        return
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Удаление стоп-слова", f"Удалено «{word}» из радара #{search_id}", status="OK")
    logger.info(f"🛡 [СТОП-СЛОВА] Пользователь ID:{call.from_user.id} удалил «{word}» из радара #{search_id}")
    await call.answer(f"«{word}» удалено")
    await cb_manage_stops(call)
