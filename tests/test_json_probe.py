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
        if "m.avito.ru" in url and "categoryId=" in url:
            data = json.loads(mobile_json(IDS))
            data["result"].update(lastStamp=1793987000, count=5, totalCount=5)
            return answer(200, json.dumps(data))
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
        self.assertFalse(any("/api/9/items" in c.url for c in found), "API 9 answered 400 everywhere")
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
        self.assertIn("- данные маршрута routes/search: ✅ те же лоты (5 из 5) на всех живых каналах (1)", summary)
        self.assertIn("нет полей: продавец", summary)
        self.assertIn("слова запроса в названиях: 0 из 5", summary)
        self.assertIn("поля времени у лота: time=", report)
        self.assertIn('первый лот: {"id":4000000000', report)
        self.assertIn("- мобильный API /api/11/items: ❌ блок или капча", summary)
        self.assertIn("- мобильный API 11 + категория: ✅ те же лоты (5 из 5) на всех живых каналах (1)", summary)
        self.assertIn("обычных со страницы 5 из 5, рекламных 0 из 0", summary)
        self.assertNotIn("lastStamp=", " ".join(session.urls), "lastStamp is not a filter: no longer tried")
        self.assertIn("счётчики: count=5", report)
        self.assertIn("порядок обычных как на странице: 4 из 4 пар", report)
        self.assertIn("обычный на странице: 4000000000", report)
        self.assertIn('первый лот: {"id":4000000000', report)
        first_lots = [line for line in report.splitlines() if line.startswith("первый лот:")]
        self.assertTrue(first_lots)
        self.assertFalse(any('"images"' in line for line in first_lots), "the lot is shown without photos")
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

    def test_without_lots_the_city_comes_from_its_own_address(self):
        html = ('<script>{"header":[{"locationId":637640},{"locationId":637640}],'
                '"crumbs":[{"id":622650,"name":"Ярославль","url":"/yaroslavl"}]}</script>')
        hints = json_probe.page_hints(html, [], "https://www.avito.ru/yaroslavl/muzykalnye_instrumenty?q=x")
        self.assertEqual((hints["location_id"], hints["location_source"]), ("622650", "по адресу города /yaroslavl"))

    def test_the_city_comes_from_the_lots_not_from_the_rest_of_the_page(self):
        noise = '<script>{"header": [' + ",".join(['{"locationId":637640}'] * 10) + "]}</script>"
        html = noise + search_page()
        self.assertEqual(json_probe.page_hints(html)["location_id"], "637640", "the page as a whole says Moscow")
        hints = json_probe.page_hints(html, IDS)
        self.assertEqual((hints["location_id"], hints["location_source"]), ("650400", "по лотам"))

    async def test_json_is_probed_even_where_the_page_was_blocked(self):
        blocked, good = proxy_pool.ProxyClient(PROXY_URL), proxy_pool.ProxyClient("http://user:pass@5.6.7.8:5500")
        sessions = {blocked: FakeSession(), good: FakeSession()}
        patches = [patch.object(json_probe.AvitoParser, "fetch_page_async", new=AsyncMock(side_effect=[None, search_page()])),
                   patch.object(json_probe, "PROBE_SPARE_BEST", 0)]
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

    def test_a_filter_in_the_path_comes_from_the_page_settings_and_the_category_from_its_lots(self):
        lots = [{"id": lot, "title": "куртка", "categoryId": 27, "locationId": 653240} for lot in IDS]
        state = {"loaderData": {"data": {
            "menu": [{"id": 5, "categoryId": 9}] * 9,
            "rubricators": {"categoryTreeTop": [{"id": 25984, "name": "Транспорт", "url": "/x", "categoryId": 1, "params": {}}]},
            "filtersV2": {"items": [{"id": 178, "title": "Для кого", "options": [{"id": 1234, "title": "Мужская", "selected": True},
                                                                                {"id": 1235, "title": "Женская", "selected": False}]}]},
            "searchCore": {"categoryId": 27, "locationId": 653240, "params": {"178": "1234", "550": [5, 6]}},
            "catalog": {"items": lots}}}}
        html = '{"categoryId":9}' * 9 + "<script>" + json.dumps(state) + "</script>"
        hints = json_probe.page_hints(html, IDS, "https://www.avito.ru/sankt-peterburg/odezhda/muzhskaya-ASgB?q=x")
        self.assertEqual((hints["category_id"], hints["category_source"]), ("27", "по лотам (5)"))
        self.assertEqual([path for path, _ in hints["settings"]], ["searchCore"], "the category menu is not the search")
        self.assertEqual(hints["filters_selected"], ["1234:Мужская=None"])
        self.assertIn("filtersV2: {items} → [1] → {id, title, options}", hints["filters_shape"])
        found = json_probe.candidates("https://www.avito.ru/sankt-peterburg/odezhda/muzhskaya-ASgB?q=x", hints, None)
        url = next(c.url for c in found if c.name == "мобильный API 11 + фильтры страницы")
        self.assertIn("categoryId=27", url)
        self.assertIn("params%5B178%5D=1234", url)
        self.assertIn("params%5B550%5D%5B%5D=5", url)
        self.assertIn("params%5B550%5D%5B%5D=6", url)

    def test_ads_and_relative_times_are_shown_by_example(self):
        reference = json_probe.PageResult(None, "c", None, json_probe.Probe("c", "p"),
                                          organic_ids=IDS[:3], promoted_ids=IDS[3:], stamps={IDS[0]: int(time.time() * 1000) - 120_000})
        data = {"result": {"items": [{"type": "item", "value": {"id": int(lot), "title": "x", "freeForm": [{"text": "5 минут назад"}],
                                                                "badgeBar": {"badges": [{"title": "Продвинуто"}]} if lot in IDS[3:] else {}}}
                                     for lot in IDS]}}
        probe = json_probe.Probe("c", "api")
        json_probe.compare_with_page(probe, data, reference)
        self.assertEqual((probe.organic_hits, probe.organic_of, probe.promo_hits, probe.promo_of), (3, 3, 2, 2))
        self.assertIn("Продвинуто", probe.promo_dump)
        self.assertIn("на странице 2 мин назад", probe.organic_dump)
        described = json_probe.describe_json(data, [])
        self.assertIn("freeForm[0].text=5 минут назад", described["time_paths"])

    def test_id_order_against_time(self):
        now = int(time.time() * 1000)
        self.assertIn("в 2 из 2 пар", json_probe.id_time_agreement({"300000001": now - 3000, "300000002": now - 2000, "300000003": now}))

    def test_verdict_ignores_channels_blocked_for_everything(self):
        ok = json_probe.Probe("a", "api", status=200, matched=5, of=5)
        dead = json_probe.Probe("b", "api", status=403, blocked=True)
        self.assertIn("на всех живых каналах (1)", json_probe._verdict([ok, dead], {"a"}))

    def test_the_best_proxies_are_left_to_the_scans(self):
        def channel(health, direct=False):
            return SimpleNamespace(is_direct=direct, state="healthy", long_health=lambda h=health: h)
        server, proxies = channel(0, True), [channel(h / 10) for h in range(9, 3, -1)]
        picked = json_probe.pick_channels(SimpleNamespace(clients=[server, *proxies]))
        self.assertEqual(picked, [server, *proxies[3:6]])
        self.assertEqual(json_probe.pick_channels(SimpleNamespace(clients=proxies[:2])), [proxies[1]])

    async def test_no_channels_says_so(self):
        report = await json_probe.run_probe("https://www.avito.ru/kazan?q=lamp", pool=SimpleNamespace(clients=[]))
        self.assertIn("Нет доступных каналов", report)


if __name__ == "__main__":
    unittest.main()
