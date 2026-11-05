"""Execute the deploy script with fake Git/systemd/Python, never touching a VPS."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import _environment

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name != "nt" else next((str(path) for path in (Path("C:/Program Files/Git/bin/bash.exe"), Path("C:/Program Files/Git/usr/bin/bash.exe")) if path.exists()), None)
TEST_SHA = "a" * 40
OLD_SHA = "b" * 40


def bash_path(path):
    value = Path(path).resolve().as_posix()
    return "/" + value[0].lower() + value[2:] if os.name == "nt" else value


@unittest.skipUnless(BASH, "Bash is needed for mocked deploy execution")
class TestDeployScript(unittest.TestCase):
    def create_script(self, directory, name, body):
        path = directory / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def run_deploy(self, *, fail_tests=False, fail_service=False, stale=False,
                   posix_shell=False, test_timeout=False, python_fallback=False,
                   unsupported_python=False, full_tests=None, preflight_failure="", stars_enabled=None,
                   stop_before_preflight=None, platega=None):
        with tempfile.TemporaryDirectory(prefix="avito-deploy-test-") as temporary:
            directory = Path(temporary)
            project = directory / "project"
            project.mkdir()
            (project / ".git").mkdir()
            (project / ".env").write_text("BOT_TOKEN=fixture-secret\nSTARS_PAYMENTS_ENABLED=0\n", encoding="utf-8")
            (project / "venv").mkdir()
            (project / "venv" / "old-environment-marker").write_text("original", encoding="utf-8")
            (directory / "head").write_text(OLD_SHA, encoding="utf-8")
            binary = directory / "bin"
            binary.mkdir()
            self.create_script(binary, "git", """
echo "git $*" >> "$MOCK_ROOT/events"
case "$1" in
  diff|fetch|cat-file|merge-base) exit 0 ;;
  symbolic-ref) echo feature/json-api ;;
  rev-parse)
    if [[ "$2" == FETCH_HEAD ]]; then
      if [[ "$STALE" == 1 ]]; then echo "$OLD_SHA"; else echo "$DEPLOY_SHA"; fi
    else cat "$MOCK_ROOT/head"; fi ;;
  archive) exit 0 ;;
  checkout)
    for arg in "$@"; do last="$arg"; done
    echo "$last" > "$MOCK_ROOT/head" ;;
  *) echo "Unexpected git command" >&2; exit 91 ;;
esac
""")
            self.create_script(binary, "systemctl", """
echo "systemctl $*" >> "$MOCK_ROOT/events"
case "$1" in
  is-active)
    if [[ -f "$MOCK_ROOT/new-start" && "$FAIL_SERVICE" == 1 ]]; then exit 1; fi ;;
  stop) rm -f "$MOCK_ROOT/new-start" ;;
  reset-failed) true ;;
  start)
    if [[ "$(cat "$MOCK_ROOT/head")" == "$DEPLOY_SHA" ]]; then touch "$MOCK_ROOT/new-start"; fi ;;
  show) echo 0 ;;
  *) exit 92 ;;
esac
""")
            self.create_script(binary, "tar", "cat >/dev/null\n")
            self.create_script(binary, "flock", "exit 0\n")
            self.create_script(binary, "sleep", "exit 0\n")
            # A regular link marker avoids Git Bash's Windows directory-copy fallback.
            # Fake systemd never executes venv; rollback only needs unlink/move semantics.
            self.create_script(binary, "ln", "printf '%s\\n' \"$2\" > \"$3\"\n")
            self.create_script(binary, "timeout", """
echo "timeout $*" >> "$MOCK_ROOT/events"
if [[ "$1" == --kill-after=* ]]; then shift; fi
shift
exec "$@"
""")
            build_environment = """
echo "build-env $*" >> "$MOCK_ROOT/events"
mkdir -p "$3/bin"
cp "$MOCK_ROOT/bin/python-template" "$3/bin/python"
chmod +x "$3/bin/python"
"""
            self.create_script(binary, "python3.11", 'if [[ "$PYTHON_FALLBACK" == 1 ]]; then exit 127; fi\n' + build_environment)
            self.create_script(binary, "python3", build_environment)
            self.create_script(binary, "python-template", """
echo "python $*" >> "$MOCK_ROOT/events"
if [[ "$*" == *"Deploy Python:"* ]]; then
  if [[ "$UNSUPPORTED_PYTHON" == 1 ]]; then
    echo "Unsupported deploy Python; expected 3.11 or 3.12" >&2
    exit 1
  fi
  echo "Deploy Python: $MOCK_PYTHON_VERSION"
fi
if [[ -n "$PREFLIGHT_FAILURE" && "$*" == *"$PREFLIGHT_FAILURE"* ]]; then exit 19; fi
if [[ "$*" == *run_offline_tests.py* ]]; then
  echo "test-runtime testing=$AVITO_RADAR_TESTING no_pyc=$PYTHONDONTWRITEBYTECODE openblas=$OPENBLAS_NUM_THREADS omp=$OMP_NUM_THREADS mkl=$MKL_NUM_THREADS" >> "$MOCK_ROOT/events"
  echo "test-sees-platega=${DEPLOY_PLATEGA_SECRET:-}${PLATEGA_SECRET:-}" >> "$MOCK_ROOT/events"
  exit "$TEST_EXIT_CODE"
fi
if [[ "$*" == *configure_stars.py* ]]; then
  echo "BOT_TOKEN=fixture-secret" > "$DEPLOY_TARGET_DIR/.env"
  echo "STARS_PAYMENTS_ENABLED=$DEPLOY_STARS_PAYMENTS_ENABLED" >> "$DEPLOY_TARGET_DIR/.env"
fi
if [[ "$*" == *configure_platega.py* ]]; then
  echo "PLATEGA_MERCHANT_ID=$PLATEGA_MERCHANT_ID" >> "$DEPLOY_TARGET_DIR/.env"
  echo "platega-secret-received=${PLATEGA_SECRET:+yes}" >> "$MOCK_ROOT/events"
fi
if [[ "$*" == *cleanup_releases.py* ]]; then
  echo "retention-executed" >> "$MOCK_ROOT/events"
fi
if [[ "$*" == "-B -" ]]; then cat >/dev/null; echo backup-created >> "$MOCK_ROOT/events"; fi
""")
            prefix = bash_path(binary)
            env = dict(os.environ)
            env.update({
                "MOCK_ROOT": bash_path(directory), "DEPLOY_TARGET_DIR": bash_path(project),
                "DEPLOY_SHA": TEST_SHA, "OLD_SHA": OLD_SHA, "DEPLOY_BRANCH": "feature/json-api",
                "TEST_EXIT_CODE": "124" if test_timeout else "17" if fail_tests else "0",
                "FAIL_SERVICE": "1" if fail_service else "0",
                "PYTHON_FALLBACK": "1" if python_fallback else "0",
                "UNSUPPORTED_PYTHON": "1" if unsupported_python else "0",
                "MOCK_PYTHON_VERSION": "3.12.5" if python_fallback else "3.11.15",
                "STALE": "1" if stale else "0",
                "PREFLIGHT_FAILURE": preflight_failure,
            })
            if full_tests is None:
                env.pop("DEPLOY_FULL_TESTS", None)
            else:
                env["DEPLOY_FULL_TESTS"] = full_tests
            env.pop("DEPLOY_STOP_BEFORE_PREFLIGHT", None)
            if stop_before_preflight is not None:
                env["DEPLOY_STOP_BEFORE_PREFLIGHT"] = stop_before_preflight
            env.pop("DEPLOY_STARS_PAYMENTS_ENABLED", None)
            if stars_enabled is not None:
                env["DEPLOY_STARS_PAYMENTS_ENABLED"] = stars_enabled
            for name in ("DEPLOY_PLATEGA_MERCHANT_ID", "DEPLOY_PLATEGA_SECRET", "PLATEGA_MERCHANT_ID", "PLATEGA_SECRET"):
                env.pop(name, None)
            if platega is not None:
                env["DEPLOY_PLATEGA_MERCHANT_ID"], env["DEPLOY_PLATEGA_SECRET"] = platega
            if posix_shell:
                wrapper = directory / "wrapper.sh"
                wrapper.write_text("bash -s <<'AVITO_DEPLOY_SCRIPT'\n" + (ROOT / "tools/deploy.sh").read_text(encoding="utf-8") + "\nAVITO_DEPLOY_SCRIPT\n", encoding="utf-8", newline="\n")
                command = f"export PATH='{prefix}':$PATH; sh '{bash_path(wrapper)}'"
            else:
                command = f"export PATH='{prefix}':$PATH; bash '{bash_path(ROOT / 'tools/deploy.sh')}'"
            result = subprocess.run([BASH, "-c", command], env=env, capture_output=True, text=True, timeout=15)
            head = (directory / "head").read_text(encoding="utf-8").strip()
            events_path = directory / "events"
            events = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
            events += "\nruntime-env:" + (project / ".env").read_text(encoding="utf-8")
            old_environment = (project / "venv" / "old-environment-marker").is_file()
            release_count = len(list((project / ".releases").glob("venv-*")))
            return result, head, events, old_environment, release_count

    def test_success_deploys_exact_sha_after_tests_and_backup(self):
        result, head, events, old_environment, release_count = self.run_deploy(full_tests="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, TEST_SHA)
        self.assertFalse(old_environment)
        self.assertEqual(release_count, 1)
        self.assertIn("timeout --kill-after=30s 600s", events)
        self.assertIn("test-runtime testing=1 no_pyc=1 openblas=1 omp=1 mkl=1", events)
        self.assertIn("Deploy Python: 3.11.15", result.stdout)
        self.assertLess(events.index("run_offline_tests.py"), events.index("systemctl stop"))
        self.assertLess(events.index("backup-created"), events.index("git checkout"))
        self.assertEqual(events.count("systemctl show"), 10)

    def test_test_failure_never_stops_running_bot(self):
        result, head, events, old_environment, release_count = self.run_deploy(fail_tests=True)
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertEqual(head, OLD_SHA)
        self.assertTrue(old_environment)
        self.assertEqual(release_count, 0)
        self.assertIn("Deploy failed before activation (exit 17)", result.stdout)
        self.assertNotIn("systemctl stop", events)
        self.assertNotIn("git checkout", events)

    def test_failed_service_restores_code_and_old_environment(self):
        result, head, events, old_environment, release_count = self.run_deploy(fail_service=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(head, OLD_SHA)
        self.assertTrue(old_environment, result.stdout + result.stderr + events)
        self.assertEqual(release_count, 1)
        self.assertIn(f"git checkout -B feature/json-api {OLD_SHA}", events)

    def test_stale_workflow_skips_deploy(self):
        result, head, events, old_environment, release_count = self.run_deploy(stale=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, OLD_SHA)
        self.assertTrue(old_environment)
        self.assertEqual(release_count, 0)
        self.assertNotIn("build-env", events)
        self.assertNotIn("systemctl stop", events)

    def test_ssh_wrapper_runs_under_a_posix_login_shell(self):
        result, head, events, old_environment, release_count = self.run_deploy(posix_shell=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, TEST_SHA)
        self.assertIn("backup-created", events)

    def test_test_timeout_preserves_running_version_and_removes_unused_env(self):
        result, head, events, old_environment, release_count = self.run_deploy(test_timeout=True)
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertEqual(head, OLD_SHA)
        self.assertTrue(old_environment)
        self.assertEqual(release_count, 0)
        self.assertNotIn("systemctl stop", events)
        self.assertNotIn("git checkout", events)
        self.assertIn("Deploy failed before activation (exit 124)", result.stdout)
        self.assertIn(f"Deployment state: commit={OLD_SHA} service_active=1 environment_changed=0", result.stdout)

    def test_python_312_fallback_is_reported_and_retested(self):
        result, head, events, old_environment, release_count = self.run_deploy(python_fallback=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, TEST_SHA)
        self.assertIn("Deploy Python: 3.12.5", result.stdout)
        self.assertIn("run_offline_tests.py", events)
        self.assertFalse(old_environment)
        self.assertEqual(release_count, 1)

    def test_unsupported_python_fails_before_install_without_stopping_bot(self):
        result, head, events, old_environment, release_count = self.run_deploy(unsupported_python=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(head, OLD_SHA)
        self.assertTrue(old_environment)
        self.assertEqual(release_count, 0)
        self.assertIn("Unsupported deploy Python", result.stderr)
        self.assertNotIn("pip install", events)
        self.assertNotIn("systemctl stop", events)

    def test_minimal_checks_skip_suite_but_keep_backup_and_service_checks(self):
        result, head, events, old_environment, release_count = self.run_deploy(full_tests="0", fail_tests=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, TEST_SHA)
        self.assertFalse(old_environment)
        self.assertEqual(release_count, 1)
        self.assertIn("pip check", events)
        self.assertIn("check_syntax.py", events)
        self.assertNotIn("run_offline_tests.py", events)
        self.assertNotIn("timeout ", events)
        self.assertIn("full offline suite skipped (DEPLOY_FULL_TESTS=0)", result.stdout)
        self.assertLess(events.index("pip check"), events.index("backup-created"))
        self.assertLess(events.index("check_syntax.py"), events.index("backup-created"))
        self.assertLess(events.index("backup-created"), events.index("git checkout"))
        self.assertEqual(events.count("systemctl show"), 10)

    def test_minimal_check_failure_never_stops_running_bot(self):
        for check in ("pip check", "check_syntax.py"):
            with self.subTest(check=check):
                result, head, events, old_environment, release_count = self.run_deploy(full_tests="0", preflight_failure=check)
                self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
                self.assertEqual(head, OLD_SHA)
                self.assertTrue(old_environment)
                self.assertEqual(release_count, 0)
                self.assertNotIn("systemctl stop", events)
                self.assertNotIn("git checkout", events)

    def test_minimal_service_failure_restores_code_and_old_environment(self):
        result, head, events, old_environment, release_count = self.run_deploy(full_tests="0", fail_service=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, OLD_SHA)
        self.assertTrue(old_environment)
        self.assertEqual(release_count, 1)
        self.assertIn("backup-created", events)
        self.assertIn(f"git checkout -B feature/json-api {OLD_SHA}", events)
        self.assertNotIn("run_offline_tests.py", events)

    def test_invalid_test_mode_fails_before_install_or_activation(self):
        result, head, events, old_environment, release_count = self.run_deploy(full_tests="invalid")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(head, OLD_SHA)
        self.assertTrue(old_environment)
        self.assertEqual(release_count, 0)
        self.assertIn("DEPLOY_FULL_TESTS must be 0 or 1", result.stdout)
        self.assertNotIn("build-env", events)
        self.assertNotIn("systemctl stop", events)

    def test_stars_runtime_setting_is_applied_after_backup_and_before_start(self):
        result, head, events, _, _ = self.run_deploy(full_tests="0", stars_enabled="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, TEST_SHA)
        self.assertLess(events.index("backup-created"), events.index("configure_stars.py"))
        self.assertLess(events.index("configure_stars.py"), events.index("systemctl start"))
        self.assertIn("runtime-env:BOT_TOKEN=fixture-secret\nSTARS_PAYMENTS_ENABLED=1", events)
        self.assertGreater(events.index("retention-executed"), events.rindex("systemctl show"))

    def test_failed_stars_activation_restores_previous_runtime_setting(self):
        result, head, events, _, _ = self.run_deploy(full_tests="0", stars_enabled="1", fail_service=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(head, OLD_SHA)
        self.assertIn("runtime-env:BOT_TOKEN=fixture-secret\nSTARS_PAYMENTS_ENABLED=0", events)
        self.assertNotIn("retention-executed", events)

    def test_invalid_stars_setting_never_stops_running_bot(self):
        result, head, events, _, _ = self.run_deploy(full_tests="0", stars_enabled="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(head, OLD_SHA)
        self.assertNotIn("systemctl stop", events)

    def test_platega_keys_reach_only_the_runtime_env(self):
        result, head, events, _, _ = self.run_deploy(full_tests="1", platega=("merchant-uuid-1234", "secretvalue12345"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, TEST_SHA)
        self.assertLess(events.index("backup-created"), events.index("configure_platega.py"))
        self.assertLess(events.index("configure_platega.py"), events.index("systemctl start"))
        self.assertIn("platega-secret-received=yes", events)
        self.assertIn("test-sees-platega=\n", events, "tests and pip never inherit the keys")
        self.assertIn("PLATEGA_MERCHANT_ID=merchant-uuid-1234", events.split("runtime-env:")[1])
        self.assertNotIn("secretvalue12345", result.stdout + result.stderr + events)

    def test_one_platega_secret_alone_changes_nothing(self):
        result, head, events, _, _ = self.run_deploy(full_tests="0", platega=("merchant-uuid-1234", ""))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("configure_platega.py", events)
        self.assertIn("Only one Platega secret is set", result.stdout)
        self.assertIn("runtime-env:BOT_TOKEN=fixture-secret\nSTARS_PAYMENTS_ENABLED=0\n", events)

    def test_invalid_platega_secret_never_stops_running_bot(self):
        result, head, events, _, _ = self.run_deploy(full_tests="0", platega=("merchant id", "secret'value123"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(head, OLD_SHA)
        self.assertNotIn("build-env", events)
        self.assertNotIn("systemctl stop", events)
        self.assertIn("Invalid Platega secret format", result.stdout)
        self.assertNotIn("secret'value123", result.stdout + result.stderr)

    def test_failed_activation_restores_the_env_without_new_keys(self):
        result, head, events, _, _ = self.run_deploy(full_tests="0", fail_service=True,
                                                     platega=("merchant-uuid-1234", "secretvalue12345"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(head, OLD_SHA)
        self.assertIn("configure_platega.py", events)
        self.assertEqual(events.split("runtime-env:")[1], "BOT_TOKEN=fixture-secret\nSTARS_PAYMENTS_ENABLED=0\n")

    def test_stop_before_preflight_stops_service_early_and_deploys_successfully(self):
        result, head, events, old_environment, release_count = self.run_deploy(full_tests="0", stop_before_preflight="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, TEST_SHA)
        self.assertFalse(old_environment)
        self.assertEqual(release_count, 1)
        self.assertIn("Stopping existing bot before preflight to free RAM", result.stdout)
        self.assertLess(events.index("systemctl stop"), events.index("build-env"))

    def test_stop_before_preflight_restarts_service_on_preflight_failure(self):
        result, head, events, old_environment, release_count = self.run_deploy(
            full_tests="0", stop_before_preflight="1", preflight_failure="pip check"
        )
        self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
        self.assertEqual(head, OLD_SHA)
        self.assertTrue(old_environment)
        self.assertEqual(release_count, 0)
        self.assertIn("Stopping existing bot before preflight to free RAM", result.stdout)
        self.assertIn("Restarting original bot after preflight failure", result.stdout)
        self.assertLess(events.index("systemctl stop"), events.index("pip check"))
        self.assertLess(events.index("pip check"), events.index("systemctl start"))

    def test_invalid_stop_before_preflight_setting_fails_validation(self):
        result, head, events, _, _ = self.run_deploy(stop_before_preflight="invalid")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(head, OLD_SHA)
        self.assertIn("DEPLOY_STOP_BEFORE_PREFLIGHT must be 0 or 1", result.stdout)
        self.assertNotIn("systemctl stop", events)


class TestConfigurePlatega(unittest.TestCase):
    MERCHANT = "32d0abf7-fb7e-48f2-87b4-306c426a84a4"

    def test_keys_replace_old_lines_and_keep_everything_else(self):
        from tools.configure_platega import configure
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("BOT_TOKEN=x\nexport PLATEGA_SECRET=old-value-1\nPROXY_URL=y\n", encoding="utf-8")
            values = {"PLATEGA_MERCHANT_ID": self.MERCHANT, "PLATEGA_SECRET": "a1" * 50}
            self.assertTrue(configure(env_file, values))
            self.assertEqual(env_file.read_text(encoding="utf-8"),
                             f"BOT_TOKEN=x\nPROXY_URL=y\nPLATEGA_MERCHANT_ID={self.MERCHANT}\nPLATEGA_SECRET={'a1' * 50}\n")
            self.assertFalse(configure(env_file, values), "the same keys do not rewrite the file")

    def test_unsafe_values_are_rejected_and_the_file_is_untouched(self):
        from tools.configure_platega import configure
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("BOT_TOKEN=x\n", encoding="utf-8")
            for bad in ("", "short", "with space 123", "quote'value12", "dollar$value12", "hash#value1234", "line\nbreak123"):
                with self.assertRaises(ValueError):
                    configure(env_file, {"PLATEGA_MERCHANT_ID": self.MERCHANT, "PLATEGA_SECRET": bad})
            self.assertEqual(env_file.read_text(encoding="utf-8"), "BOT_TOKEN=x\n")


if __name__ == "__main__":
    unittest.main()
