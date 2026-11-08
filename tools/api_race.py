"""
Local race: who shows a new Avito lot first, the search page (what the bot uses) or the mobile API?

Run on a PC with a Russian (or Belarusian) exit IP:

    venv\\Scripts\\python.exe tools\\api_race.py

On prod the admin command /race starts it as a child process through one of the bot's proxies; it then sends to the
admin chat through the bot itself and never calls getUpdates (RACE_NO_UPDATES=1), so the bot's polling is untouched.

Settings come from race.env (git-ignored, next to bot.py) or the same RACE_* environment variables:
    RACE_BOT_TOKEN   test bot that receives the lots
    RACE_CHAT_ID     optional: found by itself after you send /start to the test bot
    RACE_PROXY       optional: http://user:pass@ip:port for Avito; empty = direct
    RACE_WEB_URL     optional: search page, newest first (s=104)
    RACE_API_QUERY   optional: words for the mobile API (default «iphone»)
    RACE_INTERVAL    optional: seconds between rounds (default 15)

Every round fetches the page and the API at the same moment. For each lot both sources showed, the bot gets one
message on first sight and a reply when the other source catches up. Rows go to logs/api_race.csv; /stats in the
test bot (or Ctrl+C) prints the summary.
"""

from __future__ import annotations

import asyncio
import csv
import html
import os
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from curl_cffi import requests as curl_requests  # noqa: E402

from anti_bot.manager import BROWSER_PROFILE  # noqa: E402
from parser import AvitoParser  # noqa: E402

MOBILE_API_KEY = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"
MOSCOW_LOCATION_ID = "637640"
PHONES_CATEGORY_ID = "84"
DEFAULT_WEB_URL = "https://www.avito.ru/moskva/telefony/mobilnye_telefony/apple-ASgBAgICAkS0wA3OqzmwwQ2I_Dc?s=104"
FRESH_S = 15 * 60          # a lot older than this at first sight is not new, only a resort of the list
SETTLE_S = 10 * 60         # after this, a lot one source never showed counts as «only that source»
BLOCK_PAUSE_S = 90
WEB_FAIL_PAUSE_S = 30
SUMMARY_EVERY_S = 30 * 60
CSV_PATH = ROOT / "logs" / "api_race.csv"


def load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    env.update({key: value for key, value in os.environ.items() if key.startswith("RACE_")})
    return env


def to_seconds(value) -> Optional[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    return value / 1000.0 if value > 10 ** 11 else float(value)


def api_url(query: str) -> str:
    from urllib.parse import urlencode
    params = {"key": MOBILE_API_KEY, "locationId": MOSCOW_LOCATION_ID, "categoryId": PHONES_CATEGORY_ID,
              "sort": "date", "page": 1, "display": "list", "limit": 50}
    if query:
        params["query"] = query
    return "https://m.avito.ru/api/11/items?" + urlencode(params)


def parse_api(data) -> dict[str, dict]:
    """id -> {title, price, pub, url} from /api/11/items. Ads (vip, xl) are skipped: they are not new lots."""
    found = {}
    items = ((data or {}).get("result") or {}).get("items") or []
    for entry in items:
        if not isinstance(entry, dict) or entry.get("type") not in (None, "item"):
            continue
        value = entry.get("value") if isinstance(entry.get("value"), dict) else entry
        ident = value.get("id")
        if not ident:
            continue
        uri = value.get("uri_mweb") or value.get("uri") or ""
        label = next((token.get("title") for block in value.get("freeForm") or [] if isinstance(block, dict)
                      and (block.get("content") or {}).get("id") == "sortTimeGrid"
                      for token in (block["content"].get("tokens") or []) if isinstance(token, dict)), "")
        found[str(ident)] = {
            "label": label or "",
            "title": str(value.get("title") or ""),
            "price": str((value.get("price") or "")) if not isinstance(value.get("price"), dict)
            else str(value["price"].get("current") or value["price"].get("value") or value["price"].get("title") or ""),
            "pub": to_seconds(value.get("time") or value.get("sortTimeStamp")),
            "url": f"https://www.avito.ru/{ident}" if not uri.startswith("http") else uri,
        }
    return found


def parse_web(page: str) -> Optional[dict[str, dict]]:
    items = AvitoParser.parse_catalog(page)
    if items is None:
        return None
    return {
        str(item.item_id): {"title": item.title, "price": item.price, "pub": to_seconds(item.timestamp),
                            "url": f"https://www.avito.ru/{item.item_id}"}
        for item in items if not item.is_promotion
    }


class Race:
    def __init__(self, env: dict):
        self.token = env.get("RACE_BOT_TOKEN", "")
        self.chat_id = env.get("RACE_CHAT_ID", "")
        self.proxy = env.get("RACE_PROXY", "")
        self.web_url = env.get("RACE_WEB_URL") or DEFAULT_WEB_URL
        self.api_url = api_url(env.get("RACE_API_QUERY", "iphone"))
        self.interval = float(env.get("RACE_INTERVAL") or 15)
        self.duration_s = float(env.get("RACE_MINUTES") or 0) * 60
        self.no_updates = env.get("RACE_NO_UPDATES") == "1"   # prod: the bot itself polls this token
        self.keep_proxy = env.get("RACE_KEEP_PROXY") == "1"   # prod: the server IP is banned, never fall back to it
        self.label = env.get("RACE_LABEL", "")
        self.lots: dict[str, dict] = {}       # id -> {title, price, pub, url, web, api, msg, done}
        self.baseline = {"web": False, "api": False}
        self.paused_until = {"web": 0.0, "api": 0.0}
        self.errors = {"web": 0, "api": 0}
        self.answers = {"web": 0, "api": 0}   # rounds where the source returned a list
        self.blocks = {"web": 0, "api": 0}    # 403/429/302-to-firewall answers
        self.rounds = 0
        self.update_offset = 0
        self.started = time.time()
        self.last_summary = time.time()
        self.avito = None
        self.avito_proxy = False
        self.lock = threading.Lock()
        self.last_seen = {"web": time.time(), "api": time.time()}
        self.tg = curl_requests.AsyncSession(impersonate=BROWSER_PROFILE)

    def new_avito_session(self, use_proxy: bool) -> AvitoParser:
        return AvitoParser(self.proxy if use_proxy and self.proxy else None)

    # ── Telegram ──────────────────────────────────────────────────────────
    async def tg_call(self, method: str, **payload):
        try:
            resp = await self.tg.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=20)
            data = resp.json()
            return data.get("result") if data.get("ok") else None
        except Exception as exc:
            print(f"telegram {method}: {exc}")
            return None

    async def send(self, text: str, reply_to: Optional[int] = None) -> Optional[int]:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        result = await self.tg_call("sendMessage", **payload)
        return result.get("message_id") if isinstance(result, dict) else None

    async def poll_updates(self, wait: int = 0):
        if self.no_updates:
            return
        updates = await self.tg_call("getUpdates", offset=self.update_offset, timeout=wait) or []
        for update in updates:
            self.update_offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat = (message.get("chat") or {}).get("id")
            text = (message.get("text") or "").strip()
            if chat and not self.chat_id:
                self.chat_id = str(chat)
                print(f"chat_id = {chat} (впиши в race.env как RACE_CHAT_ID, чтобы не ждать /start)")
                await self.send("Гонка запущена: сайт против мобильного API. /stats — итоги.")
            elif chat and str(chat) == self.chat_id and text.startswith("/stats"):
                await self.send(self.summary())

    # ── Avito ─────────────────────────────────────────────────────────────
    async def fetch(self, source: str) -> Optional[dict[str, dict]]:
        if time.time() < self.paused_until[source]:
            return None
        return await asyncio.to_thread(self._fetch_sync, source)

    def _fetch_sync(self, source: str) -> Optional[dict[str, dict]]:
        """The page goes through the bot's own fetch (anti-bot cookies, PoW, redirects); the API request reuses
        that session's cookies. One request at a time: the session is not thread-safe."""
        with self.lock:
            found = self._fetch_locked(source)
            self.last_seen[source] = time.time()
            return found

    def _fetch_locked(self, source: str) -> Optional[dict[str, dict]]:
        if source == "web":
            try:
                page = self.avito.fetch_page(self.web_url)
            except Exception as exc:
                page, failure = None, f"{type(exc).__name__}: {str(exc)[:120]}"
            else:
                fail = getattr(self.avito, "last_failure", None)
                failure = f"{getattr(fail, 'kind', '?')} {getattr(fail, 'status', '') or ''}".strip()
            if not page:
                self.errors["web"] += 1
                if getattr(getattr(self.avito, "last_failure", None), "status", None) in (302, 403, 429, 439):
                    self.blocks["web"] += 1
                self.paused_until["web"] = time.time() + WEB_FAIL_PAUSE_S
                print(f"web: страница не загрузилась ({failure}), пауза {WEB_FAIL_PAUSE_S} с")
                self.maybe_drop_proxy("web")
                return None
            found = parse_web(page)
            if found is None:
                print("web: на странице нет каталога (заглушка или логин)")
            else:
                self.errors["web"] = 0
            return found
        url, headers = self.api_url, {"Accept": "application/json", "Referer": "https://m.avito.ru/"}
        try:
            resp = self.avito.session.get(url, headers=headers, timeout=20, allow_redirects=False)
        except Exception as exc:
            self.errors["api"] += 1
            print(f"api: {type(exc).__name__}: {str(exc)[:120]}")
            self.maybe_drop_proxy("api")
            return None
        if AvitoParser._is_firewall(resp):
            self.errors["api"] += 1
            self.blocks["api"] += 1
            self.paused_until["api"] = time.time() + BLOCK_PAUSE_S
            print(f"api: блок {resp.status_code}, пауза {BLOCK_PAUSE_S} с")
            self.maybe_drop_proxy("api")
            return None
        if resp.status_code != 200:
            self.errors["api"] += 1
            print(f"api: HTTP {resp.status_code} {resp.text[:160]!r}")
            return None
        try:
            found = parse_api(resp.json())
        except Exception:
            print(f"api: не JSON {resp.text[:160]!r}")
            return None
        self.errors["api"] = 0
        return found

    def maybe_drop_proxy(self, source: str):
        """A blocked or dead proxy is dropped once for the whole race: both sources go direct from then on."""
        if self.keep_proxy:
            return
        if self.proxy and self.avito_proxy and "--check" not in sys.argv and self.errors[source] >= 2:
            print("прокси не работает — дальше напрямую")
            self.avito_proxy = False
            self.avito = self.new_avito_session(False)
            self.paused_until = {"web": 0.0, "api": 0.0}

    async def note(self, source: str, found: dict[str, dict], seen_at: float):
        if not self.baseline[source]:
            self.baseline[source] = True
            for ident, info in found.items():
                lot = self.lots.setdefault(ident, {**info, "web": None, "api": None, "msg": None, "done": True})
                lot[source] = seen_at
            print(f"{source}: стартовая выдача {len(found)} лотов")
            return
        other = "api" if source == "web" else "web"
        for ident, info in found.items():
            lot = self.lots.get(ident)
            if lot is None:
                pub = info.get("pub")
                fresh = pub is None or seen_at - pub <= FRESH_S
                lot = self.lots[ident] = {**info, "web": None, "api": None, "msg": None, "done": not fresh}
            if lot[source] is not None:
                continue
            lot[source] = seen_at
            if not lot.get("pub") and info.get("pub"):
                lot["pub"] = info["pub"]
            if lot["done"]:
                continue
            name = "API" if source == "api" else "сайт"
            pub_age = (f"{seen_at - lot['pub']:.0f} с после публикации" if lot.get("pub")
                       else f"Авито пишет «{info['label']}»" if info.get("label") else "время публикации неизвестно")
            if lot[other] is None:
                lot["msg"] = await self.send(
                    f"🆕 <b>{name}</b> первым · {pub_age}\n"
                    f"{html.escape(lot['title'][:80])} · {html.escape(str(lot['price']))}\n{lot['url']}"
                )
            else:
                lead = seen_at - lot[other]
                first = "API" if other == "api" else "Сайт"
                await self.send(f"⏱ {name} догнал: {first} был быстрее на <b>{lead:.0f} с</b>", reply_to=lot["msg"])
                lot["done"] = True
                self.write_row(ident, lot)

    def settle(self, now: float, settle_s: float = SETTLE_S):
        for ident, lot in self.lots.items():
            if not lot["done"] and (lot["web"] or lot["api"]) and now - (lot["web"] or lot["api"]) > settle_s:
                lot["done"] = True
                self.write_row(ident, lot)

    def write_row(self, ident: str, lot: dict):
        new_file = not CSV_PATH.exists()
        CSV_PATH.parent.mkdir(exist_ok=True)
        with CSV_PATH.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(["item", "pub", "web_seen", "api_seen", "api_lead_s", "title"])
            lead = (lot["web"] - lot["api"]) if lot["web"] and lot["api"] else ""
            writer.writerow([ident, lot.get("pub") or "", lot["web"] or "", lot["api"] or "",
                             f"{lead:.1f}" if lead != "" else "", lot["title"][:60]])
        lot["row"] = True

    # ── Summary ───────────────────────────────────────────────────────────
    def summary(self) -> str:
        rows = [lot for lot in self.lots.values() if lot.get("row")]
        both = [lot for lot in rows if lot["web"] and lot["api"]]
        leads = [lot["web"] - lot["api"] for lot in both]
        only_api = sum(1 for lot in rows if lot["api"] and not lot["web"])
        only_web = sum(1 for lot in rows if lot["web"] and not lot["api"])
        web_age = [lot["web"] - lot["pub"] for lot in rows if lot["web"] and lot.get("pub")]
        api_age = [lot["api"] - lot["pub"] for lot in rows if lot["api"] and lot.get("pub")]
        minutes = (time.time() - self.started) / 60
        took = f"{minutes:.0f} мин" if minutes < 120 else f"{minutes / 60:.1f} ч"

        def med(values):
            return f"{statistics.median(values):.0f} с" if values else "—"

        lines = [f"📊 <b>Итоги за {took}</b> ({self.rounds} раундов по {self.interval:.0f} с)"]
        if leads:
            api_first = sum(1 for lead in leads if lead > 0)
            lines += [
                f"Лотов у обоих: {len(both)}",
                f"API раньше: {api_first}, сайт раньше: {sum(1 for lead in leads if lead < 0)}, вместе: {sum(1 for lead in leads if lead == 0)}",
                f"Выигрыш API, медиана: <b>{statistics.median(leads):.0f} с</b> (от {min(leads):.0f} до {max(leads):.0f})",
            ]
        else:
            lines.append("Общих лотов пока нет.")
        lines += [
            f"От публикации до появления, медиана: сайт {med(web_age)}, API {med(api_age)}",
            f"Только в API: {only_api}, только на сайте: {only_web}",
            f"Ответили: сайт {self.answers['web']} из {self.rounds} раундов, API {self.answers['api']} из {self.rounds}",
            f"Блоков Авито: сайт {self.blocks['web']}, API {self.blocks['api']}",
        ]
        return "\n".join(lines)

    # ── Loop ──────────────────────────────────────────────────────────────
    async def run(self):
        if not self.token:
            raise SystemExit("В race.env нет RACE_BOT_TOKEN")
        self.avito_proxy = bool(self.proxy)
        self.avito = self.new_avito_session(self.avito_proxy)
        print(f"Сайт: {self.web_url}\nAPI:  {self.api_url}\nПрокси: {'да' if self.proxy else 'нет'}")
        while not self.chat_id:
            print("Напиши /start тестовому боту…")
            await self.poll_updates(wait=25)
        if self.update_offset == 0:
            await self.poll_updates()
            minutes = f" на {self.duration_s / 60:.0f} мин" if self.duration_s else ""
            via = f" через {html.escape(self.label)}" if self.label else ""
            hint = "Итоги придут в конце." if self.no_updates else "/stats — итоги."
            await self.send(f"🏁 Гонка запущена{minutes}{via}: сайт против мобильного API. {hint}")
        self.started = self.last_summary = time.time()  # the clock starts after /start, not while waiting for it
        while True:
            tick = time.monotonic()
            # One session, so the two requests go one after the other; the order alternates so neither source
            # is always second, and each source's lots are stamped with the time its own answer came back.
            order = ("web", "api") if self.rounds % 2 == 0 else ("api", "web")
            results = {source: await self.fetch(source) for source in order}
            web, api = results["web"], results["api"]
            seen_at = time.time()
            self.rounds += 1
            for source in order:
                if results[source] is not None:
                    self.answers[source] += 1
                    await self.note(source, results[source], self.last_seen[source])
            self.settle(seen_at)
            print(f"{time.strftime('%H:%M:%S')} раунд {self.rounds}: сайт {len(web) if web is not None else '×'}, "
                  f"API {len(api) if api is not None else '×'}")
            if self.duration_s and time.time() - self.started >= self.duration_s:
                self.settle(time.time() + SETTLE_S, settle_s=0)  # lots one source still lacks count as «only»
                await self.send(self.summary())
                print(self.summary().replace("<b>", "").replace("</b>", ""))
                return
            if seen_at - self.last_summary >= SUMMARY_EVERY_S:
                self.last_summary = seen_at
                await self.send(self.summary())
            await self.poll_updates()
            await asyncio.sleep(max(1.0, self.interval - (time.monotonic() - tick)))


async def check(race: "Race"):
    """--check: one request to each source, no Telegram. Shows whether the API answers at all."""
    for use_proxy in ([True, False] if race.proxy else [False]):
        race.avito_proxy = use_proxy
        race.avito = race.new_avito_session(use_proxy)
        race.paused_until = {"web": 0.0, "api": 0.0}  # a block on the proxy must not skip the direct try
        race.errors = {"web": 0, "api": 0}
        print(f"\n=== {'через прокси' if use_proxy else 'напрямую'} ===")
        for source in ("api", "web"):
            started = time.monotonic()
            found = await race.fetch(source)
            ms = int((time.monotonic() - started) * 1000)
            name = "API " if source == "api" else "Сайт"
            if found is None:
                print(f"{name}: НЕ РАБОТАЕТ ({ms} мс), причина строкой выше")
                continue
            ages = sorted(time.time() - lot["pub"] for lot in found.values() if lot.get("pub"))
            newest = f", самый свежий {ages[0]:.0f} с назад" if ages else ""
            print(f"{name}: OK, {len(found)} лотов за {ms} мс{newest}")
            for ident, lot in list(found.items())[:3]:
                print(f"   {ident} · {lot['title'][:45]} · {lot['price']} {lot.get('label', '')}")
        if not race.errors["api"] and not race.errors["web"]:
            break


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # the Windows console is cp1251: «₽» must not kill the run
        except Exception:
            pass
    race = Race(load_env(ROOT / "race.env"))
    if "--check" in sys.argv:
        asyncio.run(check(race))
        return
    try:
        asyncio.run(race.run())
    except KeyboardInterrupt:
        print(race.summary().replace("<b>", "").replace("</b>", ""))


if __name__ == "__main__":
    main()
