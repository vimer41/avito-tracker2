"""Referral program.

An admin issues one link per owner (``ref_links``) with a term. A NEW user who opens the bot with
``/start ref_<code>`` while the link is on is bound to its owner for good (``users.referred_by``).
Every paid tariff of a bound user while the link is on writes a reward (``ref_rewards``) in the SAME
transaction that grants the tariff: percent of the price in kopecks, stars converted at the rate from the
settings. The reward's state is read from its order at read time, so nothing has to be hooked into refunds:
an order refunded → the reward is canceled; disputed or a Stars refund in progress → frozen; a partial ruble
refund → the reward shrinks by the same share. A reward can be paid out ``REF_HOLD_DAYS`` after the payment.
Payouts are manual (card or SBP), recorded by an admin in ``ref_payouts``; a reward canceled after a payout
leaves the balance negative and later rewards cover it.
"""
import html
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
import database

logger = logging.getLogger(__name__)

START_PREFIX = "ref_"
CODE_LENGTH = 8
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
TEST_PLANS = {"test", config.RUB_TEST_PLAN}
CANCELED_STATUSES = {"refunded"}
FROZEN_STATUSES = {"disputed", "refunding"}

STATE_PENDING = "pending"    # waiting for the hold to end
STATE_READY = "ready"        # can be paid out
STATE_FROZEN = "frozen"      # dispute or refund in progress
STATE_CANCELED = "canceled"  # money went back to the buyer
STATE_TITLES = {
    STATE_PENDING: "ожидает",
    STATE_READY: "доступно",
    STATE_FROZEN: "заморожено (спор)",
    STATE_CANCELED: "отменено (возврат)",
}


class ReferralError(ValueError):
    pass


def now_sql(delta: timedelta = timedelta()) -> str:
    """UTC in SQLite's datetime('now') format, so stored values compare as strings."""
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%d %H:%M:%S")


def fmt_rub(kop: int) -> str:
    kop = int(round(kop or 0))
    sign = "−" if kop < 0 else ""
    kop = abs(kop)
    rubles = f"{kop // 100:,}".replace(",", " ")
    return f"{sign}{rubles} ₽" if kop % 100 == 0 else f"{sign}{rubles},{kop % 100:02d} ₽"


def parse_rub(text: str) -> Optional[int]:
    """'1500', '1 500,50' → kopecks; None when it is not a positive amount."""
    raw = (text or "").replace(" ", "").replace(" ", "").replace("₽", "").replace(",", ".").strip()
    try:
        value = round(float(raw) * 100)
    except ValueError:
        return None
    return value if 0 < value <= 100_000_000 else None


def format_date(value) -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' → 'DD.MM.YYYY' in MSK."""
    if not value:
        return "бессрочно"
    try:
        moment = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S") + timedelta(hours=3)
    except ValueError:
        return str(value)
    return moment.strftime("%d.%m.%Y %H:%M")


def parse_start_code(text: Optional[str]) -> Optional[str]:
    parts = (text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().lower().startswith(START_PREFIX):
        return None
    code = parts[1].strip()[len(START_PREFIX):].lower()
    return code if 4 <= len(code) <= 32 and code.isalnum() else None


def link_url(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={START_PREFIX}{code}"


def is_active(link: Optional[dict], now: Optional[str] = None) -> bool:
    if not link or link.get("revoked_at"):
        return False
    expires = link.get("expires_at")
    return not expires or str(expires) > (now or now_sql())


def link_status(link: dict) -> str:
    if link.get("revoked_at"):
        return "выключена"
    if not is_active(link):
        return f"истекла {format_date(link['expires_at'])}"
    return "действует " + ("бессрочно" if not link.get("expires_at") else f"до {format_date(link['expires_at'])}")


async def _rows(sql: str, params: tuple = ()) -> list[dict]:
    async with database.reader_connection() as db:
        async with db.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def _row(sql: str, params: tuple = ()) -> Optional[dict]:
    rows = await _rows(sql, params)
    return rows[0] if rows else None


# ---------- links ----------

async def get_link(link_id: int) -> Optional[dict]:
    return await _row("SELECT * FROM ref_links WHERE id = ?", (int(link_id),))


async def get_link_by_code(code: str) -> Optional[dict]:
    return await _row("SELECT * FROM ref_links WHERE code = ?", (str(code).lower(),))


async def get_owner_link(owner_id: int) -> Optional[dict]:
    return await _row("SELECT * FROM ref_links WHERE owner_id = ? ORDER BY id DESC LIMIT 1", (int(owner_id),))


async def list_links(offset: int = 0, limit: int = 10) -> tuple[list[dict], int]:
    rows = await _rows("""
        SELECT l.*, u.username, u.first_name,
               (SELECT COUNT(*) FROM users r WHERE r.ref_link_id = l.id) AS came
        FROM ref_links l LEFT JOIN users u ON u.user_id = l.owner_id
        ORDER BY l.id DESC LIMIT ? OFFSET ?
    """, (int(limit), int(offset)))
    total = await _row("SELECT COUNT(*) AS n FROM ref_links")
    return rows, int(total["n"]) if total else 0


async def create_link(owner_id: int, admin_id: int, days: Optional[int], percent: Optional[int] = None) -> dict:
    """One link per owner: a second one is refused, the existing link is turned on again instead."""
    if config.is_admin(owner_id):
        raise ReferralError("Администратору ссылка не нужна.")
    percent = int(percent or config.REF_PERCENT)
    expires = now_sql(timedelta(days=int(days))) if days else None
    async with database.writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT id FROM ref_links WHERE owner_id = ?", (int(owner_id),)) as cursor:
            if await cursor.fetchone():
                await db.rollback()
                raise ReferralError("У пользователя уже есть ссылка: её можно продлить или включить.")
        for _ in range(10):
            code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))
            async with db.execute("SELECT 1 FROM ref_links WHERE code = ?", (code,)) as cursor:
                if not await cursor.fetchone():
                    break
        else:
            await db.rollback()
            raise ReferralError("Не удалось подобрать код ссылки, попробуйте ещё раз.")
        cursor = await db.execute(
            "INSERT INTO ref_links (code, owner_id, percent, created_by, expires_at) VALUES (?, ?, ?, ?, ?)",
            (code, int(owner_id), percent, int(admin_id), expires),
        )
        link_id = cursor.lastrowid
        await db.commit()
    return await get_link(link_id)


async def extend_link(link_id: int, days: Optional[int]) -> dict:
    """Turns the link on for `days` more (from its end if it is still on, else from now); None = no end.
    Works for an expired or switched-off link too: the code and everyone bound to it stay the same."""
    link = await get_link(link_id)
    if not link:
        raise ReferralError("Ссылка не найдена.")
    if days:
        start = link["expires_at"] if is_active(link) and link.get("expires_at") else now_sql()
        base = datetime.strptime(str(start)[:19], "%Y-%m-%d %H:%M:%S")
        expires = (base + timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    else:
        expires = None
    async with database.writer_connection() as db:
        await db.execute("UPDATE ref_links SET expires_at = ?, revoked_at = NULL, revoked_by = NULL WHERE id = ?",
                         (expires, int(link_id)))
        await db.commit()
    return await get_link(link_id)


async def revoke_link(link_id: int, admin_id: int) -> dict:
    async with database.writer_connection() as db:
        await db.execute("UPDATE ref_links SET revoked_at = datetime('now'), revoked_by = ? WHERE id = ? AND revoked_at IS NULL",
                         (int(admin_id), int(link_id)))
        await db.commit()
    return await get_link(link_id)


async def set_percent(link_id: int, percent: int) -> dict:
    if not 1 <= int(percent) <= 100:
        raise ReferralError("Процент должен быть от 1 до 100.")
    async with database.writer_connection() as db:
        await db.execute("UPDATE ref_links SET percent = ? WHERE id = ?", (int(percent), int(link_id)))
        await db.commit()
    return await get_link(link_id)


# ---------- binding ----------

async def bind_from_start(user_id: int, username: Optional[str], first_name: Optional[str],
                          text: Optional[str]) -> Optional[dict]:
    """Creates a NEW user bound to the link from `/start ref_<code>`. Returns the link when bound.
    An existing user, the owner himself, an admin, an unknown, expired or switched-off link → None."""
    code = parse_start_code(text)
    if not code or config.is_admin(user_id):
        return None
    link = await get_link_by_code(code)
    if not is_active(link) or int(link["owner_id"]) == int(user_id):
        return None
    user = await database.get_or_create_user(user_id, username, first_name, referral=(link["owner_id"], link["id"]))
    if user.get("created") and user.get("ref_link_id") == link["id"]:
        logger.info("Referral bound: user=%s owner=%s link=%s", user_id, link["owner_id"], link["id"])
        return link
    return None


# ---------- rewards ----------

async def star_rate() -> float:
    try:
        value = float(await database.get_bot_setting("ref_star_rate", "") or config.REF_STAR_RATE)
    except (TypeError, ValueError):
        value = config.REF_STAR_RATE
    return value if value > 0 else config.REF_STAR_RATE


async def set_star_rate(value: float) -> None:
    if not 0 < float(value) <= 100:
        raise ReferralError("Курс должен быть больше 0 и не больше 100 ₽ за звезду.")
    await database.set_bot_setting("ref_star_rate", str(round(float(value), 4)))


async def accrue_in_transaction(db, source: str, order_id: int, buyer_id: int, plan: Optional[str],
                                amount: float, currency: str) -> Optional[dict]:
    """Called inside the transaction that grants a paid tariff. Writes the reward once per order (UNIQUE)
    when the buyer was brought by a link that is on right now. Never raises: a referral problem must not
    break a payment."""
    try:
        if plan in TEST_PLANS or not amount or amount <= 0:
            return None
        async with db.execute("SELECT referred_by, ref_link_id FROM users WHERE user_id = ?", (int(buyer_id),)) as cursor:
            user = await cursor.fetchone()
        if not user or not user[0] or not user[1] or int(user[0]) == int(buyer_id):
            return None
        async with db.execute("SELECT * FROM ref_links WHERE id = ? AND owner_id = ?", (int(user[1]), int(user[0]))) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        link = dict(zip([c[0] for c in cursor.description], row))
        if not is_active(link):
            return None
        rate = None
        if currency == "XTR":
            async with db.execute("SELECT value FROM bot_settings WHERE key = 'ref_star_rate'") as cursor:
                setting = await cursor.fetchone()
            try:
                rate = float(setting[0]) if setting else config.REF_STAR_RATE
            except (TypeError, ValueError):
                rate = config.REF_STAR_RATE
            if rate <= 0:
                rate = config.REF_STAR_RATE
            base_rub = float(amount) * rate
        else:
            base_rub = float(amount)
        reward_kop = int(round(base_rub * 100 * int(link["percent"]) / 100))
        if reward_kop <= 0:
            return None
        available_at = now_sql(timedelta(days=config.REF_HOLD_DAYS))
        cursor = await db.execute("""
            INSERT OR IGNORE INTO ref_rewards (link_id, owner_id, buyer_id, source, order_id, tariff_plan,
                base_amount, currency, star_rate, percent, reward_kop, available_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (link["id"], link["owner_id"], int(buyer_id), source, int(order_id), plan, float(amount), currency,
              rate, int(link["percent"]), reward_kop, available_at))
        if cursor.rowcount != 1:
            return None
        return {"owner_id": link["owner_id"], "link_id": link["id"], "buyer_id": int(buyer_id), "source": source,
                "order_id": int(order_id), "amount": amount, "currency": currency, "reward_kop": reward_kop,
                "available_at": available_at}
    except Exception:
        logger.exception("Referral reward not written: source=%s order=%s", source, order_id)
        return None


_REWARDS_SQL = """
    SELECT r.*, s.status AS stars_status, o.status AS rub_status,
           o.refunded_amount AS rub_refunded, o.paid_amount AS rub_paid, o.amount AS rub_amount
    FROM ref_rewards r
    LEFT JOIN stars_orders s ON r.source = 'stars' AND s.id = r.order_id
    LEFT JOIN rub_orders o ON r.source = 'rub' AND o.id = r.order_id
"""


def reward_state(row: dict, now: Optional[str] = None) -> tuple[str, int]:
    """(state, kopecks that count) from the order's current status."""
    status = row.get("stars_status") if row["source"] == "stars" else row.get("rub_status")
    if status in CANCELED_STATUSES:
        return STATE_CANCELED, 0
    kop = int(row["reward_kop"])
    refunded = float(row.get("rub_refunded") or 0)
    if row["source"] == "rub" and refunded > 0:
        paid = float(row.get("rub_paid") or row.get("rub_amount") or 0)
        share = min(1.0, refunded / paid) if paid > 0 else 1.0
        kop = int(round(kop * (1 - share)))
    if status in FROZEN_STATUSES:
        return STATE_FROZEN, kop
    if str(row["available_at"]) > (now or now_sql()):
        return STATE_PENDING, kop
    return STATE_READY, kop


async def rewards(owner_id: int, limit: Optional[int] = None) -> list[dict]:
    sql = _REWARDS_SQL + " WHERE r.owner_id = ? ORDER BY r.id DESC" + (f" LIMIT {int(limit)}" if limit else "")
    rows = await _rows(sql, (int(owner_id),))
    now = now_sql()
    for row in rows:
        row["state"], row["kop"] = reward_state(row, now)
    return rows


async def summary(owner_id: int) -> dict[str, Any]:
    link = await get_owner_link(owner_id)
    rows = await rewards(owner_id)
    totals = {state: 0 for state in STATE_TITLES}
    for row in rows:
        totals[row["state"]] += row["kop"]
    came = await _row("SELECT COUNT(*) AS n FROM users WHERE referred_by = ?", (int(owner_id),))
    paid_out = await _row("SELECT COALESCE(SUM(amount_kop), 0) AS n FROM ref_payouts WHERE owner_id = ?", (int(owner_id),))
    buyers = {row["buyer_id"] for row in rows if row["state"] != STATE_CANCELED}
    paid_out_kop = int(paid_out["n"]) if paid_out else 0
    return {
        "link": link,
        "came": int(came["n"]) if came else 0,
        "buyers": len(buyers),
        "payments": sum(1 for row in rows if row["state"] != STATE_CANCELED),
        "earned": totals[STATE_PENDING] + totals[STATE_READY] + totals[STATE_FROZEN],
        "pending": totals[STATE_PENDING],
        "ready": totals[STATE_READY],
        "frozen": totals[STATE_FROZEN],
        "paid_out": paid_out_kop,
        "balance": totals[STATE_READY] - paid_out_kop,
        "recent": rows[:10],
    }


async def record_payout(owner_id: int, amount_kop: int, admin_id: int, note: str = "") -> dict:
    if int(amount_kop) <= 0:
        raise ReferralError("Сумма должна быть больше нуля.")
    async with database.writer_connection() as db:
        cursor = await db.execute("INSERT INTO ref_payouts (owner_id, amount_kop, admin_id, note) VALUES (?, ?, ?, ?)",
                                  (int(owner_id), int(amount_kop), int(admin_id), str(note or "")[:200]))
        payout_id = cursor.lastrowid
        await db.commit()
    return await _row("SELECT * FROM ref_payouts WHERE id = ?", (payout_id,))


async def payouts(owner_id: int, limit: int = 10) -> list[dict]:
    return await _rows("SELECT * FROM ref_payouts WHERE owner_id = ? ORDER BY id DESC LIMIT ?", (int(owner_id), int(limit)))


# ---------- notices to the owner ----------

def reward_line(row: dict) -> str:
    paid = f"{int(row['base_amount'])} ⭐" if row["currency"] == "XTR" else fmt_rub(round(float(row["base_amount"]) * 100))
    return (f"{format_date(row['created_at'])[:10]} · оплата {paid} → <b>{fmt_rub(row['kop'])}</b> · "
            f"{STATE_TITLES[row['state']]}")


async def notify_bound(bot, link: Optional[dict]) -> None:
    if not link:
        return
    import ui
    try:
        await bot.send_message(link["owner_id"], (
            f"{ui.e('users')} <b>По вашей ссылке пришёл новый пользователь</b>\n\n"
            f"С каждой его оплаты вам будет начисляться {link['percent']} %."
        ), parse_mode="HTML")
    except Exception as exc:
        logger.info("Referral bound notice failed: owner=%s error=%s", link["owner_id"], type(exc).__name__)


async def notify_reward(bot, reward: Optional[dict]) -> None:
    if not reward:
        return
    import ui
    try:
        await bot.send_message(reward["owner_id"], (
            f"{ui.e('price')} <b>Начислено {fmt_rub(reward['reward_kop'])}</b>\n\n"
            "Приглашённый вами пользователь оплатил подписку.\n"
            f"Сумму можно вывести после {format_date(reward['available_at'])} (МСК). "
            "Баланс — в «Профиль» → «Партнёрская программа»."
        ), parse_mode="HTML")
    except Exception as exc:
        logger.info("Referral reward notice failed: owner=%s error=%s", reward["owner_id"], type(exc).__name__)


def owner_name(row: dict) -> str:
    if row.get("username"):
        return "@" + html.escape(row["username"])
    return html.escape(row.get("first_name") or str(row.get("owner_id")))
