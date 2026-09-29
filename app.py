#!/usr/bin/env python3
"""
The Price Watch web page, served by waitress (see deploy/price-watch.service).

The page itself is one file (static/index.html) that talks to the small JSON
API below, the same way the Cineplex watcher's dashboard works.

Open http://<this machine's name>.local:5000 from a device on the home network.
For a quick local run:  .venv/bin/python app.py
"""

import copy
import os
import re
import secrets
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request, send_from_directory

import config
import extract
import health
import notify
import price_watch as pw
import search
import share
import sites
import store
from extract import format_price

app = Flask(__name__, static_folder=None)

BAD_NUMBERS = ("Check the target price and minimum drop. Use a number like 49.99 for the target, "
               "and 5 or 5% for the minimum drop.")


class ApiError(Exception):
    """A problem to show the person using the page, as a red message."""


@app.errorhandler(ApiError)
def api_error(e):
    return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------------------
# Request checks
# ---------------------------------------------------------------------------


@app.before_request
def guard():
    if config.PW_USER and config.PW_PASSWORD:
        auth = request.authorization
        if not auth or not (secrets.compare_digest(auth.username or "", config.PW_USER)
                            and secrets.compare_digest(auth.password or "", config.PW_PASSWORD)):
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Price Watch"'})
    if request.method == "POST":
        # Requiring a JSON body means other websites open in the browser cannot
        # post here (browsers will not send cross-site JSON without asking first,
        # and this server never agrees). The Origin check is a second guard.
        if request.mimetype != "application/json":
            return jsonify({"error": "Send JSON"}), 415
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if source and urlparse(source).netloc != request.host:
            return jsonify({"error": "Not allowed"}), 403


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Send JSON")
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def cents_from_text(text):
    text = str(text or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(text)
    if value <= 0 or value > 1_000_000:
        raise ValueError(text)
    return int((value * 100).quantize(Decimal(1)))


def start_check(ids=None):
    """Run the checker in the background so the page does not wait for it."""
    if store.check_running():
        return False
    args = [sys.executable, str(config.HERE / "price_watch.py")]
    if ids:
        args += ["--ids", ",".join(ids)]
    proc = subprocess.Popen(args, cwd=config.HERE, start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()  # collect it when it ends
    return True


def result_json(r):
    return {
        "kind": r.kind, "ok": r.ok, "status": r.status, "method": r.method, "name": r.name,
        "image": r.image, "suggestion": r.suggestion,
        "offers": [{"key": o.key, "label": o.label, "price": o.price, "currency": o.currency,
                    "regular": o.regular, "note": o.note, "seller": o.seller, "suggested": o.suggested,
                    "in_stock": o.in_stock}
                   for o in r.offers],
    }


def _systemd_time(text):
    """'Wed 2026-09-23 23:00:45 EDT' as an ISO time, or None."""
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", text or "")
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=config.TZ).isoformat()


def service_status(data):
    def show(unit, prop):
        return health.run("systemctl", "show", unit, "-p", prop, "--value")

    return {
        "web": "active",
        "web_since": _systemd_time(show("price-watch.service", "ActiveEnterTimestamp")),
        "timer": health.run("systemctl", "is-active", "price-watch-check.timer") or "unknown",
        "enabled": health.run("systemctl", "is-enabled", "price-watch-check.timer") or "unknown",
        "next_check": _systemd_time(show("price-watch-check.timer", "NextElapseUSecRealtime")),
        "last_run": data.get("last_run"),
        "running": store.check_running(),
        "topic": config.NTFY_TOPIC,
        "server": config.NTFY_SERVER,
    }


def recent_log(lines=200):
    out = health.run("journalctl", "-u", "price-watch-check", "-u", "price-watch", "-n", str(lines),
                     "--no-pager", "-o", "cat")
    noise = re.compile(r"Consumed .* CPU time|Deactivated successfully|Scheduled restart job|^INFO:waitress")
    return [line for line in out.splitlines() if line.strip() and not line.startswith("-- ") and not noise.search(line)]


def with_cart_links(items):
    for item in items:
        for e in item["editions"]:
            e["cart"] = sites.cart_links(item["url"], e["key"])
    return items


def dashboard_json():
    data = store.load()
    settings = data["settings"]
    held = [a for a in data["pending_alerts"] if a.get("held")]
    return {
        "items": with_cart_links(data["items"]),
        "settings": {**settings, "quiet_now": pw.in_quiet_hours(settings), "snoozed": pw.snoozed(settings)},
        "held": [{"title": a["title"], "message": a["message"].split(" Fix it here")[0], "created": a["created"]}
                 for a in held],
        "status": {**service_status(data), "share_link": share.share_link()},
        "pi": health.pi_health(),
        "log": recent_log(),
        "now": pw.now_iso(),
    }


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    response = send_from_directory(config.HERE / "static", "index.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


# Safari's icon for Favourites and the Home Screen. Its own file name, because Safari
# mixed up the icons of the Pi's pages when they had none.
@app.get("/touch-icon-pricewatch.png")
def touch_icon():
    response = send_from_directory(config.HERE / "static", "touch-icon-pricewatch.png")
    response.headers["Cache-Control"] = "max-age=86400"
    return response


# Older links (for example in "Cannot read" notifications) open the item's settings.
@app.get("/item/<item_id>")
def old_item_link(item_id):
    return Response(status=302, headers={"Location": f"/#item-{item_id}"})


@app.get("/settings")
def old_settings_link():
    return Response(status=302, headers={"Location": "/#alerts"})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/dashboard")
def api_dashboard():
    return jsonify(dashboard_json())


@app.get("/api/search/stores")
def api_search_stores():
    return jsonify([{"key": k, "name": name} for k, (name, _) in search.STORES.items()])


@app.get("/api/search")
def api_search():
    key = request.args.get("store", "")
    q = request.args.get("q", "").strip()
    if key not in search.STORES:
        raise ApiError("Unknown store.")
    if len(q) < 2:
        raise ApiError("Type at least 2 letters.")
    return jsonify(search.search(key, q))


@app.post("/api/test")
def api_test():
    data = body()
    url = str(data.get("url", "")).strip()
    try:
        extract.validate_url(url)
    except extract.FetchError as e:
        raise ApiError(e.message[:1].upper() + e.message[1:] + ".")
    result = extract.check(url, str(data.get("selector") or "").strip() or None, details=True,
                           allow_foreign=bool(data.get("allow_foreign")))
    return jsonify({"result": result_json(result)})


def _item_or_error(data, item_id):
    item = store.find_item(data, str(item_id))
    if item is None:
        raise ApiError("That item is not on the watchlist anymore.")
    return item


@app.post("/api/items/add")
def api_add():
    data = body()
    url = str(data.get("url", "")).strip()
    allow_foreign = bool(data.get("allow_foreign"))
    try:
        extract.validate_url(url)
    except extract.FetchError as e:
        raise ApiError(e.message[:1].upper() + e.message[1:] + ".")
    verdict, message, _ = extract.canada_check(url)
    if verdict == "no" and not allow_foreign:
        raise ApiError(message)
    try:
        target = cents_from_text(data.get("target"))
        min_drop = pw.parse_min_drop(str(data.get("min_drop") or ""))
    except ValueError:
        raise ApiError(BAD_NUMBERS)

    editions = []
    for pair in data.get("editions") or []:
        if isinstance(pair, list) and len(pair) == 2:
            editions.append(pw.new_edition(str(pair[0])[:200], str(pair[1])[:200]))
    name = str(data.get("name") or "").strip()[:200] or urlparse(url).hostname
    item = pw.new_item(url, name, editions, target=target, min_drop=min_drop,
                       selector=str(data.get("selector") or "").strip()[:300])
    item["mute_until_working"] = not data.get("found")  # saved without a price: no "cannot read" ping for that
    item["allow_foreign"] = allow_foreign
    item["stock_alert"] = bool(data.get("stock_alert"))
    item["image"] = extract.clean_image(data.get("image") or "")
    with store.update() as d:
        d["items"].append(item)
    started = start_check([item["id"]])
    return jsonify({"message": f"Added {name}." + ("" if started else " It gets its first price at the next check."),
                    "id": item["id"]})


@app.post("/api/items/update")
def api_update():
    data = body()
    changes = data.get("changes") or {}
    recheck = False
    with store.update() as d:
        item = _item_or_error(d, data.get("id"))
        if "name" in changes:
            item["name"] = str(changes["name"]).strip()[:200] or item["name"]
        try:
            if "target" in changes:
                target = cents_from_text(changes["target"])
                if target != item.get("target"):
                    item["target"] = target
                    for e in item["editions"]:
                        e["target_alerted"] = False  # so the new target can alert
            if "min_drop" in changes:
                item["min_drop"] = pw.parse_min_drop(str(changes["min_drop"] or ""))
        except ValueError:
            raise ApiError(BAD_NUMBERS)
        for flag in ("paused", "muted", "stock_alert"):
            if flag in changes:
                item[flag] = bool(changes[flag])
        if "selector" in changes:
            selector = str(changes["selector"] or "").strip()[:300]
            if selector != (item.get("selector") or ""):
                item["selector"] = selector
                item.update(failure_alerted=False, consecutive_failures=0, mute_until_working=False)
                recheck = True
        if "keep" in changes:
            keep = set(changes["keep"] or [])
            kept = [e for e in item["editions"] if e["key"] in keep]
            if not kept:
                raise ApiError("Keep at least one edition, or remove the item instead.")
            item["editions"] = kept
        name, muted, paused, stock_alert = item["name"], item.get("muted"), item.get("paused"), item.get("stock_alert")
    if recheck:
        start_check([str(data.get("id"))])
    if "muted" in changes:
        message = f"Muted {name}. It is still checked, but sends no alerts." if muted else f"Unmuted {name}."
    elif "stock_alert" in changes and len(changes) == 1:
        message = (f"You'll get an alert when {name} is back in stock." if stock_alert
                   else f"Back in stock alert off for {name}.")
    elif "paused" in changes and len(changes) == 1:
        message = f"Paused {name}. It is not checked until you resume it." if paused else f"Resumed {name}."
    else:
        message = "Saved." + (" Checking with the new selector now." if recheck else "")
    return jsonify({"message": message})


@app.post("/api/items/remove")
def api_remove():
    data = body()
    with store.update() as d:
        item = _item_or_error(d, data.get("id"))
        d["items"] = [i for i in d["items"] if i["id"] != item["id"]]
        d["pending_alerts"] = [a for a in d["pending_alerts"] if a["item_id"] != item["id"]]
    return jsonify({"message": f"Removed {item['name']}.", "item": item})


@app.post("/api/items/restore")
def api_restore():
    """Undo for Remove: put back the exact item the page was handed."""
    item = body().get("item")
    if not isinstance(item, dict) or not isinstance(item.get("editions"), list):
        raise ApiError("Nothing to restore.")
    try:
        extract.validate_url(str(item.get("url", "")))
    except extract.FetchError:
        raise ApiError("Nothing to restore.")
    item = copy.deepcopy(item)
    item["image"] = extract.clean_image(item.get("image") or "")
    with store.update() as d:
        if store.find_item(d, item.get("id")):
            item["id"] = uuid.uuid4().hex[:8]
        d["items"].append(item)
    return jsonify({"message": f"Put back {item.get('name', 'the item')}."})


@app.post("/api/check")
def api_check():
    ids = body().get("ids")
    ids = [str(i) for i in ids] if isinstance(ids, list) and ids else None
    if not start_check(ids):
        raise ApiError("A check is already running.")
    return jsonify({"message": "Checking now." if ids else "Checking all items now."})


@app.post("/api/settings")
def api_settings():
    changes = body().get("settings") or {}
    with store.update() as d:
        s = d["settings"]
        for flag in ("quiet_enabled", "quiet_allow_stock"):
            if flag in changes:
                s[flag] = bool(changes[flag])
        for key in ("quiet_start", "quiet_end"):
            if key in changes:
                value = str(changes[key] or "")
                if value and pw._minutes(value) is None:
                    raise ApiError("Use a time like 23:00.")
                s[key] = value
        if "snooze_hours" in changes:
            try:
                hours = float(changes["snooze_hours"] or 0)
            except (TypeError, ValueError):
                raise ApiError("Pick how long to snooze for.")
            s["snooze_until"] = (datetime.now(config.TZ) + timedelta(hours=hours)).isoformat(timespec="seconds") \
                if hours > 0 else ""
        settings = dict(s)
    if "snooze_hours" in changes:
        if settings["snooze_until"]:
            until = datetime.fromisoformat(settings["snooze_until"])
            message = f"Alerts snoozed until {until:%a %-I:%M %p}. They are held, not lost."
        else:
            message = "Alerts resumed."
            threading.Thread(target=pw.flush_alerts, daemon=True).start()  # send anything that was held
    elif "quiet_enabled" in changes:
        message = "Quiet hours on." if settings["quiet_enabled"] else "Quiet hours off."
    elif "quiet_allow_stock" in changes:
        message = ("Back in stock alerts now come through during quiet hours." if settings["quiet_allow_stock"]
                   else "Back in stock alerts now wait for the end of quiet hours too.")
    else:
        message = "Saved."
    return jsonify({"message": message})


@app.post("/api/test-notification")
def api_test_notification():
    if not notify.send("Price Watch test", "Notifications are working.", click=config.BASE_URL):
        raise ApiError("Could not send. Check NTFY_TOPIC in .env and the log on the Status tab.")
    return jsonify({"message": "Test notification sent."})


@app.post("/api/alerts/preview")
def api_preview():
    """A sample price drop or back in stock alert, using a real item on the watchlist."""
    kind = body().get("kind", "drop")
    priced = [(i, e) for i in store.load()["items"] for e in i["editions"] if e.get("price")]
    if not priced:
        raise ApiError("Add an item with a price first.")
    if kind == "stock":
        item, edition = next(((i, e) for i, e in priced if i.get("stock_alert")), priced[0])
        buttons = sites.cart_links(item["url"], edition["key"])
        ok = notify.send(f"Sample - Back in stock: {pw.edition_name(item, edition)}",
                         f"Now {format_price(edition['price'], edition.get('currency'))}. It can sell out again quickly. "
                         "This is only a sample; the stock did not change."
                         + (" The buttons are real: Buy now opens checkout, but nothing is bought unless you finish it."
                            if buttons else ""),
                         click=item["url"], priority=5, actions=buttons + [("Open page", item["url"])] if buttons else None)
        if not ok:
            raise ApiError("Could not send. Check NTFY_TOPIC in .env and the log on the Status tab.")
        return jsonify({"message": "Sample back in stock alert sent."})
    item, edition = priced[0]
    now, cur = edition["price"], edition.get("currency")
    was = now * 6 // 5 // 100 * 100 + 99
    ok = notify.send(f"Sample - Price drop: {pw.edition_name(item, edition)}",
                     f"Now {format_price(now, cur)} (was {format_price(was, cur)}). "
                     "This is only a sample; the price did not change.", click=item["url"], priority=4)
    if not ok:
        raise ApiError("Could not send. Check NTFY_TOPIC in .env and the log on the Status tab.")
    return jsonify({"message": "Sample alert sent."})


# What waitress serves (see deploy/price-watch.service): the main page on port
# 5000, and the read-only share page on the share port (see share.py).
application = share.ByPort(app)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
