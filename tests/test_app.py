"""
Tests for the web page's JSON API: add, edit, mute, remove with undo,
settings, and the safety checks on requests.
"""

import json
import unittest
from unittest import mock

from tests import TMP  # noqa: E402  (sets up a throwaway data file)

import app as webapp  # noqa: E402
import store  # noqa: E402
from extract import Offer, Result  # noqa: E402

LEGO = "https://www.playstation.com/en-ca/games/lego-batman-legacy-of-the-dark-knight/"


def lego_result():
    return Result("ok", "ok", method="playstation", name="LEGO Batman", image="https://img.example.com/lego.png",
                  offers=[Offer(key="ps:STD", label="Standard Edition", price=8999, currency="CAD"),
                          Offer(key="ps:DLX", label="Deluxe Edition", price=11999, currency="CAD"),
                          Offer(key="ps:UPG", label="Deluxe Edition Upgrade", price=3349, currency="CAD",
                                suggested=False)])


class Api(unittest.TestCase):
    def setUp(self):
        for p in TMP.glob("*"):
            p.unlink()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        patcher = mock.patch.object(webapp, "start_check", return_value=True)
        self.start_check = patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("pi_health", "run"):
            p = mock.patch.object(webapp.health, name, return_value={} if name == "pi_health" else "")
            p.start()
            self.addCleanup(p.stop)

    def post(self, path, data, **kw):
        return self.client.post(path, data=json.dumps(data), content_type="application/json", **kw)

    def add_lego(self, **extra):
        body = {"url": LEGO, "name": "LEGO Batman", "found": True, "image": "https://img.example.com/lego.png",
                "editions": [["ps:STD", "Standard Edition"], ["ps:DLX", "Deluxe Edition"]], **extra}
        return self.post("/api/items/add", body)

    def test_page_and_dashboard(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("💰", page.get_data(as_text=True))
        d = self.client.get("/api/dashboard").get_json()
        self.assertEqual(d["items"], [])
        self.assertIn("quiet_now", d["settings"])

    def test_old_links_redirect(self):
        self.assertEqual(self.client.get("/item/abc").headers["Location"], "/#item-abc")
        self.assertEqual(self.client.get("/settings").headers["Location"], "/#alerts")

    def test_test_endpoint(self):
        with mock.patch("extract.check", return_value=lego_result()):
            r = self.post("/api/test", {"url": LEGO}).get_json()["result"]
        self.assertEqual([o["label"] for o in r["offers"] if o["suggested"]], ["Standard Edition", "Deluxe Edition"])
        self.assertEqual(r["image"], "https://img.example.com/lego.png")

    def test_add_edit_mute_remove_undo(self):
        r = self.add_lego(target="60", min_drop="5%").get_json()
        item = store.load()["items"][0]
        self.assertEqual((item["target"], item["min_drop"], item["image"]), (6000, "5%", "https://img.example.com/lego.png"))
        self.start_check.assert_called_once_with([r["id"]])

        self.post("/api/items/update", {"id": item["id"], "changes": {"target": "", "keep": ["ps:DLX"]}})
        item = store.load()["items"][0]
        self.assertEqual((item["target"], [e["key"] for e in item["editions"]]), (None, ["ps:DLX"]))

        msg = self.post("/api/items/update", {"id": item["id"], "changes": {"muted": True}}).get_json()["message"]
        self.assertIn("Muted", msg)
        self.assertTrue(store.load()["items"][0]["muted"])

        removed = self.post("/api/items/remove", {"id": item["id"]}).get_json()
        self.assertEqual(store.load()["items"], [])
        self.post("/api/items/restore", {"item": removed["item"]})
        self.assertEqual(store.load()["items"][0]["name"], "LEGO Batman")

    def test_stock_alert_switch(self):
        self.add_lego(stock_alert=True)
        item = store.load()["items"][0]
        self.assertTrue(item["stock_alert"])
        msg = self.post("/api/items/update", {"id": item["id"], "changes": {"stock_alert": False}}).get_json()["message"]
        self.assertIn("off", msg)
        self.assertFalse(store.load()["items"][0]["stock_alert"])
        self.post("/api/settings", {"settings": {"quiet_allow_stock": True}})
        self.assertTrue(store.load()["settings"]["quiet_allow_stock"])

    def test_keep_at_least_one_edition(self):
        self.add_lego()
        item = store.load()["items"][0]
        r = self.post("/api/items/update", {"id": item["id"], "changes": {"keep": []}})
        self.assertEqual(r.status_code, 400)

    def test_us_link_refused_unless_asked(self):
        url = "https://direct.playstation.com/en-us/buy-consoles/ps5"
        r = self.post("/api/items/add", {"url": url, "name": "PS5"})
        self.assertIn("does not sell to Canada", r.get_json()["error"])
        self.post("/api/items/add", {"url": url, "name": "PS5", "allow_foreign": True, "found": True,
                                     "editions": [["main", ""]]})
        self.assertTrue(store.load()["items"][0]["allow_foreign"])

    def test_bad_numbers(self):
        r = self.add_lego(target="cheap")
        self.assertIn("Check the target price", r.get_json()["error"])

    def test_bad_image_dropped(self):
        self.add_lego(image="javascript:alert(1)")
        self.assertEqual(store.load()["items"][0]["image"], "")

    def test_local_address_refused(self):
        r = self.post("/api/test", {"url": "http://192.168.86.1/admin"}).get_json()["result"]
        self.assertEqual(r["kind"], "bad_url")

    def test_needs_json_and_same_site(self):
        self.assertEqual(self.client.post("/api/check", data="ids=").status_code, 415)
        r = self.post("/api/check", {}, headers={"Origin": "https://evil.example.com"})
        self.assertEqual(r.status_code, 403)

    def test_settings(self):
        self.post("/api/settings", {"settings": {"quiet_start": "23:00", "quiet_end": "07:00"}})
        s = store.load()["settings"]
        self.assertEqual((s["quiet_start"], s["quiet_end"]), ("23:00", "07:00"))
        self.assertEqual(self.post("/api/settings", {"settings": {"quiet_start": "25:99"}}).status_code, 400)
        self.post("/api/settings", {"settings": {"quiet_enabled": False}})
        self.assertFalse(store.load()["settings"]["quiet_enabled"])
        msg = self.post("/api/settings", {"settings": {"snooze_hours": 8}}).get_json()["message"]
        self.assertIn("snoozed", msg)
        self.assertTrue(store.load()["settings"]["snooze_until"])
        with mock.patch("price_watch.flush_alerts"):
            self.post("/api/settings", {"settings": {"snooze_hours": 0}})
        self.assertEqual(store.load()["settings"]["snooze_until"], "")


if __name__ == "__main__":
    unittest.main()
