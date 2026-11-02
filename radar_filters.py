"""Per-radar lot filters, the market price estimate and the seller message template.

Everything here works on lots the scan already has: no I/O and no extra Avito requests. These run
on the scan path, so each stays at a few comparisons per lot.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

MUST_WORDS_LIMIT = 20
WORD_MAX_LEN = 40
REPLY_TEMPLATE_MAX_LEN = 200
REPLY_COPY_LIMIT = 256  # Bot API limit of CopyTextButton.text

MARKET_MIN_PRICES = 12     # fewer lots say nothing about the market
MARKET_MAX_SPREAD = 3.0    # p75 / p25: a wider spread means a broad radar mixing unlike goods
MARKET_MIN_PERCENT = 10    # a smaller gap is noise
MARKET_MAX_PERCENT = 70    # a deeper one is usually another product: a case in a phone search

CLEAR_WORDS = frozenset({"0", "-", "нет", "сброс", "убрать"})
_PRICE_GROUP_RE = re.compile(r"(?<=\d)[ .](?=\d{3}\b)")
_PRICE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(млн|лям\w*|m\b|м\b|тыс\w*|т\b|к\b|k\b)?")
PRICE_MAX = 10_000_000_000
TITLE_WORD_RE = re.compile(r"[\w\d]+")


def parse_words(raw: Any) -> list[str]:
    """Lowercase words from the JSON column or from a list."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else []
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    return [word for word in (str(w).strip().lower() for w in raw) if word]


def split_words(text: str) -> list[str]:
    """Comma-separated input of the user, without duplicates. Keeps the order."""
    words: list[str] = []
    for word in (text or "").split(","):
        word = word.strip().lower()[:WORD_MAX_LEN]
        if word and word not in words:
            words.append(word)
    return words[:MUST_WORDS_LIMIT]


def parse_price_range(text: str) -> Optional[tuple[int, int]]:
    """(min, max) from «10000-50000», «от 10 000», «до 50 000»; 0 means no bound. None when unclear."""
    value = (text or "").lower().replace("\xa0", " ").replace("₽", "").replace("руб", "").strip(" .")
    if value in CLEAR_WORDS:
        return 0, 0
    tokens = _PRICE_RE.findall(_PRICE_GROUP_RE.sub("", value))
    if not tokens or len(tokens) > 2:
        return None
    numbers = [float(number.replace(",", ".")) for number, _unit in tokens]
    scales = [_price_scale(unit) for _number, unit in tokens]
    # «10-50 тыс»: the unit is written once, after the second number.
    if len(tokens) == 2 and not tokens[0][1] and numbers[0] <= numbers[1]:
        scales[0] = scales[1]
    prices = [round(number * scale) for number, scale in zip(numbers, scales)]
    if max(prices) > PRICE_MAX:
        return None
    if len(prices) == 2:
        low, high = sorted(prices)
        return low, high
    return (0, prices[0]) if "до" in value else (prices[0], 0)


def _price_scale(unit: str) -> int:
    if not unit:
        return 1
    return 1_000_000 if unit[0] in "мmл" else 1000


def format_rub(value: int) -> str:
    return f"{int(value):,} ₽".replace(",", " ")


def price_label(price_min: int, price_max: int) -> str:
    if price_min and price_max:
        return f"{format_rub(price_min)} – {format_rub(price_max)}"
    if price_min:
        return f"от {format_rub(price_min)}"
    if price_max:
        return f"до {format_rub(price_max)}"
    return "любая"


def filter_reason(item: Any, radar: dict) -> Optional[str]:
    """Which filter of this radar drops the lot, None when the lot passes.

    A lot without a price passes the price filter: losing a real lot costs more than one extra message.
    """
    price = item.raw_price or 0
    price_min = int(radar.get("price_min") or 0)
    price_max = int(radar.get("price_max") or 0)
    if price and ((price_min and price < price_min) or (price_max and price > price_max)):
        return "price"
    if radar.get("delivery_only") and not item.delivery:
        return "delivery"
    if radar.get("hide_promoted") and item.is_promotion:
        return "promoted"
    words = parse_words(radar.get("must_words"))
    if words:
        text = f"{item.title} {item.description or ''}".lower()
        if not any(word in text for word in words):
            return "must_words"
    return None


def seller_token(seller_key: str) -> str:
    """Blacklist entry of a seller known by id. Prefixed, so it can never equal a seller's name."""
    return f"key:{seller_key}"


def seller_is_blocked(item: Any, blocked: Iterable[str]) -> bool:
    """A seller name is not unique, so a name only counts for old entries saved without an id."""
    key = getattr(item, "seller_key", None)
    if key and seller_token(key) in blocked:
        return True
    name = (item.seller_name or "").lower().strip()
    return bool(name) and name in blocked


def title_matches(title: str, query: str) -> bool:
    """Every word of the query is in the title. The last two letters are ignored: «кроссовки» finds «кроссовок»."""
    text = (title or "").lower()
    words = [word for word in TITLE_WORD_RE.findall((query or "").lower()) if len(word) >= 2]
    return all((word if len(word) <= 4 or word.isdigit() else word[:-2]) in text for word in words)


def summary(radar: dict) -> str:
    """One line for the radar card."""
    parts = []
    price_min = int(radar.get("price_min") or 0)
    price_max = int(radar.get("price_max") or 0)
    if price_min or price_max:
        parts.append(f"цена {price_label(price_min, price_max)}")
    words = parse_words(radar.get("must_words"))
    if words:
        parts.append(f"обязательных слов: {len(words)}")
    if radar.get("delivery_only"):
        parts.append("с доставкой")
    if radar.get("hide_promoted"):
        parts.append("без продвинутых")
    return ", ".join(parts) or "нет"


def market_median(items: Iterable[Any]) -> Optional[int]:
    """Median price of the search page, None when the page does not describe one market."""
    prices = sorted(price for price in (item.raw_price for item in items) if price and price > 0)
    count = len(prices)
    if count < MARKET_MIN_PRICES:
        return None
    low, high = prices[count // 4], prices[count * 3 // 4]
    if high > low * MARKET_MAX_SPREAD:
        return None
    return prices[count // 2]


def mark_market(items: list[Any]) -> None:
    """Fill market_median and market_percent of the lots that are clearly cheaper than the page."""
    median = market_median(items)
    for item in items:
        item.market_median = item.market_percent = None
        if median and item.raw_price and item.raw_price > 0:
            percent = round((median - item.raw_price) * 100 / median)
            if MARKET_MIN_PERCENT <= percent <= MARKET_MAX_PERCENT:
                item.market_median = median
                item.market_percent = percent


def render_reply(template: Optional[str], item: Any) -> str:
    """Message for the seller; empty when the radar has no template."""
    text = (template or "").strip()
    if not text:
        return ""
    text = text.replace("{название}", item.title or "").replace("{цена}", item.price or "")
    return text.strip()[:REPLY_COPY_LIMIT]
