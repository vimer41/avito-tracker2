"""Premium (animated) custom emoji for the bot UI.

Every icon in the interface is a named slot with a plain emoji fallback. Out of the box the slots use
Telegram's official animated pack (DEFAULT_IDS). An admin can override them from the bot (🎨 in the
admin panel): send any emoji of another pack and every slot whose plain emoji exists in it switches.
Tests never call load(), so they see plain emoji.

Bot API 9.4+: a bot may use custom emoji in messages and on buttons when its owner has Telegram
Premium. If Telegram ever rejects them, `PremiumEmojiFallback` resends the request with plain emoji
and switches premium emoji off until an admin turns them back on.
"""

import html
import json
import logging
import re
from typing import Iterable, Optional

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import config

logger = logging.getLogger(__name__)

SETTING_IDS = "ui_emoji"
SETTING_ENABLED = "ui_emoji_enabled"

# name -> (plain emoji, what it marks). The order is the order of the admin screen.
SLOTS: dict[str, tuple[str, str]] = {
    "logo": ("🛰", "логотип, главный экран"),
    "hello": ("👋", "приветствие"),
    "radar": ("🎯", "радар, добавить радар"),
    "radars": ("📁", "мои радары"),
    "add": ("➕", "добавить"),
    "tariffs": ("💎", "тарифы"),
    "profile": ("👤", "профиль, продавец"),
    "help": ("📖", "инструкция"),
    "docs": ("📚", "документы"),
    "support": ("💬", "поддержка"),
    "home": ("🏠", "главное меню"),
    "trial": ("🎁", "пробный период"),
    "lite": ("⚡", "тариф Базовый"),
    "standart": ("🚀", "тариф Средний"),
    "pro": ("🔥", "тариф Полный"),
    "on": ("💚", "радар работает"),
    "pause": ("💤", "пауза"),
    "play": ("🔔", "запустить"),
    "shield": ("✂️", "стоп-слова"),
    "ban": ("🙈", "чёрный список"),
    "link": ("🔗", "открыть на Авито"),
    "trash": ("✖️", "удалить"),
    "refresh": ("🔄", "обновить"),
    "ok": ("✅", "готово"),
    "warn": ("❗️", "предупреждение"),
    "cancel": ("❌", "отмена"),
    "wait": ("⏳", "ожидание"),
    "new": ("⚡", "новый лот"),
    "price": ("💰", "цена"),
    "place": ("🗺", "адрес"),
    "time": ("⏰", "время"),
    "box": ("🛍", "детали лота"),
    "views": ("👁", "просмотры"),
    "note": ("📝", "описание"),
    "card": ("💸", "оплата СБП"),
    "bank": ("🏦", "оплата банковской картой"),
    "star": ("⭐", "Telegram Stars"),
    "coin": ("🪙", "криптовалюта"),
    "lock": ("🔐", "конфиденциальность, резерв"),
    "scope": ("🔍", "область поиска, поиск"),
    "write": ("✍️", "написать"),
    "mail": ("📨", "почта"),
    "admin": ("👑", "админ-панель"),
    "users": ("👥", "пользователи"),
    "net": ("🌍", "сеть и прокси"),
    "stats": ("📊", "мониторинг"),
    "db": ("🗂", "база"),
    "logs": ("📰", "логи"),
    "palette": ("🎨", "премиум-эмодзи"),
}

# Reply-keyboard labels and their legacy spellings. A reply button sends its label back as text,
# so these keep plain emoji: handlers match on the exact string.
MENU: dict[str, tuple[str, set[str]]] = {
    "add": ("🎯 Добавить радар", {"➕ Добавить радар", "➕ Добавить поиск"}),
    "radars": ("📁 Мои радары", {"📂 Мои радары", "📋 Мои поиски", "Мои радары"}),
    "tariffs": ("💳 Тарифы", {"💳 Тарифы и подписка", "Тарифы", "Подписка", "💳 Подписка", "💎 Тарифы"}),
    "profile": ("👤 Мой профиль", {"📊 Мой статус", "Мой профиль"}),
    "help": ("📖 Инструкция", {"❓ Помощь", "Инструкция"}),
    "docs": ("📄 Документы", {"Документы"}),
    "support": ("💬 Поддержка", {"Поддержка", "Контакты", "Помощь", "SOS"}),
    "admin": ("👑 Админ-панель", {"👑 Админка"}),
}
CANCEL_TEXTS = {"❌ Отмена", "Отмена"}
# Bottom menu labels of the clean theme; menu_texts() accepts them under any theme, so old keyboards keep working.
MENU_CLEAN: dict[str, str] = {
    "add": "Добавить радар", "radars": "Мои радары", "tariffs": "Тарифы", "profile": "Профиль",
    "help": "Инструкция", "docs": "Документы", "support": "Поддержка", "admin": "Админка",
}


def clean() -> bool:
    """The clean theme: monochrome icons instead of colour emoji, the button colour carries the meaning."""
    return config.UI_THEME == "clean"


def menu_label(key: str) -> str:
    return MENU_CLEAN[key] if clean() else MENU[key][0]


def menu_texts(*keys: str) -> set[str]:
    out: set[str] = set()
    for key in keys:
        label, aliases = MENU[key]
        out |= {label, *aliases, MENU_CLEAN[key]}
    return out


ALL_MENU_TEXTS = menu_texts(*MENU) | CANCEL_TEXTS

# Telegram's official animated pack «Animated Emoji» (t.me/addemoji/RestrictedEmoji): every slot's
# plain emoji is picked so that this pack has it. Rebuild with getStickerSet + match_slots.
DEFAULT_PACK = "RestrictedEmoji"
DEFAULT_IDS: dict[str, str] = {
    "logo": "5321304062715517873", "hello": "5472055112702629499", "radar": "5350460637182993292",
    "radars": "5433653135799228968", "add": "5226945370684140473", "tariffs": "5471952986970267163",
    "profile": "5373012449597335010", "help": "5226512880362332956", "docs": "5373098009640836781",
    "support": "5465300082628763143", "home": "5465226866321268133", "trial": "5199749070830197566",
    "lite": "5431449001532594346", "standart": "5445284980978621387", "pro": "5420315771991497307",
    "on": "5449380056201697322", "pause": "5451959871257713464", "play": "5242628160297641831",
    "shield": "5237808360882977239", "ban": "5467370583282950466", "link": "5375129357373165375",
    "trash": "5226660202035554522", "refresh": "5264727218734524899", "ok": "5427009714745517609",
    "warn": "5467928559664242360", "cancel": "5465665476971471368", "wait": "5451732530048802485",
    "new": "5431449001532594346", "price": "5375296873982604963", "place": "5415803062738504079",
    "time": "5413704112220949842", "box": "5373052667671093676", "views": "5424892643760937442",
    "note": "5334882760735598374", "card": "5472030678633684592", "bank": "5264895611517300926", "star": "5435957248314579621",
    "coin": "5379600444098093058", "lock": "5472308992514464048", "scope": "5188217332748527444",
    "write": "5470060791883374114", "mail": "5406631276042002796", "admin": "5467406098367521267",
    "users": "5372926953978341366", "net": "5399898266265475100", "stats": "5431577498364158238",
    "db": "5431736674147114227", "logs": "5433982607035474385", "palette": "5431456208487716895",
}

# The clean theme's own monochrome pack (tools/make_icon_pack.py, Tabler outline icons): adaptive custom emoji,
# painted in the text colour by Telegram, so white on a dark button.
CLEAN_PACK = "avito_radar_icons_by_DIMADIMASIKSSSbot"
CLEAN_IDS: dict[str, str] = {
    "logo": "5440580544385563268", "hello": "5443129933893249057", "radar": "5443116155638165457",
    "radars": "5442712540381490678", "add": "5442722659324440092", "tariffs": "5440825722593651193",
    "profile": "5443115219335293391", "help": "5440683473276807521", "docs": "5442960544678065378",
    "support": "5440838740639526523", "home": "5442806531445796460", "trial": "5440878507741720245",
    "lite": "5442899199660174973", "standart": "5443067695522167168", "pro": "5443147062222829092",
    "on": "5443045275792882768", "pause": "5442862280121300179", "play": "5442617681733792807",
    "shield": "5443023255495551803", "ban": "5440732341414701771", "link": "5443014197409525630",
    "trash": "5442883776432615325", "refresh": "5442605140429288767", "ok": "5442830398579061649",
    "warn": "5442803378939801834", "cancel": "5442748523617498340", "wait": "5442811346104133972",
    "new": "5442899199660174973", "price": "5440533037752311161", "place": "5443048827730833363",
    "time": "5443066514406159431", "box": "5440419465932088986", "views": "5443082921181229039",
    "note": "5443056008916152185", "card": "5443133000499900201", "bank": "5443088839646164096",
    "star": "5443080958381174053", "coin": "5440433841187630855", "lock": "5442727542702263331",
    "scope": "5440456415535739749", "write": "5442683862884857149", "mail": "5443146177459564482",
    "admin": "5440497368048903622", "users": "5442698680522026406", "net": "5442814700473593080",
    "stats": "5440819954452576036", "db": "5443161798255620490", "logs": "5440897667590827311",
    "palette": "5443082023533065616",
}


ENABLED = True
IDS: dict[str, str] = {}          # effective: defaults + admin overrides, minus slots set to plain
OVERRIDES: dict[str, str] = {}    # admin choices; "0" = plain emoji for that slot
_defaults: dict[str, str] = {}    # DEFAULT_IDS once load() ran; empty in tests, so they see plain emoji
_CHAR_BY_ID: dict[str, str] = {}
_TAG_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def _norm(char: str) -> str:
    return (char or "").replace("️", "").strip()


def _apply(overrides: dict, enabled: bool, defaults: Optional[dict] = None) -> None:
    global ENABLED, IDS, OVERRIDES, _defaults, _CHAR_BY_ID
    if defaults is not None:
        _defaults = dict(defaults)
    ENABLED = bool(enabled)
    OVERRIDES = {k: str(v) for k, v in (overrides or {}).items() if k in SLOTS and str(v).isdigit()}
    IDS = {k: v for k, v in {**_defaults, **OVERRIDES}.items() if v != "0"}
    _CHAR_BY_ID = {v: SLOTS[k][0] for k, v in IDS.items()}


def plain(name: str) -> str:
    return SLOTS[name][0]


def custom_id(name: str) -> Optional[str]:
    if clean():
        return CLEAN_IDS.get(name) if ENABLED else None
    return IDS.get(name) if ENABLED else None


def e(name: str) -> str:
    """Emoji for HTML text: animated when the slot is filled, plain otherwise.
    The clean theme shows only its own monochrome icon, never a colour emoji."""
    char = SLOTS[name][0]
    cid = custom_id(name)
    if clean() and not cid:
        return ""
    return f'<tg-emoji emoji-id="{cid}">{char}</tg-emoji>' if cid else char


def btn(text: str, icon: Optional[str] = None, *, style: Optional[str] = None, **kwargs) -> InlineKeyboardButton:
    """Inline button with an icon slot: animated icon when filled, plain emoji in the text otherwise.
    The clean theme uses its own monochrome icon and never puts a colour emoji into the text."""
    if clean():
        return InlineKeyboardButton(text=text, icon_custom_emoji_id=custom_id(icon) if icon else None,
                                    style=style, **kwargs)
    cid = custom_id(icon) if icon else None
    if icon and not cid:
        text = f"{SLOTS[icon][0]} {text}"
    return InlineKeyboardButton(text=text, icon_custom_emoji_id=cid, style=style, **kwargs)


def kb(*rows: Iterable[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def home_btn() -> InlineKeyboardButton:
    return btn("Меню" if clean() else "Главное меню", "home", callback_data="start_approved")


async def show(event, text: str, markup=None, *, answer: bool = True, **kwargs) -> None:
    """Edit the screen in place for a button press, send a new message for a typed command."""
    if isinstance(event, CallbackQuery):
        if answer:
            try:
                await event.answer()
            except Exception:
                pass
        if event.message is not None:
            try:
                await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML", **kwargs)
                return
            except Exception as exc:
                if "not modified" in str(exc).lower():
                    return
        message = event.message
    else:
        message = event
    if message is not None:
        await message.answer(text, reply_markup=markup, parse_mode="HTML", **kwargs)


def visible_len(text: str) -> int:
    """Length Telegram counts for a caption: without HTML tags and entities."""
    return len(html.unescape(_ANY_TAG_RE.sub("", text or "")))


def strip_html(text: str) -> str:
    return _TAG_RE.sub(r"\1", text or "")


# ── Persistence ────────────────────────────────────────────────────────

async def load() -> None:
    import database
    try:
        ids = json.loads(await database.get_bot_setting(SETTING_IDS, "{}") or "{}")
    except (TypeError, ValueError):
        ids = {}
    enabled = (await database.get_bot_setting(SETTING_ENABLED, "1")) != "0"
    _apply(ids if isinstance(ids, dict) else {}, enabled, DEFAULT_IDS)
    logger.info("Премиум-эмодзи: заполнено %s из %s слотов, своих замен %s, включены=%s",
                len(IDS), len(SLOTS), len(OVERRIDES), ENABLED)


async def save(overrides: Optional[dict] = None, enabled: Optional[bool] = None) -> None:
    import database
    if overrides is not None:
        await database.set_bot_setting(SETTING_IDS, json.dumps(overrides))
    if enabled is not None:
        await database.set_bot_setting(SETTING_ENABLED, "1" if enabled else "0")
    _apply(OVERRIDES if overrides is None else overrides, ENABLED if enabled is None else enabled)


def match_slots(stickers: Iterable[tuple[str, str]], only_empty: bool = False) -> dict[str, str]:
    """(plain emoji, custom_emoji_id) pairs -> {slot: id} for slots whose plain emoji matches."""
    by_char: dict[str, str] = {}
    for char, cid in stickers:
        by_char.setdefault(_norm(char), str(cid))
    found = {}
    for name, (char, _desc) in SLOTS.items():
        if only_empty and name in IDS:
            continue
        cid = by_char.get(_norm(char))
        if cid:
            found[name] = cid
    return found


def custom_emoji_in(message) -> list[tuple[str, str]]:
    """(plain emoji, custom_emoji_id) for every custom emoji in a message."""
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    utf16 = text.encode("utf-16-le")
    out = []
    for ent in entities:
        if ent.type == "custom_emoji" and ent.custom_emoji_id:
            char = utf16[ent.offset * 2:(ent.offset + ent.length) * 2].decode("utf-16-le", "ignore")
            out.append((char, ent.custom_emoji_id))
    return out


# ── Fallback when Telegram rejects custom emoji ────────────────────────

def _strip_markup(markup):
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup, False
    changed = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            if b.icon_custom_emoji_id:
                char = "" if clean() else _CHAR_BY_ID.get(b.icon_custom_emoji_id, "")
                b = b.model_copy(update={"icon_custom_emoji_id": None, "text": f"{char} {b.text}".strip()})
                changed = True
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows), changed


def strip_method(method):
    """A copy of a Bot API call without custom emoji, or None when it has none."""
    update = {}
    for field in ("text", "caption"):
        value = getattr(method, field, None)
        if isinstance(value, str) and "<tg-emoji" in value:
            update[field] = strip_html(value)
    markup, changed = _strip_markup(getattr(method, "reply_markup", None))
    if changed:
        update["reply_markup"] = markup
    return method.model_copy(update=update) if update else None


_LINE_LEAD = re.compile(r"(?m)^[ \t]+")
# Status marks that keep their colour: in admin lists 🟢🟡🔴 tell a working channel from a blocked one, ❌ marks an
# error, and ✓ ✗ ☑ ☐ ● ○ are text symbols of checklists.
_KEEP = "🟢🟡🔴🟠⚪⚫🔵🟣❌✓✗☑☐●○"
_EMOJI = re.compile(
    "(?:[\U0001F000-\U0001FAFF⌀-⏿Ⓜ■-➿⬀-⯿〰〽㊗㊙]"
    "(?:️|⃣|[\U0001F3FB-\U0001F3FF])*(?:‍\S(?:️)?)*)"
)
_TG_EMOJI = re.compile(r"(<tg-emoji[^>]*>.*?</tg-emoji>)", re.S)
# «<b>Статус:</b>» at a line start, after an optional icon tag.
_BOLD_LABEL = re.compile(r"(?m)^((?:<tg-emoji[^>]*>.*?</tg-emoji>\s*)?)<b>([^<\n]{1,40}):</b>")
# Emoji written straight into older screens that have an icon of their own in the clean pack.
_EXTRA_SLOTS = {
    "⚠": "warn", "🚨": "warn", "💥": "warn", "✅": "ok", "🎉": "ok", "🗑": "trash", "📦": "box", "⏸": "pause",
    "⏹": "pause", "🛑": "ban", "⛔": "ban", "🚫": "ban", "🚷": "ban", "📅": "time", "🗓": "time", "🕒": "time",
    "🕰": "time", "⌛": "wait", "🌐": "net", "🔌": "net", "📡": "radar", "📄": "docs", "📜": "docs",
    "🧾": "note", "💳": "tariffs", "▶": "play", "⏯": "play", "📣": "mail", "📬": "mail", "📩": "mail",
    "🆔": "profile", "🏷": "price", "🧪": "stats", "📈": "stats", "🛡": "lock", "🔑": "lock", "💾": "db",
    "🗜": "db", "♻": "refresh", "🔁": "refresh", "🔎": "scope", "📂": "radars", "📋": "radars",
    # network modes: every mode button gets an icon, not just «Прокси» (🛰 = logo)
    "🧠": "radar", "🔀": "refresh", "🖥": "db", "🤖": "net",
}
_HOME_LABELS = {"Меню", "Главное меню", "« Меню", "« Главное меню", "В меню"}


def _bare(char: str) -> str:
    return char.replace("️", "").replace("⃣", "")


_SLOT_BY_CHAR: dict[str, str] = {}
for _slot, (_char, _desc) in SLOTS.items():
    _SLOT_BY_CHAR.setdefault(_bare(_char), _slot)
for _char, _slot in _EXTRA_SLOTS.items():
    _SLOT_BY_CHAR.setdefault(_bare(_char), _slot)


def slot_for(char: str) -> Optional[str]:
    return _SLOT_BY_CHAR.get(_bare(char))


def drop_colour_emoji(text: str, *, icons: bool = False) -> str:
    """The text without colour emoji. With icons, an emoji that has a clean-pack icon becomes that icon (HTML only);
    status marks stay, custom-emoji tags are left as they are."""
    def swap(match) -> str:
        char = match.group(0)
        if char[0] in _KEEP:
            return char
        slot = slot_for(char)
        cid = custom_id(slot) if icons and slot else None
        return f'<tg-emoji emoji-id="{cid}">{char}</tg-emoji>' if cid else ""

    parts = _TG_EMOJI.split(text)
    out = "".join(part if i % 2 else _EMOJI.sub(swap, part) for i, part in enumerate(parts))
    out = re.sub(r"«<tg-emoji[^>]*>[^<]*</tg-emoji>\s*", "«", out)  # «Проверить каналы», not «[icon] Проверить…»
    out = re.sub(r"<(b|i)>[ \t]+", r"<\1>", out)  # «<b> Авто</b>» left by a removed emoji
    return re.sub(r"[ \t]{2,}", " ", _LINE_LEAD.sub("", out))


def _clean_button(b):
    """A button of an older screen: its leading emoji becomes the icon, ✅ (the chosen option) becomes ✓."""
    text = b.text.replace("✅", "✓").replace("✔️", "✓").replace("✔", "✓")
    icon = b.icon_custom_emoji_id
    if not icon:
        for match in _EMOJI.finditer(text):
            slot = slot_for(match.group(0))
            if slot and custom_id(slot):
                icon = custom_id(slot)
                break
    label = drop_colour_emoji(text).strip()
    if label in _HOME_LABELS:
        label, icon = "Меню", icon or custom_id("home")
    if not label:  # an icon-only button («🗑»): a word instead of the bare picture
        slot = next((slot_for(m.group(0)) for m in _EMOJI.finditer(text) if slot_for(m.group(0))), None)
        label = SLOTS[slot][1].split(",")[0].capitalize() if slot else b.text
    if label == b.text and icon == b.icon_custom_emoji_id:
        return b, False
    return b.model_copy(update={"text": label, "icon_custom_emoji_id": icon}), True


def _clean_markup(markup):
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup, False
    changed, rows = False, []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            b, did = _clean_button(b)
            changed |= did
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows), changed


def clean_method(method):
    """Clean theme for every outgoing call: emoji in texts and buttons become clean-pack icons or go, field labels
    lose their bold, no space is left where an empty slot was. Popups (no parse mode) get plain text only;
    messages with <pre> blocks (logs, stack traces) keep their text as is."""
    update = {}
    html_ok = getattr(method, "parse_mode", None) is not None
    for field in ("text", "caption"):
        value = getattr(method, field, None)
        if isinstance(value, str) and "<pre" not in value:
            fixed = drop_colour_emoji(value, icons=html_ok)
            if html_ok:
                # Field labels read as plain text («Статус: Работает»), the bold is kept for titles and values.
                fixed = _BOLD_LABEL.sub(r"\1\2:", fixed)
            if fixed != value:
                update[field] = fixed
    markup, changed = _clean_markup(getattr(method, "reply_markup", None))
    if changed:
        update["reply_markup"] = markup
    return method.model_copy(update=update) if update else method


class PremiumEmojiFallback(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        if clean():
            method = clean_method(method)
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                raise
            plain_method = strip_method(method)
            if plain_method is None:
                raise
            result = await make_request(bot, plain_method)
            global ENABLED
            if ENABLED:
                ENABLED = False
                logger.error("Telegram отклонил премиум-эмодзи (%s): выключаю их до ручного включения в админке", exc)
            return result
