#!/usr/bin/env python3
"""
The price checker. The systemd timer runs it every 30 minutes, and the web UI
runs it for "Check now".

    python price_watch.py                 check every item that is not paused
    python price_watch.py --ids a1b2,c3d4 check only these items
    python price_watch.py --dry-run       check everything, save nothing, send nothing
    python price_watch.py --test URL      show what the price reader finds for a link
    python price_watch.py --notify-test   send a test notification

How a run works: read the list of items, release the lock, fetch every page
(which can take minutes), then lock again, re-read the file, and apply the
results only to items that still exist unchanged. That way the web UI is never
blocked and an item added or deleted during a run is not lost.
"""

import argparse
import copy
import random
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import config
import extract
import notify
import sites
import store
from extract import format_price

POLITE_GAP = 5  # minimum seconds between two requests to the same site
FAILURES_BEFORE_PING = 6  # for temporary problems (about 3 hours at 30 minute checks)
TEMPORARY = ("network", "server")
SUSPICIOUS_DROP = Decimal("0.4")  # a new price under 40% of the old one gets double-checked
HISTORY_LENGTH = 30
PENDING_MAX_AGE = timedelta(hours=48)


def log(message):
    print(f"{datetime.now(config.TZ):%Y-%m-%d %H:%M:%S} {message}", flush=True)


def now_iso():
    return datetime.now(config.TZ).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def new_edition(key="main", label=""):
    return {
        "key": key, "label": label, "price": None, "currency": None, "regular": None,
        "last_good_price": None, "lowest_price": None, "note": "", "seller": "",
        "target_alerted": False, "history": [], "in_stock": None,
    }


def new_item(url, name, editions, target=None, min_drop="", selector=""):
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "url": url,
        "site": sites.site_name(url),
        "created": now_iso(),
        "target": target,
        "min_drop": min_drop,
        "selector": selector,
        "paused": False,
        "editions": editions or [new_edition()],
        "last_checked": None,
        "last_success": None,
        "status": "not checked yet",
        "status_kind": "new",
        "method": "",
        "consecutive_failures": 0,
        "failure_alerted": False,
        "mute_until_working": False,
        "allow_foreign": False,  # tracked even though it is not a Canadian listing
        "stock_alert": False,  # alert when it goes from out of stock to in stock
    }


def parse_min_drop(text):
    """Accept '5', '$5.50', '5%' or blank. Return the normalized text, or raise ValueError."""
    text = (text or "").strip().replace("$", "").replace(" ", "")
    if not text:
        return ""
    try:
        if text.endswith("%"):
            value = Decimal(text[:-1])
            if not 0 < value < 100:
                raise ValueError
            return f"{value.normalize():f}%"
        value = Decimal(text)
        if value <= 0:
            raise ValueError
        return f"{value:.2f}"
    except InvalidOperation:
        raise ValueError(text)


def drop_is_big_enough(old, new, min_drop):
    if not min_drop:
        return True
    drop = old - new
    if min_drop.endswith("%"):
        return Decimal(drop) * 100 >= Decimal(min_drop[:-1]) * old
    return drop >= int(Decimal(min_drop) * 100)


def describe_min_drop(min_drop):
    if not min_drop:
        return "any drop"
    return f"drops of {min_drop}" if min_drop.endswith("%") else f"drops of ${min_drop}"


def edition_name(item, edition):
    if len(item["editions"]) > 1 and edition.get("label"):
        return f"{item['name']} ({edition['label']})"
    return item["name"]


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------


def _minutes(hhmm):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", hhmm or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def in_quiet_hours(settings, when=None):
    if settings.get("quiet_enabled") is False:
        return False
    start, end = _minutes(settings.get("quiet_start")), _minutes(settings.get("quiet_end"))
    if start is None or end is None or start == end:
        return False
    when = when or datetime.now(config.TZ)
    now = when.hour * 60 + when.minute
    if start < end:
        return start <= now < end
    return now >= start or now < end  # quiet hours that cross midnight


def snoozed(settings, when=None):
    until = settings.get("snooze_until") or ""
    return bool(until) and datetime.fromisoformat(until) > (when or datetime.now(config.TZ))


def holding(settings):
    """Alerts are held (and sent later as one summary) in quiet hours or while snoozed."""
    return in_quiet_hours(settings) or snoozed(settings)


# ---------------------------------------------------------------------------
# Applying a result and deciding on alerts
# ---------------------------------------------------------------------------


def _alert(item, edition_key, kind, title, message, click, priority):
    buttons = []
    if kind in ("drop", "target", "stock"):
        buttons = sites.cart_links(item.get("url", ""), edition_key)
        if buttons:
            buttons.append(("Open page", item["url"]))
    return {
        "id": uuid.uuid4().hex[:10], "item_id": item["id"], "edition_key": edition_key,
        "kind": kind, "title": title, "message": message, "click": click,
        "priority": priority, "created": now_iso(), "held": False,
        "urgent": kind == "stock",  # can get through quiet hours if the owner allows it
        "actions": buttons,
    }


def should_hold(alert, settings):
    """Held alerts wait for the end of quiet hours or snooze, then go out as one summary."""
    if snoozed(settings):
        return True
    if not in_quiet_hours(settings):
        return False
    return not (alert.get("urgent") and settings.get("quiet_allow_stock"))


def looks_suspicious(item, result):
    """A result worth fetching a second time before alerting on it."""
    if not result.ok or not item.get("last_success"):
        return False
    base = lambda method: (method or "").replace(" (chrome-like)", "")  # noqa: E731  (the client used does not matter)
    if item.get("method") and base(result.method) != base(item["method"]):
        return True
    old = {e["key"]: e.get("last_good_price") for e in item["editions"]}
    for offer in result.offers:
        prev = old.get(offer.key) or (old.get("main") if len(old) == 1 else None)
        if prev and offer.price is not None and offer.price < prev * SUSPICIOUS_DROP:
            return True
    return False


def _match_offers(item, result):
    by_key = {o.key: o for o in result.offers}
    matched = {}
    for edition in item["editions"]:
        offer = by_key.get(edition["key"])
        if offer is None and len(item["editions"]) == 1 and len(result.offers) == 1:
            offer = result.offers[0]  # single-price pages: keys do not matter
        if offer is not None:
            matched[edition["key"]] = offer
    return matched


def _fail(item, kind, status, alerts):
    item["consecutive_failures"] = item.get("consecutive_failures", 0) + 1
    item["status"], item["status_kind"] = status, kind
    needed = FAILURES_BEFORE_PING if kind in TEMPORARY else 1
    if item.get("mute_until_working"):
        return  # saved from the web UI without a price; the owner already knows
    if item["consecutive_failures"] >= needed and not item.get("failure_alerted"):
        fix = f"{config.BASE_URL}/item/{item['id']}"
        alerts.append(_alert(item, None, "problem", f"Cannot read: {item['name']}",
                             f"{status[:1].upper()}{status[1:]}. Fix it here: {fix}", fix, 3))
        item["failure_alerted"] = True


def apply_result(item, result):
    """Update one item from a check result. Returns a list of alerts to send."""
    alerts = _apply(item, result)
    return [] if item.get("muted") else alerts  # muted items stay up to date, silently


def _apply(item, result):
    alerts = []
    item["last_checked"] = now_iso()
    if result.image:
        item["image"] = result.image
    if not result.ok:
        _fail(item, result.kind, result.status, alerts)
        return alerts

    matched = _match_offers(item, result)
    if not matched and result.kind == "ok":
        _fail(item, "not_found", "none of the tracked editions are on the page anymore", alerts)
        return alerts

    if item.get("failure_alerted") and item.get("consecutive_failures"):
        alerts.append(_alert(item, None, "recovered", f"Working again: {item['name']}",
                             "Price Watch can read this item again.", item["url"], 2))
    item["failure_alerted"] = False
    item["mute_until_working"] = False
    item["consecutive_failures"] = 0
    item["last_success"] = item["last_checked"]
    if result.method:
        item["method"] = result.method

    missing = []
    for edition in item["editions"]:
        offer = matched.get(edition["key"])
        if offer is None:
            missing.append(edition.get("label") or edition["key"])
            edition["price"] = None
            edition["note"] = "not listed on the page anymore"
            continue
        alerts.extend(_update_edition(item, edition, offer, result.suspicious))

    if result.kind == "no_price":
        item["status"], item["status_kind"] = result.status, "no_price"
    elif missing:
        item["status"], item["status_kind"] = f"ok, but not listed anymore: {', '.join(missing)}", "partial"
    else:
        item["status"], item["status_kind"] = "ok", "ok"
    return alerts


def _update_edition(item, edition, offer, suspicious):
    alerts = []
    old_seller = edition.get("seller") or ""
    was_in_stock = edition.get("in_stock")
    edition.update(currency=offer.currency, regular=offer.regular, note=offer.note,
                   seller=offer.seller, price=offer.price, in_stock=offer.in_stock)
    price = offer.price
    name = edition_name(item, edition)
    if item.get("stock_alert") and was_in_stock is False and offer.in_stock is True:
        now = f"Now {format_price(price, offer.currency)}. " if price is not None else ""
        alerts.append(_alert(item, edition["key"], "stock", f"Back in stock: {name}",
                             now + "It can sell out again quickly.", item["url"], 5))
    if price is None:
        return alerts

    history = edition.setdefault("history", [])
    today = now_iso()[:10]
    if not history or history[-1][1] != price or history[-1][0][:10] != today:
        history.append([now_iso(), price])
        del history[:-HISTORY_LENGTH]
    if edition.get("lowest_price") is None or price < edition["lowest_price"]:
        edition["lowest_price"] = price

    cur = offer.currency
    prev = edition.get("last_good_price")
    target = item.get("target")
    reached = target is not None and price <= target
    extra = []
    if offer.regular:
        extra.append(f"Regular price {format_price(offer.regular, cur)}.")
    if offer.note:
        extra.append(offer.note[:1].upper() + offer.note[1:] + ".")
    if old_seller and offer.seller and offer.seller != old_seller:
        extra.append(f"Seller changed from {old_seller} to {offer.seller}.")
    if suspicious:
        extra.append(f"Large change, checked twice (found by: {item.get('method') or 'unknown'}).")

    dropped = prev is not None and price < prev and drop_is_big_enough(prev, price, item.get("min_drop"))
    if dropped:
        message = f"Now {format_price(price, cur)} (was {format_price(prev, cur)})."
        if reached and not edition.get("target_alerted"):
            message += f" At or below your {format_price(target, cur)} target."
        alerts.append(_alert(item, edition["key"], "drop", f"Price drop: {name}",
                             " ".join([message] + extra), item["url"], 4))
    elif reached and not edition.get("target_alerted"):
        message = f"Now {format_price(price, cur)}, at or below your {format_price(target, cur)} target."
        alerts.append(_alert(item, edition["key"], "target", f"Target reached: {name}",
                             " ".join([message] + extra), item["url"], 4))

    if target is not None:
        edition["target_alerted"] = reached  # resets when the price goes back above the target
    edition["last_good_price"] = price
    return alerts


# ---------------------------------------------------------------------------
# Sending alerts
# ---------------------------------------------------------------------------


def _current_price_text(data, alert):
    item = store.find_item(data, alert["item_id"])
    if not item or not alert.get("edition_key"):
        return ""
    for edition in item["editions"]:
        if edition["key"] == alert["edition_key"]:
            return f" Now {format_price(edition.get('price'), edition.get('currency'))}."
    return ""


def flush_alerts():
    """Send pending alerts, unless it is quiet hours. Alerts that fail stay queued."""
    data = store.load()
    pending = data["pending_alerts"]
    if not pending or snoozed(data["settings"]):
        return
    quiet = in_quiet_hours(data["settings"])
    done = set()
    cutoff = datetime.now(config.TZ) - PENDING_MAX_AGE
    for alert in pending:
        if datetime.fromisoformat(alert["created"]) < cutoff:
            log(f"dropping an alert that could not be sent for 48 hours: {alert['title']}")
            done.add(alert["id"])

    for alert in pending:
        if alert["id"] in done or alert["held"]:
            continue
        if notify.send(alert["title"], alert["message"], click=alert["click"], priority=alert["priority"],
                       actions=alert.get("actions")):
            done.add(alert["id"])
            log(f"sent: {alert['title']}")

    held = [a for a in pending if a["held"] and a["id"] not in done]
    if held and not quiet:
        latest = {}
        for alert in held:  # keep only the newest alert per item edition
            group = "price" if alert["kind"] in ("drop", "target") else "status"
            latest[(alert["item_id"], alert["edition_key"], group)] = alert
        lines = [f"- {a['title']}: {a['message'].split(' Fix it here')[0]}{_current_price_text(data, a)}"
                 for a in latest.values()]
        title = f"Price Watch: {len(lines)} held alert{'s' if len(lines) != 1 else ''}"
        if notify.send(title, "\n".join(lines), click=config.BASE_URL, priority=4):
            done.update(a["id"] for a in held)
            log(f"sent quiet hours summary with {len(lines)} alerts")

    if done:
        with store.update() as fresh:
            fresh["pending_alerts"] = [a for a in fresh["pending_alerts"] if a["id"] not in done]


# ---------------------------------------------------------------------------
# A check run
# ---------------------------------------------------------------------------


def _domain(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _update_backoff(backoff, domain, result):
    """Leave a site alone for a while after it blocks us: 1, 2, 4, then 8 hours."""
    if result.kind == "blocked":
        level = min((backoff.get(domain) or {}).get("level", 0) + 1, 4)
        until = datetime.now(config.TZ) + timedelta(hours=2 ** (level - 1))
        backoff[domain] = {"level": level, "until": until.isoformat(timespec="seconds")}
    elif result.ok:
        backoff[domain] = None


def _summary(item, result):
    prices = ", ".join(
        f"{o.label or o.key}: {format_price(o.price, o.currency)}" for o in result.offers
    ) if result.ok else ""
    return f"{item['name']}: {result.status} [{result.method or '-'}] {prices}"


def run(ids=None, dry_run=False):
    with store.run_lock() as got:
        if not got:
            log("another check is already running, skipping this one")
            return 2
        data = store.load()
        explicit = ids is not None
        todo = [i for i in data["items"] if (i["id"] in ids if explicit else not i.get("paused"))]
        backoff = {}
        stored_backoff = data.get("domain_backoff", {})
        results, last_hit = [], {}

        for n, item in enumerate(todo):
            domain = _domain(item["url"])
            b = stored_backoff.get(domain)
            if b and not explicit and datetime.fromisoformat(b["until"]) > datetime.now(config.TZ):
                log(f"{item['name']}: skipped, {domain} blocked us, trying again after {b['until'][11:16]}")
                continue
            if n:
                time.sleep(random.uniform(1.5, 4))
            wait = POLITE_GAP - (time.time() - last_hit.get(domain, 0))
            if wait > 0:
                time.sleep(wait)
            result = extract.check(item["url"], item.get("selector") or None,
                                   allow_foreign=item.get("allow_foreign", False))
            last_hit[domain] = time.time()
            if looks_suspicious(item, result):
                log(f"{item['name']}: large change or new method, checking again")
                time.sleep(POLITE_GAP)
                result = extract.check(item["url"], item.get("selector") or None,
                                       allow_foreign=item.get("allow_foreign", False))
                last_hit[domain] = time.time()
                result.suspicious = result.ok
            backoff.setdefault(domain, dict(stored_backoff.get(domain) or {}))
            _update_backoff(backoff, domain, result)
            results.append((item["id"], item["url"], result))
            log(_summary(item, result))

        if dry_run:
            scratch = copy.deepcopy(data)
            for item_id, url, result in results:
                for alert in apply_result(store.find_item(scratch, item_id), result):
                    log(f"(dry run, not sent) {alert['title']}: {alert['message']}")
            return 0

        with store.update() as fresh:
            for item_id, url, result in results:
                item = store.find_item(fresh, item_id)
                if item is None or item["url"] != url:
                    continue  # deleted or changed while we were checking
                alerts = apply_result(item, result)
                for alert in alerts:
                    alert["held"] = should_hold(alert, fresh["settings"])
                    log(f"{'holding for later' if alert['held'] else 'queued'}: {alert['title']}")
                fresh["pending_alerts"].extend(alerts)
            for domain, value in backoff.items():
                if value:
                    fresh["domain_backoff"][domain] = value
                else:
                    fresh["domain_backoff"].pop(domain, None)
            fresh["last_run"] = {
                "finished": now_iso(), "checked": len(results),
                "ok": sum(1 for _, _, r in results if r.ok),
                "problems": sum(1 for _, _, r in results if not r.ok),
                "all": not explicit,
            }

        store.daily_backup()
        flush_alerts()
    return 0


def show_test(url, selector=None, allow_foreign=False):
    result = extract.check(url, selector, details=True, allow_foreign=allow_foreign)
    print(f"kind:    {result.kind}")
    print(f"status:  {result.status}")
    print(f"method:  {result.method}")
    print(f"name:    {result.name}")
    if result.suggestion:
        print(f"try:     {result.suggestion}")
    for o in result.offers:
        regular = f" (regular {format_price(o.regular)})" if o.regular else ""
        regular += "" if o.in_stock is None else "  [in stock]" if o.in_stock else "  [OUT OF STOCK]"
        tick = "x" if o.suggested else " "
        print(f"  [{tick}] {o.key}: {o.label} = {format_price(o.price, o.currency)}{regular}"
              f"{'  ' + o.note if o.note else ''}")
    return 0 if result.ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Price Watch checker")
    parser.add_argument("--ids", help="comma separated item ids to check (ignores pause and backoff)")
    parser.add_argument("--dry-run", action="store_true", help="check, but save nothing and send nothing")
    parser.add_argument("--test", metavar="URL", help="show what the price reader finds for a link")
    parser.add_argument("--selector", help="CSS selector to use with --test")
    parser.add_argument("--allow-foreign", action="store_true", help="with --test, skip the Canada check")
    parser.add_argument("--notify-test", action="store_true", help="send a test notification")
    args = parser.parse_args(argv)

    if args.test:
        return show_test(args.test, args.selector, args.allow_foreign)
    if args.notify_test:
        ok = notify.send("Price Watch test", "Notifications are working.", click=config.BASE_URL)
        print("sent" if ok else "not sent")
        return 0 if ok else 1
    ids = set(args.ids.split(",")) if args.ids else None
    return run(ids=ids, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
