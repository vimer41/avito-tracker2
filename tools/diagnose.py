"""Bounded production diagnostics. No getUpdates, migrations or raw user logs."""

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def scan_summary(metrics_text: str, log_text: str = ""):
    """Totals from the minute rows of metrics.jsonl; bounded, and they hold no user data."""
    rows = []
    for line in metrics_text.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    successes = sum(row["scans"]["ok"] for row in rows)
    failures = sum(row["scans"]["fail"] for row in rows)
    attempts = successes + failures
    scan_p95 = sorted(row["scan_s"]["p95"] for row in rows if row["scan_s"]["p95"] is not None)
    channels = [channel for row in rows for channel in row["ch"].values()]
    return {
        "scope": "minute rows from a bounded tail of metrics.jsonl, not a sustained SLA",
        "minutes": len(rows), "attempts": attempts, "successes": successes, "failures": failures,
        "success_rate": round(successes / attempts, 4) if attempts else None,
        "scan_latency_p95_s_median": scan_p95[len(scan_p95) // 2] if scan_p95 else None,
        "http_403_entries": sum(channel["403"] for channel in channels),
        "http_429_entries": sum(channel["429"] for channel in channels),
        "ad_deliveries": sum(row["sent"] for row in rows),
        "loop_stalls": sum(row["stalls"] for row in rows),
        "telegram_flood_waits": sum(row["flood"] for row in rows),
        "traceback_entries": log_text.count("Traceback (most recent call last)"),
        "payment_confirmation_entries": log_text.count("Stars payment fulfilled:"),
        "real_admin_probe_entries": log_text.count("Live Telegram probe completed by admin="),
    }


def tail(path: Path, size=2_000_000):
    if not path.is_file():
        return ""
    with path.open("rb") as source:
        source.seek(max(0, path.stat().st_size - size))
        return source.read(size).decode("utf-8", errors="replace")


def print_database_summary(path: Path):
    if not path.is_file():
        print("Database not found")
        return
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as db:
        print("SQLite quick_check:", db.execute("PRAGMA quick_check").fetchone()[0])
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        print("SQLite row counts:", json.dumps({name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                                               for name in ("users", "searches", "notification_outbox", "stars_orders") if name in tables}))
        if "searches" in tables:
            print("Active radar flags:", db.execute("SELECT COUNT(*) FROM searches WHERE is_active = 1").fetchone()[0])
        if "stars_orders" in tables:
            print("Stars order states:", json.dumps(dict(db.execute("SELECT status, COUNT(*) FROM stars_orders GROUP BY status"))))
            print("Refunds requiring subscription review:", db.execute("SELECT COUNT(*) FROM stars_orders WHERE refund_needs_review = 1").fetchone()[0])
        if "rub_orders" in tables:
            print("Ruble order states:", json.dumps(dict(db.execute("SELECT status, COUNT(*) FROM rub_orders GROUP BY status"))))
            print("Ruble orders requiring review:", db.execute(
                "SELECT COUNT(*) FROM rub_orders WHERE status = 'review' OR refund_needs_review = 1").fetchone()[0])


async def telegram_probe(config, notify_admin: bool = False):
    from aiogram import Bot
    bot = Bot(config.BOT_TOKEN)
    try:
        me = await bot.get_me(request_timeout=10)
        print("Telegram API getMe:", json.dumps({"ok": True, "username": me.username}))
        info = await bot.get_webhook_info(request_timeout=10)
        print("Telegram webhook:", json.dumps({"webhook_configured": bool(info.url), "pending_updates": info.pending_update_count}))
    except Exception as exc:
        # Transient network issues during post-deploy diagnostic probe must not fail the workflow
        print("Telegram diagnostic check note (non-fatal):", type(exc).__name__)
    finally:
        await bot.session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notify-admin", action="store_true")
    args = parser.parse_args()
    import config
    print("Safe configuration:", json.dumps({"stars_enabled": config.STARS_PAYMENTS_ENABLED,
                                             "lite": config.STARS_PRICE_LITE, "standart": config.STARS_PRICE_STANDART,
                                             "pro": config.STARS_PRICE_PRO,
                                             "check_interval": config.CHECK_INTERVAL, "workers": config.MAX_CONCURRENT_WORKERS,
                                             "admin_radar_limit": config.MAX_SEARCHES_PER_USER,
                                             "proxy_configured": bool(config.PROXY_URL)}))
    print_database_summary(config.DB_PATH)
    from logger_setup import COMBINED_LOG, METRICS_LOG
    print("Avito / delivery metrics:", json.dumps(scan_summary(tail(METRICS_LOG), tail(COMBINED_LOG))))
    if sys.platform == "linux":
        result = subprocess.run(["journalctl", "-u", "avito-bot", "--since", "-20 minutes", "--no-pager", "-o", "cat"],
                                capture_output=True, text=True, timeout=15)
        print("Journal summary (20 minutes):", json.dumps({"lines": len(result.stdout.splitlines()),
                                                          "tracebacks": result.stdout.count("Traceback"),
                                                          "failed_entries": result.stdout.lower().count("failed"),
                                                          "error_entries": result.stdout.count("[ERROR]")}))
    asyncio.run(telegram_probe(config, args.notify_admin))


if __name__ == "__main__":
    main()