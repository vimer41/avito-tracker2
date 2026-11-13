#!/usr/bin/env python3
"""
Менеджер обхода защит Авито (Anti-Bot Manager):
1. QRATOR Challenge (HTTP 302/429):
   - Загрузка fingerprint-скрипта (/[0-9a-f]{20}.js) и favicon.ico
   - Генерация XTEA-зашифрованных полезных нагрузок 'f' и 's'
   - Отправка multipart POST на /web/2/ft и установка кук 'f' и 'ft'
   - Запрос отслеживающего пикселя /web/1/u?{pixel_id}
2. Firewall PoW Challenge (HTTP 439 / 429 / Cookies):
   - Извлечение челленджа из тела ответа JSON (response.json()['pow_challenge']) или кук
   - Запрос challenge_jwt на /web/3/firewallPow/get
   - Локальный расчет SHA-256 PoW nonce заданной сложности
   - Отправка решения на /web/3/firewallPow/verify и получение unblock_ttl
3. GeeTest v4 Captcha (HTTP 429 / 403 show captcha):
   - Определение типа капчи
   - Подключение локального решателя GeekedTest (при наличии ML-зависимостей)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
from threading import Event
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests

import anti_bot.challenge_process as challenge_process
from anti_bot import geetest_solve
from anti_bot.firewallpow_payload_reference import (
    build_get_payload,
    build_verify_payload,
    decode_challenge_jwt,
)
from anti_bot.geetest_solve import (
    GEETEST_BASE_URL,
    GEETEST_CAPTCHA_ID,
    GEETEST_LOAD_URL,
    GeetestUnavailable,
)
from anti_bot.generate_qrator_variants import (
    DEFAULT_BASE_F,
    DEFAULT_BASE_S,
    generate_variants,
    load_f_base,
    load_qrator_profiles,
    load_s_base,
    profile_canvas_seed,
    select_qrator_profile,
)
from anti_bot.qrator_payload_codec import DEFAULT_KEY, extract_xtea_key_from_js
from logger_setup import log_event
from runtime_metrics import METRICS
from anti_bot.pow_budget import (
    DEFAULT_POW_MAX_ITERATIONS,
    DEFAULT_POW_TIMEOUT,
    PowSearchError,
    check_cancelled,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.avito.ru"
BROWSER_VERSION = "147"
BROWSER_PROFILE = f"firefox{BROWSER_VERSION}"
# Keep UA aligned with the Windows fingerprint in anti_bot/base.txt even on a Linux VPS.
DEFAULT_UA_PLATFORM = os.getenv("AVITO_UA_PLATFORM", "Windows NT 10.0; Win64; x64").strip() or "Windows NT 10.0; Win64; x64"
# A key found in Qrator's script replaces DEFAULT_KEY only with AVITO_QRATOR_DYNAMIC_KEY=1. Off: the find is
# only logged, because a wrong guess would break every Qrator solve that works with the built-in key.
QRATOR_DYNAMIC_KEY = os.getenv("AVITO_QRATOR_DYNAMIC_KEY", "0").strip() == "1"
_REPORTED_KEY_SCRIPTS: set[str] = set()
POW_GET_URL = f"{BASE_URL}/web/3/firewallPow/get"
POW_VERIFY_URL = f"{BASE_URL}/web/3/firewallPow/verify"
FIREWALL_CAPTCHA_GET_URL = f"{BASE_URL}/web/5/firewallCaptcha/get"
FIREWALL_CAPTCHA_VERIFY_URL = f"{BASE_URL}/web/3/firewallCaptcha/verify"
QRATOR_FT_URL = f"{BASE_URL}/web/2/ft"
QRATOR_PIXEL_URL = f"{BASE_URL}/web/1/u"
QRATOR_FAVICON_URL = f"{BASE_URL}/favicon.ico"

GEEKED_TEST_ROOT = geetest_solve.GEEKED_TEST_ROOT
geetest_solve.ensure_geeked_path()

# On top of the PoW search budget: the worker process's start-up and its answer.
POW_PROCESS_SLACK = 10.0
# Upper bound for one GeeTest solve in the worker, the scan's own deadline permitting.
GEETEST_PROCESS_TIMEOUT = 45.0


def get_browser_user_agent() -> str:
    """User-Agent с той же версией и платформой, что и TLS/Qrator-профиль."""
    return f"Mozilla/5.0 ({DEFAULT_UA_PLATFORM}; rv:{BROWSER_VERSION}.0) Gecko/20100101 Firefox/{BROWSER_VERSION}.0"


# ═══════════════════════════════════════════════════════════════════════════
# 1. QRATOR PROTECTION
# ═══════════════════════════════════════════════════════════════════════════

def is_qrator_challenge_response(response: Any) -> bool:
    """Определяет, выдал ли QRATOR страницу с проверкой браузера."""
    if not hasattr(response, "status_code"):
        return False

    status = response.status_code
    headers = getattr(response, "headers", {}) or {}
    server = next((str(value).strip().upper() for key, value in headers.items() if key.lower() == "server"), "")
    text = getattr(response, "text", "") or ""

    has_meta_refresh = bool(
        re.search(r"<meta\b[^>]*\bhttp-equiv\s*=\s*[\"']refresh[\"']", text, re.IGNORECASE)
    )
    has_fingerprint_script = bool(
        re.search(r"<script\b[^>]*\bsrc\s*=\s*[\"']/[0-9a-f]{20}\.js[\"']", text, re.IGNORECASE)
    )

    if status in (302, 429) and (server == "QRATOR" or "qrator" in server.lower()):
        if has_meta_refresh or has_fingerprint_script:
            return True

    # Прокси может убрать Server, но характерный script + refresh останутся.
    if has_fingerprint_script and ("QRATOR" in server or has_meta_refresh):
        return True

    return False


def extract_qrator_script_url(qrator_html: str, page_url: str) -> Optional[str]:
    """Извлекает абсолютный URL скрипта снятия отпечатка пальца Qrator."""
    script_sources = re.findall(
        r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']",
        qrator_html,
        re.IGNORECASE,
    )
    for source in script_sources:
        absolute_url = urljoin(page_url, source)
        parsed = urlsplit(absolute_url)
        if (
            parsed.netloc == urlsplit(BASE_URL).netloc
            and re.fullmatch(r"/[0-9a-f]{20}\.js", parsed.path)
        ):
            return absolute_url
    return None


def build_qrator_multipart(f_value: str, s_value: str) -> tuple[str, bytes]:
    """Формирует multipart body для запроса /web/2/ft точь-в-точь как JS бандл Qrator."""
    boundary = f"{secrets.randbelow(10**16):016d}"
    body = (
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="f"'
        f"\r\n\r\n{f_value}"
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="s"'
        f"\r\n\r\n{s_value}"
        f"\r\n--{boundary}--\r\n"
    ).encode("ascii")
    return f'multipart/form-data;boundary="{boundary}"', body


def solve_qrator_challenge(
    session: requests.Session,
    response: Any,
    page_url: str,
    timeout: int = 15,
    *,
    cancel_event: Event | None = None,
    profile_key: str | None = None,
) -> bool:
    """
    Best-effort обмен QRATOR с эталонным отпечатком (JS не исполняется):
    1. Загружает JS-скрипт отпечатка и favicon (проверяет XTEA-ключ в скрипте)
    2. Генерирует XTEA-зашифрованные поля 'f' и 's' на базе эталонных отпечатков
    3. Отправляет /web/2/ft и сохраняет куки 'f' и 'ft'
    4. Запрашивает проверочный пиксель /web/1/u
    """
    check_cancelled(cancel_event)
    qrator_html = getattr(response, "text", "") or ""
    logger.debug("🛡 Обнаружен челлендж QRATOR. Запуск протокола обхода...")
    sequence_start = time.monotonic()

    # 1. Загрузка fingerprint-скрипта
    script_url = extract_qrator_script_url(qrator_html, page_url)
    if not script_url:
        logger.warning("QRATOR challenge has no supported fingerprint script")
        return False
    xtea_key = DEFAULT_KEY
    try:
        script_resp = session.get(
            script_url,
            headers={"Referer": page_url, "Accept": "*/*"},
            timeout=timeout,
            allow_redirects=False,
        )
        if script_resp.status_code != 200:
            logger.warning("QRATOR script rejected: HTTP %s", script_resp.status_code)
            return False
        script_text = getattr(script_resp, "text", "") or ""
        extracted_key = extract_xtea_key_from_js(script_text)
        if extracted_key and extracted_key != DEFAULT_KEY:
            script_sha = hashlib.sha256(script_text.encode("utf-8", "ignore")).hexdigest()[:12]
            if script_sha not in _REPORTED_KEY_SCRIPTS:  # once per script version, not on every solve
                _REPORTED_KEY_SCRIPTS.add(script_sha)
                logger.warning(
                    "В скрипте Qrator (%s) найден другой ключ XTEA: %s — %s",
                    script_sha, extracted_key,
                    "применён" if QRATOR_DYNAMIC_KEY else "не применён (AVITO_QRATOR_DYNAMIC_KEY=0)",
                )
            if QRATOR_DYNAMIC_KEY:
                xtea_key = extracted_key
    except Exception as e:
        logger.debug(f"Ошибка загрузки Qrator JS скрипта: {e}")
        return False

    # 2. Загрузка favicon
    check_cancelled(cancel_event)
    try:
        session.get(
            QRATOR_FAVICON_URL,
            headers={"Referer": page_url, "Accept": "image/*,*/*;q=0.8"},
            timeout=timeout,
            allow_redirects=False,
        )
    except Exception as e:
        logger.debug(f"Ошибка загрузки Qrator favicon: {e}")

    # Имитация времени выполнения JS в браузере (1 сек)
    elapsed = time.monotonic() - sequence_start
    if elapsed < 1.0:
        if cancel_event is not None:
            cancel_event.wait(1.0 - elapsed)
        else:
            time.sleep(1.0 - elapsed)
    check_cancelled(cancel_event)

    # 3. Синтетические поля 'f' и 's'; валидность определяет только сервер.
    try:
        if load_qrator_profiles():
            f_base, s_base, _profile_label = select_qrator_profile(profile_key)
        else:
            f_base = load_f_base(DEFAULT_BASE_F)
            s_base = load_s_base(DEFAULT_BASE_S)
        variant_kwargs: dict[str, Any] = {"count": 1, "timestamp": int(time.time())}
        seed = profile_canvas_seed(profile_key)
        if seed is not None:
            variant_kwargs["seed"] = seed
        if xtea_key != DEFAULT_KEY:
            variant_kwargs["key"] = xtea_key
        variants = generate_variants(f_base, s_base, **variant_kwargs)
        f_value = variants[0]["f"]
        s_value = variants[0]["s"]
    except Exception as e:
        logger.error(f"Ошибка генерации отпечатка Qrator: {e}")
        return False

    # Устанавливаем куку f
    session.cookies.delete("f")
    session.cookies.set("f", f_value, domain=".avito.ru", path="/", secure=True)
    check_cancelled(cancel_event)

    # 4. Отправка multipart в /web/2/ft
    content_type, multipart_body = build_qrator_multipart(f_value, s_value)
    ft_headers = {
        "Accept": "*/*",
        "Origin": BASE_URL,
        "Referer": page_url,
        "Content-Type": content_type,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    try:
        ft_resp = session.post(
            QRATOR_FT_URL,
            data=multipart_body,
            headers=ft_headers,
            timeout=timeout,
            allow_redirects=False,
        )
        if ft_resp.status_code != 200:
            logger.warning(f"Qrator /web/2/ft вернул статус {ft_resp.status_code}")
            return False

        # Qrator возвращает строку токена в JSON
        token = ft_resp.json()
        if not isinstance(token, str) or not token:
            logger.warning("Qrator /web/2/ft вернул пустой токен")
            return False

        # Важно: JS Qrator помещает в куку токен с обрамляющими кавычками
        ft_cookie_val = json.dumps(token, ensure_ascii=False, separators=(",", ":"))
        session.cookies.delete("ft")
        session.cookies.set("ft", ft_cookie_val, domain=".avito.ru", path="/", secure=True)

    except Exception as e:
        logger.error(f"Исключение при запросе к Qrator /web/2/ft: {e}")
        return False

    # 5. Запрос проверочного пикселя /web/1/u?{pixel_id}
    check_cancelled(cancel_event)
    pixel_id = secrets.randbelow(0xFFFFFFFF)
    pixel_headers = {
        "Accept": "image/avif,image/webp,image/png,image/svg+xml,image/*;q=0.8,*/*;q=0.5",
        "Referer": page_url,
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-origin",
    }
    try:
        pixel_resp = session.get(
            f"{QRATOR_PIXEL_URL}?{pixel_id}",
            headers=pixel_headers,
            timeout=timeout,
            allow_redirects=False,
        )
        if pixel_resp.status_code not in (200, 204):
            logger.warning("Qrator pixel rejected: HTTP %s", pixel_resp.status_code)
            return False
    except Exception as e:
        logger.debug(f"Ошибка запроса пикселя Qrator: {e}")
        return False

    check_cancelled(cancel_event)
    logger.debug("🍪 QRATOR выдал токен; доступ подтвердит только повторный запрос целевой страницы")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# 2. FIREWALL PROOF-OF-WORK (PoW)
# ═══════════════════════════════════════════════════════════════════════════

def extract_pow_challenge(response: Any, session: requests.Session) -> Optional[str]:
    """
    Извлекает pow_challenge из:
    1. Тела ответа JSON (при HTTP 439 / 429)
    2. Кук ответа
    3. Кук сессии (только если ответ вернул статус блокировки 403, 429 или 439)
    """
    # 1. Из тела ответа JSON
    try:
        data = response.json()
        if isinstance(data, dict):
            ch = data.get("pow_challenge")
            if isinstance(ch, str) and ch:
                return ch
    except Exception:
        pass

    # 2. Из кук ответа
    if hasattr(response, "cookies") and response.cookies.get("pow_challenge"):
        return response.cookies.get("pow_challenge")

    # 3. Из кук сессии — ТОЛЬКО если код ответа сигнализирует о блоке или челлендже
    status = getattr(response, "status_code", None)
    if status in (403, 429, 439) and hasattr(session, "cookies") and session.cookies.get("pow_challenge"):
        return session.cookies.get("pow_challenge")

    return None


def _pow_verify_payload(
    challenge_jwt: str,
    *,
    timeout: float,
    max_iterations: int,
    cancel_event: Event | None,
) -> dict:
    """The nonce is searched in the worker process; without one, in this thread as before."""
    if challenge_process.enabled():
        parameters = decode_challenge_jwt(challenge_jwt)
        try:
            nonce = challenge_process.run(
                "pow", parameters.challenge_id, parameters.complexity, timeout, max_iterations,
                timeout=timeout + POW_PROCESS_SLACK, cancel_event=cancel_event,
            )
        except challenge_process.ChallengeProcessUnavailable:
            pass
        else:
            return {"challenge": challenge_jwt, "nonce": int(nonce)}
    return build_verify_payload(challenge_jwt, timeout=timeout, max_iterations=max_iterations, cancel_event=cancel_event)


def solve_pow_challenge(
    session: requests.Session,
    pow_challenge: str,
    referer: str = "https://www.avito.ru/",
    timeout: int = 15,
    *,
    cancel_event: Event | None = None,
    pow_timeout: float = DEFAULT_POW_TIMEOUT,
    max_iterations: int = DEFAULT_POW_MAX_ITERATIONS,
) -> bool:
    """
    Решает криптографический Proof-of-Work (PoW) Авито:
    1. Запрашивает JWT задания на /web/3/firewallPow/get
    2. Декодирует сложность (compl) и ID задания из полезной нагрузки JWT
    3. Подбирает nonce, при котором sha256(f"{cid}:{nonce}") начинается с N нулей
    4. Отправляет решение на /web/3/firewallPow/verify
    5. Проверяет серверное подтверждение; cookie и их TTL задаёт сервер
    """
    check_cancelled(cancel_event)
    try:
        headers = {
            "Content-Type": "application/json",
            "Referer": referer,
            "Origin": BASE_URL,
            "Accept": "application/json, text/plain, */*",
        }

        # 1. Запрос challenge_jwt
        get_payload = build_get_payload(pow_challenge)
        get_resp = session.post(
            POW_GET_URL,
            json=get_payload,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        if get_resp.status_code != 200:
            logger.warning(f"firewallPow/get ответил статусом {get_resp.status_code}")
            return False

        get_json = get_resp.json()
        challenge_jwt = get_json.get("success", {}).get("result", {}).get("challenge_jwt")
        if not challenge_jwt:
            logger.warning("firewallPow/get: challenge_jwt не найден в ответе")
            return False

        # 2. Вычисление SHA-256 PoW
        logger.debug("🧩 firewallPow/get получен, вычисляем SHA-256 PoW...")
        verify_payload = _pow_verify_payload(
            challenge_jwt, timeout=pow_timeout, max_iterations=max_iterations, cancel_event=cancel_event,
        )
        check_cancelled(cancel_event)

        # 3. Отправка решения на проверку
        verify_resp = session.post(
            POW_VERIFY_URL,
            json=verify_payload,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        check_cancelled(cancel_event)
        if verify_resp.status_code != 200:
            logger.warning(f"firewallPow/verify ответил статусом {verify_resp.status_code}")
            return False

        verify_json = verify_resp.json()
        result = verify_json.get("success", {}).get("result", {})
        if result.get("verified") is True:
            unblock_ttl = result.get("unblock_ttl")
            # Только сервер задаёт время жизни cookie; локальная метка не является пропуском.
            # Очищаем временный вызов pow_challenge, чтобы исключить ложные срабатывания
            try:
                session.cookies.delete("pow_challenge")
            except Exception:
                pass
            logger.debug(f"🧩 Сервер принял PoW (unblock_ttl: {unblock_ttl} сек); проверяем целевую страницу")
            return True
        else:
            logger.info(f"PoW отклонен сервером: {verify_resp.text[:200]}")
            return False

    except PowSearchError:
        raise
    except Exception as e:
        logger.warning(f"Не удалось решить PoW: {str(e)[:200]}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# 3. GEETEST v4 CAPTCHA
# ═══════════════════════════════════════════════════════════════════════════

def is_geetest_challenge(response: Any) -> bool:
    """Определяет, требует ли ответ прохождения визуальной капчи GeeTest."""
    text = getattr(response, "text", "") or ""
    if "initGeetest4" in text and "gt4.js" in text and "captchaId" in text:
        return True

    if getattr(response, "status_code", 0) in (403, 429):
        try:
            data = response.json()
            if isinstance(data, dict):
                tmr = data.get("too-many-requests", {})
                if isinstance(tmr, dict) and "/firewall/captcha/show" in str(tmr.get("link", "")):
                    return True
        except Exception:
            pass

    return False


# GeeTest's own servers do not care which IP solves the puzzle; only Avito's final verify must come from
# the challenged IP. Talking to GeeTest directly avoids slow-proxy timeouts and keeps those requests out of
# the proxy's rate budget and block statistics.
GEETEST_DIRECT = os.getenv("GEETEST_DIRECT", "1").strip() != "0"


def _session_user_agent(avito_session: Any) -> str:
    headers = getattr(avito_session, "headers", None) or {}
    return headers.get("User-Agent") or headers.get("user-agent") or get_browser_user_agent()


def _geetest_session(avito_session: Any) -> tuple[Any, bool]:
    """(session for GeeTest calls, whether it must be closed afterwards)."""
    if not GEETEST_DIRECT:
        return avito_session, False
    return geetest_solve.direct_session(_session_user_agent(avito_session), BROWSER_PROFILE), True


def _geetest_seccode(
    session: Any,
    captcha_id: str,
    *,
    cancel_event: Event | None,
    deadline: float | None,
) -> Optional[dict]:
    """GeeTest's answer: from the worker process when GeeTest goes direct, else in this thread."""
    if GEETEST_DIRECT and challenge_process.enabled():
        budget = GEETEST_PROCESS_TIMEOUT
        if deadline is not None:
            budget = min(budget, deadline - time.monotonic())
        if budget <= 0:
            return None  # the scan is out of time: Avito's verify could not be sent anyway
        try:
            return challenge_process.run(
                "geetest", captcha_id, _session_user_agent(session), BROWSER_PROFILE,
                timeout=budget, cancel_event=cancel_event,
            )
        except challenge_process.ChallengeProcessUnavailable:
            pass
    geetest_session, owns_geetest_session = _geetest_session(session)
    try:
        return geetest_solve.request_seccode(geetest_session, captcha_id, cancel_event=cancel_event)
    finally:
        if owns_geetest_session:
            try:
                geetest_session.close()
            except Exception:
                pass


def solve_geetest_if_available(
    session: requests.Session,
    response: Any,
    page_url: str,
    *,
    cancel_event: Event | None = None,
    deadline: float | None = None,
) -> bool:
    """
    Решает GeeTest v4 модулем GeekedTest: в процессе антибота, если он запущен, иначе в этом потоке.
    Если ML-библиотеки (opencv, ddddocr, pycryptodome) не установлены,
    выводит понятное предупреждение в лог без падения бота.
    """
    check_cancelled(cancel_event)
    try:
        captcha_id = geetest_solve.captcha_id_from_page(getattr(response, "text", "") or "")
        logger.debug(
            f"🤖 Запуск автономного решателя GeeTest v4 (id: {captcha_id}, "
            f"{'GeeTest напрямую с сервера' if GEETEST_DIRECT else 'через канал сканирования'})..."
        )
        seccode = _geetest_seccode(session, captcha_id, cancel_event=cancel_event, deadline=deadline)
        if seccode is None:
            return False

        # Подтверждение уходит на Авито с того же IP, который получил капчу (через канал сканирования)
        check_cancelled(cancel_event)
        cube_val = str(10 + secrets.randbelow(90))
        verify_payload = {
            "captcha": "",
            "hCaptchaResponse": "",
            **seccode,
        }
        verify_resp = session.post(
            FIREWALL_CAPTCHA_VERIFY_URL,
            json=verify_payload,
            headers={
                "Referer": page_url,
                "Origin": BASE_URL,
                "X-Cube": cube_val,
                "Content-Type": "application/json",
            },
            timeout=15,
            allow_redirects=False,
        )
        check_cancelled(cancel_event)
        if verify_resp.status_code != 200:
            return False

        v_body = verify_resp.json()
        if v_body.get("success", {}).get("result", {}).get("verified") is True:
            logger.debug("🧩 Авито принял решение GeeTest; проверяем целевую страницу")
            return True

        logger.debug("GeeTest: Авито отклонил решение капчи")
        return False

    except PowSearchError:
        raise
    except GeetestUnavailable as e:
        logger.warning(
            "⚠️ Авито выдал визуальную капчу GeeTest v4! "
            "Для автономного решения установите зависимости: "
            "pip install opencv-python numpy pycryptodome requests ddddocr. "
            f"(Причина отсутствия модуля: {e})"
        )
        return False
    except Exception as e:
        logger.warning(f"Не удалось решить GeeTest: {str(e)[:200]}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# 4. ОБЩИЙ ДИСПЕТЧЕР ЗАЩИТЫ
# ═══════════════════════════════════════════════════════════════════════════

def _timed_solve(kind: str, solver, *args, **kwargs) -> bool:
    """One line per challenge: which one, solved or not, how long the solver held its thread."""
    started = time.monotonic()
    solved = False
    try:
        solved = bool(solver(*args, **kwargs))
        return solved
    finally:
        seconds = time.monotonic() - started
        METRICS.record_challenge(kind, solved=solved, seconds=seconds)
        log_event(
            logger,
            logging.INFO,
            f"antibot.{kind}",
            f"{'✓' if solved else '✗'} {kind} {seconds:.1f} с",
            kind=kind,
            solved=solved,
            s=round(seconds, 2),
        )


def handle_anti_bot_challenge(
    session: requests.Session,
    response: Any,
    page_url: str,
    *,
    cancel_event: Event | None = None,
    deadline: float | None = None,
    profile_key: str | None = None,
) -> bool:
    """
    Универсальный обработчик защиты Авито.
    Проверяет тип защиты (QRATOR, PoW 439, GeeTest) и решает её.
    True означает, что этап решения принят и можно повторить запрос.
    Это НЕ подтверждение доступа: вызывающий код обязан проверить исходную страницу.
    deadline (time.monotonic) ограничивает решение GeeTest в процессе антибота сроком скана.
    """
    check_cancelled(cancel_event)
    # 1. QRATOR 302/429
    if is_qrator_challenge_response(response):
        qrator_kwargs: dict[str, Any] = {"cancel_event": cancel_event}
        if profile_key is not None:
            qrator_kwargs["profile_key"] = profile_key
        return _timed_solve("qrator", solve_qrator_challenge, session, response, page_url, **qrator_kwargs)

    # 2. Firewall PoW (439, 429 или в куках)
    pow_challenge = extract_pow_challenge(response, session)
    if pow_challenge:
        return _timed_solve("pow", solve_pow_challenge, session, pow_challenge, referer=page_url, cancel_event=cancel_event)

    # 3. GeeTest v4 Captcha
    if is_geetest_challenge(response):
        return _timed_solve("geetest", solve_geetest_if_available, session, response, page_url,
                            cancel_event=cancel_event, deadline=deadline)

    return False
