"""
Site-specific price readers.

To support a new store, write one function and add it to API_SITES or
PAGE_SITES at the bottom of this file, then add a saved page to
tests/fixtures and a test for it.

- API readers take (url, fetch, details) and return a Result, or None to let
  the normal page chain handle the URL. They fetch JSON instead of the page.
  `details` is True when the web UI is testing a new link, so the reader may
  make an extra request for the product name.
- Page readers take (page, soup, url) and return a Result, or None to let the
  generic methods (JSON-LD, meta tags, text) try the same page.
"""

import html
import json
import re

import config
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from extract import FetchError, Offer, Result, clean_image, require_ok, stock_from_text, to_cents

TORONTO = ZoneInfo("America/Toronto")


def find(registry, host):
    host = host.lower()
    for domain, func in registry.items():
        if host == domain or host.endswith("." + domain):
            return func
    return None


def _plain(text):
    """Remove the trademark signs that make names hard to compare."""
    return re.sub(r"[®™©]", "", text or "").strip()


def _short_label(label, game_name):
    """'Game Name - Deluxe Edition' becomes 'Deluxe Edition'."""
    plain, game = _plain(label), _plain(game_name)
    if game and plain.lower().startswith(game.lower()):
        rest = plain[len(game):].lstrip(" -:–")
        if rest:
            return rest
    return plain


def _json_or_fail(page, what):
    try:
        return page.json()
    except ValueError:
        raise FetchError("not_found", f"{what} returned something unexpected (its data format may have changed)")


# ---------------------------------------------------------------------------
# Steam: the store's JSON endpoint, never the HTML.
# ---------------------------------------------------------------------------


def steam(url, fetch, details):
    m = re.search(r"/app/(\d+)", urlparse(url).path)
    if not m:
        return None  # bundle or package links: fall back to the page chain
    appid = m.group(1)
    page = fetch(f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=ca&l=english")
    require_ok(page, "Steam")
    entry = _steam_entry(_json_or_fail(page, "Steam"), appid)
    if not entry.get("success"):
        return Result("not_found", "Steam has no store data for this game in Canada", method="steam-api")
    data = entry.get("data") or {}
    name = data.get("name", "")
    image = clean_image(data.get("header_image") or "")
    overview = data.get("price_overview") or {}
    currency = overview.get("currency") or "CAD"

    if data.get("is_free"):  # free-to-play: package names like "Commercial License" mean nothing here
        return Result("ok", "ok", method="steam-api", name=name, image=image,
                      offers=[Offer(label=name, price=0, currency=currency, note="free to play")])

    offers = []
    for group in data.get("package_groups") or []:
        for sub in group.get("subs") or []:
            text = html.unescape(sub.get("option_text") or "")
            original = re.search(r'discount_original_price">([^<]+)<', text)
            text = re.sub(r"<[^>]+>", " ", text)
            label = re.sub(r"\s*-\s*(CDN\$|\$|Free).*$", "", text).replace("(RoW)", "")
            label = _short_label(re.sub(r"\s+", " ", label).strip(), name)
            cents = 0 if sub.get("is_free_license") else sub.get("price_in_cents_with_discount")
            savings = sub.get("percent_savings") or 0
            offers.append(Offer(
                key=f"steam-sub:{sub.get('packageid')}",
                label=label,
                price=cents,
                currency=currency,
                regular=to_cents(original.group(1)) if original else None,
                note=f"{savings}% off" if savings else "",
            ))

    if not offers:
        if overview:
            offers = [Offer(
                label=name,
                price=overview.get("final"),
                regular=overview.get("initial"),
                currency=currency,
                note=f"{overview['discount_percent']}% off" if overview.get("discount_percent") else "",
            )]
        else:
            return Result("no_price", "no price yet (not released, or not sold in Canada)",
                          method="steam-api", name=name, image=image)
    return Result("ok", "ok", method="steam-api", offers=offers, name=name, image=image)


# ---------------------------------------------------------------------------
# Best Buy Canada: the product page blocks automated requests, so use the
# offers endpoint the page itself uses, keyed by the SKU at the end of the URL.
# ---------------------------------------------------------------------------


def _steam_entry(answer, appid):
    """The answer for this game. Steam normally labels it with the game's number, but
    sometimes uses another number (seen September 2026: 311210 came back as "830461"),
    so fall back to the entry whose data says it is this game, or the only entry."""
    if not isinstance(answer, dict):
        return {}
    if isinstance(answer.get(appid), dict):
        return answer[appid]
    entries = [e for e in answer.values() if isinstance(e, dict)]
    for e in entries:
        if str((e.get("data") or {}).get("steam_appid")) == appid:
            return e
    return entries[0] if len(entries) == 1 else {}


def bestbuy(url, fetch, details):
    m = re.search(r"(\d{7,9})(?:\.aspx)?/?$", urlparse(url).path)
    if not m:
        return Result("not_found", "could not find the Best Buy product number (SKU) in the link",
                      method="bestbuy-api")
    sku = m.group(1)
    page = fetch(f"https://www.bestbuy.ca/api/offers/v1/products/{sku}/offers")
    require_ok(page, "Best Buy")
    offers = _json_or_fail(page, "Best Buy")
    if not isinstance(offers, list):
        raise FetchError("not_found", "Best Buy returned something unexpected (its data format may have changed)")

    name = image = ""
    if details:
        product = fetch(f"https://www.bestbuy.ca/api/v2/json/product/{sku}?currentRegion=ON&lang=en-CA")
        if product.status == 200:
            try:
                info = product.json()
                name = info.get("name", "")
                media = info.get("additionalMedia") or []
                image = clean_image(info.get("highResImage") or (media[0].get("url") if media else "") or "")
            except (ValueError, AttributeError):
                pass

    if not offers:
        return Result("no_price", "no seller is offering this right now (sold out or removed)",
                      method="bestbuy-api", name=name, image=image,
                      offers=[Offer(label=name, currency="CAD", in_stock=False)])
    win = next((o for o in offers if o.get("isWinner")), offers[0])
    in_stock = _bestbuy_stock(sku, fetch)
    regular = to_cents(win.get("regularPrice"))
    sale = to_cents(win.get("salePrice"))
    price = sale if win.get("isOnSale") and sale else regular

    notes = []
    seller = ""
    if win.get("isMarketplace"):
        seller = win.get("sellerNameEn") or ""
        notes.append(f"sold by {seller} (marketplace seller)")
    if win.get("isOnSale") and win.get("saleEndDate"):
        try:
            end = datetime.fromisoformat(win["saleEndDate"].replace("Z", "+00:00")).astimezone(TORONTO)
            notes.append(f"sale ends {end:%b %-d}")
        except ValueError:
            pass
    return Result("ok", "ok", method="bestbuy-api", name=name, image=image, offers=[Offer(
        label=name, price=price, currency="CAD",
        regular=regular if regular and price and regular > price else None,
        note="; ".join(notes), seller=seller, in_stock=in_stock,
    )])


def _bestbuy_stock(sku, fetch):
    """Whether Best Buy will ship it now, from the availability data its page uses. None if unknown."""
    try:
        page = fetch("https://www.bestbuy.ca/ecomm-api/availability/products?accept=application%2Fvnd.bestbuy."
                     f"standardproduct.v1%2Bjson&accept-language=en-CA&skus={sku}")
        if page.status != 200:
            return None
        shipping = (page.json().get("availabilities") or [{}])[0].get("shipping") or {}
        return bool(shipping["purchasable"]) if "purchasable" in shipping else None
    except (FetchError, ValueError, AttributeError, IndexError):
        return None


# ---------------------------------------------------------------------------
# PlayStation: game pages (playstation.com/en-ca/games/...) and Store pages
# (store.playstation.com/en-ca/product/...) both embed the Store's data cache.
# Each edition is its own product. We track the regular (non PS Plus) price,
# and use the sale price when there is one.
# ---------------------------------------------------------------------------


def _ps_cache(page_text):
    merged = {}
    for m in re.finditer(r'<script id="env:[^"]+" type="application/json">(.*?)</script>', page_text, re.S):
        try:
            blob = json.loads(m.group(1))
        except ValueError:
            continue
        cache = blob.get("cache") if isinstance(blob, dict) else None
        if isinstance(cache, dict):
            for key, entry in cache.items():
                if isinstance(entry, dict):
                    merged.setdefault(key, {}).update(entry)
    return merged


def playstation(page, soup, url):
    cache = _ps_cache(page.text)
    if not cache:
        return None

    # Prices from the buy buttons, in cents, per product.
    regular_price, plus_price = {}, {}
    for key, entry in cache.items():
        if not key.startswith("GameCTA:"):
            continue
        detail = ((entry.get("local") or {}).get("telemetryMeta") or {}).get("skuDetail") or {}
        sku = detail.get("skuId") or ""
        product_id = sku.rsplit("-", 1)[0]
        if entry.get("type") not in ("ADD_TO_CART", "PREORDER", "PURCHASE", "BUY_NOW"):
            continue
        for d in detail.get("skuPriceDetail") or []:
            if d.get("priceCurrencyCode") and d.get("discountPriceValue") is not None:
                pair = (d["discountPriceValue"], d.get("originalPriceValue"), d["priceCurrencyCode"])
                if d.get("offerBranding") in (None, "NONE") and not d.get("offerIsTiedToSubscription"):
                    if product_id not in regular_price or pair[0] < regular_price[product_id][0]:
                        regular_price[product_id] = pair
                elif d.get("offerBranding") == "PS_PLUS" and d["discountPriceValue"] > 0:
                    plus_price[product_id] = pair

    game_name = ""
    for key, entry in cache.items():
        if key.startswith("Concept:") and entry.get("name"):
            game_name = entry["name"]
            break

    editions, addons = [], []
    for key, entry in cache.items():
        if not key.startswith("Product:"):
            continue
        pid = key.split(":", 1)[1]
        name = entry.get("name") or ""
        edition = entry.get("edition")
        offer = Offer(key=f"ps:{pid}", label=(edition or {}).get("name") or _short_label(name, game_name))
        if pid in regular_price:
            cents, original, currency = regular_price[pid]
            offer.price, offer.currency = cents, currency
            if original and original > cents:
                offer.regular = original
        elif isinstance(entry.get("price"), dict):
            p = entry["price"]
            if p.get("isFree"):
                offer.price = 0
            elif "NONE" in (p.get("serviceBranding") or []):
                offer.price = to_cents(p.get("discountedPrice"))
                base = to_cents(p.get("basePrice"))
                if base and offer.price and base > offer.price:
                    offer.regular = base
            if offer.price is None:
                offer.note = (p.get("discountedPrice") or "not for sale").lower()
        elif edition is None:
            continue  # a product mentioned without any price data
        if pid in plus_price:
            offer.note = "; ".join(filter(None, [offer.note, f"PS Plus price ${plus_price[pid][0] / 100:.2f} (not tracked)"]))
        if edition:
            editions.append((edition.get("ordering", 99), offer))
        elif offer.price is not None:
            offer.suggested = False
            addons.append(offer)

    # Keep the order the Store uses (standard first), then add-ons.
    offers = [o for _, o in sorted(editions, key=lambda e: -e[0])] + addons
    if not offers:
        return None
    # A product page without an edition picker: use the product in the URL.
    m = re.search(r"/(?:product|concept)/([A-Z0-9_-]+)", url)
    if not editions and m:
        for o in offers:
            o.suggested = o.key == f"ps:{m.group(1)}"
    if all(o.price is None for o in offers):
        return Result("no_price", "no price listed yet", method="playstation", offers=offers, name=game_name)
    return Result("ok", "ok", method="playstation", offers=offers, name=_plain(game_name) or game_name)


# ---------------------------------------------------------------------------
# Walmart Canada: the page's embedded data has the product's own price. Do not
# search it loosely, because add-on services (warranties) also have prices.
# ---------------------------------------------------------------------------


def walmart(page, soup, url):
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        product = json.loads(tag.string)["props"]["pageProps"]["initialData"]["data"]["product"]
        info = product["priceInfo"]
    except (ValueError, KeyError, TypeError):
        return None
    current = info.get("currentPrice") or {}
    cents = to_cents(current.get("price"))
    if not cents:
        return None
    was = to_cents((info.get("wasPrice") or {}).get("price"))
    return Result("ok", "ok", method="walmart", name=product.get("name", ""), offers=[Offer(
        label=product.get("name", ""), price=cents, currency=current.get("currencyUnit") or "CAD",
        regular=was if was and was > cents else None,
        in_stock=stock_from_text(product.get("availabilityStatus")),
    )])


# ---------------------------------------------------------------------------
# Newegg Canada: the product box shows the current price (dollars and cents in
# separate tags) and the "was" price. Other boxes on the page (other sellers,
# related items) also have prices, so only the main product box is read.
# ---------------------------------------------------------------------------


def newegg(page, soup, url):
    box = soup.select_one(".product-pane .product-price") or soup.select_one(".product-price")
    if box is None:
        return None
    cents = None
    for el in box.select("[class*=price-current]"):
        cents = to_cents(el.get_text(" ", strip=True))
        if cents:
            break
    if not cents:
        return None
    was_el = box.select_one(".price-was")
    was = to_cents(was_el.get_text(" ", strip=True)) if was_el else None
    title = soup.select_one("h1.product-title")
    name = title.get_text(" ", strip=True) if title else ""
    return Result("ok", "ok", method="newegg", name=name, offers=[Offer(
        label=name, price=cents, currency="CAD", regular=was if was and was > cents else None,
        in_stock=_newegg_stock(page.text, url),
    )])


def _newegg_stock(text, url):
    """The "Instock" flag next to this item's number in the page data (N82E16824281271 is 24-281-271)."""
    m = re.search(r"N82E168(\d{2})(\d{3})(\d{3})", url)
    if m:
        # The product entry has the item number followed by its quantity (guide links have a title instead).
        item = re.escape(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
        flag = re.search(r'"Item":"' + item + r'","Qty":\d+.{0,400}?"Instock":(true|false)', text)
        if flag:
            return flag.group(1) == "true"
    return None


# ---------------------------------------------------------------------------
# Memory Express: no structured price data. The product's own pricing box
# says "Regular Price $439.98 ... Only $259.99". Other boxes on the page
# (warranty plans, accessories) also have prices, so only this box is read.
# ---------------------------------------------------------------------------


def memoryexpress(page, soup, url):
    box = soup.select_one("#ProductPricing")
    if box is None:
        return None
    text = re.sub(r"\s+", " ", box.get_text(" ", strip=True))
    m = re.search(r"Only \$\s?([\d,]+\.\d{2})", text) or re.search(r"Your Price:? \$\s?([\d,]+\.\d{2})", text)
    if not m:
        amounts = [a for a in re.findall(r"(?<!\()\$\s?([\d,]+\.\d{2})", text)]
        if not amounts:
            return None
        m = re.match(r"(.*)", amounts[-1])
    cents = to_cents(m.group(1))
    was_el = box.select_one("[class*=strikethrough]")
    was = to_cents(was_el.get_text(" ", strip=True)) if was_el else None
    ends = re.search(r"Sale Ends: ([A-Z][a-z]{2} \d{1,2})", text)
    title = soup.select_one("h1")
    name = title.get_text(" ", strip=True) if title else ""
    return Result("ok", "ok", method="memoryexpress", name=name, offers=[Offer(
        label=name, price=cents, currency="CAD", regular=was if was and was > cents else None,
        note=f"sale ends {ends.group(1)}" if ends else "",
    )])


# ---------------------------------------------------------------------------
# Amazon.ca: the buy box price. Amazon often blocks automated requests, so
# this may stop working at any time; the item then shows "blocked".
# ---------------------------------------------------------------------------


def amazon(page, soup, url):
    cents = None
    for sel in ("#corePrice_feature_div .priceToPay .a-offscreen", "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
                ".priceToPay .a-offscreen", "#corePrice_feature_div .a-offscreen", "#priceblock_ourprice"):
        el = soup.select_one(sel)
        if el and to_cents(el.get_text(" ", strip=True)):
            cents = to_cents(el.get_text(" ", strip=True))
            break
    if not cents:
        m = re.search(r'"priceAmount"\s*:\s*([\d.]+)', page.text)
        cents = to_cents(m.group(1)) if m else None
    if not cents:
        return None
    was_el = soup.select_one("#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen") or \
        soup.select_one(".basisPrice .a-offscreen")
    was = to_cents(was_el.get_text(" ", strip=True)) if was_el else None
    title = soup.select_one("#productTitle")
    name = title.get_text(" ", strip=True) if title else ""
    avail = soup.select_one("#availability")
    avail = avail.get_text(" ", strip=True).lower() if avail else ""
    in_stock = False if "unavailable" in avail or "out of stock" in avail else True if "in stock" in avail else None
    return Result("ok", "ok", method="amazon", name=name, offers=[Offer(
        label=name, price=cents, currency="CAD", regular=was if was and was > cents else None, in_stock=in_stock,
    )])


# ---------------------------------------------------------------------------
# Shopify stores (thousands of shops, for example gear.bethesda.net). Any
# Shopify product page has its data at the same address plus ".js": every
# variant (size, colour) with its price in cents and whether it can be bought.
# Found by looking at the page, not by domain, so it works for any Shopify shop.
# ---------------------------------------------------------------------------


def is_shopify(page):
    return "/products/" in page.url and ("cdn.shopify.com" in page.text or "Shopify.shop" in page.text)


def shopify(page, soup, url, fetch):
    parts = urlparse(page.url)
    m = re.search(r"/products/([^/?#.]+)", parts.path)
    if not m:
        return None
    data_url = f"{parts.scheme}://{parts.netloc}{parts.path[:m.end()]}.js"
    data_page = fetch(data_url, client=page.client) if page.client != "requests" else fetch(data_url)
    if data_page.status != 200:
        return None
    try:
        data = data_page.json()
        variants = data["variants"]
    except (ValueError, KeyError, TypeError):
        return None
    cur = re.search(r'Shopify\.currency\s*=\s*\{"active":"(\w{3})"', page.text)
    currency = cur.group(1) if cur else None
    wanted = re.search(r"[?&]variant=(\d+)", url)
    name = data.get("title", "")
    offers = []
    for v in variants:
        price = v.get("price")
        compare = v.get("compare_at_price")
        label = v.get("title") or ""
        offers.append(Offer(
            key=f"shopify:{v.get('id')}",
            label=name if len(variants) == 1 or label == "Default Title" else label,
            price=price if isinstance(price, int) else to_cents(price),
            currency=currency,
            regular=compare if isinstance(compare, int) and price and compare > price else None,
            in_stock=bool(v.get("available")) if "available" in v else None,
            suggested=(str(v.get("id")) == wanted.group(1)) if wanted else True,
        ))
    if not offers:
        return None
    image = data.get("featured_image") or ""
    image = clean_image("https:" + image if image.startswith("//") else image)
    return Result("ok", "ok", method="shopify", name=name, image=image, offers=offers)


# ---------------------------------------------------------------------------
# Canadian Tire: the product page loads its price with JavaScript, from the
# same data service this reader uses. It needs the public key that every
# product page contains; if Canadian Tire changes it, the new one is read from
# the page. Prices and stock are for one store (CT_STORE_ID in .env).
# ---------------------------------------------------------------------------

_CT = {"key": config.CT_API_KEY}
CT_API = "https://www.canadiantire.ca/api/v1/product/api/v2/product"


def _ct_headers():
    return {"Ocp-Apim-Subscription-Key": _CT["key"], "baseSiteId": "CTR", "bannerid": "CTR",
            "service-version": "v1", "service-client": "ctr/web", "x-web-host": "www.canadiantire.ca",
            "accept": "application/json, text/plain, */*", "browse-mode": "OFF",
            "Referer": "https://www.canadiantire.ca/"}


def _ct_call(fetch, url, json_body=None, page_url=None):
    page = fetch(url, client="chrome-like", headers=_ct_headers(), json_body=json_body)
    if page.status in (401, 403) and page_url:
        # The public key may have changed: read the current one from the product page.
        product_page = fetch(page_url, client="chrome-like")
        m = re.search(r'subscriptionKey\s*=\s*"([0-9a-f]{32})"', product_page.text)
        if m and m.group(1) != _CT["key"]:
            _CT["key"] = m.group(1)
            page = fetch(url, client="chrome-like", headers=_ct_headers(), json_body=json_body)
    require_ok(page, "Canadian Tire")
    return _json_or_fail(page, "Canadian Tire")


def ct_require_store():
    if not config.CT_STORE_ID:
        raise FetchError("not_found", "set CT_STORE_ID in .env to read Canadian Tire prices")


def canadiantire(url, fetch, details):
    m = re.search(r"-(\d{6,8}[pP])(?:\.(\d{6,8}))?\.html", urlparse(url).path)
    if not m:
        return None  # not a product page: let the page chain try
    ct_require_store()
    family = _ct_call(fetch, f"{CT_API}/productFamily/{m.group(1).lower()}?baseStoreId=CTR&lang=en_CA"
                             f"&storeId={config.CT_STORE_ID}&light=true", page_url=url)
    skus = [s for s in family.get("skus") or [] if s.get("code")]
    if not skus:
        return Result("not_found", "Canadian Tire has no product data for this link", method="canadiantire-api")
    prices = _ct_call(fetch, f"{CT_API}/sku/PriceAvailability?lang=en_CA&storeId={config.CT_STORE_ID}&cache=true",
                      json_body={"skus": [{"code": s["code"], "lowStockThreshold": "0"} for s in skus]}, page_url=url)
    by_code = {p.get("code"): p for p in prices.get("skus") or []}
    name = family.get("name", "")
    image = ""
    if details and family.get("images"):
        first = family["images"][0]
        image = clean_image(first.get("url", "") if isinstance(first, dict) else str(first))
    offers = []
    for s in skus:
        p = by_code.get(s["code"]) or {}
        current = p.get("currentPrice") or {}
        cents = to_cents(current.get("value") if current.get("value") is not None else current.get("minPrice"))
        original = to_cents((p.get("originalPrice") or {}).get("value"))
        note = []
        if p.get("isOnSale") and p.get("priceValidUntil"):
            try:
                note.append(f"sale ends {datetime.fromisoformat(p['priceValidUntil'].replace('Z', '+00:00')).astimezone(TORONTO):%b %-d}")
            except ValueError:
                pass
        offers.append(Offer(
            key=f"ct:{s['code']}",
            label=name if len(skus) == 1 else (s.get("name") or s["code"]),
            price=cents, currency="CAD",
            regular=original if original and cents and original > cents else None,
            in_stock=_ct_stock(p), note="; ".join(note),
            suggested=(s["code"] == m.group(2)) if m.group(2) else True,
        ))
    if all(o.price is None for o in offers):
        return Result("no_price", "Canadian Tire shows no price right now", method="canadiantire-api",
                      name=name, image=image, offers=offers)
    return Result("ok", "ok", method="canadiantire-api", name=name, image=image, offers=offers)


def _ct_stock(p):
    """In stock when it can be shipped or picked up; the store's shelf alone is not required."""
    if not p:
        return None
    if p.get("sellable") is False:
        return False
    avail = ((p.get("fulfillment") or {}).get("availability") or {})
    corporate = avail.get("Corporate") or {}
    can_ship = bool(corporate.get("sthETA")) or (corporate.get("Quantity") or 0) > 0
    in_store = (avail.get("quantity") or 0) > 0
    return can_ship or in_store


API_SITES = {
    "store.steampowered.com": steam,
    "bestbuy.ca": bestbuy,
    "canadiantire.ca": canadiantire,
}

PAGE_SITES = {
    "playstation.com": playstation,
    "walmart.ca": walmart,
    "newegg.ca": newegg,
    "memoryexpress.com": memoryexpress,
    "amazon.ca": amazon,
}

# Friendly names for the web UI.
SITE_NAMES = {
    "store.steampowered.com": "Steam",
    "bestbuy.ca": "Best Buy",
    "playstation.com": "PlayStation",
    "walmart.ca": "Walmart",
    "newegg.ca": "Newegg",
    "memoryexpress.com": "Memory Express",
    "canadacomputers.com": "Canada Computers",
    "amazon.ca": "Amazon",
    "canadiantire.ca": "Canadian Tire",
    "homedepot.ca": "Home Depot",
    "staples.ca": "Staples",
    "ikea.com": "IKEA",
    "lululemon.com": "Lululemon",
    "costco.ca": "Costco",
    "gamestop.ca": "EB Games",
    "gear.bethesda.net": "Bethesda Gear",
}


def cart_links(item_url, edition_key):
    """One-tap "Buy now" and "Add to cart" links for an edition, as [(label, url)].

    Only Shopify stores have documented links for this (cart permalinks,
    https://shopify.dev/docs/apps/build/checkout/create-cart-permalinks):
    /cart/<variant>:1 opens checkout with the item in it (guest checkout is
    allowed), and ?storefront=true opens the cart instead. The links are opened
    on your own phone; nothing is added or bought by the Pi. Other stores have
    no documented link, so their alerts open the product page.
    """
    if not str(edition_key or "").startswith("shopify:"):
        return []
    variant = edition_key.split(":", 1)[1]
    if not variant.isdigit():
        return []
    parts = urlparse(item_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    return [("Buy now", f"{origin}/cart/{variant}:1"),
            ("Add to cart", f"{origin}/cart/{variant}:1?storefront=true")]


def site_name(url):
    host = (urlparse(url).hostname or "").lower()
    for domain, label in SITE_NAMES.items():
        if host == domain or host.endswith("." + domain):
            return label
    return host[4:] if host.startswith("www.") else host
