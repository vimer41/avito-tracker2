"""
Logging system. All files are written by one background thread, so a log call never touches
the disk from the event loop.

  bot.log               human timeline: what the bot did, one line per event (MSK time)
  logs/errors.log       WARNING and above: only what needs attention, with tracebacks
  logs/user_actions.log user clicks and messages
  logs/events.jsonl     machine-readable copy of everything, rotated to .gz
  logs/metrics.jsonl    one row per minute; survives restarts
  logs/crash.log        native crashes (segfault in onnx/cv2)

Every file except crash.log rotates by size into .gz copies that are kept LOG_KEEP_DAYS (30) days,
within a total size cap per file (LOG_LIMITS_MB).

Levels: ERROR = bug, WARNING = needs attention, INFO = what happened, DEBUG = routine detail
(goes to events.jsonl only while debug mode is on). Avito's 403 is routine, not a warning.
Every scan gets a 4-char trace id that follows it through parser, anti-bot, fanout and delivery.
"""

import atexit
import contextvars
import faulthandler
import gzip
import logging
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import time
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Optional

import orjson

from runtime_metrics import sanitize_proxy_credentials

# Базовая директория логов
LOGS_DIR = Path(os.getenv("LOGS_DIR", str(Path(__file__).resolve().parent / "logs")))

USER_ACTIONS_LOG = LOGS_DIR / "user_actions.log"
ERRORS_LOG = LOGS_DIR / "errors.log"
EVENTS_LOG = LOGS_DIR / "events.jsonl"
METRICS_LOG = LOGS_DIR / "metrics.jsonl"
CRASH_LOG = LOGS_DIR / "crash.log"
COMBINED_LOG = Path(os.getenv("COMBINED_LOG", str(Path(__file__).resolve().parent / "bot.log")))

# Логгер для действий пользователей
user_logger = logging.getLogger("user_action")
metrics_logger = logging.getLogger("metrics.minute")

MSK_OFFSET_SECONDS = 3 * 3600
QUEUE_SIZE = 20000
# History: files rotate by size and are kept by age and total size, not by a fixed count.
LOG_KEEP_DAYS = max(1, int(os.getenv("LOG_KEEP_DAYS", "30") or 30))
MAX_ROTATIONS = 999
EVENTS_MAX_MB = 16
# (live file MB, MB of rotated .gz files kept): a safety cap far above a month of normal traffic.
LOG_LIMITS_MB = {
    "bot": (8, 400),
    "errors": (4, 200),
    "user_actions": (8, 300),
    "events": (EVENTS_MAX_MB, 3000),
    "metrics": (8, 600),
}
PROJECT_LOGGERS = ("monitoring", "scheduler", "parser", "collector", "proxy_pool", "anti_bot", "reports", "handlers")

_LEVEL_TAGS = {"DEBUG": "DBG", "INFO": "   ", "WARNING": "WRN", "ERROR": "ERR", "CRITICAL": "CRT"}
_LEVEL_LETTERS = {"DEBUG": "D", "INFO": "I", "WARNING": "W", "ERROR": "E", "CRITICAL": "C"}
_COMPONENTS = (
    ("monitoring", "scan"), ("collector", "scan"), ("parser", "net"), ("proxy_pool", "proxy"),
    ("anti_bot", "antibot"), ("scheduler", "send"), ("reports", "report"), ("handlers", "ui"),
    ("middlewares", "ui"), ("database", "db"), ("aiogram", "tg"), ("bot", "core"), ("trial_gate", "ui"),
)
_TOKEN_RE = re.compile(r"\b(?:bot)?\d{8,12}:[\w-]{30,}\b")

_trace: contextvars.ContextVar[str] = contextvars.ContextVar("log_trace", default="")
_listener: Optional[QueueListener] = None
_crash_file = None
_debug_timer: Optional[threading.Timer] = None
_debug_until = 0.0
dropped_records = 0


def new_trace() -> str:
    """Starts a new trace in the current task and returns its id."""
    trace_id = secrets.token_hex(2)
    _trace.set(trace_id)
    return trace_id


def set_trace(trace_id: str = "") -> None:
    _trace.set(trace_id or "")


def get_trace() -> str:
    return _trace.get()


def log_event(logger: logging.Logger, level: int, event: str, message: str, **fields) -> None:
    """A human line for bot.log plus typed fields for events.jsonl. Secrets are masked on write."""
    if logger.isEnabledFor(level):
        logger.log(level, message, extra={"ev": event, "fields": fields})


_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """A key that must never reach a log file (Platega's API key and the like)."""
    if value and len(value) >= 8:
        _SECRETS.add(value)


def _redact(text: str) -> str:
    text = _TOKEN_RE.sub("<token>", sanitize_proxy_credentials(text))
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "<secret>")
    return text


def _msk_stamp(created: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.gmtime(created + MSK_OFFSET_SECONDS))


def _component(record: logging.LogRecord) -> str:
    event = getattr(record, "ev", "")
    if event:
        return event.split(".", 1)[0]
    for prefix, component in _COMPONENTS:
        if record.name.startswith(prefix):
            return component
    return record.name.split(".", 1)[0][:7]


def _exception_text(record: logging.LogRecord) -> str:
    if record.exc_info and not record.exc_text:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    return record.exc_text or ""


class HumanFormatter(logging.Formatter):
    """`10-30 17:08:12 WRN scan    a1f3 message`: columns stay aligned so a tail reads as a table."""

    def format(self, record: logging.LogRecord) -> str:
        trace = getattr(record, "trace", "") or "····"
        line = (
            f"{_msk_stamp(record.created)} {_LEVEL_TAGS.get(record.levelname, record.levelname[:3])} "
            f"{_component(record):<7} {trace} {record.getMessage()}"
        )
        exception = _exception_text(record)
        if exception:
            line += "\n" + exception
        return _redact(line)


class JsonFormatter(logging.Formatter):
    """One JSON object per line; `t` comes first so readers can skip old lines without parsing them."""

    def format(self, record: logging.LogRecord) -> str:
        row = {
            "t": round(record.created, 2),
            "ts": _msk_stamp(record.created),
            "l": _LEVEL_LETTERS.get(record.levelname, "I"),
            "c": _component(record),
        }
        trace = getattr(record, "trace", "")
        if trace:
            row["id"] = trace
        event = getattr(record, "ev", "")
        fields = getattr(record, "fields", None)
        if event:
            row["ev"] = event
        if fields:
            row["f"] = fields
        # A typed event carries its data in the fields; the text is repeated only where it gets read.
        if not fields or record.levelno >= logging.WARNING:
            row["msg"] = record.getMessage()[:500]
        exception = _exception_text(record)
        if exception:
            row["exc"] = exception[-4000:]
        return _redact(orjson.dumps(row, default=str).decode("utf-8"))


class MetricsFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact(orjson.dumps(getattr(record, "fields", {}), default=str).decode("utf-8"))


class _NameFilter(logging.Filter):
    def __init__(self, *, only: tuple[str, ...] = (), exclude: tuple[str, ...] = ()):
        super().__init__()
        self.only, self.exclude = only, exclude

    def filter(self, record: logging.LogRecord) -> bool:
        if self.only and record.name not in self.only:
            return False
        return record.name not in self.exclude


class _ErrorsFilter(logging.Filter):
    """A failed user action is the bot's error; a user-level warning is not."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == metrics_logger.name:
            return False
        return record.name != user_logger.name or record.levelno >= logging.ERROR


class _Handoff(QueueHandler):
    """Runs on the caller's thread: takes the trace id and leaves everything else to the writer thread."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        record.trace = _trace.get()
        return record

    def enqueue(self, record: logging.LogRecord) -> None:
        global dropped_records
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            dropped_records += 1  # a log record is never worth blocking the event loop


def _gzip_rotator(source: str, dest: str) -> None:
    with open(source, "rb") as raw, gzip.open(dest, "wb", compresslevel=6) as packed:
        shutil.copyfileobj(raw, packed)
    os.remove(source)


def prune_rotations(path: Path, keep_days: float, keep_bytes: int, now: Optional[float] = None) -> int:
    """Deletes rotated copies of `path` older than keep_days, then the oldest beyond keep_bytes."""
    now = time.time() if now is None else now
    found = []
    for rotated in path.parent.glob(f"{path.name}.*"):
        try:
            info = rotated.stat()
        except OSError:
            continue
        if rotated.is_file():
            found.append((info.st_mtime, info.st_size, rotated))
    found.sort(key=lambda entry: entry[0], reverse=True)
    cutoff = now - keep_days * 86400
    total = removed = 0
    for mtime, size, rotated in found:
        total += size
        if mtime < cutoff or total > keep_bytes:
            try:
                rotated.unlink()
                removed += 1
            except OSError:
                pass
    return removed


class _KeepingRotatingFileHandler(RotatingFileHandler):
    """Size rotation into .gz; old files go by age (LOG_KEEP_DAYS) and total size."""

    def __init__(self, path: Path, max_mb: float, keep_mb: float):
        super().__init__(path, maxBytes=int(max_mb * 1024 * 1024), backupCount=MAX_ROTATIONS, encoding="utf-8")
        self.keep_bytes = int(keep_mb * 1024 * 1024)
        self.namer = lambda name: f"{name}.gz"
        self.rotator = _gzip_rotator

    def doRollover(self) -> None:
        super().doRollover()
        prune_rotations(Path(self.baseFilename), LOG_KEEP_DAYS, self.keep_bytes)


def _rotating(path: Path, kind: str, max_mb: Optional[float] = None) -> RotatingFileHandler:
    live_mb, keep_mb = LOG_LIMITS_MB[kind]
    prune_rotations(path, LOG_KEEP_DAYS, int(keep_mb * 1024 * 1024))  # old-scheme files (.1, .2) age out too
    return _KeepingRotatingFileHandler(path, live_mb if max_mb is None else max_mb, keep_mb)


def _stdout_is_combined_log() -> bool:
    # systemd StandardOutput=append:.../bot.log
    try:
        if COMBINED_LOG.exists() and hasattr(sys.stdout, "fileno"):
            stdout_stat = os.fstat(sys.stdout.fileno())
            log_stat = COMBINED_LOG.stat()
            return stdout_stat.st_ino == log_stat.st_ino and stdout_stat.st_dev == log_stat.st_dev
    except Exception:
        pass
    return False


def stop_logging() -> None:
    """Writes out the queue and closes the files."""
    global _listener
    listener, _listener = _listener, None
    if listener is None:
        return
    listener.stop()
    for handler in listener.handlers:
        handler.close()


atexit.register(stop_logging)


def setup_logging():
    """Starts the writer thread and its files; safe to call again."""
    global _crash_file, _listener
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    COMBINED_LOG.parent.mkdir(parents=True, exist_ok=True)
    # parser.log was merged into bot.log and events.jsonl.
    for stale in LOGS_DIR.glob("parser.log*"):
        stale.unlink(missing_ok=True)

    stop_logging()
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()

    human = HumanFormatter()
    timeline = _NameFilter(exclude=(user_logger.name, metrics_logger.name))
    handlers: list[logging.Handler] = []

    def add(handler: logging.Handler, formatter: logging.Formatter, level: int, name_filter: logging.Filter) -> None:
        handler.setFormatter(formatter)
        handler.setLevel(level)
        handler.addFilter(name_filter)
        handlers.append(handler)

    if not _stdout_is_combined_log():
        add(logging.StreamHandler(sys.stdout), human, logging.INFO, timeline)
    add(_rotating(COMBINED_LOG, "bot"), human, logging.INFO, timeline)
    add(_rotating(ERRORS_LOG, "errors"), human, logging.WARNING, _ErrorsFilter())
    add(
        _rotating(USER_ACTIONS_LOG, "user_actions"),
        logging.Formatter(fmt="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"),
        logging.INFO,
        _NameFilter(only=(user_logger.name,)),
    )
    add(_rotating(EVENTS_LOG, "events", EVENTS_MAX_MB), JsonFormatter(), logging.DEBUG,
        _NameFilter(exclude=(metrics_logger.name,)))
    add(_rotating(METRICS_LOG, "metrics"), MetricsFormatter(), logging.INFO, _NameFilter(only=(metrics_logger.name,)))

    record_queue: queue.Queue = queue.Queue(QUEUE_SIZE)
    root_logger.addHandler(_Handoff(record_queue))
    _listener = QueueListener(record_queue, *handlers, respect_handler_level=True)
    _listener.start()

    # Подавляем шумные технические логи aiogram
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiogram.dispatcher").setLevel(logging.WARNING)
    if not any(isinstance(f, _PollingNoise) for f in logging.getLogger("aiogram.dispatcher").filters):
        logging.getLogger("aiogram.dispatcher").addFilter(_PollingNoise())
    user_logger.setLevel(logging.INFO)
    metrics_logger.setLevel(logging.INFO)

    # A segfault in a native library leaves no Python traceback without this.
    if _crash_file is None:
        _trim_crash_log()
        _crash_file = open(CRASH_LOG, "a", encoding="utf-8")
        faulthandler.enable(file=_crash_file)


class _PollingNoise(logging.Filter):
    """A Telegram polling timeout that aiogram retries by itself is routine: INFO, not an error to read."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = str(record.msg)
        if message.startswith("Failed to fetch updates") or message.startswith("Sleep for"):
            record.levelno, record.levelname = logging.INFO, "INFO"
        return True


CRASH_LOG_MAX_BYTES = 2 * 1024 * 1024
CRASH_LOG_KEEP_BYTES = 256 * 1024
STALL_MARK_RE = re.compile(r"^=== зависание .* · epoch (\d+) ===$")


def _trim_crash_log() -> None:
    try:
        if CRASH_LOG.is_file() and CRASH_LOG.stat().st_size > CRASH_LOG_MAX_BYTES:
            data = CRASH_LOG.read_bytes()[-CRASH_LOG_KEEP_BYTES:]
            CRASH_LOG.write_bytes(data[data.find(b"\n") + 1:])
    except OSError:
        pass


def mark_stall_dump(lag_seconds: float) -> None:
    """The stack dump faulthandler just wrote has no time: this line after it dates it for the report."""
    if _crash_file is None:
        return
    now = time.time()
    stamp = time.strftime("%m-%d %H:%M:%S", time.gmtime(now + MSK_OFFSET_SECONDS))
    try:
        _crash_file.write(f"=== зависание {stamp} МСК · {lag_seconds:.1f} с · epoch {int(now)} ===\n")
        _crash_file.flush()
    except (OSError, ValueError):
        pass


def write_metrics_row(row: dict) -> None:
    metrics_logger.info("", extra={"fields": row})


def arm_stall_dump(seconds: float) -> None:
    """All thread stacks go to crash.log unless re-armed in time. faulthandler's C thread works while the GIL is held."""
    if _crash_file is not None:
        faulthandler.dump_traceback_later(seconds, file=_crash_file)


def disarm_stall_dump() -> None:
    faulthandler.cancel_dump_traceback_later()


def debug_mode_remaining() -> float:
    return max(0.0, _debug_until - time.time())


def set_debug_mode(minutes: float) -> None:
    """Detailed events for a while. Project loggers only: aiosqlite at DEBUG would flood the disk."""
    global _debug_timer, _debug_until
    if _debug_timer is not None:
        _debug_timer.cancel()
        _debug_timer = None
    enabled = minutes > 0
    _debug_until = time.time() + minutes * 60 if enabled else 0.0
    for name in PROJECT_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if enabled else logging.NOTSET)
    if enabled:
        _debug_timer = threading.Timer(minutes * 60, set_debug_mode, args=(0,))
        _debug_timer.daemon = True
        _debug_timer.start()


def log_user_event(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    action: str,
    details: str = "",
    status: str = "OK",
    fsm_state: Optional[str] = None
):
    """
    Записывает структурированное событие пользователя в logs/user_actions.log.
    Удобно фильтровать по user_id при обращении в поддержку.
    """
    uname = f"@{username}" if username else "no_username"
    fname = first_name or ""
    fsm_info = f" | [FSM: {fsm_state}]" if fsm_state else ""
    det_info = f" ── {details}" if details else ""

    # Символы статусов для быстрой визуальной навигации
    icons = {
        "OK": "✅",
        "WARN": "⚠️",
        "ERROR": "❌",
        "CLICK": "🔘",
        "MSG": "💬",
        "INFO": "ℹ️"
    }
    icon = icons.get(status, "🔹")

    log_line = (
        f"[{icon} {status}] [ID:{user_id} {uname} ({fname})]{fsm_info} "
        f"{action}{det_info}"
    )

    if status in ("ERROR", "FAIL"):
        user_logger.error(log_line)
    elif status == "WARN":
        user_logger.warning(log_line)
    else:
        user_logger.info(log_line)


def _scrub_log_file(path: Path, needle: str) -> None:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="ignore") as source:
        lines = source.readlines()
    kept = [line for line in lines if needle not in line.lower()]
    if len(kept) != len(lines):
        with opener(path, "wt", encoding="utf-8") as target:
            target.writelines(kept)


def sanitize_logs_for_user(pattern: str):
    """
    Удаляет строки с упоминанием пользователя из всех файлов логов, включая ротированные.
    """
    clean = pattern.strip().lstrip("@").lower()
    if not clean or len(clean) < 3:
        return
    log_files = [*LOGS_DIR.glob("*.log*"), *LOGS_DIR.glob("*.jsonl*"), *COMBINED_LOG.parent.glob(f"{COMBINED_LOG.name}*")]
    for lf in log_files:
        if lf.is_file():
            try:
                _scrub_log_file(lf, clean)
            except Exception as e:
                logging.getLogger(__name__).warning(f"Не удалось очистить лог {lf}: {e}")


def cleanup_logs(max_total_mb: Optional[int] = None, keep_days: float = LOG_KEEP_DAYS) -> int:
    """
    Автоматическая очистка старых ротированных логов.
    По умолчанию применяет единую политику хранения LOG_KEEP_DAYS (30 дней) и лимиты LOG_LIMITS_MB
    через prune_rotations для каждого лог-файла. Если передан max_total_mb — использует старый общий лимит.
    """
    if max_total_mb is None:
        removed_count = 0
        managed = (
            (COMBINED_LOG, "bot"),
            (ERRORS_LOG, "errors"),
            (USER_ACTIONS_LOG, "user_actions"),
            (EVENTS_LOG, "events"),
            (METRICS_LOG, "metrics"),
        )
        for path, kind in managed:
            _, keep_mb = LOG_LIMITS_MB[kind]
            removed_count += prune_rotations(path, keep_days, int(keep_mb * 1024 * 1024))
        return removed_count

    removed_count = 0
    now = time.time()
    cutoff = now - (keep_days * 86400)

    target_dirs = {LOGS_DIR, COMBINED_LOG.parent}
    log_files: list[Path] = []

    for d in target_dirs:
        if not d.is_dir():
            continue
        for p in d.glob("*.log.*"):
            if p.is_file():
                log_files.append(p)

    # 1. Удаление ротированных архивов логов старше keep_days
    for p in list(log_files):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                log_files.remove(p)
                removed_count += 1
        except Exception:
            pass

    # 2. Если общий объем оставшихся ротаций превышает max_total_mb — удаляем самые старые
    try:
        total_bytes = sum(p.stat().st_size for p in log_files if p.is_file())
        max_bytes = max_total_mb * 1024 * 1024
        if total_bytes > max_bytes:
            log_files.sort(key=lambda f: f.stat().st_mtime)
            for p in log_files:
                if total_bytes <= max_bytes:
                    break
                try:
                    sz = p.stat().st_size
                    p.unlink(missing_ok=True)
                    total_bytes -= sz
                    removed_count += 1
                except Exception:
                    pass
    except Exception:
        pass

    return removed_count
