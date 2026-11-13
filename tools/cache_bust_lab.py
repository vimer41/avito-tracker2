"""A/B замер кэш-бастинга (?_t=...): чистый URL против _t раз в 15 с и _t на каждый запрос.

Запуск из папки проекта (напрямую или с --proxy из .env):
    venv\\Scripts\\python.exe tools\\cache_bust_lab.py
Параметры: --tick=10 (секунд между запросами), --cycles=12 (кругов), --bucket=15 (секунд окна), --proxy.

Сравнивает три режима по кругу:
  clean    — чистый URL без _t (CDN может кэшировать, но ниже риск 403/429)
  bucket   — _t меняется раз в --bucket секунд
  ms       — свежий _t в миллисекундах на каждый запрос (текущее поведение бота)

Отчёт: tools/cache_bust_lab_report.txt
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from parser import AvitoParser, apply_cache_busting  # noqa: E402

URLS = [
    "https://www.avito.ru/moskva/telefony/mobilnye_telefony/apple-ASgBAgICAkS0wA3OqzmwwQ2I_Dc?s=104",
    "https://www.avito.ru/moskva/audio_i_video?q=Bose+QC45&s=104",
    "https://www.avito.ru/sankt-peterburg/odezhda_obuv_aksessuary/muzhskaya_odezhda-ASgBAgICAUTeAtgL?s=104",
]
MODES = ("clean", "bucket", "ms")
REPORT = ROOT / "tools" / "cache_bust_lab_report.txt"


def _arg(name: str, default, cast):
    return next((cast(a.split("=", 1)[1]) for a in sys.argv if a.startswith(f"--{name}=")), default)


def target_url_for_mode(url: str, mode: str, bucket_seconds: float, now: float | None = None) -> str:
    if mode == "clean":
        return apply_cache_busting(url, mode="off")
    if mode == "bucket":
        return apply_cache_busting(url, mode="bucket", window_seconds=bucket_seconds, now=now)
    return apply_cache_busting(url, mode="ms", window_seconds=0.0, now=now)


def main() -> None:
    tick = _arg("tick", 10.0, float)
    cycles = _arg("cycles", 12, int)
    bucket_seconds = _arg("bucket", 15.0, float)
    proxy = (config.PROXY_URL or None) if "--proxy" in sys.argv else None

    lines: list[str] = []

    def out(text: str = "") -> None:
        lines.append(text)
        print(text, flush=True)

    channel = f"прокси {proxy.rsplit('@', 1)[-1]}" if proxy else "напрямую"
    out(f"Канал: {channel} · tick {tick:.0f} с · кругов {cycles} · bucket {bucket_seconds:.0f} с · {time.strftime('%Y-%m-%d %H:%M:%S')}")

    orig_mode = config.AVITO_CACHE_BUST_MODE
    orig_window = config.AVITO_CACHE_BUST_WINDOW
    # One parser for all modes: Avito blocks the IP, not the session, so separate parsers isolate nothing
    # and only hide that a block caused by one mode hits the next ones.
    parser = AvitoParser(proxy=proxy)
    stats = {
        mode: {"req": 0, "ok": 0, "block": 0, "cool": 0, "err": 0, "times": [], "kb": [], "top_ts": []}
        for mode in MODES
    }
    total = cycles * len(URLS) * len(MODES)
    step = 0
    turn = 0
    try:
        for cycle in range(cycles):
            for url in URLS:
                # Rotate the order, so no mode is always the first request after a pause.
                shift = turn % len(MODES)
                turn += 1
                for mode in MODES[shift:] + MODES[:shift]:
                    step += 1
                    config.AVITO_CACHE_BUST_MODE = "off" if mode == "clean" else mode
                    config.AVITO_CACHE_BUST_WINDOW = bucket_seconds if mode == "bucket" else 0.0
                    t0 = time.monotonic()
                    ok = False
                    blocked = False
                    cooldown = 0.0
                    kb = 0
                    top_ts = None
                    status = None
                    try:
                        html = parser.fetch_page(url, deadline=t0 + 30.0)
                        if html and AvitoParser.has_catalog(html):
                            ok = True
                            status = "200"
                            kb = round(len(html.encode("utf-8", errors="ignore")) / 1024)
                            items = AvitoParser.parse_items(html)
                            top_ts = max((it.timestamp for it in items if it.timestamp), default=None)
                        elif parser.last_failure is not None:
                            kind = parser.last_failure.kind
                            status = f"{kind}:{parser.last_failure.status}" if parser.last_failure.status else kind
                            blocked = kind in ("forbidden", "rate_limit", "challenge", "server_retry_after")
                            if kind == "cooldown":  # a local pause after an earlier block: no request was sent
                                cooldown = float(parser.last_failure.retry_after or 0.0)
                        else:
                            status = "empty"
                    except Exception as exc:
                        status = type(exc).__name__
                    elapsed = time.monotonic() - t0
                    st = stats[mode]
                    st["req"] += 1
                    if ok:
                        st["ok"] += 1
                        st["times"].append(elapsed)
                        st["kb"].append(kb)
                        if top_ts:
                            st["top_ts"].append(top_ts)
                    elif blocked:
                        st["block"] += 1
                    elif status == "cooldown":
                        st["cool"] += 1
                    else:
                        st["err"] += 1
                    out(f"  [{step:3d}/{total}] {mode:6s}  {str(status):>14s}  {elapsed:4.2f} с  {kb:3d} КБ  top_ts={top_ts or '-'}")
                    if step < total:
                        # Sit the pause out, so the next mode sends a real request instead of another "cooldown".
                        time.sleep(max(tick, min(cooldown, 300.0)))
    finally:
        config.AVITO_CACHE_BUST_MODE = orig_mode
        config.AVITO_CACHE_BUST_WINDOW = orig_window
        parser.close()

    out("")
    out("ИТОГ (блок% = блоки от запросов, дошедших до Авито; «пауза» = шаг пропущен из-за паузы после чужого блока)")
    out("  режим    шагов  ок  блок  пауза  ошиб  блок%   t_med   КБ_med")
    for mode in MODES:
        st = stats[mode]
        blk_pct = round(100 * st["block"] / max(1, st["req"] - st["cool"]))
        t_med = f"{statistics.median(st['times']):.2f} с" if st["times"] else "—"
        kb_med = f"{statistics.median(st['kb']):.0f}" if st["kb"] else "—"
        out(f"  {mode:6s}   {st['req']:5d}  {st['ok']:2d}  {st['block']:4d}  {st['cool']:5d}  {st['err']:4d}   {blk_pct:3d}%  {t_med:>6s}   {kb_med:>6s}")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
