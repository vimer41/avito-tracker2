#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

: "${DEPLOY_SHA:?DEPLOY_SHA is required}"
: "${DEPLOY_BRANCH:?DEPLOY_BRANCH is required}"
[[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid commit SHA"; exit 1; }
[[ "$DEPLOY_BRANCH" == main || "$DEPLOY_BRANCH" == feature/json-api ]] || { echo "Invalid deploy branch"; exit 1; }
DEPLOY_FULL_TESTS="${DEPLOY_FULL_TESTS:-1}"
[[ "$DEPLOY_FULL_TESTS" == 0 || "$DEPLOY_FULL_TESTS" == 1 ]] || { echo "DEPLOY_FULL_TESTS must be 0 or 1"; exit 1; }
DEPLOY_STOP_BEFORE_PREFLIGHT="${DEPLOY_STOP_BEFORE_PREFLIGHT:-0}"
[[ "$DEPLOY_STOP_BEFORE_PREFLIGHT" == 0 || "$DEPLOY_STOP_BEFORE_PREFLIGHT" == 1 ]] || { echo "DEPLOY_STOP_BEFORE_PREFLIGHT must be 0 or 1"; exit 1; }
DEPLOY_STARS_PAYMENTS_ENABLED="${DEPLOY_STARS_PAYMENTS_ENABLED:-}"
[[ -z "$DEPLOY_STARS_PAYMENTS_ENABLED" || "$DEPLOY_STARS_PAYMENTS_ENABLED" == 0 || "$DEPLOY_STARS_PAYMENTS_ENABLED" == 1 ]] || { echo "Invalid Stars enable flag"; exit 1; }
# Platega keys from GitHub secrets. Kept in non-exported shell variables, so tests, pip and the bot
# never inherit them; only configure_platega.py receives them. Both or none: one alone changes nothing.
PLATEGA_ID_VALUE="${DEPLOY_PLATEGA_MERCHANT_ID:-}"
PLATEGA_SECRET_VALUE="${DEPLOY_PLATEGA_SECRET:-}"
unset DEPLOY_PLATEGA_MERCHANT_ID DEPLOY_PLATEGA_SECRET
PLATEGA_KEYS=0
# Length is checked apart: some regex engines refuse a {8,256} bound (RE_DUP_MAX is 255 there).
PLATEGA_SAFE_VALUE='^[A-Za-z0-9._~+/=:-]+$'
if [[ -n "$PLATEGA_ID_VALUE" && -n "$PLATEGA_SECRET_VALUE" ]]; then
    for platega_value in "$PLATEGA_ID_VALUE" "$PLATEGA_SECRET_VALUE"; do
        [[ "$platega_value" =~ $PLATEGA_SAFE_VALUE ]] && (( ${#platega_value} >= 8 && ${#platega_value} <= 256 )) \
            || { echo "Invalid Platega secret format: expected 8-256 characters without spaces, quotes, # or \$"; exit 1; }
    done
    unset platega_value
    PLATEGA_KEYS=1
elif [[ -n "$PLATEGA_ID_VALUE" || -n "$PLATEGA_SECRET_VALUE" ]]; then
    echo "Only one Platega secret is set; runtime Platega keys left unchanged"
fi
DEPLOY_CLEANUP="${DEPLOY_CLEANUP:-1}"
[[ "$DEPLOY_CLEANUP" == 0 || "$DEPLOY_CLEANUP" == 1 ]] || { echo "DEPLOY_CLEANUP must be 0 or 1"; exit 1; }

TARGET_DIR=""
for candidate in "${DEPLOY_TARGET_DIR:-}" /opt/avito_parser_bot /opt/avito-bot; do
    if [[ -d "$candidate/.git" ]]; then
        TARGET_DIR="$candidate"
        break
    fi
done
[[ -n "$TARGET_DIR" ]] || { echo "Project directory not found"; exit 1; }
cd "$TARGET_DIR"

# Prevent overlapping remote installs/restarts, including manually started workflows.
exec 9>"$TARGET_DIR/.deploy.lock"
flock -w 900 9 || { echo "Deploy lock timeout"; exit 1; }
git diff --quiet && git diff --cached --quiet || { echo "Tracked server changes found; refusing overwrite"; exit 1; }
git fetch --no-tags origin "$DEPLOY_BRANCH"
git cat-file -e "$DEPLOY_SHA^{commit}"
git merge-base --is-ancestor "$DEPLOY_SHA" FETCH_HEAD || { echo "Commit is not on requested branch"; exit 1; }
[[ "$(git rev-parse FETCH_HEAD)" == "$DEPLOY_SHA" ]] || { echo "Newer branch commit found; skipping stale deploy"; exit 0; }

PREVIOUS_SHA="$(git rev-parse HEAD)"
PREVIOUS_BRANCH="$(git symbolic-ref --short -q HEAD || true)"
NEW_ENV=""
STAGE_DIR="$(mktemp -d "$TARGET_DIR/.stage-$DEPLOY_SHA-XXXXXX")"
ENV_CHANGED=0
PREVIOUS_ENV=""
WAS_ACTIVE=0
STOPPED_EARLY=0
CHANGED=0
ENV_CONFIG_CHANGED=0
HAD_ENV_FILE=0
systemctl is-active --quiet avito-bot && WAS_ACTIVE=1

cleanup() {
    rm -rf "$STAGE_DIR" "$TARGET_DIR/.tmp" "$TARGET_DIR"/.stage-* 2>/dev/null || true
    # Failed preflight environments were never used by the service.
    if [[ -n "$NEW_ENV" && "$ENV_CHANGED" == 0 ]]; then
        rm -rf "$NEW_ENV"
    fi
}

report_state() {
    local current_sha service_active=0
    current_sha="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    if systemctl is-active --quiet avito-bot; then
        service_active=1
    fi
    echo "Deployment state: commit=$current_sha service_active=$service_active environment_changed=$ENV_CHANGED"
}

rollback() {
    local status="${1:-$?}"
    trap - ERR INT TERM
    rm -rf "$TARGET_DIR/.tmp" 2>/dev/null || true
    if [[ "$CHANGED" == 1 ]]; then
        echo "Deploy failed; restoring previous code/environment"
        systemctl stop avito-bot || true
        if [[ -n "$PREVIOUS_BRANCH" ]]; then
            git checkout -B "$PREVIOUS_BRANCH" "$PREVIOUS_SHA" || true
        else
            git checkout --detach "$PREVIOUS_SHA" || true
        fi
        if [[ "$ENV_CHANGED" == 1 ]]; then
            rm -f "$TARGET_DIR/venv"
            if [[ -n "$PREVIOUS_ENV" ]]; then
                mv "$PREVIOUS_ENV" "$TARGET_DIR/venv" || true
            fi
        fi
        if [[ "$ENV_CONFIG_CHANGED" == 1 ]]; then
            if [[ "$HAD_ENV_FILE" == 1 ]]; then
                cp -p "$STAGE_DIR/runtime.env.before" "$TARGET_DIR/.env" || true
            else
                rm -f "$TARGET_DIR/.env"
            fi
        fi
        if [[ "$WAS_ACTIVE" == 1 ]]; then
            systemctl start avito-bot || true
        fi
    else
        echo "Deploy failed before activation (exit $status); running code/environment were not changed"
        if [[ "$STOPPED_EARLY" == 1 && "$WAS_ACTIVE" == 1 ]]; then
            echo "Restarting original bot after preflight failure"
            systemctl start avito-bot || true
        fi
    fi
    report_state
    exit "$status"
}
trap cleanup EXIT
trap rollback ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM

# Clean up temporary pip build artifacts, old logs, stale environments and VPS cache to free disk space
if [[ -z "${MOCK_ROOT:-}" ]]; then
    echo "=== Initial Disk & Memory Status ==="
    df -h "$TARGET_DIR" / /tmp 2>/dev/null || df -h "$TARGET_DIR" || true
    free -m 2>/dev/null || true

    # Clean package manager cache and systemd journals
    command -v apt-get >/dev/null 2>&1 && apt-get clean 2>/dev/null || true
    command -v journalctl >/dev/null 2>&1 && journalctl --vacuum-size=20M --vacuum-time=2d 2>/dev/null || true

    # Clean pip caches and system temp directories
    rm -rf /root/.cache/pip ~/.cache/pip /tmp/pip-* /tmp/*.whl /var/tmp/pip-* "$TARGET_DIR/.tmp" 2>/dev/null || true
    find "$TARGET_DIR/logs" "$TARGET_DIR" -maxdepth 2 -name "*.log.*" -mtime +2 -delete 2>/dev/null || true

    # Prune old releases in .releases to free disk BEFORE building NEW_ENV:
    # Always keep the active venv and at most 1 rollback candidate (newest previous-venv).
    if [[ -d "$TARGET_DIR/.releases" ]]; then
        ACTIVE_VENV=""
        if [[ -e "$TARGET_DIR/venv" || -L "$TARGET_DIR/venv" ]]; then
            ACTIVE_VENV="$(readlink -f "$TARGET_DIR/venv" 2>/dev/null || true)"
        fi
        NEWEST_PREV=""
        for prev_env in $(ls -dt "$TARGET_DIR/.releases"/previous-venv-* 2>/dev/null); do
            prev_real="$(readlink -f "$prev_env" 2>/dev/null || true)"
            if [[ -z "$NEWEST_PREV" && -d "$prev_env" ]]; then
                NEWEST_PREV="$prev_real"
            elif [[ -d "$prev_env" && -n "$prev_real" && "$prev_real" != "$ACTIVE_VENV" ]]; then
                echo "Pre-clean: removing older previous-venv $(basename "$prev_env")"
                rm -rf "$prev_env" 2>/dev/null || true
            fi
        done
        for old_v in $(ls -dt "$TARGET_DIR/.releases"/venv-* 2>/dev/null); do
            v_real="$(readlink -f "$old_v" 2>/dev/null || true)"
            if [[ -n "$v_real" && "$v_real" != "$ACTIVE_VENV" && "$v_real" != "$NEWEST_PREV" ]]; then
                echo "Pre-clean: removing stale release environment $(basename "$old_v")"
                rm -rf "$old_v" 2>/dev/null || true
            fi
        done
    fi

    # Prune old SQLite backups in backups/ (keep at most 3 newest)
    if [[ -d "$TARGET_DIR/backups" ]]; then
        ls -dt "$TARGET_DIR/backups"/bot-before-*.db 2>/dev/null | tail -n +4 | xargs -r rm -f 2>/dev/null || true
    fi

    echo "=== Disk Status After Pre-Deploy Cleanup ==="
    df -h "$TARGET_DIR" / /tmp 2>/dev/null || df -h "$TARGET_DIR" || true
fi

# Use dedicated on-disk temporary directory for pip to avoid filling RAM-backed /tmp tmpfs
BUILD_TMP="$TARGET_DIR/.tmp"
rm -rf "$BUILD_TMP" 2>/dev/null || true
mkdir -p "$BUILD_TMP"
export TMPDIR="$BUILD_TMP"

# Free RAM on constrained systems (e.g. 600MB VPS) by stopping old bot before pip install
if [[ "$DEPLOY_STOP_BEFORE_PREFLIGHT" == 1 && "$WAS_ACTIVE" == 1 ]]; then
    echo "Stopping existing bot before preflight to free RAM (DEPLOY_STOP_BEFORE_PREFLIGHT=1)"
    systemctl stop avito-bot || true
    STOPPED_EARLY=1
fi

# Build and validate in isolation.
report_state
git archive "$DEPLOY_SHA" | tar -x -C "$STAGE_DIR"
mkdir -p "$TARGET_DIR/.releases"
NEW_ENV="$(mktemp -d "$TARGET_DIR/.releases/venv-$DEPLOY_SHA-XXXXXX")"
python3.11 -m venv "$NEW_ENV" 2>/dev/null || python3 -m venv "$NEW_ENV"
"$NEW_ENV/bin/python" -B -c 'import sys; print("Deploy Python:", sys.version.split()[0], flush=True); sys.exit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) else "Unsupported deploy Python; expected 3.11 or 3.12")'

# Seed site-packages from existing active venv using hardlinks (zero disk space, instant)
if [[ -e "$TARGET_DIR/venv" || -L "$TARGET_DIR/venv" ]]; then
    ACTIVE_SRC="$(readlink -f "$TARGET_DIR/venv" 2>/dev/null || true)"
    if [[ -d "$ACTIVE_SRC/lib" ]]; then
        for sp in "$ACTIVE_SRC"/lib/python3.*/site-packages; do
            if [[ -d "$sp" ]]; then
                for dst_sp in "$NEW_ENV"/lib/python3.*/site-packages; do
                    if [[ -d "$dst_sp" ]]; then
                        cp -al "$sp"/* "$dst_sp"/ 2>/dev/null || cp -r "$sp"/* "$dst_sp"/ 2>/dev/null || true
                    fi
                done
            fi
        done
    fi
fi

"$NEW_ENV/bin/python" -m pip install --no-cache-dir --disable-pip-version-check -r "$STAGE_DIR/requirements.txt"
rm -rf "$BUILD_TMP" 2>/dev/null || true
"$NEW_ENV/bin/python" -m pip check
"$NEW_ENV/bin/python" -B "$STAGE_DIR/tools/check_syntax.py"
echo "Resources before activation:"
if command -v free >/dev/null 2>&1; then free -m || true; fi
if [[ "$DEPLOY_FULL_TESTS" == 1 ]]; then
    echo "Running offline tests: timeout=600s, native_threads=1"
    AVITO_RADAR_TESTING=1 PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
        timeout --kill-after=30s 600s "$NEW_ENV/bin/python" -B "$STAGE_DIR/tools/run_offline_tests.py"
else
    echo "Minimal VPS checks passed: dependencies and syntax; full offline suite skipped (DEPLOY_FULL_TESTS=0)"
fi

# No user deletion or log scrubbing. Backup is read-only and precedes migrations.
if [[ -n "$DEPLOY_STARS_PAYMENTS_ENABLED" || "$PLATEGA_KEYS" == 1 ]]; then
    [[ ! -L "$TARGET_DIR/.env" ]] || { echo "Symlinked runtime .env cannot be changed"; exit 1; }
    if [[ -f "$TARGET_DIR/.env" ]]; then
        cp -p "$TARGET_DIR/.env" "$STAGE_DIR/runtime.env.before"
        HAD_ENV_FILE=1
    fi
fi
CHANGED=1
if [[ "$STOPPED_EARLY" == 0 ]]; then
    systemctl stop avito-bot
fi
mkdir -p "$TARGET_DIR/backups"
BACKUP_PATH="$TARGET_DIR/backups/bot-before-$DEPLOY_SHA-$(date -u +%Y%m%dT%H%M%SZ).db"
export BACKUP_PATH TARGET_DIR
PYTHONPATH="$STAGE_DIR" "$NEW_ENV/bin/python" -B - <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.environ['TARGET_DIR'])
import config

database_path = Path(config.DB_PATH)
if database_path.is_file():
    with sqlite3.connect(database_path.resolve().as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(os.environ['BACKUP_PATH']) as destination:
            source.backup(destination)
    print('SQLite backup created')
else:
    print('No existing database; first deployment')
PY

git checkout -B "$DEPLOY_BRANCH" "$DEPLOY_SHA"
if [[ -e "$TARGET_DIR/venv" || -L "$TARGET_DIR/venv" ]]; then
    PREVIOUS_ENV="$TARGET_DIR/.releases/previous-venv-$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$TARGET_DIR/venv" "$PREVIOUS_ENV"
fi
ENV_CHANGED=1
ln -s "$NEW_ENV" "$TARGET_DIR/venv"
if [[ -n "$DEPLOY_STARS_PAYMENTS_ENABLED" ]]; then
    ENV_CONFIG_CHANGED=1
    "$NEW_ENV/bin/python" -B "$STAGE_DIR/tools/configure_stars.py" --env-file "$TARGET_DIR/.env" \
        --enabled "$DEPLOY_STARS_PAYMENTS_ENABLED" --standart "${DEPLOY_STARS_PRICE_STANDART:-170}" --pro "${DEPLOY_STARS_PRICE_PRO:-470}"
fi
if [[ "$PLATEGA_KEYS" == 1 ]]; then
    ENV_CONFIG_CHANGED=1
    PLATEGA_MERCHANT_ID="$PLATEGA_ID_VALUE" PLATEGA_SECRET="$PLATEGA_SECRET_VALUE" \
        "$NEW_ENV/bin/python" -B "$STAGE_DIR/tools/configure_platega.py" --env-file "$TARGET_DIR/.env"
fi
if [[ -z "${MOCK_ROOT:-}" ]]; then
    systemctl enable avito-bot 2>/dev/null || true
fi
systemctl reset-failed avito-bot
systemctl start avito-bot

# A successful process start alone is not a successful deployment.
for _ in $(seq 1 10); do
    sleep 2
    systemctl is-active --quiet avito-bot
    [[ "$(systemctl show avito-bot --property=NRestarts --value)" == 0 ]]
done
[[ "$(git rev-parse HEAD)" == "$DEPLOY_SHA" ]]
report_state
echo "Deployed tested commit $DEPLOY_SHA; service stayed active for 20 seconds"

# Retention runs only after validation. Active/rollback environments and this
# deployment's SQLite backup are pinned; failure never triggers a late rollback.
if [[ "$DEPLOY_CLEANUP" == 1 ]]; then
    if ! "$NEW_ENV/bin/python" -B "$STAGE_DIR/tools/cleanup_releases.py" --target "$TARGET_DIR" --apply \
        --keep-releases "${DEPLOY_KEEP_RELEASES:-4}" --keep-backups "${DEPLOY_KEEP_BACKUPS:-7}" \
        --release-age-days "${DEPLOY_RELEASE_MIN_AGE_DAYS:-7}" --backup-age-days "${DEPLOY_BACKUP_MIN_AGE_DAYS:-14}" \
        --protect "$NEW_ENV" --protect "${PREVIOUS_ENV:-$NEW_ENV}" --protect "$BACKUP_PATH"; then
        echo "Retention skipped/failed safely; deployed service remains active"
    fi
fi
