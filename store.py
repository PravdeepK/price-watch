"""
Loading and saving watchlist.json.

The checker and the web UI both write the file, so every change goes through
update(), which holds a file lock while it reads, changes, and writes. Writes
go to a temporary file first and then replace the real one, so a power cut
cannot leave a half-written file. The previous version is kept as .bak.

A second lock (check.lock) makes sure only one check run happens at a time.
"""

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime

from config import BACKUP_DIR, DATA_FILE, TZ

SCHEMA_VERSION = 1


def lock_path():
    return DATA_FILE.with_name(DATA_FILE.name + ".lock")


def run_lock_path():
    return DATA_FILE.with_name("check.lock")


def empty():
    return {
        "schema_version": SCHEMA_VERSION,
        "settings": {"quiet_enabled": True, "quiet_start": "", "quiet_end": "", "snooze_until": "",
                     "quiet_allow_stock": False},
        "pending_alerts": [],
        "domain_backoff": {},
        "items": [],
    }


@contextmanager
def _locked(shared=False):
    with open(lock_path(), "a") as f:
        fcntl.flock(f, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read():
    path = DATA_FILE
    if not path.exists():
        backup = path.with_name(path.name + ".bak")
        if not backup.exists():
            return empty()
        path = backup
    data = json.loads(path.read_text())
    base = empty()
    base.update(data)
    base["settings"] = {**empty()["settings"], **base.get("settings", {})}
    return base


def _write(data):
    tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if DATA_FILE.exists():
        shutil.copy2(DATA_FILE, DATA_FILE.with_name(DATA_FILE.name + ".bak"))
    os.replace(tmp, DATA_FILE)
    dir_fd = os.open(DATA_FILE.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def load():
    """Read the watchlist. Do not write back what this returns; use update()."""
    with _locked(shared=True):
        return _read()


@contextmanager
def update():
    """Read, let the caller change the data, then write it back, all under the lock.

    If the caller raises, nothing is written.
    """
    with _locked():
        data = _read()
        yield data
        _write(data)


def find_item(data, item_id):
    return next((i for i in data["items"] if i["id"] == item_id), None)


@contextmanager
def run_lock():
    """Yield True if this process may run a check, False if one is already running."""
    with open(run_lock_path(), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def check_running():
    with run_lock() as got:
        return not got


def daily_backup(keep=7):
    """Copy watchlist.json into backups/ once a day and keep the last few copies."""
    if not DATA_FILE.exists():
        return
    BACKUP_DIR.mkdir(exist_ok=True)
    target = BACKUP_DIR / f"watchlist-{datetime.now(TZ):%Y-%m-%d}.json"
    if not target.exists():
        with _locked(shared=True):
            shutil.copy2(DATA_FILE, target)
    for old in sorted(BACKUP_DIR.glob("watchlist-*.json"))[:-keep]:
        old.unlink()
