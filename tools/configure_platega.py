"""Atomically write the Platega keys into a runtime .env during a deploy.

The values come from the environment (PLATEGA_MERCHANT_ID, PLATEGA_SECRET), never from the command
line, so they do not show up in the process list. Nothing prints a value.
"""

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

KEYS = ("PLATEGA_MERCHANT_ID", "PLATEGA_SECRET")
LINE = re.compile(r"^\s*(?:export\s+)?(PLATEGA_MERCHANT_ID|PLATEGA_SECRET)\s*=")
# No spaces, quotes, # or $: such a value would be read differently by dotenv and by the shell.
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._~+/=:-]{8,256}$")


def configure(env_file: Path, values: dict[str, str]) -> bool:
    """Replaces both keys and keeps every other line. Returns False when the file already had them."""
    for key in KEYS:
        if not SAFE_VALUE.match(values.get(key) or ""):
            raise ValueError(f"{key}: expected 8-256 characters without spaces, quotes, # or $")
    if env_file.is_symlink():
        raise ValueError("Refusing to replace a symlinked .env")
    original = env_file.read_text(encoding="utf-8-sig") if env_file.exists() else ""
    kept = [line for line in original.splitlines() if not LINE.match(line)]
    wanted = [f"{key}={values[key]}" for key in KEYS]
    if [line for line in original.splitlines() if LINE.match(line)] == wanted:
        return False
    env_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".platega-config-", dir=env_file.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(kept + wanted) + "\n")
        os.chmod(name, 0o600)
        os.replace(name, env_file)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        changed = configure(arguments.env_file, {key: os.environ.get(key, "").strip() for key in KEYS})
    except ValueError as exc:
        print(f"Platega runtime configuration rejected: {exc}", file=sys.stderr)
        sys.exit(2)
    print("Platega keys " + ("written to the runtime .env" if changed else "already up to date") + " (values not shown)")
