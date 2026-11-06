"""Small shared snapshots and single-flight calibration for identical sources."""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass

import config
from collector import collect_source
from parser import AvitoParser, AvitoItem, lane_parser
from proxy_pool import get_proxy_pool


# Answers about the page itself. Blocks and timeouts prove nothing, and invalid_redirect also hits working links.
BAD_LINK_KINDS = frozenset({"catalog_missing", "http_client"})


class BadSourceLink(Exception):
    """Every channel reached Avito and got no catalog: the link is wrong, not the network."""


@dataclass
class SourceSnapshot:
    title: str
    items: tuple[AvitoItem, ...]
    captured_at: float


class SourceSnapshotCache:
    def __init__(
        self,
        maxsize: int = config.SOURCE_SNAPSHOT_CACHE_SIZE,
        ttl: float = config.SOURCE_SNAPSHOT_TTL,
    ):
        self.maxsize, self.ttl = maxsize, ttl
        self._data = OrderedDict()
        self._pending: dict[str, asyncio.Task] = {}

    def get(self, key: str):
        entry = self._data.get(key)
        if entry is None:
            return None
        snapshot, saved_at = entry
        if time.monotonic() - saved_at > self.ttl:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return snapshot

    def put(self, key: str, snapshot: SourceSnapshot):
        self._data[key] = snapshot, time.monotonic()
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    async def load(self, key: str):
        cached = self.get(key)
        if cached is not None:
            return cached
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch(key))
            self._pending[key] = task
            task.add_done_callback(lambda completed: self._finish(key, completed))
        # A cancelled UI request must not cancel calibration for another user.
        return await asyncio.shield(task)

    def _finish(self, key: str, task: asyncio.Task):
        if self._pending.get(key) is task:
            del self._pending[key]
        if not task.cancelled():
            task.exception()  # Retrieve exceptions even when all UI callers cancelled.

    async def _fetch(self, key: str):
        pool = get_proxy_pool()
        excluded: set[str] = set()
        failed_attempts = bad_pages = 0
        bad_kind = ""
        for _ in range(max(1, int(config.SCAN_PROXY_ATTEMPTS))):
            lease = pool.acquire_client(exclude_ids=excluded)
            if lease is None and len(pool.clients) == 1:
                # Single client (e.g. direct mode or 1 proxy): clear quarantine/excluded and retry
                only_client = pool.clients[0]
                only_client.quarantined_until = 0.0
                only_client._state = "healthy"
                only_client._half_open_probe_active = False
                lease = pool.acquire_client()
            if lease is None:
                break
            client = lease.client
            excluded.add(client.proxy_id)
            parser = lane_parser(client, getattr(lease, "lane", 0), AvitoParser)
            try:
                result = await collect_source(parser, key, max_age_minutes=30, max_pages=1)
                if result is not None:
                    snapshot = SourceSnapshot(result.title, result.items, result.captured_at)
                    self.put(key, snapshot)
                    return snapshot
                failed_attempts += 1
                failure = parser.last_failure
                if failure is not None and failure.kind in BAD_LINK_KINDS:
                    bad_pages += 1
                    bad_kind = failure.kind
            finally:
                lease.release()

        if failed_attempts and bad_pages == failed_attempts:
            raise BadSourceLink(bad_kind)

        # Fallback for direct/legacy mode when pool has no clients
        parser = None
        try:
            parser = AvitoParser(proxy=config.PROXY_URL)
            result = await collect_source(parser, key, max_age_minutes=30, max_pages=1)
            if result is None:
                return None
            snapshot = SourceSnapshot(result.title, result.items, result.captured_at)
            self.put(key, snapshot)
            return snapshot
        finally:
            if parser is not None:
                parser.close()

    async def close(self):
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


SOURCE_SNAPSHOTS = SourceSnapshotCache()
