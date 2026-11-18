"""Platega API: create a payment, read its status, refund it (docs.platega.io)."""

import asyncio
import json
from typing import Any

import aiohttp

import config

BASE_URL = "https://app.platega.io"
TIMEOUT = aiohttp.ClientTimeout(total=20)
_SESSIONS: dict[int, tuple[asyncio.AbstractEventLoop, aiohttp.ClientSession]] = {}


def _get_session() -> aiohttp.ClientSession:
    loop = asyncio.get_running_loop()
    key = id(loop)
    # Clean closed loops/sessions so test suites running multiple loops don't retain stale entries.
    for old_key, (old_loop, old_sess) in list(_SESSIONS.items()):
        if old_loop.is_closed() or old_sess.closed:
            _SESSIONS.pop(old_key, None)
    entry = _SESSIONS.get(key)
    if entry is not None and entry[0] is loop and not entry[1].closed:
        return entry[1]
    session = aiohttp.ClientSession(timeout=TIMEOUT)
    _SESSIONS[key] = (loop, session)
    return session


async def close_session() -> None:
    for key, (loop, session) in list(_SESSIONS.items()):
        _SESSIONS.pop(key, None)
        if not session.closed and not loop.is_closed():
            try:
                await session.close()
            except Exception:
                pass


class PlategaError(Exception):
    """`status` is the HTTP code, `code` Platega's error code (REFUND_IN_PROGRESS, …) when the body had one."""

    def __init__(self, text: str, *, status: int | None = None, code: str = "", message: str = ""):
        super().__init__(text)
        self.status = status
        self.code = code
        self.message = message


async def _request(method: str, path: str, body: dict | None = None) -> dict[str, Any]:
    headers = {"X-MerchantId": config.PLATEGA_MERCHANT_ID, "X-Secret": config.PLATEGA_SECRET}
    try:
        session = _get_session()
        async with session.request(method, BASE_URL + path, json=body, headers=headers) as response:
            text = await response.text()
            if response.status != 200:
                try:
                    error = json.loads(text)
                except json.JSONDecodeError:
                    error = {}
                error = error if isinstance(error, dict) else {}
                raise PlategaError(f"HTTP {response.status}: {text[:300]}", status=response.status,
                                   code=str(error.get("code") or ""), message=str(error.get("message") or ""))
            return json.loads(text)
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as exc:
        raise PlategaError(f"{type(exc).__name__}: {exc}") from exc


# Коды способов оплаты Platega (PaymentMethodInt).
METHODS: dict[str, int] = {"sbp": 2, "card": 11, "crypto": 13}


async def create_payment(*, amount: int, description: str, order_id: int, user_id: int, return_url: str,
                         method: str | None = None) -> dict[str, Any]:
    """Returns transactionId, status, url (payment page) and expiresIn.

    With a method the buyer lands straight on it (the reply names the page `redirect`);
    without one the page lets the buyer choose."""
    body: dict[str, Any] = {
        "paymentDetails": {"amount": amount, "currency": "RUB"},
        "description": description,
        "return": return_url,
        "failedUrl": return_url,
        "payload": f"order:{order_id}",
        "orderId": str(order_id),
        # Platega links a buyer's payments by this address; a Telegram shop uses the numeric id.
        "metadata": {"user_email": f"{user_id}@telegram.org", "userId": str(user_id), "userName": str(user_id)},
    }
    if method is None:
        return await _request("POST", "/v2/transaction/process", body)
    reply = await _request("POST", "/transaction/process", {"paymentMethod": METHODS[method], **body})
    reply.setdefault("url", reply.get("redirect"))
    return reply


async def get_status(transaction_id: str) -> dict[str, Any]:
    """status: PENDING, CONFIRMED, CANCELED (and later CHARGEBACKED and others)."""
    return await _request("GET", f"/transaction/{transaction_id}")


async def refund_check(transaction_id: str) -> dict[str, Any]:
    """Whether a full refund is possible and what it costs; moves no money.

    supported, blockReason (INSUFFICIENT_FUNDS…), totalDeductUsdt (taken from the USDT balance with the fee),
    penaltyNativeAmount / penaltyNativeCurrency (the provider's refund fee, when it takes one)."""
    return await _request("GET", f"/transaction/{transaction_id}/cancel-supported")


async def refund(transaction_id: str) -> dict[str, Any]:
    """Full refund to the payer. accepted, manualControlRequired, refundOperationId, message.

    The result comes later: the payment turns CHARGEBACKED. A repeat while one runs answers accepted=false
    (or HTTP 409 REFUND_IN_PROGRESS); after a partial refund Platega answers 400 PARTIAL_REFUND_EXISTS."""
    return await _request("POST", f"/transaction/{transaction_id}/cancel")
