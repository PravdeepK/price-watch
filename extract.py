"""
Fetching pages safely and finding the price on them.

check(url) runs the extraction chain and returns a Result:

  1. Canada check: refuse links that are clearly not a Canadian store.
  2. Site API parsers that do not need the product page (Steam, Best Buy).
  3. Fetch the page. Sites known to block ordinary clients get a client that
     connects the way Chrome does (curl_cffi); any other site that blocks us
     gets one retry with it.
  4. The item's CSS selector, if one is set.
  5. Site page parsers (PlayStation, Walmart).
  6. Generic methods: JSON-LD, then meta tags, then a loose text search.

The first step that finds a sensible price wins. Site parsers live in sites.py.
"""

import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:  # optional: a client that connects like Chrome, for sites that block Python
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
}
TIMEOUT = (6, 20)  # seconds to connect, seconds between bytes
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_PRICE_CENTS = 20_000_00

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
_CHROME_SESSION = None

# Sites whose bot protection rejects ordinary Python clients. They always get
# the Chrome-like client. Other sites only get it after they block us once.
CHROME_LIKE_SITES = ("walmart.ca", "newegg.ca", "canadacomputers.com", "memoryexpress.com")


class FetchError(Exception):
    """A fetch that failed in a way the item's status should explain."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class Page:
    url: str
    status: int
    text: str
    client: str = "requests"  # or "chrome-like"

    def json(self):
        return json.loads(self.text)


@dataclass
class Offer:
    """One price on a page. Pages with several editions return several offers."""

    key: str = "main"
    label: str = ""
    price: int | None = None  # cents; None means no price is listed right now
    currency: str | None = None
    regular: int | None = None  # the non-sale price, when the site shows one
    note: str = ""
    seller: str = ""
    suggested: bool = True  # pre-ticked in the web UI's edition picker
    in_stock: bool | None = None  # None when the store does not say


@dataclass
class Result:
    # ok, no_price, not_found, blocked, pihole, network, server, http,
    # not_canadian, bad_url
    kind: str
    status: str
    method: str = ""
    offers: list = field(default_factory=list)
    name: str = ""
    suggestion: str = ""  # a Canadian version of the URL, when there is one
    suspicious: bool = False
    image: str = ""  # product picture for the web page, when the site has one

    @property
    def ok(self):
        return self.kind in ("ok", "no_price")


# ---------------------------------------------------------------------------
# Safe fetching
# ---------------------------------------------------------------------------


def validate_url(url):
    """Return the host name, or raise FetchError if the URL is not allowed."""
    try:
        parts = urlparse(url)
        port = parts.port
    except ValueError:
        raise FetchError("bad_url", "that does not look like a valid link")
    if parts.scheme not in ("http", "https"):
        raise FetchError("bad_url", "only http and https links are allowed")
    if not parts.hostname:
        raise FetchError("bad_url", "the link has no website name")
    if parts.username or parts.password:
        raise FetchError("bad_url", "links with a username or password are not allowed")
    if port not in (None, 80, 443):
        raise FetchError("bad_url", "links to unusual ports are not allowed")
    return parts.hostname.lower()


def check_host(host):
    """Make sure every address the host resolves to is on the public internet.

    This stops the web page from being used to reach devices on the home
    network. It also spots Pi-hole blocking, which answers with 0.0.0.0.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise FetchError("network", f"could not look up {host}")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if addr.is_unspecified:
            raise FetchError("pihole", f"{host} is blocked by Pi-hole")
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if not addr.is_global:
            raise FetchError("bad_url", f"{host} points to a private or local address")


def _read_capped(response):
    chunks, size = [], 0
    for chunk in response.iter_content(64 * 1024):
        size += len(chunk)
        if size > MAX_BYTES:
            raise FetchError("http", "the page is too large (over 5 MB)")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode(headers, body):
    ctype = headers.get("Content-Type", "") or ""
    m = re.search(r"charset=([\w-]+)", ctype)
    try:
        return body.decode(m.group(1) if m else "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def chrome_like_available():
    return cffi_requests is not None


def _get_requests(url, headers=None, json_body=None):
    try:
        r = SESSION.request("POST" if json_body is not None else "GET", url, headers=headers, json=json_body,
                            timeout=TIMEOUT, allow_redirects=False, stream=True)
    except requests.Timeout:
        raise FetchError("network", "the site took too long to answer")
    except requests.RequestException as e:
        raise FetchError("network", f"could not connect ({e.__class__.__name__})")
    with r:
        if r.is_redirect:
            return r.status_code, r.headers, b""
        try:
            return r.status_code, r.headers, _read_capped(r)
        except requests.RequestException as e:
            raise FetchError("network", f"connection dropped ({e.__class__.__name__})")


def _get_chrome_like(url, headers=None, json_body=None):
    """Fetch with curl_cffi, which makes the connection look like Chrome's.

    Its own browser headers are kept (overriding the user agent would not
    match the connection), except for asking for Canadian English pages.
    """
    global _CHROME_SESSION
    if _CHROME_SESSION is None:
        _CHROME_SESSION = cffi_requests.Session(impersonate="chrome")
    try:
        r = _CHROME_SESSION.request("POST" if json_body is not None else "GET", url, json=json_body,
                                    timeout=TIMEOUT[0] + TIMEOUT[1], allow_redirects=False, stream=True,
                                    headers={"Accept-Language": HEADERS["Accept-Language"], **(headers or {})})
    except Exception as e:  # curl_cffi raises its own error types
        if "timed out" in str(e).lower() or "timeout" in e.__class__.__name__.lower():
            raise FetchError("network", "the site took too long to answer")
        raise FetchError("network", f"could not connect ({e.__class__.__name__})")
    try:
        if r.status_code in (301, 302, 303, 307, 308):
            return r.status_code, r.headers, b""
        return r.status_code, r.headers, _read_capped(r)
    except FetchError:
        raise
    except Exception as e:
        raise FetchError("network", f"connection dropped ({e.__class__.__name__})")
    finally:
        r.close()


def safe_get(url, client="requests", headers=None, json_body=None):
    """GET a URL (or POST json_body) with timeouts, a size cap, and the address check on every redirect.

    client is "requests" (normal) or "chrome-like" (curl_cffi, for sites that
    block ordinary Python clients). headers are added to the browser headers.
    """
    getter = _get_chrome_like if client == "chrome-like" else _get_requests
    for _ in range(MAX_REDIRECTS + 1):
        check_host(validate_url(url))
        status, headers_in, body = getter(url, headers, json_body)
        if status in (301, 302, 303, 307, 308) and headers_in.get("Location"):
            url = urljoin(url, headers_in["Location"])
            json_body = None  # like browsers, follow a redirect with a plain GET
            continue
        return Page(url=url, status=status, text=_decode(headers_in, body), client=client)
    raise FetchError("http", "too many redirects")


def require_ok(page, what="the site"):
    """Raise FetchError for a page that is blocked or an HTTP error."""
    reason = blocked_reason(page)
    if reason:
        raise FetchError("blocked", reason)
    if page.status in (404, 410):
        raise FetchError("http", f"{what} says this page does not exist (HTTP {page.status})")
    if page.status >= 500:
        raise FetchError("server", f"{what} had an error (HTTP {page.status})")
    if page.status >= 400:
        raise FetchError("http", f"{what} refused the request (HTTP {page.status})")


BLOCK_WORDS = re.compile(
    r"captcha|access denied|just a moment|attention required|robot or human|"
    r"are you a human|verify you are human|request blocked|pardon our interruption",
    re.I,
)


def blocked_reason(page):
    """Return a message if the page is a bot-protection page, else None."""
    title = re.search(r"<title[^>]*>(.*?)</title>", page.text[:20000], re.I | re.S)
    title = title.group(1).strip() if title else ""
    if page.status == 429:
        return "blocked by site protection (too many requests, HTTP 429)"
    if BLOCK_WORDS.search(title):
        return f"blocked by site protection (the site showed a '{title[:40]}' page)"
    if page.status in (401, 403):
        return f"blocked by site protection (HTTP {page.status})"
    if page.status >= 400 and re.search(r'"/blocked\?|px-captcha|perimeterx|"appId":"PX', page.text[:5000], re.I):
        return f"blocked by site protection (HTTP {page.status}, PerimeterX)"
    return None


# ---------------------------------------------------------------------------
# Prices and currency
# ---------------------------------------------------------------------------

NUMBER = re.compile(r"\d{1,3}(?:[, ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")


def to_cents(value):
    """Turn a number or price text into integer cents, or None.

    Handles $1,299.99, 1 299,99 $ (French Canadian), CDN$ 79.99, and numbers.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return int((Decimal(str(value)) * 100).quantize(Decimal(1)))
        except InvalidOperation:
            return None
    text = str(value).replace(" ", " ").replace(" ", " ")
    text = re.sub(r"(\d)\s+([.,]\d{2})(?!\d)", r"\1\2", text)  # "$ 169 .99": dollars and cents in separate tags
    m = NUMBER.search(text)
    if not m:
        return None
    raw = m.group()
    dec = re.search(r"[.,](\d{1,2})$", raw)
    if dec:
        whole, frac = raw[: dec.start()], dec.group(1).ljust(2, "0")
    else:
        whole, frac = raw, "00"
    whole = re.sub(r"\D", "", whole) or "0"
    return int(whole) * 100 + int(frac)


def sensible(cents, allow_zero=False):
    if cents is None:
        return False
    return (0 if allow_zero else 1) <= cents <= MAX_PRICE_CENTS


def is_canadian_host(host):
    return host.endswith(".ca")


def currency_from_text(text, canadian):
    """Guess the currency from a price's text. A bare $ is CAD only on Canadian pages."""
    t = (text or "").upper()
    if re.search(r"CDN\s?\$|CA\s?\$|C\s?\$|\bCAD\b", t):
        return "CAD"
    if re.search(r"US\s?\$|\bUSD\b", t):
        return "USD"
    if "£" in t or "GBP" in t:
        return "GBP"
    if "€" in t or "EUR" in t:
        return "EUR"
    if "$" in t and canadian:
        return "CAD"
    return None


def format_price(cents, currency=None):
    if cents is None:
        return "--"
    if cents == 0:
        return "Free"
    text = f"${cents // 100:,}.{cents % 100:02d}"
    if currency and currency != "CAD":
        text += f" {currency}"
    return text


# ---------------------------------------------------------------------------
# Canada check
# ---------------------------------------------------------------------------

# Stores whose .com site is the US store, with the Canadian site to use instead.
US_STORES = {
    "bestbuy.com": "www.bestbuy.ca",
    "walmart.com": "www.walmart.ca",
    "newegg.com": "www.newegg.ca",
    "amazon.com": "www.amazon.ca",
    "costco.com": "www.costco.ca",
    "staples.com": "www.staples.ca",
    "homedepot.com": "www.homedepot.ca",
    "ebay.com": "www.ebay.ca",
    "gamestop.com": "www.gamestop.ca",
}
LOCALE_IN_PATH = re.compile(r"/([a-z]{2})[-_]([a-z]{2})(?=/|$)", re.I)

# Canadian stores that use a .com address.
CANADIAN_COM_STORES = ("canadacomputers.com", "memoryexpress.com", "thebrick.com",
                       "sportchek.ca", "leons.ca")


def canada_check(url):
    """Return (verdict, message, suggested_url).

    verdict is "yes" (a Canadian store), "no" (clearly not), or "unknown".
    """
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    bare = host[4:] if host.startswith("www.") else host

    if bare == "direct.playstation.com":
        return ("no", "PlayStation Direct does not sell to Canada. "
                "Track this product at Best Buy Canada or Walmart Canada instead.", "")
    for us, ca in US_STORES.items():
        if bare == us or bare.endswith("." + us):
            fixed = parts._replace(netloc=ca).geturl()
            return ("no", f"This is the US store ({us}). The Canadian store is {ca}.", fixed)

    m = LOCALE_IN_PATH.search(parts.path)
    if m and m.group(2).lower() != "ca":
        lang = "fr" if m.group(1).lower() == "fr" else "en"
        fixed_path = parts.path[: m.start()] + f"/{lang}-ca" + parts.path[m.end():]
        fixed = parts._replace(path=fixed_path).geturl()
        return ("no", f"This link is for the {m.group(1)}-{m.group(2)} store, not Canada.", fixed)

    if is_canadian_host(host) or (m and m.group(2).lower() == "ca"):
        return ("yes", "", "")
    if any(bare == d or bare.endswith("." + d) for d in CANADIAN_COM_STORES):
        return ("yes", "", "")
    if re.match(r"/ca(/|$)", parts.path, re.I):  # for example ikea.com/ca/en/...
        return ("yes", "", "")
    if bare == "store.steampowered.com":
        return ("yes", "", "")  # the Steam parser always asks for Canadian prices
    return ("unknown", "", "")


# ---------------------------------------------------------------------------
# Generic extraction
# ---------------------------------------------------------------------------


def _json_ld_nodes(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))


IN_STOCK_WORDS = ("instock", "in stock", "limitedavailability", "onlineonly", "preorder", "presale", "backorder")
OUT_OF_STOCK_WORDS = ("outofstock", "out of stock", "oos", "soldout", "sold out", "discontinued", "instoreonly")


def stock_from_text(value):
    """schema.org availability ("https://schema.org/InStock") or similar text as True, False, or None.

    Pre-orders and back-orders count as in stock: they can be bought now.
    """
    v = str(value or "").lower().rsplit("/", 1)[-1].replace("_", " ").strip()
    if not v:
        return None
    if any(w in v for w in OUT_OF_STOCK_WORDS):
        return False
    if any(w in v for w in IN_STOCK_WORDS):
        return True
    return None


def any_in_stock(values):
    known = [v for v in values if v is not None]
    return any(known) if known else None


def from_json_ld(soup, canadian):
    """The product's price from its schema.org data.

    Clothing and similar pages list one offer per colour or size, often with
    different prices. Then the lowest is used, with a note saying so.
    """
    found = []  # from every product node: a page may list each colour as its own product
    for node in _json_ld_nodes(soup):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" not in types and "ProductGroup" not in types:
            continue
        offers = node.get("offers")
        for offer in offers if isinstance(offers, list) else [offers]:
            if not isinstance(offer, dict):
                continue
            spec = offer.get("priceSpecification")
            spec = spec[0] if isinstance(spec, list) and spec else spec
            for raw in (offer.get("price"), offer.get("lowPrice"),
                        spec.get("price") if isinstance(spec, dict) else None):
                cents = to_cents(raw)
                if sensible(cents):
                    currency = offer.get("priceCurrency") or (
                        spec.get("priceCurrency") if isinstance(spec, dict) else None)
                    found.append((cents, currency or ("CAD" if canadian else None), stock_from_text(offer.get("availability"))))
                    break
    if not found:
        return None
    cents, currency, _ = min(found, key=lambda f: f[0])
    varies = len({c for c, _, _ in found}) > 1
    return Offer(price=cents, currency=currency, in_stock=any_in_stock(f[2] for f in found),
                 note="price varies by option (colour, size); the lowest is tracked" if varies else "")


def from_meta(soup, canadian):
    for attrs in ({"property": "product:price:amount"}, {"property": "og:price:amount"},
                  {"itemprop": "price"}):
        el = soup.find(attrs=attrs)
        if not el:
            continue
        cents = to_cents(el.get("content") or el.get_text(" ", strip=True))
        if not sensible(cents):
            continue
        cur = soup.find(attrs={"property": "product:price:currency"}) or \
            soup.find(attrs={"property": "og:price:currency"}) or \
            soup.find(attrs={"itemprop": "priceCurrency"})
        currency = (cur.get("content") or cur.get_text(strip=True)) if cur else None
        avail = soup.find("meta", attrs={"property": "product:availability"}) or \
            soup.find("meta", attrs={"property": "og:availability"}) or soup.find(attrs={"itemprop": "availability"})
        stock = stock_from_text((avail.get("content") or avail.get("href")) if avail else None)
        return Offer(price=cents, currency=currency or ("CAD" if canadian else None), in_stock=stock)
    return None


PRICE_IN_TEXT = re.compile(
    r"(?:CDN\s?\$|CA\s?\$|C\s?\$|US\s?\$|\$)\s?(\d[\d,.  ]*\d|\d)"
    r"|(\d[\d,  ]*[.,]\d{2})\s?\$"
)
NOT_THE_PRICE = re.compile(
    r"/\s?mo\b|month|mois|\bsave\b|économ|\bwas\b|\breg\b|financ|shipping|livraison|"
    r"\boff\b|rabais|\bper\b|installment|from\s*$|gift card",
    re.I,
)


def from_text(soup, canadian):
    """Last resort: the first dollar amount on the page that is not a
    financing, savings, or "was" price."""
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" "))
    text = re.sub(r"(\d)\s+([.,]\d{2})(?!\d)", r"\1\2", text)
    for m in PRICE_IN_TEXT.finditer(text):
        context = text[max(0, m.start() - 12): m.end() + 12]
        if NOT_THE_PRICE.search(context):
            continue
        cents = to_cents(m.group(1) or m.group(2))
        if sensible(cents):
            return Offer(price=cents, currency=currency_from_text(m.group(), canadian))
    return None


def from_selector(soup, selector, canadian):
    try:
        el = soup.select_one(selector)
    except Exception:
        raise FetchError("not_found", "the CSS selector is not valid")
    if el is None:
        raise FetchError("not_found", "the CSS selector did not match anything on the page")
    text = el.get("content") or el.get_text(" ", strip=True)
    cents = to_cents(text)
    if not sensible(cents):
        raise FetchError("not_found", f"the CSS selector matched '{text[:40]}', which is not a price")
    return Offer(price=cents, currency=currency_from_text(text, canadian) or ("CAD" if canadian else None))


def page_image(soup, base_url):
    """The product picture a page advertises for link previews, if any."""
    for attrs in ({"property": "og:image"}, {"name": "og:image"}, {"name": "twitter:image"},
                  {"property": "twitter:image"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return clean_image(urljoin(base_url, tag["content"].strip()))
    for node in _json_ld_nodes(soup):
        if "Product" in (node.get("@type") if isinstance(node.get("@type"), list) else [node.get("@type")]):
            img = node.get("image")
            img = img[0] if isinstance(img, list) and img else img
            img = img.get("url") if isinstance(img, dict) else img
            if isinstance(img, str):
                return clean_image(urljoin(base_url, img))
    return ""


def clean_image(url):
    """Only plain web addresses are allowed as picture sources on the page."""
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) and len(url) < 1000 else ""


def page_name(soup):
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def _finish(result, verdict, allow_foreign=False):
    """Apply the currency part of the Canada check to a successful result."""
    if not result.ok:
        return result
    for offer in result.offers:
        if offer.currency is None and verdict == "yes":
            offer.currency = "CAD"
        if offer.currency and offer.currency.upper() != "CAD":
            if allow_foreign:
                continue  # tracked on purpose; prices show their currency everywhere
            return Result("not_canadian", f"the price is in {offer.currency.upper()}, not CAD",
                          method=result.method, name=result.name)
    if verdict == "unknown" and any(o.currency is None for o in result.offers):
        for offer in result.offers:
            if offer.currency is None:
                offer.note = (offer.note + "; " if offer.note else "") + \
                    "currency not shown on the page, check that it is CAD"
    return result


def _chrome_only(fetch):
    """A fetch that always uses the Chrome-like client (used for one retry)."""
    return lambda url, client="chrome-like", **kw: fetch(url, client="chrome-like", **kw)


def check(url, selector=None, details=False, fetch=None, allow_foreign=False):
    """Find the price(s) for a URL. Never raises; failures come back as a Result.

    allow_foreign skips the Canada check, for links the owner chose to track anyway.
    """
    import sites  # imported here so sites.py can import helpers from this module

    fetch = fetch or safe_get
    verdict, message, suggestion = canada_check(url)
    if verdict == "no":
        if not allow_foreign:
            return Result("not_canadian", message, suggestion=suggestion)
        verdict = "unknown"
    try:
        host = validate_url(url)
        canadian = verdict == "yes"

        api = sites.find(sites.API_SITES, host)
        if api:
            result = api(url, fetch, details)
            if result is not None:
                return _finish(result, verdict, allow_foreign)

        wants_chrome = chrome_like_available() and sites.find({d: True for d in CHROME_LIKE_SITES}, host)
        page = fetch(url, client="chrome-like") if wants_chrome else fetch(url)
        if blocked_reason(page) and page.client == "requests" and chrome_like_available():
            page = fetch(url, client="chrome-like")  # one retry, connecting like Chrome
        require_ok(page)
        soup = BeautifulSoup(page.text, "lxml")
        via = " (chrome-like)" if page.client == "chrome-like" else ""
        name = page_name(soup)

        if selector:
            offer = from_selector(soup, selector, canadian)
            return _finish(Result("ok", "ok", method="selector" + via, offers=[offer], name=name,
                                  image=page_image(soup, page.url)), verdict, allow_foreign)

        image = page_image(soup, page.url)
        parser = sites.find(sites.PAGE_SITES, host)
        if parser:
            result = parser(page, soup, url)
            if result is not None:
                result.method += via
                result.image = result.image or image
                return _finish(result, verdict, allow_foreign)

        if sites.is_shopify(page):
            result = sites.shopify(page, soup, url, fetch)
            if result is not None:
                result.method += via
                result.image = result.image or image
                return _finish(result, verdict, allow_foreign)

        for method, finder in (("json-ld", from_json_ld), ("meta", from_meta), ("text", from_text)):
            offer = finder(soup, canadian)
            if offer:
                offer.label = name
                return _finish(Result("ok", "ok", method=method + via, offers=[offer], name=name, image=image),
                               verdict, allow_foreign)
        if page.client == "requests" and chrome_like_available() and not selector:
            # Some sites send ordinary clients a cut-down page without prices,
            # without looking blocked. Try once more, connecting like Chrome.
            return check(url, selector, details, _chrome_only(fetch), allow_foreign)
        return Result("not_found", "no price found on the page", name=name)
    except FetchError as e:
        return Result(e.kind, e.message)
    except Exception as e:  # a bug or an unexpected page layout; never crash the run
        return Result("not_found", f"price reader failed ({e.__class__.__name__}: {e})")
