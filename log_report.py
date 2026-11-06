"""
Debug report from logs/events.jsonl and logs/metrics.jsonl: one compact text file that holds
what is needed to diagnose the bot without reading raw logs.

    python log_report.py [--minutes 60]     the report (the same as the button in the admin panel)
    python log_report.py --ready 24         can the bot be sold: a verdict with its evidence
    python log_report.py --load 60          load test: plan against fact, the bottleneck, what the channels carry
    python log_report.py --trace a1f3       everything that happened to one scan
    python log_report.py --radar 12         recent events of a radar
    python log_report.py --chat 986832959   recent deliveries to a chat
    python log_report.py --grep "catalog"   any text in the events
    python log_report.py --lot 4123456789   a lot's finds and deliveries over the kept history, JSON

The bot runs it as a separate process (handlers/admin.py `_log_report`), so reading weeks of logs
never holds the bot's GIL; `--live-stdin` takes the bot's current state as JSON on stdin.
"""

import argparse
import gzip
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Iterable, Iterator, Optional

from logger_setup import CRASH_LOG, EVENTS_LOG, LOG_KEEP_DAYS, METRICS_LOG

MAX_LINES = 400000
# The readiness report needs only these events; skipping the rest unread keeps a week's report light.
READINESS_TAGS = ('"ev":"app.', '"ev":"lot.new"', '"l":"W"', '"l":"E"', '"l":"C"')
LOT_TAGS = ('"ev":"lot.new"', '"ev":"send.ok"')
LOT_HISTORY_LIMIT = 40
ERROR_GROUPS = 15
SAMPLE_TRACEBACK_CHARS = 1500
TAIL_EVENTS = 25
BUCKET_MINUTES = 5
# Above this age of the worst link, or at this share of failed scans, a minute counts as a work outage.
OUTAGE_COVERAGE_S = 180
OUTAGE_FAIL_SHARE = 0.1
COVERED_S = 120  # a link is covered when its last good scan is younger than this
LONG_STALL_MS = 5000
SAFE_RPS_PER_IP = 0.5
GOOD_CHANNEL_OK = 0.7
CAPACITY_WINDOW_MIN = 10  # one minute holds too few requests per channel to judge it
RADAR_SPEED_ROWS = 10
ENOUGH_LOTS = 20


def _files_oldest_first(path: Path, since: float) -> list[Path]:
    rotated = sorted(path.parent.glob(f"{path.name}.*"), key=lambda p: p.stat().st_mtime)
    files = [p for p in rotated if p.stat().st_mtime >= since]
    return files + ([path] if path.is_file() else [])


def _read_lines(path: Path) -> Iterator[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as source:
        yield from source


def _epoch_prefix(line: str) -> Optional[float]:
    # Rows start with {"t":<epoch>, so old lines are skipped without parsing the JSON.
    try:
        return float(line[5 : line.index(",", 5, 24)])
    except ValueError:
        return None


def read_rows(path: Path, since: float, tags: Optional[tuple[str, ...]] = None) -> list[dict]:
    """Rows since `since`, oldest first; past MAX_LINES the newest are kept. `tags`: read only lines
    holding one of these substrings."""
    rows: deque = deque(maxlen=MAX_LINES)
    for file in _files_oldest_first(path, since):
        for index, line in enumerate(_read_lines(file)):
            if index % 2000 == 0:
                time.sleep(0)  # gives the event loop the GIL when run from the bot
            if tags is not None and not any(tag in line for tag in tags):
                continue
            stamp = _epoch_prefix(line)
            if stamp is None or stamp < since:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue  # a line cut by a crash
    return list(rows)


def _capped_note(rows: list) -> str:
    return f" Событий больше {MAX_LINES}: взяты последние." if len(rows) >= MAX_LINES else ""


def lot_history(item_id: str, days: float = LOG_KEEP_DAYS, limit: int = LOT_HISTORY_LIMIT) -> list[dict]:
    """Every find (lot.new) and delivery (send.ok) of one lot in the kept events, newest last."""
    needle = str(item_id)
    since = time.time() - days * 86400
    found: deque = deque(maxlen=limit)
    for file in _files_oldest_first(EVENTS_LOG, since):
        for line in _read_lines(file):
            if needle not in line or not any(tag in line for tag in LOT_TAGS):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str((row.get("f") or {}).get("item")) == needle and (row.get("t") or 0) >= since:
                found.append(row)
    return list(found)


def _sum(rows: Iterable[dict], *path: str) -> int:
    total = 0
    for row in rows:
        value = row
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        total += value or 0
    return total


def _pct(part: float, total: float) -> str:
    return f"{100 * part / total:.0f}%" if total else "—"


def _top(counter: dict, limit: int = 4) -> str:
    return ", ".join(f"{key}×{count}" for key, count in Counter(counter).most_common(limit)) or "—"


def _merge(rows: Iterable[dict], key: str) -> Counter:
    merged: Counter = Counter()
    for row in rows:
        merged.update(row.get(key) or {})
    return merged


def _values(rows: Iterable[dict], *path: str) -> list[float]:
    found = []
    for row in rows:
        value = row
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is not None:
            found.append(value)
    return found


def _worst(rows: list[dict], *path: str) -> str:
    values = _values(rows, *path)
    return f"{max(values):g}" if values else "—"


def _median(rows: list[dict], *path: str) -> str:
    values = sorted(_values(rows, *path))
    return f"{values[len(values) // 2]:g}" if values else "—"


def _channels(metrics: list[dict]) -> dict[str, dict]:
    merged: dict[str, dict] = defaultdict(lambda: {"fail": Counter()})
    for row in metrics:
        for name, stats in (row.get("ch") or {}).items():
            target = merged[name]
            for key in ("http", "ok", "403", "429", "5xx", "terr", "coll", "coll_ok"):
                target[key] = target.get(key, 0) + (stats.get(key) or 0)
            target["fail"].update(stats.get("fail") or {})
            target["state"], target["rate"] = stats.get("state"), stats.get("rate")
            target.setdefault("ms", []).extend([stats["ms_p50"]] if stats.get("ms_p50") is not None else [])
    return merged


def channel_history(name: str, hours: int = 24) -> Optional[dict]:
    """One channel's totals from the minute rows; they survive restarts, unlike METRICS."""
    rows = read_rows(METRICS_LOG, time.time() - hours * 3600)
    stats = _channels(rows).get(name)
    if stats is None:
        return None
    minutes = [row["ch"][name] for row in rows if name in (row.get("ch") or {})]
    stats["minutes"] = len(minutes)
    stats["paused"] = sum(1 for minute in minutes if minute.get("state") == "quarantined")
    return stats


def _outages(metrics: list[dict]) -> list[list[dict]]:
    """Runs of 2+ minutes when scans failed or some link went unchecked; an hourly average hides them."""
    runs: list[list[dict]] = []
    current: list[dict] = []
    for row in metrics:
        ok, failed = _sum([row], "scans", "ok"), _sum([row], "scans", "fail")
        if (ok + failed and failed / (ok + failed) >= OUTAGE_FAIL_SHARE) or (row.get("coverage_s") or 0) > OUTAGE_COVERAGE_S:
            current.append(row)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return [run for run in runs if len(run) >= 2]


def findings(metrics: list[dict], events: list[dict]) -> list[str]:
    """What stands out, so the reader starts with the likely cause and not with the tables."""
    if not metrics:
        return ["Нет минутных метрик за период: бот только что запущен или мониторинг не работал."]
    notes = []
    ok, failed = _sum(metrics, "scans", "ok"), _sum(metrics, "scans", "fail")
    if ok + failed == 0:
        notes.append("Сканов нет вообще: проверьте режим сети и список каналов.")
    elif failed / (ok + failed) >= 0.5:
        notes.append(f"Падает больше половины сканов ({_pct(failed, ok + failed)}), главные причины: {_top(_merge(metrics, 'fail'), 3)}.")
    for name, stats in _channels(metrics).items():
        if stats.get("http", 0) >= 20:
            raw_ok, coll = stats["ok"] / stats["http"], stats.get("coll_ok", 0) / max(stats.get("coll", 0), 1)
            if raw_ok - coll >= 0.3:
                notes.append(f"{name}: HTTP ok {raw_ok:.0%}, но каталог получен лишь в {coll:.0%} сканов: ответы приходят без каталога.")
            if stats["403"] / stats["http"] >= 0.5:
                notes.append(f"{name}: {_pct(stats['403'], stats['http'])} ответов 403, канал почти бесполезен.")
    stalls = _sum(metrics, "stalls")
    if stalls:
        notes.append(f"Event loop зависал {stalls} раз, максимум {_worst(metrics, 'loop_max_ms')} мс: что-то блокирует все корутины.")
    flood = _sum(metrics, "flood")
    if flood:
        notes.append(f"Telegram flood-wait {flood} раз, максимум {_worst(metrics, 'flood_max_s')} с.")
    deficits = [row for row in metrics if (row.get("rps_need") or 0) > (row.get("rps_have") or 0)]
    if len(deficits) >= len(metrics) / 2:
        notes.append(f"Мощности не хватает {len(deficits)} мин из {len(metrics)}: нужно {_median(metrics, 'rps_need')} req/s, есть {_median(metrics, 'rps_have')}.")
    drops = max(_values(metrics, "log_drops") or [0])
    if drops:
        notes.append(f"Потеряно {drops} записей лога: очередь логов переполнялась.")
    starts = [row for row in events if row.get("ev") == "app.start"]
    if len(starts) > 1:
        notes.append(f"Бот перезапускался {len(starts)} раз за период.")
    outages = _outages(metrics)
    if outages:
        worst = max(outages, key=len)
        notes.append(f"Провалов работы: {len(outages)}, всего {sum(map(len, outages))} мин, самый долгий {len(worst)} мин с {worst[0].get('ts')}.")
    if not notes:
        notes.append("Явных проблем не видно.")
    return notes


def _restarts(events: list[dict]) -> list[str]:
    lines = []
    previous_stop = True
    for row in events:
        if row.get("ev") == "app.stop":
            previous_stop = True
        elif row.get("ev") == "app.start":
            fields = row.get("f") or {}
            state = "" if previous_stop else " · ПРЕДЫДУЩИЙ ЗАПУСК УПАЛ (нет app.stop)"
            lines.append(f"{row.get('ts')} версия {fields.get('version')} · сеть {fields.get('net_mode')}{state}")
            previous_stop = False
    return lines


def _timeline(metrics: list[dict], bucket: int = BUCKET_MINUTES) -> list[str]:
    lines = ["время  сканы ok/всего  причины  лоты→очередь  отпр  pub→sent p50  опозд p95  rps нужно/есть  outbox  loop мс  rss"]
    for start in range(0, len(metrics), bucket):
        chunk = metrics[start : start + bucket]
        ok, failed = _sum(chunk, "scans", "ok"), _sum(chunk, "scans", "fail")
        lines.append(
            f"{chunk[0].get('ts', '')[-5:]}  {ok}/{ok + failed} ({_pct(ok, ok + failed)})  {_top(_merge(chunk, 'fail'), 3)}  "
            f"{_sum(chunk, 'lots', 'new')}→{_sum(chunk, 'lots', 'queued')}  {_sum(chunk, 'sent')}  "
            f"{_median(chunk, 'pub_sent_s', 'p50')}  {_worst(chunk, 'due_s', 'p95')}  "
            f"{_median(chunk, 'rps_need')}/{_median(chunk, 'rps_have')}  {_worst(chunk, 'outbox')}  "
            f"{_worst(chunk, 'loop_max_ms')}  {_worst(chunk, 'rss_mb')}"
        )
    return lines


def _channel_table(metrics: list[dict]) -> list[str]:
    lines = ["канал: http · ok · 403 · 429 · 5xx · транспорт · каталог ok/сканы · латентность p50 мс · состояние · rate · причины"]
    for name, stats in sorted(_channels(metrics).items()):
        http = stats.get("http", 0)
        latency = sorted(stats.get("ms") or [0])
        lines.append(
            f"{name}: {http} · {_pct(stats.get('ok', 0), http)} · {_pct(stats.get('403', 0), http)} · {stats.get('429', 0)} · "
            f"{stats.get('5xx', 0)} · {stats.get('terr', 0)} · {stats.get('coll_ok', 0)}/{stats.get('coll', 0)} "
            f"({_pct(stats.get('coll_ok', 0), stats.get('coll', 0))}) · {latency[len(latency) // 2]} · {stats.get('state')} · "
            f"{stats.get('rate')} · {_top(stats['fail'], 3)}"
        )
    return lines


def _failure_details(events: list[dict]) -> list[str]:
    details: Counter = Counter()
    for row in events:
        if row.get("ev") == "scan.fail":
            fields = row.get("f") or {}
            details[f"{fields.get('kind')}: {re.sub(r'[0-9]+ КБ', 'N КБ', fields.get('detail') or 'без деталей')}"] += 1
    return [f"{count}× {text}" for text, count in details.most_common(12)]


def _worst_sources(events: list[dict]) -> list[str]:
    failures: dict[str, Counter] = defaultdict(Counter)
    for row in events:
        if row.get("ev") == "scan.fail":
            fields = row.get("f") or {}
            failures[str(fields.get("src"))][fields.get("kind")] += 1
    ranked = sorted(failures.items(), key=lambda pair: -sum(pair[1].values()))[:10]
    return [f"{sum(kinds.values())}× «{source}»: {_top(kinds, 3)}" for source, kinds in ranked]


def _challenges(metrics: list[dict]) -> list[str]:
    merged: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0.0])
    for row in metrics:
        for kind, stats in (row.get("challenge") or {}).items():
            attempts = stats["ok"] + stats["fail"]
            merged[kind][0] += stats["ok"]
            merged[kind][1] += stats["fail"]
            merged[kind][2] += stats["avg_s"] * attempts
    return [
        f"{kind}: решено {int(ok)}/{int(ok + failed)} ({_pct(ok, ok + failed)}), среднее {seconds / max(ok + failed, 1):.1f} с"
        for kind, (ok, failed, seconds) in sorted(merged.items())
    ]


def _funnel(metrics: list[dict]) -> str:
    lots = _merge((row for row in metrics), "lots")
    return (
        f"новых {lots['new']} → в очередь {lots['queued']} · отсеяно: возраст {lots['old']}, стоп-слова {lots['stop']}, "
        f"продавцы {lots['seller']}, название {lots['title']} · отправлено {_sum(metrics, 'sent')}"
    )


def _error_groups(events: list[dict]) -> list[str]:
    groups: dict[str, dict] = {}
    for row in events:
        if row.get("l") not in ("W", "E", "C"):
            continue
        text = str(row.get("msg") or row.get("ev") or "")
        key = re.sub(r"\d+", "N", text)[:90]
        group = groups.setdefault(key, {"count": 0, "first": row.get("ts"), "level": row.get("l"), "row": row})
        group["count"] += 1
        group["last"] = row.get("ts")
        if row.get("exc"):
            group["row"] = row
    ranked = sorted(groups.values(), key=lambda g: ("CEW".index(g["level"]), -g["count"]))[:ERROR_GROUPS]
    lines = []
    for group in ranked:
        row = group["row"]
        lines.append(f"[{group['level']}] {group['count']}× {group['first']} … {group['last']} · {row.get('c')} · {str(row.get('msg') or row.get('ev'))[:300]}")
        if row.get("exc"):
            lines.append("    " + row["exc"][-SAMPLE_TRACEBACK_CHARS:].replace("\n", "\n    "))
    return lines


def _compact(row: dict) -> str:
    fields = row.get("f")
    text = row.get("msg") or ""
    tail = json.dumps(fields, ensure_ascii=False) if fields else ""
    return f"{row.get('ts')} {row.get('l')} {row.get('c')} {row.get('id', '····')} {row.get('ev', '')} {text} {tail}".rstrip()


STALL_MARK = re.compile(r"^=== зависание (.*) · ([\d.]+) с · epoch (\d+) ===$")
CRASH_DUMPS_SHOWN = 4
CRASH_STACK_LINES = 14


def _main_thread(chunk: list[str]) -> list[str]:
    """The event loop thread is the one running asyncio; the other threads are idle pools."""
    blocks, current = [], []
    for line in chunk:
        if line.startswith(("Thread 0x", "Current thread 0x")) and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)
    main = next((b for b in reversed(blocks) if any("asyncio" in line or "bot.py" in line for line in b)), blocks[-1] if blocks else [])
    frames = [line for line in main if line.strip()]
    return frames[:CRASH_STACK_LINES]


def _crash_tail(since: float = 0.0) -> list[str]:
    """Dated stall dumps of the period, main thread only. Dumps without a date predate the marks and are skipped,
    except a native crash (Fatal Python error) written during the period."""
    if not CRASH_LOG.is_file() or CRASH_LOG.stat().st_size == 0:
        return []
    lines = CRASH_LOG.read_text(encoding="utf-8", errors="replace")[-200000:].splitlines()
    dumps, chunk = [], []
    for line in lines:
        mark = STALL_MARK.match(line.strip())
        if mark:
            if int(mark.group(3)) >= since:
                dumps.append([f"{mark.group(1)} · пауза {mark.group(2)} с", *("  " + l for l in _main_thread(chunk))])
            chunk = []
        else:
            chunk.append(line)
    if any("Fatal Python error" in line for line in chunk) and CRASH_LOG.stat().st_mtime >= since:
        dumps.append(["Падение (Fatal Python error) в конце файла:", *("  " + l for l in chunk[-CRASH_STACK_LINES:])])
    result = [line for dump in dumps[-CRASH_DUMPS_SHOWN:] for line in dump]
    if len(dumps) > CRASH_DUMPS_SHOWN:
        result.insert(0, f"Показаны последние {CRASH_DUMPS_SHOWN} из {len(dumps)} дампов.")
    return result


def _stall_rows(metrics: list[dict]) -> list[dict]:
    return [row for row in metrics if (row.get("loop_max_ms") or 0) >= LONG_STALL_MS]


def stall_cause(rows: list[dict]) -> str:
    """The likely reason for long stalls from the server numbers of those minutes."""
    measured = [row for row in rows if "steal" in row]
    if not rows:
        return ""
    if not measured:
        return "системных цифр нет: бот старой версии или не на Linux"
    steal = max(row.get("steal") or 0 for row in measured)
    iowait = max(row.get("iowait") or 0 for row in measured)
    swap_in = max(row.get("swap_in") or 0 for row in measured)
    server = max(row.get("sys_cpu") or 0 for row in measured)
    own = max(row.get("cpu_pct") or 0 for row in measured)
    if steal >= 20:
        return f"хостинг забирает процессор (steal до {steal:g}%): поможет только другой сервер"
    if iowait >= 20 or swap_in >= 100:
        return f"сервер ждёт диск и подкачку (iowait до {iowait:g}%, swap-in {swap_in}): не хватает памяти"
    if server >= 90 and own >= 70:
        return f"процессор занят самим ботом ({own:g}%): тяжёлая работа в основном потоке, стек — в crash.log"
    if server >= 90:
        return f"процессор занят другими программами на сервере (всего {server:g}%, бот {own:g}%)"
    return "сервер в эти минуты не перегружен: код бота держит поток, стек — в crash.log"


def _stall_lines(metrics: list[dict]) -> list[str]:
    rows = _stall_rows(metrics)
    if not rows:
        return []
    lines = [f"Причина, похоже: {stall_cause(rows)}", "минута · пауза · cpu бота · cpu сервера · iowait · steal · swap-in · свободно МБ"]
    for row in rows[-12:]:
        lines.append(
            f"{row.get('ts')} · {(row.get('loop_max_ms') or 0) / 1000:.1f} с · {row.get('cpu_pct', '—')}% · "
            f"{row.get('sys_cpu', '—')}% · {row.get('iowait', '—')}% · {row.get('steal', '—')}% · "
            f"{row.get('swap_in', '—')} · {row.get('mem_avail_mb', '—')}"
        )
    return lines


def _section(title: str, lines: list[str]) -> list[str]:
    return [f"## {title}", *(lines or ["нет данных"]), ""]


def build_report(minutes: int = 60, live_state: Optional[dict] = None) -> str:
    now = time.time()
    since = now - minutes * 60
    events = read_rows(EVENTS_LOG, since)
    metrics = read_rows(METRICS_LOG, since)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now + 3 * 3600))
    lines = [f"# Avito Radar: отчёт за {minutes} мин, {stamp} МСК",
             f"Минутных строк: {len(metrics)}, событий: {len(events)}.{_capped_note(events)}", ""]
    lines += _section("Главное", [f"- {note}" for note in findings(metrics, events)])
    lines += _section("Запуски", _restarts(events))
    if live_state:
        lines += _section("Состояние сейчас", [json.dumps(live_state, ensure_ascii=False, default=str)])
    lines += _section(f"Динамика по {BUCKET_MINUTES} мин", _timeline(metrics))
    lines += _section("Каналы", _channel_table(metrics))
    lines += _section("Причины провалов сканов", _failure_details(events))
    lines += _section("Худшие источники", _worst_sources(events))
    lines += _section("Антибот", _challenges(metrics))
    lines += _section("Воронка лотов", [_funnel(metrics)])
    lines += _section("Ошибки и предупреждения", _error_groups(events))
    stalls = _stall_lines(metrics)
    if stalls:
        lines += _section("Зависания", stalls)
    crash = _crash_tail(since)
    if crash:
        lines += _section("crash.log", crash)
    lines += _section(
        f"Последние {TAIL_EVENTS} важных событий",
        [_compact(row) for row in [r for r in events if r.get("l") in ("W", "E", "C")][-TAIL_EVENTS:]],
    )
    return "\n".join(lines)


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * share))] if ordered else 0.0


def _grade(value: float, good: float, fair: float) -> str:
    """Higher is better when good > fair, lower is better otherwise."""
    if good >= fair:
        return "✅" if value >= good else "⚠️" if value >= fair else "❌"
    return "✅" if value <= good else "⚠️" if value <= fair else "❌"


def _lots(events: list[dict]) -> list[dict]:
    return [row.get("f") or {} for row in events if row.get("ev") == "lot.new"]


def _bot_waits(lots: list[dict]) -> list[float]:
    """How long a lot already visible on Avito waited for the bot's next look."""
    return [min(lot["gap"], lot["age"]) for lot in lots if lot.get("fresh") and not lot.get("promo")]


def _safe_capacity_share(metrics: list[dict]) -> float:
    """Share of minutes when proven channels cover the load at the safe per-IP rate, with a 20 % reserve.
    The server IP counts as one channel: in smart mode it carries the load alone and idle proxies prove nothing."""
    covered = total = 0
    for start in range(0, len(metrics), CAPACITY_WINDOW_MIN):
        window = metrics[start:start + CAPACITY_WINDOW_MIN]
        needs = [row["rps_need"] for row in window if row.get("rps_need")]
        if not needs:
            continue
        good = sum(
            1 for stats in _channels(window).values()
            if stats.get("http", 0) >= 3 and stats.get("ok", 0) / stats["http"] >= GOOD_CHANNEL_OK
        )
        total += len(needs)
        covered += len(needs) if good * SAFE_RPS_PER_IP >= max(needs) * 1.2 else 0
    return covered / total if total else 0.0


def _worst_link(rows: list[dict]) -> str:
    worst = max(rows, key=lambda row: row.get("coverage_s") or 0, default={})
    return f" (#{worst['coverage_rid']})" if worst.get("coverage_rid") else ""


def readiness_checks(metrics: list[dict], events: list[dict], hours: int) -> list[str]:
    """Each line starts with ✅ ⚠️ ❌ or ⏳ (not enough data). Avito's own delay is not graded: the bot cannot change it."""
    checks = []

    def add(mark: str, name: str, value: str, norm: str) -> None:
        checks.append(f"{mark} {name}: {value} (норма: {norm})")

    seen = min(1.0, len(metrics) / (hours * 60))
    crashes = sum("УПАЛ" in line for line in _restarts(events))
    mark = "⏳" if seen < 0.8 else "❌" if crashes else _grade(seen, 0.99, 0.95)
    add(mark, "Работа без перерывов", f"{seen:.1%} времени, падений {crashes}", "99 % и 0 падений")

    coverage = [row["coverage_s"] for row in metrics if row.get("coverage_s") is not None]
    if coverage:
        share, worst = sum(1 for age in coverage if age <= COVERED_S) / len(coverage), max(coverage)
        mark = _grade(share, 0.99, 0.95)
        add("⚠️" if mark == "✅" and worst > 300 else mark, "Все ссылки под присмотром",
            f"{share:.1%} минут, худшая слепая зона {worst:.0f} с{_worst_link(metrics)}", f"99 % минут за {COVERED_S} с, зона до 300 с")
    else:
        add("⏳", "Все ссылки под присмотром", "нет данных", "99 % минут")

    ok, failed = _sum(metrics, "scans", "ok"), _sum(metrics, "scans", "fail")
    add(_grade(ok / (ok + failed), 0.97, 0.93) if ok + failed else "⏳", "Успешные сканы", f"{_pct(ok, ok + failed)} из {ok + failed}", "97 %")

    waits = _bot_waits(_lots(events))
    value = f"p95 {_percentile(waits, 0.95):.0f} с по {len(waits)} лотам"
    add(_grade(_percentile(waits, 0.95), 45, 90) if len(waits) >= ENOUGH_LOTS else "⏳", "Бот замечает лот", value, f"p95 до 45 с, от {ENOUGH_LOTS} лотов")

    sent, lost = _sum(metrics, "sent"), _sum(metrics, "lost", "expired") + _sum(metrics, "lost", "failed")
    flood = _sum(metrics, "flood")
    mark = _grade(lost / (sent + lost), 0.005, 0.02) if sent + lost else "⏳"
    add("⚠️" if mark == "✅" and flood else mark, "Доставка", f"отправлено {sent}, потеряно {lost}, flood-wait {flood}", "потери до 0,5 %, без flood-wait")

    stall_rows = _stall_rows(metrics)
    long_stalls = len(stall_rows)
    cause = f" · похоже: {stall_cause(stall_rows)}" if stall_rows else ""
    add(_grade(long_stalls, 0, 2), "Зависания бота", f"{long_stalls} минут с паузой от {LONG_STALL_MS // 1000} с{cause}", "0")

    capacity = _safe_capacity_share(metrics)
    add(_grade(capacity, 0.95, 0.8) if metrics else "⏳", "Запас хороших каналов", f"хватает {capacity:.0%} минут",
        f"95 % минут при {SAFE_RPS_PER_IP} запр/с на IP и запасе 20 %")
    return checks


def readiness_verdict(checks: list[str]) -> str:
    marks = {line.split(" ", 1)[0] for line in checks}
    if "❌" in marks:
        return "🔴 Продавать рано: есть проваленные пункты."
    if "⏳" in marks:
        return "🟡 Данных мало: вердикт будет точным после суток работы и 20+ лотов."
    if "⚠️" in marks:
        return "🟡 Можно продавать осторожно: есть пункты на грани."
    return "🟢 Можно продавать: все пункты в норме."


def _lot_timing(events: list[dict]) -> list[str]:
    lots = _lots(events)
    if not lots:
        return []
    organic = [lot for lot in lots if not lot.get("promo")]
    fresh = [lot for lot in organic if lot.get("fresh")]
    old = [lot for lot in organic if not lot.get("fresh")]

    def split(group: list[dict], title: str) -> list[str]:
        if not group:
            return []
        ages = [lot["age"] for lot in group]
        bot = [min(lot["gap"], lot["age"]) for lot in group]
        hidden = [lot["age"] - min(lot["gap"], lot["age"]) for lot in group]
        return [
            f"{title} ({len(group)}): от публикации до бота p50 {_percentile(ages, 0.5):.0f} с, p95 {_percentile(ages, 0.95):.0f} с",
            f"  Авито не показывал: p50 {_percentile(hidden, 0.5):.0f} с, p95 {_percentile(hidden, 0.95):.0f} с",
            f"  бот не смотрел: p50 {_percentile(bot, 0.5):.0f} с, p95 {_percentile(bot, 0.95):.0f} с",
        ]

    by_radar = Counter(lot.get("rid") for lot in old)
    return [
        f"Новых лотов с точным временем: {len(lots)}, реклама {len(lots) - len(organic)}.",
        *split(fresh, "Свежие"),
        *split(old, "Старше лимита радара"),
        *([f"Старые по радарам: {_top(by_radar, 5)}"] if by_radar else []),
        *_old_lot_origin(fresh, old),
    ]


def _old_lot_origin(fresh: list[dict], old: list[dict]) -> list[str]:
    """Deep positions mean a late insert into the list; one or two channels mean they see a different list."""
    old_places = [lot["pos"] for lot in old if lot.get("pos") is not None]
    if not old_places:
        return []
    fresh_places = [lot["pos"] for lot in fresh if lot.get("pos") is not None]
    channels = Counter(lot.get("ch") or "?" for lot in old)
    return [
        f"Место в выдаче: старые p50 {_percentile(old_places, 0.5):.0f}, p95 {_percentile(old_places, 0.95):.0f}; "
        f"свежие p50 {_percentile(fresh_places, 0.5):.0f}, p95 {_percentile(fresh_places, 0.95):.0f}",
        f"Старые по каналам: {_top(channels, 5)}",
    ]


def _radar_speed(events: list[dict]) -> list[str]:
    """One flooded radar sets the shared percentiles, so each is shown alone and the rest without the busiest."""
    by_radar: dict = defaultdict(list)
    for lot in _lots(events):
        if not lot.get("promo"):
            by_radar[lot.get("rid")].append(lot)
    ranked = sorted(by_radar.items(), key=lambda pair: -len(pair[1]))
    if not ranked:
        return []

    def fresh_ages(lots: list[dict]) -> list[float]:
        return [lot["age"] for lot in lots if lot.get("fresh")]

    lines = ["радар: лотов · старше лимита · свежие от публикации до бота p50 · p95"]
    for rid, lots in ranked[:RADAR_SPEED_ROWS]:
        ages = fresh_ages(lots)
        speed = f"{_percentile(ages, 0.5):.0f} с · {_percentile(ages, 0.95):.0f} с" if ages else "— · —"
        lines.append(f"#{rid}: {len(lots)} · {_pct(len(lots) - len(ages), len(lots))} · {speed}")
    others = fresh_ages([lot for _, lots in ranked[1:] for lot in lots])
    if others:
        share = _pct(len(ranked[0][1]), sum(len(lots) for _, lots in ranked))
        lines.append(
            f"Без #{ranked[0][0]} (у него {share} лотов): p50 {_percentile(others, 0.5):.0f} с, "
            f"p95 {_percentile(others, 0.95):.0f} с, свежих {len(others)}"
        )
    return lines


def _channel_load(metrics: list[dict]) -> list[str]:
    lines = ["канал: запр/мин · ok · 403/429 · обрывы · каталог ok · на паузе"]
    for name, stats in sorted(_channels(metrics).items()):
        minutes = [row["ch"][name] for row in metrics if name in (row.get("ch") or {})]
        http = stats.get("http", 0)
        blocks = stats.get("403", 0) + stats.get("429", 0)
        paused = sum(1 for minute in minutes if minute.get("state") == "quarantined")
        lines.append(
            f"{name}: {http / max(len(minutes), 1):.1f} · {_pct(stats.get('ok', 0), http)} · {_pct(blocks, http)} · "
            f"{stats.get('terr', 0)} · {_pct(stats.get('coll_ok', 0), stats.get('coll', 0))} · {paused} мин из {len(minutes)}"
        )
    return lines


def _source_table(metrics: list[dict]) -> list[str]:
    merged: dict[str, dict] = {}
    for row in metrics:
        for key, stats in (row.get("src") or {}).items():
            target = merged.setdefault(key, Counter())
            target.update({field: stats.get(field, 0) for field in ("ok", "fail", "retry", "blocked")})
            target["q"], target["to"] = stats.get("q"), stats.get("to")
    if not merged:
        return []
    lines = ["радар «запрос» → адрес на Авито: сканов · провалов · повторов · блоков"]
    for key, stats in sorted(merged.items(), key=lambda pair: -pair[1]["fail"]):
        scans = stats["ok"] + stats["fail"]
        lines.append(f"#{key} «{stats['q']}» → {stats['to']}: {scans} · {_pct(stats['fail'], scans)} · {stats['retry']} · {stats['blocked']}")
    return lines


def _outage_lines(metrics: list[dict]) -> list[str]:
    lines = []
    for run in _outages(metrics):
        ok, failed = _sum(run, "scans", "ok"), _sum(run, "scans", "fail")
        dead = [
            name for name, stats in sorted(_channels(run).items())
            if stats.get("http", 0) >= 5 and stats.get("ok", 0) / stats["http"] < 0.1
        ]
        lines.append(
            f"{run[0].get('ts')} … {str(run[-1].get('ts'))[-5:]} · {len(run)} мин · провалов {failed} из {ok + failed} · "
            f"слепая зона до {_worst(run, 'coverage_s')} с{_worst_link(run)} · {_top(_merge(run, 'fail'), 3)}"
            + (f" · не отвечали: {', '.join(dead)}" if dead else "")
        )
    return lines


def build_readiness(hours: int = 24, live_state: Optional[dict] = None) -> str:
    """The sale decision on one page: verdict first, then the numbers behind each point."""
    now = time.time()
    events = read_rows(EVENTS_LOG, now - hours * 3600, READINESS_TAGS)
    metrics = read_rows(METRICS_LOG, now - hours * 3600)
    checks = readiness_checks(metrics, events, hours)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now + 3 * 3600))
    lines = [f"# Avito Radar: готовность к продаже за {hours} ч, {stamp} МСК",
             f"Минутных строк: {len(metrics)}, событий: {len(events)}.{_capped_note(events)}", ""]
    lines += _section("Вердикт", [readiness_verdict(checks)])
    lines += _section("Критерии", checks)
    lines += _section("Скорость лота", _lot_timing(events))
    lines += _section("Скорость по радарам", _radar_speed(events))
    lines += _section("Провалы работы", _outage_lines(metrics))
    lines += _section("По часам", _timeline(metrics, 60))
    lines += _section("Каналы", _channel_load(metrics))
    lines += _section("Ссылки", _source_table(metrics))
    lines += _section("Воронка лотов", [_funnel(metrics)])
    lines += _section("Антибот", _challenges(metrics))
    lines += _section("Запуски", _restarts(events))
    if live_state:
        lines += _section("Состояние сейчас", [json.dumps(live_state, ensure_ascii=False, default=str)])
    lines += _section("Ошибки и предупреждения", _error_groups(events))
    stalls = _stall_lines(metrics)
    if stalls:
        lines += _section("Зависания", stalls)
    crash = _crash_tail(now - hours * 3600)
    if crash:
        lines += _section("crash.log", crash)
    return "\n".join(lines)


# ── Load report: plan against fact, where scan time goes, what the channels can carry ──

LOAD_SUM_KEYS = ("http", "ok", "403", "429", "5xx", "terr", "coll", "coll_ok", "fetch", "busy_s", "wait_s", "waited",
                 "pace_s", "http_s", "terr_s", "kb", "pages", "page_kb")
LOAD_WINDOW_MIN = 5  # a channel's rate and its blocks are judged over 5 minutes, one minute holds too few requests
RATE_BINS = (3, 6, 12, 18, 24, 30)  # requests per minute through one IP; 30 a minute is 0.5 req/s
BANNED_SHARE = 0.3  # this share of 403/429 or of broken requests makes a channel useless
BUSY_FOR_CEILING_S = 30.0  # a channel's throughput ceiling is measured only after this much work
SLOW_PAGE_S = 3.0
TOP_BOUND = {"lat_h": 20.0, "scan_h": 300.0, "due_h": 300.0, "revisit_h": 300.0}


def _bound(key: str) -> float:
    return float("inf") if key == "inf" else float(key)


def _hist_merge(rows: Iterable[dict], *path: str) -> Counter:
    merged: Counter = Counter()
    for value in _values(rows, *path):
        if isinstance(value, dict):
            merged.update(value)
    return merged


def _hist_value(hist: Counter, share: float) -> Optional[float]:
    """Upper bound of the bucket that holds `share` of the samples; None without samples."""
    total = sum(hist.values())
    if not total:
        return None
    target, running = max(1, math.ceil(total * share)), 0
    for key in sorted(hist, key=_bound):
        running += hist[key]
        if running >= target:
            return _bound(key)
    return None


def _hist_text(hist: Counter, share: float, kind: str, scale: float = 1.0, unit: str = "с") -> str:
    value = _hist_value(hist, share)
    if value is None:
        return "—"
    if math.isinf(value):
        return f"> {TOP_BOUND[kind] * scale:g} {unit}"
    return f"≤ {value * scale:g} {unit}"


def _load_channels(metrics: list[dict]) -> dict[str, dict]:
    merged: dict[str, dict] = defaultdict(lambda: {"lat_h": Counter(), "fail": Counter(), "ttfb_sum": 0.0})
    for row in metrics:
        for name, stats in (row.get("ch") or {}).items():
            target = merged[name]
            for key in LOAD_SUM_KEYS:
                target[key] = target.get(key, 0) + (stats.get(key) or 0)
            target["lat_h"].update(stats.get("lat_h") or {})
            target["fail"].update(stats.get("fail") or {})
            target["ttfb_sum"] += (stats.get("ttfb_ms") or 0) * (stats.get("pages") or 0)
            target["state"], target["rate"] = stats.get("state"), stats.get("rate")
    return merged


def _channel_verdict(stats: dict) -> str:
    http = stats.get("http", 0)
    if http < 10:
        return "мало данных"
    if (stats.get("403", 0) + stats.get("429", 0)) / http >= BANNED_SHARE:
        return "блок Авито"
    if stats.get("terr", 0) / http >= BANNED_SHARE:
        return "обрывы"
    if stats.get("ok", 0) / http < GOOD_CHANNEL_OK:
        return "слабый"
    page_p50 = _hist_value(stats["lat_h"], 0.5)
    return "медленный" if page_p50 is not None and page_p50 > SLOW_PAGE_S else "хороший"


def _usable(stats: dict) -> bool:
    return _channel_verdict(stats) in ("хороший", "медленный")


def _ceiling_rps(stats: dict, lanes: int = 1) -> Optional[float]:
    """Requests per second the channel gives when busy all the time: it serves `lanes` requests at a time,
    so the ceiling is lanes / (time per request), capped by its rate limit."""
    work = stats.get("busy_s", 0) - stats.get("pace_s", 0)
    if work < BUSY_FOR_CEILING_S or not stats.get("http"):
        return None
    return min(lanes * stats["http"] / work, float(stats.get("rate") or 1.5))


def _lanes_of(name: str, metrics: list[dict]) -> int:
    """Lanes of a channel in the period: the server always has one, proxies the setting of the minute rows."""
    if name == "direct":
        return 1
    values = sorted(_values(metrics, "lanes"))
    return max(1, int(values[len(values) // 2])) if values else 1


def _load_summary(metrics: list[dict]) -> dict:
    """The numbers every section of the load report reads."""
    minutes = len(metrics)
    loads = [row.get("load") or {} for row in metrics]
    channels = _load_channels(metrics)
    usable = {name: stats for name, stats in channels.items() if _usable(stats)}
    scans = _sum(metrics, "scans", "ok") + _sum(metrics, "scans", "fail")
    http = sum(stats.get("http", 0) for stats in channels.values())
    plan_values = sorted(load.get("plan_sps") or 0 for load in loads if load.get("plan_sps"))
    plan_sps = plan_values[len(plan_values) // 2] if plan_values else 0.0
    urls = sorted(row.get("urls") or 0 for row in metrics if row.get("urls"))
    url_count = urls[len(urls) // 2] if urls else 0
    workers = sum(load.get("workers") or 0 for load in loads) / max(minutes, 1)
    busy_workers = sum(load.get("busy") or 0 for load in loads) / max(minutes, 1)
    lanes = {name: _lanes_of(name, metrics) for name in channels}
    ceilings = {name: _ceiling_rps(stats, lanes[name]) for name, stats in usable.items()}
    measured = [value for value in ceilings.values() if value]
    challenge_s = sum(
        stats.get("avg_s", 0) * (stats.get("ok", 0) + stats.get("fail", 0))
        for row in metrics for stats in (row.get("challenge") or {}).values()
    )
    return {
        "minutes": minutes,
        "channels": channels,
        "usable": usable,
        "ceilings": ceilings,
        "pool_rps": sum(measured) + (len(usable) - len(measured)) * (sum(measured) / len(measured) if measured else 0.0),
        "pool_safe_rps": (
            sum(min(value, SAFE_RPS_PER_IP) for value in measured)
            + (len(usable) - len(measured)) * (min(sum(measured) / len(measured), SAFE_RPS_PER_IP) if measured else 0.0)
        ),
        "scans": scans,
        "http": http,
        "fact_sps": scans / (60.0 * max(minutes, 1)),
        "plan_sps": plan_sps,
        "urls": url_count,
        "interval": url_count / plan_sps if plan_sps else 0.0,
        "cost": http / scans if scans else 0.0,
        "workers": workers,
        "busy_workers": busy_workers,
        "queue": sum(load.get("queue") or 0 for load in loads) / max(minutes, 1),
        "queue_max": max((load.get("queue_max") or 0 for load in loads), default=0),
        "oldest_s": max((load.get("oldest_s") or 0 for load in loads), default=0),
        "scan_busy_s": sum(load.get("scan_busy_s") or 0 for load in loads),
        "wait_s": sum(stats.get("wait_s", 0) for stats in channels.values()),
        "http_s": sum(stats.get("http_s", 0) for stats in channels.values()),
        "terr_s": sum(stats.get("terr_s", 0) for stats in channels.values()),
        "pace_s": sum(stats.get("pace_s", 0) for stats in channels.values()),
        "challenge_s": challenge_s,
        "lanes": lanes,
        "good_busy": (
            sum(stats.get("busy_s", 0) / lanes[name] for name, stats in usable.items()) / (60.0 * minutes * len(usable))
            if usable and minutes else 0.0
        ),
        "revisit_h": _hist_merge(loads, "revisit_h"),
        "due_h": _hist_merge(loads, "due_h"),
        "scan_h": _hist_merge(loads, "scan_h"),
        "tries": _hist_merge(loads, "tries"),
    }


def _time_split(s: dict) -> list[tuple[str, float]]:
    """Where the scan workers' time went, largest first."""
    total = s["scan_busy_s"]
    if total <= 0:
        return []
    parts = [
        ("ждали занятый канал", s["wait_s"]),
        ("ответы Авито", s["http_s"]),
        ("таймауты и обрывы", s["terr_s"]),
        ("капча", s["challenge_s"]),
        ("лимит частоты", s["pace_s"]),
    ]
    parts.append(("разбор и прочее", max(0.0, total - sum(value for _, value in parts))))
    return sorted(((name, min(1.0, value / total)) for name, value in parts), key=lambda part: -part[1])


def _load_conclusions(s: dict, metrics: list[dict]) -> list[str]:
    lines = []
    fulfil = s["fact_sps"] / s["plan_sps"] if s["plan_sps"] else 0.0
    revisit = _hist_text(s["revisit_h"], 0.5, "revisit_h")
    lines.append(
        f"Сканов по плану: {fulfil:.0%} (факт {s['fact_sps']:.2f} из {s['plan_sps']:.2f} в секунду). "
        f"Ссылку проверяют в среднем раз в {revisit}, цель {s['interval']:.0f} с."
    )
    worker_use = s["busy_workers"] / s["workers"] if s["workers"] else 0.0
    split = _time_split(s)
    top = ", ".join(f"{name} {share:.0%}" for name, share in split[:3]) or "нет данных"
    due_p95 = _hist_value(s["due_h"], 0.95) or 0.0
    cpu_p95 = _percentile(_values(metrics, "cpu_pct"), 0.95)
    if fulfil >= 0.95 and due_p95 <= 5:
        lines.append(f"Бот успевает: сканы идут по плану. Хорошие каналы заняты на {s['good_busy']:.0%}.")
    elif cpu_p95 >= 85:
        lines.append(f"Упор в процессор бота: CPU p95 {cpu_p95:g}% одного ядра. Время сканов: {top}.")
    elif s["good_busy"] >= 0.8:
        lines.append(
            f"Упор в каналы: хорошие каналы заняты {s['good_busy']:.0%} времени (по одному запросу за раз), "
            f"воркеры заняты на {worker_use:.0%}. Время сканов: {top}."
        )
    elif worker_use >= 0.9:
        lines.append(
            f"Упор в воркеры: заняты {worker_use:.0%} из {s['workers']:.0f}, а хорошие каналы работают лишь "
            f"{s['good_busy']:.0%} времени. Время сканов: {top}."
        )
    elif s["queue"] < 1:
        lines.append("Очереди нет, но сканов меньше плана: ссылки замедлены после ошибок (интервал ×5) или каналы на паузе.")
    else:
        lines.append(f"Сканы отстают. Время сканов: {top}.")
    if s["usable"] and s["pool_rps"] and s["cost"] and s["interval"]:
        per_channel = s["pool_rps"] / len(s["usable"])
        links = s["pool_rps"] * s["interval"] / s["cost"]
        need = s["urls"] * s["cost"] / s["interval"]
        lines.append(
            f"Мощность: {len(s['usable'])} хороших каналов дают {s['pool_rps']:.2f} запр/с "
            f"(≈{per_channel:.2f} на канал) → ~{links:.0f} ссылок при проверке раз в {s['interval']:.0f} с. "
            f"Сейчас ссылок {s['urls']}: им нужно {need:.2f} запр/с, это ~{math.ceil(need / per_channel)} таких каналов, "
            f"с запасом 20% — {math.ceil(need * 1.2 / per_channel)}."
        )
        if s["pool_safe_rps"] < s["pool_rps"] * 0.95:
            lines.append(
                f"Если держать не больше {SAFE_RPS_PER_IP} запр/с на IP: {s['pool_safe_rps']:.2f} запр/с → "
                f"~{s['pool_safe_rps'] * s['interval'] / s['cost']:.0f} ссылок."
            )
    bot_view = _median(metrics, "rps_have")
    limits = _median(metrics, "rps_limits")
    if bot_view != "—" and limits != "—":
        lines.append(f"Оценка мощности у бота: {bot_view} запр/с по замерам каналов (сумма лимитов частоты {limits}).")
    elif bot_view != "—":
        lines.append(f"Бот сам считает, что у него {bot_view} запр/с: это сумма лимитов частоты, а не то, что каналы реально успевают.")
    stretches = [value for value in _values(metrics, "stretch") if value]
    if stretches and max(stretches) > 1.0:
        stretched = sum(1 for value in stretches if value > 1.0)
        lines.append(
            f"Интервалы растягивались из-за нехватки мощности {stretched} мин из {len(stretches)}, "
            f"до ×{max(stretches):g}: тарифы сохраняли пропорцию, ссылки не копились в очередь."
        )
    lanes = sorted(set(_values(metrics, "lanes")))
    if lanes:
        lines.append(f"Запросов на прокси одновременно: {'/'.join(f'{value:g}' for value in lanes)}.")
    return lines


def _plan_lines(s: dict) -> list[str]:
    tries = s["tries"]
    tried = sum(tries.values())
    tries_text = ", ".join(f"{key} канал{'а' if key != '1' else ''} — {_pct(count, tried)}" for key, count in sorted(tries.items())) or "—"
    worker_use = s["busy_workers"] / s["workers"] if s["workers"] else 0.0
    return [
        f"ссылок: {s['urls']}, цель: раз в {s['interval']:.1f} с, план {s['plan_sps']:.2f} скан/с",
        f"факт: {s['fact_sps']:.2f} скан/с, {s['http'] / (60.0 * max(s['minutes'], 1)):.2f} запр/с, запросов на скан {s['cost']:.2f}",
        f"повторная проверка ссылки: p50 {_hist_text(s['revisit_h'], 0.5, 'revisit_h')} · p95 {_hist_text(s['revisit_h'], 0.95, 'revisit_h')}",
        f"опоздание старта скана: p50 {_hist_text(s['due_h'], 0.5, 'due_h')} · p95 {_hist_text(s['due_h'], 0.95, 'due_h')}",
        f"ждут свободного воркера: в среднем {s['queue']:.1f} ссылок, максимум {s['queue_max']}, самая старая {s['oldest_s']:.0f} с",
        f"воркеры: {s['workers']:.0f}, заняты в среднем {s['busy_workers']:.1f} ({worker_use:.0%})",
        f"длительность скана: p50 {_hist_text(s['scan_h'], 0.5, 'scan_h')} · p95 {_hist_text(s['scan_h'], 0.95, 'scan_h')}",
        f"каналов на скан: {tries_text}",
    ]


def _load_channel_lines(s: dict) -> list[str]:
    minutes = max(s["minutes"], 1)
    lines = ["канал: запр/мин · занят · ждали его · ok · 403/429 · обрывы · страница p50/p95 · до 1-го байта · КБ/стр · потолок · вывод"]
    for name, stats in sorted(s["channels"].items(), key=lambda pair: -pair[1].get("http", 0)):
        http, pages = stats.get("http", 0), stats.get("pages", 0)
        ceiling = s["ceilings"].get(name)
        verdict = _channel_verdict(stats)
        if pages:
            page = (f"{_hist_text(stats['lat_h'], 0.5, 'lat_h', 1000, 'мс')}/"
                    f"{_hist_text(stats['lat_h'], 0.95, 'lat_h', 1000, 'мс')} · "
                    f"{stats['ttfb_sum'] / pages:.0f} мс · {stats.get('page_kb', 0) / pages:.0f}")
        else:
            page = "— · — · —"
        lines.append(
            f"{name}: {http / minutes:.1f} · {stats.get('busy_s', 0) / (60.0 * minutes * s['lanes'].get(name, 1)):.0%} · "
            f"{stats.get('wait_s', 0) / max(stats.get('fetch', 0), 1):.1f} с · {_pct(stats.get('ok', 0), http)} · "
            f"{_pct(stats.get('403', 0) + stats.get('429', 0), http)} · {_pct(stats.get('terr', 0), http)} · {page} · "
            f"{f'{ceiling:.2f}/с' if ceiling else '—'} · {verdict}"
            + ("" if verdict in ("хороший", "медленный") else f" ({_top(stats['fail'], 2)})")
        )
    return lines


def _rate_block_lines(metrics: list[dict], s: dict) -> list[str]:
    """Blocks against the request rate of one IP, over 5-minute windows of the channels that are not banned:
    shows the rate where Avito starts to answer 403/429."""
    bins: dict[int, Counter] = defaultdict(Counter)
    names = [name for name, stats in s["channels"].items() if _channel_verdict(stats) not in ("блок Авито", "обрывы")]
    for start in range(0, len(metrics), LOAD_WINDOW_MIN):
        window = metrics[start:start + LOAD_WINDOW_MIN]
        for name in names:
            minutes = [row["ch"][name] for row in window if name in (row.get("ch") or {})]
            http = sum(minute.get("http", 0) for minute in minutes)
            if not http:
                continue
            rate = http / len(window)
            index = next((i for i, edge in enumerate(RATE_BINS) if rate < edge), len(RATE_BINS))
            target = bins[index]
            target["windows"] += 1
            target["http"] += http
            target["blocks"] += sum(minute.get("403", 0) + minute.get("429", 0) for minute in minutes)
            target["terr"] += sum(minute.get("terr", 0) for minute in minutes)
    if not bins:
        return []
    lines = ["запр/мин на IP (запр/с) · окон по 5 мин · запросов · 403/429 · обрывы"]
    edges = (0, *RATE_BINS)
    for index in sorted(bins):
        low = edges[index]
        label = f"{low}+" if index == len(RATE_BINS) else f"{low}–{RATE_BINS[index]}"
        per_second = f"до {RATE_BINS[index] / 60:.2f}" if index < len(RATE_BINS) else f"от {low / 60:.2f}"
        target = bins[index]
        lines.append(
            f"{label} ({per_second}) · {target['windows']} · {target['http']} · "
            f"{_pct(target['blocks'], target['http'])} · {_pct(target['terr'], target['http'])}"
        )
    return lines


def _time_lines(s: dict) -> list[str]:
    split = _time_split(s)
    if not split:
        return []
    return [
        " · ".join(f"{name} {share:.0%}" for name, share in split),
        f"всего {s['scan_busy_s'] / 60:.0f} воркер-минут за {s['minutes']} мин",
    ]


def _server_lines(metrics: list[dict]) -> list[str]:
    cpu = _values(metrics, "cpu_pct")
    if not cpu:
        return []
    return [
        f"CPU бота (доля одного ядра): p50 {_percentile(cpu, 0.5):g}% · p95 {_percentile(cpu, 0.95):g}% · макс {max(cpu):g}%",
        f"CPU сервера p95 {_percentile(_values(metrics, 'sys_cpu'), 0.95):g}% · steal макс {_worst(metrics, 'steal')}% · "
        f"iowait макс {_worst(metrics, 'iowait')}%",
        f"память бота макс {_worst(metrics, 'rss_mb')} МБ · свободно на сервере мин "
        f"{min(_values(metrics, 'mem_avail_mb') or [0]):g} МБ",
        f"задержка event loop макс {_worst(metrics, 'loop_max_ms')} мс · зависаний {_sum(metrics, 'stalls')}",
    ]


def _load_timeline(metrics: list[dict]) -> list[str]:
    lines = ["время · ссылок · скан/с план/факт · запр/с · воркеры заняты · ждут ср/макс · опозд p95 · ok · CPU"]
    for start in range(0, len(metrics), BUCKET_MINUTES):
        chunk = metrics[start:start + BUCKET_MINUTES]
        loads = [row.get("load") or {} for row in chunk]
        ok, failed = _sum(chunk, "scans", "ok"), _sum(chunk, "scans", "fail")
        http = sum(stats.get("http", 0) for row in chunk for stats in (row.get("ch") or {}).values())
        seconds = 60.0 * len(chunk)
        lines.append(
            f"{chunk[0].get('ts', '')[-5:]} · {_median(chunk, 'urls')} · {_median(loads, 'plan_sps')}/{(ok + failed) / seconds:.2f} · "
            f"{http / seconds:.2f} · {_median(loads, 'busy')}/{_median(loads, 'workers')} · "
            f"{_median(loads, 'queue')}/{_worst(loads, 'queue_max')} · {_hist_text(_hist_merge(loads, 'due_h'), 0.95, 'due_h')} · "
            f"{_pct(ok, ok + failed)} · {_median(chunk, 'cpu_pct')}%"
        )
    return lines


def build_load(minutes: int = 60, live_state: Optional[dict] = None) -> str:
    """The load test on one page: does the bot keep the plan, what holds it back, how many links the channels carry."""
    now = time.time()
    rows = read_rows(METRICS_LOG, now - minutes * 60)
    metrics = [row for row in rows if "load" in row]
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now + 3 * 3600))
    lines = [f"# Avito Radar: нагрузка за {minutes} мин, {stamp} МСК",
             f"Минут с замером нагрузки: {len(metrics)} из {len(rows)}."]
    if not metrics:
        lines += ["", "Нет данных о нагрузке: бот работает без этого обновления или только что запущен."]
        return "\n".join(lines)
    urls = _values(metrics, "urls")
    lines.append(
        f"Ссылок {min(urls or [0]):g}–{max(urls or [0]):g}, радаров {_worst(metrics, 'radars')}, "
        f"каналов в работе сейчас {metrics[-1].get('channels', '—')}"
        + (f", сеть {live_state.get('net_mode')}" if live_state and live_state.get("net_mode") else "") + "."
    )
    lines.append("")
    summary = _load_summary(metrics)
    lines += _section("Вывод", [f"- {line}" for line in _load_conclusions(summary, metrics)])
    lines += _section("План и факт", _plan_lines(summary))
    lines += _section("Куда уходит время сканов", _time_lines(summary))
    lines += _section("Каналы", _load_channel_lines(summary))
    lines += _section("Частота запросов на IP и блоки", _rate_block_lines(metrics, summary))
    lines += _section("Провалы сканов", [_top(_merge(metrics, "fail"), 8)])
    lines += _section("Антибот", _challenges(metrics))
    lines += _section("Сервер", _server_lines(metrics))
    lines += _section(f"По {BUCKET_MINUTES} мин", _load_timeline(metrics))
    return "\n".join(lines)


def select(rows: list[dict], trace: str = "", radar: int = 0, chat: int = 0, text: str = "") -> list[dict]:
    def matches(row: dict) -> bool:
        fields = row.get("f") or {}
        if trace and row.get("id") != trace:
            return False
        if radar and radar not in (fields.get("radar"), fields.get("rid")) and radar not in (fields.get("rids") or ()):
            return False
        if chat and chat != fields.get("chat"):
            return False
        return not text or text.lower() in json.dumps(row, ensure_ascii=False).lower()

    return [row for row in rows if matches(row)]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    args.add_argument("--minutes", type=int, default=60)
    args.add_argument("--ready", type=int, default=0)
    args.add_argument("--load", type=int, default=0, help="minutes of the load report")
    args.add_argument("--trace", default="")
    args.add_argument("--radar", type=int, default=0)
    args.add_argument("--chat", type=int, default=0)
    args.add_argument("--grep", default="")
    args.add_argument("--limit", type=int, default=200)
    args.add_argument("--lot", default="")
    args.add_argument("--live-stdin", action="store_true", help="the bot's current state as JSON on stdin")
    options = args.parse_args()
    live_state = None
    if options.live_stdin:
        try:
            live_state = json.loads(sys.stdin.read() or "null")
        except ValueError:
            live_state = None
    if options.lot:
        print(json.dumps(lot_history(options.lot), ensure_ascii=False))
        return
    if options.ready:
        print(build_readiness(options.ready, live_state))
        return
    if options.load:
        print(build_load(options.load, live_state))
        return
    if not (options.trace or options.radar or options.chat or options.grep):
        print(build_report(options.minutes, live_state))
        return
    rows = read_rows(EVENTS_LOG, time.time() - options.minutes * 60)
    for row in select(rows, options.trace, options.radar, options.chat, options.grep)[-options.limit:]:
        print(_compact(row))


if __name__ == "__main__":
    main()
