#!/usr/bin/env python3
"""Generate conservative Qrator f variants from an encode-ready base."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import secrets
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

try:
    from anti_bot.qrator_payload_codec import DEFAULT_KEY, decode_field, encode_field
except ImportError:
    from qrator_payload_codec import DEFAULT_KEY, decode_field, encode_field


ROOT = Path(__file__).resolve().parent
DEFAULT_BASE_F = ROOT / "base.txt"
DEFAULT_BASE_S = ROOT / "base-s.json"
DEFAULT_PROFILES_DIR = ROOT / "profiles"
CANVAS_HASH_INDEX = 106
UNIX_TIME_INDEX = 128
F_VALUE_COUNT = 141
MIN_SYNTHETIC_HASH = 4
MAX_UINT32 = 0xFFFFFFFF
REQUIRED_S_KEYS = frozenset({
    "monospace",
    "readOnly",
    "noLengthPlugins",
    "installedExtensions",
})


def parse_f_values(raw: str, *, source: str = "f") -> list[int]:
    values = raw.strip().split(";")
    if len(values) != F_VALUE_COUNT:
        raise ValueError(
            f"{source} must contain {F_VALUE_COUNT} semicolon-separated values, "
            f"received {len(values)}"
        )
    try:
        return [int(value) for value in values]
    except ValueError as error:
        raise ValueError(f"{source} contains a non-integer value") from error


def normalize_s_payload(raw_or_obj: str | dict, *, source: str = "s") -> str:
    parsed = json.loads(raw_or_obj) if isinstance(raw_or_obj, str) else raw_or_obj
    if not isinstance(parsed, dict) or set(parsed) != set(REQUIRED_S_KEYS):
        raise ValueError(
            f"{source} must contain exactly these keys: {sorted(REQUIRED_S_KEYS)}"
        )
    # Qrator encrypts compact JSON.stringify output, not pretty JSON.
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def load_f_base(path: Path) -> list[int]:
    return parse_f_values(path.read_text(encoding="utf-8"), source=str(path))


def load_s_base(path: Path) -> str:
    return normalize_s_payload(path.read_text(encoding="utf-8").strip(), source=str(path))


def profile_canvas_seed(profile_key: str | None) -> int | None:
    """Derive a stable per-channel/session seed so one IP does not mutate its canvas hash on every check."""
    if not profile_key:
        return None
    digest = hashlib.sha256(profile_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def load_qrator_profiles(profiles_dir: Path = DEFAULT_PROFILES_DIR) -> list[tuple[list[int], str, str]]:
    """Load extra captured Qrator profiles (*.json) from anti_bot/profiles/ when present."""
    if not profiles_dir.is_dir():
        return []
    profiles: list[tuple[list[int], str, str]] = []
    for path in sorted(profiles_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            raw_f = payload.get("f")
            if isinstance(raw_f, list):
                f_values = [int(v) for v in raw_f]
                if len(f_values) != F_VALUE_COUNT:
                    continue
            elif isinstance(raw_f, str):
                f_values = parse_f_values(raw_f, source=str(path))
            else:
                continue
            s_value = normalize_s_payload(payload.get("s"), source=str(path))
            label = str(payload.get("label") or path.stem)
            profiles.append((f_values, s_value, label))
        except Exception:
            continue
    return profiles


def select_qrator_profile(
    profile_key: str | None = None,
    profiles_dir: Path = DEFAULT_PROFILES_DIR,
) -> tuple[list[int], str, str]:
    """Deterministically pick a captured profile for profile_key, falling back to base.txt / base-s.json."""
    profiles = load_qrator_profiles(profiles_dir)
    if profiles:
        if profile_key:
            idx = int.from_bytes(hashlib.sha256(profile_key.encode("utf-8")).digest()[:4], "big") % len(profiles)
        else:
            idx = 0
        return profiles[idx]
    return load_f_base(DEFAULT_BASE_F), load_s_base(DEFAULT_BASE_S), "default"


def import_captured_profile(
    f_cookie: str,
    s_payload: str,
    *,
    out_dir: Path = DEFAULT_PROFILES_DIR,
    label: str = "",
    key: Sequence[int] = DEFAULT_KEY,
) -> Path:
    """Decode a real browser Qrator f cookie (5.<hex>) and s field into a reusable profile JSON."""
    raw_f = decode_field(f_cookie.strip(), "f", key) if f_cookie.strip().startswith("5.") else f_cookie.strip()
    f_values = parse_f_values(raw_f, source="captured f")
    raw_s_text = s_payload.strip()
    if not raw_s_text.startswith("{"):
        raw_s_text = decode_field(raw_s_text, "s", key)
    s_compact = normalize_s_payload(raw_s_text, source="captured s")

    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{raw_f}|{s_compact}".encode("utf-8")).hexdigest()[:10]
    safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label.strip())[:32]
    stem = f"{safe_label}_{digest}" if safe_label else f"profile_{digest}"
    target = out_dir / f"{stem}.json"
    record = {
        "label": label.strip() or stem,
        "f": ";".join(map(str, f_values)),
        "s": json.loads(s_compact),
    }
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def random_source(seed: int | None) -> Callable[[], int]:
    if seed is None:
        return lambda: MIN_SYNTHETIC_HASH + secrets.randbelow(
            MAX_UINT32 - MIN_SYNTHETIC_HASH + 1
        )
    generator = random.Random(seed)
    return lambda: generator.randrange(
        MIN_SYNTHETIC_HASH,
        MAX_UINT32 + 1,
    )


def generate_variants(
    base_values: list[int],
    raw_s: str,
    *,
    count: int,
    timestamp: int,
    seed: int | None = None,
    include_raw: bool = False,
    key: Sequence[int] = DEFAULT_KEY,
) -> list[dict[str, object]]:
    if count < 1:
        raise ValueError("count must be positive")
    available_hashes = MAX_UINT32 - MIN_SYNTHETIC_HASH + 1
    if count > available_hashes:
        raise ValueError(f"count cannot exceed {available_hashes}")

    next_hash = random_source(seed)
    used_hashes: set[int] = set()
    encoded_s = encode_field(raw_s, "s", key)
    variants: list[dict[str, object]] = []

    while len(variants) < count:
        canvas_hash = next_hash()
        if canvas_hash in used_hashes:
            continue
        used_hashes.add(canvas_hash)

        values = base_values.copy()
        values[CANVAS_HASH_INDEX] = canvas_hash
        values[UNIX_TIME_INDEX] = timestamp
        raw_f = ";".join(map(str, values))
        item: dict[str, object] = {
            "f": encode_field(raw_f, "f", key),
            "s": encoded_s,
            "canvasHash": canvas_hash,
            "timestamp": timestamp,
        }
        if include_raw:
            item["rawF"] = raw_f
            item["rawS"] = raw_s
        variants.append(item)

    return variants


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--base-f", type=Path, default=DEFAULT_BASE_F)
    parser.add_argument("--base-s", type=Path, default=DEFAULT_BASE_S)
    parser.add_argument(
        "--output",
        type=Path,
        help="NDJSON output file; omit to write to stdout",
    )
    parser.add_argument(
        "--timestamp",
        type=int,
        help="Unix seconds; defaults to the current time",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="reproducible PRNG seed; default uses secrets",
    )
    parser.add_argument("--include-raw", action="store_true")
    parser.add_argument("--import-f", help="captured Qrator f cookie (5.<hex> or raw 141 ints) to save into profiles/")
    parser.add_argument("--import-s", help="captured Qrator s field (<hex> or JSON); defaults to base-s.json")
    parser.add_argument("--label", default="", help="optional label for --import-f")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.import_f:
        s_source = args.import_s if args.import_s else load_s_base(args.base_s)
        saved_path = import_captured_profile(args.import_f, s_source, label=args.label)
        print(f"imported profile to {saved_path}", file=sys.stderr)
        return
    timestamp = int(time.time()) if args.timestamp is None else args.timestamp
    variants = generate_variants(
        load_f_base(args.base_f),
        load_s_base(args.base_s),
        count=args.count,
        timestamp=timestamp,
        seed=args.seed,
        include_raw=args.include_raw,
    )
    output = "".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
        for item in variants
    )
    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(
            f"saved {len(variants)} variants to {args.output}; "
            f"static s={variants[0]['s']}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(output)


if __name__ == "__main__":
    main()
