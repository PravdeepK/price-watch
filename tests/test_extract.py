"""
Tests for the price readers, using pages saved in tests/fixtures.

Run from the project folder:  .venv/bin/python -m unittest -v
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract  # noqa: E402
from extract import Page, canada_check, check, to_cents  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fake_fetch(mapping):
    """A fetch function that serves saved files instead of the internet."""

    def fetch(url, client="requests", **kw):
        fetch.clients.append(client)
        for prefix, (name, status) in mapping.items():
            if url.startswith(prefix):
                return Page(url=url, status=status, text=(FIXTURES / name).read_text(), client=client)
        raise AssertionError(f"unexpected fetch: {url}")

    fetch.clients = []

    return fetch


def prices(result):
    return {o.label: o.price for o in result.offers}


class PriceText(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(to_cents("$1,299.99"), 129999)
        self.assertEqual(to_cents("1 299,99 $"), 129999)
        self.assertEqual(to_cents("1 299,99 $"), 129999)
        self.assertEqual(to_cents("CDN$ 79.99"), 7999)
        self.assertEqual(to_cents("$89"), 8900)
        self.assertEqual(to_cents("12,5"), 1250)
        self.assertEqual(to_cents("$ 169 .99"), 16999)
        self.assertEqual(to_cents(379.99), 37999)
        self.assertEqual(to_cents(228), 22800)
        self.assertIsNone(to_cents("Unavailable"))
        self.assertIsNone(to_cents(None))

    def test_currency(self):
        self.assertEqual(extract.currency_from_text("CDN$ 79.99", False), "CAD")
        self.assertEqual(extract.currency_from_text("US$ 10", True), "USD")
        self.assertEqual(extract.currency_from_text("$10", True), "CAD")
        self.assertIsNone(extract.currency_from_text("$10", False))


class CanadaCheck(unittest.TestCase):
    def test_canadian(self):
        for url in ("https://www.bestbuy.ca/en-ca/product/x/19832007",
                    "https://www.playstation.com/en-ca/games/lego-batman-legacy-of-the-dark-knight/",
                    "https://store.steampowered.com/app/311210/x/",
                    "https://www.walmart.ca/en/ip/x/6000204597271"):
            self.assertEqual(canada_check(url)[0], "yes", url)

    def test_not_canadian(self):
        verdict, message, fixed = canada_check("https://direct.playstation.com/en-us/buy-consoles/ps5")
        self.assertEqual(verdict, "no")
        self.assertIn("does not sell to Canada", message)
        verdict, _, fixed = canada_check("https://www.bestbuy.com/site/x/123.p")
        self.assertEqual((verdict, fixed), ("no", "https://www.bestbuy.ca/site/x/123.p"))
        verdict, _, fixed = canada_check("https://store.playstation.com/en-us/product/ABC")
        self.assertEqual((verdict, fixed), ("no", "https://store.playstation.com/en-ca/product/ABC"))
        verdict, _, fixed = canada_check("https://www.playstation.com/fr-fr/games/x/")
        self.assertEqual(fixed, "https://www.playstation.com/fr-ca/games/x/")

    def test_unknown(self):
        self.assertEqual(canada_check("https://shop.example.com/item/1")[0], "unknown")

    def test_track_anyway(self):
        fetch = fake_fetch({"https://direct.playstation.com/": ("psdirect_us.html", 200)})
        url = "https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console-2-tb"
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("23.45.67.89", 0))]):
            result = check(url, fetch=fetch, allow_foreign=True)
        self.assertEqual(result.kind, "ok")
        self.assertEqual((result.offers[0].price, result.offers[0].currency), (89900, "USD"))

    def test_check_refuses_without_fetching(self):
        result = check("https://direct.playstation.com/en-us/buy-consoles/ps5",
                       fetch=fake_fetch({}))
        self.assertEqual(result.kind, "not_canadian")


class AddressCheck(unittest.TestCase):
    def fake_resolve(self, address):
        return mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (address, 0))])

    def test_private_addresses_refused(self):
        for address in ("192.168.86.1", "10.0.0.1", "127.0.0.1", "169.254.1.1",
                        "100.101.102.103", "::1", "fd7a:115c:a1e0::1"):
            with self.fake_resolve(address):
                with self.assertRaises(extract.FetchError) as e:
                    extract.check_host("example.com")
                self.assertEqual(e.exception.kind, "bad_url", address)

    def test_pihole(self):
        with self.fake_resolve("0.0.0.0"):
            with self.assertRaises(extract.FetchError) as e:
                extract.check_host("tracker.example.com")
            self.assertEqual(e.exception.kind, "pihole")

    def test_public_ok(self):
        with self.fake_resolve("23.45.67.89"):
            extract.check_host("example.com")

    def test_bad_urls(self):
        for url in ("ftp://example.com/", "http://user:pw@example.com/", "http://example.com:8080/",
                    "javascript:alert(1)"):
            with self.assertRaises(extract.FetchError):
                extract.validate_url(url)


class Sites(unittest.TestCase):
    def test_steam_editions(self):
        fetch = fake_fetch({"https://store.steampowered.com/api/appdetails": ("steam_311210.json", 200)})
        result = check("https://store.steampowered.com/app/311210/Call_of_Duty_Black_Ops_III/", fetch=fetch)
        self.assertEqual(result.kind, "ok")
        self.assertEqual(result.method, "steam-api")
        self.assertEqual(prices(result), {
            "Zombies Chronicles Edition": 7999,
            "Zombies Deluxe": 12999,
            "Multiplayer Starter Pack": 1899,
        })
        self.assertTrue(all(o.currency == "CAD" for o in result.offers))

    def test_steam_answer_under_another_label(self):
        data = json.loads((FIXTURES / "steam_311210.json").read_text())
        relabelled = json.dumps({"830461": data["311210"]})
        result = check("https://store.steampowered.com/app/311210/x/",
                       fetch=lambda url, client="requests", **kw: Page(url, 200, relabelled))
        self.assertEqual((result.kind, result.offers[0].price), ("ok", 7999))

    def steam_json(self, data):
        return lambda url, client="requests", **kw: Page(url, 200, json.dumps({"1": {"success": True, "data": data}}))

    def test_steam_free_and_unreleased(self):
        url = "https://store.steampowered.com/app/1/x/"
        free = check(url, fetch=self.steam_json({"name": "Dota 2", "is_free": True, "package_groups": [
            {"subs": [{"packageid": 197846, "option_text": "Commercial License - Free", "is_free_license": True}]}]}))
        self.assertEqual((free.kind, free.offers[0].price, free.offers[0].label), ("ok", 0, "Dota 2"))
        unreleased = check(url, fetch=self.steam_json({"name": "Future Game", "is_free": False}))
        self.assertEqual(unreleased.kind, "no_price")
        self.assertIn("no price yet", unreleased.status)

    def test_bestbuy(self):
        fetch = fake_fetch({
            "https://www.bestbuy.ca/api/offers/": ("bestbuy_offers_19832007.json", 200),
            "https://www.bestbuy.ca/api/v2/json/product/": ("bestbuy_product_19832007.json", 200),
            "https://www.bestbuy.ca/ecomm-api/availability/": ("bestbuy_availability_19832007.json", 200),
        })
        result = check("https://www.bestbuy.ca/en-ca/product/samsung-990-pro-ssd-1tb/19832007",
                       details=True, fetch=fetch)
        self.assertEqual(result.kind, "ok")
        offer = result.offers[0]
        self.assertEqual((offer.price, offer.regular, offer.currency), (37999, 49999, "CAD"))
        self.assertEqual(offer.seller, "North Deals")
        self.assertIn("marketplace", offer.note)
        self.assertTrue(result.name.startswith("SAMSUNG 990 PRO"))
        self.assertTrue(offer.in_stock)

    def test_bestbuy_page_is_blocked(self):
        page = Page(url="x", status=403, text=(FIXTURES / "bestbuy_page_blocked.html").read_text())
        self.assertIn("blocked", extract.blocked_reason(page))

    def test_playstation_game_page(self):
        fetch = fake_fetch({"https://www.playstation.com/": ("psgame_legobatman.html", 200)})
        result = check("https://www.playstation.com/en-ca/games/lego-batman-legacy-of-the-dark-knight/",
                       fetch=fetch)
        self.assertEqual(result.kind, "ok")
        self.assertEqual(result.method, "playstation")
        suggested = {o.label: o.price for o in result.offers if o.suggested}
        self.assertEqual(suggested, {"Standard Edition": 8999, "Deluxe Edition": 11999})
        self.assertEqual(result.offers[0].label, "Standard Edition")
        addons = {o.label: o.price for o in result.offers if not o.suggested}
        self.assertEqual(addons, {"Deluxe Edition Upgrade": 3349})
        self.assertIn("LEGO", result.name)

    def test_playstation_store_page(self):
        fetch = fake_fetch({"https://store.playstation.com/": ("psstore_LEGOBATMANDLXEDT.html", 200)})
        result = check("https://store.playstation.com/en-ca/product/UP1018-PPSA16833_00-LEGOBATMANDLXEDT",
                       fetch=fetch)
        self.assertEqual(result.kind, "ok")
        self.assertEqual({o.key: o.price for o in result.offers if o.suggested}, {
            "ps:UP1018-PPSA16833_00-LEGOBATMAN000000": 8999,
            "ps:UP1018-PPSA16833_00-LEGOBATMANDLXEDT": 11999,
        })

    def test_walmart(self):
        fetch = fake_fetch({"https://www.walmart.ca/": ("walmart_6000204597271.html", 200)})
        with mock.patch("extract.chrome_like_available", return_value=False):  # curl_cffi not installed
            result = check("https://www.walmart.ca/en/ip/onn-43-4K-UHD-HDR10-Smart-Roku-TV/6000204597271",
                           fetch=fetch)
        self.assertEqual(result.kind, "ok")
        self.assertEqual(result.method, "walmart")
        self.assertEqual((result.offers[0].price, result.offers[0].currency), (22800, "CAD"))
        self.assertTrue(result.offers[0].in_stock)

    def test_newegg(self):
        fetch = fake_fetch({"https://www.newegg.ca/": ("newegg_N82E16824281271.html", 200)})
        with mock.patch("extract.chrome_like_available", return_value=True):
            result = check("https://www.newegg.ca/asus-vg27aq3a/p/N82E16824281271?Item=N82E16824281271",
                           fetch=fetch)
        self.assertEqual(fetch.clients, ["chrome-like"])
        self.assertEqual(result.method, "newegg (chrome-like)")
        offer = result.offers[0]
        self.assertEqual((offer.price, offer.regular, offer.currency), (16999, 29999, "CAD"))
        self.assertIn("ASUS TUF Gaming", result.name)
        self.assertTrue(offer.in_stock)

    def test_walmart_chrome_like(self):
        fetch = fake_fetch({"https://www.walmart.ca/": ("walmart_cffi_6000204597271.html", 200)})
        with mock.patch("extract.chrome_like_available", return_value=True):
            result = check("https://www.walmart.ca/en/ip/x/6000204597271", fetch=fetch)
        self.assertEqual((fetch.clients, result.method, result.offers[0].price),
                         (["chrome-like"], "walmart (chrome-like)", 22800))

    def test_blocked_unknown_site_retries_chrome_like(self):
        pages = {"requests": Page("u", 403, "<title>Access Denied</title>"),
                 "chrome-like": Page("u", 200, '<meta property="product:price:amount" content="12.50">',
                                     client="chrome-like")}
        calls = []

        def fetch(url, client="requests", **kw):
            calls.append(client)
            return pages[client]
        with mock.patch("extract.chrome_like_available", return_value=True):
            result = check("https://shop.example.ca/a", fetch=fetch)
        self.assertEqual(calls, ["requests", "chrome-like"])
        self.assertEqual((result.method, result.offers[0].price), ("meta (chrome-like)", 1250))

    def test_memoryexpress(self):
        fetch = fake_fetch({"https://www.memoryexpress.com/": ("memoryexpress_MX00123889.html", 200)})
        with mock.patch("extract.chrome_like_available", return_value=True):
            result = check("https://www.memoryexpress.com/Products/MX00123889", fetch=fetch)
        offer = result.offers[0]
        self.assertEqual((result.method, offer.price, offer.regular, offer.currency),
                         ("memoryexpress (chrome-like)", 25999, 43998, "CAD"))
        self.assertIn("sale ends", offer.note)

    def test_amazon(self):
        html = ('<html><span id="productTitle"> Switch 2 Pro Controller </span><div id="corePrice_feature_div">'
                '<span class="priceToPay"><span class="a-offscreen">$109.00</span></span></div>'
                '<div class="a-carousel"><span class="a-offscreen">$14.99</span></div></html>')
        result = check("https://www.amazon.ca/dp/B0F3VTFD6J", fetch=lambda url, client="requests", **kw: Page(url, 200, html))
        self.assertEqual((result.method, result.offers[0].price, result.name), ("amazon", 10900, "Switch 2 Pro Controller"))

    def test_json_ld_price_varies_by_option(self):
        html = ('<script type="application/ld+json">{"@type":"Product","offers":['
                '{"@type":"Offer","price":"138","priceCurrency":"CAD"},{"@type":"Offer","price":"89","priceCurrency":"CAD"}]}</script>')
        result = check("https://shop.example.ca/jogger", fetch=lambda url, client="requests", **kw: Page(url, 200, html))
        self.assertEqual(result.offers[0].price, 8900)
        self.assertIn("varies", result.offers[0].note)
        two_colours = html.replace('"138"', '"99"').replace("</script>", "</script>") + \
            '<script type="application/ld+json">{"@type":"Product","offers":{"@type":"Offer","price":"69","priceCurrency":"CAD"}}</script>'
        result = check("https://shop.example.ca/jogger", fetch=lambda url, client="requests", **kw: Page(url, 200, two_colours))
        self.assertEqual(result.offers[0].price, 6900)

    def test_nothing_found_retries_chrome_like(self):
        pages = {"requests": Page("u", 200, "<html>Loading...</html>"),
                 "chrome-like": Page("u", 200, '<meta property="product:price:amount" content="99.99">', client="chrome-like")}
        calls = []

        def fetch(url, client="requests", **kw):
            calls.append(client)
            return pages[client]
        with mock.patch("extract.chrome_like_available", return_value=True):
            result = check("https://shop.example.ca/a", fetch=fetch)
        self.assertEqual((calls, result.offers[0].price), (["requests", "chrome-like"], 9999))

    def test_canadian_com_stores(self):
        for url in ("https://www.canadacomputers.com/en/x/246793/y.html", "https://www.memoryexpress.com/Products/MX1",
                    "https://www.ikea.com/ca/en/p/billy-bookcase-white-10522037/"):
            self.assertEqual(canada_check(url)[0], "yes", url)

    def test_shopify_out_of_stock_in_usd(self):
        fetch = fake_fetch({
            "https://gear.bethesda.net/products/lucky-38-poker-set.js": ("shopify_bethesda_lucky38.js.json", 200),
            "https://gear.bethesda.net/": ("shopify_bethesda_lucky38.html", 200),
        })
        url = "https://gear.bethesda.net/products/lucky-38-poker-set"
        self.assertEqual(check(url, fetch=fetch).kind, "not_canadian")  # a US store, priced in USD
        result = check(url, fetch=fetch, allow_foreign=True, details=True)
        offer = result.offers[0]
        self.assertEqual((result.method, result.name, offer.price, offer.currency, offer.in_stock),
                         ("shopify", "Fallout Lucky 38 Poker Set", 20000, "USD", False))
        self.assertTrue(result.image.startswith("https://"))

    def test_stock_words(self):
        for value, want in (("https://schema.org/InStock", True), ("http://schema.org/OutOfStock", False),
                            ("PreOrder", True), ("SoldOut", False), ("IN_STOCK", True), ("OUT_OF_STOCK", False),
                            ("", None), ("Discontinued", False)):
            self.assertEqual(extract.stock_from_text(value), want, value)

    def test_canadiantire(self):
        fetch = fake_fetch({
            "https://www.canadiantire.ca/api/v1/product/api/v2/product/productFamily/": ("canadiantire_family_0439582p.json", 206),
            "https://www.canadiantire.ca/api/v1/product/api/v2/product/sku/PriceAvailability": ("canadiantire_price_0439582.json", 200),
        })
        result = check("https://www.canadiantire.ca/en/pdp/ninja-non-stick-air-fryer-with-cool-touch-handle-4-qt-0439582p.html",
                       fetch=fetch, details=True)
        offer = result.offers[0]
        self.assertEqual((result.kind, result.method, offer.price, offer.currency, offer.in_stock),
                         ("ok", "canadiantire-api", 15999, "CAD", True))
        self.assertIn("Ninja", result.name)

    def test_newegg_captcha(self):
        fetch = fake_fetch({"https://www.newegg.ca/": ("newegg_captcha.html", 403)})
        result = check("https://www.newegg.ca/asus-vg27aq3a/p/N82E16824281271", fetch=fetch)
        self.assertEqual(result.kind, "blocked")

    def test_walmart_perimeterx_block(self):
        fetch = fake_fetch({"https://www.walmart.ca/": ("walmart_blocked_412.json", 412)})
        result = check("https://www.walmart.ca/en/ip/x/6000204597271", fetch=fetch)
        self.assertEqual(result.kind, "blocked")
        self.assertIn("PerimeterX", result.status)

    def test_selector(self):
        fetch = fake_fetch({"https://www.walmart.ca/": ("walmart_6000204597271.html", 200)})
        url = "https://www.walmart.ca/en/ip/x/6000204597271"
        result = check(url, selector="#does-not-exist", fetch=fetch)
        self.assertIn("did not match", result.status)

    def test_selector_on_text(self):
        html = '<html><body><div class="p">Sold out</div><span id="x">1 299,99 $</span></body></html>'
        fetch = lambda url, client="requests", **kw: Page(url=url, status=200, text=html)  # noqa: E731
        result = check("https://shop.example.ca/a", selector=".p", fetch=fetch)
        self.assertIn("not a price", result.status)
        result = check("https://shop.example.ca/a", selector="#x", fetch=fetch)
        self.assertEqual((result.method, result.offers[0].price), ("selector", 129999))

    def test_generic_json_ld_and_usd(self):
        html = ('<html><head><title>Thing</title><script type="application/ld+json">'
                '{"@type":"Product","name":"Thing","offers":{"@type":"Offer","price":"49.50",'
                '"priceCurrency":"USD"}}</script></head><body>$10 /mo</body></html>')
        fetch = lambda url, client="requests", **kw: Page(url=url, status=200, text=html)  # noqa: E731
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("23.45.67.89", 0))]):
            result = check("https://shop.example.com/thing", fetch=fetch)
        self.assertEqual(result.kind, "not_canadian")

    def test_generic_text_skips_financing(self):
        html = "<html><body><p>or $25.00/mo with financing</p><p>Save $50</p><p>Price: $199.99</p></body></html>"
        fetch = lambda url, client="requests", **kw: Page(url=url, status=200, text=html)  # noqa: E731
        result = check("https://shop.example.ca/thing", fetch=fetch)
        self.assertEqual((result.method, result.offers[0].price, result.offers[0].currency),
                         ("text", 19999, "CAD"))


if __name__ == "__main__":
    unittest.main()
