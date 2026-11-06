"""Anti-bot work in a separate process: answers, kills on cancel and timeout, never an orphan."""

import hashlib
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tests import _environment  # noqa: F401
import anti_bot.challenge_process as challenge_process
import anti_bot.challenge_worker as challenge_worker
from anti_bot import geetest_solve, manager
from anti_bot.pow_budget import PowSearchCancelled, PowSearchTimeout


def wait_until(condition, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class WorkerCase(unittest.TestCase):
    def setUp(self):
        for name, value in (("ENABLED", True), ("WARM", False), ("_down_until", 0.0)):
            patcher = patch.object(challenge_process, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(challenge_process.shutdown)


class TestWorkerProcess(WorkerCase):
    def test_nonce_comes_from_another_process(self):
        nonce = challenge_process.run("pow", "abc", 3, 5.0, 2_000_000, timeout=20)
        self.assertTrue(hashlib.sha256(f"abc:{nonce}".encode()).hexdigest().startswith("000"))
        self.assertEqual(len(challenge_process.pids()), 1)
        self.assertNotEqual(challenge_process.pids(), [os.getpid()])

    def test_search_budget_error_keeps_the_worker(self):
        challenge_process.run("ping", timeout=20)
        before = challenge_process.pids()
        with self.assertRaises(PowSearchTimeout):
            challenge_process.run("pow", "abc", 64, 0.3, 2_000_000_000, timeout=20)
        self.assertEqual(challenge_process.pids(), before)

    def test_cancel_kills_the_busy_worker_and_the_next_call_starts_a_fresh_one(self):
        first = challenge_process.run("ping", timeout=20)
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(PowSearchCancelled):
            challenge_process.run("pow", "abc", 64, 60.0, 2_000_000_000, timeout=90, cancel_event=cancel)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(challenge_process.pids(), [], "a cancelled search must not keep running")
        self.assertNotEqual(challenge_process.run("ping", timeout=20), first)
        self.assertEqual(len(challenge_process.pids()), 1)

    def test_no_answer_in_time_kills_the_worker(self):
        challenge_process.run("ping", timeout=20)
        with self.assertRaises(challenge_process.ChallengeTimeout):
            challenge_process.run("pow", "abc", 64, 60.0, 2_000_000_000, timeout=0.5)
        self.assertEqual(challenge_process.pids(), [])

    def test_unknown_task_fails_at_once(self):
        started = time.monotonic()
        with self.assertRaisesRegex(challenge_process.ChallengeTaskError, "unknown task"):
            challenge_process.run("nope", timeout=20)
        self.assertLess(time.monotonic() - started, 15)

    def test_worker_exits_when_the_bot_side_of_the_pipe_closes(self):
        proc, channel = challenge_process._spawn(
            [sys.executable, str(challenge_process.WORKER_PATH), str(os.getpid())], dict(os.environ),
        )
        try:
            self.assertIn("ready", json.loads(proc.stdout.readline()))
            channel.close()  # what the OS does when the bot dies
            self.assertTrue(wait_until(lambda: proc.poll() is not None), "the worker outlived the bot")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            proc.stdout.close()

    def test_start_failure_falls_back_to_the_thread(self):
        with patch.object(challenge_process, "_spawn", side_effect=OSError("no python")), \
                patch.object(challenge_process, "_workers", []), patch.object(challenge_process, "_idle", None):
            with self.assertRaises(challenge_process.ChallengeProcessUnavailable):
                challenge_process.run("ping", timeout=5)
            with self.assertRaises(challenge_process.ChallengeProcessUnavailable):
                challenge_process.run("ping", timeout=5)  # no retry storm: in-thread for a while


class TestManagerUsesTheWorker(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(challenge_process, "ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pow_nonce_is_searched_in_the_worker(self):
        token = "e30." + __import__("base64").urlsafe_b64encode(json.dumps({"id": "abc", "compl": 2}).encode()).decode().rstrip("=") + ".sig"
        with patch.object(challenge_process, "run", return_value=17) as run:
            payload = manager._pow_verify_payload(token, timeout=5.0, max_iterations=1000, cancel_event=None)
        self.assertEqual(payload, {"challenge": token, "nonce": 17})
        self.assertEqual(run.call_args.args[:3], ("pow", "abc", 2))
        self.assertGreater(run.call_args.kwargs["timeout"], 5.0)

    def test_pow_without_a_worker_is_solved_in_the_thread(self):
        token = "e30." + __import__("base64").urlsafe_b64encode(json.dumps({"id": "abc", "compl": 1}).encode()).decode().rstrip("=") + ".sig"
        with patch.object(challenge_process, "run", side_effect=challenge_process.ChallengeProcessUnavailable("x")):
            payload = manager._pow_verify_payload(token, timeout=5.0, max_iterations=100000, cancel_event=None)
        self.assertTrue(hashlib.sha256(f"abc:{payload['nonce']}".encode()).hexdigest().startswith("0"))

    def avito_session(self):
        avito = Mock(name="avito_session", headers={"User-Agent": "UA/1.0"})
        avito.post.return_value = Mock(status_code=200, json=lambda: {"success": {"result": {"verified": True}}})
        return avito

    def test_geetest_is_solved_in_the_worker_and_verified_through_the_channel(self):
        avito = self.avito_session()
        seccode = {"lot_number": "lot-1", "pass_token": "t", "gen_time": "1", "captcha_output": "o"}
        page = Mock(text="const captchaId = 'abc';")
        with patch.object(manager, "GEETEST_DIRECT", True), \
                patch.object(challenge_process, "run", return_value=seccode) as run, \
                patch.object(manager.requests, "Session") as session_factory:
            ok = manager.solve_geetest_if_available(avito, page, "https://www.avito.ru/x", deadline=time.monotonic() + 15)
        self.assertTrue(ok)
        self.assertEqual(run.call_args.args, ("geetest", "abc", "UA/1.0", manager.BROWSER_PROFILE))
        self.assertLessEqual(run.call_args.kwargs["timeout"], 15)
        session_factory.assert_not_called()  # the bot's process opens no GeeTest session
        self.assertEqual(avito.post.call_args.args[0], manager.FIREWALL_CAPTCHA_VERIFY_URL)
        self.assertEqual(avito.post.call_args.kwargs["json"]["lot_number"], "lot-1")

    def test_geetest_is_skipped_when_the_scan_is_out_of_time(self):
        avito = self.avito_session()
        with patch.object(manager, "GEETEST_DIRECT", True), patch.object(challenge_process, "run") as run:
            ok = manager.solve_geetest_if_available(avito, Mock(text=""), "https://www.avito.ru/x", deadline=time.monotonic() - 1)
        self.assertFalse(ok)
        run.assert_not_called()
        avito.post.assert_not_called()

    def test_geetest_worker_failure_is_a_failed_challenge_not_a_crash(self):
        avito = self.avito_session()
        with patch.object(manager, "GEETEST_DIRECT", True), \
                patch.object(challenge_process, "run", side_effect=challenge_process.ChallengeWorkerDied("code -11")):
            self.assertFalse(manager.solve_geetest_if_available(avito, Mock(text=""), "https://www.avito.ru/x"))
        avito.post.assert_not_called()

    def test_worker_geetest_task_uses_its_own_session_and_closes_it(self):
        session = Mock(name="direct")
        with patch.object(geetest_solve, "direct_session", return_value=session) as factory, \
                patch.object(geetest_solve, "request_seccode", return_value={"lot_number": "x"}) as solve:
            self.assertEqual(challenge_worker._task_geetest("abc", "UA/1.0", "firefox147"), {"lot_number": "x"})
        factory.assert_called_once_with("UA/1.0", "firefox147")
        self.assertEqual(solve.call_args.args, (session, "abc"))
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
