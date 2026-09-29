"""
Tests for store search, using saved search results.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from tests import TMP  # noqa: F401  (sets up a throwaway data file)

import app as webapp  # noqa: E402
import search  # noqa: E402
from extract import FetchError, Page  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def serve(name):
    return lambda url, client="requests", headers=None: Page(url, 200, (FIXTURES / name).read_text())


class Stores(unittest.TestCase):
    def setUp(self):
        search._cache.clear()

    def test_bestbuy(self):
        with mock.patch("search.safe_get", serve("search_bestbuy.json")):
            a = search.search("bestbuy", "samsung 990 pro")
        r = a["results"][0]
        self.assertEqual((a["error"], r["store"], r["price"], r["regular"]), ("", "Best Buy", 66495, 110900))
        self.assertTrue(r["url"].startswith("https://www.bestbuy.ca/en-ca/product/"))

    def test_steam(self):
        with mock.patch("search.safe_get", serve("search_steam.json")):
            r = search.search("steam", "elden ring")["results"][0]
        self.assertEqual((r["name"], r["price"], r["url"]), ("ELDEN RING", 7999, "https://store.steampowered.com/app/1245620/"))

    def test_shopify_store_in_usd(self):
        with mock.patch("search.safe_get", serve("search_bethesda.json")):
            r = search.search("bethesda", "fallout")["results"][0]
        self.assertEqual((r["currency"], r["in_stock"]), ("USD", True))
        self.assertTrue(r["url"].startswith("https://gear.bethesda.net/products/"))
        self.assertNotIn("?", r["url"])

    def test_store_error_is_reported_not_raised(self):
        def blocked(*a, **kw):
            raise FetchError("blocked", "blocked by site protection")
        with mock.patch("search.safe_get", blocked):
            a = search.search("walmart", "tv")
        self.assertEqual((a["results"], a["error"]), ([], "blocked by site protection"))

    def test_cached(self):
        calls = []

        def fetch(url, **kw):
            calls.append(url)
            return serve("search_steam.json")(url)
        with mock.patch("search.safe_get", fetch):
            search.search("steam", "Elden Ring")
            search.search("steam", "elden  ring ")
        self.assertEqual(len(calls), 1)


class Api(unittest.TestCase):
    def setUp(self):
        self.client = webapp.app.test_client()

    def test_store_list(self):
        keys = [s["key"] for s in self.client.get("/api/search/stores").get_json()]
        self.assertIn("bestbuy", keys)
        self.assertIn("bethesda", keys)

    def test_search_checks_input(self):
        self.assertEqual(self.client.get("/api/search?store=nope&q=ssd").status_code, 400)
        self.assertEqual(self.client.get("/api/search?store=steam&q=a").status_code, 400)
        with mock.patch("search.search", return_value={"store": "Steam", "results": [], "error": ""}) as s:
            self.assertEqual(self.client.get("/api/search?store=steam&q=ssd").status_code, 200)
            s.assert_called_once_with("steam", "ssd")


if __name__ == "__main__":
    unittest.main()
