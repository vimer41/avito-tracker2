"""Shared test bootstrap: no .env, production SQLite, cookies or logs."""

import atexit
import os
import sys
import tempfile
from pathlib import Path

# Set these before discovery imports NumPy/ONNX. VPS retests share resources
# with the running bot; native libraries must not size pools for the host CPU.
NATIVE_THREAD_LIMITS = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
}
os.environ.update(NATIVE_THREAD_LIMITS)
# Older tests assert the emoji theme's texts; theme tests patch config.UI_THEME themselves.
os.environ.setdefault("UI_THEME", "emoji")

_ORIG_TEMPDIR_CLEANUP = tempfile.TemporaryDirectory.cleanup


def _safe_tempdir_cleanup(self) -> None:
    db_mod = sys.modules.get("database")
    if db_mod is not None and hasattr(db_mod, "close_db_sync"):
        try:
            db_mod.close_db_sync(self.name)
        except Exception:
            pass
    _ORIG_TEMPDIR_CLEANUP(self)


tempfile.TemporaryDirectory.cleanup = _safe_tempdir_cleanup

TEST_ROOT = tempfile.TemporaryDirectory(prefix="avito-radar-tests-")
atexit.register(TEST_ROOT.cleanup)
root = Path(TEST_ROOT.name)
os.environ.update({
    "AVITO_RADAR_TESTING": "1",
    "ALLOW_DIRECT_AVITO": "1",
    "BOT_TOKEN": "1234567890:TEST_TOKEN_FOR_CI_CD_MOCK",
    "ADMIN_ID": "986832959",
    "ADMIN_IDS": "986832959,455386428",
    "DATA_DIR": str(root / "data"),
    "DB_PATH": str(root / "test.db"),
    "BACKUP_DIR": str(root / "backups"),
    "LOGS_DIR": str(root / "logs"),
    "COMBINED_LOG": str(root / "bot.log"),
    "PROXY_URL": "",
    "STARS_PAYMENTS_ENABLED": "0",
    "STARS_PRICE_BASIC": "199",
    "STARS_PRICE_MEDIUM": "449",
    "STARS_PRICE_FULL": "899",
})
