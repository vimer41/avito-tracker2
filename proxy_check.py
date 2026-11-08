"""
Live probe of one network channel (a proxy or the server's own IP) against a real Avito search.

The probe goes through exactly the production scan path (ProxyClient pacing, curl_cffi
impersonation, anti-bot challenge handling, SSR catalog parsing), so its result predicts
how radars will behave on that channel.
"""

from __future__ import annotations

import asyncio
import ipaddress
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

from curl_cffi import requests as curl_requests

from collector import collect_source
from parser import AvitoParser
from proxy_pool import ProxyClient, compute_proxy_id

DEFAULT_PROBE_URL = "https://www.avito.ru/moskva?q=iphone&s=104"
EXIT_IP_URL = "https://api.ipify.org"

# proxy_id -> public IP the channel exits from. Two "proxies" with one exit IP are one IP for Avito.
EXIT_IPS: dict[str, str] = {}


@dataclass
class ProbeAttempt:
    ok: bool
    elapsed_ms: int
    items: int = 0
    failure: str = ""
    status: Optional[int] = None


@dataclass
class ProbeResult:
    proxy_id: str
    masked_url: str
    target_url: str
    attempts: list[ProbeAttempt] = field(default_factory=list)

    @property
    def successes(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.ok)

    @property
    def median_ok_ms(self) -> Optional[int]:
        times = [attempt.elapsed_ms for attempt in self.attempts if attempt.ok]
        return int(statistics.median(times)) if times else None


async def detect_exit_ip(proxy_url: Optional[str], timeout: float = 8.0) -> Optional[str]:
    """Public IP seen by websites through this channel (None if it cannot be determined)."""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        async with curl_requests.AsyncSession(proxies=proxies, timeout=timeout) as session:
            response = await session.get(EXIT_IP_URL)
        value = (response.text or "").strip()
        ipaddress.ip_address(value)
    except Exception:
        return None
    EXIT_IPS[compute_proxy_id(proxy_url)] = value
    return value


async def exit_ips_for(proxy_urls: list[Optional[str]], concurrency: int = 5) -> dict[Optional[str], Optional[str]]:
    """Exit IPs for many channels at once, reusing values already learned in this process."""
    gate = asyncio.Semaphore(concurrency)

    async def one(url: Optional[str]) -> Optional[str]:
        cached = EXIT_IPS.get(compute_proxy_id(url))
        if cached:
            return cached
        async with gate:
            return await detect_exit_ip(url)

    values = await asyncio.gather(*(one(url) for url in proxy_urls))
    return dict(zip(proxy_urls, values))


async def probe_channel(
    proxy_url: Optional[str],
    target_url: Optional[str] = None,
    attempts: int = 3,
    *,
    deadline_seconds: Optional[float] = None,
    stop_on_failure: bool = False,
) -> ProbeResult:
    """Run `attempts` back-to-back single-page scans through a throwaway client for this channel.
    deadline_seconds gives each scan longer than a live one; stop_on_failure ends at the first failed scan
    (a blocked throwaway client would only answer «cooldown» to the next one)."""
    target = AvitoParser.normalize_url(target_url or DEFAULT_PROBE_URL)
    client = ProxyClient(proxy_url)
    parser = AvitoParser(proxy_client=client)
    result = ProbeResult(proxy_id=client.proxy_id, masked_url=client.masked_url, target_url=target)
    try:
        for _ in range(max(1, attempts)):
            started = time.monotonic()
            collection = await collect_source(parser, target, max_pages=1, deadline_seconds=deadline_seconds)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if collection is not None:
                result.attempts.append(ProbeAttempt(True, elapsed_ms, items=len(collection.items)))
                continue
            failure = parser.last_failure
            result.attempts.append(ProbeAttempt(
                False,
                elapsed_ms,
                failure=failure.kind if failure else "unknown",
                status=failure.status if failure else None,
            ))
            if stop_on_failure:
                break
    finally:
        parser.close()
        await client.close()
    return result
