"""
Tests for the alert rules, quiet hours, and the saved file.

Run from the project folder:  .venv/bin/python -m unittest -v
"""

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from tests import TMP  # noqa: E402  (sets up a throwaway data file)
from datetime import timedelta  # noqa: E402

import config  # noqa: E402
import price_watch as pw  # noqa: E402
import store  # noqa: E402
from extract import Offer, Result  # noqa: E402


def quiet_now():
    """Quiet hours that include the current minute, whatever time the tests run."""
    now = datetime.now(config.TZ)
    return {"quiet_start": (now - timedelta(minutes=30)).strftime("%H:%M"),
            "quiet_end": (now + timedelta(minutes=30)).strftime("%H:%M")}


def ok(*prices, keys=None, method="steam-api", note="", seller=""):
    keys = keys or ["main"] * len(prices)
    return Result("ok", "ok", method=method, offers=[
        Offer(key=k, label=k, price=p, currency="CAD", note=note, seller=seller) for k, p in zip(keys, prices)
    ])


def item(**kw):
    return pw.new_item("https://store.steampowered.com/app/1/x/", "Game",
                       kw.pop("editions", None), **kw)


def kinds(alerts):
    return [a["kind"] for a in alerts]


class Drops(unittest.TestCase):
    def test_first_check_is_a_baseline(self):
        it = item()
        self.assertEqual(pw.apply_result(it, ok(7999)), [])
        self.assertEqual(it["editions"][0]["last_good_price"], 7999)
        self.assertEqual(it["status"], "ok")

    def test_drop_alerts(self):
        it = item()
        pw.apply_result(it, ok(7999))
        alerts = pw.apply_result(it, ok(5999))
        self.assertEqual(kinds(alerts), ["drop"])
        self.assertIn("Now $59.99 (was $79.99)", alerts[0]["message"])
        self.assertEqual(pw.apply_result(it, ok(5999)), [])  # no repeat while it stays there

    def test_foreign_currency_in_alert(self):
        it = item()
        pw.apply_result(it, Result("ok", "ok", offers=[Offer(price=89900, currency="USD")]))
        alerts = pw.apply_result(it, Result("ok", "ok", offers=[Offer(price=79900, currency="USD")]))
        self.assertIn("Now $799.00 USD (was $899.00 USD)", alerts[0]["message"])

    def test_rise_is_quiet(self):
        it = item()
        pw.apply_result(it, ok(5999))
        self.assertEqual(pw.apply_result(it, ok(7999)), [])

    def test_min_drop_amount(self):
        it = item(min_drop=pw.parse_min_drop("5"))
        pw.apply_result(it, ok(10000))
        self.assertEqual(pw.apply_result(it, ok(9800)), [])  # $2 drop, below $5
        self.assertEqual(pw.apply_result(it, ok(9600)), [])  # another $2, drops do not add up
        self.assertEqual(kinds(pw.apply_result(it, ok(9000))), ["drop"])  # $6

    def test_min_drop_percent(self):
        it = item(min_drop=pw.parse_min_drop("10%"))
        pw.apply_result(it, ok(10000))
        self.assertEqual(pw.apply_result(it, ok(9500)), [])
        self.assertEqual(kinds(pw.apply_result(it, ok(8500))), ["drop"])

    def test_parse_min_drop(self):
        self.assertEqual(pw.parse_min_drop("$5"), "5.00")
        self.assertEqual(pw.parse_min_drop(" 7.5 % "), "7.5%")
        self.assertEqual(pw.parse_min_drop(""), "")
        for bad in ("abc", "-5", "0", "150%"):
            with self.assertRaises(ValueError):
                pw.parse_min_drop(bad)

    def test_failed_then_same_price_is_not_a_drop(self):
        it = item()
        pw.apply_result(it, ok(7999))
        pw.apply_result(it, Result("network", "timed out"))
        self.assertEqual(pw.apply_result(it, ok(7999)), [])


class Targets(unittest.TestCase):
    def test_target_fires_once_and_resets(self):
        it = item()
        pw.apply_result(it, ok(7999))
        it["target"] = 9000  # target set above the current price
        self.assertEqual(kinds(pw.apply_result(it, ok(7999))), ["target"])
        self.assertEqual(pw.apply_result(it, ok(7999)), [])  # does not repeat
        pw.apply_result(it, ok(9999))  # back above target
        self.assertEqual(kinds(pw.apply_result(it, ok(9999))), [])
        alerts = pw.apply_result(it, ok(8999))
        self.assertEqual(kinds(alerts), ["drop"])  # a drop that reaches the target again
        self.assertIn("target", alerts[0]["message"])


class Editions(unittest.TestCase):
    def test_each_edition_tracked(self):
        eds = [pw.new_edition("ps:STD", "Standard Edition"), pw.new_edition("ps:DLX", "Deluxe Edition")]
        it = item(editions=eds)
        pw.apply_result(it, ok(8999, 11999, keys=["ps:STD", "ps:DLX"]))
        alerts = pw.apply_result(it, ok(8999, 9999, keys=["ps:STD", "ps:DLX"]))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["title"], "Price drop: Game (Deluxe Edition)")

    def test_missing_edition(self):
        eds = [pw.new_edition("ps:STD", "Standard Edition"), pw.new_edition("ps:DLX", "Deluxe Edition")]
        it = item(editions=eds)
        pw.apply_result(it, ok(8999, keys=["ps:STD"]))
        self.assertIn("not listed anymore: Deluxe Edition", it["status"])

    def test_all_editions_missing_is_a_failure(self):
        it = item(editions=[pw.new_edition("ps:STD", "Standard Edition")])
        alerts = pw.apply_result(it, ok(100, 200, keys=["ps:A", "ps:B"]))
        self.assertEqual(kinds(alerts), ["problem"])

    def test_seller_change_mentioned(self):
        it = item()
        pw.apply_result(it, ok(10000, seller="A"))
        alerts = pw.apply_result(it, ok(9000, seller="B"))
        self.assertIn("Seller changed from A to B", alerts[0]["message"])


class Failures(unittest.TestCase):
    def test_not_found_pings_once_then_recovers(self):
        it = item()
        pw.apply_result(it, ok(7999))
        self.assertEqual(kinds(pw.apply_result(it, Result("not_found", "no price found on the page"))),
                         ["problem"])
        self.assertEqual(pw.apply_result(it, Result("not_found", "no price found on the page")), [])
        self.assertEqual(kinds(pw.apply_result(it, ok(7999))), ["recovered"])
        self.assertEqual(it["consecutive_failures"], 0)

    def test_network_waits_for_six(self):
        it = item()
        for _ in range(5):
            self.assertEqual(pw.apply_result(it, Result("network", "timed out")), [])
        self.assertEqual(kinds(pw.apply_result(it, Result("network", "timed out"))), ["problem"])

    def test_saved_without_price_does_not_ping(self):
        it = item()
        it["mute_until_working"] = True  # what the web UI sets when saving an item that had no price
        self.assertEqual(pw.apply_result(it, Result("not_found", "no price")), [])
        self.assertEqual(pw.apply_result(it, ok(100)), [])  # no "working again" either

    def test_suspicious(self):
        it = item()
        pw.apply_result(it, ok(10000))
        self.assertTrue(pw.looks_suspicious(it, ok(3000)))
        self.assertFalse(pw.looks_suspicious(it, ok(9000)))
        self.assertTrue(pw.looks_suspicious(it, ok(9000, method="text")))
        self.assertFalse(pw.looks_suspicious(it, ok(9000, method="steam-api (chrome-like)")))


class QuietHours(unittest.TestCase):
    def at(self, hh, mm=0):
        return datetime(2026, 9, 23, hh, mm, tzinfo=config.TZ)

    def test_overnight(self):
        s = {"quiet_start": "23:00", "quiet_end": "07:00"}
        self.assertTrue(pw.in_quiet_hours(s, self.at(23, 30)))
        self.assertTrue(pw.in_quiet_hours(s, self.at(3)))
        self.assertFalse(pw.in_quiet_hours(s, self.at(7)))
        self.assertFalse(pw.in_quiet_hours(s, self.at(12)))

    def test_daytime_and_blank(self):
        self.assertTrue(pw.in_quiet_hours({"quiet_start": "09:00", "quiet_end": "17:00"}, self.at(12)))
        self.assertFalse(pw.in_quiet_hours({"quiet_start": "", "quiet_end": ""}, self.at(12)))


def stock(price, in_stock):
    return Result("ok", "ok", method="shopify", offers=[Offer(price=price, currency="USD", in_stock=in_stock)])


class BackInStock(unittest.TestCase):
    def test_alerts_once_when_back(self):
        it = item()
        it["stock_alert"] = True
        self.assertEqual(pw.apply_result(it, stock(20000, False)), [])
        self.assertEqual(pw.apply_result(it, stock(20000, False)), [])
        alerts = pw.apply_result(it, stock(20000, True))
        self.assertEqual(kinds(alerts), ["stock"])
        self.assertEqual(alerts[0]["title"], "Back in stock: Game")
        self.assertIn("$200.00 USD", alerts[0]["message"])
        self.assertTrue(alerts[0]["urgent"])
        self.assertEqual(pw.apply_result(it, stock(20000, True)), [])  # stays in stock: quiet

    def test_shopify_alerts_get_cart_buttons(self):
        it = pw.new_item("https://gear.bethesda.net/products/lucky-38-poker-set", "Poker Set",
                         [pw.new_edition("shopify:29982703845491", "Poker Set")])
        it["stock_alert"] = True
        pw.apply_result(it, Result("ok", "ok", offers=[Offer(key="shopify:29982703845491", price=20000, in_stock=False)]))
        alert = pw.apply_result(it, Result("ok", "ok", offers=[Offer(key="shopify:29982703845491", price=20000, in_stock=True)]))[0]
        self.assertEqual(alert["actions"], [
            ("Buy now", "https://gear.bethesda.net/cart/29982703845491:1"),
            ("Add to cart", "https://gear.bethesda.net/cart/29982703845491:1?storefront=true"),
            ("Open page", "https://gear.bethesda.net/products/lucky-38-poker-set")])

    def test_other_stores_get_no_cart_buttons(self):
        it = item()
        it["stock_alert"] = True
        pw.apply_result(it, stock(20000, False))
        self.assertEqual(pw.apply_result(it, stock(20000, True))[0]["actions"], [])

    def test_needs_the_switch(self):
        it = item()
        pw.apply_result(it, stock(20000, False))
        self.assertEqual(pw.apply_result(it, stock(20000, True)), [])

    def test_in_stock_when_added_does_not_alert(self):
        it = item()
        it["stock_alert"] = True
        self.assertEqual(pw.apply_result(it, stock(20000, True)), [])

    def test_unknown_stock_does_not_alert(self):
        it = item()
        it["stock_alert"] = True
        pw.apply_result(it, stock(20000, None))
        self.assertEqual(pw.apply_result(it, stock(20000, True)), [])

    def test_back_in_stock_without_price(self):
        it = item()
        it["stock_alert"] = True
        pw.apply_result(it, Result("no_price", "sold out", offers=[Offer(in_stock=False)]))
        self.assertEqual(kinds(pw.apply_result(it, Result("ok", "ok", offers=[Offer(price=100, in_stock=True)]))), ["stock"])

    def test_quiet_hours_let_stock_through_only_if_allowed(self):
        quiet = quiet_now()
        stock_alert = {"urgent": True}
        self.assertTrue(pw.should_hold(stock_alert, quiet))
        self.assertFalse(pw.should_hold(stock_alert, {**quiet, "quiet_allow_stock": True}))
        self.assertTrue(pw.should_hold({"urgent": False}, {**quiet, "quiet_allow_stock": True}))
        snooze = {"snooze_until": datetime(2099, 1, 1, tzinfo=config.TZ).isoformat(), "quiet_allow_stock": True}
        self.assertTrue(pw.should_hold(stock_alert, snooze))  # snooze holds everything


class MuteAndSnooze(unittest.TestCase):
    def test_muted_item_updates_but_does_not_alert(self):
        it = item()
        pw.apply_result(it, ok(7999))
        it["muted"] = True
        self.assertEqual(pw.apply_result(it, ok(5999)), [])
        self.assertEqual(it["editions"][0]["last_good_price"], 5999)

    def test_snooze_holds(self):
        later = (datetime.now(config.TZ).replace(microsecond=0)).isoformat()
        self.assertFalse(pw.snoozed({"snooze_until": later}))  # already over
        future = datetime(2099, 1, 1, tzinfo=config.TZ).isoformat()
        self.assertTrue(pw.holding({"snooze_until": future}))
        self.assertFalse(pw.in_quiet_hours({"quiet_enabled": False, **quiet_now()}))


class Flushing(unittest.TestCase):
    def setUp(self):
        for p in TMP.glob("*"):
            p.unlink()

    def seed(self, quiet, *alerts):
        with store.update() as data:
            data["settings"] = quiet
            it = item(editions=[pw.new_edition("ps:DLX", "Deluxe Edition")])
            it["editions"][0]["price"] = 9999
            it["id"] = "abc"
            data["items"] = [it]
            data["pending_alerts"] = list(alerts)
        return it

    def make(self, kind, title, held):
        a = pw._alert({"id": "abc"}, "ps:DLX", kind, title, "Now $99.99 (was $119.99).", "u", 4)
        a["held"] = held
        return a

    def test_held_alerts_wait_then_send_one_summary(self):
        quiet = quiet_now()
        self.seed(quiet, self.make("drop", "Price drop: A", True), self.make("drop", "Price drop: B", True))
        with mock.patch("notify.send", return_value=True) as send:
            pw.flush_alerts()
            send.assert_not_called()  # still quiet hours
            with store.update() as data:
                data["settings"] = {"quiet_start": "", "quiet_end": ""}
            pw.flush_alerts()
        self.assertEqual(send.call_count, 1)
        title, message = send.call_args[0]
        self.assertIn("1 held alert", title)  # same item edition: newest kept
        self.assertIn("Price drop: B", message)
        self.assertIn("Now $99.99.", message)
        self.assertEqual(store.load()["pending_alerts"], [])

    def test_failed_send_stays_queued(self):
        self.seed({}, self.make("drop", "Price drop: A", False))
        with mock.patch("notify.send", return_value=False):
            pw.flush_alerts()
        self.assertEqual(len(store.load()["pending_alerts"]), 1)
        with mock.patch("notify.send", return_value=True):
            pw.flush_alerts()
        self.assertEqual(store.load()["pending_alerts"], [])

    def test_atomic_write_keeps_backup(self):
        with store.update() as data:
            data["items"] = []
        with store.update() as data:
            data["items"] = [item()]
        self.assertTrue(Path(os.environ["PW_DATA_FILE"] + ".bak").exists())
        self.assertEqual(len(store.load()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
