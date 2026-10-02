#!/usr/bin/env python3
"""
reachy_connect.py — find the robot and point the stack at it, without the
Reachy Mini desktop app.

The desktop app turns out to be just a GUI client: the robot runs its own
FastAPI daemon on port 8000 and serves everything over plain HTTP —
including WiFi provisioning (`/wifi/*`) — so nothing here needs the app, or
the SDK. Confirmed by `/api/daemon/status` reporting desktop_app_daemon:false
while the app was not running at all.

What actually bites in daily use is that the robot's DHCP lease moves, and
every service in this repo reads REACHY_URL once at startup and then holds
that address forever. So this module does two things:

    discover()        find candidate robots: the mDNS name, whatever .env
                      currently says, the AP fallback, and a sweep of the
                      local /24 for anything answering the daemon API
    set_env_url()     rewrite REACHY_URL in .env so the next start sticks

plus thin wrappers over the robot's own WiFi endpoints, so you can move it
to a new network from the dashboard instead of the desktop app.

Deliberately stdlib-only: it has to import into the dashboard's plain
system-python process, which has no venv.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
MDNS_NAME = os.environ.get("REACHY_MDNS", "reachy-mini.local")
PORT = 8000
# The robot's own access point, for when there's no shared network at all.
AP_ADDRESSES = ("10.42.0.1", "192.168.4.1")


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def _get(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _port_open(host: str, port: int = PORT, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def probe(host: str, timeout: float = 2.5) -> dict | None:
    """Ask a candidate address whether it's actually a Reachy daemon.

    Identity comes from /api/daemon/hardware-id, so two robots on one network
    are distinguishable rather than both showing up as 'a robot'.
    """
    base = f"http://{host}:{PORT}"
    st = _get(f"{base}/api/daemon/status", timeout)
    if not st or st.get("type") != "daemon_status":
        return None
    hw = _get(f"{base}/api/daemon/hardware-id", 1.5) or {}
    wifi = _get(f"{base}/wifi/status", 1.5) or {}
    return {
        "host": host,
        "url": base,
        "name": st.get("robot_name"),
        "state": st.get("state"),
        "wireless": st.get("wireless_version"),
        "hardware_id": hw.get("hardware_id"),
        "network": wifi.get("connected_network"),
    }


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def resolve_mdns(name: str = MDNS_NAME) -> str | None:
    """Resolve a .local name to an address.

    Uses ping rather than socket.getaddrinfo because that's what actually
    works reliably for mDNS here (and it's what start_wonder.sh already
    does). Returns None on guest networks, where multicast is blocked.
    """
    try:
        out = subprocess.run(["ping", "-c1", "-t2", name],
                             capture_output=True, text=True, timeout=4).stdout
        m = re.search(r"\(([0-9.]+)\)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def local_subnet() -> str | None:
    """The /24 this Mac is on, as a '10.0.0' style prefix."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # no packets sent; just picks a route
        ip = s.getsockname()[0]
        s.close()
        return ip.rsplit(".", 1)[0]
    except Exception:
        return None


def current_env_url() -> str | None:
    try:
        for line in open(ENV_PATH):
            if line.strip().startswith("REACHY_URL="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def discover(scan: bool = True) -> dict:
    """Find every reachable robot, cheapest lookups first.

    The subnet sweep is the slow path (254 hosts), so it only runs when the
    quick candidates — mDNS, whatever .env says, the AP — turn up nothing,
    unless the caller insists.
    """
    seen: dict[str, dict] = {}      # keyed by hardware_id, not address
    tried: set[str] = set()
    notes: list[str] = []

    def add(host: str, how: str):
        """Record a reachable robot, merging addresses for the same machine.

        The mDNS name and the IP behind it are the same robot, so key on
        hardware_id and keep every address we found it at. Prefer a numeric
        address as the primary: the SDK's GStreamer/WebRTC path is happier
        with a literal IP than with a .local name.
        """
        if not host or host in tried:
            return
        tried.add(host)
        info = probe(host)
        if not info:
            return
        key = info.get("hardware_id") or host
        prev = seen.get(key)
        if prev is None:
            info["found_by"] = how
            info["addresses"] = [host]
            seen[key] = info
            return
        prev["addresses"].append(host)
        prev["found_by"] += f", {how}"
        is_ip = re.match(r"^\d+\.\d+\.\d+\.\d+$", host)
        if is_ip and not re.match(r"^\d+\.\d+\.\d+\.\d+$", prev["host"]):
            prev["host"], prev["url"] = host, f"http://{host}:{PORT}"

    mdns = resolve_mdns()
    if mdns:
        add(mdns, "mdns")
    else:
        notes.append(f"{MDNS_NAME} did not resolve — normal on guest/hotel WiFi, "
                     "where multicast and device-to-device traffic are blocked")

    env = current_env_url()
    if env:
        host = urllib.parse.urlparse(env).hostname
        if host:
            add(host, "env")

    for ap in AP_ADDRESSES:
        if _port_open(ap, timeout=0.25):
            add(ap, "robot access point")

    if scan and not seen:
        prefix = local_subnet()
        if prefix:
            notes.append(f"swept {prefix}.0/24")
            with ThreadPoolExecutor(max_workers=64) as pool:
                hosts = [f"{prefix}.{i}" for i in range(1, 255)]
                for host, is_open in zip(hosts, pool.map(_port_open, hosts)):
                    if is_open:
                        add(host, "subnet scan")
        else:
            notes.append("could not determine the local subnet")

    return {"robots": list(seen.values()), "env_url": env, "notes": notes}


# --------------------------------------------------------------------------- #
# find_robot: the one answer to "where is it right now?"
# --------------------------------------------------------------------------- #
HOTSPOT = "10.42.0.1"


def is_robot(host: str, timeout: float = 1.5, port: int = PORT) -> dict | None:
    """One GET to /api/daemon/status. Returns the status dict if a Reachy Mini
    daemon answered, else None. Cheap enough to call every few seconds."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/daemon/status",
                                    timeout=timeout) as r:
            body = r.read().decode(errors="replace")
    except Exception:
        return None
    if "reachy_mini" not in body:
        return None
    try:
        return json.loads(body)
    except Exception:
        return {"raw": body}


def _iface_net() -> tuple[str, int] | None:
    """(this Mac's IP, prefix length) on the default-route interface.

    The AGIHouse network was a /21, so assuming /24 misses the robot entirely.
    Capped at /21 (2046 hosts) so a /16 coffee-shop network can't turn the
    sweep into a minute-long scan.
    """
    try:
        out = subprocess.run(["route", "-n", "get", "default"], capture_output=True,
                             text=True, timeout=3).stdout
        ifc = re.search(r"interface:\s*(\S+)", out).group(1)
        ip = subprocess.run(["ipconfig", "getifaddr", ifc], capture_output=True,
                            text=True, timeout=3).stdout.strip()
        mask = subprocess.run(["ipconfig", "getoption", ifc, "subnet_mask"],
                              capture_output=True, text=True, timeout=3).stdout.strip()
        bits = sum(bin(int(o)).count("1") for o in mask.split(".")) if mask else 24
        if ip:
            return ip, max(21, min(bits, 30))
    except Exception:
        pass
    prefix = local_subnet()
    return (prefix + ".1", 24) if prefix else None


def sweep(timeout: float = 0.8) -> str | None:
    """Parallel probe of every host on the Mac's subnet. First hit wins."""
    import ipaddress
    from concurrent.futures import as_completed
    net = _iface_net()
    if not net:
        return None
    me = net[0]
    hosts = [str(h) for h in ipaddress.ip_network(f"{net[0]}/{net[1]}", strict=False).hosts()
             if str(h) != me]
    with ThreadPoolExecutor(max_workers=128) as pool:
        futs = {pool.submit(is_robot, h, timeout): h for h in hosts}
        for f in as_completed(futs):
            if f.result():
                for other in futs:
                    other.cancel()
                return futs[f]
    return None


def find_robot(current: str | None = None, scan: bool = True,
               log=lambda m: None) -> str | None:
    """Base URL (literal IP preferred) of the robot, or None.

    Order: the address we were already using, the mDNS name, the robot's own
    hotspot, then a sweep of this Mac's subnet. mDNS has died on its own
    (2026-09-22) and the DHCP lease moves, so no single one of these can be
    trusted alone.
    """
    tried = []
    for cand in (current, current_env_url()):
        host = urllib.parse.urlparse(cand).hostname if cand else None
        if host and host not in tried:
            tried.append(host)
            if host.endswith(".local"):
                ip = resolve_mdns(host)
                if ip and is_robot(ip):
                    log(f"found via {host} → {ip}")
                    return f"http://{ip}:{PORT}"
            elif is_robot(host):
                return f"http://{host}:{PORT}"
    if MDNS_NAME not in tried:
        ip = resolve_mdns()
        if ip and is_robot(ip):
            log(f"found via mDNS {MDNS_NAME} → {ip}")
            return f"http://{ip}:{PORT}"
    if is_robot(HOTSPOT, 1.0):
        log("found on the robot's own hotspot")
        return f"http://{HOTSPOT}:{PORT}"
    if scan:
        hit = sweep()
        if hit:
            log(f"found by subnet sweep at {hit}")
            return f"http://{hit}:{PORT}"
    return None


def env_points_at(url: str) -> bool:
    """Does what .env says still lead to this robot? A .local name that
    resolves to it counts, so the deliberate mDNS setting is left alone."""
    env = current_env_url()
    if not env:
        return False
    want = urllib.parse.urlparse(url).hostname
    host = urllib.parse.urlparse(env).hostname or ""
    if host == want:
        return True
    return host.endswith(".local") and resolve_mdns(host) == want


# --------------------------------------------------------------------------- #
# Pointing the stack at a robot
# --------------------------------------------------------------------------- #
def set_env_url(url: str) -> str:
    """Rewrite REACHY_URL in .env, preserving the rest of the file.

    Every service reads this at startup, so this is what makes a new address
    stick across restarts. Callers still have to restart (or re-point) the
    already-running services.
    """
    url = url.rstrip("/")
    if not re.match(r"^https?://[^\s/]+$", url):
        raise ValueError(f"not a bare robot URL: {url!r}")
    try:
        lines = open(ENV_PATH).read().splitlines()
    except FileNotFoundError:
        lines = []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith("REACHY_URL="):
            out.append(f"REACHY_URL={url}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"REACHY_URL={url}")
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(out) + "\n")
    return url


# --------------------------------------------------------------------------- #
# WiFi — thin wrappers over the robot's own endpoints
# --------------------------------------------------------------------------- #
def _post(base: str, path: str, params: dict | None = None, timeout: float = 30.0):
    url = f"{base.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    try:
        return json.loads(body)
    except Exception:
        return {"raw": body}


def wifi_status(base: str) -> dict:
    return _get(f"{base.rstrip('/')}/wifi/status", 6.0) or {}


def wifi_scan(base: str) -> list:
    nets = _post(base, "/wifi/scan_and_list", timeout=40.0)
    if isinstance(nets, list):
        # The scan returns duplicates and a blank entry for hidden networks.
        return sorted({n for n in nets if n})
    return []


def wifi_join(base: str, ssid: str, password: str) -> dict:
    """Join a network.

    The robot's API takes the password as a QUERY parameter, so it lands in
    the daemon's request log on the robot. That's their API shape, not ours —
    we take it in a POST body and only build the query for this final LAN
    hop, so it never appears in the dashboard's own URLs or logs.
    """
    return _post(base, "/wifi/connect", {"ssid": ssid, "password": password},
                 timeout=45.0)


def wifi_forget(base: str, ssid: str) -> dict:
    return _post(base, "/wifi/forget", {"ssid": ssid}, timeout=20.0)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "find":
        # python3 reachy_connect.py find [--quick] [--write] [--current URL]
        # Prints the robot's base URL on stdout (nothing, exit 1, if not found).
        # --write rewrites .env only when what it says no longer leads there.
        args = sys.argv[2:]
        cur = args[args.index("--current") + 1] if "--current" in args else None
        url = find_robot(cur, scan="--quick" not in args,
                         log=lambda m: print(m, file=sys.stderr))
        if not url:
            sys.exit(1)
        if "--write" in args and not env_points_at(url):
            set_env_url(url)
            print(f".env REACHY_URL → {url}", file=sys.stderr)
        print(url)
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "wifi":
        url = current_env_url() or "http://reachy-mini.local:8000"
        print(json.dumps(wifi_status(url), indent=2))
        print("\n".join(wifi_scan(url)))
    else:
        res = discover()
        for n in res["notes"]:
            print(f"note: {n}")
        if not res["robots"]:
            print("no robot found")
        for r in res["robots"]:
            print(f"{r['url']:28s} {r['name']}  state={r['state']}  "
                  f"net={r['network']}  id={r['hardware_id']}  via {r['found_by']}")
