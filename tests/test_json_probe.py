import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import _environment  # noqa: F401  (isolated data dir)
import json_probe
import proxy_pool

PROXY_URL = "http://user:pass@1.2.3.4:5500"
IDS = [str(4000000000 + i) for i in range(5)]


def search_page() -> str:
    now_ms = int(time.time() * 1000)
    cards = [{"id": lot, "title": f"Lot {lot}", "urlPath": f"/kazan/lot_{lot}", "sortTimeStamp": now_ms, "locationId": 650400}
             for lot in IDS]
    state = {"loaderData": {"data": {"catalog": {"items": cards}}}}
    context = {"routes": [{"id": "root"}, {"id": "routes/search"}], "api": "/web/1/js/items/list"}
    return ("<html><script>window.__remixContext = " + json.dumps(context) + "</script><script>"
            + json.dumps(state) + "</script></html>")


def mobile_json(ids) -> str:
    now = int(time.time())
    items = [{"type": "item", "value": {"id": int(lot), "title": f"Lot {lot}", "price": "100 ₽", "time": now - 60,
                                        "images": [{"x": "y"}], "location": "Казань", "uri": f"ru.avito://{lot}"}}
             for lot in ids]
    return json.dumps({"status": "ok", "result": {"items": items}})


class FakeSession:
    def __init__(self):
        self.urls = []

    async def request(self, method, url, **kwargs):
        self.urls.append(url)

        def answer(status, text, kind="application/json"):
            return SimpleNamespace(status_code=status, text=text, headers={"content-type": kind}, download_size=len(text) // 4,
                                   infos={}, json=lambda: json.loads(text))

        if "_data=" in url:
            return answer(200, mobile_json(IDS))
        if ".data" in url:
            return answer(404, "not found", "text/plain")
        if "m.avito.ru" in url:
            return answer(403, "<div class='firewall-container'>доступ ограничен</div>", "text/html")
        if "/web/1/js/items" in url:
            return answer(200, mobile_json(["9999999999"]))
        return answer(200, search_page(), "text/html")


class TestJsonProbe(unittest.IsolatedAsyncioTestCase):
    def test_page_hints_and_candidates(self):
        hints = json_probe.page_hints(search_page())
        self.assertEqual(hints["routes"], ["routes/search"])
        self.assertEqual((hints["location_id"], hints["remix"]), ("650400", True))
        self.assertEqual(hints["api_paths"], ["/web/1/js/items/list"])
        found = json_probe.candidates("https://www.avito.ru/kazan?q=lamp&s=104", hints, "https://www.avito.ru/x.json")
        names = [candidate.name for candidate in found]
        self.assertEqual(names[0], "ваш адрес")
        self.assertIn("https://www.avito.ru/kazan?q=lamp&s=104&_data=routes%2Fsearch", [c.url for c in found])
        self.assertIn("https://www.avito.ru/kazan.data?q=lamp&s=104", [c.url for c in found])
        mobile = next(c.url for c in found if "m.avito.ru" in c.url)
        self.assertIn("locationId=650400", mobile)
        self.assertIn("query=lamp", mobile)
        self.assertLessEqual(len(found), json_probe.CANDIDATES_MAX)

    def test_lots_and_their_fields_are_found_at_any_depth(self):
        lots, fields, keys, newest = json_probe.analyze_json(json.loads(mobile_json(IDS)))
        self.assertEqual(lots, 5)
        self.assertTrue(fields["название"] and fields["цена"] and fields["время"] and fields["фото"])
        self.assertFalse(fields["продавец"])
        self.assertIn("title", keys)
        self.assertAlmostEqual(newest, time.time() - 60, delta=5)

    async def test_probe_compares_every_answer_with_the_page(self):
        client = proxy_pool.ProxyClient(PROXY_URL)
        session = FakeSession()
        pool = SimpleNamespace(clients=[client])
        with patch.object(json_probe.AvitoParser, "fetch_page_async", new=AsyncMock(return_value=search_page())), \
                patch.object(client, "lane_sessions", return_value={json_probe.PROBE_LANE: session}), \
                patch.object(client, "wait_for_rate_slot", new=AsyncMock()):
            report = await json_probe.run_probe("https://www.avito.ru/kazan?q=lamp", pool=pool)

        summary = report.split("## Итог\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("- данные маршрута routes/search: ✅ те же лоты (5 из 5) на всех каналах", summary)
        self.assertIn("нет полей: продавец", summary)
        self.assertIn("- <путь>.data (React Router): ❌ не отдаёт (404×1)", summary)
        self.assertIn("- мобильный API /api/11/items: ❌ блок или капча", summary)
        self.assertIn("API со страницы /web/1/js/items/list: ⚠️ отвечает, но лоты другие (0 из 5)", summary)
        self.assertIn("город лотов (locationId): 650400", report)
        self.assertIn("1.2.3.4:5500 · страница, как в скане · 200", report)
        self.assertTrue(all("avito.ru" in url for url in session.urls))
        self.assertEqual(client._lane_users, {}, "the probe takes no scan lane")

    async def test_no_channels_says_so(self):
        report = await json_probe.run_probe("https://www.avito.ru/kazan?q=lamp", pool=SimpleNamespace(clients=[]))
        self.assertIn("Нет доступных каналов", report)


if __name__ == "__main__":
    unittest.main()
