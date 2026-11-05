import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

import aiosqlite
import config
import radar_filters
from config import DB_PATH, ADMIN_ID, ADMIN_IDS, MAX_SEARCHES_PER_USER

logger = logging.getLogger(__name__)

MSK_TZ = timezone(timedelta(hours=3))

DEFAULT_SELLER_MESSAGE = "Здравствуйте! Товар ещё продаётся? Готов забрать сегодня."
MIGRATION_VERSION_300 = "v2026_04_avito_radar_300"


class RadarLimitError(ValueError):
    """The user's radar quota was reached during an atomic insert."""


async def _configure_connection(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")
    await db.execute("PRAGMA busy_timeout=5000;")
    await db.execute("PRAGMA cache_size=-2000;")
    await db.execute("PRAGMA temp_store=MEMORY;")
    db.row_factory = aiosqlite.Row


class _DatabasePool:
    """Долгоживущий пул SQLite-соединений: 1 writer под asyncio.Lock и пул из N reader-соединений."""

    def __init__(self, db_path: Path, loop: asyncio.AbstractEventLoop, read_pool_size: int) -> None:
        self.db_path = db_path
        self.loop = loop
        self.read_pool_size = max(1, int(read_pool_size))
        self._init_lock = asyncio.Lock()
        self._writer_lock = asyncio.Lock()
        self._writer: aiosqlite.Connection | None = None
        self._readers_all: list[aiosqlite.Connection] = []
        self._readers_queue: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._closed = False

    async def ensure_open(self) -> None:
        if self._writer is not None and not self._closed:
            return
        async with self._init_lock:
            if self._writer is not None and not self._closed:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            w_conn = aiosqlite.connect(self.db_path)
            w_conn.daemon = True
            writer = await w_conn
            await _configure_connection(writer)
            self._writer = writer

            self._readers_all.clear()
            while not self._readers_queue.empty():
                try:
                    self._readers_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            for _ in range(self.read_pool_size):
                r_conn = aiosqlite.connect(self.db_path)
                r_conn.daemon = True
                reader = await r_conn
                await _configure_connection(reader)
                self._readers_all.append(reader)
                self._readers_queue.put_nowait(reader)
            self._closed = False

    async def close(self) -> None:
        self._closed = True
        conns: list[aiosqlite.Connection] = []
        if self._writer is not None:
            conns.append(self._writer)
            self._writer = None
        conns.extend(self._readers_all)
        self._readers_all.clear()
        while not self._readers_queue.empty():
            try:
                self._readers_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        for conn in conns:
            try:
                await conn.close()
            except Exception:
                pass

    def close_sync(self) -> None:
        self._closed = True
        conns: list[aiosqlite.Connection] = []
        if self._writer is not None:
            conns.append(self._writer)
            self._writer = None
        conns.extend(self._readers_all)
        self._readers_all.clear()
        for conn in conns:
            try:
                conn.stop()
                thread = getattr(conn, "_thread", None)
                if thread is not None and thread.is_alive():
                    thread.join(timeout=2.0)
            except Exception:
                pass


_ACTIVE_POOLS: dict[tuple[str, int], _DatabasePool] = {}
_RESOLVED_PATHS: dict[str, Path] = {}


def _current_db_path() -> Path:
    raw = str(DB_PATH)
    cached = _RESOLVED_PATHS.get(raw)
    if cached is None:
        cached = Path(raw).expanduser().resolve()
        _RESOLVED_PATHS[raw] = cached
    return cached


async def _get_pool() -> _DatabasePool:
    loop = asyncio.get_running_loop()
    path = _current_db_path()
    key = (str(path), id(loop))

    # Close stale pools for dead loops or changed DB_PATH
    stale_keys = [
        k for k, pool in list(_ACTIVE_POOLS.items())
        if k != key and (pool.loop.is_closed() or pool.loop is loop)
    ]
    for sk in stale_keys:
        old_pool = _ACTIVE_POOLS.pop(sk, None)
        if old_pool is not None:
            if old_pool.loop is loop and not loop.is_closed():
                await old_pool.close()
            else:
                old_pool.close_sync()

    pool = _ACTIVE_POOLS.get(key)
    if pool is None or pool._closed:
        pool = _DatabasePool(path, loop, config.DB_READ_POOL_SIZE)
        _ACTIVE_POOLS[key] = pool
    await pool.ensure_open()
    return pool


@asynccontextmanager
async def writer_connection():
    pool = await _get_pool()
    async with pool._writer_lock:
        await pool.ensure_open()
        db = pool._writer
        assert db is not None
        try:
            yield db
        except BaseException:
            if db.in_transaction:
                try:
                    await db.rollback()
                except Exception:
                    pass
            raise


@asynccontextmanager
async def reader_connection():
    pool = await _get_pool()
    reader = await pool._readers_queue.get()
    try:
        if reader.in_transaction:
            try:
                await reader.rollback()
            except Exception:
                pass
        yield reader
    finally:
        if not pool._closed:
            pool._readers_queue.put_nowait(reader)


async def close_db() -> None:
    """Закрывает все активные соединения пула БД."""
    invalidate_blacklist_cache()
    for key, pool in list(_ACTIVE_POOLS.items()):
        _ACTIVE_POOLS.pop(key, None)
        try:
            loop = asyncio.get_running_loop()
            if pool.loop is loop:
                await pool.close()
            else:
                pool.close_sync()
        except Exception:
            pool.close_sync()


def close_db_sync(target_dir: str | Path | None = None) -> None:
    """Синхронно закрывает пулы БД (используется при очистке временных директорий в тестах на Windows)."""
    invalidate_blacklist_cache()
    target_prefix = str(Path(target_dir).resolve()).lower() if target_dir else None
    for key, pool in list(_ACTIVE_POOLS.items()):
        path_str = key[0].lower()
        if target_prefix is None or path_str.startswith(target_prefix):
            _ACTIVE_POOLS.pop(key, None)
            pool.close_sync()


# --- In-memory TTL кэш черного списка продавцов (45 с) ---
_BLACKLIST_CACHE: dict[int, tuple[float, set[str]]] = {}


def invalidate_blacklist_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _BLACKLIST_CACHE.clear()
    else:
        _BLACKLIST_CACHE.pop(int(user_id), None)


async def _add_seller_key_column(db) -> None:
    """Two sellers may share a name, so the key has to join the UNIQUE rule: that needs a table rebuild."""
    async with db.execute("PRAGMA table_info(blocked_sellers)") as cursor:
        if "seller_key" in {row[1] for row in await cursor.fetchall()}:
            return
    await db.execute("ALTER TABLE blocked_sellers RENAME TO blocked_sellers_old")
    await db.execute("""
        CREATE TABLE blocked_sellers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            seller_name TEXT NOT NULL,
            seller_key TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, seller_name, seller_key) ON CONFLICT IGNORE,
            FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
        );
    """)
    await db.execute("""
        INSERT INTO blocked_sellers (id, user_id, seller_name, created_at)
        SELECT id, user_id, seller_name, created_at FROM blocked_sellers_old
    """)
    await db.execute("DROP TABLE blocked_sellers_old")


async def ensure_columns(db, table: str, columns: dict[str, str]):
    """Idempotent additive migrations; real SQLite errors must fail startup."""
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        existing = {row[1] for row in await cursor.fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def format_subscription_date(dt_val) -> str:
    """
    Форматирует дату окончания подписки в Московском часовом поясе (UTC+3, МСК)
    с указанием оставшегося времени (например: '26.10.2026 в 19:53 (МСК) · осталось 23 ч. 58 мин.').
    """
    if not dt_val:
        return "не активна"
    try:
        raw_str = str(dt_val).strip()
        if len(raw_str) >= 19:
            utc_dt = datetime.strptime(raw_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        elif len(raw_str) >= 16:
            utc_dt = datetime.strptime(raw_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        else:
            return raw_str

        msk_dt = utc_dt.astimezone(MSK_TZ)
        now_utc = datetime.now(timezone.utc)
        diff = utc_dt - now_utc

        formatted_date = msk_dt.strftime("%d.%m.%Y в %H:%M (МСК)")
        if diff.total_seconds() > 0:
            total_minutes = int(diff.total_seconds() // 60)
            hours = total_minutes // 60
            minutes = total_minutes % 60
            if hours >= 24:
                days = hours // 24
                rem_hours = hours % 24
                time_left = f"{days} дн. {rem_hours} ч."
            elif hours > 0:
                time_left = f"{hours} ч. {minutes} мин."
            else:
                time_left = f"{minutes} мин."
            return f"{formatted_date} (осталось {time_left})"
        else:
            return f"{formatted_date} (истекла)"
    except Exception as e:
        logger.warning(f"Ошибка форматирования даты подписки '{dt_val}': {e}")
        return str(dt_val)[:16]


def classify_user_tariff(
    user_id: int | Dict[str, Any],
    tariff_plan: str | None = None,
    *,
    is_allowed: int | bool = 1,
    is_test_order: bool = False,
) -> tuple[str, int, float]:
    """
    Возвращает (tariff_key, priority, delivery_delay_seconds):
    - admin / Полный (pro) -> ('pro', 10, ...)
    - Средний (standart) -> ('standart', 20, ...)
    - Базовый (lite) -> ('lite', 25, ...)
    - триал (is_allowed = 0, в скан попадает только при действующем сроке) -> как Полный
    - тестовый заказ -> ('base', 30, ...)
    """
    if isinstance(user_id, dict):
        row = user_id
        uid = int(row.get("user_id") or 0)
        plan_raw = tariff_plan if tariff_plan is not None else row.get("tariff_plan")
        allowed_val = row.get("is_allowed", 1) if "is_allowed" in row and row.get("is_allowed") is not None else is_allowed
        test_val = bool(row.get("is_test_order", is_test_order))
    else:
        uid = int(user_id)
        plan_raw = tariff_plan
        allowed_val = is_allowed
        test_val = is_test_order

    if uid in ADMIN_IDS:
        return "pro", 10, config.tariff_delivery_delay("pro")
    plan = (plan_raw or "standart").strip().lower()
    if plan == "pro":
        return "pro", 10, config.tariff_delivery_delay("pro")
    if plan in ("base", "test", "trial") or test_val:
        return "base", 30, config.tariff_delivery_delay("base")
    if not bool(allowed_val):
        return "pro", 10, config.tariff_delivery_delay("pro")
    if plan == "lite":
        return "lite", 25, config.tariff_delivery_delay("lite")
    return "standart", 20, config.tariff_delivery_delay("standart")


def create_pre_migration_backup(db_path: Path) -> Path:
    """
    Создает резервную копию существующей БД перед применением миграции схемы.
    При любой ошибке выбрасывает исключение, прерывая старт.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.parent / f"{db_path.stem}.pre_migration_{ts}.bak"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(backup_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    if not backup_path.exists() or backup_path.stat().st_size == 0:
        raise RuntimeError(f"Резервная копия БД пуста или не создана: {backup_path}")
    return backup_path


def _check_needs_pre_migration_backup(db_path: Path) -> bool:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return False
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        if not tables:
            return False
        if "schema_migrations" in tables:
            cur2 = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (MIGRATION_VERSION_300,),
            )
            if cur2.fetchone() is not None:
                return False
        return bool(tables & {"users", "searches", "seen_items", "notification_outbox"})
    finally:
        conn.close()


async def _run_migration_300(db: aiosqlite.Connection, migration_started_at: float) -> None:
    """Одноразовая аддитивная миграция к схеме Avito Radar 300."""
    from parser import AvitoParser

    await db.execute("BEGIN IMMEDIATE")
    async with db.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        (MIGRATION_VERSION_300,),
    ) as cur:
        if await cur.fetchone() is not None:
            await db.commit()
            return

    # 1. Сброс устаревших порогов скидки (колонка сохраняется для совместимости)
    await db.execute("UPDATE searches SET min_discount_percent = 0 WHERE COALESCE(min_discount_percent, 0) != 0")

    # 2. Перенос живых элементов из legacy notification_outbox в notification_events + dedupe
    admin_placeholders = ",".join("?" for _ in ADMIN_IDS)
    async with db.execute(
        f"""
        SELECT o.id, o.search_id, o.user_id, o.item_id, o.event_key, o.payload,
               CAST(strftime('%s', o.created_at) AS REAL) AS created_epoch,
               s.title AS radar_title, s.is_active, s.max_age_minutes,
               u.tariff_plan, u.is_allowed,
               (u.user_id IN ({admin_placeholders})
                OR (u.is_allowed = 1 AND u.subscription_until IS NULL)
                OR u.subscription_until > datetime('now')) AS user_has_access
        FROM notification_outbox o
        LEFT JOIN searches s ON s.id = o.search_id
        LEFT JOIN users u ON u.user_id = o.user_id
        ORDER BY o.id ASC
        """,
        tuple(ADMIN_IDS),
    ) as cur:
        outbox_rows = await cur.fetchall()

    pending_migrated = 0
    expired_discarded = 0
    grouped_valid: dict[tuple[int, str], dict[str, Any]] = {}

    for row in outbox_rows:
        event_key = row["event_key"] or ""
        created_epoch = float(row["created_epoch"] or migration_started_at)
        max_age_sec = min(
            int(config.NOTIFICATION_MAX_AGE),
            max(60, int(row["max_age_minutes"] or 30) * 60),
        )
        age = max(0.0, migration_started_at - created_epoch)

        if (
            event_key != "new"
            or not row["is_active"]
            or not row["user_has_access"]
            or age > max_age_sec
            or age > config.NOTIFICATION_MAX_AGE
        ):
            expired_discarded += 1
            continue

        try:
            payload_obj = json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            expired_discarded += 1
            continue

        item_data = payload_obj.get("item") if isinstance(payload_obj.get("item"), dict) else payload_obj
        key = (int(row["user_id"]), str(row["item_id"]))
        tariff_key, priority, _ = classify_user_tariff(
            int(row["user_id"]),
            row["tariff_plan"],
            is_allowed=row["is_allowed"] if row["is_allowed"] is not None else 1,
        )
        radar_title = str(
            row["radar_title"]
            or payload_obj.get("radar_title")
            or payload_obj.get("search_title")
            or "Радар"
        )
        envelope = payload_obj if isinstance(payload_obj.get("item"), dict) else {"item": item_data}

        if key not in grouped_valid:
            t_pub = item_data.get("timestamp") if isinstance(item_data, dict) else None
            t_pub_val = float(t_pub) if isinstance(t_pub, (int, float)) and t_pub > 0 else None
            grouped_valid[key] = {
                "user_id": key[0],
                "item_id": key[1],
                "tariff_plan": tariff_key,
                "priority": priority,
                "queued_at": created_epoch,
                "deliver_at": migration_started_at,
                "item_json": json.dumps(envelope, ensure_ascii=False),
                "t_pub": t_pub_val,
                "t_pub_reliable": 1 if t_pub_val is not None else 0,
                "t_seen": float(payload_obj.get("discovered_at") or created_epoch),
                "radars": [(int(row["search_id"]), radar_title)],
            }
        else:
            entry = grouped_valid[key]
            if priority < entry["priority"]:
                entry["priority"] = priority
                entry["tariff_plan"] = tariff_key
            pair = (int(row["search_id"]), radar_title)
            if pair not in entry["radars"]:
                entry["radars"].append(pair)

    for (uid, iid), entry in grouped_valid.items():
        cur_ev = await db.execute(
            """
            INSERT OR IGNORE INTO notification_events (
                user_id, item_id, tariff_plan, priority, is_catchup, crash_retry,
                status, queued_at, deliver_at, next_attempt_at,
                item_json, t_pub, t_pub_reliable, t_seen
            ) VALUES (?, ?, ?, ?, 0, 0, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                iid,
                entry["tariff_plan"],
                entry["priority"],
                entry["queued_at"],
                entry["deliver_at"],
                entry["deliver_at"],
                entry["item_json"],
                entry["t_pub"],
                entry["t_pub_reliable"],
                entry["t_seen"],
            ),
        )
        ev_id = cur_ev.lastrowid
        await db.execute(
            """
            INSERT OR REPLACE INTO notification_dedupe (
                user_id, item_id, event_id, status, first_queued_at, sent_at, updated_at
            ) VALUES (?, ?, ?, 'queued', ?, NULL, ?)
            """,
            (uid, iid, ev_id, entry["queued_at"], migration_started_at),
        )
        for sid, rtitle in entry["radars"]:
            await db.execute(
                """
                INSERT OR IGNORE INTO notification_dedupe_radars (
                    user_id, item_id, search_id, radar_title, matched_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (uid, iid, sid, rtitle, migration_started_at),
            )
        pending_migrated += 1

    if outbox_rows:
        await db.execute("DELETE FROM notification_outbox")

    # 3. Инициализация source_scan_state для всех активных URL
    async with db.execute("SELECT DISTINCT url FROM searches WHERE is_active = 1") as cur:
        active_urls = [r[0] for r in await cur.fetchall() if r[0]]

    for raw_url in active_urls:
        try:
            norm_url = AvitoParser.normalize_url(raw_url)
        except Exception:
            norm_url = str(raw_url).strip()
        if norm_url:
            await db.execute(
                """
                INSERT OR IGNORE INTO source_scan_state (normalized_url, last_top_ids, processed_at)
                VALUES (?, '', ?)
                """,
                (norm_url, migration_started_at),
            )

    await db.execute(
        "INSERT INTO schema_migrations (version) VALUES (?)",
        (MIGRATION_VERSION_300,),
    )
    await db.commit()
    logger.info(
        "Миграция БД %s выполнена: pending migrated=%d, expired discarded=%d",
        MIGRATION_VERSION_300,
        pending_migrated,
        expired_discarded,
    )


async def init_db():
    """Инициализация базы данных, пула соединений и аддитивных миграций."""
    invalidate_blacklist_cache()
    db_file = _current_db_path()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    migration_started_at = time.time()

    # Если БД уже существует и еще не мигрирована до v2026_04_avito_radar_300 — создаем бэкап до миграции
    if _check_needs_pre_migration_backup(db_file):
        await close_db()
        backup_path = create_pre_migration_backup(db_file)
        logger.info("Создан предмиграционный бэкап БД: %s", backup_path)

    async with writer_connection() as db:
        await db.execute("PRAGMA auto_vacuum = INCREMENTAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                default_template TEXT DEFAULT 'Здравствуйте! Товар ещё продаётся? Готов забрать сегодня.',
                is_allowed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await ensure_columns(db, "users", {
            "default_template": "TEXT DEFAULT 'Здравствуйте! Товар ещё продаётся? Готов забрать сегодня.'",
            "is_allowed": "INTEGER DEFAULT 0",
            "created_at": "TIMESTAMP",
            "subscription_until": "TIMESTAMP",
            "trial_used": "INTEGER DEFAULT 0",
            "tariff_plan": "TEXT DEFAULT 'standart'",
            "subscription_revision": "INTEGER NOT NULL DEFAULT 0",
            "extra_radars": "INTEGER NOT NULL DEFAULT 0",
            "night_mode": "INTEGER NOT NULL DEFAULT 0",
            "night_from": "INTEGER NOT NULL DEFAULT 23",
            "night_to": "INTEGER NOT NULL DEFAULT 8",
            "reminded_key": "TEXT NOT NULL DEFAULT ''",
            "mirror_chat_id": "INTEGER",
            "last_seen_at": "TIMESTAMP",
            "admin_note": "TEXT NOT NULL DEFAULT ''",
        })
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                user_id INTEGER,
                action TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS admin_actions_user ON admin_actions(user_id, id)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS stars_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                tariff_plan TEXT NOT NULL CHECK(tariff_plan IN ('lite', 'standart', 'pro', 'test')),
                total_amount INTEGER NOT NULL CHECK(total_amount > 0),
                days INTEGER NOT NULL CHECK(days > 0),
                status TEXT NOT NULL DEFAULT 'pending',
                pre_checkout_query_id TEXT,
                approved_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                telegram_payment_charge_id TEXT UNIQUE,
                provider_payment_charge_id TEXT,
                paid_at TIMESTAMP,
                subscription_before TIMESTAMP,
                tariff_before TEXT,
                allowed_before INTEGER,
                trial_before INTEGER,
                subscription_after TIMESTAMP,
                tariff_after TEXT,
                revision_after INTEGER,
                refund_requested_at TIMESTAMP,
                refund_admin_id INTEGER,
                refunded_at TIMESTAMP,
                refund_needs_review INTEGER NOT NULL DEFAULT 0
            )
        """)
        async with db.execute("SELECT sql FROM sqlite_master WHERE name = 'stars_orders'") as cur:
            row = await cur.fetchone()
            # Rebuild when the CHECK predates a tariff (first 'test', then 'lite' for «Базовый»).
            if row and row[0] and "'lite'" not in row[0]:
                await db.execute("ALTER TABLE stars_orders RENAME TO stars_orders_old;")
                await db.execute("""
                    CREATE TABLE stars_orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        payload TEXT NOT NULL UNIQUE,
                        user_id INTEGER NOT NULL,
                        tariff_plan TEXT NOT NULL CHECK(tariff_plan IN ('lite', 'standart', 'pro', 'test')),
                        total_amount INTEGER NOT NULL CHECK(total_amount > 0),
                        days INTEGER NOT NULL CHECK(days > 0),
                        status TEXT NOT NULL DEFAULT 'pending',
                        pre_checkout_query_id TEXT,
                        approved_at TIMESTAMP,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP NOT NULL,
                        telegram_payment_charge_id TEXT UNIQUE,
                        provider_payment_charge_id TEXT,
                        paid_at TIMESTAMP,
                        subscription_before TIMESTAMP,
                        tariff_before TEXT,
                        allowed_before INTEGER,
                        trial_before INTEGER,
                        subscription_after TIMESTAMP,
                        tariff_after TEXT,
                        revision_after INTEGER,
                        refund_requested_at TIMESTAMP,
                        refund_admin_id INTEGER,
                        refunded_at TIMESTAMP,
                        refund_needs_review INTEGER NOT NULL DEFAULT 0
                    )
                """)
                await db.execute("INSERT INTO stars_orders SELECT * FROM stars_orders_old;")
                await db.execute("DROP TABLE stars_orders_old;")
        await db.execute("CREATE INDEX IF NOT EXISTS stars_orders_user ON stars_orders(user_id, id)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_subscription_actions (
                token TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                tariff_plan TEXT NOT NULL,
                days INTEGER NOT NULL,
                page INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                applied_at TIMESTAMP,
                subscription_until TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                stop_words TEXT DEFAULT '[]',
                template TEXT,
                is_active INTEGER DEFAULT 1,
                min_discount_percent INTEGER DEFAULT 0,
                search_in_title_only INTEGER DEFAULT 0,
                max_age_minutes INTEGER DEFAULT 5,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            );
        """)

        await ensure_columns(db, "searches", {
            "stop_words": "TEXT DEFAULT '[]'",
            "template": "TEXT",
            "is_active": "INTEGER DEFAULT 1",
            "min_discount_percent": "INTEGER DEFAULT 0",
            "search_in_title_only": "INTEGER DEFAULT 0",
            "max_age_minutes": "INTEGER DEFAULT 5",
            "created_at": "TIMESTAMP",
            "initialized_at": "TEXT DEFAULT ''",
            "price_min": "INTEGER DEFAULT 0",
            "price_max": "INTEGER DEFAULT 0",
            "must_words": "TEXT DEFAULT '[]'",
            "delivery_only": "INTEGER DEFAULT 0",
            "hide_promoted": "INTEGER DEFAULT 0",
            "reply_template": "TEXT DEFAULT ''",
            "pause_reason": "TEXT DEFAULT ''",
        })
        await db.execute("""
            CREATE TABLE IF NOT EXISTS radar_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                search_id INTEGER NOT NULL,
                at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                action TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS radar_history_user ON radar_history(user_id, id)")
        await db.execute("CREATE INDEX IF NOT EXISTS radar_history_search ON radar_history(search_id)")
        # Radars made before the history existed get their creation row once.
        await db.execute("""
            INSERT INTO radar_history (user_id, search_id, at, action, title, url, detail)
            SELECT s.user_id, s.id, COALESCE(s.created_at, datetime('now')), 'create', s.title, s.url, 'до журнала'
            FROM searches s
            WHERE NOT EXISTS (SELECT 1 FROM radar_history h WHERE h.search_id = s.id)
        """)
        await db.execute("UPDATE searches SET is_active = 1 WHERE is_active IS NULL;")
        await db.execute("UPDATE searches SET stop_words = '[]' WHERE stop_words IS NULL;")
        await db.execute("UPDATE searches SET initialized_at = COALESCE(created_at, datetime('now')) WHERE initialized_at = ''")
        await db.execute("UPDATE searches SET min_discount_percent = 0 WHERE min_discount_percent IS NULL;")
        await db.execute(
            "UPDATE searches SET max_age_minutes = 5 WHERE max_age_minutes IS NULL OR max_age_minutes <= 0 OR max_age_minutes > 30;"
        )

        for aid in ADMIN_IDS:
            await db.execute("UPDATE users SET is_allowed = 1, tariff_plan = 'pro' WHERE user_id = ?", (aid,))

        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                search_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                last_price INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (search_id, item_id),
                FOREIGN KEY (search_id) REFERENCES searches (id) ON DELETE CASCADE
            );
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocked_sellers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                seller_name TEXT NOT NULL,
                seller_key TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, seller_name, seller_key) ON CONFLICT IGNORE,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            );
        """)
        await _add_seller_key_column(db)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS support_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL DEFAULT '',
                file_id TEXT NOT NULL DEFAULT '',
                is_payment INTEGER NOT NULL DEFAULT 0,
                created_epoch REAL NOT NULL
            );
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                event_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claimed_at TIMESTAMP,
                lease_token TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(search_id, item_id, event_key),
                FOREIGN KEY (search_id) REFERENCES searches (id) ON DELETE CASCADE
            );
        """)
        await ensure_columns(db, "notification_outbox", {
            "claimed_at": "TIMESTAMP",
            "lease_token": "TEXT",
            "available_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        })
        await db.execute("CREATE INDEX IF NOT EXISTS outbox_ready ON notification_outbox(available_at, claimed_at);")

        # Новые таблицы Avito Radar 300
        await db.execute("""
            CREATE TABLE IF NOT EXISTS source_scan_state (
                normalized_url TEXT PRIMARY KEY,
                last_top_ids TEXT DEFAULT '',
                processed_at REAL NOT NULL,
                failure_streak INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await ensure_columns(db, "source_scan_state", {
            "failure_streak": "INTEGER NOT NULL DEFAULT 0",
        })

        await db.execute("""
            CREATE TABLE IF NOT EXISTS notification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                tariff_plan TEXT NOT NULL DEFAULT 'base',
                priority INTEGER NOT NULL DEFAULT 30,
                is_catchup INTEGER NOT NULL DEFAULT 0,
                crash_retry INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                queued_at REAL NOT NULL,
                deliver_at REAL NOT NULL,
                next_attempt_at REAL NOT NULL,
                lease_until REAL,
                send_started_at REAL,
                sent_at REAL,
                failed_at REAL,
                expired_at REAL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                generic_error_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                item_json TEXT NOT NULL,
                t_pub REAL,
                t_pub_reliable INTEGER NOT NULL DEFAULT 0,
                t_seen REAL NOT NULL,
                UNIQUE(user_id, item_id)
            );
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS notification_dedupe (
                user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                event_id INTEGER,
                status TEXT NOT NULL DEFAULT 'queued',
                first_queued_at REAL NOT NULL,
                sent_at REAL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, item_id)
            );
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS notification_dedupe_radars (
                user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                search_id INTEGER NOT NULL,
                radar_title TEXT NOT NULL,
                matched_at REAL NOT NULL,
                PRIMARY KEY (user_id, item_id, search_id)
            );
        """)

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_events_dispatch "
            "ON notification_events(status, next_attempt_at, deliver_at);"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_dedupe_radars_search "
            "ON notification_dedupe_radars(search_id, user_id, item_id);"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_events_sent_at "
            "ON notification_events(sent_at) WHERE sent_at IS NOT NULL;"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_events_failed_at "
            "ON notification_events(failed_at) WHERE failed_at IS NOT NULL;"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_events_expired_at "
            "ON notification_events(expired_at) WHERE expired_at IS NOT NULL;"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await db.commit()
        await _run_migration_300(db, migration_started_at)
        async with db.execute("SELECT value FROM bot_settings WHERE key = 'allow_direct_avito'") as cur:
            s_row = await cur.fetchone()
            if s_row is not None:
                config.ALLOW_DIRECT_AVITO = str(s_row[0]) == "1"
        # Proxies added from the bot live in bot_settings.proxy_urls (JSON list); the old single
        # proxy_url setting is folded into that list once and then cleared.
        async with db.execute("SELECT key, value FROM bot_settings WHERE key IN ('proxy_urls', 'proxy_url')") as cur:
            proxy_rows = {str(row[0]): str(row[1]) for row in await cur.fetchall()}
        try:
            proxy_urls = [str(u) for u in json.loads(proxy_rows.get("proxy_urls") or "[]") if str(u).strip()]
        except (TypeError, ValueError):
            proxy_urls = []
        legacy_proxy = (proxy_rows.get("proxy_url") or "").strip()
        if legacy_proxy:
            if legacy_proxy not in proxy_urls:
                proxy_urls.append(legacy_proxy)
            await db.execute(
                """
                INSERT INTO bot_settings (key, value, updated_at) VALUES ('proxy_urls', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (json.dumps(proxy_urls),),
            )
            await db.execute("UPDATE bot_settings SET value = '' WHERE key = 'proxy_url'")
            await db.commit()
        config.PROXY_URLS = proxy_urls
        async with db.execute("SELECT value FROM bot_settings WHERE key = 'net_mode'") as cur:
            m_row = await cur.fetchone()
        async with db.execute("SELECT value FROM bot_settings WHERE key = 'net_mode_smart_offered'") as cur:
            offered = await cur.fetchone() is not None
        if not offered:
            # Once: «both» becomes «smart» (the server alone while it copes). A later choice of «both» stays.
            if m_row is not None and str(m_row[0]) == "both":
                m_row = ("smart",)
                await db.execute("UPDATE bot_settings SET value = 'smart', updated_at = CURRENT_TIMESTAMP WHERE key = 'net_mode'")
                logger.info("Режим сети «Оба» переведён на «Умный»: сервер сканирует один, пока справляется")
            await db.execute(
                "INSERT OR IGNORE INTO bot_settings (key, value, updated_at) VALUES ('net_mode_smart_offered', '1', CURRENT_TIMESTAMP)"
            )
            await db.commit()
        if m_row is not None and str(m_row[0]) in config.NET_MODES:
            config.NET_MODE = str(m_row[0])
        async with db.execute("SELECT value FROM bot_settings WHERE key = 'stars_prices'") as cur:
            p_row = await cur.fetchone()
        if p_row is not None:
            try:
                config.apply_stars_prices(*json.loads(str(p_row[0])))
            except (TypeError, ValueError):
                logger.warning("Некорректные цены Stars в bot_settings: оставляю цены из настроек окружения")
        logger.info("База данных успешно инициализирована")


async def get_bot_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        async with reader_connection() as db:
            async with db.execute("SELECT value FROM bot_settings WHERE key = ?", (str(key),)) as cur:
                row = await cur.fetchone()
                return str(row[0]) if row is not None else default
    except sqlite3.OperationalError:
        return default


async def get_proxy_urls() -> list[str]:
    try:
        return [str(u) for u in json.loads(await get_bot_setting("proxy_urls", "[]") or "[]") if str(u).strip()]
    except (TypeError, ValueError):
        return []


async def save_proxy_urls(proxy_urls: list[str]) -> None:
    await set_bot_setting("proxy_urls", json.dumps(list(proxy_urls)))


async def set_bot_setting(key: str, value: str) -> None:
    async with writer_connection() as db:
        await db.execute(
            """
            INSERT INTO bot_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (str(key), str(value)),
        )
        await db.commit()


async def get_or_create_user(user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> Dict[str, Any]:
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                await db.commit()
                return dict(row)

        is_adm = user_id in ADMIN_IDS
        if is_adm:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name, default_template, is_allowed, tariff_plan) VALUES (?, ?, ?, ?, 1, 'pro')",
                (user_id, username, first_name, DEFAULT_SELLER_MESSAGE)
            )
        else:
            # No access yet: the trial is taken through the channel gate (trial_gate.py).
            await db.execute("""
                INSERT OR IGNORE INTO users (user_id, username, first_name, default_template, is_allowed, tariff_plan)
                VALUES (?, ?, ?, ?, 0, 'standart')
            """, (user_id, username, first_name, DEFAULT_SELLER_MESSAGE))
        await db.commit()

        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row)


def _plan_max_radars(plan: str, is_allowed: bool, trial_used: bool) -> int:
    if is_allowed or plan == "pro":
        return config.plan_radars(plan)
    return config.TRIAL_RADARS


PAUSE_LIMIT = "limit"      # paused by the bot: the radar does not fit the tariff
PAUSE_BLOCKED = "blocked"  # paused by the bot: the user blocked it


async def sync_radar_limit_in_transaction(db, user_id: int) -> None:
    """Running radars fit the tariff: extras pause, and radars paused for this reason return when room appears."""
    if user_id in ADMIN_IDS:
        return
    async with db.execute(
        "SELECT tariff_plan, is_allowed, trial_used, extra_radars FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        user = await cursor.fetchone()
    if user is None:
        return
    plan = (user[0] or "standart").lower()
    limit = _plan_max_radars(plan, bool(user[1]), bool(user[2])) + int(user[3] or 0)
    async with db.execute(
        "SELECT id, is_active, pause_reason FROM searches WHERE user_id = ? ORDER BY id", (user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
    active = [row[0] for row in rows if row[1]]
    waiting = [row[0] for row in rows if not row[1] and row[2] == PAUSE_LIMIT]
    if active[limit:]:
        await db.executemany(
            "UPDATE searches SET is_active = 0, pause_reason = ? WHERE id = ?",
            [(PAUSE_LIMIT, radar_id) for radar_id in active[limit:]],
        )
        for radar_id in active[limit:]:
            await log_radar_history(db, user_id, radar_id, "limit_pause", detail=f"лимит {limit}")
    returning = waiting[: max(0, limit - len(active))]
    if returning:
        await db.executemany(
            "UPDATE searches SET is_active = 1, pause_reason = '' WHERE id = ?",
            [(radar_id,) for radar_id in returning],
        )
        for radar_id in returning:
            await log_radar_history(db, user_id, radar_id, "limit_resume", detail=f"лимит {limit}")


async def sync_radar_limit(user_id: int) -> None:
    async with writer_connection() as db:
        await sync_radar_limit_in_transaction(db, user_id)
        await db.commit()


async def enforce_radar_limits() -> None:
    """Startup pass for radars left over a limit before the limit covered running radars."""
    async with reader_connection() as db:
        async with db.execute("SELECT DISTINCT user_id FROM searches WHERE is_active = 1") as cursor:
            user_ids = [int(row[0]) for row in await cursor.fetchall()]
    for user_id in user_ids:
        await sync_radar_limit(user_id)


async def count_limit_paused(user_id: int) -> int:
    async with reader_connection() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM searches WHERE user_id = ? AND is_active = 0 AND pause_reason = ?",
            (user_id, PAUSE_LIMIT),
        ) as cursor:
            return int((await cursor.fetchone())[0])


async def resume_blocked_searches(user_id: int) -> int:
    """The user is back after blocking the bot: their radars run again, within the tariff."""
    async with writer_connection() as db:
        async with db.execute(
            "SELECT id FROM searches WHERE user_id = ? AND pause_reason = ?", (user_id, PAUSE_BLOCKED)
        ) as cur:
            back = [int(row[0]) for row in await cur.fetchall()]
        cursor = await db.execute(
            "UPDATE searches SET is_active = 1, pause_reason = '' WHERE user_id = ? AND pause_reason = ?",
            (user_id, PAUSE_BLOCKED),
        )
        resumed = max(0, int(cursor.rowcount or 0))
        for radar_id in back:
            await log_radar_history(db, user_id, radar_id, "blocked_resume")
        if resumed:
            await sync_radar_limit_in_transaction(db, user_id)
        await db.commit()
        return resumed


def subscription_seconds_left(user: dict) -> int:
    raw = str(user.get("subscription_until") or "").strip()[:19]
    try:
        until = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return max(0, int((until - datetime.now(timezone.utc)).total_seconds()))


async def get_user_max_radars(user_id: int) -> int:
    """Tariff limit plus the extra radars an admin granted (admins start from MAX_SEARCHES_PER_USER)."""
    if user_id in ADMIN_IDS:
        user = await get_user(user_id)
        return MAX_SEARCHES_PER_USER + int((user or {}).get("extra_radars") or 0)

    user = await get_or_create_user(user_id)
    plan = (user.get("tariff_plan") or "standart").lower()
    base = _plan_max_radars(plan, bool(user.get("is_allowed")), bool(user.get("trial_used")))
    return base + int(user.get("extra_radars") or 0)


async def change_user_extra_radars(user_id: int, delta: int) -> int:
    """Adds delta (may be negative) to the admin-granted radar bonus; never below zero. Returns the new bonus."""
    await get_or_create_user(user_id)
    async with writer_connection() as db:
        await db.execute(
            "UPDATE users SET extra_radars = MAX(0, COALESCE(extra_radars, 0) + ?) WHERE user_id = ?",
            (int(delta), user_id),
        )
        await sync_radar_limit_in_transaction(db, user_id)
        await db.commit()
        async with db.execute("SELECT extra_radars FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return int(row[0] or 0) if row else 0


async def is_user_allowed(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    async with reader_connection() as db:
        async with db.execute("""
            SELECT 1 FROM users 
            WHERE user_id = ? 
              AND ((is_allowed = 1 AND subscription_until IS NULL) OR subscription_until > datetime('now'))
        """, (user_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row)


async def set_user_allowed(user_id: int, is_allowed: int = 1):
    async with writer_connection() as db:
        if is_allowed:
            await recalibrate_if_no_access_in_transaction(db, user_id)
        await db.execute(
            "UPDATE users SET is_allowed = ?, subscription_until = NULL, trial_used = 1, subscription_revision = subscription_revision + 1 WHERE user_id = ?",
            (int(bool(is_allowed)), user_id),
        )
        await sync_radar_limit_in_transaction(db, user_id)
        await db.commit()


async def get_all_users() -> List[Dict[str, Any]]:
    async with reader_connection() as db:
        async with db.execute("SELECT * FROM users ORDER BY created_at DESC") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    async with reader_connection() as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def find_users_by_query(query: str) -> List[Dict[str, Any]]:
    clean_q = query.strip().lstrip("@")
    if not clean_q:
        return []
    async with reader_connection() as db:
        results = []
        if clean_q.isdigit():
            target_id = int(clean_q)
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (target_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    results.append(dict(row))

        like_str = f"%{clean_q}%"
        async with db.execute("""
            SELECT * FROM users 
            WHERE username LIKE ? OR first_name LIKE ? OR CAST(user_id AS TEXT) LIKE ?
            ORDER BY user_id DESC
            LIMIT 20
        """, (like_str, like_str, like_str)) as cur:
            rows = await cur.fetchall()
            existing_ids = {u["user_id"] for u in results}
            for r in rows:
                if r["user_id"] not in existing_ids:
                    results.append(dict(r))
        return results


async def recalibrate_if_no_access_in_transaction(db, user_id: int) -> None:
    """Access is coming back after a gap: the first scan only remembers what is already listed.
    Without this the user would get a catch-up of old lots, as after downtime."""
    if int(user_id) in ADMIN_IDS:
        return
    async with db.execute(
        "SELECT 1 FROM users WHERE user_id = ? AND ((is_allowed = 1 AND subscription_until IS NULL) "
        "OR subscription_until > datetime('now'))",
        (user_id,),
    ) as cursor:
        if await cursor.fetchone():
            return
    await db.execute("UPDATE searches SET initialized_at = NULL WHERE user_id = ?", (user_id,))


async def grant_vip_access(user_id: int, tariff_plan: str = "pro") -> Dict[str, Any]:
    plan = tariff_plan.lower() if tariff_plan in config.PAID_PLANS else "pro"
    async with writer_connection() as db:
        await recalibrate_if_no_access_in_transaction(db, user_id)
        await db.execute("""
            UPDATE users 
            SET is_allowed = 1,
                subscription_until = NULL,
                tariff_plan = ?,
                trial_used = 1,
                subscription_revision = subscription_revision + 1
            WHERE user_id = ?
        """, (plan, user_id))
        await sync_radar_limit_in_transaction(db, user_id)
        await db.commit()
    user = await get_user(user_id)
    return user or {}


async def reset_user_trial(user_id: int) -> Dict[str, Any]:
    async with writer_connection() as db:
        await recalibrate_if_no_access_in_transaction(db, user_id)
        await db.execute("""
            UPDATE users 
            SET trial_used = 1,
                subscription_until = datetime('now', ?),
                tariff_plan = 'standart',
                is_allowed = 0,
                subscription_revision = subscription_revision + 1
            WHERE user_id = ?
        """, (f"+{config.TRIAL_HOURS} hours", user_id))
        await sync_radar_limit_in_transaction(db, user_id)
        await db.commit()
    user = await get_user(user_id)
    return user or {}


async def update_user_default_template(user_id: int, template: str):
    async with writer_connection() as db:
        await db.execute(
            "UPDATE users SET default_template = ? WHERE user_id = ?",
            (template.strip(), user_id)
        )
        await db.commit()


async def add_search(user_id: int, title: str, url: str, search_in_title_only: int = 0, max_age_minutes: int = 5,
                     *, initial_items=()) -> int:
    from parser import AvitoParser

    max_radars = await get_user_max_radars(user_id)
    max_age_minutes = min(30, max(1, int(max_age_minutes)))
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        if user_id not in ADMIN_IDS:
            async with db.execute("""
                SELECT * FROM users WHERE user_id = ?
                  AND ((is_allowed = 1 AND subscription_until IS NULL) OR subscription_until > datetime('now'))
            """, (user_id,)) as cursor:
                user = await cursor.fetchone()
                if user is None:
                    raise PermissionError("Доступ к мониторингу истёк или отозван")
                plan = (user["tariff_plan"] or "standart").lower()
                max_radars = (
                    _plan_max_radars(plan, bool(user["is_allowed"]), bool(user["trial_used"]))
                    + int(user["extra_radars"] or 0)
                )
        async with db.execute("SELECT COUNT(*) FROM searches WHERE user_id = ?", (user_id,)) as cursor:
            if (await cursor.fetchone())[0] >= max_radars:
                raise RadarLimitError(f"Достигнут лимит радаров ({max_radars})")
        cursor = await db.execute(
            "INSERT INTO searches (user_id, title, url, stop_words, search_in_title_only, min_discount_percent, max_age_minutes) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (user_id, title, url, json.dumps([], ensure_ascii=False), search_in_title_only, max_age_minutes)
        )
        search_id = cursor.lastrowid
        await log_radar_history(db, user_id, search_id, "create", title, url)
        if initial_items is not None:
            await db.executemany(
                "INSERT OR IGNORE INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)",
                [(search_id, str(item_id), price) for item_id, price in initial_items],
            )
            await db.execute("UPDATE searches SET initialized_at = datetime('now') WHERE id = ?", (search_id,))
            try:
                norm_url = AvitoParser.normalize_url(url)
            except Exception:
                norm_url = str(url).strip()
            if norm_url:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO source_scan_state (normalized_url, last_top_ids, processed_at)
                    VALUES (?, '', ?)
                    """,
                    (norm_url, time.time()),
                )
        else:
            await db.execute("UPDATE searches SET initialized_at = NULL WHERE id = ?", (search_id,))
        await db.commit()
        return search_id


async def initialize_search_snapshot(search_id: int, items) -> bool:
    """An empty baseline is valid; capture it atomically and never overwrite it."""
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT initialized_at FROM searches WHERE id = ?", (search_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None or row[0] is not None:
            await db.commit()
            return False
        await db.executemany(
            "INSERT OR IGNORE INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)",
            [(search_id, str(item_id), price) for item_id, price in items],
        )
        await db.execute("UPDATE searches SET initialized_at = datetime('now') WHERE id = ?", (search_id,))
        await db.commit()
        return True


async def update_search_max_age(search_id: int, user_id: int, max_age_minutes: int) -> bool:
    clamped_val = min(30, max(1, int(max_age_minutes)))
    async with writer_connection() as db:
        cursor = await db.execute(
            "UPDATE searches SET max_age_minutes = ? WHERE id = ? AND user_id = ?",
            (clamped_val, search_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_search_title_scope(search_id: int, user_id: int, search_in_title_only: int, new_url: str) -> bool:
    async with writer_connection() as db:
        cursor = await db.execute(
            "UPDATE searches SET search_in_title_only = ?, url = ? WHERE id = ? AND user_id = ?",
            (search_in_title_only, new_url, search_id, user_id)
        )
        if cursor.rowcount > 0:
            await log_radar_history(
                db, user_id, search_id, "scope", url=new_url,
                detail="только в заголовках" if search_in_title_only else "заголовок и текст",
            )
        await db.commit()
        return cursor.rowcount > 0


async def rename_search(search_id: int, user_id: int, title: str) -> bool:
    async with writer_connection() as db:
        cursor = await db.execute(
            "UPDATE searches SET title = ? WHERE id = ? AND user_id = ?",
            (title.strip()[:100], search_id, user_id),
        )
        if cursor.rowcount > 0:
            await log_radar_history(db, user_id, search_id, "rename", title.strip()[:100])
        await db.commit()
        return cursor.rowcount > 0


RADAR_HISTORY_LABELS = {
    "create": "создан",
    "delete": "удалён",
    "url": "новая ссылка",
    "rename": "переименован",
    "scope": "область поиска",
    "pause": "пауза",
    "resume": "запущен",
    "limit_pause": "пауза: лимит тарифа",
    "limit_resume": "снова в работе: место в тарифе",
    "blocked_pause": "пауза: бот заблокирован",
    "blocked_resume": "снова в работе: бот разблокирован",
}


async def log_radar_history(db, user_id: int, search_id: int, action: str, title: Optional[str] = None,
                            url: Optional[str] = None, detail: str = "") -> None:
    """One row per radar change, inside the caller's transaction. Missing title/url are taken from the radar."""
    if title is None or url is None:
        async with db.execute("SELECT title, url FROM searches WHERE id = ?", (search_id,)) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            title = row[0] if title is None else title
            url = row[1] if url is None else url
    await db.execute(
        "INSERT INTO radar_history (user_id, search_id, action, title, url, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (int(user_id), int(search_id), action, str(title or "")[:100], str(url or "")[:500], str(detail or "")[:200]),
    )


async def get_radar_history(user_id: int, limit: int = 300) -> List[Dict[str, Any]]:
    """Newest first."""
    async with reader_connection() as db:
        async with db.execute(
            "SELECT * FROM radar_history WHERE user_id = ? ORDER BY id DESC LIMIT ?", (int(user_id), int(limit))
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_ever_subscribed_user_ids() -> set[int]:
    """Anyone who ever had access: a trial, a paid order or an admin grant."""
    async with reader_connection() as db:
        async with db.execute("""
            SELECT user_id FROM users
            WHERE COALESCE(trial_used, 0) = 1 OR COALESCE(is_allowed, 0) = 1 OR subscription_until IS NOT NULL
            UNION
            SELECT user_id FROM stars_orders WHERE status IN ('paid', 'refunding')
        """) as cursor:
            return {int(row[0]) for row in await cursor.fetchall()}


async def find_lot_traces(item_id: str) -> dict[str, list[dict]]:
    """Where a lot left marks: radars that remember it as seen (with when) and notifications still waiting."""
    async with reader_connection() as db:
        async with db.execute("""
            SELECT si.search_id, si.created_at, s.title, s.user_id, s.is_active
            FROM seen_items si LEFT JOIN searches s ON s.id = si.search_id
            WHERE si.item_id = ? ORDER BY si.created_at
        """, (str(item_id),)) as cursor:
            seen = [dict(row) for row in await cursor.fetchall()]
        async with db.execute("""
            SELECT e.user_id, e.status, e.attempt_count, e.queued_at, e.sent_at, e.expired_at, e.failed_at,
                   e.last_error, r.search_id
            FROM notification_events e
            LEFT JOIN notification_dedupe_radars r ON r.user_id = e.user_id AND r.item_id = e.item_id
            WHERE e.item_id = ? ORDER BY e.queued_at
        """, (str(item_id),)) as cursor:
            deliveries = [dict(row) for row in await cursor.fetchall()]
    return {"seen": seen, "deliveries": deliveries}


PAID_STATUSES = "('paid', 'refunding')"


async def log_admin_action(admin_id: int, user_id: Optional[int], action: str, detail: str = "") -> None:
    try:
        async with writer_connection() as db:
            await db.execute(
                "INSERT INTO admin_actions (admin_id, user_id, action, detail) VALUES (?, ?, ?, ?)",
                (int(admin_id), int(user_id) if user_id is not None else None, action[:60], str(detail or "")[:300]),
            )
            await db.commit()
    except Exception:
        logger.warning("Не удалось записать действие администратора", exc_info=True)


async def get_admin_actions(user_id: Optional[int] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """Newest first; user_id=None gives the whole journal."""
    query = "SELECT * FROM admin_actions" + (" WHERE user_id = ?" if user_id is not None else "") + " ORDER BY id DESC LIMIT ?"
    params = ((int(user_id),) if user_id is not None else ()) + (int(limit),)
    async with reader_connection() as db:
        async with db.execute(query, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def set_admin_note(user_id: int, note: str) -> None:
    async with writer_connection() as db:
        await db.execute("UPDATE users SET admin_note = ? WHERE user_id = ?", (str(note or "")[:500], int(user_id)))
        await db.commit()


_TOUCHED: dict[int, float] = {}
TOUCH_INTERVAL = 300.0


async def touch_user(user_id: int) -> None:
    """Last time the user did anything in the bot; written at most once per TOUCH_INTERVAL."""
    now = time.monotonic()
    if now - _TOUCHED.get(int(user_id), -TOUCH_INTERVAL) < TOUCH_INTERVAL:
        return
    _TOUCHED[int(user_id)] = now
    try:
        async with writer_connection() as db:
            await db.execute("UPDATE users SET last_seen_at = datetime('now') WHERE user_id = ?", (int(user_id),))
            await db.commit()
    except Exception:
        logger.debug("Не удалось обновить last_seen_at", exc_info=True)


async def get_user_activity(user_id: int) -> Dict[str, Any]:
    """For the admin card: lots delivered, payments, whether the user blocked the bot."""
    now = time.time()
    async with reader_connection() as db:
        async with db.execute(
            "SELECT COUNT(*), MAX(sent_at), SUM(CASE WHEN sent_at > ? THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN sent_at > ? THEN 1 ELSE 0 END) FROM notification_events WHERE user_id = ? AND status = 'sent'",
            (now - 86400, now - 7 * 86400, int(user_id)),
        ) as cursor:
            total, last_sent, day, week = await cursor.fetchone()
        async with db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(total_amount), 0), MAX(paid_at) FROM stars_orders "
            f"WHERE user_id = ? AND status IN {PAID_STATUSES}",
            (int(user_id),),
        ) as cursor:
            paid_count, paid_total, last_paid = await cursor.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM searches WHERE user_id = ? AND pause_reason = ?", (int(user_id), PAUSE_BLOCKED)
        ) as cursor:
            blocked = (await cursor.fetchone())[0] > 0
    return {
        "lots_total": int(total or 0), "lots_day": int(day or 0), "lots_week": int(week or 0), "last_sent": last_sent,
        "paid_count": int(paid_count or 0), "paid_total": int(paid_total or 0), "last_paid": last_paid,
        "blocked_bot": bool(blocked),
    }


async def get_money_stats() -> Dict[str, Any]:
    """Revenue by period, the trial → payment funnel of the last 30 days, renewals and churn."""
    async with reader_connection() as db:
        revenue = {}
        for key, since in (("day", "-1 day"), ("week", "-7 days"), ("month", "-30 days"), ("all", "-100 years")):
            async with db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(total_amount), 0), COUNT(DISTINCT user_id) FROM stars_orders "
                f"WHERE status IN {PAID_STATUSES} AND paid_at > datetime('now', ?)",
                (since,),
            ) as cursor:
                revenue[key] = tuple(int(v or 0) for v in await cursor.fetchone())
        async with db.execute(
            f"SELECT tariff_plan, COUNT(*), COALESCE(SUM(total_amount), 0) FROM stars_orders "
            f"WHERE status IN {PAID_STATUSES} AND paid_at > datetime('now', '-30 days') GROUP BY tariff_plan"
        ) as cursor:
            by_plan = [(row[0], int(row[1]), int(row[2])) for row in await cursor.fetchall()]
        async with db.execute(f"""
            SELECT COUNT(*),
                   SUM(CASE WHEN COALESCE(u.trial_used, 0) = 1 OR u.subscription_until IS NOT NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN EXISTS (SELECT 1 FROM stars_orders o WHERE o.user_id = u.user_id
                                         AND o.status IN {PAID_STATUSES}) THEN 1 ELSE 0 END)
            FROM users u WHERE u.created_at > datetime('now', '-30 days')
        """) as cursor:
            new_users, tried, paid = (int(v or 0) for v in await cursor.fetchone())
        async with db.execute(f"""
            SELECT COUNT(*), SUM(CASE WHEN n >= 2 THEN 1 ELSE 0 END)
            FROM (SELECT user_id, COUNT(*) AS n FROM stars_orders WHERE status IN {PAID_STATUSES} GROUP BY user_id)
        """) as cursor:
            payers, renewed = (int(v or 0) for v in await cursor.fetchone())
        async with db.execute("""
            SELECT SUM(CASE WHEN is_allowed = 1 THEN 1 ELSE 0 END), SUM(CASE WHEN is_allowed = 0 THEN 1 ELSE 0 END)
            FROM users WHERE subscription_until > datetime('now')
        """) as cursor:
            active_paid, active_trial = (int(v or 0) for v in await cursor.fetchone())
        async with db.execute(f"""
            SELECT COUNT(*), SUM(CASE WHEN EXISTS (SELECT 1 FROM stars_orders o WHERE o.user_id = u.user_id
                                                   AND o.status IN {PAID_STATUSES}) THEN 1 ELSE 0 END)
            FROM users u WHERE u.subscription_until <= datetime('now') AND u.subscription_until > datetime('now', '-30 days')
        """) as cursor:
            lapsed, lapsed_paid = (int(v or 0) for v in await cursor.fetchone())
    return {
        "revenue": revenue, "by_plan": by_plan,
        "funnel": {"new": new_users, "tried": tried, "paid": paid},
        "payers": payers, "renewed": renewed,
        "active_paid": active_paid, "active_trial": active_trial,
        "lapsed": lapsed, "lapsed_paid": lapsed_paid,
    }


async def count_paid_orders(user_id: int) -> int:
    async with reader_connection() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM stars_orders WHERE user_id = ? AND status IN {PAID_STATUSES}", (int(user_id),)
        ) as cursor:
            return int((await cursor.fetchone())[0])


async def count_radar_lots(search_id: int, hours: int = 24) -> int:
    """Lots this radar delivered in the last hours."""
    async with reader_connection() as db:
        async with db.execute("""
            SELECT COUNT(*) FROM notification_dedupe_radars r
            JOIN notification_events e ON e.user_id = r.user_id AND e.item_id = r.item_id
            WHERE r.search_id = ? AND e.status = 'sent' AND e.sent_at > ?
        """, (int(search_id), time.time() - hours * 3600)) as cursor:
            return int((await cursor.fetchone())[0])


async def get_paid_user_ids() -> set[int]:
    async with reader_connection() as db:
        async with db.execute("SELECT DISTINCT user_id FROM stars_orders WHERE status IN ('paid', 'refunding')") as cursor:
            return {int(row[0]) for row in await cursor.fetchall()}


async def replace_search_url(search_id: int, user_id: int, url: str, search_in_title_only: int,
                             *, title: Optional[str] = None, initial_items=None) -> bool:
    """New search link for an existing radar: settings and stop words stay, the old baseline is dropped.
    initial_items=None defers calibration to the first scan, like add_search."""
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "UPDATE searches SET url = ?, search_in_title_only = ?, title = COALESCE(?, title) WHERE id = ? AND user_id = ?",
            (url, search_in_title_only, title, search_id, user_id),
        )
        if cursor.rowcount == 0:
            await db.commit()
            return False
        await log_radar_history(db, user_id, search_id, "url")
        await db.execute("DELETE FROM seen_items WHERE search_id = ?", (search_id,))
        await db.execute("DELETE FROM notification_outbox WHERE search_id = ?", (search_id,))
        if initial_items is not None:
            await db.executemany(
                "INSERT OR IGNORE INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)",
                [(search_id, str(item_id), price) for item_id, price in initial_items],
            )
            await db.execute("UPDATE searches SET initialized_at = datetime('now') WHERE id = ?", (search_id,))
        else:
            await db.execute("UPDATE searches SET initialized_at = NULL WHERE id = ?", (search_id,))
        await db.commit()
        return True


NIGHT_OFF, NIGHT_SILENT, NIGHT_HOLD = 0, 1, 2
# user_id -> (mode, from_hour, to_hour), hours in MSK. Off by default; the user turns it on in the profile.
_NIGHT_MODE_CACHE: dict[int, tuple[int, int, int]] = {}


async def set_night_settings(user_id: int, *, mode: Optional[int] = None,
                             from_hour: Optional[int] = None, to_hour: Optional[int] = None) -> tuple[int, int, int]:
    current = await get_night_settings(user_id)
    new = (
        current[0] if mode is None else int(mode),
        current[1] if from_hour is None else int(from_hour) % 24,
        current[2] if to_hour is None else int(to_hour) % 24,
    )
    async with writer_connection() as db:
        await db.execute(
            "UPDATE users SET night_mode = ?, night_from = ?, night_to = ? WHERE user_id = ?", (*new, user_id),
        )
        await db.commit()
    _NIGHT_MODE_CACHE[int(user_id)] = new
    return new


async def get_night_settings(user_id: int) -> tuple[int, int, int]:
    """Cached per user: it is read for every delivered lot and changes only from the profile."""
    uid = int(user_id)
    if uid not in _NIGHT_MODE_CACHE:
        async with reader_connection() as db:
            async with db.execute("SELECT night_mode, night_from, night_to FROM users WHERE user_id = ?", (uid,)) as cursor:
                row = await cursor.fetchone()
        _NIGHT_MODE_CACHE[uid] = (
            (int(row[0] or 0), int(row[1] if row[1] is not None else 23), int(row[2] if row[2] is not None else 8))
            if row else (NIGHT_OFF, 23, 8)
        )
    return _NIGHT_MODE_CACHE[uid]


# user_id -> group chat that gets a copy of every lot (Средний and Полный), or None.
_MIRROR_CHAT_CACHE: dict[int, Optional[int]] = {}


async def set_mirror_chat(user_id: int, chat_id: Optional[int]) -> None:
    async with writer_connection() as db:
        await db.execute("UPDATE users SET mirror_chat_id = ? WHERE user_id = ?", (chat_id, int(user_id)))
        await db.commit()
    _MIRROR_CHAT_CACHE[int(user_id)] = int(chat_id) if chat_id else None


async def get_mirror_chat(user_id: int) -> Optional[int]:
    """Cached per user: it is read for every delivered lot and changes only from the profile or the group."""
    uid = int(user_id)
    if uid not in _MIRROR_CHAT_CACHE:
        async with reader_connection() as db:
            async with db.execute("SELECT mirror_chat_id FROM users WHERE user_id = ?", (uid,)) as cursor:
                row = await cursor.fetchone()
        _MIRROR_CHAT_CACHE[uid] = int(row[0]) if row and row[0] else None
    return _MIRROR_CHAT_CACHE[uid]


async def get_users_with_subscription() -> List[Dict[str, Any]]:
    """Users whose access ends at some point: candidates for renewal reminders."""
    async with reader_connection() as db:
        async with db.execute(
            "SELECT * FROM users WHERE subscription_until IS NOT NULL AND subscription_until > datetime('now', '-1 day')"
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def set_reminded_key(user_id: int, key: str) -> None:
    async with writer_connection() as db:
        await db.execute("UPDATE users SET reminded_key = ? WHERE user_id = ?", (key, user_id))
        await db.commit()


async def get_daily_report_stats(hours: int = 24) -> Dict[str, Any]:
    since = f"-{int(hours)} hours"
    async with reader_connection() as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE created_at > datetime('now', ?)", (since,)) as cur:
            new_users = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT tariff_plan, COUNT(*), COALESCE(SUM(total_amount), 0) FROM stars_orders "
            "WHERE paid_at > datetime('now', ?) AND status IN ('paid', 'refunding') GROUP BY tariff_plan",
            (since,),
        ) as cur:
            payments = [(r[0], int(r[1]), int(r[2])) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE subscription_until > datetime('now') "
            "AND subscription_until < datetime('now', '+3 days') AND is_allowed = 1"
        ) as cur:
            expiring = (await cur.fetchone())[0]
    return {"new_users": new_users, "payments": payments, "expiring_3d": expiring}


async def get_search(search_id: int) -> Optional[Dict[str, Any]]:
    async with reader_connection() as db:
        async with db.execute(
            """
            SELECT s.*, u.username, u.first_name, u.tariff_plan, u.is_allowed, u.trial_used, u.subscription_until
            FROM searches s
            LEFT JOIN users u ON s.user_id = u.user_id
            WHERE s.id = ?
            """,
            (search_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            data = dict(row)
            data["stop_words"] = json.loads(data["stop_words"]) if data["stop_words"] else []
            return data


async def get_user_searches(user_id: int) -> List[Dict[str, Any]]:
    async with reader_connection() as db:
        async with db.execute("SELECT * FROM searches WHERE user_id = ? ORDER BY id DESC", (user_id,)) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                data = dict(row)
                data["stop_words"] = json.loads(data["stop_words"]) if data["stop_words"] else []
                results.append(data)
            return results


async def count_user_searches(user_id: int) -> int:
    async with reader_connection() as db:
        async with db.execute("SELECT COUNT(*) FROM searches WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def toggle_search_active(search_id: int, user_id: int) -> Optional[bool]:
    max_radars = await get_user_max_radars(user_id)
    async with writer_connection() as db:
        async with db.execute("SELECT is_active FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row[0] == 1 else 1

        if new_status and user_id not in ADMIN_IDS:
            async with db.execute(
                "SELECT COUNT(*) FROM searches WHERE user_id = ? AND is_active = 1", (user_id,)
            ) as cursor:
                if (await cursor.fetchone())[0] >= max_radars:
                    raise RadarLimitError(f"Тариф вмещает работающих радаров: {max_radars}")

        await db.execute(
            "UPDATE searches SET is_active = ?, pause_reason = '' WHERE id = ? AND user_id = ?",
            (new_status, search_id, user_id)
        )
        await log_radar_history(db, user_id, search_id, "resume" if new_status else "pause")
        await db.commit()
        return bool(new_status)


async def is_search_active(search_id: int) -> bool:
    async with reader_connection() as db:
        async with db.execute("SELECT 1 FROM searches WHERE id = ? AND is_active = 1", (search_id,)) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def can_deliver_to_search(search_id: int, user_id: int) -> bool:
    placeholders = ",".join("?" for _ in ADMIN_IDS)
    async with reader_connection() as db:
        async with db.execute(f"""
            SELECT 1 FROM searches s JOIN users u ON u.user_id = s.user_id
            WHERE s.id = ? AND s.user_id = ? AND s.is_active = 1
              AND (u.user_id IN ({placeholders})
                   OR (u.is_allowed = 1 AND u.subscription_until IS NULL)
                   OR u.subscription_until > datetime('now'))
        """, (search_id, user_id, *ADMIN_IDS)) as cursor:
            return await cursor.fetchone() is not None


async def get_deliverable_search_ids(search_ids: list[int]) -> set[int]:
    """Batch form of can_deliver_to_search: active radars whose owner is allowed, in one query."""
    ids = sorted({int(sid) for sid in search_ids})
    if not ids:
        return set()
    id_marks = ",".join("?" for _ in ids)
    admin_marks = ",".join("?" for _ in ADMIN_IDS) or "NULL"
    async with reader_connection() as db:
        async with db.execute(f"""
            SELECT s.id FROM searches s LEFT JOIN users u ON u.user_id = s.user_id
            WHERE s.id IN ({id_marks}) AND s.is_active = 1
              AND (s.user_id IN ({admin_marks})
                   OR (u.is_allowed = 1 AND u.subscription_until IS NULL)
                   OR u.subscription_until > datetime('now'))
        """, (*ids, *ADMIN_IDS)) as cursor:
            return {int(row[0]) for row in await cursor.fetchall()}


async def delete_search(search_id: int, user_id: int) -> bool:
    """
    Удаляет радар пользователя и связанные просмотренные лоты и записи дедупликации.
    Если все сопоставленные радары ожидающего события удалены, переводит событие в expired.
    """
    now_epoch = time.time()
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT title, url FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id)) as cur:
            before = await cur.fetchone()
        cursor = await db.execute("DELETE FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id))
        if cursor.rowcount > 0:
            await log_radar_history(db, user_id, search_id, "delete", before[0] if before else "", before[1] if before else "")
            await db.execute("DELETE FROM seen_items WHERE search_id = ?", (search_id,))
            await db.execute("DELETE FROM notification_outbox WHERE search_id = ?", (search_id,))

            async with db.execute(
                "SELECT DISTINCT item_id FROM notification_dedupe_radars WHERE search_id = ? AND user_id = ?",
                (search_id, user_id),
            ) as cur:
                affected_items = [r[0] for r in await cur.fetchall()]

            await db.execute(
                "DELETE FROM notification_dedupe_radars WHERE search_id = ? AND user_id = ?",
                (search_id, user_id),
            )

            for iid in affected_items:
                async with db.execute(
                    "SELECT COUNT(*) FROM notification_dedupe_radars WHERE user_id = ? AND item_id = ?",
                    (user_id, iid),
                ) as c2:
                    rem = (await c2.fetchone())[0]
                if rem == 0:
                    await db.execute(
                        """
                        UPDATE notification_events
                        SET status = 'expired', expired_at = ?, lease_until = NULL, last_error = 'radar_deleted'
                        WHERE user_id = ? AND item_id = ? AND status IN ('pending', 'sending')
                        """,
                        (now_epoch, user_id, iid),
                    )
                    await db.execute(
                        """
                        UPDATE notification_dedupe
                        SET status = 'expired', updated_at = ?
                        WHERE user_id = ? AND item_id = ? AND status = 'queued'
                        """,
                        (now_epoch, user_id, iid),
                    )

            await db.commit()
            return True
        await db.commit()
        return False


async def add_stop_word_to_search(search_id: int, word: str, user_id: int) -> bool:
    word = word.strip().lower()
    if not word:
        return False
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT stop_words FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await db.commit()
                return False
            stop_words = json.loads(row[0]) if row[0] else []

        if word not in stop_words:
            stop_words.append(word)
            await db.execute(
                "UPDATE searches SET stop_words = ? WHERE id = ? AND user_id = ?",
                (json.dumps(stop_words, ensure_ascii=False), search_id, user_id)
            )
        await db.commit()
        return True


async def remove_stop_word_from_search(search_id: int, word: str, user_id: int) -> bool:
    word = word.strip().lower()
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT stop_words FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await db.commit()
                return False
            stop_words = json.loads(row[0]) if row[0] else []

        if word in stop_words:
            stop_words.remove(word)
            await db.execute(
                "UPDATE searches SET stop_words = ? WHERE id = ? AND user_id = ?",
                (json.dumps(stop_words, ensure_ascii=False), search_id, user_id)
            )
            await db.commit()
            return True
        await db.commit()
        return False


async def update_search_template(search_id: int, user_id: int, template: Optional[str]):
    async with writer_connection() as db:
        await db.execute(
            "UPDATE searches SET template = ? WHERE id = ? AND user_id = ?",
            (template.strip() if template else None, search_id, user_id)
        )
        await db.commit()


async def get_all_active_searches() -> List[Dict[str, Any]]:
    async with reader_connection() as db:
        admin_placeholders = ",".join("?" for _ in ADMIN_IDS)
        query = f"""
            SELECT s.*, u.username, u.first_name, u.tariff_plan, u.is_allowed, u.trial_used, u.subscription_until,
                   EXISTS (
                       SELECT 1 FROM stars_orders so
                       WHERE so.user_id = u.user_id
                         AND so.status = 'paid'
                         AND so.tariff_plan = 'test'
                         AND so.subscription_after = u.subscription_until
                   ) AS is_test_order,
                   CAST(strftime('%s', s.created_at) AS REAL) AS created_epoch,
                   CAST(strftime('%s', s.initialized_at) AS REAL) AS initialized_epoch
            FROM searches s 
            JOIN users u ON s.user_id = u.user_id 
            WHERE s.is_active = 1
              AND (u.user_id IN ({admin_placeholders}) 
                    OR (u.is_allowed = 1 AND u.subscription_until IS NULL)
                   OR u.subscription_until > datetime('now'))
        """
        async with db.execute(query, ADMIN_IDS) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                data = dict(row)
                data["stop_words"] = json.loads(data["stop_words"]) if data["stop_words"] else []
                eff_tariff, priority, delay = classify_user_tariff(
                    data["user_id"],
                    data.get("tariff_plan"),
                    is_allowed=data.get("is_allowed", 1),
                    is_test_order=bool(data.get("is_test_order")),
                )
                data["effective_tariff"] = eff_tariff
                data["delivery_priority"] = priority
                data["delivery_delay"] = delay
                results.append(data)
            return results


async def add_blocked_seller(user_id: int, seller_name: str, seller_key: str = "") -> bool:
    name = seller_name.strip()
    if not name or name.lower() in {"продавец", "пользователь", "авито", "компания", "частное лицо"}:
        return False
    async with writer_connection() as db:
        await db.execute(
            "INSERT OR IGNORE INTO blocked_sellers (user_id, seller_name, seller_key) VALUES (?, ?, ?)",
            (user_id, name, (seller_key or "").strip())
        )
        await db.commit()
    invalidate_blacklist_cache(user_id)
    return True


async def get_blocked_sellers(user_id: int) -> set[str]:
    res = await get_blocked_sellers_for_users([user_id])
    return res.get(int(user_id), set())


async def get_blocked_sellers_for_users(user_ids: Sequence[int]) -> dict[int, set[str]]:
    """
    Пакетное чтение черного списка продавцов с in-memory TTL кэшем 45 секунд.
    При добавлении/удалении продавца кэш инвалидируется мгновенно.
    """
    unique_ids = sorted({int(uid) for uid in user_ids if uid is not None})
    if not unique_ids:
        return {}

    now = time.monotonic()
    ttl = float(config.BLACKLIST_CACHE_TTL)
    result: dict[int, set[str]] = {}
    missing_ids: list[int] = []

    for uid in unique_ids:
        cached = _BLACKLIST_CACHE.get(uid)
        if cached is not None and (now - cached[0]) <= ttl:
            result[uid] = set(cached[1])
        else:
            missing_ids.append(uid)

    if missing_ids:
        placeholders = ",".join("?" for _ in missing_ids)
        fetched: dict[int, set[str]] = {uid: set() for uid in missing_ids}
        async with reader_connection() as db:
            async with db.execute(
                f"SELECT user_id, seller_name, seller_key FROM blocked_sellers WHERE user_id IN ({placeholders})",
                tuple(missing_ids),
            ) as cur:
                for row in await cur.fetchall():
                    uid = int(row[0])
                    token = radar_filters.seller_token(row[2]) if row[2] else str(row[1] or "").lower().strip()
                    if token:
                        fetched[uid].add(token)
        for uid, sset in fetched.items():
            _BLACKLIST_CACHE[uid] = (now, set(sset))
            result[uid] = sset

    return result


async def get_user_blocked_sellers_list(user_id: int) -> List[Dict[str, Any]]:
    async with reader_connection() as db:
        async with db.execute(
            "SELECT id, seller_name, created_at FROM blocked_sellers WHERE user_id = ? ORDER BY id DESC",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def remove_blocked_seller_by_id(user_id: int, block_id: int) -> bool:
    async with writer_connection() as db:
        cursor = await db.execute(
            "DELETE FROM blocked_sellers WHERE id = ? AND user_id = ?",
            (block_id, user_id)
        )
        await db.commit()
        changed = cursor.rowcount > 0
    if changed:
        invalidate_blacklist_cache(user_id)
    return changed


async def get_group_seen_state(search_ids: Sequence[int], item_ids: Sequence[str]) -> dict[int, set[str]]:
    """
    Один пакетный SQL-запрос seen_items для всех радаров одного нормализованного URL.
    Использует составной PRIMARY KEY(search_id, item_id).
    """
    sids = sorted({int(s) for s in search_ids if s is not None})
    iids = sorted({str(i).strip() for i in item_ids if str(i).strip()})
    result: dict[int, set[str]] = {sid: set() for sid in sids}
    if not sids or not iids:
        return result

    s_placeholders = ",".join("?" for _ in sids)
    i_placeholders = ",".join("?" for _ in iids)
    query = (
        f"SELECT search_id, item_id FROM seen_items "
        f"WHERE search_id IN ({s_placeholders}) AND item_id IN ({i_placeholders})"
    )
    params = tuple(sids) + tuple(iids)
    async with reader_connection() as db:
        async with db.execute(query, params) as cur:
            for row in await cur.fetchall():
                sid = int(row[0])
                if sid in result:
                    result[sid].add(str(row[1]))
    return result


async def get_seen_items_with_prices(search_id: int, item_ids: Optional[Any] = None) -> Dict[str, int]:
    async with reader_connection() as db:
        if item_ids is not None:
            ids = [str(x) for x in item_ids if str(x).strip()]
            if not ids:
                return {}
            ids = ids[:500]
            placeholders = ",".join("?" for _ in ids)
            query = f"SELECT item_id, COALESCE(last_price, 0) FROM seen_items WHERE search_id = ? AND item_id IN ({placeholders})"
            params = [search_id] + ids
        else:
            query = "SELECT item_id, COALESCE(last_price, 0) FROM seen_items WHERE search_id = ?"
            params = [search_id]

        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return {str(r[0]): int(r[1] or 0) for r in rows}


async def get_seen_item_ids_for_search(search_id: int) -> set:
    async with reader_connection() as db:
        async with db.execute(
            "SELECT item_id FROM seen_items WHERE search_id = ?",
            (search_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return {r[0] for r in rows}


async def is_item_seen(search_id: int, item_id: str) -> bool:
    async with reader_connection() as db:
        async with db.execute(
            "SELECT 1 FROM seen_items WHERE search_id = ? AND item_id = ?",
            (search_id, str(item_id))
        ) as cursor:
            return await cursor.fetchone() is not None


async def mark_item_seen(search_id: int, item_id: str, price: int = 0):
    async with writer_connection() as db:
        await db.execute(
            """
            INSERT INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)
            ON CONFLICT(search_id, item_id) DO UPDATE SET last_price = excluded.last_price
            """,
            (search_id, str(item_id), int(price or 0))
        )
        await db.commit()


async def mark_items_seen_bulk(search_id: int, items_data: list):
    if not items_data:
        return
    async with writer_connection() as db:
        records = []
        for entry in items_data:
            if isinstance(entry, (tuple, list)):
                iid, price = entry[0], entry[1]
            else:
                iid, price = entry, 0
            records.append((search_id, str(iid), int(price or 0)))

        await db.executemany(
            """
            INSERT INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)
            ON CONFLICT(search_id, item_id) DO UPDATE SET last_price = excluded.last_price
            """,
            records
        )
        await db.commit()


async def update_seen_item_price(search_id: int, item_id: str, new_price: int):
    async with writer_connection() as db:
        await db.execute(
            "UPDATE seen_items SET last_price = ? WHERE search_id = ? AND item_id = ?",
            (int(new_price or 0), search_id, str(item_id))
        )
        await db.commit()


SEARCH_FILTER_COLUMNS = frozenset({
    "price_min", "price_max", "must_words", "delivery_only", "hide_promoted", "reply_template",
})


async def update_search_filters(search_id: int, user_id: int, **fields: Any) -> bool:
    unknown = set(fields) - SEARCH_FILTER_COLUMNS
    if unknown or not fields:
        raise ValueError(f"Неизвестные поля фильтра: {sorted(unknown)}")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    async with writer_connection() as db:
        cursor = await db.execute(
            f"UPDATE searches SET {assignments} WHERE id = ? AND user_id = ?",
            (*fields.values(), search_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_search_discount_percent(search_id: int, user_id: int, percent: int) -> bool:
    """Колонка сохранена для совместимости схемы; повторные алерты о скидках отключены."""
    async with writer_connection() as db:
        cursor = await db.execute(
            "UPDATE searches SET min_discount_percent = ? WHERE id = ? AND user_id = ?",
            (int(percent), search_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_source_scan_state(normalized_url: str) -> Optional[Dict[str, Any]]:
    states = await get_source_scan_states([normalized_url])
    return states.get(normalized_url)


async def get_source_scan_states(normalized_urls: Sequence[str]) -> dict[str, dict[str, Any]]:
    urls = sorted({str(u).strip() for u in normalized_urls if str(u).strip()})
    if not urls:
        return {}
    placeholders = ",".join("?" for _ in urls)
    result: dict[str, dict[str, Any]] = {}
    async with reader_connection() as db:
        async with db.execute(
            f"SELECT normalized_url, last_top_ids, processed_at, COALESCE(failure_streak, 0) AS failure_streak "
            f"FROM source_scan_state WHERE normalized_url IN ({placeholders})",
            tuple(urls),
        ) as cur:
            for row in await cur.fetchall():
                top_str = str(row["last_top_ids"] or "")
                top_tuple = tuple(x for x in top_str.split(",") if x)
                result[str(row["normalized_url"])] = {
                    "normalized_url": str(row["normalized_url"]),
                    "last_top_ids": top_tuple,
                    "processed_at": float(row["processed_at"]),
                    "failure_streak": int(row["failure_streak"] or 0),
                }
    return result


async def load_all_source_scan_states() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    try:
        async with reader_connection() as db:
            async with db.execute(
                "SELECT normalized_url, last_top_ids, processed_at, COALESCE(failure_streak, 0) AS failure_streak FROM source_scan_state"
            ) as cur:
                for row in await cur.fetchall():
                    top_str = str(row["last_top_ids"] or "")
                    top_tuple = tuple(x for x in top_str.split(",") if x)
                    result[str(row["normalized_url"])] = {
                        "normalized_url": str(row["normalized_url"]),
                        "last_top_ids": top_tuple,
                        "processed_at": float(row["processed_at"]),
                        "failure_streak": int(row["failure_streak"] or 0),
                    }
    except sqlite3.OperationalError:
        return {}
    return result


async def upsert_source_scan_states(entries: Sequence[tuple[str, Sequence[str], float]]) -> None:
    if not entries:
        return
    rows = [
        (str(url), ",".join(str(x) for x in top_ids[:3] if x), float(proc_at), 0)
        for url, top_ids, proc_at in entries
        if url
    ]
    if not rows:
        return
    try:
        async with writer_connection() as db:
            await db.executemany(
                """
                INSERT INTO source_scan_state (normalized_url, last_top_ids, processed_at, failure_streak, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(normalized_url) DO UPDATE SET
                    last_top_ids = excluded.last_top_ids,
                    processed_at = excluded.processed_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                rows,
            )
            await db.commit()
    except sqlite3.OperationalError:
        pass


async def upsert_source_scan_states_batch(entries: Sequence[tuple[str, float, Sequence[str], int]]) -> None:
    if not entries:
        return
    rows = [
        (str(url), ",".join(str(x) for x in top_ids[:3] if x), float(proc_at), int(streak or 0))
        for url, proc_at, top_ids, streak in entries
        if url
    ]
    if not rows:
        return
    try:
        async with writer_connection() as db:
            await db.executemany(
                """
                INSERT INTO source_scan_state (normalized_url, last_top_ids, processed_at, failure_streak, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(normalized_url) DO UPDATE SET
                    last_top_ids = excluded.last_top_ids,
                    processed_at = excluded.processed_at,
                    failure_streak = excluded.failure_streak,
                    updated_at = CURRENT_TIMESTAMP
                """,
                rows,
            )
            await db.commit()
    except sqlite3.OperationalError:
        pass


async def enqueue_group_events(
    user_events: Sequence[Dict[str, Any]],
    seen_records: Sequence[tuple[int, str, int | None]] = (),
    *,
    initialized_search_ids: Sequence[int] = (),
    scan_state_update: tuple[str, Sequence[str], float] | None = None,
    now_epoch: float | None = None,
) -> list[dict[str, Any]]:
    """
    Пакетная атомарная запись результатов обработки одного URL-источника:
    - калибровка новых радаров (initialized_at)
    - bulk insert в seen_items для отфильтрованных/калибровочных лотов
    - user-level дедупликация в notification_dedupe + агрегация радаров в notification_dedupe_radars
    - постановка в notification_events с защитой от переполнения OUTBOX_MAX_PENDING
    - обновление source_scan_state
    Возвращает список созданных/обновленных событий для немедленной подачи в PriorityQueue.
    """
    now = float(time.time() if now_epoch is None else now_epoch)
    dispatched: list[dict[str, Any]] = []

    # Нормализуем и группируем входные события по (user_id, item_id), если переданы кандидаты на уровне радаров
    normalized_events: list[dict[str, Any]] = []
    grouped_by_user_item: dict[tuple[int, str], dict[str, Any]] = {}
    for raw_ev in user_events:
        uid = int(raw_ev["user_id"])
        iid = str(raw_ev["item_id"])
        key = (uid, iid)
        radars_list: list[tuple[int, str]] = []
        if "matched_radars" in raw_ev and raw_ev["matched_radars"]:
            radars_list = [(int(r[0]), str(r[1])) for r in raw_ev["matched_radars"]]
        elif "radar_id" in raw_ev:
            radars_list = [(int(raw_ev["radar_id"]), str(raw_ev.get("radar_title") or raw_ev.get("search_title") or "Радар"))]

        tariff_val = str(raw_ev.get("tariff_plan") or raw_ev.get("tariff") or "base").lower()
        prio_val = int(raw_ev.get("priority", 30))
        q_at = float(raw_ev.get("queued_at", now))
        if "deliver_at" in raw_ev and raw_ev["deliver_at"] is not None:
            deliv_at = float(raw_ev["deliver_at"])
        else:
            deliv_at = q_at + float(raw_ev.get("delay_seconds", config.tariff_delivery_delay(tariff_val)))

        if isinstance(raw_ev.get("item_json"), str):
            item_json_str = raw_ev["item_json"]
        elif isinstance(raw_ev.get("payload_dict"), dict):
            item_json_str = json.dumps(raw_ev["payload_dict"], ensure_ascii=False)
        elif isinstance(raw_ev.get("item"), dict):
            item_dict = raw_ev["item"]
            envelope = item_dict if "item" in item_dict else {"item": item_dict}
            item_json_str = json.dumps(envelope, ensure_ascii=False)
        else:
            item_json_str = "{}"

        if key not in grouped_by_user_item:
            grouped_by_user_item[key] = {
                "user_id": uid,
                "item_id": iid,
                "matched_radars": list(radars_list),
                "tariff_plan": tariff_val,
                "priority": prio_val,
                "queued_at": q_at,
                "deliver_at": deliv_at,
                "is_catchup": bool(raw_ev.get("is_catchup", False)),
                "item_json": item_json_str,
                "raw_price": int(raw_ev.get("raw_price") or 0),
                "t_pub": raw_ev.get("t_pub"),
                "t_pub_reliable": raw_ev.get("t_pub_reliable"),
                "t_seen": float(raw_ev.get("t_seen", now)),
            }
            normalized_events.append(grouped_by_user_item[key])
        else:
            existing_entry = grouped_by_user_item[key]
            for r_pair in radars_list:
                if r_pair not in existing_entry["matched_radars"]:
                    existing_entry["matched_radars"].append(r_pair)
            if prio_val < existing_entry["priority"]:
                existing_entry["priority"] = prio_val
                existing_entry["tariff_plan"] = tariff_val
            existing_entry["deliver_at"] = min(existing_entry["deliver_at"], deliv_at)
            existing_entry["queued_at"] = min(existing_entry["queued_at"], q_at)

    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")

        if initialized_search_ids:
            for sid in set(int(x) for x in initialized_search_ids):
                await db.execute(
                    "UPDATE searches SET initialized_at = datetime('now') WHERE id = ? AND initialized_at IS NULL",
                    (sid,),
                )

        if seen_records:
            await db.executemany(
                """
                INSERT INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)
                ON CONFLICT(search_id, item_id) DO UPDATE SET last_price = COALESCE(excluded.last_price, seen_items.last_price)
                """,
                [(int(sid), str(iid), price) for sid, iid, price in seen_records],
            )

        admin_placeholders = ",".join("?" for _ in ADMIN_IDS)

        for ev in normalized_events:
            uid = int(ev["user_id"])
            iid = str(ev["item_id"])
            matched_radars: list[tuple[int, str]] = [
                (int(r[0]), str(r[1])) for r in ev.get("matched_radars", [])
            ]
            if not matched_radars:
                continue

            # Проверяем, какие из сопоставленных радаров еще активны и пользователь имеет доступ
            sids = [r[0] for r in matched_radars]
            s_placeholders = ",".join("?" for _ in sids)
            async with db.execute(
                f"""
                SELECT s.id, s.title
                FROM searches s
                JOIN users u ON u.user_id = s.user_id
                WHERE s.user_id = ? AND s.id IN ({s_placeholders}) AND s.is_active = 1
                  AND (u.user_id IN ({admin_placeholders})
                       OR (u.is_allowed = 1 AND u.subscription_until IS NULL)
                       OR u.subscription_until > datetime('now'))
                """,
                (uid, *sids, *ADMIN_IDS),
            ) as cur:
                valid_radars = [(int(r[0]), str(r[1])) for r in await cur.fetchall()]

            if not valid_radars:
                continue

            # Фильтруем радары, где этот item_id уже в seen_items
            valid_sids = [r[0] for r in valid_radars]
            vs_placeholders = ",".join("?" for _ in valid_sids)
            async with db.execute(
                f"SELECT search_id FROM seen_items WHERE item_id = ? AND search_id IN ({vs_placeholders})",
                (iid, *valid_sids),
            ) as cur:
                already_seen_sids = {int(r[0]) for r in await cur.fetchall()}

            unseen_radars = [r for r in valid_radars if r[0] not in already_seen_sids]
            if not unseen_radars:
                continue

            tariff_plan = str(ev.get("tariff_plan") or "base").lower()
            priority = int(ev.get("priority", 30))
            queued_at = float(ev.get("queued_at", now))
            deliver_at = float(ev.get("deliver_at", now))
            is_catchup = int(bool(ev.get("is_catchup", False)))
            item_json = (
                ev["item_json"]
                if isinstance(ev.get("item_json"), str)
                else json.dumps(ev.get("item") or {}, ensure_ascii=False)
            )
            raw_price = int(ev.get("raw_price") or 0)
            t_pub = ev.get("t_pub")
            t_pub_val = float(t_pub) if isinstance(t_pub, (int, float)) and t_pub > 0 else None
            t_pub_reliable = int(bool(ev.get("t_pub_reliable", t_pub_val is not None)))
            t_seen = float(ev.get("t_seen", now))

            # Проверяем user-level дедупликацию
            async with db.execute(
                "SELECT event_id, status FROM notification_dedupe WHERE user_id = ? AND item_id = ?",
                (uid, iid),
            ) as cur:
                dedupe_row = await cur.fetchone()

            if dedupe_row is not None and dedupe_row["status"] == "sent":
                # Пользователь уже получил этот item_id через другой радар — помечаем новые радары в seen_items без повторной отправки
                for sid, rtitle in unseen_radars:
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO notification_dedupe_radars (user_id, item_id, search_id, radar_title, matched_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (uid, iid, sid, rtitle, now),
                    )
                    await db.execute(
                        """
                        INSERT INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)
                        ON CONFLICT(search_id, item_id) DO UPDATE SET last_price = excluded.last_price
                        """,
                        (sid, iid, raw_price),
                    )
                continue

            # Проверяем существующее событие в notification_events
            async with db.execute(
                "SELECT id, status, send_started_at, priority, deliver_at, next_attempt_at, queued_at, tariff_plan, is_catchup "
                "FROM notification_events WHERE user_id = ? AND item_id = ?",
                (uid, iid),
            ) as cur:
                existing_ev = await cur.fetchone()

            if existing_ev is not None:
                ev_status = existing_ev["status"]
                if ev_status == "sent":
                    for sid, rtitle in unseen_radars:
                        await db.execute(
                            """
                            INSERT INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)
                            ON CONFLICT(search_id, item_id) DO UPDATE SET last_price = excluded.last_price
                            """,
                            (sid, iid, raw_price),
                        )
                    continue

                if ev_status in ("pending", "sending"):
                    # Если отправка еще не началась, агрегируем названия радаров в то же самое уведомление!
                    inserted_any = False
                    if existing_ev["send_started_at"] is None:
                        for sid, rtitle in unseen_radars:
                            c_ins = await db.execute(
                                """
                                INSERT OR IGNORE INTO notification_dedupe_radars (
                                    user_id, item_id, search_id, radar_title, matched_at
                                ) VALUES (?, ?, ?, ?, ?)
                                """,
                                (uid, iid, sid, rtitle, now),
                            )
                            if c_ins.rowcount > 0:
                                inserted_any = True

                        new_prio = min(int(existing_ev["priority"]), priority)
                        new_deliv = min(float(existing_ev["deliver_at"]), deliver_at)
                        new_next = min(float(existing_ev["next_attempt_at"]), new_deliv)
                        new_tariff = tariff_plan if priority < int(existing_ev["priority"]) else existing_ev["tariff_plan"]
                        if new_prio != existing_ev["priority"] or new_deliv != existing_ev["deliver_at"]:
                            await db.execute(
                                """
                                UPDATE notification_events
                                SET priority = ?, tariff_plan = ?, deliver_at = ?, next_attempt_at = ?
                                WHERE id = ? AND status = 'pending'
                                """,
                                (new_prio, new_tariff, new_deliv, new_next, existing_ev["id"]),
                            )
                            dispatched.append({
                                "id": int(existing_ev["id"]),
                                "user_id": uid,
                                "item_id": iid,
                                "tariff_plan": new_tariff,
                                "priority": new_prio,
                                "deliver_at": new_deliv,
                                "next_attempt_at": new_next,
                                "queued_at": float(existing_ev["queued_at"]),
                                "is_catchup": int(existing_ev["is_catchup"]),
                                " newly_created": False,
                                "aggregated": inserted_any,
                            })
                    continue

            # Контроль переполнения OUTBOX_MAX_PENDING (3000) с вытеснением по приоритету
            async with db.execute(
                "SELECT COUNT(*) FROM notification_events WHERE status = 'pending'"
            ) as cur:
                pending_count = (await cur.fetchone())[0]

            if pending_count >= config.OUTBOX_MAX_PENDING:
                # Ищем самую старую pending-запись с низшим приоритетом (сначала base=30, затем standart=20)
                async with db.execute(
                    """
                    SELECT id, user_id, item_id, priority FROM notification_events
                    WHERE status = 'pending' AND priority >= ? AND priority > 10
                    ORDER BY priority DESC, queued_at ASC, id ASC LIMIT 1
                    """,
                    (priority,),
                ) as cur:
                    victim = await cur.fetchone()
                if victim is not None:
                    await db.execute(
                        """
                        UPDATE notification_events
                        SET status = 'expired', expired_at = ?, lease_until = NULL, last_error = 'outbox_overflow_evicted'
                        WHERE id = ?
                        """,
                        (now, victim["id"]),
                    )
                    await db.execute(
                        """
                        UPDATE notification_dedupe
                        SET status = 'expired', updated_at = ?
                        WHERE user_id = ? AND item_id = ?
                        """,
                        (now, victim["user_id"], victim["item_id"]),
                    )
                else:
                    logger.warning("Очередь notification_events заполнена (%d); событие отложено до следующего скана", pending_count)
                    continue

            if existing_ev is not None:
                # Переоткрываем ранее expired/failed событие
                event_id = int(existing_ev["id"])
                await db.execute(
                    """
                    UPDATE notification_events
                    SET tariff_plan = ?, priority = ?, is_catchup = ?, crash_retry = 0,
                        status = 'pending', queued_at = ?, deliver_at = ?, next_attempt_at = ?,
                        lease_until = NULL, send_started_at = NULL, sent_at = NULL,
                        failed_at = NULL, expired_at = NULL,
                        attempt_count = 0, generic_error_count = 0, last_error = NULL,
                        item_json = ?, t_pub = ?, t_pub_reliable = ?, t_seen = ?
                    WHERE id = ?
                    """,
                    (
                        tariff_plan,
                        priority,
                        is_catchup,
                        queued_at,
                        deliver_at,
                        deliver_at,
                        item_json,
                        t_pub_val,
                        t_pub_reliable,
                        t_seen,
                        event_id,
                    ),
                )
            else:
                cur_ins = await db.execute(
                    """
                    INSERT INTO notification_events (
                        user_id, item_id, tariff_plan, priority, is_catchup, crash_retry,
                        status, queued_at, deliver_at, next_attempt_at,
                        item_json, t_pub, t_pub_reliable, t_seen
                    ) VALUES (?, ?, ?, ?, ?, 0, 'pending', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        iid,
                        tariff_plan,
                        priority,
                        is_catchup,
                        queued_at,
                        deliver_at,
                        deliver_at,
                        item_json,
                        t_pub_val,
                        t_pub_reliable,
                        t_seen,
                    ),
                )
                event_id = int(cur_ins.lastrowid)

            await db.execute(
                """
                INSERT INTO notification_dedupe (
                    user_id, item_id, event_id, status, first_queued_at, sent_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, NULL, ?)
                ON CONFLICT(user_id, item_id) DO UPDATE SET
                    event_id = excluded.event_id,
                    status = 'queued',
                    updated_at = excluded.updated_at
                """,
                (uid, iid, event_id, queued_at, now),
            )

            for sid, rtitle in unseen_radars:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO notification_dedupe_radars (
                        user_id, item_id, search_id, radar_title, matched_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (uid, iid, sid, rtitle, now),
                )

            dispatched.append({
                "id": event_id,
                "user_id": uid,
                "item_id": iid,
                "tariff_plan": tariff_plan,
                "priority": priority,
                "deliver_at": deliver_at,
                "next_attempt_at": deliver_at,
                "queued_at": queued_at,
                "is_catchup": is_catchup,
                "t_seen": t_seen,
                "newly_created": True,
                "aggregated": False,
            })

        if scan_state_update is not None:
            norm_url, top_ids, proc_at = scan_state_update
            if norm_url:
                await db.execute(
                    """
                    INSERT INTO source_scan_state (normalized_url, last_top_ids, processed_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(normalized_url) DO UPDATE SET
                        last_top_ids = excluded.last_top_ids,
                        processed_at = excluded.processed_at,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (str(norm_url), ",".join(str(x) for x in top_ids[:3] if x), float(proc_at)),
                )

        await db.commit()

    return dispatched


async def recover_outbox_on_startup(now_epoch: float | None = None) -> dict[str, int]:
    """
    Восстанавливает застрявшие события после рестарта:
    - старше NOTIFICATION_MAX_AGE (30 мин) -> expired
    - незавершенные с send_started_at IS NOT NULL -> pending, crash_retry = 1
    - с просроченным lease_until -> pending
    Возвращает сводку восстановления (expired, requeued, flagged_possible_duplicate).
    """
    now = float(time.time() if now_epoch is None else now_epoch)
    max_age = float(config.NOTIFICATION_MAX_AGE)

    try:
        async with writer_connection() as db:
            await db.execute("BEGIN IMMEDIATE")

            # 1. Протухшие события старше 30 минут -> expired
            c_exp = await db.execute(
                """
                UPDATE notification_events
                SET status = 'expired', expired_at = ?, lease_until = NULL, last_error = 'startup_max_age_expired'
                WHERE status IN ('pending', 'sending') AND (? - queued_at) > ?
                """,
                (now, now, max_age),
            )
            expired_count = max(0, int(c_exp.rowcount or 0))
            await db.execute(
                """
                UPDATE notification_dedupe
                SET status = 'expired', updated_at = ?
                WHERE status = 'queued' AND (user_id, item_id) IN (
                    SELECT user_id, item_id FROM notification_events WHERE status = 'expired'
                )
                """,
                (now,),
            )

            # 2. Незавершенные отправки (бот упал после старта вызова Bot API) -> crash_retry = 1
            c_flag = await db.execute(
                """
                UPDATE notification_events
                SET status = 'pending', lease_until = NULL, send_started_at = NULL,
                    crash_retry = 1, next_attempt_at = ?
                WHERE status = 'sending' AND send_started_at IS NOT NULL
                """,
                (now,),
            )
            flagged_count = max(0, int(c_flag.rowcount or 0))

            # 3. Обычные незавершенные lease -> pending
            c_req = await db.execute(
                """
                UPDATE notification_events
                SET status = 'pending', lease_until = NULL
                WHERE status = 'sending'
                """,
            )
            requeued_other = max(0, int(c_req.rowcount or 0))

            await db.commit()
    except sqlite3.OperationalError:
        return {"expired": 0, "requeued": 0, "flagged_possible_duplicate": 0}

    return {
        "expired": expired_count,
        "requeued": flagged_count + requeued_other,
        "flagged_possible_duplicate": flagged_count,
    }


async def claim_notification_event(
    event_id: int | None = None,
    *,
    lease_seconds: float | None = None,
    now_epoch: float | None = None,
    now: float | None = None,
    exclude_user_ids: Iterable[int] = (),
    user_id: int | None = None,
) -> Optional[Dict[str, Any]]:
    """
    Атомарно захватывает одно готовое к отправке событие (по ID из PriorityQueue или самое приоритетное готовое).
    exclude_user_ids пропускает чаты, которые уже обслуживает другой отправщик; user_id берёт событие только этого чата.
    Возвращает событие вместе со списком агрегированных радаров.
    """
    excluded = sorted({int(uid) for uid in exclude_user_ids})
    user_filter = ""
    user_params: list[Any] = []
    if excluded:
        user_filter += f" AND user_id NOT IN ({','.join('?' for _ in excluded)})"
        user_params += excluded
    if user_id is not None:
        user_filter += " AND user_id = ?"
        user_params.append(int(user_id))
    eff_now = float(now if now is not None else (time.time() if now_epoch is None else now_epoch))
    lease_dur = float(config.NOTIFICATION_LEASE_SECONDS if lease_seconds is None else lease_seconds)
    force_reclaim = lease_seconds is not None and float(lease_seconds) <= 0
    lease_until = eff_now + max(0.0, lease_dur)

    async with writer_connection() as db:
        def _sync_claim():
            conn = db._conn
            conn.execute("BEGIN IMMEDIATE")
            if event_id is not None:
                cur = conn.execute(
                    """
                    SELECT * FROM notification_events
                    WHERE id = ?
                      AND ((status = 'pending' AND next_attempt_at <= ?)
                           OR (status = 'sending' AND (? = 1 OR (lease_until IS NOT NULL AND lease_until <= ?))))
                    """,
                    (int(event_id), eff_now, 1 if force_reclaim else 0, eff_now),
                )
            else:
                cur = conn.execute(
                    f"""
                    SELECT * FROM notification_events
                    WHERE ((status = 'pending' AND next_attempt_at <= ?)
                       OR (status = 'sending' AND (? = 1 OR (lease_until IS NOT NULL AND lease_until <= ?)))){user_filter}
                    ORDER BY priority ASC, deliver_at ASC, id ASC
                    LIMIT 1
                    """,
                    (eff_now, 1 if force_reclaim else 0, eff_now, *user_params),
                )
            r = cur.fetchone()
            cur.close()
            if r is None:
                conn.commit()
                return None

            ev_id = int(r["id"])
            uid = int(r["user_id"])
            iid = str(r["item_id"])
            ltoken = f"{lease_until:.6f}:{secrets.token_hex(8)}"
            conn.execute(
                """
                UPDATE notification_events
                SET status = 'sending',
                    send_started_at = ?,
                    lease_until = ?,
                    attempt_count = attempt_count + 1,
                    last_error = ?
                WHERE id = ?
                """,
                (eff_now, lease_until, f"lease:{ltoken}", ev_id),
            )
            rcur = conn.execute(
                """
                SELECT dr.search_id, dr.radar_title,
                       s.id AS live_id, s.title AS live_title, s.is_active, s.stop_words, s.search_in_title_only,
                       s.max_age_minutes, s.url, s.template, s.price_min, s.price_max, s.must_words,
                       s.delivery_only, s.hide_promoted, s.reply_template
                FROM notification_dedupe_radars dr
                LEFT JOIN searches s ON s.id = dr.search_id AND s.user_id = dr.user_id
                WHERE dr.user_id = ? AND dr.item_id = ?
                ORDER BY dr.search_id ASC
                """,
                (uid, iid),
            )
            r_rows = rcur.fetchall()
            rcur.close()

            u_allowed = True
            if uid not in ADMIN_IDS:
                ucur = conn.execute(
                    "SELECT is_allowed, subscription_until FROM users WHERE user_id = ?",
                    (uid,),
                )
                urow = ucur.fetchone()
                ucur.close()
                # Same rule as is_user_allowed: a trial has subscription_until (UTC) and is_allowed = 0.
                if urow is None:
                    u_allowed = False
                elif urow["subscription_until"]:
                    try:
                        sub_until = datetime.fromisoformat(urow["subscription_until"])
                        if sub_until <= datetime.now(timezone.utc).replace(tzinfo=None):
                            u_allowed = False
                    except ValueError:
                        u_allowed = False
                elif not urow["is_allowed"]:
                    u_allowed = False

            conn.commit()
            return dict(r), r_rows, ltoken, u_allowed

        claimed = await db._execute(_sync_claim)
        if claimed is None:
            return None
        row, radar_rows, lease_token, user_allowed = claimed

    radars: list[dict[str, Any]] = []
    for r in radar_rows:
        sw = json.loads(r["stop_words"]) if r["stop_words"] else []
        radars.append({
            "search_id": int(r["search_id"]),
            "radar_title": str(r["live_title"] or r["radar_title"]),
            "exists": r["live_id"] is not None,
            "is_active": bool(r["is_active"]) if r["live_id"] is not None else False,
            "stop_words": sw,
            "search_in_title_only": int(r["search_in_title_only"] or 0),
            "max_age_minutes": int(r["max_age_minutes"] or 5),
            "url": r["url"] or "",
            "template": r["template"],
            "price_min": int(r["price_min"] or 0),
            "price_max": int(r["price_max"] or 0),
            "must_words": r["must_words"] or "[]",
            "delivery_only": int(r["delivery_only"] or 0),
            "hide_promoted": int(r["hide_promoted"] or 0),
            "reply_template": r["reply_template"] or "",
        })

    first_sid = radars[0]["search_id"] if radars else 0
    result = dict(row)
    result["attempt_count"] = int(row["attempt_count"]) + 1
    result["attempts"] = result["attempt_count"]
    result["send_started_at"] = eff_now
    result["lease_until"] = lease_until
    result["lease_token"] = lease_token
    result["radars"] = radars
    result["search_id"] = first_sid
    result["radar_id"] = first_sid
    result["search_ids"] = [r["search_id"] for r in radars]
    result["radar_titles"] = [r["radar_title"] for r in radars]
    result["tariff"] = row["tariff_plan"]
    result["payload"] = row["item_json"]
    result["payload_json"] = row["item_json"]
    result["possible_duplicate_after_crash"] = bool(row["crash_retry"])
    result["_fast_claimed"] = True
    result["_user_allowed"] = user_allowed
    return result


async def mark_event_send_started(event_id: int, lease_token: str, *, now_epoch: float | None = None) -> bool:
    now = float(time.time() if now_epoch is None else now_epoch)
    async with writer_connection() as db:
        cur = await db.execute(
            """
            UPDATE notification_events
            SET send_started_at = ?
            WHERE id = ? AND status = 'sending' AND last_error = ?
            """,
            (now, int(event_id), f"lease:{lease_token}"),
        )
        await db.commit()
        return cur.rowcount > 0


async def mark_event_sent(
    event_id: int,
    lease_token: str,
    *,
    raw_price: int = 0,
    now_epoch: float | None = None,
) -> bool:
    """
    Фиксирует успешную доставку только по актуальному lease_token:
    - переводит notification_events в 'sent'
    - переводит notification_dedupe в 'sent'
    - массово отмечает все агрегированные радары пользователя в seen_items
    """
    now = float(time.time() if now_epoch is None else now_epoch)
    async with writer_connection() as db:
        def _sync_ack():
            conn = db._conn
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "SELECT user_id, item_id FROM notification_events WHERE id = ? AND status = 'sending' AND last_error = ?",
                (int(event_id), f"lease:{lease_token}"),
            )
            r = cur.fetchone()
            cur.close()
            if r is None:
                conn.commit()
                return False

            uid = int(r["user_id"])
            iid = str(r["item_id"])

            conn.execute(
                """
                UPDATE notification_events
                SET status = 'sent', sent_at = ?, lease_until = NULL, send_started_at = NULL, last_error = NULL
                WHERE id = ?
                """,
                (now, int(event_id)),
            )
            conn.execute(
                """
                UPDATE notification_dedupe
                SET status = 'sent', sent_at = ?, updated_at = ?
                WHERE user_id = ? AND item_id = ?
                """,
                (now, now, uid, iid),
            )

            scur = conn.execute(
                """
                SELECT dr.search_id FROM notification_dedupe_radars dr
                JOIN searches s ON s.id = dr.search_id AND s.user_id = dr.user_id
                WHERE dr.user_id = ? AND dr.item_id = ?
                """,
                (uid, iid),
            )
            sids = [int(sr[0]) for sr in scur.fetchall()]
            scur.close()

            if sids:
                conn.executemany(
                    """
                    INSERT INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)
                    ON CONFLICT(search_id, item_id) DO UPDATE SET last_price = excluded.last_price
                    """,
                    [(sid, iid, int(raw_price or 0)) for sid in sids],
                )

            conn.commit()
            return True

        return await db._execute(_sync_ack)


async def reschedule_event(
    event_id: int,
    lease_token: str,
    *,
    delay_seconds: float,
    error_text: str | None = None,
    is_generic_error: bool = False,
    now_epoch: float | None = None,
) -> Optional[Dict[str, Any]]:
    """
    Освобождает lease и переносит повторную попытку на delay_seconds (до NOTIFICATION_RETRY_MAX).
    Если возраст превысил NOTIFICATION_MAX_AGE -> переводит в expired.
    Если превышен лимит NOTIFICATION_GENERIC_ATTEMPTS -> переводит в failed.
    Возвращает обновленные поля события, если оно осталось pending.
    """
    now = float(time.time() if now_epoch is None else now_epoch)
    delay = min(float(config.NOTIFICATION_RETRY_MAX), max(0.0, float(delay_seconds)))

    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT * FROM notification_events WHERE id = ? AND status = 'sending' AND last_error = ?",
            (int(event_id), f"lease:{lease_token}"),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await db.commit()
            return None

        uid = int(row["user_id"])
        iid = str(row["item_id"])
        queued_at = float(row["queued_at"])

        if (now - queued_at) > float(config.NOTIFICATION_MAX_AGE):
            await db.execute(
                """
                UPDATE notification_events
                SET status = 'expired', expired_at = ?, lease_until = NULL, send_started_at = NULL, last_error = ?
                WHERE id = ?
                """,
                (now, error_text or "max_age_expired", int(event_id)),
            )
            await db.execute(
                "UPDATE notification_dedupe SET status = 'expired', updated_at = ? WHERE user_id = ? AND item_id = ?",
                (now, uid, iid),
            )
            await db.commit()
            return None

        gen_count = int(row["generic_error_count"]) + (1 if is_generic_error else 0)
        if is_generic_error and gen_count >= config.NOTIFICATION_GENERIC_ATTEMPTS:
            await db.execute(
                """
                UPDATE notification_events
                SET status = 'failed', failed_at = ?, lease_until = NULL, send_started_at = NULL,
                    generic_error_count = ?, last_error = ?
                WHERE id = ?
                """,
                (now, gen_count, error_text or "generic_attempts_exhausted", int(event_id)),
            )
            await db.execute(
                "UPDATE notification_dedupe SET status = 'failed', updated_at = ? WHERE user_id = ? AND item_id = ?",
                (now, uid, iid),
            )
            await db.commit()
            return None

        next_attempt = max(float(row["deliver_at"]), now + delay)
        await db.execute(
            """
            UPDATE notification_events
            SET status = 'pending', lease_until = NULL, send_started_at = NULL,
                next_attempt_at = ?, generic_error_count = ?, last_error = ?
            WHERE id = ?
            """,
            (next_attempt, gen_count, error_text, int(event_id)),
        )
        await db.commit()
        return {
            "id": int(row["id"]),
            "user_id": uid,
            "item_id": iid,
            "tariff_plan": row["tariff_plan"],
            "priority": int(row["priority"]),
            "deliver_at": float(row["deliver_at"]),
            "next_attempt_at": next_attempt,
            "queued_at": queued_at,
            "is_catchup": int(row["is_catchup"]),
        }


async def mark_event_expired(
    event_id: int,
    lease_token: str | None = None,
    *,
    reason: str = "expired",
    mark_seen: bool = False,
    raw_price: int = 0,
    now_epoch: float | None = None,
) -> bool:
    now = float(time.time() if now_epoch is None else now_epoch)
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        if lease_token is not None:
            async with db.execute(
                "SELECT user_id, item_id FROM notification_events WHERE id = ? AND status = 'sending' AND last_error = ?",
                (int(event_id), f"lease:{lease_token}"),
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                "SELECT user_id, item_id FROM notification_events WHERE id = ?",
                (int(event_id),),
            ) as cur:
                row = await cur.fetchone()

        if row is None:
            await db.commit()
            return False

        uid = int(row["user_id"])
        iid = str(row["item_id"])

        await db.execute(
            """
            UPDATE notification_events
            SET status = 'expired', expired_at = ?, lease_until = NULL, send_started_at = NULL, last_error = ?
            WHERE id = ?
            """,
            (now, reason, int(event_id)),
        )
        await db.execute(
            "UPDATE notification_dedupe SET status = 'expired', updated_at = ? WHERE user_id = ? AND item_id = ?",
            (now, uid, iid),
        )

        if mark_seen:
            async with db.execute(
                """
                SELECT dr.search_id FROM notification_dedupe_radars dr
                JOIN searches s ON s.id = dr.search_id AND s.user_id = dr.user_id
                WHERE dr.user_id = ? AND dr.item_id = ?
                """,
                (uid, iid),
            ) as cur:
                sids = [int(r[0]) for r in await cur.fetchall()]
            if sids:
                await db.executemany(
                    """
                    INSERT INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)
                    ON CONFLICT(search_id, item_id) DO UPDATE SET last_price = excluded.last_price
                    """,
                    [(sid, iid, int(raw_price or 0)) for sid in sids],
                )

        await db.commit()
        return True


async def mark_event_failed(
    event_id: int,
    lease_token: str | None = None,
    *,
    reason: str = "failed",
    error: str | None = None,
    fail_all_user_pending: bool = False,
    now_epoch: float | None = None,
) -> bool:
    if error is not None:
        reason = error
    now = float(time.time() if now_epoch is None else now_epoch)
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        if lease_token is not None:
            async with db.execute(
                "SELECT user_id, item_id FROM notification_events WHERE id = ? AND status = 'sending' AND last_error = ?",
                (int(event_id), f"lease:{lease_token}"),
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                "SELECT user_id, item_id FROM notification_events WHERE id = ?",
                (int(event_id),),
            ) as cur:
                row = await cur.fetchone()

        if row is None:
            await db.commit()
            return False

        uid = int(row["user_id"])
        iid = str(row["item_id"])

        await db.execute(
            """
            UPDATE notification_events
            SET status = 'failed', failed_at = ?, lease_until = NULL, send_started_at = NULL, last_error = ?
            WHERE id = ?
            """,
            (now, reason, int(event_id)),
        )
        await db.execute(
            "UPDATE notification_dedupe SET status = 'failed', updated_at = ? WHERE user_id = ? AND item_id = ?",
            (now, uid, iid),
        )

        if fail_all_user_pending:
            await db.execute(
                """
                UPDATE notification_events
                SET status = 'failed', failed_at = ?, lease_until = NULL, send_started_at = NULL, last_error = ?
                WHERE user_id = ? AND status = 'pending'
                """,
                (now, reason, uid),
            )
            await db.execute(
                """
                UPDATE notification_dedupe
                SET status = 'failed', updated_at = ?
                WHERE user_id = ? AND status = 'queued'
                """,
                (now, uid),
            )

        await db.commit()
        return True


async def get_event_for_callback(event_id: int) -> Optional[Dict[str, Any]]:
    """
    Возвращает событие уведомления и список его актуальных радаров для inline-кнопок
    ('Стоп-слово' и 'Забанить продавца').
    """
    async with reader_connection() as db:
        async with db.execute(
            "SELECT * FROM notification_events WHERE id = ?",
            (int(event_id),),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None

        uid = int(row["user_id"])
        iid = str(row["item_id"])
        async with db.execute(
            """
            SELECT dr.search_id, dr.radar_title, s.id AS live_id, s.title AS live_title, s.is_active
            FROM notification_dedupe_radars dr
            LEFT JOIN searches s ON s.id = dr.search_id AND s.user_id = dr.user_id
            WHERE dr.user_id = ? AND dr.item_id = ?
            ORDER BY dr.search_id ASC
            """,
            (uid, iid),
        ) as cur:
            r_rows = await cur.fetchall()

    try:
        item_data = json.loads(row["item_json"]) if row["item_json"] else {}
    except Exception:
        item_data = {}

    radars = [
        {
            "search_id": int(r["search_id"]),
            "radar_title": str(r["live_title"] or r["radar_title"] or f"Радар #{r['search_id']}"),
            "exists": r["live_id"] is not None,
            "is_active": bool(r["is_active"]) if r["live_id"] is not None else False,
        }
        for r in r_rows
    ]
    data = dict(row)
    data["item"] = item_data
    data["radars"] = radars
    data["active_radars"] = [r for r in radars if r["exists"]]
    return data


async def cleanup_terminal_notification_events(
    retention_hours: int | None = None,
    *,
    now_epoch: float | None = None,
) -> int:
    hours = int(
        config.NOTIFICATION_TERMINAL_RETENTION_HOURS if retention_hours is None else retention_hours
    )
    now = float(time.time() if now_epoch is None else now_epoch)
    cutoff = now - hours * 3600.0
    dedupe_sent_cutoff = now - 7 * 86400.0

    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        c1 = await db.execute(
            """
            DELETE FROM notification_events
            WHERE (status = 'sent' AND sent_at IS NOT NULL AND sent_at < ?)
               OR (status = 'failed' AND failed_at IS NOT NULL AND failed_at < ?)
               OR (status = 'expired' AND expired_at IS NOT NULL AND expired_at < ?)
            """,
            (cutoff, cutoff, cutoff),
        )
        deleted = c1.rowcount

        await db.execute(
            """
            DELETE FROM notification_dedupe
            WHERE (status IN ('failed', 'expired') AND updated_at < ?)
               OR (status = 'sent' AND updated_at < ?)
            """,
            (cutoff, dedupe_sent_cutoff),
        )
        await db.execute(
            """
            DELETE FROM notification_dedupe_radars
            WHERE NOT EXISTS (
                SELECT 1 FROM notification_dedupe d
                WHERE d.user_id = notification_dedupe_radars.user_id
                  AND d.item_id = notification_dedupe_radars.item_id
            )
            """
        )
        await db.commit()
        return deleted


async def cleanup_old_seen_items(days: int = 5) -> int:
    try:
        async with writer_connection() as db:
            cursor = await db.execute(
                "DELETE FROM seen_items WHERE created_at < datetime('now', '-' || ? || ' days')",
                (days,)
            )
            deleted_count = cursor.rowcount

            orphan_cursor = await db.execute(
                "DELETE FROM seen_items WHERE search_id NOT IN (SELECT id FROM searches)"
            )
            orphans_deleted = orphan_cursor.rowcount
            deleted_count += orphans_deleted

            await db.commit()
            if deleted_count > 0:
                await db.execute("PRAGMA incremental_vacuum;")
                logger.info(f"Очистка БД: удалено {deleted_count} записей seen_items (из них сирот: {orphans_deleted})")
        await cleanup_terminal_notification_events()
        return deleted_count
    except Exception as e:
        logger.error(f"Ошибка при очистке устаревших записей БД: {e}")
        return 0


async def get_system_stats() -> Dict[str, int]:
    async with reader_connection() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            total_users = row[0] if row else 0
        admin_placeholders = ",".join("?" for _ in ADMIN_IDS)
        query_allowed = f"""
            SELECT COUNT(*) FROM users 
            WHERE user_id IN ({admin_placeholders}) 
               OR (is_allowed = 1 AND subscription_until IS NULL)
               OR subscription_until > datetime('now')
        """
        async with db.execute(query_allowed, ADMIN_IDS) as cur:
            row = await cur.fetchone()
            allowed_users = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM searches") as cur:
            row = await cur.fetchone()
            total_searches = row[0] if row else 0
        query_active = f"""
            SELECT COUNT(*) FROM searches s JOIN users u ON s.user_id = u.user_id
            WHERE s.is_active = 1
              AND (u.user_id IN ({admin_placeholders})
                   OR (u.is_allowed = 1 AND u.subscription_until IS NULL)
                   OR u.subscription_until > datetime('now'))
        """
        async with db.execute(query_active, ADMIN_IDS) as cur:
            row = await cur.fetchone()
            active_searches = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM seen_items") as cur:
            row = await cur.fetchone()
            total_seen = row[0] if row else 0

        return {
            "total_users": total_users,
            "allowed_users": allowed_users,
            "total_searches": total_searches,
            "active_searches": active_searches,
            "total_seen": total_seen,
        }


async def activate_trial(user_id: int) -> Dict[str, Any]:
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT trial_used, subscription_until FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                await db.commit()
                return {"success": False, "message": "Пользователь не найден."}
            if row["trial_used"]:
                await db.commit()
                return {"success": False, "message": "Пробный период уже был использован ранее."}

        await recalibrate_if_no_access_in_transaction(db, user_id)
        await db.execute("""
            UPDATE users 
            SET trial_used = 1,
                subscription_until = datetime('now', ?)
            WHERE user_id = ?
        """, (f"+{config.TRIAL_HOURS} hours", user_id))
        await db.commit()

        async with db.execute("SELECT subscription_until FROM users WHERE user_id = ?", (user_id,)) as cur:
            r = await cur.fetchone()
            until = r[0] if r else ""

        return {"success": True, "message": f"Пробный период {config.TRIAL_TEXT} успешно активирован!", "until": until}


async def count_sent_lots(user_id: int, since_epoch: float, until_epoch: float) -> int:
    """Lots delivered to the user in the window. Sent rows of notification_dedupe live 7 days."""
    async with reader_connection() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM notification_dedupe WHERE user_id = ? AND status = 'sent' AND sent_at >= ? AND sent_at <= ?",
            (int(user_id), float(since_epoch), float(until_epoch)),
        ) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def grant_subscription_in_transaction(db, user_id: int, days: int, tariff_plan: Optional[str] = None) -> str:
    """Shared atomic update; the caller owns the transaction and commit."""
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError("days must be a positive integer")
    if tariff_plan is not None and tariff_plan.lower() not in (*config.PAID_PLANS, "test"):
        raise ValueError("Unknown tariff plan")
    effective_plan = "standart" if (tariff_plan and tariff_plan.lower() == "test") else (tariff_plan.lower() if tariff_plan else None)
    async with db.execute("""
        SELECT tariff_plan, is_allowed, subscription_until FROM users WHERE user_id = ?
    """, (user_id,)) as cursor:
        current = await cursor.fetchone()
    if current is None:
        raise ValueError("Пользователь не найден")
    await recalibrate_if_no_access_in_transaction(db, user_id)
    # A trial has is_allowed = 0: its hours are free and are not converted.
    if current[1] and effective_plan and effective_plan != (current[0] or "").lower():
        left = subscription_seconds_left({"subscription_until": current[2]})
        if left:
            await db.execute(
                "UPDATE users SET subscription_until = datetime('now', '+' || ? || ' seconds') WHERE user_id = ?",
                (config.plan_switch_seconds(current[0], effective_plan, left), user_id),
            )
    cursor = await db.execute("""
        UPDATE users
        SET subscription_until = datetime(
                CASE WHEN subscription_until > datetime('now') THEN subscription_until ELSE datetime('now') END,
                '+' || ? || ' days'),
            tariff_plan = COALESCE(?, tariff_plan),
            is_allowed = 1, trial_used = 1,
            subscription_revision = subscription_revision + 1
        WHERE user_id = ?
    """, (days, effective_plan, user_id))
    if cursor.rowcount != 1:
        raise ValueError("Пользователь не найден")
    await sync_radar_limit_in_transaction(db, user_id)
    async with db.execute("SELECT subscription_until FROM users WHERE user_id = ?", (user_id,)) as cursor:
        return (await cursor.fetchone())[0]


async def grant_subscription(user_id: int, days: int = 30, tariff_plan: Optional[str] = None) -> str:
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        until = await grant_subscription_in_transaction(db, user_id, days, tariff_plan)
        await db.commit()
        return until


async def count_active_subscriptions() -> int:
    """Non-admin users whose paid access or trial is running now."""
    async with reader_connection() as db:
        async with db.execute("SELECT user_id FROM users WHERE subscription_until > datetime('now')") as cur:
            return sum(1 for r in await cur.fetchall() if int(r[0]) not in ADMIN_IDS)


async def extend_active_subscriptions(seconds: int) -> list[tuple[int, str]]:
    """Adds time to every running paid access and trial (compensation after downtime). Returns (user_id, new until)."""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise ValueError("seconds must be a positive integer")
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT user_id FROM users WHERE subscription_until > datetime('now')") as cur:
            ids = [int(r[0]) for r in await cur.fetchall() if int(r[0]) not in ADMIN_IDS]
        await db.executemany(
            "UPDATE users SET subscription_until = datetime(subscription_until, '+' || ? || ' seconds'), "
            "subscription_revision = subscription_revision + 1 WHERE user_id = ?",
            [(seconds, uid) for uid in ids],
        )
        result = []
        for uid in ids:
            async with db.execute("SELECT subscription_until FROM users WHERE user_id = ?", (uid,)) as cur:
                row = await cur.fetchone()
            result.append((uid, row[0] if row else ""))
        await db.commit()
    return result


async def create_admin_subscription_action(admin_id: int, user_id: int, plan: str, days: int, page: int = 0) -> str:
    if admin_id not in ADMIN_IDS or user_id in ADMIN_IDS:
        raise PermissionError("Нельзя изменять подписку администратора")
    if plan not in config.PAID_PLANS or days not in (1, 7, 14, 30, 90, 180, 365):
        raise ValueError("Неверный тариф или срок")
    token = secrets.token_hex(16)
    async with writer_connection() as db:
        cursor = await db.execute("""
            INSERT INTO admin_subscription_actions(token, admin_id, user_id, tariff_plan, days, page)
            SELECT ?, ?, user_id, ?, ?, ? FROM users WHERE user_id = ?
        """, (token, admin_id, plan, days, max(0, page), user_id))
        if cursor.rowcount != 1:
            raise ValueError("Пользователь не найден")
        await db.commit()
    return token


async def apply_admin_subscription_action(token: str, admin_id: int) -> Dict[str, Any]:
    if admin_id not in ADMIN_IDS:
        raise PermissionError("У вас нет прав администратора")
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("""
            SELECT * FROM admin_subscription_actions WHERE token = ? AND admin_id = ?
              AND (applied_at IS NOT NULL OR created_at > datetime('now', '-1 day'))
        """, (token, admin_id)) as cursor:
            row = await cursor.fetchone()
        if not row or row["user_id"] in ADMIN_IDS:
            await db.commit()
            raise ValueError("Подтверждение истекло или недоступно")
        result = dict(row)
        result["applied"] = row["applied_at"] is None
        if result["applied"]:
            result["subscription_until"] = await grant_subscription_in_transaction(
                db, row["user_id"], row["days"], row["tariff_plan"],
            )
            await db.execute("""
                UPDATE admin_subscription_actions SET applied_at = datetime('now'), subscription_until = ?
                WHERE token = ?
            """, (result["subscription_until"], token))
        await db.commit()
        return result


async def deactivate_user_searches(user_id: int):
    async with writer_connection() as db:
        async with db.execute("SELECT id FROM searches WHERE user_id = ? AND is_active = 1", (user_id,)) as cursor:
            stopped = [int(row[0]) for row in await cursor.fetchall()]
        for radar_id in stopped:
            await log_radar_history(db, user_id, radar_id, "blocked_pause")
        await db.execute(
            "UPDATE searches SET is_active = 0, pause_reason = ? WHERE user_id = ? AND is_active = 1",
            (PAUSE_BLOCKED, user_id),
        )
        await db.commit()


# --- Совместимые функции очереди уведомлений поверх notification_events ---

async def enqueue_notification(search_id: int, user_id: int, item_id: str, event_key: str, payload: dict) -> bool:
    """
    Постановка уведомления в user-level outbox (notification_events + notification_dedupe).
    Уведомления о снижении цены (drop:*) полностью отключены.
    """
    if event_key != "new":
        return False

    search = await get_search(search_id)
    if not search or int(search["user_id"]) != int(user_id) or not search.get("is_active"):
        return False

    user = await get_user(user_id)
    tariff_key, priority, delay = classify_user_tariff(
        user_id,
        user.get("tariff_plan") if user else None,
        is_allowed=user.get("is_allowed", 1) if user else 0,
    )

    now = time.time()
    item_data = payload.get("item") if isinstance(payload.get("item"), dict) else payload
    t_pub = item_data.get("timestamp") if isinstance(item_data, dict) else None
    t_pub_val = float(t_pub) if isinstance(t_pub, (int, float)) and t_pub > 0 else None
    t_seen = float(payload.get("discovered_at") or now)
    raw_price = int((item_data.get("raw_price") if isinstance(item_data, dict) else 0) or 0)

    dispatched = await enqueue_group_events(
        [
            {
                "user_id": int(user_id),
                "item_id": str(item_id),
                "tariff_plan": tariff_key,
                "priority": priority,
                "queued_at": now,
                "deliver_at": now + delay,
                "is_catchup": bool(payload.get("is_catchup", False)),
                "item_json": json.dumps(payload, ensure_ascii=False),
                "raw_price": raw_price,
                "t_pub": t_pub_val,
                "t_pub_reliable": t_pub_val is not None,
                "t_seen": t_seen,
                "matched_radars": [(int(search_id), str(payload.get("radar_title") or search.get("title") or "Радар"))],
            }
        ],
        now_epoch=now,
    )
    return len(dispatched) > 0


async def get_outbox_metrics() -> dict:
    """Метрики очереди уведомлений (pending, leased, oldest_seconds)."""
    now = time.time()
    try:
        async with reader_connection() as db:
            async with db.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(status = 'sending'), 0),
                       COALESCE(MAX(CAST(? - queued_at AS INTEGER)), 0)
                FROM notification_events
                WHERE status IN ('pending', 'sending')
                """,
                (now,),
            ) as cursor:
                pending, leased, oldest = await cursor.fetchone()
    except sqlite3.OperationalError:
        return {"pending": 0, "leased": 0, "oldest_seconds": 0}
    return {
        "pending": int(pending or 0),
        "leased": int(leased or 0),
        "oldest_seconds": max(0, int(oldest or 0)),
    }


async def get_pending_new_item_ids(search_id: int, item_ids: Optional[Any] = None) -> set[str]:
    async with reader_connection() as db:
        if item_ids is not None:
            ids = [str(x) for x in item_ids if str(x).strip()]
            if not ids:
                return set()
            ids = ids[:500]
            placeholders = ",".join("?" for _ in ids)
            query = f"""
                SELECT dr.item_id
                FROM notification_dedupe_radars dr
                JOIN notification_events e ON e.user_id = dr.user_id AND e.item_id = dr.item_id
                WHERE dr.search_id = ? AND e.status IN ('pending', 'sending') AND dr.item_id IN ({placeholders})
            """
            params = [search_id] + ids
        else:
            query = """
                SELECT dr.item_id
                FROM notification_dedupe_radars dr
                JOIN notification_events e ON e.user_id = dr.user_id AND e.item_id = dr.item_id
                WHERE dr.search_id = ? AND e.status IN ('pending', 'sending')
            """
            params = [search_id]

        async with db.execute(query, params) as cursor:
            return {str(row[0]) for row in await cursor.fetchall()}


async def claim_notification(
    lease_seconds: int = 60,
    *,
    ignore_deliver_delay: bool = True,
    now: float | None = None,
) -> Optional[Dict[str, Any]]:
    """
    Совместимый метод атомарного захвата следующего уведомления из notification_events.
    """
    eff_now = float(time.time() if now is None else now)
    force_reclaim = int(lease_seconds) <= 0
    lease_until = eff_now + max(0, int(lease_seconds))
    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        if ignore_deliver_delay:
            async with db.execute(
                """
                SELECT * FROM notification_events
                WHERE (status = 'pending' AND (
                        next_attempt_at <= ?
                        OR (attempt_count = 0 AND next_attempt_at <= ?)
                      ))
                   OR (status = 'sending' AND (? = 1 OR (lease_until IS NOT NULL AND lease_until <= ?)))
                ORDER BY priority ASC, deliver_at ASC, id ASC
                LIMIT 1
                """,
                (eff_now, eff_now + 2.5, 1 if force_reclaim else 0, eff_now),
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                """
                SELECT * FROM notification_events
                WHERE (status = 'pending' AND next_attempt_at <= ?)
                   OR (status = 'sending' AND (? = 1 OR (lease_until IS NOT NULL AND lease_until <= ?)))
                ORDER BY priority ASC, deliver_at ASC, id ASC
                LIMIT 1
                """,
                (eff_now, 1 if force_reclaim else 0, eff_now),
            ) as cur:
                row = await cur.fetchone()

        if row is None:
            await db.commit()
            return None

        ev_id = int(row["id"])
        token = secrets.token_hex(16)
        await db.execute(
            """
            UPDATE notification_events
            SET status = 'sending',
                lease_until = ?,
                attempt_count = attempt_count + 1,
                last_error = ?
            WHERE id = ?
            """,
            (lease_until, f"lease:{token}", ev_id),
        )
        await db.commit()

    ev_full = await get_event_for_callback(ev_id)
    radars = ev_full["radars"] if ev_full else []
    first_search_id = radars[0]["search_id"] if radars else 0

    result = dict(row)
    result.update(
        search_id=first_search_id,
        radar_id=first_search_id,
        search_ids=[r["search_id"] for r in radars],
        radar_titles=[r["radar_title"] for r in radars],
        tariff=row["tariff_plan"],
        event_key="new",
        payload=row["item_json"],
        payload_json=row["item_json"],
        lease_token=token,
        lease_until=lease_until,
        attempts=int(row["attempt_count"]) + 1,
        attempt_count=int(row["attempt_count"]) + 1,
        radars=radars,
        possible_duplicate_after_crash=bool(row["crash_retry"]),
    )
    return result


async def acknowledge_notification(notification_id: int, lease_token: str, price: int) -> bool:
    return await mark_event_sent(notification_id, lease_token, raw_price=price)


async def release_notification(notification_id: int, lease_token: str, delay_seconds: float = 0):
    await reschedule_event(
        notification_id,
        lease_token,
        delay_seconds=delay_seconds,
        error_text="released",
        is_generic_error=False,
    )


async def discard_notification(notification_id: int, lease_token: str):
    await mark_event_expired(notification_id, lease_token, reason="discarded", mark_seen=False)


def create_database_backup_file() -> Optional[str]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"avito_radar_backup_{timestamp}.db"
    backup_path = os.path.join(tempfile.gettempdir(), backup_filename)

    try:
        src = sqlite3.connect(_current_db_path())
        dest = sqlite3.connect(backup_path)
        src.backup(dest)
        dest.close()
        src.close()
        return backup_path
    except Exception as e:
        logger.error(f"Ошибка при создании бэкапа SQLite: {e}")
        return None


async def save_support_ticket(user_id: int, full_name: str, username: str, text: str,
                              media_type: str, file_id: str, is_payment: bool, created_epoch: float) -> int:
    async with writer_connection() as db:
        cursor = await db.execute("""
            INSERT INTO support_outbox (user_id, full_name, username, text, media_type, file_id, is_payment, created_epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, full_name, username, text, media_type, file_id, int(is_payment), created_epoch))
        await db.commit()
        return int(cursor.lastrowid)


async def get_undelivered_support_tickets(limit: int = 20) -> List[Dict[str, Any]]:
    async with reader_connection() as db:
        async with db.execute("SELECT * FROM support_outbox ORDER BY id LIMIT ?", (limit,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def delete_support_ticket(ticket_id: int) -> None:
    async with writer_connection() as db:
        await db.execute("DELETE FROM support_outbox WHERE id = ?", (ticket_id,))
        await db.commit()


async def purge_user_by_pattern(pattern: str) -> Dict[str, Any]:
    clean = pattern.strip().lstrip("@").lower()
    if not clean:
        return {"success": False, "deleted": 0, "message": "Паттерн не может быть пустым"}

    async with writer_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        if clean.isdigit():
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (int(clean),)) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute("""
                SELECT * FROM users 
                WHERE LOWER(COALESCE(username, '')) = ?
            """, (clean,)) as cur:
                rows = await cur.fetchall()

        if not rows:
            await db.commit()
            return {"success": False, "deleted": 0, "message": f"Пользователь '{pattern}' не найден в базе данных."}
        if len(rows) != 1:
            await db.commit()
            return {"success": False, "deleted": 0, "message": "Несколько пользователей с этим username. Укажите точный ID."}
        if rows[0]["user_id"] in ADMIN_IDS:
            await db.commit()
            return {"success": False, "deleted": 0, "message": "Удаление администратора запрещено."}

        deleted_users = []
        for r in rows:
            uid = r["user_id"]
            uname = r["username"] or "без username"
            fname = r["first_name"] or ""
            deleted_users.append(f"{fname} (@{uname}, ID:{uid})")

            await db.execute("""
                DELETE FROM seen_items 
                WHERE search_id IN (SELECT id FROM searches WHERE user_id = ?)
            """, (uid,))
            await db.execute("DELETE FROM notification_dedupe_radars WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM notification_dedupe WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM notification_events WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM notification_outbox WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM searches WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM radar_history WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM blocked_sellers WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM support_outbox WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM admin_subscription_actions WHERE user_id = ? AND applied_at IS NULL", (uid,))
            await db.execute("DELETE FROM users WHERE user_id = ?", (uid,))
            invalidate_blacklist_cache(uid)

        await db.commit()

        try:
            from logger_setup import sanitize_logs_for_user
            sanitize_logs_for_user(clean)
        except Exception as e:
            logger.warning(f"Ошибка очистки логов при удалении пользователя {pattern}: {e}")

        logger.info(f"🗑 Полностью удалены данные пользователей: {', '.join(deleted_users)}")
        return {
            "success": True,
            "deleted": len(deleted_users),
            "users": deleted_users,
            "message": f"Успешно удалено: {', '.join(deleted_users)}"
        }
