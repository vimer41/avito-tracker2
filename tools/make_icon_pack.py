"""
Build the bot's own monochrome custom-emoji pack from Tabler outline icons (MIT) and print the slot ids.

The pack is «adaptive» (needs_repainting): Telegram paints each icon in the text colour, so the icons are white on
a dark button and match the clean theme. Every sticker carries its slot's plain emoji, so ui.match_slots maps it.

    venv\\Scripts\\python.exe tools\\make_icon_pack.py            # create (or --replace) and print CLEAN_IDS

Needs `resvg-py` (pip install resvg-py; not a bot dependency) and BOT_TOKEN / ADMIN_ID in .env: the pack is created
by that bot and owned by that admin. Run it with the bot whose name the pack should carry.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TABLER = "https://cdn.jsdelivr.net/npm/@tabler/icons@3.19.0/icons/outline/{}.svg"
PACK_TITLE = "Avito Radar"
PACK_SHORT = "avito_radar_icons"

# slot -> Tabler outline icon. Slots sharing a plain emoji (lite/new: ⚡) share one sticker.
ICONS = {
    "logo": "satellite", "hello": "mood-smile", "radar": "radar-2", "radars": "list-details", "add": "plus",
    "tariffs": "credit-card", "profile": "user", "help": "book-2", "docs": "file-text", "support": "message-circle",
    "home": "home", "trial": "gift", "lite": "bolt", "standart": "rocket", "pro": "flame", "on": "circle-dot",
    "pause": "player-pause", "play": "player-play", "shield": "scissors", "ban": "user-off",
    "link": "external-link", "trash": "trash", "refresh": "refresh", "ok": "check", "warn": "alert-triangle",
    "cancel": "x", "wait": "hourglass", "new": "bolt", "price": "currency-rubel", "place": "map-pin",
    "time": "clock", "box": "package", "views": "eye", "note": "notes", "card": "qrcode",
    "bank": "building-bank", "star": "star", "coin": "currency-bitcoin", "lock": "lock", "scope": "search",
    "write": "pencil", "mail": "mail", "admin": "shield-lock", "users": "users", "net": "world",
    "stats": "chart-line", "db": "database", "logs": "list", "palette": "palette",
    "bug": "bug", "question": "help-circle", "tag": "tag", "flag": "flag",
    "prio_low": "bell", "prio_mid": "bell-ringing", "prio_high": "urgent",
    "inbox": "inbox", "clip": "paperclip",
    "moon": "moon", "sliders": "adjustments", "back": "arrow-left", "megaphone": "speakerphone",
}


def env() -> dict:
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    values.update(os.environ)
    return values


def render(icon: str) -> bytes:
    import resvg_py

    svg = requests.get(TABLER.format(icon), timeout=20)
    svg.raise_for_status()
    # White strokes on transparent, a little margin so the icon sits like a letter, 100x100 as Telegram wants.
    text = svg.text.replace('stroke="currentColor"', 'stroke="#FFFFFF"').replace('viewBox="0 0 24 24"',
                                                                                 'viewBox="-1.5 -1.5 27 27"')
    return bytes(resvg_py.svg_to_bytes(svg_string=text, width=100, height=100))


def api(token: str, method: str, files=None, **data):
    resp = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=60)
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"{method}: {body}")
    return body["result"]


def main():
    cfg = env()
    token, owner = cfg["BOT_TOKEN"], int(cfg["ADMIN_ID"].split(",")[0])
    import ui

    bot = api(token, "getMe")["username"]
    name = f"{PACK_SHORT}_by_{bot}"
    existing_pack = None
    if "--replace" in sys.argv:
        try:
            api(token, "deleteStickerSet", name=name)
            print(f"старый набор {name} удалён")
        except RuntimeError:
            pass
    else:
        try:
            existing_pack = api(token, "getStickerSet", name=name)
        except RuntimeError:
            existing_pack = None

    existing_chars = {ui._norm(s.get("emoji", "")) for s in (existing_pack or {}).get("stickers", [])}
    stickers, seen = [], set()
    for slot, icon in ICONS.items():
        char = ui.SLOTS[slot][0]
        norm_char = ui._norm(char)
        if norm_char in seen or norm_char in existing_chars:
            continue
        seen.add(norm_char)
        png = render(icon)
        file_id = api(token, "uploadStickerFile", files={"sticker": (f"{slot}.png", png, "image/png")},
                      user_id=owner, sticker_format="static")["file_id"]
        sticker_obj = {"sticker": file_id, "format": "static", "emoji_list": [char]}
        if existing_pack is not None:
            api(token, "addStickerToSet", user_id=owner, name=name, sticker=json.dumps(sticker_obj, ensure_ascii=False))
        else:
            stickers.append(sticker_obj)
        print(f"{slot:9} {icon:18} {char}")
        time.sleep(0.2)

    if existing_pack is None:
        api(token, "createNewStickerSet", user_id=owner, name=name, title=PACK_TITLE, sticker_type="custom_emoji",
            needs_repainting="true", stickers=json.dumps(stickers, ensure_ascii=False))
    pack = api(token, "getStickerSet", name=name)
    ids = ui.match_slots((s.get("emoji", ""), s["custom_emoji_id"]) for s in pack["stickers"])
    print(f"\nНабор: https://t.me/addemoji/{name}  ({len(pack['stickers'])} иконок, слотов {len(ids)} из {len(ui.SLOTS)})")
    print(f'CLEAN_PACK = "{name}"')
    print("CLEAN_IDS = " + json.dumps(ids, ensure_ascii=False, indent=4))


if __name__ == "__main__":
    main()
