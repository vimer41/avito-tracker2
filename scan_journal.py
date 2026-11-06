"""
Per-radar scan journal for the admin panel: the last scans of every radar and the lots they found.

Lives in memory only (cleared by a restart): a few dicts per scan, no I/O on the hot path.
It answers «how often is this radar really checked» and «whose fault is a 4-minute-old lot»:
for every new lot, age = Avito's hold (the lot was absent from the previous good scan) + the bot's gap.
"""
from __future__ import annotations

import statistics
import time
from collections import OrderedDict, deque
from typing import Iterable, Optional

SCANS_PER_RADAR = 80
LOTS_PER_RADAR = 30
DECISION_ITEMS = 5000

_scans: dict[int, deque] = {}
_lots: dict[int, deque] = {}
# item_id -> what each radar decided about this lot when it was new; answers «why did this lot not come»
_decisions: "OrderedDict[str, list[dict]]" = OrderedDict()

VERDICTS = {
    "queued": "отправлен в очередь на доставку",
    "title": "нет слов запроса в названии (радар ищет только в названиях)",
    "old": "старше лимита радара по возрасту",
    "unknown_age": "Авито не дал время публикации, лот ждёт повторной проверки",
    "stop": "стоп-слово",
    "seller": "продавец в чёрном списке",
    "filter": "фильтр радара",
    "catchup": "догон после паузы: взяты только самые свежие",
}


def record_decisions(radar_id: int, at: float, verdicts: dict, items: dict | None = None) -> None:
    """verdicts: item_id -> (verdict, detail). items: item_id -> AvitoItem for the title and link."""
    for item_id, (verdict, detail) in verdicts.items():
        item = (items or {}).get(item_id)
        entries = _decisions.setdefault(str(item_id), [])
        entries.append({
            "rid": int(radar_id), "at": float(at), "verdict": verdict, "detail": str(detail or "")[:80],
            "title": str(getattr(item, "title", "") or "")[:60], "pub": getattr(item, "timestamp", None), "sent": None,
        })
        del entries[:-10]
        _decisions.move_to_end(str(item_id))
    while len(_decisions) > DECISION_ITEMS:
        _decisions.popitem(last=False)


def decisions(item_id: str) -> list[dict]:
    return list(_decisions.get(str(item_id), ()))


def record_scan(radar_ids: Iterable, *, ok: bool, at: Optional[float] = None, ms: float = 0.0, channel: str = "",
                items: int = 0, pages: int = 0, fast: bool = False, kind: str = "", detail: str = "",
                due_lag: float = 0.0) -> None:
    entry = {
        "at": float(at or time.time()),
        "ok": bool(ok),
        "ms": int(ms),
        "ch": channel,
        "items": int(items),
        "pages": int(pages),
        "fast": bool(fast),
        "kind": kind,
        "detail": str(detail or "")[:120],
        "due_lag": round(float(due_lag or 0.0), 1),
        "new": {},
    }
    for rid in radar_ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        _scans.setdefault(rid, deque(maxlen=SCANS_PER_RADAR)).append(entry)


def record_lot(radar_id: int, *, item_id: str, title: str, url: str, published: Optional[float], seen_at: float,
               scan_at: float, previous_scan_at: float, reliable: bool = False, fresh: bool = False,
               promo: bool = False, pos: Optional[int] = None, channel: str = "") -> None:
    radar_id = int(radar_id)
    _lots.setdefault(radar_id, deque(maxlen=LOTS_PER_RADAR)).append({
        "item": str(item_id),
        "title": str(title or "")[:60],
        "url": url,
        "pub": float(published) if published else None,
        "seen": float(seen_at),
        "prev": float(previous_scan_at or 0.0),
        "reliable": bool(reliable),
        "fresh": bool(fresh),
        "promo": bool(promo),
        "pos": pos,
        "ch": channel,
        "sent": None,
    })
    for entry in reversed(_scans.get(radar_id, ())):
        if abs(entry["at"] - scan_at) < 0.001:
            entry["new"][radar_id] = entry["new"].get(radar_id, 0) + 1
            break


def record_sent(radar_ids: Iterable, item_id: str, sent_at: float) -> None:
    radar_ids = [int(rid) for rid in radar_ids]
    for entry in _decisions.get(str(item_id), ()):
        if entry["rid"] in radar_ids and entry["sent"] is None:
            entry["sent"] = float(sent_at)
    for rid in radar_ids:
        for lot in reversed(_lots.get(int(rid), ())):
            if lot["item"] == str(item_id):
                if lot["sent"] is None:
                    lot["sent"] = float(sent_at)
                break


def scans(radar_id: int) -> list[dict]:
    return list(_scans.get(int(radar_id), ()))


def lots(radar_id: int) -> list[dict]:
    return list(_lots.get(int(radar_id), ()))


def forget(radar_id: int) -> None:
    _scans.pop(int(radar_id), None)
    _lots.pop(int(radar_id), None)


def reset() -> None:
    """«Start over» in the admin panel: scans and lots of every radar start empty. The lot decisions stay:
    «why did this lot not come» must still answer for lots found before the reset."""
    _scans.clear()
    _lots.clear()


def summary(radar_id: int, window: float = 3600.0, now: Optional[float] = None) -> dict:
    """Real check rate over the last `window` seconds: gaps between scans, share of failures."""
    now = now or time.time()
    rows = [s for s in scans(radar_id) if now - s["at"] <= window]
    ok_times = [s["at"] for s in rows if s["ok"]]
    gaps = [b - a for a, b in zip(ok_times, ok_times[1:])]
    return {
        "scans": len(rows),
        "ok": len(ok_times),
        "fail": len(rows) - len(ok_times),
        "span": (rows[-1]["at"] - rows[0]["at"]) if len(rows) > 1 else 0.0,
        "gap_median": statistics.median(gaps) if gaps else None,
        "gap_max": max(gaps) if gaps else None,
        "since_last_ok": (now - ok_times[-1]) if ok_times else None,
        "ms_median": statistics.median([s["ms"] for s in rows if s["ok"]]) if ok_times else None,
        "kinds": _count(s["kind"] for s in rows if not s["ok"]),
    }


def lot_split(lot: dict) -> dict:
    """age = seen − published; the bot was not looking for at most seen − previous scan, the rest is Avito."""
    if not lot.get("pub"):
        return {"age": None, "bot": None, "avito": None, "send": None}
    age = max(0.0, lot["seen"] - lot["pub"])
    bot = max(0.0, lot["seen"] - lot["prev"]) if lot.get("prev") else None
    avito = max(0.0, age - bot) if bot is not None else None
    send = max(0.0, lot["sent"] - lot["seen"]) if lot.get("sent") else None
    return {"age": age, "bot": min(bot, age) if bot is not None else None, "avito": avito, "send": send}


def _count(values: Iterable[str]) -> dict:
    out: dict[str, int] = {}
    for value in values:
        out[value or "?"] = out.get(value or "?", 0) + 1
    return out
