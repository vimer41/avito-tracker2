"""Links that can never be an Avito search: told apart by the address alone, without a request.

Only certain cases are refused here (the home page, Avito's own service pages). Everything else is
judged by the live check when a radar is added and by the dead-link alert while it runs.
"""
import asyncio
import html
import logging
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

import config
import ui

logger = logging.getLogger(__name__)

HOME = "home"          # avito.ru/ without a section: a recommendations feed, no search results
SERVICE = "service"    # profile, favourites, messenger…: a page of the account, not a search
NO_SEARCH = "no_search"  # /all, /moskva with no section and no query: every listing of a region in a row

# First path segment of Avito's own pages. No city or category on Avito has these slugs.
SERVICE_SECTIONS = frozenset({
    "profile", "favorites", "account", "messenger", "orders", "compare",
    "mobile_app", "business", "help", "safety", "about", "legal", "login",
})

PROBLEM_TEXT = {
    HOME: ("Это главная страница Авито, а не поиск",
           "На главной нет выдачи, по ней радар ничего не пришлёт. Откройте на avito.ru нужный раздел "
           "или найдите товар через поиск, затем скопируйте адрес из браузера."),
    SERVICE: ("Это служебная страница Авито, а не поиск",
              "Профиль, избранное, сообщения и подобные страницы не содержат выдачи. Откройте на avito.ru "
              "нужный раздел или найдите товар через поиск, затем скопируйте адрес из браузера."),
    NO_SEARCH: ("В ссылке нет ни раздела, ни запроса",
                "По ней идут все объявления региона подряд, радар успеет показать лишь малую часть. Выберите на "
                "avito.ru раздел или введите запрос в поиске, затем скопируйте адрес из браузера."),
}


def link_problem(url: str) -> Optional[str]:
    """HOME / SERVICE when the address can never be a search, NO_SEARCH for a bare region, else None."""
    raw = (url or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    parts = [p for p in unquote(parsed.path).split("/") if p]
    if not parts:
        return HOME
    if parts[0].lower() in SERVICE_SECTIONS:
        return SERVICE
    if len(parts) == 1:
        # Only a region (/all, /moskva): a search needs a section in the path, a text or a category in the query.
        query = parse_qs(parsed.query)
        if not query.get("q") and not any("categor" in key.lower() for key in query):
            return NO_SEARCH
    return None


def is_home_with_query(url: str) -> bool:
    """The home page with a search text: Avito may still serve it, so running radars keep it."""
    try:
        return link_problem(url) == HOME and bool(parse_qs(urlparse(url).query).get("q"))
    except ValueError:
        return False


def keeps_running(url: str) -> bool:
    """Running radars are paused only when the link surely brings nothing. A bare region does bring lots, so it is
    refused only for new radars and link changes."""
    return link_problem(url) in (None, NO_SEARCH) or is_home_with_query(url)


def problem_message(problem: str) -> str:
    title, hint = PROBLEM_TEXT[problem]
    return f"{ui.e('warn')} <b>{title}</b>\n\n{hint}"


def card_warning(url: str) -> str:
    problem = link_problem(url)
    if problem in (None, NO_SEARCH) or is_home_with_query(url):
        return ""
    return f"{ui.e('warn')} <b>{PROBLEM_TEXT[problem][0]}.</b> Радар ничего не пришлёт: смените ссылку.\n\n"


async def warn_bad_links(bot, *, pause_between: float = 0.1) -> int:
    """Startup pass: running radars with a link that can never be a search pause, and each owner is told once
    (a radar paused for this reason cannot be resumed until its link changes, so the pass never repeats)."""
    import database
    try:
        radars = await database.pause_bad_link_searches(keeps_running)
    except Exception:
        logger.exception("Проверка ссылок радаров при запуске не удалась")
        return 0
    for radar in radars:
        problem = link_problem(radar["url"]) or HOME
        text = (
            f"{ui.e('warn')} <b>Радар поставлен на паузу</b>\n\n"
            f"«{html.escape(str(radar.get('title') or 'Радар'))}»: {PROBLEM_TEXT[problem][0].lower()}. "
            "По такой ссылке радар ничего не пришлёт.\n\n"
            "<blockquote>Откройте на avito.ru нужный раздел или найдите товар через поиск, "
            "скопируйте адрес и пришлите его кнопкой ниже. Радар запустится сам.</blockquote>"
        )
        markup = ui.kb([
            ui.btn("Сменить ссылку", "refresh", callback_data=f"change_url:{radar['id']}", style="primary"),
            ui.btn("Удалить", "trash", callback_data=f"delete_search:{radar['id']}", style="danger"),
        ])
        try:
            await bot.send_message(chat_id=int(radar["user_id"]), text=text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            logger.warning("Не удалось предупредить владельца радара #%s о плохой ссылке", radar["id"], exc_info=True)
        await asyncio.sleep(pause_between)
    if radars:
        lines = "\n".join(f"#{r['id']} «{r.get('title') or 'Радар'}», пользователь {r['user_id']}" for r in radars[:20])
        more = f"\n…и ещё {len(radars) - 20}" if len(radars) > 20 else ""
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"⚠️ Радаров с ссылкой не на поиск: {len(radars)}. Поставлены на паузу, владельцы предупреждены.\n\n{lines}{more}",
                    parse_mode=None,
                )
            except Exception:
                logger.warning("Не удалось отправить админу сводку о плохих ссылках", exc_info=True)
    return len(radars)
