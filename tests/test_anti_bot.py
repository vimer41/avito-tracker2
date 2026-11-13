import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from tests import _environment
from curl_cffi import requests
import parser as parser_module
from parser import AvitoParser, NetworkBlockState, parse_retry_after
from anti_bot import manager
from anti_bot.firewallpow_payload_reference import PowParameters, build_verify_payload, decode_challenge_jwt, find_nonce, sha256_hex
from anti_bot.pow_budget import PowSearchCancelled, PowSearchLimit, PowSearchTimeout, has_leading_zero_bits
from geeked.sign import Signer
from geeked.geeked import Geeked
from geeked import dddd_server


def make_jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class FakeResponse:
    def __init__(self, status=200, text="", *, payload=None, headers=None):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}
        self.cookies = requests.Cookies()
        self.payload = payload

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def __bool__(self):
        return self.status_code < 400


class FakeSession:
    def __init__(self, responses=()):
        self.cookies = requests.Cookies()
        self.headers = {}
        self.get = Mock(side_effect=list(responses))
        self.post = Mock()
        self.close = Mock()


class TestLocalPow(unittest.TestCase):
    def test_low_difficulty_payload_is_valid(self):
        token = make_jwt({"id": "offline-fixture", "compl": 2})
        payload = build_verify_payload(token, timeout=1, max_iterations=10000)
        self.assertEqual(payload["challenge"], token)
        self.assertTrue(sha256_hex("offline-fixture", payload["nonce"]).startswith("00"))

    def test_difficulty_65_is_rejected_before_search(self):
        with patch("anti_bot.firewallpow_payload_reference.find_nonce") as search:
            with self.assertRaises(ValueError):
                build_verify_payload(make_jwt({"id": "test", "compl": 65}))
            search.assert_not_called()
        with self.assertRaises(ValueError):
            PowParameters("test", 65)

    def test_malformed_jwt_and_parameters_are_rejected(self):
        for payload in ([], None, {"id": "", "compl": 0}, {"id": "x", "compl": True}, {"id": "x", "compl": -1}, {"id": "x", "compl": 1.0}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_challenge_jwt(make_jwt(payload))
        for value in (None, "", "one-part", "header.%%%.signature"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_challenge_jwt(value)

    def test_iteration_and_time_limits_are_finite(self):
        with patch("anti_bot.firewallpow_payload_reference.sha256_hex", return_value="f" * 64) as hash_function:
            with self.assertRaises(PowSearchLimit):
                find_nonce(PowParameters("x", 1), max_iterations=3)
            self.assertEqual(hash_function.call_count, 3)
        with patch("anti_bot.pow_budget.time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(PowSearchTimeout):
                find_nonce(PowParameters("x", 1), timeout=1)

    def test_cancellation_and_invalid_budgets(self):
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(PowSearchCancelled):
            find_nonce(PowParameters("x", 0), cancel_event=cancelled)
        for kwargs in ({"timeout": 0}, {"timeout": float("inf")}, {"max_iterations": True}, {"max_iterations": 0}, {"start": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                find_nonce(PowParameters("x", 0), **kwargs)

    def test_all_partial_nibble_thresholds(self):
        for bits in (1, 2, 3, 5, 6, 7):
            full, remainder = divmod(bits, 4)
            for nibble in range(16):
                digest = "0" * full + f"{nibble:x}" + "f" * (63 - full)
                with self.subTest(bits=bits, nibble=nibble):
                    self.assertEqual(has_leading_zero_bits(digest, bits), nibble < 2 ** (4 - remainder))
        self.assertTrue(has_leading_zero_bits("0" * 64, 256))
        self.assertFalse(has_leading_zero_bits("1" + "0" * 63, 4))
        with self.assertRaises(ValueError):
            has_leading_zero_bits("0" * 64, 257)

    def test_geetest_rejects_the_old_invalid_one_bit_solution(self):
        prefix = "1|1|md5|test|captcha|lot||"
        candidates = [f"{i:016x}" for i in range(100)]
        invalid = next(c for c in candidates if int(hashlib.md5((prefix + c).encode()).hexdigest()[0], 16) >= 8)
        valid = next(c for c in candidates if int(hashlib.md5((prefix + c).encode()).hexdigest()[0], 16) < 8)
        with patch.object(Signer, "rand_uid", side_effect=[invalid, valid]) as uid:
            result = Signer.generate_pow("lot", "captcha", "md5", 1, 1, "test", "", max_iterations=2)
        self.assertEqual(result["pow_msg"], prefix + valid)
        self.assertEqual(uid.call_count, 2)
        self.assertTrue(has_leading_zero_bits(result["pow_sign"], 1))

    def test_geetest_pow_limits_and_unknown_protocol(self):
        for hash_function, size in (("md5", 128), ("sha1", 160), ("sha256", 256)):
            with self.subTest(hash_function=hash_function), self.assertRaises(ValueError):
                Signer.generate_pow("lot", "captcha", hash_function, 1, size + 1, "test", "")
        with self.assertRaises(ValueError):
            Signer.generate_pow("lot", "captcha", "unknown", 1, 0, "test", "")
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(PowSearchCancelled):
            Signer.generate_pow("lot", "captcha", "sha256", 1, 1, "test", "", cancel_event=cancelled)
        with patch.object(Signer, "rand_uid", return_value="0" * 16):
            with self.assertRaises(PowSearchLimit):
                Signer.generate_pow("lot", "captcha", "sha256", 1, 256, "test", "", max_iterations=2)
        with self.assertRaises(NotImplementedError):
            Signer.generate_w({"pt": "2"}, "captcha", "invisible")
        self.assertEqual(Signer.encrypt_w("a b", 0), "a+b")


class TestChallengeManager(unittest.TestCase):
    def test_bootstrap_limits_native_threads_before_ml_imports(self):
        environment = dict(os.environ)
        environment.update({name: "64" for name in _environment.NATIVE_THREAD_LIMITS})
        probe = (
            "from tests import _environment; import json, os, sys; "
            "print(json.dumps({'limits': {name: os.environ[name] for name in "
            "_environment.NATIVE_THREAD_LIMITS}, 'ml_imported': any(name in "
            "sys.modules for name in ('numpy', 'onnxruntime', 'ddddocr'))}))"
        )
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", "-c", probe],
            cwd=Path(__file__).resolve().parents[1], env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["limits"], _environment.NATIVE_THREAD_LIMITS)
        self.assertFalse(observed["ml_imported"])

    def test_qrator_pixel_failure_is_not_success(self):
        challenge = FakeResponse(429, '<script src="/0123456789abcdefabcd.js"></script>', headers={"server": "QRATOR"})
        for status, expected in ((429, False), (200, True)):
            session = FakeSession([FakeResponse(), FakeResponse(), FakeResponse(status)])
            session.post.return_value = FakeResponse(payload="offline-token")
            with self.subTest(status=status), patch.object(manager, "load_f_base", return_value={}), patch.object(manager, "load_s_base", return_value={}), patch.object(manager, "generate_variants", return_value=[{"f": "fingerprint", "s": "signature"}]), patch.object(manager.time, "sleep"):
                self.assertEqual(manager.solve_qrator_challenge(session, challenge, "https://www.avito.ru/test"), expected)
            self.assertEqual(session.get.call_count, 3)

    def test_pow_server_acceptance_does_not_create_a_fake_clearance_cookie(self):
        session = FakeSession()
        session.cookies.set("pow_challenge", "temporary", domain=".avito.ru")
        session.post.side_effect = [
            FakeResponse(payload={"success": {"result": {"challenge_jwt": make_jwt({"id": "test", "compl": 0})}}}),
            FakeResponse(payload={"success": {"result": {"verified": True, "unblock_ttl": 10}}}),
        ]
        self.assertTrue(manager.solve_pow_challenge(session, "test"))
        self.assertIsNone(session.cookies.get("pow_solved"))
        self.assertIsNone(session.cookies.get("pow_challenge"))

    def test_geetest_uses_task_protocol_session_and_finite_timeouts(self):
        session = FakeSession()
        solver = Geeked("captcha", session=session, request_headers={"Referer": "https://www.avito.ru/"})
        data = {"lot_number": "lot", "payload": "payload", "process_token": "token", "captcha_type": "invisible", "pt": "0", "payload_protocol": "2"}
        session.get.side_effect = lambda url, **kwargs: FakeResponse(text=f'{kwargs["params"]["callback"]}({{"status":"success","data":{{"seccode":{{"lot_number":"lot"}}}}}});')
        with patch.object(Signer, "generate_w", return_value="offline-w"):
            self.assertEqual(solver.submit_captcha(data), {"lot_number": "lot"})
        kwargs = session.get.call_args.kwargs
        self.assertEqual(kwargs["params"]["pt"], "0")
        self.assertEqual(kwargs["params"]["payload_protocol"], "2")
        self.assertEqual(kwargs["timeout"], 15)
        self.assertIs(solver.session, session)
        solver.callback = "expected"
        with self.assertRaises(ValueError):
            solver.format_response('other({"status":"success","data":{}})')

    def test_icon_service_is_lazy_and_thread_safe(self):
        sentinel = object()
        with patch.object(dddd_server, "_dddd_service", None), patch.object(dddd_server, "DdddService", return_value=sentinel) as constructor:
            constructor.assert_not_called()
            with ThreadPoolExecutor(max_workers=4) as pool:
                services = list(pool.map(lambda _: dddd_server.get_dddd_service(), range(12)))
            self.assertTrue(all(service is sentinel for service in services))
            constructor.assert_called_once_with()

    def test_icon_models_initialize_offline(self):
        service = dddd_server.DdddService()
        try:
            self.assertTrue(callable(service.det.detection))
            self.assertTrue(callable(service.cnn.classification))
        finally:
            service.det.cleanup()
            service.cnn.cleanup()

    def test_icon_recognition_never_fills_missing_answers_randomly(self):
        import cv2
        import numpy as np
        from geeked.icon import IconSolver
        _, encoded = cv2.imencode(".png", np.zeros((20, 20, 3), dtype=np.uint8))
        session = FakeSession()
        response = FakeResponse()
        response.content = encoded.tobytes()
        session.get.return_value = response
        session.get.side_effect = None
        service = Mock()
        service.detection.return_value = [[0, 0, 10, 10]]
        service.classification.return_value = "icon_u"
        solver = IconSolver("fixture.png", [next(iter(IconSolver.ICON_MAPPING))], session=session)
        with patch.object(dddd_server, "get_dddd_service", return_value=service):
            self.assertEqual(solver.find_icon_position(), [[165.0, 245.0]])
            service.classification.return_value = "unknown"
            with self.assertRaises(ValueError):
                solver.find_icon_position()


class TestParserProtection(unittest.IsolatedAsyncioTestCase):
    def make_parser(self, session, block_state=None, save=False):
        with patch.object(AvitoParser, "_create_session", return_value=session), patch.object(AvitoParser, "_load_cookies"):
            result = AvitoParser(block_state=block_state or NetworkBlockState(requests_per_second=1000))
        if not save:
            result._save_cookies = Mock()
        self.addCleanup(result.close)
        return result

    def test_solver_token_is_not_page_acceptance(self):
        challenge = FakeResponse(200, '<script src="/0123456789abcdefabcd.js"></script>', headers={"server": "QRATOR"})
        session = FakeSession([challenge, challenge, challenge])
        parser = self.make_parser(session)
        with patch.object(parser_module, "handle_anti_bot_challenge", return_value=True) as solve:
            self.assertIsNone(parser.fetch_page("https://www.avito.ru/search"))
        self.assertEqual(solve.call_count, 2)
        self.assertEqual(session.get.call_count, 3)
        self.assertTrue(parser.is_blocked())
        parser._save_cookies.assert_not_called()

    def test_challenge_chain_preserves_session_and_checks_final_page(self):
        session = FakeSession([
            FakeResponse(439, payload={"pow_challenge": "test"}),
            FakeResponse(429, 'initGeetest4 gt4.js captchaId'),
            FakeResponse(200, "<html><body>catalog</body></html>"),
        ])
        parser = self.make_parser(session)
        def accept(challenge_session, response, url, **kwargs):
            self.assertIs(challenge_session, session)
            challenge_session.cookies.set("continuity", "yes", domain=".avito.ru")
            return True
        with patch.object(parser_module, "handle_anti_bot_challenge", side_effect=accept):
            self.assertIn("catalog", parser.fetch_page("https://www.avito.ru/search"))
        self.assertIs(parser.session, session)
        self.assertEqual(session.cookies.get("continuity"), "yes")
        parser._save_cookies.assert_called_once_with()

    def test_retry_after_blocks_all_sessions_on_same_connection(self):
        state = NetworkBlockState()
        first = self.make_parser(FakeSession([FakeResponse(429, headers={"Retry-After": "120"})]), state)
        second = self.make_parser(FakeSession(), state)
        with patch.object(parser_module, "handle_anti_bot_challenge", return_value=False):
            self.assertIsNone(first.fetch_page("https://www.avito.ru/search"))
        self.assertGreaterEqual(second.block_remaining(), 119)
        self.assertIsNone(second.fetch_page("https://www.avito.ru/search"))
        second.session.get.assert_not_called()
        self.assertEqual(parse_retry_after({"retry-after": "nan"}), 0)
        self.assertEqual(parse_retry_after({"Retry-After": "not-a-date"}), 0)

    def test_falsey_403_response_is_still_a_firewall(self):
        response = FakeResponse(403, "<html>Forbidden</html>")
        self.assertFalse(bool(response))
        self.assertTrue(AvitoParser._is_firewall(response))

    def test_client_profile_and_user_agent_match_and_handle_is_shared(self):
        session = FakeSession()
        with patch.object(parser_module.requests, "Session", return_value=session) as constructor, patch.object(AvitoParser, "_load_cookies"):
            parser = AvitoParser(proxy="http://127.0.0.1:9999", block_state=NetworkBlockState())
        self.addCleanup(parser.close)
        options = constructor.call_args.kwargs
        self.assertEqual(options["impersonate"], manager.BROWSER_PROFILE)
        self.assertIn(f"Firefox/{manager.BROWSER_VERSION}.0", options["headers"]["User-Agent"])
        self.assertFalse(options["use_thread_local_curl"])
        self.assertEqual(options["proxies"]["https"], "http://127.0.0.1:9999")

    def test_ssr_redirect_success_is_validated_before_cache_and_cookie_save(self):
        session = FakeSession([
            FakeResponse(200, '<script>{"isSuspenseRedirect":true,"redirect":"/canonical"}</script>'),
            FakeResponse(200, '<html><body>canonical catalog</body></html>'),
        ])
        parser = self.make_parser(session)
        self.assertIn("canonical catalog", parser.fetch_page("https://www.avito.ru/search"))
        self.assertEqual(parser._canonical_cache["https://www.avito.ru/search"], "https://www.avito.ru/canonical")
        parser._save_cookies.assert_called_once_with()

    def test_qrator_fingerprint_detected_with_capitalized_server_header(self):
        challenge = FakeResponse(200, '<script src="/0123456789abcdefabcd.js"></script>', headers={"Server": "QRATOR"})
        self.assertTrue(manager.is_qrator_challenge_response(challenge))

    def test_redirects_and_non_html_do_not_pass_validation(self):
        for response in (FakeResponse(302, headers={"Location": "https://avito.ru.evil.test/path"}), FakeResponse(200, text='{"success":true}'), FakeResponse(404)):
            session = FakeSession([response])
            parser = self.make_parser(session)
            self.assertIsNone(parser.fetch_page("https://www.avito.ru/search"))
            self.assertEqual(session.get.call_count, 1)
            parser._save_cookies.assert_not_called()
        for url in ("https://avito.ru.evil.test/", "https://evil.test/avito.ru", "https://avito.ru@evil.test/", "https://avito.ru:1234/", "https://user@avito.ru/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                AvitoParser.normalize_url(url)
        self.assertEqual(AvitoParser.normalize_url("HTTP://WWW.AVITO.RU/search?q=test&context=x"), "https://www.avito.ru/search?q=test&s=104")

    def test_cookies_round_trip_scope_expiry_and_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.make_parser(FakeSession(), save=True)
            first.cookie_file = Path(directory) / "cookies.json"
            first.session.cookies.set("scoped", "value", domain="m.avito.ru", path="/api", secure=True)
            cookie = next(iter(first.session.cookies.jar))
            cookie.expires = int(time.time()) + 3600
            cookie.discard = False
            first.session.cookies.set("pow_challenge", "temporary", domain=".avito.ru")
            first._save_cookies()
            second = self.make_parser(FakeSession(), save=True)
            second.cookie_file = first.cookie_file
            second._load_cookies()
            stored = list(second.session.cookies.jar)
            self.assertEqual(len(stored), 1)
            self.assertEqual((stored[0].domain, stored[0].path, stored[0].secure, stored[0].expires), ("m.avito.ru", "/api", True, cookie.expires))
            self.assertEqual(json.loads(first.cookie_file.read_text(encoding="utf-8"))["version"], 2)

    def test_hot_path_cookie_save_never_waits_for_fsync(self):
        import os
        import threading
        import parser as parser_module

        with tempfile.TemporaryDirectory() as directory:
            parser = self.make_parser(FakeSession(), save=True)
            parser.cookie_file = Path(directory) / "cookies.json"
            parser.session.cookies.set("scoped", "value", domain="m.avito.ru")
            jar = parser.session.cookies.jar
            release = threading.Event()
            entered = threading.Event()
            fsync_threads = []

            def slow_fsync(_fd):
                fsync_threads.append(threading.get_ident())
                entered.set()
                release.wait(5)

            with patch.object(os, "fsync", slow_fsync):
                parser._queue_cookie_save(jar)
                pending = parser._cookie_write
                self.assertTrue(entered.wait(5))
                self.assertFalse(pending.done())
                parser._queue_cookie_save(jar)  # a second write never queues behind a slow disk
                self.assertIs(parser._cookie_write, pending)
                self.assertTrue(parser_module.COOKIE_IO_LOCK.acquire(timeout=1))  # session creation reads under it
                parser_module.COOKIE_IO_LOCK.release()
                release.set()
                pending.result(5)

            self.assertNotEqual(fsync_threads, [threading.get_ident()])
            self.assertEqual(json.loads(parser.cookie_file.read_text(encoding="utf-8"))["cookies"][0]["name"], "scoped")

    async def test_async_cancellation_stops_worker_before_returning(self):
        parser = self.make_parser(FakeSession())
        started, stopped = Event(), Event()
        def cancellable_fetch(url, *, cancel_event):
            started.set()
            try:
                if not cancel_event.wait(2):
                    raise AssertionError("worker did not receive cancellation")
                raise PowSearchCancelled("test cancelled")
            finally:
                stopped.set()
        with patch.object(parser, "fetch_page", side_effect=cancellable_fetch):
            task = asyncio.create_task(parser.fetch_page_async("https://www.avito.ru/search"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
            self.assertTrue(stopped.is_set())

    def test_windows_user_agent_and_clean_navigation_headers(self):
        with patch.object(manager.sys, "platform", "linux"):
            ua = manager.get_browser_user_agent()
        self.assertIn("Windows NT 10.0; Win64; x64", ua)
        self.assertIn(f"Firefox/{manager.BROWSER_VERSION}.0", ua)
        headers = parser_module._default_headers()
        self.assertNotIn("Pragma", headers)
        self.assertNotIn("Cache-Control", headers)
        self.assertEqual(headers["Sec-Fetch-Site"], "none")

    def test_qrator_xtea_key_extraction_and_rotation(self):
        from anti_bot.qrator_payload_codec import DEFAULT_KEY, decode_field, extract_xtea_key_from_js

        rotated = (0x11223344, 0x55667788, 0x99AABBCC, 0xDDEEFF00)
        js_body = f"var d=0x9e3779b9;var w=[{rotated[0]},{rotated[1]},{rotated[2]},{rotated[3]}];"
        self.assertEqual(extract_xtea_key_from_js(js_body), rotated)
        self.assertIsNone(extract_xtea_key_from_js("var coords=[1920,1080,24,24];"))
        # The negated delta is recognised; without any delta a hash-constant array is not taken for a key.
        self.assertEqual(extract_xtea_key_from_js(js_body.replace("0x9e3779b9", "-0x61c88647")), rotated)
        self.assertIsNone(extract_xtea_key_from_js("var h=[0x67452301,0xefcdab89,0x98badcfe,0x10325476];"))

        challenge = FakeResponse(429, '<script src="/0123456789abcdefabcd.js"></script>', headers={"server": "QRATOR"})

        def solved_f_cookie(dynamic_key: bool) -> str:
            session = FakeSession([FakeResponse(200, js_body), FakeResponse(200), FakeResponse(200)])
            session.post.return_value = FakeResponse(payload="ok-token")
            with patch.object(manager.time, "sleep"), patch.object(manager, "QRATOR_DYNAMIC_KEY", dynamic_key):
                self.assertTrue(
                    manager.solve_qrator_challenge(session, challenge, "https://www.avito.ru/test", profile_key="p-abc")
                )
            return session.cookies.get("f")

        # Off (default): the found key is only logged, the built-in one still encrypts.
        self.assertEqual(len(decode_field(solved_f_cookie(False), "f", DEFAULT_KEY).split(";")), 141)
        # On: the key from the script is used.
        self.assertEqual(len(decode_field(solved_f_cookie(True), "f", rotated).split(";")), 141)
        self.assertEqual(DEFAULT_KEY[0], 1901231474)

    def test_qrator_profile_import_and_deterministic_selection(self):
        from anti_bot.generate_qrator_variants import (
            DEFAULT_BASE_F,
            DEFAULT_BASE_S,
            generate_variants,
            import_captured_profile,
            load_f_base,
            load_s_base,
            select_qrator_profile,
        )

        f_vals = load_f_base(DEFAULT_BASE_F)
        s_raw = load_s_base(DEFAULT_BASE_S)
        sample = generate_variants(f_vals, s_raw, count=1, timestamp=1700000000, seed=42)[0]
        with tempfile.TemporaryDirectory() as tmp:
            profiles_dir = Path(tmp)
            p1 = import_captured_profile(str(sample["f"]), str(sample["s"]), out_dir=profiles_dir, label="win11")
            p2 = import_captured_profile("1920;1080;" + ";".join(["1"] * 139), s_raw, out_dir=profiles_dir, label="alt")
            self.assertTrue(p1.exists() and p2.exists())
            sel_a = select_qrator_profile("proxy-1", profiles_dir=profiles_dir)
            sel_b = select_qrator_profile("proxy-1", profiles_dir=profiles_dir)
            self.assertEqual(sel_a, sel_b)

    def test_cache_busting_modes(self):
        base = "https://www.avito.ru/moskva?q=test&s=104"
        self.assertEqual(parser_module.apply_cache_busting(base + "&_t=123", mode="off"), base)
        b1 = parser_module.apply_cache_busting(base, mode="bucket", window_seconds=15.0, now=1005.0)
        b2 = parser_module.apply_cache_busting(base, mode="bucket", window_seconds=15.0, now=1019.0)
        b3 = parser_module.apply_cache_busting(base, mode="bucket", window_seconds=15.0, now=1020.0)
        self.assertEqual(b1, b2)
        self.assertNotEqual(b2, b3)
        ms = parser_module.apply_cache_busting(base, mode="ms", window_seconds=0.0, now=1001.234)
        self.assertIn("_t=1001234", ms)
        # The mode decides: "ms" ignores a window, "bucket" without a window uses 15 s.
        self.assertIn("_t=1001234", parser_module.apply_cache_busting(base, mode="ms", window_seconds=15.0, now=1001.234))
        self.assertIn("_t=990000", parser_module.apply_cache_busting(base, mode="bucket", window_seconds=0.0, now=1001.234))

    def test_redirect_and_ssr_redirect_send_same_origin_referer(self):
        session = FakeSession([
            FakeResponse(302, headers={"Location": "/step1"}),
            FakeResponse(200, '<script>{"isSuspenseRedirect":true,"redirect":"/final"}</script>'),
            FakeResponse(200, "<html><body>catalog</body></html>"),
        ])
        parser = self.make_parser(session)
        html = parser.fetch_page("https://www.avito.ru/search")
        self.assertIn("catalog", html)
        self.assertNotIn("headers", session.get.call_args_list[0].kwargs)
        # HTTP 30x: a browser adds no Referer. Only the page-driven SSR redirect carries one.
        self.assertNotIn("headers", session.get.call_args_list[1].kwargs)
        self.assertEqual(
            session.get.call_args_list[2].kwargs["headers"],
            {"Referer": "https://www.avito.ru/step1", "Sec-Fetch-Site": "same-origin"},
        )

    def test_session_warmup_runs_once_on_empty_cookies_when_enabled(self):
        import config

        session = FakeSession([
            FakeResponse(200, "<html>home</html>"),
            FakeResponse(200, "<html><body>catalog</body></html>"),
            FakeResponse(200, "<html><body>catalog2</body></html>"),
        ])
        parser = self.make_parser(session)
        with patch.object(config, "AVITO_SESSION_WARMUP", True), patch.dict(parser_module._WARMUP_AT, clear=True):
            self.assertIn("catalog", parser.fetch_page("https://www.avito.ru/search"))
            self.assertIn("catalog2", parser.fetch_page("https://www.avito.ru/search"))
            self.assertEqual(session.get.call_count, 3)
            self.assertEqual(session.get.call_args_list[0].args[0], "https://www.avito.ru/")
            # A new session of the same channel inside the gap (a blocked IP recreates it on every 403)
            # goes straight to the search.
            again = FakeSession([FakeResponse(200, "<html><body>catalog3</body></html>")])
            self.assertIn("catalog3", self.make_parser(again).fetch_page("https://www.avito.ru/search"))
            self.assertEqual(again.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
