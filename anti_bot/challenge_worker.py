"""Anti-bot CPU work in a process of its own: Avito's firewall PoW nonce and the GeeTest puzzle.

Started by anti_bot.challenge_process as `python anti_bot/challenge_worker.py <bot pid> [--warm]`.
In the bot's own process a tight hash loop or a cv2/onnx call holds the GIL and freezes the event
loop (scans, deliveries, button presses). Here it costs one core at a lower priority, and a crash in
a native library kills only this process.

Protocol: one JSON object per line, ASCII only. The bot writes {"id", "task", "args"} into the request
pipe given by `--channel` (an inherited fd, a handle on Windows); the worker answers {"id", "ok": true,
"value"} or {"id", "ok": false, "error": <class name>, "message"} on stdout, after a first {"ready": pid}
line; {"warm": {...}} reports the solver warm-up. Stray prints of the solvers go to stderr, never into
the protocol. Requests do not come through stdin on purpose: on Windows a pending read of a std
handle blocks every DLL that touches it while loading, so numpy's import hung.

The worker never outlives the bot: it exits when the request pipe closes (the bot exited, crashed or
was killed) and, on POSIX, when its parent process changes.
"""

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Started as a script: put the project root first instead of this file's folder.
if sys.path and Path(sys.path[0] or ".").resolve() == Path(__file__).resolve().parent:
    sys.path[0] = str(ROOT)
elif str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

THREAD_LIMITS = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
PARENT_CHECK_SECONDS = 2.0
WARM_WAIT_SECONDS = 120.0
NICE = 5

_warm_thread: threading.Thread | None = None


def _wait_warm() -> None:
    if _warm_thread is not None:
        _warm_thread.join(WARM_WAIT_SECONDS)


def _task_ping():
    return os.getpid()


def _task_pow(challenge_id, complexity, timeout, max_iterations):
    from anti_bot.firewallpow_payload_reference import PowParameters, find_nonce

    return find_nonce(
        PowParameters(str(challenge_id), int(complexity)),
        timeout=float(timeout),
        max_iterations=int(max_iterations),
    )


def _task_geetest(captcha_id, user_agent, profile):
    from anti_bot import geetest_solve

    _wait_warm()
    session = geetest_solve.direct_session(str(user_agent), str(profile))
    try:
        return geetest_solve.request_seccode(session, str(captcha_id))
    finally:
        try:
            session.close()
        except Exception:
            pass


TASKS = {"ping": _task_ping, "pow": _task_pow, "geetest": _task_geetest}


def _watch_parent(parent_pid: int) -> None:
    while True:
        time.sleep(PARENT_CHECK_SECONDS)
        if os.getppid() != parent_pid:
            os._exit(0)


def _open_channel(argv: list[str]):
    """The request pipe from `--channel <fd or handle>`; stdin when started by hand."""
    if "--channel" not in argv:
        return sys.stdin.buffer
    value = int(argv[argv.index("--channel") + 1])
    if os.name == "nt":
        import msvcrt

        value = msvcrt.open_osfhandle(value, os.O_RDONLY)
    return os.fdopen(value, "rb")


def _read_requests(source, requests: queue.Queue) -> None:
    try:
        for line in source:
            requests.put(line)
    except (OSError, ValueError):
        pass
    os._exit(0)  # the bot closed the pipe or is gone: never outlive it


def main(argv: list[str]) -> None:
    global _warm_thread
    parent_pid = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 0
    source = _open_channel(argv)
    for name, value in THREAD_LIMITS.items():
        os.environ.setdefault(name, value)
    if hasattr(os, "nice"):
        try:
            os.nice(NICE)  # captcha work waits; the bot's event loop does not
        except OSError:
            pass

    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="ascii", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    out_lock = threading.Lock()

    def send(message: dict) -> None:
        line = json.dumps(message, default=str)
        with out_lock:
            out.write(line + "\n")
            out.flush()

    requests: queue.Queue = queue.Queue()
    threading.Thread(target=_read_requests, args=(source, requests), daemon=True, name="requests").start()
    if parent_pid and os.name == "posix":
        if os.getppid() != parent_pid:
            os._exit(0)
        threading.Thread(target=_watch_parent, args=(parent_pid,), daemon=True, name="parent").start()

    if "--warm" in argv:
        def warm() -> None:
            from anti_bot import geetest_solve

            send({"warm": geetest_solve.warm_up()})

        _warm_thread = threading.Thread(target=warm, daemon=True, name="warm")
        _warm_thread.start()

    send({"ready": os.getpid()})
    while True:
        raw = requests.get()
        try:
            request = json.loads(raw)
            ident = request.get("id")
        except (ValueError, AttributeError) as exc:
            send({"id": None, "ok": False, "error": "ProtocolError", "message": str(exc)[:200]})
            continue
        task = TASKS.get(request.get("task"))
        if task is None:
            send({"id": ident, "ok": False, "error": "ProtocolError", "message": f"unknown task {request.get('task')!r}"})
            continue
        try:
            value = task(*(request.get("args") or ()))
        except Exception as exc:
            send({"id": ident, "ok": False, "error": type(exc).__name__, "message": str(exc)[:500]})
        else:
            send({"id": ident, "ok": True, "value": value})


if __name__ == "__main__":
    main(sys.argv)
