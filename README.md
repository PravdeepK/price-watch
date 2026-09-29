# Price Watch

[![Tests](https://github.com/PravdeepK/price-watch/actions/workflows/tests.yml/badge.svg)](https://github.com/PravdeepK/price-watch/actions/workflows/tests.yml)

A self-hosted price tracker for Canadian online stores. Paste a product link (or
search the stores from the web page), optionally set a target price, and Price Watch
checks the price every 30 minutes. When the price drops, reaches your target, or an
out-of-stock item comes back, it sends a push notification to your phone through
[ntfy](https://ntfy.sh).

It is built to run unattended on a small always-on machine such as a Raspberry Pi:
a Flask web page and JSON API served by Waitress, and a checker run by a systemd
timer. Data is a single JSON file, so there is no database server to maintain.

![Dashboard overview](docs/screenshots/dashboard.png)

## Features

- **Price drops.** Alerts when a price falls below the last good price. An optional
  minimum drop per item (an amount like `5` or a percentage like `10%`) filters out
  small changes.
- **Target prices.** A one-time alert when the price reaches or goes below your
  target. It re-arms only after the price goes back above the target.
- **Restocks.** Optional "back in stock" alert when an item goes from out of stock to
  in stock, for stores that report stock.
- **Editions and variants.** Pages with several purchase options (game editions,
  Shopify variants, Canadian Tire SKUs) are tracked per option.
- **Alert deduplication.** Target and "cannot read" alerts are sent once until the
  condition resets. Alerts held during quiet hours are collapsed to the newest one per
  item and edition, and sent as a single summary.
- **Quiet hours and snooze.** Alerts are held during quiet hours (which can cross
  midnight) or while snoozed, then sent as one summary. Back-in-stock alerts can
  optionally break through quiet hours.
- **Failure thresholds.** Temporary problems (network errors, server errors) must
  happen 6 checks in a row, about 3 hours, before a "cannot read" alert. Other
  failures, such as a page with no price, alert on the first failure. A "working
  again" alert follows once the item recovers.
- **Suspicious change check.** A new price under 40% of the previous one, or a price
  found by a different method than last time, is fetched a second time before it is
  trusted.
- **Canadian price and currency checks.** Links to US stores (for example
  `amazon.com`) or non-Canadian locale paths (`/en-us/`) are refused, with a
  suggested Canadian link. Prices in another currency are rejected unless you choose
  to track that link anyway, in which case it is shown in its own currency. A bare
  `$` counts as CAD only on a known Canadian store.
- **Store search.** A search-as-you-type box queries several stores at once and
  shows results as each store answers.
- **Web page.** Watchlist, alert settings, and a status tab with the last run, logs
  and machine health. An optional login (HTTP basic auth) is available.

![Item detail](docs/screenshots/item-detail.png)

## Architecture

```
          systemd timer (every 30 min)            browser / phone
                    |                                    |
                    v                                    v
             price_watch.py  <---- "Check now" ----   app.py (Flask on Waitress)
                    |                                    |      \
                    v                                    |       search.py
               extract.py  ----->  sites.py              |
                    |                                    |
                    +---------->  store.py  <------------+
                    |          (watchlist.json)
                    v
               notify.py  -----> ntfy -----> phone
```

- **`extract.py`: the extraction chain.** `check(url)` never raises; every failure
  comes back as a result with a kind (`blocked`, `network`, `not_found`, ...) and a
  message. The steps, in order, stopping at the first one that finds a sensible
  price:
  1. Canada check on the link.
  2. Site API readers that do not need the product page (Steam, Best Buy, Canadian
     Tire).
  3. Fetch the page. Every address the host resolves to must be on the public
     internet, so the web page can't be used to reach devices on the local network.
     Responses are capped at 5 MB and redirects are re-checked.
  4. The item's CSS selector override, if one is set.
  5. Site page readers (PlayStation, Walmart, Newegg, Memory Express, Amazon), then
     Shopify detection.
  6. JSON-LD product data.
  7. Price meta tags.
  8. A filtered regex over the page text: the first dollar amount that is not near
     words like "was", "save", "shipping" or "/mo".
- **`sites.py`: the site registry.** Two dictionaries map domains to readers:
  `API_SITES` (fetch JSON themselves) and `PAGE_SITES` (parse the fetched page).
  Supporting a new store means writing one function, adding it to a registry, and
  adding a saved page with a test.
- **`search.py`: store search.** One small function per store. The web page sends
  one request per store at the same time and shows each store's results as they
  arrive. Successful results are cached for 10 minutes.
- **`store.py`: storage.** All data lives in `watchlist.json`. Every change goes
  through `update()`, which holds an exclusive `fcntl` file lock while it reads,
  changes and writes. Writes go to a temporary file, are fsynced, and replace the
  real file atomically, and the previous version is kept as `.bak`. A second lock
  makes sure only one check runs at a time. A daily copy is kept for 7 days.
- **Integer cents.** Prices are parsed into integer cents with `Decimal`, including
  formats like `$1,299.99`, `1 299,99 $` and `CDN$ 79.99`, and are stored and
  compared as integers.
- **`price_watch.py`: the checker.** It reads the item list, releases the lock while
  fetching (which can take minutes), then re-locks, re-reads, and applies results
  only to items that still exist unchanged, so the web page is never blocked.
  Requests to the same site are spaced at least 5 seconds apart, and a site that
  blocks the checker is left alone for 1, 2, 4, then 8 hours.
- **`app.py`: web page and API.** A Flask app serving a single page
  (`static/index.html`) and a small JSON API, run by Waitress in production.
- **`notify.py`: notifications.** Publishes JSON messages to an ntfy topic with
  priority, a click link and up to three action buttons, retrying up to three times.
  Failed alerts stay queued and are retried on the next run, for up to 48 hours.
- **`share.py`: optional read-only share page.** Off unless `PW_SHARE_TOKEN` is set.
  It serves a filtered view of the watchlist (no settings, topic or alert state)
  under a secret path.

## Supported stores

| Store | How the price is read |
|---|---|
| Steam | Steam's `appdetails` API with Canadian pricing, one entry per purchase option |
| PlayStation Store | Data embedded in `playstation.com/en-ca` and `store.playstation.com/en-ca` pages |
| Best Buy Canada | Best Buy's offers and availability data |
| Canadian Tire | The data service its product pages use, for one store (`CT_STORE_ID`) |
| Walmart Canada | Data embedded in the product page |
| Newegg Canada | The main product price box |
| Memory Express | The product pricing box |
| Amazon.ca | The buy box price. Amazon often blocks automated requests. |
| Shopify stores | Detected from the page; reads the product's `.js` data for every variant |
| Other stores | JSON-LD, then meta tags, then the filtered text search |

Search covers Best Buy, Walmart, Canadian Tire, Newegg, Canada Computers, Amazon,
Steam and the Bethesda Gear Shopify store.

## Setup

Requirements: Python 3.13 (the version it is developed and tested with) on Linux or
macOS. `store.py` uses `fcntl` file locks, so Windows is not supported.

```sh
git clone <this repository> price-watch
cd price-watch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Then edit `.env`. Subscribe to the same topic in the ntfy app on your phone.

| Setting | Required | Purpose |
|---|---|---|
| `NTFY_TOPIC` | yes | Your ntfy topic. Anyone who knows it can read your alerts, so make it long and random. |
| `NTFY_SERVER` | no | A self-hosted ntfy server (default `https://ntfy.sh`) |
| `PW_BASE_URL` | no | Address of the web page, used for links in notifications (default `http://localhost:5000`) |
| `PW_USER`, `PW_PASSWORD` | no | Turn on a login for the web page |
| `CT_STORE_ID` | for Canadian Tire | The store whose prices and stock are read |
| `CT_API_KEY` | no | Canadian Tire's public web API key. If blank, product checks read it from the product page; search needs it set. |
| `PW_SHARE_TOKEN`, `PW_SHARE_BASE_URL`, `PW_SHARE_PORT` | no | The read-only share page. A blank token keeps it off. |
| `PW_DATA_FILE` | no | Where to keep `watchlist.json` (default: next to the code) |

## Running it

```sh
.venv/bin/python app.py                         # web page on http://localhost:5000
.venv/bin/python price_watch.py                 # check every item now
.venv/bin/python price_watch.py --dry-run       # check, but save nothing and send nothing
.venv/bin/python price_watch.py --test "https://store.steampowered.com/app/311210/"
.venv/bin/python price_watch.py --notify-test   # send a test notification
```

`--test` shows what the price reader finds for a link without saving anything.
`--ids a1b2,c3d4` checks only those items.

The web page has no access restriction of its own beyond the optional login, so run
it on a trusted home network and do not expose it to the internet.

### Optional: systemd deployment

`deploy/` has example units for running it on a Linux machine such as a Raspberry
Pi. They assume the code is in `/opt/price-watch` with a `.venv` inside and a user
named `YOUR_USER`, so change those to match your setup.

- `price-watch.service`: the web page, `waitress-serve --listen=0.0.0.0:5000 app:app`
- `price-watch-check.service` + `price-watch-check.timer`: the checker, every 30
  minutes, and soon after boot if a check was missed

```sh
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now price-watch.service price-watch-check.timer
```

![Alert on phone](docs/screenshots/alert-phone.png)

## Running the tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests run fully offline. Store pages are served from saved copies in
`tests/fixtures`, trimmed to the parts the readers use, and network calls are
replaced with fakes or mocks. GitHub Actions runs the same suite on every push and
pull request.

## Design decisions

- **Integer cents.** Floating point can't represent most prices exactly, which makes
  "is the new price lower" and "has it reached the target" comparisons unreliable.
  Parsing into integer cents once, with `Decimal`, keeps every comparison exact.
- **Extraction chain order.** The most specific and reliable sources come first: a
  store's own API or embedded data, then structured data (JSON-LD, meta tags), and
  only then a text search, which is the most likely to pick the wrong number. A
  user-set CSS selector overrides everything on the page, as an escape hatch for
  stores the readers get wrong.
- **Alert deduplication.** A tracker that repeats the same alert every 30 minutes
  gets muted. Target and failure alerts are remembered per item and edition and only
  re-armed when the condition resets, and alerts held during quiet hours are
  collapsed into one summary.
- **Failure threshold before alerting.** Home internet and store servers have brief
  outages. Waiting for 6 consecutive temporary failures avoids alerts for problems
  that fix themselves, while problems that need action, such as a page layout change,
  alert right away.

## Notes on scraping

Price Watch reads public product pages and store data for personal price tracking,
at a low rate: each item is checked every 30 minutes, requests to the same site are
spaced out, and a site that blocks it is left alone for up to 8 hours.

Some stores block automated clients, and some stores' terms of use don't allow
automated access. Price Watch sends a browser-like User-Agent on all requests. For
Walmart, Newegg, Canada Computers, Memory Express and Canadian Tire, and as a single retry for any
site that blocks the normal client or returns a page without a price, it uses
[curl_cffi](https://github.com/lexiforest/curl_cffi), a client with a Chrome-like
TLS fingerprint. It does not solve CAPTCHAs, use proxies, reuse browser cookies or
log in to accounts. A site that still blocks it shows "blocked by site protection".
Check a store's terms before tracking it, and keep the check rate low.

## License

MIT. See [LICENSE](LICENSE).
