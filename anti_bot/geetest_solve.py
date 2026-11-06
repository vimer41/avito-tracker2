"""GeeTest v4 up to GeeTest's own answer (seccode): the load request and the solver.

No Avito traffic and no logging setup here: the challenge worker process imports this module, so its
imports stay light. The heavy solvers (cv2, numpy, onnx via ddddocr) are imported by GeekedTest only
for the captcha types that need them.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import time
from pathlib import Path
from threading import Event
from typing import Any, Optional

from anti_bot.pow_budget import check_cancelled

GEETEST_LOAD_URL = "https://gcaptcha4.geevisit.com/load"
GEETEST_BASE_URL = "https://gcaptcha4.geevisit.com"
GEETEST_CAPTCHA_ID = "2d9c743cf7d63dbc9db578a608196bcd"
AVITO_REFERER = "https://www.avito.ru/"
GEEKED_TEST_ROOT = Path(__file__).resolve().parent / "GeekedTest"

_CAPTCHA_ID_RE = re.compile(r"const\s+captchaId\s*=\s*['\"]([^'\"]+)['\"]")


class GeetestUnavailable(RuntimeError):
    """The GeeTest solver or one of its libraries is not installed."""


def ensure_geeked_path() -> None:
    root = str(GEEKED_TEST_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def captcha_id_from_page(text: str) -> str:
    match = _CAPTCHA_ID_RE.search(text or "")
    return match.group(1) if match else GEETEST_CAPTCHA_ID


def direct_session(user_agent: str, profile: str):
    """A session of its own for GeeTest: its servers do not care which IP solves the puzzle."""
    from curl_cffi import requests

    return requests.Session(
        impersonate=profile,
        headers={"User-Agent": user_agent, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
        timeout=(5, 10),
    )


def request_seccode(session: Any, captcha_id: str, *, cancel_event: Event | None = None) -> Optional[dict]:
    """GeeTest's seccode for one puzzle, or None when GeeTest refused the load.

    Raises GeetestUnavailable without the solver libraries, CaptchaSolveRejected when GeeTest rejects
    the answer, PowSearchError when cancelled or out of budget.
    """
    ensure_geeked_path()
    try:
        from geeked import Geeked
    except ImportError as exc:
        raise GeetestUnavailable(str(exc)) from exc

    check_cancelled(cancel_event)
    challenge = str(secrets.token_hex(16))
    callback = f"geetest_{int(time.time() * 1000) + secrets.randbelow(10000)}"
    load_resp = session.get(
        GEETEST_LOAD_URL,
        params={
            "callback": callback,
            "captcha_id": captcha_id,
            "challenge": challenge,
            "client_type": "web",
            "lang": "rus",
        },
        headers={"Referer": AVITO_REFERER},
        timeout=(5, 10),
        allow_redirects=False,
    )
    check_cancelled(cancel_event)
    if load_resp.status_code != 200:
        return None

    match = re.fullmatch(rf"\s*{re.escape(callback)}\((.*)\)\s*;?\s*", load_resp.text, re.DOTALL)
    if not match:
        raise ValueError("GeeTest load: некорректный JSONP ответ")
    load_data = json.loads(match.group(1))
    if load_data.get("status") != "success":
        return None

    task_data = load_data.get("data", {})
    solver = Geeked(
        captcha_id,
        lang="rus",
        session=session,
        request_headers={"Referer": AVITO_REFERER},
        cancel_event=cancel_event,
    )
    solver.lot_number = task_data.get("lot_number")
    solver.base_url = GEETEST_BASE_URL
    seccode = solver.submit_captcha(task_data)
    if not isinstance(seccode, dict) or "lot_number" not in seccode:
        return None
    return seccode


def warm_up() -> dict:
    """Imports the solvers and loads the icon models so the first captcha does not wait for them."""
    started = time.monotonic()
    ensure_geeked_path()
    loaded = []
    try:
        import geeked.geeked  # noqa: F401  (curl_cffi, Crypto)
        loaded.append("geeked")
        import cv2
        cv2.setNumThreads(1)  # the bot keeps the other core
        import geeked.slide  # noqa: F401
        import geeked.icon  # noqa: F401
        loaded.append("cv2")
        from geeked.dddd_server import get_dddd_service
        get_dddd_service()
        loaded.append("onnx")
    except Exception as exc:  # a missing library only disables that captcha type
        return {"loaded": loaded, "missing": f"{type(exc).__name__}: {exc}"[:300], "s": round(time.monotonic() - started, 2)}
    return {"loaded": loaded, "s": round(time.monotonic() - started, 2)}
