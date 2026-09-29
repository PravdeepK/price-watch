"""
Product search across the stores Price Watch can read, for the web page's
search-as-you-type box.

Each store has one small function that runs a search the same way the
store's own search page does, and returns up to a few results as plain dicts:
{store, name, url, price, currency, regular, image, in_stock}. The web page
asks every store at once and shows results as each one answers, so a slow
store never holds up the others. Results are cached for 10 minutes, so typing
the same thing again does not search again.

To add a store: write one function and add it to STORES at the bottom.
"""

import json
import re
import threading
import time
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

import config
import sites
from extract import FetchError, clean_image, require_ok, safe_get, stock_from_text, to_cents

MAX_RESULTS = 6
CACHE_SECONDS = 600
_cache = {}
_cache_lock = threading.Lock()


def _result(store, name, url, price=None, currency="CAD", regular=None, image="", in_stock=None):
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not name or not url or not str(url).startswith("https://"):
        return None
    return {"store": store, "name": name[:200], "url": url, "price": price, "currency": currency,
            "regular": regular if regular and price and regular > price else None,
            "image": clean_image(image or ""), "in_stock": in_stock}


def _page(url, client="chrome-like", headers=None):
    page = safe_get(url, client=client, headers=headers)
    require_ok(page, "the store")
    return page


# ---------------------------------------------------------------------------
# One function per store
# ---------------------------------------------------------------------------


def bestbuy(q):
    data = _page(f"https://www.bestbuy.ca/api/v2/json/search?query={quote(q)}&lang=en-CA&page=1"
                 f"&pageSize={MAX_RESULTS}&currentRegion=ON", client="requests").json()
    out = []
    for p in data.get("products") or []:
        price = to_cents(p.get("salePrice") or p.get("regularPrice"))
        out.append(_result("Best Buy", p.get("name"), urljoin("https://www.bestbuy.ca", p.get("productUrl") or ""),
                           price, regular=to_cents(p.get("regularPrice")),
                           image=p.get("highResImage") or p.get("thumbnailImage")))
    return out


def walmart(q):
    page = _page(f"https://www.walmart.ca/search?q={quote(q)}")
    tag = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page.text, re.S)
    if not tag:
        return []
    stacks = json.loads(tag.group(1))["props"]["pageProps"]["initialData"]["searchResult"].get("itemStacks") or []
    out = []
    for item in (stacks[0].get("items") if stacks else []) or []:
        if not item.get("name") or not item.get("canonicalUrl"):
            continue
        info = item.get("priceInfo") or {}
        price = to_cents(info.get("linePrice") or info.get("itemPrice"))
        was = to_cents(info.get("wasPrice"))
        out.append(_result("Walmart", item["name"], urljoin("https://www.walmart.ca", item["canonicalUrl"]),
                           price, regular=was, image=(item.get("imageInfo") or {}).get("thumbnailUrl"),
                           in_stock=stock_from_text((item.get("availabilityStatusV2") or {}).get("value"))))
    return out


def canadiantire(q):
    sites.ct_require_store()
    if not sites._CT["key"]:
        raise FetchError("not_found", "set CT_API_KEY in .env to search Canadian Tire")
    data = _page(f"https://www.canadiantire.ca/api/v1/search/v2/search?q={quote(q)}&store={config.CT_STORE_ID}"
                 f"&lang=en_CA&baseStoreId=CTR&count={MAX_RESULTS}", headers=sites._ct_headers()).json()
    out = []
    for p in data.get("products") or []:
        cur = p.get("currentPrice") or {}
        price = to_cents(cur.get("value") if cur.get("value") is not None else cur.get("minPrice"))
        images = p.get("images") or [{}]
        out.append(_result("Canadian Tire", p.get("title"),
                           urljoin("https://www.canadiantire.ca", (p.get("url") or "").split("?")[0]),
                           price, image=images[0].get("url") if isinstance(images[0], dict) else ""))
    return out


def newegg(q):
    soup = BeautifulSoup(_page(f"https://www.newegg.ca/p/pl?d={quote(q)}").text, "lxml")
    out = []
    for cell in soup.select(".item-cell"):
        link = cell.select_one("a.item-title")
        if not link or "/p/" not in link.get("href", ""):
            continue
        price_el = cell.select_one(".price-current")
        was_el = cell.select_one(".price-was-data") or cell.select_one(".price-was")
        img = cell.select_one("a.item-img img")
        out.append(_result("Newegg", link.get_text(" ", strip=True), link["href"].split("?")[0],
                           to_cents(price_el.get_text(" ", strip=True)) if price_el else None,
                           regular=to_cents(was_el.get_text(" ", strip=True)) if was_el else None,
                           image=img.get("src") if img else ""))
    return out


def canadacomputers(q):
    soup = BeautifulSoup(_page(f"https://www.canadacomputers.com/en/search?s={quote(q)}").text, "lxml")
    out = []
    for art in soup.select("article.product-miniature"):
        link = art.select_one("a.product-thumbnail") or art.select_one("a[href$='.html']")
        title = art.select_one(".product-title") or link
        desc = art.select_one("[data-final_price]")
        img = art.select_one("img")
        if not link:
            continue
        out.append(_result("Canada Computers", title.get_text(" ", strip=True) if title else "",
                           link["href"].split("?")[0], to_cents(desc.get("data-final_price")) if desc else None,
                           image=(img.get("data-full-size-image-url") or img.get("data-cc-src")) if img else ""))
    return out


def amazon(q):
    soup = BeautifulSoup(_page(f"https://www.amazon.ca/s?k={quote(q)}").text, "lxml")
    out = []
    for r in soup.select('div[data-component-type="s-search-result"]'):
        asin = r.get("data-asin")
        if not asin or "AdHolder" in (r.get("class") or []):
            continue
        img = r.select_one("img.s-image")
        name = (img.get("alt") if img else "") or (r.select_one("h2").get_text(" ", strip=True) if r.select_one("h2") else "")
        price = r.select_one(".a-price:not(.a-text-price) .a-offscreen")
        was = r.select_one(".a-price.a-text-price .a-offscreen")
        out.append(_result("Amazon", name, f"https://www.amazon.ca/dp/{asin}",
                           to_cents(price.get_text()) if price else None,
                           regular=to_cents(was.get_text()) if was else None, image=img.get("src") if img else ""))
    return out


def steam(q):
    data = _page(f"https://store.steampowered.com/api/storesearch/?term={quote(q)}&cc=CA&l=english",
                 client="requests").json()
    out = []
    for it in data.get("items") or []:
        price = it.get("price") or {}
        out.append(_result("Steam", it.get("name"), f"https://store.steampowered.com/app/{it.get('id')}/",
                           price.get("final", 0) if price else 0, currency=price.get("currency") or "CAD",
                           regular=price.get("initial"), image=it.get("tiny_image")))
    return out


def shopify_store(store, origin, currency):
    """Search for any Shopify shop, through Shopify's standard predictive search."""
    def search(q):
        data = _page(f"{origin}/search/suggest.json?q={quote(q)}&resources[type]=product"
                     f"&resources[limit]={MAX_RESULTS}", client="requests").json()
        out = []
        for p in ((data.get("resources") or {}).get("results") or {}).get("products") or []:
            price = to_cents(p.get("price"))
            compare = to_cents(p.get("compare_at_price_max"))
            out.append(_result(store, p.get("title"), urljoin(origin, (p.get("url") or "").split("?")[0]),
                               price, currency=currency, regular=compare, image=p.get("image"),
                               in_stock=p.get("available")))
        return out
    return search


STORES = {
    "bestbuy": ("Best Buy", bestbuy),
    "walmart": ("Walmart", walmart),
    "canadiantire": ("Canadian Tire", canadiantire),
    "newegg": ("Newegg", newegg),
    "canadacomputers": ("Canada Computers", canadacomputers),
    "amazon": ("Amazon", amazon),
    "steam": ("Steam", steam),
    "bethesda": ("Bethesda Gear (USD)", shopify_store("Bethesda Gear", "https://gear.bethesda.net", "USD")),
}


def search(store_key, query):
    """Search one store. Returns {"store", "results", "error"}; never raises."""
    name, func = STORES[store_key]
    q = re.sub(r"\s+", " ", query).strip()[:100]
    key = (store_key, q.lower())
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    started = time.time()
    try:
        results = [r for r in func(q) if r][:MAX_RESULTS]
        answer = {"store": name, "results": results, "error": ""}
    except FetchError as e:
        answer = {"store": name, "results": [], "error": e.message}
    except Exception as e:  # a changed page layout; show it as that store's problem only
        answer = {"store": name, "results": [], "error": f"search failed ({e.__class__.__name__})"}
    answer["seconds"] = round(time.time() - started, 1)
    with _cache_lock:
        if not answer["error"]:
            _cache[key] = (time.time(), answer)
        if len(_cache) > 500:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:250]:
                del _cache[k]
    return answer
