"""
Raspberry Pi health for the Status tab: temperature, power, memory, storage,
load, Wi-Fi, uptime, and whether Pi-hole is running. Everything here can be
read without sudo. Anything that cannot be read is left out (None).

Adapted from the Cineplex watcher's dashboard, so both pages show the same tiles.
"""

import os
import re
import shutil
import subprocess

# Bits of `vcgencmd get_throttled`. The low bits mean "right now", the high
# ones "at some point since boot".
THROTTLE_BITS = [
    (0, "Under-voltage now"), (1, "CPU speed capped now"), (2, "Throttled now"), (3, "Temperature limit now"),
    (16, "Under-voltage since boot"), (17, "CPU speed capped since boot"),
    (18, "Throttled since boot"), (19, "Temperature limit since boot"),
]


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def pi_health():
    temp = _read("/sys/class/thermal/thermal_zone0/temp")

    throttled = None
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", run("vcgencmd", "get_throttled"))
    if m:
        bits = int(m.group(1), 16)
        throttled = [label for bit, label in THROTTLE_BITS if bits >> bit & 1]

    meminfo = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        meminfo[key] = int(rest.split()[0]) * 1024 if rest.split() else 0

    disk = shutil.disk_usage("/")
    load = _read("/proc/loadavg").split()
    freq = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    uptime = _read("/proc/uptime").split()

    signal = None
    for line in _read("/proc/net/wireless").splitlines():
        if line.strip().startswith("wlan0:"):
            signal = int(float(line.split()[3]))
    ssid = re.search(r"SSID: (.+)", run("/usr/sbin/iw", "dev", "wlan0", "link"))

    return {
        "temp_c": round(int(temp) / 1000, 1) if temp else None,
        "throttled": throttled,
        "mem_total": meminfo.get("MemTotal"),
        "mem_available": meminfo.get("MemAvailable"),
        "swap_used": meminfo.get("SwapTotal", 0) - meminfo.get("SwapFree", 0),
        "disk_total": disk.total,
        "disk_free": disk.free,
        "load": [float(x) for x in load[:3]] if load else None,
        "cores": os.cpu_count(),
        "cpu_mhz": int(freq) // 1000 if freq else None,
        "wifi_ssid": ssid.group(1).strip() if ssid else None,
        "wifi_dbm": signal,
        "uptime_seconds": float(uptime[0]) if uptime else None,
        "pihole": run("systemctl", "is-active", "pihole-FTL") or None,
    }
