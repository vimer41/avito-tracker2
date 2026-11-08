"""Bounded exact-source collection; never broaden subscriber searches."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import config
from freshness import publication_age_seconds
from parser import AvitoItem, AvitoParser, FetchFailure, page_fingerprint

logger = logging.getLogger(__name__)


@dataclass
class SourceCollection:
    title: str
    items: tuple[AvitoItem, ...]
    captured_at: float
    pages: int
    coverage_gap: bool
    elapsed_ms: int
    discovery_times: dict[str, float] = field(default_factory=dict)
    top_ids: tuple[str, ...] = ()
    fast_skipped: bool = False
    channel: str = ""  # the egress that served this collection


def reached_old_boundary(items, max_age_minutes):
    organic = [item for item in items if not item.is_promotion]
    ages = [publication_age_seconds(item.date, item.timestamp) for item in organic]
    return bool(
        ages
        and all(age is not None for age in ages)
        and all(ages[index] <= ages[index + 1] for index in range(len(ages) - 1))
        and ages[-1] > max_age_minutes * 60
    )


def settled_item_ids(collection: "SourceCollection") -> list[str]:
    """
    IDs whose fanout outcome is final. Items without a reliable publication age are
    deliberately left out: fanout keeps re-evaluating them, so they must keep reaching it.
    """
    settled = []
    for item in collection.items:
        seen_at = collection.discovery_times.get(item.item_id, collection.captured_at)
        if publication_age_seconds(item.date, item.timestamp, now=seen_at) is not None:
            settled.append(item.item_id)
    return settled


def extract_top_ids(page_items: list[AvitoItem]) -> tuple[str, ...]:
    organic = [item.item_id for item in page_items if not getattr(item, "is_promotion", False)]
    if organic:
        return tuple(organic[:3])
    return tuple(item.item_id for item in page_items[:3])


async def collect_source(
    parser,
    url: str,
    max_age_minutes: int = 5,
    *,
    max_pages: Optional[int] = None,
    can_fast_skip: bool = False,
    last_top_ids: tuple[str, ...] = (),
    read_next_page: Optional[Callable[[list[AvitoItem]], Awaitable[bool]]] = None,
    deadline_seconds: Optional[float] = None,
) -> Optional[SourceCollection]:
    """
    read_next_page, when given, is asked after each page whether the next one is worth fetching
    (a broad radar flooded with new lots); without it every page up to max_pages is read.
    """
    started = time.monotonic()
    deadline = started + float(deadline_seconds or config.AVITO_FETCH_TIMEOUT)
    page_limit = max(1, int(max_pages if max_pages is not None else config.AVITO_MAX_PAGES_PER_SCAN))
    items_by_id = {}
    discovery_times = {}
    page_url = url
    visited = set()
    pages = 0
    title = ""
    gap = False
    top_ids: tuple[str, ...] = ()
    fast_skipped = False
    is_real_parser = type(parser) is AvitoParser

    while page_url and pages < page_limit:
        visited.add(page_url)
        html = await parser.fetch_page_async(page_url, deadline=deadline)
        captured_at = time.time()
        if not html:
            if pages == 0:
                return None
            gap = True
            break
        if is_real_parser:
            # One orjson pass over the SSR catalog; None means the page carries no catalog at all.
            catalog = parser.parse_catalog(html)
            catalog_missing = catalog is None
            page_items = catalog or []
        else:
            page_items = await asyncio.to_thread(parser.parse_items, html)
            catalog_missing = not page_items and not await asyncio.to_thread(parser.has_catalog, html)
        if catalog_missing:
            parser.last_failure = FetchFailure("catalog_missing", detail=page_fingerprint(html) if is_real_parser else "")
            if pages == 0:
                return None
            gap = True
            break
        pages += 1
        if not title:
            if is_real_parser:
                extracted_title = parser.extract_search_title(html, url)
            else:
                extracted_title = await asyncio.to_thread(parser.extract_search_title, html, url)
            title = extracted_title if isinstance(extracted_title, str) else "Поиск Авито"
        for item in page_items:
            discovery_times.setdefault(item.item_id, captured_at)
            items_by_id.setdefault(item.item_id, item)

        if pages == 1:
            top_ids = extract_top_ids(page_items)
            if can_fast_skip and last_top_ids and top_ids and top_ids == tuple(last_top_ids[:3]):
                fast_skipped = True
                gap = False
                break

        if reached_old_boundary(page_items, max_age_minutes):
            gap = False
            break
        if read_next_page is not None and pages < page_limit and not await read_next_page(page_items):
            gap = False  # the page already reaches lots seen before: nothing new is left further down
            break
        if is_real_parser:
            next_url = parser.next_page_url(html, page_url)
        else:
            next_url = await asyncio.to_thread(parser.next_page_url, html, page_url)
        if next_url is not None and not isinstance(next_url, str):
            next_url = None  # Non-network test doubles must not invent pagination.
        if next_url in visited:
            gap = True
            break
        page_url = next_url
        gap = bool(next_url)
        if time.monotonic() >= deadline:
            break

    elapsed_ms = int((time.monotonic() - started) * 1000)
    parser.last_coverage_gap = gap
    if gap:
        logger.debug(
            "Покрытие источника ограничено: pages=%s items=%s limit=%s; продолжение не проверено",
            pages,
            len(items_by_id),
            page_limit,
        )
    return SourceCollection(
        title=title,
        items=tuple(items_by_id.values()),
        captured_at=time.time(),
        pages=pages,
        coverage_gap=gap,
        elapsed_ms=elapsed_ms,
        discovery_times=discovery_times,
        top_ids=top_ids,
        fast_skipped=fast_skipped,
    )
