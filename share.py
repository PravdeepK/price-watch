"""
The read-only share page, for showing your watchlist to someone else.

It is a separate little web app. The main service serves it on its own port
(PW_SHARE_PORT, 5001, only on 127.0.0.1), and Tailscale Funnel points the
public address at that port. So from the internet, only this app can be
reached: it has no buttons and no way to change anything, and it never shows
the ntfy topic, settings, alerts, logs, or Pi details.

The page lives under a secret path, /s/<PW_SHARE_TOKEN>. Everything else is
"not found". With no token set, sharing is off.
"""

import secrets

from flask import Flask, abort, jsonify, send_from_directory

import config
import store

app = Flask(__name__, static_folder=None)


def _check(token):
    if not config.SHARE_TOKEN or not secrets.compare_digest(token, config.SHARE_TOKEN):
        abort(404)


def share_link():
    return f"{config.SHARE_BASE_URL}/s/{config.SHARE_TOKEN}" if config.SHARE_TOKEN else ""


def _public_item(item):
    """Only what the share page shows. No ids, settings, selectors, or alert state."""
    failing = item.get("status_kind") not in ("ok", "new", "no_price", "partial")
    return {
        "name": item.get("name", ""),
        "site": item.get("site", ""),
        "url": item.get("url", ""),
        "image": item.get("image", ""),
        "target": item.get("target"),
        "last_checked": item.get("last_checked"),
        "problem": failing,
        "not_canadian": bool(item.get("allow_foreign")),
        "editions": [{k: e.get(k) for k in ("label", "price", "currency", "regular", "in_stock", "lowest_price")}
                     for e in item.get("editions", [])],
    }


@app.after_request
def headers(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/s/<token>")
@app.get("/s/<token>/")
def page(token):
    _check(token)
    return send_from_directory(config.HERE / "static", "share.html")


@app.get("/s/<token>/data")
def data(token):
    _check(token)
    items = [i for i in store.load()["items"] if not i.get("paused")]
    return jsonify({"items": [_public_item(i) for i in sorted(items, key=lambda i: i.get("name", "").lower())]})


@app.errorhandler(404)
def not_found(e):
    return "Not found", 404


class ByPort:
    """Send requests on the share port to the share app, everything else to the main app."""

    def __init__(self, main_app):
        self.main_app = main_app

    def __call__(self, environ, start_response):
        if str(environ.get("SERVER_PORT")) == str(config.SHARE_PORT):
            return app(environ, start_response)
        return self.main_app(environ, start_response)
