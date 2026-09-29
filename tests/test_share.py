"""
Tests for the read-only share page: only the secret path works, only on the
share port, and it never exposes settings or the ntfy topic.
"""

import json
import unittest
from unittest import mock

from tests import TMP  # noqa: F401  (sets up a throwaway data file)

import app as webapp  # noqa: E402
import config  # noqa: E402
import price_watch as pw  # noqa: E402
import store  # noqa: E402
from werkzeug.test import Client  # noqa: E402

TOKEN = "s3cr3t-token-for-tests"


class SharePage(unittest.TestCase):
    def setUp(self):
        for p in TMP.glob("*"):
            p.unlink()
        with store.update() as d:
            it = pw.new_item("https://gear.bethesda.net/products/x", "Poker Set", [pw.new_edition("shopify:1", "Poker Set")])
            it.update(selector=".secret-selector", min_drop="5%")
            it["editions"][0].update(price=20000, currency="USD", in_stock=False)
            paused = pw.new_item("https://store.steampowered.com/app/1/", "Paused Game", None)
            paused["paused"] = True
            d["items"] = [it, paused]
        patcher = mock.patch.multiple(config, SHARE_TOKEN=TOKEN, SHARE_PORT=5001, NTFY_TOPIC="pricewatch-private-topic")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.share = Client(webapp.application)

    def get(self, path, port=5001):
        return self.share.get(path, environ_overrides={"SERVER_PORT": str(port)})

    def test_page_and_data(self):
        self.assertEqual(self.get(f"/s/{TOKEN}").status_code, 200)
        r = self.get(f"/s/{TOKEN}/data")
        data = r.get_json()
        self.assertEqual([i["name"] for i in data["items"]], ["Poker Set"])  # paused items are not shown
        item = data["items"][0]
        self.assertEqual(item["editions"][0]["price"], 20000)
        text = r.get_data(as_text=True)
        for secret in ("pricewatch-private-topic", "secret-selector", "5%", item.get("id", "no-id-field"), "failure_alerted"):
            self.assertNotIn(secret, text)
        self.assertNotIn("id", item)
        self.assertEqual(r.headers["X-Robots-Tag"], "noindex, nofollow")

    def test_wrong_or_missing_token(self):
        for path in ("/s/wrong", "/s/wrong/data", "/", "/s/"):
            self.assertEqual(self.get(path).status_code, 404, path)

    def test_main_app_not_reachable_on_share_port(self):
        for path in ("/api/dashboard", "/api/search/stores", "/", "/item/abc"):
            self.assertEqual(self.get(path).status_code, 404, path)
        r = self.share.post("/api/check", data=json.dumps({}), content_type="application/json",
                            environ_overrides={"SERVER_PORT": "5001"})
        self.assertIn(r.status_code, (404, 405))

    def test_sharing_off_without_token(self):
        with mock.patch.object(config, "SHARE_TOKEN", ""):
            self.assertEqual(self.get("/s/").status_code, 404)
            self.assertEqual(self.get("/s/anything/data").status_code, 404)

    def test_main_port_unchanged(self):
        with mock.patch.object(webapp.health, "pi_health", return_value={}), mock.patch.object(webapp.health, "run", return_value=""):
            r = self.get("/api/dashboard", port=5000)
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/s/{TOKEN}", r.get_json()["status"]["share_link"])


if __name__ == "__main__":
    unittest.main()
