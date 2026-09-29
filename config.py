"""
Settings shared by the checker and the web UI.

Secrets and settings live in .env next to this file (see .env.example). The
systemd units load it too, so running a script by hand behaves the same way.
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent


def _load_env_file(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(HERE / ".env")

TZ = ZoneInfo("America/Toronto")
DATA_FILE = Path(os.environ.get("PW_DATA_FILE", HERE / "watchlist.json"))
BACKUP_DIR = HERE / "backups"
BASE_URL = os.environ.get("PW_BASE_URL", "http://localhost:5000").rstrip("/")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
PW_USER = os.environ.get("PW_USER", "")
# Canadian Tire prices and stock are per store. No default: set CT_STORE_ID in .env.
CT_STORE_ID = os.environ.get("CT_STORE_ID", "")
# Canadian Tire's public web API key. If blank or out of date, the reader reads
# the current one from the product page.
CT_API_KEY = os.environ.get("CT_API_KEY", "")
PW_PASSWORD = os.environ.get("PW_PASSWORD", "")
# Read-only share page, served on its own port for Tailscale Funnel. Blank token: sharing is off.
SHARE_PORT = int(os.environ.get("PW_SHARE_PORT", "5001"))
SHARE_TOKEN = os.environ.get("PW_SHARE_TOKEN", "")
SHARE_BASE_URL = os.environ.get("PW_SHARE_BASE_URL", "").rstrip("/")
