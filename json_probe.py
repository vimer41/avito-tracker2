"""
One-off check for the admin, /jsonprobe: can a lighter answer than the search page give the same lots,
and does Avito block it? A few requests through the server and the best proxies; scanning is untouched.

For every channel: the search page the usual way (anti-bot and redirects as in a scan), the same page once more
raw (its compressed size and first byte), then up to five JSON candidates in the same session:
    - the address the admin found in the browser (optional second link of the command);
    - the page's own data route (Remix `?_data=<route>`, React Router `<path>.data`): the SSR data comes from
      `loaderData`, so the site may serve the same data without the HTML;
    - Avito's mobile API with the location of the page's lots;
    - an API path written in the page itself.
Each answer is compared with the page: status, block or challenge, time, size, how many of the page's lots it
holds, which lot fields it has. JSON probe answers are not fed into the channel's health: a probe must not
make a good proxy look bad to the scanner.
"""

from __future__ import annotations

import asyncio
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import orjson

import config
from parser import AvitoParser, _default_headers, _response_size, _response_ttfb
from runtime_metrics import short_channel

PROBE_LANE = 9  # a session of its own on every channel: the probe never shares one with a running scan
PROBE_CHANNELS = 6
PROBE_TIMEOUT_S = 150.0
PAGE_DEADLINE_S = 25.0
RATE_WAIT_S = 15.0
CANDIDATES_MAX = 5
COMPARE_LOTS = 20
SAMPLE_CHARS = 300
FRESH_WINDOW_S = 365 * 86400

ID_RE = re.compile(r"(?<![\d.])(\d{9,11})(?![\d.])")
ROUTE_ID_RE = re.compile(r'"(?:id|routeId)"\s*:\s*"(routes/[^"\\]{1,120})"')
API_PATH_RE = re.compile(r'"(/(?:web|api|js)/\d+/[A-Za-z0-9_\-/]{2,80})"')
LOCATION_RE = re.compile(r'"locationId"\s*:\s*(\d{3,9})')
CATEGORY_RE = re.compile(r'"categoryId"\s*:\s*(\d{1,6})')
LOT_LOCATION_RE = re.compile(r'"locationId"\s*:\s*(\d{3,9})|"location"\s*:\s*\{\s*"id"\s*:\s*(\d{3,9})')
# The mobile site's public client key, as published in open Avito parsers; the probe shows whether it still works.
MOBILE_API_KEY = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"
ITEM_FIELDS = {
    "название": ("title",),
    "цена": ("price", "priceDetailed", "priceFormatted"),
    "время": ("time", "sortTimeStamp", "timestamp", "date"),
    "фото": ("images", "image", "imagesCount", "gallery"),
    "место": ("location", "geo", "address", "coords"),
    "продавец": ("user", "seller", "sellerName", "userType", "sellerInfo"),
    "ссылка": ("uri", "url", "urlPath", "uri_mweb"),
    "реклама": ("isPromotion", "vas", "isVip", "isHighlighted"),
}
TIME_KEYS = ("sortTimeStamp", "time", "timestamp")


@dataclass
class Candidate:
    name: str
    url: str


@dataclass
class Probe:
    channel: str
    name: str
    status: Optional[int] = None
    ms: Optional[int] = None
    ttfb_ms: Optional[int] = None
    kb: Optional[float] = None  # as received (compressed)
    body_kb: Optional[float] = None  # decoded
    content_type: str = ""
    blocked: bool = False
    json_ok: bool = False
    matched: int = 0
    of: int = 0
    items: int = 0
    fields: dict = field(default_factory=dict)
    item_keys: list = field(default_factory=list)
    newest_age_s: Optional[int] = None
    title_hits: int = 0  # lots whose title has a word of the query: is the query applied at all
    result_keys: list = field(default_factory=list)
    time_paths: list = field(default_factory=list)
    first_item: str = ""
    error: str = ""
    sample: str = ""

    @property
    def answered(self) -> bool:
        return self.status == 200 and not self.blocked and not self.error


# ── What the page itself tells ─────────────────────────────────────────────

def lot_location(html: str, page_ids: Iterable[str]) -> tuple[Optional[str], int]:
    """The city of the page's own lots: the locationId written next to each lot. The page as a whole names other
    places too (Moscow 637640 led the votes on a Kostroma page, so the API was asked about Moscow)."""
    votes: Counter = Counter()
    for lot in page_ids:
        match = re.search(r'"id"\s*:\s*"?%s"?' % re.escape(str(lot)), html)
        found = LOT_LOCATION_RE.search(html, match.end(), match.end() + 3000) if match else None
        if found:
            votes[found.group(1) or found.group(2)] += 1
    return votes.most_common(1)[0] if votes else (None, 0)


def page_hints(html: str, page_ids: Iterable[str] = ()) -> dict:
    routes = list(dict.fromkeys(ROUTE_ID_RE.findall(html)))
    api_paths = sorted({path for path in API_PATH_RE.findall(html) if not path.startswith(("/web/1/u", "/web/2/ft"))})
    locations = Counter(LOCATION_RE.findall(html))
    categories = Counter(CATEGORY_RE.findall(html))
    by_lots, lot_votes = lot_location(html, page_ids)
    return {
        "routes": routes[:12],
        "api_paths": api_paths[:20],
        "location_id": by_lots or (locations.most_common(1)[0][0] if locations else None),
        "location_votes": lot_votes if by_lots else (locations.most_common(1)[0][1] if locations else 0),
        "location_source": "по лотам" if by_lots else "по всей странице, город может быть не тот",
        "category_id": categories.most_common(1)[0][0] if categories else None,
        "react_router": "__reactRouterContext" in html,
        "remix": "__remixContext" in html,
    }


def _with_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = urlencode([*parse_qsl(parts.query, keep_blank_values=True), (key, value)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def candidates(search_url: str, hints: dict, user_url: Optional[str], query_url: Optional[str] = None) -> list[Candidate]:
    """JSON addresses to try. `search_url` is the page's canonical address; the query words come from the
    radar's own link (`query_url`): Avito may move «iphone» into a category path and drop `q` (06.11)."""
    parts = urlsplit(search_url)
    query = {**dict(parse_qsl(parts.query)), **dict(parse_qsl(urlsplit(query_url or search_url).query))}
    found: list[Candidate] = []
    if user_url:
        found.append(Candidate("ваш адрес", user_url))
    for route in reversed(hints["routes"][-2:]):  # the catalog is usually the deepest (last) route
        found.append(Candidate(f"данные маршрута {route}", _with_param(search_url, "_data", route)))
    # `<path>.data` (React Router) answered 404 on every channel on 06.11: Avito is not that kind of app.
    if hints.get("location_id"):
        params = {"key": MOBILE_API_KEY, "locationId": hints["location_id"], "sort": "date",
                  "page": 1, "display": "list", "limit": 30}
        if query.get("q"):
            params["query"] = query["q"]
            found.append(Candidate("мобильный API /api/11/items", "https://m.avito.ru/api/11/items?" + urlencode(params)))
        if hints.get("category_id"):
            with_category = dict(params, categoryId=hints["category_id"])
            found.append(Candidate("мобильный API 11 + категория", "https://m.avito.ru/api/11/items?" + urlencode(with_category)))
        if query.get("q"):
            found.append(Candidate("мобильный API /api/9/items", "https://m.avito.ru/api/9/items?" + urlencode(params)))
    item_paths = [path for path in hints["api_paths"] if "item" in path or "search" in path]
    if item_paths:
        found.append(Candidate(f"API со страницы {item_paths[0]}", f"https://www.avito.ru{item_paths[0]}?{parts.query}"))
    return found[:CANDIDATES_MAX]


def why_no_candidates(hints: dict, query_url: str) -> str:
    missing = []
    if not hints.get("location_id"):
        missing.append("город (locationId) не нашёлся рядом с лотами")
    if not dict(parse_qsl(urlsplit(query_url).query)).get("q") and not hints.get("category_id"):
        missing.append("в ссылке нет слова запроса (q), и категория не нашлась")
    if not hints.get("routes") and not hints.get("api_paths"):
        missing.append("на странице нет маршрутов данных и путей API")
    return "; ".join(missing) or "причина не ясна"


# ── What an answer holds ──────────────────────────────────────────────────

def _item_dicts(node: Any, depth: int = 0) -> Iterable[dict]:
    """Dicts that look like lots: a 9+ digit id and a title, at any depth."""
    if depth > 14:
        return
    if isinstance(node, dict):
        ident = node.get("id")
        if "title" in node and ((isinstance(ident, int) and ident >= 10 ** 8) or (isinstance(ident, str) and ident.isdigit() and len(ident) >= 9)):
            yield node
            return
        for value in node.values():
            yield from _item_dicts(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _item_dicts(value, depth + 1)


def _epoch(value: Any) -> Optional[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    seconds = value / 1000.0 if value > 10 ** 11 else float(value)
    now = time.time()
    return seconds if now - FRESH_WINDOW_S <= seconds <= now + 86400 else None


def analyze_json(data: Any) -> tuple[int, dict, list[str], Optional[float]]:
    """(lots, field label -> present, the lots' most common keys, newest lot time)."""
    items = list(_item_dicts(data))
    keys = Counter(key for item in items for key in item)
    fields = {label: any(alias in keys for alias in aliases) for label, aliases in ITEM_FIELDS.items()}
    times = [stamp for item in items for key in TIME_KEYS if (stamp := _epoch(item.get(key))) is not None]
    return len(items), fields, [key for key, _ in keys.most_common(25)], max(times) if times else None


def query_stems(search_url: str) -> list[str]:
    """Word stems of the search query: «конская упряжь» -> ["конск", "упряж"]."""
    words = re.findall(r"\w+", dict(parse_qsl(urlsplit(search_url).query)).get("q", "").lower())
    return [word[:5] for word in words if len(word) >= 3]


def _time_paths(item: dict, prefix: str = "", depth: int = 0) -> list[str]:
    """Fields that look like a time (epoch seconds/ms in the last year, or a key named like one), with a value."""
    found = []
    for key, value in item.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and depth < 3:
            found += _time_paths(value, path + ".", depth + 1)
        elif _epoch(value) is not None or (any(mark in key.lower() for mark in ("time", "date", "stamp")) and value not in (None, "", [])):
            found.append(f"{path}={str(value)[:40]}")
    return found[:8]


def describe_json(data: Any, stems: list[str]) -> dict:
    """Everything a probe report says about one JSON answer."""
    lots, fields, keys, newest = analyze_json(data)
    items = list(_item_dicts(data))
    titles = [str(item.get("title") or "").lower() for item in items]
    result = data.get("result") if isinstance(data, dict) and isinstance(data.get("result"), dict) else data
    return {
        "items": lots, "fields": fields, "keys": keys, "newest": newest,
        "title_hits": sum(1 for title in titles if stems and any(stem in title for stem in stems)),
        "result_keys": list(result)[:25] if isinstance(result, dict) else [],
        "time_paths": _time_paths(items[0]) if items else [],
        "first_item": orjson.dumps(items[0]).decode("utf-8", "replace")[:700] if items else "",
    }


def _json_headers(url: str, referer: str) -> dict:
    same_site = urlsplit(url).netloc == urlsplit(referer).netloc
    return {
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin" if same_site else "same-site",
    }


# ── Requests ──────────────────────────────────────────────────────────────

async def _raw(client, session, channel: str, name: str, url: str, headers: dict, page_ids: list[str],
               stems: Optional[list[str]] = None) -> Probe:
    result = Probe(channel, name, of=len(page_ids))
    try:
        await client.wait_for_rate_slot(deadline=time.monotonic() + RATE_WAIT_S)
    except TimeoutError:
        result.error = "канал занят лимитом частоты"
        return result
    started = time.monotonic()
    try:
        response = await session.request(
            "GET", url, headers=headers, allow_redirects=False,
            timeout=(config.AVITO_CONNECT_TIMEOUT, config.AVITO_READ_TIMEOUT),
        )
    except Exception as exc:  # a probe reports, it never raises
        result.ms = int((time.monotonic() - started) * 1000)
        result.error = f"{type(exc).__name__}: {exc}"[:120]
        return result
    result.ms = int((time.monotonic() - started) * 1000)
    result.status = response.status_code
    ttfb = _response_ttfb(response)
    result.ttfb_ms = int(ttfb * 1000) if ttfb else None
    result.kb = round(_response_size(response) / 1024, 1)
    text = getattr(response, "text", "") or ""
    result.body_kb = round(len(text.encode("utf-8", "replace")) / 1024, 1)
    result.content_type = str((getattr(response, "headers", None) or {}).get("content-type", ""))[:60]
    result.blocked = AvitoParser._is_firewall(response)
    found_ids = set(ID_RE.findall(text))
    result.matched = len(found_ids & set(page_ids))
    result.sample = re.sub(r"\s+", " ", text[:SAMPLE_CHARS])
    try:
        data = orjson.loads(text)
    except (orjson.JSONDecodeError, TypeError, ValueError):
        return result
    result.json_ok = True
    described = describe_json(data, stems or [])
    result.items, result.fields, result.item_keys = described["items"], described["fields"], described["keys"]
    result.title_hits, result.result_keys = described["title_hits"], described["result_keys"]
    result.time_paths, result.first_item = described["time_paths"], described["first_item"]
    result.newest_age_s = int(time.time() - described["newest"]) if described["newest"] else None
    return result


@dataclass
class PageResult:
    client: Any
    channel: str
    parser: Any
    probe: Probe
    html: Optional[str] = None
    page_ids: list = field(default_factory=list)
    canonical: str = ""


async def load_page(client, search_url: str) -> PageResult:
    """Phase 1: the search page as a scan gets it (anti-bot and redirects included)."""
    channel = short_channel(client.proxy_id, client.masked_url)
    parser = AvitoParser(proxy_client=client, lane=PROBE_LANE)
    started = time.monotonic()
    page = Probe(channel, "страница, как в скане")
    result = PageResult(client, channel, parser, page)
    try:
        html = await parser.fetch_page_async(search_url, deadline=time.monotonic() + PAGE_DEADLINE_S)
    except Exception as exc:  # a probe reports, it never raises
        page.ms, page.error = int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {exc}"[:120]
        return result
    page.ms = int((time.monotonic() - started) * 1000)
    if html is None:
        failure = parser.last_failure
        page.error = failure.kind if failure else "unknown"
        page.blocked = bool(failure and failure.kind in ("forbidden", "rate_limit", "challenge", "cooldown"))
        return result
    lots = AvitoParser.parse_catalog(html) or []
    result.page_ids = [lot.item_id for lot in lots if not lot.is_promotion][:COMPARE_LOTS]
    page.status, page.items, page.matched, page.of = 200, len(lots), len(result.page_ids), len(result.page_ids)
    page.body_kb = round(len(html.encode("utf-8", "replace")) / 1024, 1)
    stamps = [stamp for lot in lots if (stamp := _epoch(lot.timestamp)) is not None]
    page.newest_age_s = int(time.time() - max(stamps)) if stamps else None
    result.html, result.canonical = html, parser._canonical_cache.get(search_url, search_url)
    return result


def _fresh_session(client):
    """A session for a channel whose page failed: its JSON answers are still worth seeing."""
    from curl_cffi import requests as curl_requests
    from anti_bot.manager import BROWSER_PROFILE
    from parser import PAGE_CURL_INFOS

    proxies = {"http": client.proxy_url, "https": client.proxy_url} if client.proxy_url else None
    return curl_requests.AsyncSession(impersonate=BROWSER_PROFILE, headers=_default_headers(), proxies=proxies,
                                      max_clients=1, curl_infos=PAGE_CURL_INFOS)


async def probe_json(page: PageResult, reference: PageResult, found: list[Candidate], stems: list[str]) -> list[Probe]:
    """Phase 2 on one channel: the page once more raw (only where it loaded) and every JSON candidate,
    compared with the reference page, even on a channel whose own page was blocked."""
    client, results = page.client, [page.probe]
    lane_sessions = getattr(client, "lane_sessions", None)
    session = lane_sessions().get(PROBE_LANE) if callable(lane_sessions) else None
    own = None
    try:
        if session is None:
            own = session = _fresh_session(client)
        if page.html is not None:
            headers = dict(_default_headers(), Referer=reference.canonical)
            results.append(await _raw(client, session, page.channel, "страница, замер", page.canonical, headers, page.page_ids))
        for candidate in found:
            results.append(await _raw(
                client, session, page.channel, candidate.name, candidate.url,
                _json_headers(candidate.url, reference.canonical), reference.page_ids, stems,
            ))
        return results
    finally:
        if own is not None:
            try:
                await own.close()
            except Exception:
                pass
        await client.reset_async_session(PROBE_LANE)
        try:
            page.parser.close()
        except Exception:
            pass


def pick_channels(pool, count: int = PROBE_CHANNELS) -> list:
    """The server and the proxies with the best record that are not resting after a block. A channel busy with
    a scan still counts: the probe has a session of its own."""
    usable = [c for c in pool.clients if not getattr(c, "_draining", False) and c.state != "quarantined"]
    direct = [c for c in usable if c.is_direct]
    proxies = sorted((c for c in usable if not c.is_direct), key=lambda c: -c.long_health())
    return (direct[:1] + proxies)[:count]


async def run_probe(search_url: str, user_url: Optional[str] = None, pool=None) -> str:
    from proxy_pool import get_proxy_pool

    pool = pool or get_proxy_pool()
    search_url = AvitoParser.normalize_url(search_url)
    channels = pick_channels(pool)
    if not channels:
        return build_report(search_url, user_url, [], None)

    async def both_phases() -> tuple[list[list[Probe]], Optional[dict]]:
        pages = await asyncio.gather(*(load_page(client, search_url) for client in channels))
        reference = next((page for page in pages if page.html is not None), None)
        if reference is None:
            for page in pages:
                await page.client.reset_async_session(PROBE_LANE)
                page.parser.close()
            return [[page.probe] for page in pages], None
        hints = page_hints(reference.html, reference.page_ids)
        found = candidates(reference.canonical, hints, user_url, query_url=search_url)
        hints["candidates"] = [candidate.name for candidate in found]
        hints["no_candidates"] = "" if found else why_no_candidates(hints, search_url)
        stems = query_stems(search_url)
        outcomes = await asyncio.gather(*(probe_json(page, reference, found, stems) for page in pages),
                                        return_exceptions=True)
        per_channel = [
            [page.probe, Probe(page.channel, "проверка JSON", error=f"{type(outcome).__name__}: {outcome}"[:120])]
            if isinstance(outcome, BaseException) else outcome
            for page, outcome in zip(pages, outcomes)
        ]
        return per_channel, hints

    per_channel, hints = await asyncio.wait_for(both_phases(), PROBE_TIMEOUT_S)
    return build_report(search_url, user_url, per_channel, hints)


# ── Report ────────────────────────────────────────────────────────────────

def _median(values: list[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def _verdict(probes: list[Probe]) -> str:
    answered = [p for p in probes if p.answered]
    if not answered:
        if any(p.blocked for p in probes):
            return "❌ блок или капча"
        statuses = Counter(str(p.status) if p.status else (p.error.split(":")[0] or "нет ответа") for p in probes)
        return f"❌ не отдаёт ({', '.join(f'{key}×{count}' for key, count in statuses.most_common(3))})"
    best = max(answered, key=lambda p: p.matched)
    share = best.matched / best.of if best.of else 0.0
    if share < 0.5:
        return f"⚠️ отвечает, но лоты другие ({best.matched} из {best.of})"
    if len(answered) < len(probes):
        return f"⚠️ отдаёт те же лоты, но не на всех каналах ({len(answered)} из {len(probes)})"
    return f"✅ те же лоты ({best.matched} из {best.of}) на всех каналах"


def _summary(per_channel: list[list[Probe]]) -> list[str]:
    by_name: dict[str, list[Probe]] = {}
    for probes in per_channel:
        for probe in probes:
            by_name.setdefault(probe.name, []).append(probe)
    page = [p for p in by_name.get("страница, замер", []) if p.answered]
    page_kb, page_ms = _median([p.kb for p in page if p.kb]), _median([p.ms for p in page if p.ms])
    lines = []
    for name, probes in by_name.items():
        if name == "страница, как в скане":
            continue
        answered = [p for p in probes if p.answered]
        kb, ms = _median([p.kb for p in answered if p.kb]), _median([p.ms for p in answered if p.ms])
        compare = ""
        if name != "страница, замер" and kb and page_kb and ms and page_ms:
            compare = f" · {kb:.0f} КБ против {page_kb:.0f} у страницы, {ms / 1000:.1f} с против {page_ms / 1000:.1f}"
        missing = sorted({label for p in answered if p.json_ok for label, ok in p.fields.items() if not ok})
        fields = f" · нет полей: {', '.join(missing)}" if missing else ""
        best = max((p for p in answered if p.json_ok), key=lambda p: p.items, default=None)
        words = f" · слова запроса в названиях: {best.title_hits} из {best.items}" if best and best.items else ""
        lines.append(f"- {name}: {_verdict(probes)}{compare}{words}{fields}")
    return lines


def _row(p: Probe) -> str:
    def value(number, suffix=""):
        return f"{number:g}{suffix}" if isinstance(number, (int, float)) else "—"

    return (
        f"{p.channel} · {p.name} · {p.status or '—'}{' блок' if p.blocked else ''} · {value(p.ms, ' мс')} · "
        f"{value(p.ttfb_ms, ' мс')} · {value(p.kb)} · {value(p.body_kb)} · {p.matched}/{p.of} · "
        f"{'JSON, ' + str(p.items) + ' лотов' if p.json_ok else (f'{p.items} лотов, обычных {p.of}' if p.name == 'страница, как в скане' and p.status == 200 else (p.content_type or '—'))}"
        f"{' · свежий лот ' + str(p.newest_age_s // 60) + ' мин назад' if p.newest_age_s is not None else ''}"
        f"{' · ' + p.error if p.error else ''}"
    )


def build_report(search_url: str, user_url: Optional[str], per_channel: list[list[Probe]], hints: Optional[dict]) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 3 * 3600))
    lines = [f"# Avito Radar: проверка JSON, {stamp} МСК", f"Поиск: {search_url}", f"Свой адрес: {user_url or 'нет'}", ""]
    if not per_channel:
        lines.append("Нет доступных каналов: все на паузе или пул пуст.")
        return "\n".join(lines)
    lines += ["## Итог", *(_summary(per_channel) or ["нет ответов"]), ""]
    lines.append("## Что нашлось на странице")
    if hints:
        lines += [
            f"маршруты данных: {', '.join(hints['routes']) or 'нет'}",
            f"пути API: {', '.join(hints['api_paths']) or 'нет'}",
            f"город (locationId): {hints['location_id'] or 'нет'}, {hints.get('location_source', '')} ({hints['location_votes']})",
            f"Remix: {'да' if hints['remix'] else 'нет'} · React Router: {'да' if hints['react_router'] else 'нет'}",
            f"варианты JSON: {', '.join(hints.get('candidates') or []) or 'не построены: ' + hints.get('no_candidates', '')}",
        ]
    else:
        lines.append("страница не получена ни на одном канале")
    lines += ["", "## По каналам", "канал · способ · ответ · время · до 1-го байта · КБ сжато · КБ всего · лотов со страницы · что внутри"]
    lines += [_row(probe) for probes in per_channel for probe in probes]
    keys: dict[str, list[str]] = {}
    samples: dict[str, str] = {}
    for probes in per_channel:
        for probe in probes:
            if probe.json_ok and probe.item_keys and probe.name not in keys:
                keys[probe.name] = probe.item_keys
            if probe.sample and probe.name not in samples and not probe.name.startswith("страница"):
                samples[probe.name] = probe.sample
    if keys:
        lines += ["", "## Поля лотов в JSON", *(f"{name}: {', '.join(found)}" for name, found in keys.items())]
    shapes = {}
    for probes in per_channel:
        for probe in probes:
            if probe.json_ok and probe.name not in shapes:
                shapes[probe.name] = probe
    for name, probe in shapes.items():
        lines += ["", f"## Устройство ответа: {name}",
                  f"ключи ответа: {', '.join(probe.result_keys) or '—'}",
                  f"поля времени у лота: {'; '.join(probe.time_paths) or 'не нашлось'}",
                  f"первый лот: {probe.first_item or '—'}"]
    if samples:
        lines += ["", f"## Начало ответов ({SAMPLE_CHARS} символов)", *(f"{name}: {text}" for name, text in samples.items())]
    return "\n".join(lines)
