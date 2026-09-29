"""
Sending push notifications through ntfy.

Messages are published as JSON, so names with characters like the trademark
sign work in titles (plain HTTP headers cannot carry them).
"""

import time

import requests

import config


def send(title, message, click=None, priority=3, tags=None, actions=None):
    """Send one notification. Returns True only if ntfy accepted it.

    actions is a list of (label, url) buttons shown on the notification (ntfy allows 3).
    """
    if not config.NTFY_TOPIC:
        print(f"[NTFY_TOPIC is not set, not sent] {title}: {message}")
        return False
    payload = {
        "topic": config.NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": ["price-watch"] + list(tags or []),
    }
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = [{"action": "view", "label": label, "url": url, "clear": True}
                              for label, url in actions[:3]]
    for attempt in range(3):
        try:
            r = requests.post(config.NTFY_SERVER, json=payload, timeout=15)
            if r.ok:
                return True
            print(f"ntfy refused the message (HTTP {r.status_code}): {r.text[:200]}")
        except requests.RequestException as e:
            print(f"could not reach ntfy ({e.__class__.__name__})")
        time.sleep(2 * (attempt + 1))
    return False
