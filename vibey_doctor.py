#!/usr/bin/env python3
"""
vibey_doctor.py — answers "why is it broken this time?" in about five seconds.

Written after an afternoon lost to "Claude can't connect to the internet",
which turned out to be neither Claude nor the internet: the laptop had drifted
onto an iPhone hotspot. Every symptom follows from that one fact, and none of
them say so. This script says so.

    python3 vibey_doctor.py            # diagnose
    python3 vibey_doctor.py --find     # also scan the LAN for the robot
    python3 vibey_doctor.py --fix-ip   # scan, then rewrite REACHY_URL in .env

Checks, in the order that actually matters:
  1. What network am I on, and is macOS throttling it?
  2. Can I reach the internet, and how jittery is the path?
  3. Can I reach the robot — and if not, is it even on this subnet?
  4. Are the local services up?

Stdlib only. Safe to run any time; nothing is changed without --fix-ip.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).parent.resolve()
ENV_PATH = REPO / ".env"

OK, WARN, BAD, INFO = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m", " "
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"

SERVICES = {
    8770: "dashboard", 8771: "camera", 8772: "voice chat",
    8773: "face memory", 8774: "vibeverse", 8775: "robot mic",
}

# Apple hands out 172.20.10.0/28 to Personal Hotspot clients — a dead giveaway.
HOTSPOT_NET = "172.20.10."


def sh(*cmd, timeout=8) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return ""


def read_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def head(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")


# --------------------------------------------------------------------------- #
# 1. The link
# --------------------------------------------------------------------------- #
def check_link() -> dict:
    head("1. Network link")
    ip = sh("ipconfig", "getifaddr", "en0").strip()
    gw = ""
    for line in sh("route", "-n", "get", "default").splitlines():
        if "gateway:" in line:
            gw = line.split(":", 1)[1].strip()
    ifc = sh("ifconfig", "en0")
    constrained = "constrained" in ifc
    ssid = ""
    m = re.search(r"Current Wi-Fi Network: (.+)", sh("networksetup", "-getairportnetwork", "en0"))
    if m:
        ssid = m.group(1).strip()

    hotspot = ip.startswith(HOTSPOT_NET) or gw.startswith(HOTSPOT_NET)
    print(f"  {INFO} address     {ip or '(none)'}   gateway {gw or '(none)'}")
    if ssid:
        print(f"  {INFO} ssid        {ssid}")

    if hotspot:
        print(f"  {BAD} {BOLD}You are on an iPhone Personal Hotspot{RESET} "
              f"({HOTSPOT_NET}x is Apple's tethering range).")
        print(f"      This alone explains: random 'cannot connect' errors, "
              f"dropped\n      streaming replies mid-answer, and the robot being "
              f"unreachable.")
    elif ip:
        print(f"  {OK} on a normal network")

    if constrained:
        print(f"  {BAD} {BOLD}Link is marked 'constrained'{RESET} — macOS Low Data "
              f"Mode is ON.")
        print(f"      macOS actively defers background network traffic on a "
              f"constrained\n      link. Long-lived HTTPS streams (exactly what "
              f"Claude Code uses while\n      it thinks) get starved or cut. This "
              f"is the 'random' in your\n      random failures.")
        print(f"      {DIM}Fix: Settings ▸ Wi-Fi ▸ (i) next to the network ▸ "
              f"Low Data Mode off{RESET}")
    else:
        print(f"  {OK} link not constrained")

    return {"ip": ip, "gw": gw, "hotspot": hotspot, "constrained": constrained}


# --------------------------------------------------------------------------- #
# 2. The internet
# --------------------------------------------------------------------------- #
def check_internet() -> dict:
    head("2. Internet path")
    try:
        socket.getaddrinfo("api.anthropic.com", 443)
        print(f"  {OK} DNS resolves api.anthropic.com")
    except Exception as e:  # noqa: BLE001
        print(f"  {BAD} DNS FAILED: {e}")
        return {"dns": False}

    times, fails = [], 0
    for _ in range(5):
        t0 = time.time()
        try:
            req = urllib.request.Request("https://api.anthropic.com/v1/models")
            urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError:
            times.append(time.time() - t0)      # 401 is a healthy round-trip
        except Exception:                        # noqa: BLE001
            fails += 1
        else:
            times.append(time.time() - t0)

    if fails:
        print(f"  {BAD} {fails}/5 HTTPS handshakes failed")
    if times:
        avg, worst = statistics.mean(times), max(times)
        jitter = (statistics.stdev(times) if len(times) > 1 else 0.0)
        flag = OK if worst < 1.0 and jitter < 0.3 else WARN
        print(f"  {flag} handshake avg {avg*1000:.0f}ms  worst {worst*1000:.0f}ms  "
              f"jitter {jitter*1000:.0f}ms")
        if jitter > 0.3 or worst > 1.5:
            print(f"      High jitter means streaming replies can stall or drop "
                  f"mid-answer.")
    return {"dns": True, "fails": fails}


# --------------------------------------------------------------------------- #
# 3. The robot
# --------------------------------------------------------------------------- #
def probe(host: str, port: int = 8000, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def scan_for_robot(my_ip: str, port: int = 8000) -> list[str]:
    """Sweep the current /24 for anything answering the Reachy daemon port."""
    if not my_ip or my_ip.count(".") != 3:
        return []
    base = my_ip.rsplit(".", 1)[0]
    hosts = [f"{base}.{i}" for i in range(1, 255)]
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
        for host, up in zip(hosts, pool.map(lambda h: probe(h, port), hosts)):
            if up:
                found.append(host)
    return found


IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def check_isolation(my_ip: str) -> bool:
    """True if the network looks like it has AP/client isolation on.

    Guest and cafe networks routinely block device-to-device traffic while
    leaving the internet perfectly fast. That combination is vicious to debug:
    every speed test passes and the robot is still invisible. Broadcast-ping to
    populate ARP, then count neighbours that actually answered — on a normal
    LAN you see several; behind isolation you see only the router."""
    if not my_ip or not IPV4_RE.match(my_ip):
        return False
    base = my_ip.rsplit(".", 1)[0]
    sh("ping", "-c", "2", "-t", "2", f"{base}.255", timeout=6)
    peers = set()
    for line in sh("arp", "-a", "-n").splitlines():
        m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-f:]+)", line)
        if not m:
            continue
        addr, mac = m.group(1), m.group(2)
        # Skip ourselves, the broadcast entry (always ff:ff:…), and anything
        # off-subnet. Counting the broadcast row as a neighbour is exactly the
        # kind of off-by-one that makes a checker cheerfully report "healthy".
        if (not addr.startswith(base + ".") or addr == my_ip
                or addr.endswith(".255") or mac.startswith("ff:ff")):
            continue
        peers.add(addr)
    gw_only = peers <= {f"{base}.1"}
    if gw_only:
        print(f"  {BAD} {BOLD}Client isolation appears to be ON{RESET} — only the "
              f"router answered.")
        print(f"      Devices on this Wi-Fi cannot talk to each other, so the "
              f"robot will\n      be unreachable here even when it is on this "
              f"exact network and the\n      internet is perfectly fast. Common "
              f"on guest/cafe/hotel SSIDs.")
        print(f"      {DIM}Fix: use a non-guest SSID, or the robot's own "
              f"reachy-mini-ap{RESET}")
    else:
        print(f"  {OK} {len(peers)} other device(s) visible — no client isolation")
    return gw_only


def check_robot(link: dict, do_scan: bool) -> dict:
    head("3. Robot")
    env = read_env()
    url = env.get("REACHY_URL", "")
    host = re.sub(r"^https?://", "", url).split(":")[0]
    print(f"  {INFO} .env REACHY_URL = {url or '(unset)'}")

    reachable = probe(host) if host else False
    if reachable:
        print(f"  {OK} robot answering at {host}:8000")
        return {"reachable": True, "url": url}

    print(f"  {BAD} no answer from {host}:8000")

    my_ip = link.get("ip", "")
    is_hostname = bool(host) and not IPV4_RE.match(host)

    if is_hostname:
        # A .local name is mDNS — it failing is its own distinct diagnosis, and
        # comparing it to a subnet (the old bug here) produced nonsense like
        # "the robot's subnet is reachy-mini.x".
        resolved = ""
        for line in sh("dscacheutil", "-q", "host", "-a", "name", host).splitlines():
            m = re.search(r"ip_address:\s*(\S+)", line)
            if m:
                resolved = m.group(1)
                break
        if resolved:
            print(f"  {INFO} {host} resolves to {resolved}, but nothing is "
                  f"listening there")
        else:
            print(f"  {BAD} {host} does not resolve — mDNS/Bonjour is not finding "
                  f"the robot.")
            print(f"      Either the robot isn't on this network, or this network "
                  f"blocks\n      multicast (many guest networks do, which also "
                  f"breaks AirPlay).")
    elif my_ip and host:
        my_net = my_ip.rsplit(".", 1)[0]
        robot_net = host.rsplit(".", 1)[0]
        if my_net != robot_net:
            print(f"  {BAD} {BOLD}Different subnets.{RESET} You are on {my_net}.x, "
                  f"the robot's last known\n      address was {robot_net}.x — "
                  f"they cannot see each other at all.")
            if link.get("hotspot"):
                print(f"      You're tethered to your phone; the robot is on "
                      f"home Wi-Fi.\n      Rejoin the same Wi-Fi network as the "
                      f"robot and this resolves itself.")

    isolated = check_isolation(my_ip)

    found = []
    if do_scan and my_ip:
        print(f"  {INFO} scanning {my_ip.rsplit('.', 1)[0]}.0/24 for port 8000 …")
        found = scan_for_robot(my_ip)
        if found:
            print(f"  {OK} found a Reachy daemon at: {', '.join(found)}")
        elif isolated:
            # Don't let the scan lie. Behind isolation a negative result says
            # nothing about where the robot is — the packets never left.
            print(f"  {WARN} scan found nothing, but this network blocks "
                  f"device-to-device\n      traffic, so a clean scan proves "
                  f"NOTHING about where the robot is.")
        else:
            print(f"  {BAD} nothing on this subnet is serving port 8000 "
                  f"— the robot is not on this network")
    elif not do_scan:
        print(f"      {DIM}re-run with --find to scan this subnet for it{RESET}")

    return {"reachable": False, "url": url, "found": found, "isolated": isolated}


def fix_ip(new_host: str) -> None:
    """Rewrite REACHY_URL in .env, preserving everything else verbatim."""
    text = ENV_PATH.read_text()
    new = f"REACHY_URL=http://{new_host}:8000"
    if re.search(r"(?m)^REACHY_URL=.*$", text):
        text = re.sub(r"(?m)^REACHY_URL=.*$", new, text)
    else:
        text += f"\n{new}\n"
    ENV_PATH.write_text(text)
    print(f"  {OK} .env updated → {new}")


# --------------------------------------------------------------------------- #
# 4. Services
# --------------------------------------------------------------------------- #
def check_services() -> None:
    head("4. Local services")
    up = []
    for port, name in SERVICES.items():
        alive = probe("127.0.0.1", port, timeout=0.25)
        up.append(alive)
        print(f"  {OK if alive else BAD} :{port}  {name}")
    if not any(up):
        print(f"      {DIM}none running — start them with ./start_wonder.sh "
              f"(from a real shell, not the app runner, or the mic reads "
              f"zeros){RESET}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose the Vibey dev environment")
    ap.add_argument("--find", action="store_true", help="scan the LAN for the robot")
    ap.add_argument("--fix-ip", action="store_true",
                    help="scan, and write the robot's address into .env")
    args = ap.parse_args()

    print(f"{BOLD}Vibey doctor{RESET} {DIM}— {time.strftime('%Y-%m-%d %H:%M')}{RESET}")
    link = check_link()
    check_internet()
    robot = check_robot(link, do_scan=args.find or args.fix_ip)
    check_services()

    if args.fix_ip and robot.get("found"):
        head("Applying fix")
        fix_ip(robot["found"][0])

    head("Verdict")
    if link["hotspot"] or link["constrained"]:
        print(f"  {BAD} Your network is the problem, not your code.")
        if link["constrained"]:
            print(f"     → turn off Low Data Mode")
        if link["hotspot"]:
            print(f"     → rejoin your home Wi-Fi (the robot is there, your "
                  f"phone's hotspot is not)")
    elif robot.get("isolated"):
        print(f"  {BAD} Your internet is fine, but this Wi-Fi blocks "
              f"device-to-device traffic.")
        print(f"     → the robot cannot be reached from here no matter what it "
              f"is doing")
        print(f"     → switch to a non-guest SSID, or join the robot's own "
              f"reachy-mini-ap")
    elif not robot["reachable"]:
        print(f"  {WARN} Network is healthy; the robot just isn't findable. "
              f"Power-cycle it,\n     or run --fix-ip once it's back on Wi-Fi.")
    else:
        print(f"  {OK} Everything checks out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
