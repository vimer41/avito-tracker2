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
    cards = [{"id": lot, "title": f"Lot {lot}", "urlPath": f"/kazan/lot_{lot}", "sortTimeStamp": now_ms, "locationId": 650400, "categoryId": 99}
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
        self.assertNotIn(".data", " ".join(c.url for c in found), "Avito answers 404 to <path>.data")
        mobile = next(c.url for c in found if "m.avito.ru" in c.url)
        self.assertIn("locationId=650400", mobile)
        self.assertIn("query=lamp", mobile)
        self.assertIn("categoryId=99", next(c.url for c in found if c.name == "мобильный API 11 + категория"))
        self.assertTrue(any("/api/9/items" in c.url for c in found))
        self.assertLessEqual(len(found), json_probe.CANDIDATES_MAX)

    def test_query_words_and_time_fields_are_shown(self):
        data = json.loads(mobile_json(IDS))
        data["result"]["items"][0]["value"]["title"] = "Настольная лампа"
        described = json_probe.describe_json(data, json_probe.query_stems("https://www.avito.ru/kazan?q=лампа+настольная"))
        self.assertEqual(described["title_hits"], 1)
        self.assertEqual(described["result_keys"], ["items"])
        self.assertTrue(described["time_paths"][0].startswith("time="))

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
        self.assertIn("слова запроса в названиях: 0 из 5", summary)
        self.assertIn("поля времени у лота: time=", report)
        self.assertIn('первый лот: {"id":4000000000', report)
        self.assertIn("- мобильный API /api/11/items: ❌ блок или капча", summary)
        self.assertIn("API со страницы /web/1/js/items/list: ⚠️ отвечает, но лоты другие (0 из 5)", summary)
        self.assertIn("город (locationId): 650400, по лотам (5)", report)
        self.assertIn("варианты JSON: данные маршрута routes/search, мобильный API /api/11/items", report)
        self.assertIn("1.2.3.4:5500 · страница, как в скане · 200", report)
        self.assertIn("5 лотов, обычных 5", report)
        self.assertTrue(all("avito.ru" in url for url in session.urls))
        self.assertEqual(client._lane_users, {}, "the probe takes no scan lane")

    def test_the_query_comes_from_the_radar_link_when_avito_moved_it_into_the_path(self):
        hints = json_probe.page_hints(search_page(), IDS)
        found = json_probe.candidates("https://www.avito.ru/moskva/telefony/apple-ASgB?s=104", hints, None,
                                      query_url="https://www.avito.ru/moskva?q=iphone&s=104")
        self.assertIn("query=iphone", next(c.url for c in found if c.name == "мобильный API /api/11/items"))
        without_words = json_probe.candidates("https://www.avito.ru/moskva/telefony?s=104", hints, None,
                                              query_url="https://www.avito.ru/moskva/telefony?s=104")
        self.assertEqual([c.name for c in without_words if "мобильный" in c.name], ["мобильный API 11 + категория"])

    def test_the_city_comes_from_the_lots_not_from_the_rest_of_the_page(self):
        noise = '<script>{"header": [' + ",".join(['{"locationId":637640}'] * 10) + "]}</script>"
        html = noise + search_page()
        self.assertEqual(json_probe.page_hints(html)["location_id"], "637640", "the page as a whole says Moscow")
        hints = json_probe.page_hints(html, IDS)
        self.assertEqual((hints["location_id"], hints["location_source"]), ("650400", "по лотам"))

    async def test_json_is_probed_even_where_the_page_was_blocked(self):
        blocked, good = proxy_pool.ProxyClient(PROXY_URL), proxy_pool.ProxyClient("http://user:pass@5.6.7.8:5500")
        sessions = {blocked: FakeSession(), good: FakeSession()}
        patches = [patch.object(json_probe.AvitoParser, "fetch_page_async", new=AsyncMock(side_effect=[None, search_page()]))]
        for client in (blocked, good):
            patches += [patch.object(client, "lane_sessions", return_value={json_probe.PROBE_LANE: sessions[client]}),
                        patch.object(client, "wait_for_rate_slot", new=AsyncMock())]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        report = await json_probe.run_probe("https://www.avito.ru/kazan?q=lamp", pool=SimpleNamespace(clients=[blocked, good]))
        self.assertIn("1.2.3.4:5500 · данные маршрута routes/search · 200", report)
        self.assertNotIn("1.2.3.4:5500 · страница, замер", report, "no second page load where the page failed")
        self.assertIn("5.6.7.8:5500 · страница, замер · 200", report)

    async def test_no_channels_says_so(self):
        report = await json_probe.run_probe("https://www.avito.ru/kazan?q=lamp", pool=SimpleNamespace(clients=[]))
        self.assertIn("Нет доступных каналов", report)


if __name__ == "__main__":
    unittest.main()
