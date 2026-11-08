"""
Race: who shows a new Avito lot first, the search page (what the bot uses) or the mobile API?

Locally, on a PC with a Russian (or Belarusian) exit IP:

    venv\\Scripts\\python.exe tools\\api_race.py            # the race
    venv\\Scripts\\python.exe tools\\api_race.py --check    # one request to each source

On prod the admin command /race starts it as a child process with the bot's proxies; it then sends to the admin chat
through the bot itself and never calls getUpdates (RACE_NO_UPDATES=1), so the bot's polling is untouched.
SIGTERM (/race stop) ends the race early and still sends the report.

The site and the API each keep their own proxy and their own session, so a block on one never pauses the other.
A source that gets blocked (or fails twice in a row) moves to the next proxy at once; the blocked proxy rests
PROXY_REST_S for that source. The report has a per-proxy table, so «it is just this proxy» can be told apart from
«Avito guards the API».

Settings come from race.env (git-ignored, next to bot.py) or the same RACE_* environment variables:
    RACE_BOT_TOKEN     bot that receives the report
    RACE_CHAT_ID       optional locally: found by itself after you send /start to the test bot
    RACE_PROXIES       optional: proxies one per line (http://user:pass@ip:port); the first ones are tried first
    RACE_PROXY         optional: one proxy (old setting); empty with no RACE_PROXIES = direct
    RACE_WEB_URL       optional: search page, newest first (s=104)
    RACE_API_QUERY     optional: words for the mobile API (default «iphone»)
    RACE_INTERVAL      optional: seconds between rounds (default 15)
    RACE_MINUTES       optional: stop after this many minutes and send the report
    RACE_LOT_MESSAGES  optional: 1 = a message per lot as it is seen (off by default, the report has every lot)
"""

from __future__ import annotations

import asyncio
import csv
import dataclasses
import html
import json
import os
import signal
import statistics
import sys
import threading
import time
from collections import Counter
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
PROXY_REST_S = 5 * 60      # a proxy that blocked a source is not used for that source for this long
SUMMARY_EVERY_S = 30 * 60
MIN_FAIR_LOTS = 6          # a 5-minute race on the iPhone search brings ~8–10 new lots
PREWARM_TRIES = 6          # proxies the API may try before the clock starts
BLOCK_STATUSES = (302, 403, 429, 439)
SOURCES = ("web", "api")
NAMES = {"web": "сайт", "api": "API"}
CSV_PATH = ROOT / "logs" / "api_race.csv"
REPORT_DIR = ROOT / "logs"


def load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    env.update({key: value for key, value in os.environ.items() if key.startswith("RACE_")})
    return env


def proxy_label(proxy: Optional[str]) -> str:
    if not proxy:
        return "напрямую"
    return proxy.rsplit("@", 1)[-1].split("://", 1)[-1]


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


def _has_image(value: dict) -> bool:
    return any(("image" in key.lower() or "gallery" in key.lower() or "photo" in key.lower()) and value[key]
               for key in value)


def parse_api(data) -> tuple[dict[str, dict], dict]:
    """(id -> {title, price, pub, url, label, image, address}, stats) from /api/11/items.
    Ads (vip, xl, other non-item types) are skipped: they are not new lots. Stats keep what the bot would miss."""
    found: dict[str, dict] = {}
    stats = {"types": Counter(), "keys": Counter(), "sample": None, "items": 0}
    items = ((data or {}).get("result") or {}).get("items") or []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        stats["types"][str(entry.get("type"))] += 1
        if entry.get("type") not in (None, "item"):
            continue
        value = entry.get("value") if isinstance(entry.get("value"), dict) else entry
        ident = value.get("id")
        if not ident:
            continue
        stats["items"] += 1
        stats["keys"].update(value.keys())
        if stats["sample"] is None:
            stats["sample"] = value
        uri = value.get("uri_mweb") or value.get("uri") or ""
        label = next((token.get("title") for block in value.get("freeForm") or [] if isinstance(block, dict)
                      and (block.get("content") or {}).get("id") == "sortTimeGrid"
                      for token in (block["content"].get("tokens") or []) if isinstance(token, dict)), "")
        price = value.get("price")
        if isinstance(price, dict):
            price = price.get("current") or price.get("value") or price.get("title") or ""
        seller = value.get("sellerInfo") if isinstance(value.get("sellerInfo"), dict) else {}
        found[str(ident)] = {
            "label": label or "",
            "title": str(value.get("title") or ""),
            "price": str(price or ""),
            "pub": to_seconds(value.get("time") or value.get("sortTimeStamp")),
            "url": f"https://www.avito.ru/{ident}" if not uri.startswith("http") else uri,
            "image": _has_image(value),
            "address": bool(value.get("address") or value.get("location") or value.get("geo")
                            or seller.get("logoImageAlt")),
        }
    return found, stats


def parse_web(page: str) -> tuple[Optional[dict[str, dict]], dict]:
    """(id -> lot for ordinary lots, stats) from the search page; None when the page has no catalog."""
    items = AvitoParser.parse_catalog(page)
    stats = {"promo": 0, "sample": None}
    if items is None:
        return None, stats
    found = {}
    for item in items:
        if item.is_promotion:
            stats["promo"] += 1
            continue
        if stats["sample"] is None:
            stats["sample"] = dataclasses.asdict(item) if dataclasses.is_dataclass(item) else dict(vars(item))
        found[str(item.item_id)] = {
            "title": item.title, "price": item.price, "pub": to_seconds(item.timestamp), "label": "",
            "url": f"https://www.avito.ru/{item.item_id}", "image": bool(item.image_url), "address": bool(item.address),
        }
    return found, stats


def pct(part: float, total: float) -> str:
    return f"{part / total * 100:.0f}%" if total else "—"


def med(values, unit: str = " с") -> str:
    return f"{statistics.median(values):.0f}{unit}" if values else "—"


def p90(values) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]


class Race:
    def __init__(self, env: dict):
        self.token = env.get("RACE_BOT_TOKEN", "")
        self.chat_id = env.get("RACE_CHAT_ID", "")
        listed = [line.strip() for line in (env.get("RACE_PROXIES") or "").replace(",", "\n").splitlines() if line.strip()]
        self.proxies: list[Optional[str]] = listed or ([env["RACE_PROXY"]] if env.get("RACE_PROXY") else [None])
        self.proxy = self.proxies[0] or ""  # kept for --check and old callers
        self.web_url = env.get("RACE_WEB_URL") or DEFAULT_WEB_URL
        self.api_query = env.get("RACE_API_QUERY", "iphone")
        self.api_url = api_url(self.api_query)
        self.interval = float(env.get("RACE_INTERVAL") or 15)
        self.duration_s = float(env.get("RACE_MINUTES") or 0) * 60
        self.no_updates = env.get("RACE_NO_UPDATES") == "1"   # prod: the bot itself polls this token
        self.lot_messages = env.get("RACE_LOT_MESSAGES") == "1"
        self.label = env.get("RACE_LABEL", "")
        # Per source: the proxy in use, failures in a row on it, and when each proxy may be used again.
        self.current = {"web": 0, "api": min(1, len(self.proxies) - 1)}
        self.fails_in_row = {"web": 0, "api": 0}
        self.rest_until = {"web": {}, "api": {}}
        self.switches = {"web": 0, "api": 0}
        self.by_proxy: dict[str, dict] = {}                   # label -> {source: {try, ok, block, reasons}}
        self.web_parsers: dict[str, AvitoParser] = {}
        self.api_sessions: dict[str, object] = {}
        self.api_warmers: dict[str, AvitoParser] = {}         # the API's own page fetchers, for anti-bot cookies
        self.api_warm: dict[str, bool] = {}
        self.warm_ms: list[int] = []
        self.warmups = 0
        self.lots: dict[str, dict] = {}
        self.baseline = {"web": False, "api": False}
        self.paused_until = {"web": 0.0, "api": 0.0}
        self.errors = {"web": 0, "api": 0}
        self.answers = {"web": 0, "api": 0}                   # rounds where the source returned a list
        self.blocks = {"web": 0, "api": 0}                    # 302/403/429/439 or a firewall page
        self.reasons = {"web": Counter(), "api": Counter()}   # why a round gave no list
        self.latency_ms = {"web": [], "api": []}              # successful answers only
        self.kb = {"web": [], "api": []}
        self.counts = {"web": [], "api": []}
        self.overlap: list[dict] = []                         # rounds where both answered
        self.api_stats = {"types": Counter(), "keys": Counter(), "sample": None, "items": 0}
        self.web_stats = {"promo": [], "sample": None}
        self.timeline: list[dict] = []
        self.cur: dict = {}
        self.rounds = 0
        self.update_offset = 0
        self.started = time.time()
        self.last_summary = time.time()
        self.locks = {"web": threading.Lock(), "api": threading.Lock()}
        self.last_seen = {"web": time.time(), "api": time.time()}
        self.stop_event: Optional[asyncio.Event] = None
        self.stop_reason = ""
        self.tg = curl_requests.AsyncSession(impersonate=BROWSER_PROFILE)

    # ── Proxies ───────────────────────────────────────────────────────────
    def proxy_of(self, source: str) -> Optional[str]:
        return self.proxies[self.current[source]]

    def proxy_stats(self, proxy: Optional[str], source: str) -> dict:
        row = self.by_proxy.setdefault(proxy_label(proxy), {s: {"try": 0, "ok": 0, "block": 0, "reasons": Counter()}
                                                            for s in SOURCES})
        return row[source]

    def web_parser(self, proxy: Optional[str]) -> AvitoParser:
        key = proxy_label(proxy)
        if key not in self.web_parsers:
            self.web_parsers[key] = AvitoParser(proxy)
        return self.web_parsers[key]

    def api_session(self, proxy: Optional[str]):
        key = proxy_label(proxy)
        if key not in self.api_sessions:
            kwargs = {"impersonate": BROWSER_PROFILE}
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}
            self.api_sessions[key] = curl_requests.Session(**kwargs)
        return self.api_sessions[key]

    def warm_api(self, proxy: Optional[str], info: dict) -> bool:
        """Avito answers 439 «pass the check» to a session without its anti-bot cookies. The API session gets them
        the way the first races did: the bot's own page fetch on this proxy (it solves the check), then the cookies
        and headers move to the API session. The page parser is the API's own, never the site source's."""
        key = proxy_label(proxy)
        parser = self.api_warmers.get(key) or AvitoParser(proxy)
        self.api_warmers[key] = parser
        started = time.monotonic()
        try:
            page = parser.fetch_page(self.web_url)
        except Exception as exc:
            page = None
            info["kind"] = f"прогрев: {type(exc).__name__}"
        info["warm_ms"] = int((time.monotonic() - started) * 1000)
        self.warm_ms.append(info["warm_ms"])
        if not page:
            fail = getattr(parser, "last_failure", None)
            if "kind" not in info:
                status = getattr(fail, "status", None)
                info["kind"] = f"прогрев не прошёл: {getattr(fail, 'kind', '?')}" + (f" {status}" if status else "")
            return False
        session = self.api_session(proxy)
        for cookie in parser.session.cookies.jar:
            session.cookies.jar.set_cookie(cookie)
        session.headers.update({k: v for k, v in parser.session.headers.items() if k.lower() != "cookie"})
        self.api_warm[key] = True
        self.warmups += 1
        return True

    def switch_proxy(self, source: str, rest: bool):
        """Move the source to the next proxy that is not resting for it and, when there is a choice, not the one
        the other source uses. When every proxy rests, the source waits for the first one to come back."""
        now = time.time()
        if rest:
            self.rest_until[source][self.current[source]] = now + PROXY_REST_S
        self.fails_in_row[source] = 0
        total = len(self.proxies)
        if total == 1:
            if rest:
                self.paused_until[source] = now + PROXY_REST_S
            return
        other = self.current["api" if source == "web" else "web"]
        start = self.current[source]
        for step in range(1, total + 1):
            idx = (start + step) % total
            if self.rest_until[source].get(idx, 0) > now or (idx == other and total > 2):
                continue
            if idx != start:
                self.switches[source] += 1
                print(f"{source}: переключаюсь на {proxy_label(self.proxies[idx])}")
            self.current[source] = idx
            return
        first_back = min(self.rest_until[source].values(), default=now)
        self.paused_until[source] = first_back
        print(f"{source}: все прокси отдыхают, жду {first_back - now:.0f} с")

    def prewarm_api(self) -> bool:
        """Walk the API over proxies until one passes Avito's check, before the race clock starts."""
        for _ in range(min(PREWARM_TRIES, len(self.proxies))):
            proxy = self.proxy_of("api")
            info: dict = {}
            with self.locks["api"]:
                if self.warm_api(proxy, info):
                    return True
            stats = self.proxy_stats(proxy, "api")
            stats["try"] += 1
            stats["reasons"][info.get("kind") or "прогрев не прошёл"] += 1
            self.switch_proxy("api", rest=True)
            if time.time() < self.paused_until["api"]:
                return False
        return False

    # ── Telegram ──────────────────────────────────────────────────────────
    async def tg_call(self, method: str, **payload):
        try:
            resp = await self.tg.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=20)
            data = resp.json()
            if not data.get("ok"):
                print(f"telegram {method}: {str(data)[:200]}")
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

    async def send_file(self, path: Path, caption: str) -> bool:
        try:
            import aiohttp
            form = aiohttp.FormData()
            form.add_field("chat_id", str(self.chat_id))
            form.add_field("caption", caption[:1024])
            form.add_field("parse_mode", "HTML")
            form.add_field("document", path.read_bytes(), filename=path.name, content_type="text/plain")
            async with aiohttp.ClientSession() as session:
                async with session.post(f"https://api.telegram.org/bot{self.token}/sendDocument", data=form,
                                        timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    data = await resp.json(content_type=None)
            if not data.get("ok"):
                print(f"telegram sendDocument: {str(data)[:200]}")
            return bool(data.get("ok"))
        except Exception as exc:
            print(f"telegram sendDocument: {exc}")
            return False

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
            elif chat and str(chat) == self.chat_id and text.startswith("/stats"):
                await self.send(self.summary())

    # ── Avito ─────────────────────────────────────────────────────────────
    async def fetch(self, source: str) -> Optional[dict[str, dict]]:
        if time.time() < self.paused_until[source]:
            self.cur[source] = {"ok": False, "kind": "все прокси отдыхают", "ms": 0, "proxy": "—"}
            self.reasons[source]["все прокси отдыхают"] += 1
            return None
        return await asyncio.to_thread(self._fetch_sync, source)

    def _fetch_sync(self, source: str) -> Optional[dict[str, dict]]:
        """Each source has its own lock, proxy and session: the two requests of a round never wait for each other."""
        with self.locks[source]:
            proxy = self.proxy_of(source)
            stats = self.proxy_stats(proxy, source)
            started = time.monotonic()
            info: dict = {"ok": False, "proxy": proxy_label(proxy)}
            try:
                found = self._fetch_locked(source, proxy, info)
            except Exception as exc:  # a bug here must cost one round, not the race
                found, info["kind"] = None, f"сбой {type(exc).__name__}: {str(exc)[:80]}"
            info["ms"] = int((time.monotonic() - started) * 1000)
            info["ok"] = found is not None
            self.last_seen[source] = time.time()
            stats["try"] += 1
            if found is not None:
                stats["ok"] += 1
                self.fails_in_row[source] = 0
                self.latency_ms[source].append(info["ms"] - info.get("warm_ms", 0))
                self.counts[source].append(len(found))
                if info.get("kb"):
                    self.kb[source].append(info["kb"])
            else:
                kind = info.get("kind") or "?"
                self.reasons[source][kind] += 1
                stats["reasons"][kind] += 1
                if info.get("blocked"):
                    if not info.get("warm_fail"):
                        stats["block"] += 1
                        self.blocks[source] += 1
                    self.switch_proxy(source, rest=True)
                else:
                    self.fails_in_row[source] += 1
                    if self.fails_in_row[source] >= 2:
                        self.switch_proxy(source, rest=False)
            self.cur[source] = info
            return found

    def _fetch_locked(self, source: str, proxy: Optional[str], info: dict) -> Optional[dict[str, dict]]:
        if source == "web":
            parser = self.web_parser(proxy)
            fail = None
            try:
                page = parser.fetch_page(self.web_url)
            except Exception as exc:
                page = None
                info["kind"] = f"исключение {type(exc).__name__}: {str(exc)[:80]}"
            else:
                fail = getattr(parser, "last_failure", None)
            if not page:
                status = getattr(fail, "status", None)
                kind = str(getattr(fail, "kind", "") or "")
                if fail is not None:
                    info["kind"] = (kind or "?") + (f" {status}" if status else "")
                    detail = str(getattr(fail, "detail", "") or "")
                    if detail:
                        info["body"] = detail[:160]
                # A cooldown is the parser remembering a block on this proxy: for the race it is a block too.
                info["blocked"] = status in BLOCK_STATUSES or any(
                    word in kind for word in ("block", "forbidden", "captcha", "challenge", "cooldown"))
                print(f"web [{proxy_label(proxy)}]: страница не загрузилась ({info.get('kind')})")
                return None
            info["kb"] = round(len(page.encode("utf-8", errors="ignore")) / 1024)
            found, stats = parse_web(page)
            if found is None:
                info["kind"] = "нет каталога на странице"
                info["blocked"] = True  # a stub instead of the catalog is how Avito hides a soft block
                return None
            self.web_stats["promo"].append(stats["promo"])
            if self.web_stats["sample"] is None:
                self.web_stats["sample"] = stats["sample"]
            return found

        if not self.api_warm.get(proxy_label(proxy)) and not self.warm_api(proxy, info):
            info["blocked"] = info["warm_fail"] = True  # move on, but it is the page that failed, not the API
            return None
        headers = {"Accept": "application/json", "Referer": "https://m.avito.ru/"}
        try:
            resp = self.api_session(proxy).get(self.api_url, headers=headers, timeout=20, allow_redirects=False)
        except Exception as exc:
            text = str(exc)
            info["kind"] = ("таймаут" if "timed out" in text.lower() or "timeout" in text.lower()
                            else f"сеть {type(exc).__name__}")
            info["body"] = text[:160]
            print(f"api [{proxy_label(proxy)}]: {type(exc).__name__}: {text[:120]}")
            return None
        info["kb"] = round(len(resp.content or b"") / 1024)
        if resp.status_code in BLOCK_STATUSES or AvitoParser._is_firewall(resp):
            info["kind"] = f"блок {resp.status_code}"
            info["blocked"] = True
            self.api_warm[proxy_label(proxy)] = False  # next time on this proxy: fresh cookies first
            print(f"api [{proxy_label(proxy)}]: блок {resp.status_code}")
            return None
        if resp.status_code != 200:
            info["kind"] = f"HTTP {resp.status_code}"
            info["body"] = resp.text[:160]
            print(f"api [{proxy_label(proxy)}]: HTTP {resp.status_code} {resp.text[:160]!r}")
            return None
        try:
            data = resp.json()
        except Exception:
            info["kind"] = "не JSON"
            info["body"] = resp.text[:160]
            return None
        found, stats = parse_api(data)
        self.api_stats["types"].update(stats["types"])
        self.api_stats["keys"].update(stats["keys"])
        self.api_stats["items"] += stats["items"]
        if self.api_stats["sample"] is None:
            self.api_stats["sample"] = stats["sample"]
        if stats["items"] == 0:
            info["body"] = resp.text[:160]
        return found

    # ── Lots ──────────────────────────────────────────────────────────────
    def _new_lot(self, info: dict, done: bool, base: bool, stale: bool = False) -> dict:
        return {"title": info.get("title", ""), "price": info.get("price", ""), "url": info.get("url", ""),
                "pub": info.get("pub"), "label": info.get("label", ""), "web": None, "api": None,
                "web_r": None, "api_r": None, "msg": None, "done": done, "base": base, "stale": stale}

    def _remember_fields(self, lot: dict, source: str, info: dict):
        """What this very source gave for the lot: the field check must not borrow the other source's data."""
        lot[f"{source}_fields"] = {key: bool(info.get(key)) for key in ("price", "pub", "image", "address")}
        if not lot.get("pub") and info.get("pub"):
            lot["pub"] = info["pub"]
        if info.get("label"):
            lot["label"] = info["label"]

    async def note(self, source: str, found: dict[str, dict], seen_at: float, round_no: int):
        if not self.baseline[source]:
            self.baseline[source] = True
            for ident, info in found.items():
                lot = self.lots.setdefault(ident, self._new_lot(info, done=True, base=True))
                lot[source], lot[f"{source}_r"] = seen_at, round_no
            print(f"{source}: стартовая выдача {len(found)} лотов")
            return
        other = "api" if source == "web" else "web"
        for ident, info in found.items():
            lot = self.lots.get(ident)
            if lot is None:
                pub = info.get("pub")
                fresh = pub is None or seen_at - pub <= FRESH_S
                lot = self.lots[ident] = self._new_lot(info, done=not fresh, base=False, stale=not fresh)
            if lot[source] is not None:
                continue
            lot[source], lot[f"{source}_r"] = seen_at, round_no
            self._remember_fields(lot, source, info)
            if lot["done"]:
                continue
            if lot[other] is None:
                if self.lot_messages:
                    age = f"{seen_at - lot['pub']:.0f} с после публикации" if lot.get("pub") else "время неизвестно"
                    lot["msg"] = await self.send(f"🆕 <b>{NAMES[source]}</b> первым · {age}\n"
                                                 f"{html.escape(lot['title'][:80])}\n{lot['url']}")
            else:
                lot["done"] = True
                if self.lot_messages:
                    await self.send(f"⏱ {NAMES[source]} догнал через {seen_at - lot[other]:.0f} с", reply_to=lot["msg"])
                self.write_row(ident, lot)

    def note_overlap(self, web: dict, api: dict):
        """Both answered in this round: how much of each list the other one has."""
        web_ids, api_ids = set(web), set(api)
        if web_ids and api_ids:
            self.overlap.append({"web": len(web_ids), "api": len(api_ids), "both": len(web_ids & api_ids)})

    def settle(self, now: float, settle_s: float = SETTLE_S):
        for ident, lot in self.lots.items():
            if not lot["done"] and (lot["web"] or lot["api"]) and now - (lot["web"] or lot["api"]) > settle_s:
                lot["done"] = True
                self.write_row(ident, lot)

    def write_row(self, ident: str, lot: dict):
        lot["row"] = True
        try:
            new_file = not CSV_PATH.exists()
            CSV_PATH.parent.mkdir(exist_ok=True)
            with CSV_PATH.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if new_file:
                    writer.writerow(["item", "pub", "web_seen", "api_seen", "api_lead_s", "title"])
                lead = (lot["web"] - lot["api"]) if lot["web"] and lot["api"] else ""
                writer.writerow([ident, lot.get("pub") or "", lot["web"] or "", lot["api"] or "",
                                 f"{lead:.1f}" if lead != "" else "", lot["title"][:60]])
        except OSError as exc:
            print(f"csv: {exc}")

    # ── Analysis ──────────────────────────────────────────────────────────
    def answered_in(self, source: str, round_no: int) -> bool:
        if 1 <= round_no <= len(self.timeline):
            return bool(self.timeline[round_no - 1].get(source, {}).get("ok"))
        return False

    def classify(self) -> dict:
        """Every new lot, split into fair wins (the loser answered in the winner's round and the lot was not there)
        and unclear ones (the loser did not answer then, so its lag may be our polling gap)."""
        res = {"fair": {"web": [], "api": []}, "unclear": {"web": [], "api": []}, "same": 0,
               "only": {"web": [], "api": []}, "only_unclear": {"web": [], "api": []}, "rows": []}
        for ident, lot in self.lots.items():
            if lot.get("base") or lot.get("stale") or not lot.get("row"):
                continue
            w, a = lot.get("web_r"), lot.get("api_r")
            if w and a:
                if w == a:
                    res["same"] += 1
                    res["rows"].append((ident, lot, "один раунд", None))
                    continue
                winner = "api" if a < w else "web"
                first, loser = (a, "web") if winner == "api" else (w, "api")
                fair = self.answered_in(loser, first)
                res["fair" if fair else "unclear"][winner].append((abs(lot["web"] - lot["api"]), abs(w - a)))
                res["rows"].append((ident, lot, "честно" if fair else "неясно", winner))
            else:
                src = "web" if w else "api"
                other = "api" if src == "web" else "web"
                first = w or a
                later_ok = sum(1 for r in range(first, len(self.timeline) + 1) if self.answered_in(other, r))
                (res["only"] if later_ok >= 3 else res["only_unclear"])[src].append(ident)
                res["rows"].append((ident, lot, f"только {NAMES[src]} (у соперника потом {later_ok} ответов)", src))
        return res

    def field_share(self, source: str, key: str) -> Optional[float]:
        rows = [lot[f"{source}_fields"] for lot in self.lots.values() if lot.get(f"{source}_fields")]
        return sum(1 for f in rows if f.get(key)) / len(rows) if rows else None

    def proxy_verdict(self) -> list[str]:
        """Is it one bad proxy or Avito guarding the API? Count proxies where the site worked and the API did not."""
        site_ok = [label for label, row in self.by_proxy.items() if row["web"]["ok"]]
        api_tried = [label for label, row in self.by_proxy.items() if row["api"]["try"]]
        api_ok = [label for label in api_tried if self.by_proxy[label]["api"]["ok"]]
        api_dead_where_site_ok = [label for label in api_tried if label in site_ok
                                  and not self.by_proxy[label]["api"]["ok"]]
        api_blocked = [label for label in api_tried if self.by_proxy[label]["api"]["block"]
                       and not self.by_proxy[label]["api"]["ok"]]
        lines = [f"Прокси: API пробовали на {len(api_tried)}, ответил хоть раз на {len(api_ok)}, "
                 f"заблокирован на {len(api_blocked)}; сайт ответил хоть раз на {len(site_ok)}"]
        if not site_ok and not api_ok:
            lines.append("Ни сайт, ни API не ответили ни на одном прокси — дело в прокси или сети, а не в API")
        elif len(api_tried) >= 3 and not api_ok and len(api_blocked) >= 2:
            lines.append(f"API заблокирован на всех {len(api_tried)} прокси, а сайт работал на {len(site_ok)} — "
                         "дело не в конкретном прокси, Авито закрывает API")
        elif api_dead_where_site_ok:
            lines.append(f"На {len(api_dead_where_site_ok)} прокси сайт работал, а API нет — API охраняют строже")
        elif api_ok and len(api_blocked):
            lines.append(f"API блокировали на {len(api_blocked)} прокси, но на {len(api_ok)} он работал — "
                         "часть прокси просто плохие")
        return lines

    def verdict(self, res: dict) -> list[str]:
        n = max(1, self.rounds)
        ok = {s: self.answers[s] / n for s in SOURCES}
        fair_api, fair_web = res["fair"]["api"], res["fair"]["web"]
        fair_total = len(fair_api) + len(fair_web)
        api_pub_share = self.field_share("api", "pub")
        enough = ok["web"] >= 0.7 and ok["api"] >= 0.7 and fair_total >= MIN_FAIR_LOTS
        api_share = len(fair_api) / fair_total if fair_total else 0.0
        api_leads = [s for s, _ in fair_api]
        faster = bool(fair_total and api_share >= 0.7 and api_leads
                      and statistics.median(api_leads) >= self.interval * 0.8)
        new_web = sum(1 for _, lot, _, _ in res["rows"] if lot.get("web"))
        miss_share = len(res["only"]["web"]) / new_web if new_web else 0.0
        problems = []
        if miss_share > 0.1:
            problems.append(f"API пропускает {miss_share * 100:.0f}% новых лотов сайта")
        if ok["api"] < ok["web"] - 0.1:
            problems.append(f"API отвечает реже сайта ({pct(self.answers['api'], n)} против {pct(self.answers['web'], n)})")
        if api_pub_share is not None and api_pub_share < 0.9:
            problems.append(f"у {100 - api_pub_share * 100:.0f}% лотов API нет своего времени публикации "
                            "(только надпись «N минут назад»)")
        if self.api_stats["types"] and set(self.api_stats["types"]) <= {"item", "None"} and any(self.web_stats["promo"]):
            problems.append("в API нет пометки рекламы: рекламные лоты уйдут как новые")

        if not enough:
            head = "⚪️ МАЛО ДАННЫХ — окончательный вывод по скорости делать нельзя"
        elif faster and not problems:
            head = "🟢 ПЕРЕХОДИТЬ СТОИТ: API быстрее, не пропускает лоты и отвечает не хуже сайта"
        elif faster:
            head = "🟡 API БЫСТРЕЕ, НО ЕСТЬ ОГРАНИЧЕНИЯ: годится как быстрый сигнал, сайт — для проверки и полей"
        else:
            head = "🔴 ПЕРЕХОДИТЬ НЕ СТОИТ: API не быстрее сайта настолько, чтобы окупить переделку"
        lines = [head]
        if fair_total:
            lines.append(f"Честных побед: API {len(fair_api)}, сайт {len(fair_web)} (API {api_share * 100:.0f}%)"
                         + (f", отрыв API медиана {statistics.median(api_leads):.0f} с" if api_leads else ""))
        if not enough:
            if ok["web"] < 0.7 or ok["api"] < 0.7:
                lines.append(f"Ответов: сайт {pct(self.answers['web'], n)}, API {pct(self.answers['api'], n)} (нужно ≥70% у обоих)")
            if fair_total < 10:
                lines.append(f"Честно сравнимых лотов {fair_total} (нужно ≥{MIN_FAIR_LOTS})")
        lines += self.proxy_verdict()
        lines += [f"⚠️ {p}" for p in problems]
        return lines

    def summary(self) -> str:
        res = self.classify()
        n = max(1, self.rounds)
        minutes = (time.time() - self.started) / 60
        took = f"{minutes:.0f} мин" if minutes < 120 else f"{minutes / 60:.1f} ч"
        fair_api, fair_web = res["fair"]["api"], res["fair"]["web"]
        lines = [f"📊 <b>Итоги за {took}</b> ({self.rounds} раундов по {self.interval:.0f} с, прокси {len(self.proxies)})"]
        lines += [html.escape(line) for line in self.verdict(res)]
        lines += [
            "",
            f"Честно: API раньше {len(fair_api)}, сайт раньше {len(fair_web)}",
            f"Ответили: сайт {self.answers['web']} из {n}, API {self.answers['api']} из {n}",
            f"Блоков: сайт {self.blocks['web']}, API {self.blocks['api']} · смен прокси: сайт {self.switches['web']}, "
            f"API {self.switches['api']}",
            "Подробности — в файле, перешлите его целиком.",
        ]
        text = "\n".join(lines)
        return text if len(text) <= 1000 else text[:990] + "…"

    def report(self) -> str:
        res = self.classify()
        n = max(1, self.rounds)
        now = time.time()
        out: list[str] = []
        add = out.append
        add("ГОНКА: САЙТ ПРОТИВ МОБИЛЬНОГО API (свой прокси у каждого, смена прокси при блоке)")
        add(f"Начало {time.strftime('%d.%m %H:%M:%S', time.localtime(self.started))}, длительность "
            f"{(now - self.started) / 60:.1f} мин, раундов {self.rounds}, интервал {self.interval:.0f} с"
            + (f", остановлено: {self.stop_reason}" if self.stop_reason else ""))
        add(f"Прокси в запасе: {len(self.proxies)}; заблокированный прокси отдыхает {PROXY_REST_S // 60} мин для того "
            "источника, где его заблокировали")
        add(f"Сайт: {self.web_url}")
        add(f"API:  {self.api_url.replace(MOBILE_API_KEY, 'KEY')}")
        add("Фильтры не идентичны (сайт — бренд Apple в телефонах, API — категория телефоны + «"
            f"{self.api_query}»), поэтому «только в одном» частично объясняется разной выдачей.")
        add("")
        add("== ВЕРДИКТ ==")
        out.extend(self.verdict(res))
        add("")
        add("== ПРОКСИ ==")
        add("прокси | сайт: ответил/попыток, блоков | API: ответил/попыток, блоков | причины")
        for label, row in sorted(self.by_proxy.items(), key=lambda kv: -(kv[1]["web"]["try"] + kv[1]["api"]["try"])):
            w, a = row["web"], row["api"]
            why = "; ".join(f"{NAMES[s]}: " + ", ".join(f"{k} {v}" for k, v in row[s]["reasons"].most_common(3))
                            for s in SOURCES if row[s]["reasons"])
            add(f"{label} | {w['ok']}/{w['try']}, {w['block']} | {a['ok']}/{a['try']}, {a['block']} | {why}")
        add("")
        add("== СКОРОСТЬ (новые лоты, которые показали оба) ==")
        add("Честно = в раунде, где победитель впервые показал лот, проигравший тоже ответил, но лота у него не было.")
        add("Неясно = проигравший в том раунде не ответил: его отставание может быть просто дырой в опросе.")
        for kind, title in (("fair", "Честно"), ("unclear", "Неясно")):
            for src in ("api", "web"):
                leads = res[kind][src]
                secs = [s for s, _ in leads]
                rnds = [r for _, r in leads]
                add(f"{title}: {NAMES[src]} раньше — {len(leads)} лотов"
                    + (f", отрыв медиана {med(secs)}, p90 {p90(secs):.0f} с, макс {max(secs):.0f} с, "
                       f"в раундах медиана {statistics.median(rnds):.0f}" if leads else ""))
        add(f"В одном раунде: {res['same']}")
        ages = {s: [lot[s] - lot["pub"] for _, lot, _, _ in res["rows"] if lot.get(s) and lot.get("pub")]
                for s in SOURCES}
        add(f"От публикации (время с сайта) до появления, медиана: сайт {med(ages['web'])}, API {med(ages['api'])}; "
            f"p90: сайт {p90(ages['web']):.0f} с, API {p90(ages['api']):.0f} с")
        add("")
        add("== ПОЛНОТА ==")
        add(f"Новые только на сайте (API потом отвечал ≥3 раз и не показал): {len(res['only']['web'])}")
        add(f"Новые только в API (сайт потом отвечал ≥3 раз и не показал): {len(res['only']['api'])}")
        add(f"Только в одном, но соперник почти не отвечал: сайт {len(res['only_unclear']['web'])}, "
            f"API {len(res['only_unclear']['api'])}")
        if self.overlap:
            share_web = statistics.median(o["both"] / o["web"] for o in self.overlap)
            share_api = statistics.median(o["both"] / o["api"] for o in self.overlap)
            add(f"Когда ответили оба ({len(self.overlap)} раундов): из лотов сайта в API есть {share_web * 100:.0f}% "
                f"(медиана), из лотов API на сайте {share_api * 100:.0f}%")
        add(f"Лотов в ответе, медиана: сайт {med(self.counts['web'], '')}, API {med(self.counts['api'], '')}; "
            f"рекламных на странице сайта: {med(self.web_stats['promo'], '')}")
        add(f"Типы записей в API: {dict(self.api_stats['types'].most_common(8))}")
        add("")
        add("== НАДЁЖНОСТЬ ==")
        for src in SOURCES:
            lat = self.latency_ms[src]
            add(f"{NAMES[src]}: ответил {self.answers[src]} из {n} раундов ({pct(self.answers[src], n)}), "
                f"блоков {self.blocks[src]}, смен прокси {self.switches[src]}, время ответа медиана {med(lat, ' мс')}, "
                f"p90 {p90(lat):.0f} мс, размер медиана {med(self.kb[src], ' КБ')}")
            reasons = ", ".join(f"{k} — {v}" for k, v in self.reasons[src].most_common(8))
            add(f"   почему не ответил: {reasons or 'всегда отвечал'}")
        add(f"Прогрев API (страница сайта на том же прокси ради антибот-куки): удачных {self.warmups}, "
            f"попыток {len(self.warm_ms)}, время медиана {med(self.warm_ms, ' мс')}. Время прогрева не входит "
            "в время ответа API, но в бою API без него не работает.")
        add("")
        add("== ПОЛЯ ДЛЯ УВЕДОМЛЕНИЯ (только то, что дал сам источник, новые лоты) ==")
        for src in SOURCES:
            shares = {key: self.field_share(src, key) for key in ("price", "pub", "image", "address")}
            if shares["price"] is not None:
                add(f"{NAMES[src]}: с ценой {shares['price'] * 100:.0f}%, со временем публикации {shares['pub'] * 100:.0f}%, "
                    f"с фото {shares['image'] * 100:.0f}%, с адресом {shares['address'] * 100:.0f}%")
        if self.api_stats["items"]:
            keys = ", ".join(f"{k} {v * 100 // self.api_stats['items']}%"
                             for k, v in self.api_stats["keys"].most_common(40))
            add(f"Поля лота в API (доля лотов): {keys}")
        add("")
        add("== ЛОТЫ ==")
        add("id | итог | победитель | сайт: раунд/с после публ. | API: раунд/с после публ. | метка API | название")

        def first_round(row) -> int:
            return min(r for r in (row[1].get("web_r"), row[1].get("api_r")) if r)

        for ident, lot, kind, winner in sorted(res["rows"], key=first_round):
            def cell(src):
                if not lot.get(src):
                    return "—"
                age = f"{lot[src] - lot['pub']:.0f}" if lot.get("pub") else "?"
                return f"р{lot[f'{src}_r']}/{age}"
            add(f"{ident} | {kind} | {NAMES.get(winner, '—') if winner else '—'} | {cell('web')} | {cell('api')} | "
                f"{lot.get('label') or ''} | {lot['title'][:50]}")
        add(f"Старых лотов, поднятых в выдаче (не считались): {sum(1 for lot in self.lots.values() if lot.get('stale'))}")
        add("")
        add("== РАУНДЫ ==")
        add("№ | время | первым спрашивали | сайт: итог мс КБ лотов [прокси] | API: итог мс КБ лотов [прокси]")
        for row in self.timeline:
            def part(src):
                info = row.get(src) or {}
                via = f" [{info.get('proxy', '?')}]"
                if info.get("ok"):
                    return f"ok {info.get('ms', 0)} {info.get('kb', '')} {info.get('count', '')}{via}"
                body = f" «{info['body'][:70]}»" if info.get("body") else ""
                return f"× {info.get('kind', '?')} {info.get('ms', 0)}{via}{body}"
            add(f"{row['n']} | {time.strftime('%H:%M:%S', time.localtime(row['t']))} | {NAMES[row['order']]} | "
                f"{part('web')} | {part('api')}")
        add("")
        add("== СЫРОЙ ЛОТ API (первый) ==")
        add(json.dumps(self.api_stats["sample"], ensure_ascii=False, default=str)[:4000]
            if self.api_stats["sample"] else "нет")
        add("")
        add("== СЫРОЙ ЛОТ САЙТА (первый обычный) ==")
        add(json.dumps(self.web_stats["sample"], ensure_ascii=False, default=str)[:2500]
            if self.web_stats["sample"] else "нет")
        return "\n".join(out)

    async def finish(self):
        self.settle(time.time() + SETTLE_S, settle_s=0)  # lots one source still lacks count as «only»
        text = self.report()
        stamp = time.strftime("%Y%m%d_%H%M", time.localtime(self.started))
        path: Optional[Path] = REPORT_DIR / f"race_report_{stamp}.txt"
        try:
            REPORT_DIR.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"Отчёт: {path}")
        except OSError as exc:
            print(f"отчёт не записан: {exc}")
            path = None
        summary = self.summary()
        print(summary.replace("<b>", "").replace("</b>", ""))
        if self.chat_id and self.token:
            sent = await self.send_file(path, summary) if path else False
            if not sent:
                await self.send(summary)

    # ── Loop ──────────────────────────────────────────────────────────────
    def request_stop(self, reason: str = "по команде /race stop"):
        self.stop_reason = reason
        if self.stop_event is not None:
            self.stop_event.set()

    async def run(self):
        if not self.token:
            raise SystemExit("В race.env нет RACE_BOT_TOKEN")
        self.stop_event = asyncio.Event()
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self.request_stop)
        except (NotImplementedError, AttributeError, RuntimeError):
            pass  # Windows: Ctrl+C is handled in main()
        print(f"Сайт: {self.web_url}\nAPI:  {self.api_url}\nПрокси: {len(self.proxies)}")
        while not self.chat_id:
            print("Напиши /start тестовому боту…")
            await self.poll_updates(wait=25)
        if self.update_offset == 0:
            await self.poll_updates()
        minutes = f" на {self.duration_s / 60:.0f} мин" if self.duration_s else ""
        via = f" · прокси в запасе: {len(self.proxies)}" if self.proxies[0] else " · напрямую"
        await self.send(f"🏁 Гонка запущена{minutes}{via}: сайт против мобильного API, у каждого свой прокси, "
                        "при блоке — смена. В конце придёт файл-отчёт с вердиктом.")
        # The API gets its anti-bot cookies before the clock starts, so the first rounds are not spent on that.
        prewarmed = await asyncio.to_thread(self.prewarm_api)
        print(f"api: прогрев перед стартом {'удался' if prewarmed else 'не удался'} "
              f"[{proxy_label(self.proxy_of('api'))}]")
        self.started = self.last_summary = time.time()  # the clock starts after /start, not while waiting for it
        while not self.stop_event.is_set():
            tick = time.monotonic()
            # Own sessions and proxies: both requests go out at the same moment, neither waits for the other.
            order = ("web", "api") if self.rounds % 2 == 0 else ("api", "web")
            self.cur = {}
            round_t = time.time()
            fetched = await asyncio.gather(*(self.fetch(source) for source in order))
            results = dict(zip(order, fetched))
            self.rounds += 1
            row = {"n": self.rounds, "t": round_t, "order": order[0]}
            for source in SOURCES:
                info = dict(self.cur.get(source) or {"ok": False, "kind": "?"})
                if results[source] is not None:
                    info["count"] = len(results[source])
                row[source] = info
            self.timeline.append(row)
            for source in order:
                if results[source] is not None:
                    self.answers[source] += 1
                    await self.note(source, results[source], self.last_seen[source], self.rounds)
            if results["web"] is not None and results["api"] is not None:
                self.note_overlap(results["web"], results["api"])
            self.settle(time.time())
            web, api = results["web"], results["api"]
            print(f"{time.strftime('%H:%M:%S')} раунд {self.rounds}: сайт {len(web) if web is not None else '×'} "
                  f"[{row['web'].get('proxy')}], API {len(api) if api is not None else '×'} [{row['api'].get('proxy')}]")
            if self.duration_s and time.time() - self.started >= self.duration_s:
                break
            if not self.no_updates and time.time() - self.last_summary >= SUMMARY_EVERY_S:
                self.last_summary = time.time()
                await self.send(self.summary())
            await self.poll_updates()
            try:
                await asyncio.wait_for(self.stop_event.wait(),
                                       timeout=max(1.0, self.interval - (time.monotonic() - tick)))
            except asyncio.TimeoutError:
                pass
        await self.finish()


async def check(race: "Race"):
    """--check: one request to each source through each proxy (at most 5), no Telegram."""
    for idx, proxy in enumerate(race.proxies[:5]):
        race.current = {"web": idx, "api": idx}
        race.paused_until = {"web": 0.0, "api": 0.0}
        print(f"\n=== {proxy_label(proxy)} ===")
        for source in ("api", "web"):
            found = await race.fetch(source)
            info = race.cur.get(source) or {}
            name = "API " if source == "api" else "Сайт"
            if found is None:
                print(f"{name}: НЕ РАБОТАЕТ ({info.get('ms', 0)} мс): {info.get('kind', '?')} {info.get('body', '')}")
                continue
            ages = sorted(time.time() - lot["pub"] for lot in found.values() if lot.get("pub"))
            newest = f", самый свежий {ages[0]:.0f} с назад" if ages else ""
            print(f"{name}: OK, {len(found)} лотов за {info.get('ms', 0)} мс, {info.get('kb', '?')} КБ{newest}")
            for ident, lot in list(found.items())[:3]:
                print(f"   {ident} · {lot['title'][:45]} · {lot['price']} {lot.get('label', '')}")


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
        race.stop_reason = "Ctrl+C"
        print(race.report())


if __name__ == "__main__":
    main()
