"""Runs anti-bot CPU work (firewall PoW, GeeTest) in worker processes; the worker is challenge_worker.py.

Why: in the bot's process the PoW hash loop and the cv2/onnx captcha solvers hold the GIL, so the event
loop froze for seconds (and for minutes on the first cv2 import after a restart).

What October's attempt taught (timed-out searches kept running in orphaned processes and ate the only
CPU), and what is guaranteed here:
- at most CHALLENGE_PROCESSES workers exist: a worker that overran its time, or whose scan was
  cancelled, is killed and reaped before another one starts;
- the worker is a plain child process that imports only its own small modules (no copy of the bot);
- it exits by itself when the bot's pipe closes, so a crashed or killed bot leaves nothing behind;
- it runs at a lower priority, so captcha work never starves the event loop.

When a worker cannot start, callers get ChallengeProcessUnavailable and solve in their own thread, as
before the worker existed.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from threading import Event
from typing import Any, Optional

from anti_bot.geetest_solve import GeetestUnavailable
from anti_bot.pow_budget import PowSearchCancelled, PowSearchLimit, PowSearchTimeout, check_cancelled
from logger_setup import log_event

logger = logging.getLogger(__name__)

_TESTING = os.getenv("AVITO_RADAR_TESTING") == "1"
ENABLED = os.getenv("CHALLENGE_IN_PROCESS", "0" if _TESTING else "1").strip() != "0"
PROCESSES = max(1, min(4, int(os.getenv("CHALLENGE_PROCESSES", "1") or 1)))
WARM = os.getenv("CHALLENGE_WARM", "1").strip() != "0"
START_TIMEOUT = 15.0
RETRY_START_AFTER = 60.0
WORKER_PATH = Path(__file__).resolve().parent / "challenge_worker.py"
ROOT = WORKER_PATH.parents[1]
THREAD_LIMITS = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


class ChallengeProcessUnavailable(RuntimeError):
    """No worker could be started; the caller solves the challenge in its own thread."""


class ChallengeTaskError(RuntimeError):
    """The task failed inside the worker; the text names the worker's exception."""


class ChallengeTimeout(ChallengeTaskError):
    pass


class ChallengeWorkerDied(ChallengeTaskError):
    pass


_KNOWN_ERRORS = {
    cls.__name__: cls for cls in (PowSearchTimeout, PowSearchLimit, PowSearchCancelled, GeetestUnavailable)
}

STATS: Counter = Counter()
_lock = threading.Lock()
_idle: Optional[queue.Queue] = None
_workers: list = []
_down_until = 0.0


def enabled() -> bool:
    return ENABLED


def _kill(proc: subprocess.Popen, channel=None) -> Optional[int]:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        code = None
    if channel is not None:
        try:
            channel.close()
        except OSError:
            pass
    return code


def _spawn(command: list[str], environment: dict) -> tuple[subprocess.Popen, Any]:
    """Starts the worker with an inherited request pipe; returns it with the pipe's writing end."""
    read_fd, write_fd = os.pipe()
    try:
        if os.name == "nt":
            import msvcrt

            handle = msvcrt.get_osfhandle(read_fd)
            os.set_handle_inheritable(handle, True)
            inherit = {"startupinfo": subprocess.STARTUPINFO(lpAttributeList={"handle_list": [handle]})}
            channel_id = handle
        else:
            inherit = {"pass_fds": (read_fd,)}
            channel_id = read_fd
        proc = subprocess.Popen(
            [*command, "--channel", str(channel_id)], cwd=str(ROOT), env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, close_fds=True, **inherit,
        )
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)  # the worker holds its own copy; EOF reaches it once the bot's end closes
    return proc, os.fdopen(write_fd, "wb")


def _read_replies(proc: subprocess.Popen, replies: queue.Queue, index: int) -> None:
    """Moves the worker's answers into a queue, so waiting for one never holds the GIL."""
    try:
        with proc.stdout:
            for raw in proc.stdout:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(message, dict) and "warm" in message:
                    _log_warm(message["warm"], proc.pid, index)
                    continue
                replies.put(message)
    except (OSError, ValueError):
        pass
    finally:
        replies.put(None)  # the worker is gone


def _log_warm(result: Any, pid: int, index: int) -> None:
    result = result if isinstance(result, dict) else {}
    if result.get("missing"):
        log_event(logger, logging.WARNING, "antibot.worker",
                  f"Процесс антибота: решатель GeeTest загружен не полностью ({result['missing']})",
                  pid=pid, worker=index, loaded=result.get("loaded"), s=result.get("s"))
    else:
        log_event(logger, logging.INFO, "antibot.worker",
                  f"Процесс антибота: решатель GeeTest загружен за {result.get('s')} с",
                  pid=pid, worker=index, loaded=result.get("loaded"), s=result.get("s"))


class _Worker:
    def __init__(self, index: int):
        self.index = index
        self.proc: Optional[subprocess.Popen] = None
        self.channel = None
        self.replies: Optional[queue.Queue] = None
        self.seq = 0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            STATS["died"] += 1
            log_event(logger, logging.WARNING, "antibot.worker",
                      f"Процесс антибота завершился сам (код {self.proc.returncode}), перезапуск",
                      pid=self.proc.pid, worker=self.index, code=self.proc.returncode)
        self.kill("restart")
        environment = dict(os.environ)
        for name, value in THREAD_LIMITS.items():
            environment.setdefault(name, value)
        command = [sys.executable, str(WORKER_PATH), str(os.getpid())] + (["--warm"] if WARM else [])
        try:
            proc, channel = _spawn(command, environment)
        except (OSError, ValueError) as exc:
            STATS["start_failed"] += 1
            raise ChallengeProcessUnavailable(f"worker did not start: {exc}") from exc
        replies: queue.Queue = queue.Queue()
        threading.Thread(
            target=_read_replies, args=(proc, replies, self.index), daemon=True, name=f"challenge-reader-{self.index}",
        ).start()
        try:
            first = replies.get(timeout=START_TIMEOUT)
        except queue.Empty:
            first = None
        if not (isinstance(first, dict) and "ready" in first):
            code = _kill(proc, channel)
            STATS["start_failed"] += 1
            raise ChallengeProcessUnavailable(f"worker did not start (exit code {code})")
        self.proc, self.channel, self.replies = proc, channel, replies
        STATS["started"] += 1
        log_event(logger, logging.INFO, "antibot.worker", f"Процесс антибота запущен, pid {proc.pid}",
                  pid=proc.pid, worker=self.index)

    def _release(self) -> tuple[Optional[subprocess.Popen], Any]:
        proc, channel = self.proc, self.channel
        self.proc, self.channel, self.replies = None, None, None
        return proc, channel

    def kill(self, reason: str) -> None:
        proc, channel = self._release()
        if proc is None:
            return
        running = proc.poll() is None
        code = _kill(proc, channel)
        if running and reason != "restart":
            STATS[f"killed_{reason}"] += 1
            log_event(logger, logging.INFO, "antibot.worker", f"Процесс антибота остановлен ({reason}), pid {proc.pid}",
                      pid=proc.pid, worker=self.index, reason=reason, code=code)

    def call(self, task: str, args: list, deadline: float, cancel_event: Event | None) -> dict:
        if not self.alive():
            self.start()
        assert self.proc is not None and self.channel is not None and self.replies is not None
        self.seq += 1
        ident = self.seq
        try:
            self.channel.write((json.dumps({"id": ident, "task": task, "args": args}) + "\n").encode("ascii"))
            self.channel.flush()
        except (OSError, ValueError) as exc:
            self._died(f"pipe closed: {exc}")
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self.kill("cancel")
                raise PowSearchCancelled("operation cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.kill("timeout")
                raise ChallengeTimeout(f"{task}: no answer in time")
            try:
                reply = self.replies.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue
            if reply is None:
                self._died("no answer")
            if isinstance(reply, dict) and reply.get("id") == ident:
                return reply

    def _died(self, detail: str) -> None:
        proc, channel = self._release()
        code = _kill(proc, channel) if proc is not None else None
        STATS["died"] += 1
        log_event(logger, logging.WARNING, "antibot.worker", f"Процесс антибота упал (код {code}), будет перезапущен",
                  pid=getattr(proc, "pid", None), worker=self.index, code=code, detail=detail)
        raise ChallengeWorkerDied(f"worker exited with code {code}")


def _idle_workers() -> queue.Queue:
    global _idle
    with _lock:
        if _idle is None:
            _idle = queue.Queue()
            for index in range(1, PROCESSES + 1):
                worker = _Worker(index)
                _workers.append(worker)
                _idle.put(worker)
        return _idle


def _take(idle: queue.Queue, deadline: float, cancel_event: Event | None, task: str) -> _Worker:
    while True:
        check_cancelled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChallengeTimeout(f"{task}: every worker is busy")
        try:
            return idle.get(timeout=min(0.2, remaining))
        except queue.Empty:
            continue


def _error_from(reply: dict) -> Exception:
    name = str(reply.get("error") or "Error")
    message = str(reply.get("message") or "")
    known = _KNOWN_ERRORS.get(name)
    return known(message) if known else ChallengeTaskError(f"{name}: {message}")


def run(task: str, *args: Any, timeout: float, cancel_event: Event | None = None) -> Any:
    """Runs one task in a worker and returns its value; blocks the calling thread, not the GIL.

    Raises the worker's PowSearch*/GeetestUnavailable as they are, ChallengeTimeout when there is no
    answer within `timeout` (the worker is killed), ChallengeWorkerDied when it crashed, and
    ChallengeProcessUnavailable when no worker can start (solve in-thread then).
    """
    global _down_until
    if time.monotonic() < _down_until:
        raise ChallengeProcessUnavailable("worker start failed recently")
    deadline = time.monotonic() + max(0.1, float(timeout))
    idle = _idle_workers()
    worker = _take(idle, deadline, cancel_event, task)
    try:
        reply = worker.call(task, list(args), deadline, cancel_event)
    except ChallengeProcessUnavailable as exc:
        _down_until = time.monotonic() + RETRY_START_AFTER
        log_event(logger, logging.WARNING, "antibot.worker",
                  f"Процесс антибота не запустился, антибот в потоке бота {RETRY_START_AFTER:.0f} с: {exc}",
                  worker=worker.index)
        raise
    finally:
        idle.put(worker)
    if reply.get("ok"):
        return reply.get("value")
    raise _error_from(reply)


def _prestart() -> None:
    idle = _idle_workers()
    taken = []
    try:
        for _ in range(PROCESSES):
            worker = idle.get()
            taken.append(worker)
            if not worker.alive():
                worker.start()
    except ChallengeProcessUnavailable as exc:
        log_event(logger, logging.WARNING, "antibot.worker", f"Процесс антибота не запустился: {exc}")
    finally:
        for worker in taken:
            idle.put(worker)


def start_background() -> None:
    """Starts the workers off the event loop, so the first captcha finds the solver loaded."""
    if ENABLED:
        threading.Thread(target=_prestart, daemon=True, name="challenge-start").start()


def shutdown() -> None:
    """Kills idle workers; a busy one is killed by its caller's cancellation or exits with the bot."""
    with _lock:
        idle = _idle
    if idle is None:
        return
    taken = []
    while True:
        try:
            taken.append(idle.get_nowait())
        except queue.Empty:
            break
    for worker in taken:
        worker.kill("stop")
        idle.put(worker)


def pids() -> list[int]:
    """Process ids of the running workers, busy ones included."""
    with _lock:
        workers = list(_workers)
    return [proc.pid for proc in (worker.proc for worker in workers) if proc is not None and proc.poll() is None]
