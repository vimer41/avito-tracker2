"""Приём вебхуков Platega: мгновенные статусы платежей, споры и возвраты.

Сервер слушает локальный порт (PLATEGA_CALLBACK_PORT, по умолчанию выключен). Наружу его открывает
обратный прокси с HTTPS (Caddy или nginx): Platega шлёт только на публичный HTTPS-адрес с настоящим
сертификатом. Без вебхука бот всё равно узнаёт статусы сам, запросами по расписанию (rub_payments).

Защита: заголовки X-MerchantId и X-Secret сверяются с ключами мерчанта за постоянное время,
платежи не из нашей базы отбрасываются без запросов наружу, а тариф выдаётся только после того,
как бот сам спросит статус у Platega. Нагрузка — один короткий HTTP-запрос на смену статуса.
"""

import asyncio
import hmac
import json
import logging
import re

from aiohttp import web
from aiogram import Bot

import billing
import config
import rub_payments

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 32 * 1024
PROCESS_TIMEOUT_SECONDS = 20
_ID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")
# Fields of the callback body the bot uses; everything else is ignored.
_REPORTED_FIELDS = ("status", "refundStatus", "refundedAmount", "refundableAmount", "refundAmount", "refundId")


def _same(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8"))


def make_app(bot: Bot) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        if not (_same(request.headers.get("X-MerchantId", ""), config.PLATEGA_MERCHANT_ID)
                and _same(request.headers.get("X-Secret", ""), config.PLATEGA_SECRET)):
            logger.warning("Platega callback rejected: merchant id or secret did not match")
            return web.Response(status=401)
        try:
            body = json.loads(await request.text())
        except (ValueError, UnicodeDecodeError):
            return web.Response(status=400)
        if not isinstance(body, dict):
            return web.Response(status=400)
        transaction_id = str(body.get("id") or "")
        if not _ID_RE.match(transaction_id):
            return web.Response(status=400)
        order = await billing.find_rub_order(transaction_id=transaction_id)
        if order is None:
            # Not ours (a subscription, another shop's test): answer 200 so Platega stops retrying.
            logger.info("Platega callback for an unknown payment: tx=%s status=%s", transaction_id, body.get("status"))
            return web.Response(text="OK")
        reported = {key: body[key] for key in _REPORTED_FIELDS if key in body}
        try:
            result = await asyncio.wait_for(
                rub_payments.check_order(bot, order["id"], reported=reported), PROCESS_TIMEOUT_SECONDS
            )
        except Exception as exc:
            # 503 makes Platega retry in 5, 10 and 15 minutes; the sweep also re-checks the order.
            logger.warning("Platega callback not processed: order=%s error=%s", order["id"], type(exc).__name__)
            return web.Response(status=503)
        logger.info("Platega callback: order=%s reported=%s result=%s", order["id"], reported.get("status"), result)
        return web.Response(text="OK")

    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.router.add_post(config.PLATEGA_CALLBACK_PATH, handle)
    return app


async def start(bot: Bot) -> web.AppRunner | None:
    if not (config.PLATEGA_ENABLED and config.PLATEGA_CALLBACK_PORT):
        return None
    runner = web.AppRunner(make_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, config.PLATEGA_CALLBACK_HOST, config.PLATEGA_CALLBACK_PORT).start()
    logger.info("Platega callback server: http://%s:%s%s",
                config.PLATEGA_CALLBACK_HOST, config.PLATEGA_CALLBACK_PORT, config.PLATEGA_CALLBACK_PATH)
    return runner
