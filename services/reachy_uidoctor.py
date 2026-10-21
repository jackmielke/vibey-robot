"""Answers "can you see my dashboard?" honestly — by saying what the machine
reports, never by pretending to look at a screen.

Vibey has no window onto anybody's browser. What Vibey does have is the same
localhost the browser is pointed at, so the useful answer is never "yes I see
it" — it is "port 8770 is listening, so if your tab is blank the problem is
between the tab and the port, and here is the next thing to check."

The three ways a local UI goes invisible, in the order they actually happen:
  1. nothing is listening — the service isn't running
  2. something is listening but you're knocking on the wrong door — localhost
     from another device, or from inside an SSH session, is not this machine
  3. the network moved underneath you — hotspot, new SSID, robot on a new IP

`report()` walks exactly that order and stops at the first thing that explains
the symptom. When it cannot tell which door you're at, it hands back ONE
question to ask rather than guessing, because guessing here costs ten minutes.

Stdlib only, read-only, no secrets: ports, up/down, and the LAN address needed
to build a URL. Never an env value, never a key, never a WiFi password.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import urllib.parse

# port -> (name, what a person would call it out loud)
SERVICES = {
    8770: ("dashboard", "the page with the toggles and the meter"),
    8771: ("camera", "my video feed"),
    8772: ("chat", "the voice service, no page of its own"),
    8773: ("face memory", "who I recognise, no page of its own"),
    8774: ("vibeverse", "my avatar bridge"),
    8775: ("robot mic", "my ears, raw audio, no page"),
}
# The ones a browser is ever actually pointed at.
BROWSABLE = (8770, 8771, 8774)
HOTSPOT_NET = "172.20.10."     # Apple hands this out to Personal Hotspot clients


def _sh(*cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=4).stdout
    except Exception:  # noqa: BLE001
        return ""


def _lan_ip() -> str:
    ip = _sh("ipconfig", "getifaddr", "en0").strip()
    if ip:
        return ip
    try:  # no en0 (wired, or not macOS) — ask the routing table instead
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 80))
        ip, _ = s.getsockname()
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return ""


def _listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except Exception:  # noqa: BLE001
        return False


def _subnet(ip: str) -> str:
    return ".".join(ip.split(".")[:3]) if ip.count(".") == 3 else ""


def _robot_host() -> str:
    """Host only, never the whole URL — nothing after the port is ours to say."""
    raw = os.environ.get("REACHY_URL", "")
    try:
        return urllib.parse.urlsplit(raw).hostname or ""
    except Exception:  # noqa: BLE001
        return ""


def _match(what: str) -> int | None:
    """Which service somebody meant. Digits win; then names; then nothing."""
    what = (what or "").strip().lower()
    if not what:
        return None
    m = re.search(r"\b(\d{4,5})\b", what)
    if m and int(m.group(1)) in SERVICES:
        return int(m.group(1))
    for port, (name, _) in SERVICES.items():
        if name in what or what in name:
            return port
    if "dash" in what or "page" in what or "ui" in what:
        return 8770
    if "cam" in what or "video" in what or "see" in what:
        return 8771
    return None


def report(what: str | None = None, viewing_from: str | None = None) -> str:
    """A short, factual account of what this machine is serving, plus the one
    next step that fits. Written to be read aloud, not pasted."""
    ip = _lan_ip()
    constrained = "constrained" in _sh("ifconfig", "en0")
    hotspot = ip.startswith(HOTSPOT_NET)
    where = (viewing_from or "").strip().lower()

    up = {p: _listening(p) for p in SERVICES}
    port = _match(what or "")
    lines = ["I can't see your screen — this is what my own machine reports."]

    live = [f"{SERVICES[p][0]} {p}" for p in SERVICES if up[p]]
    dead = [f"{SERVICES[p][0]} {p}" for p in SERVICES if not up[p]]
    lines.append(f"Listening: {', '.join(live) or 'nothing'}."
                 + (f" Not answering: {', '.join(dead)}." if dead else ""))

    # Step 1 — is anything there at all? Everything downstream is moot if not.
    if port is not None and not up[port]:
        name = SERVICES[port][0]
        return "\n".join(lines + [
            f"So the {name} is NOT running — a blank tab is the honest result, "
            f"not a browser problem. Next: start the stack with start_wonder.sh "
            f"from the repo, then reload.",
        ])

    # Step 2 — right door? localhost means a different machine to every machine.
    target = port if port in BROWSABLE else 8770
    tname = SERVICES[target][0]
    lan = f"http://{ip}:{target}" if ip else f"http://<this-laptop>:{target}"
    if where in ("ssh", "over_ssh", "terminal"):
        lines.append(
            f"Over SSH, localhost is whatever you're logged INTO, not the "
            f"machine with the browser. Forward it: ssh -L {target}:localhost:"
            f"{target} to this machine, then open http://localhost:{target} in "
            f"your own browser.")
    elif where in ("another_device", "phone", "ipad", "other"):
        lines.append(
            f"From another device, localhost points at that device. Use {lan} "
            f"instead, on the same WiFi. If it still hangs, the network is "
            f"probably a guest or cafe one with client isolation — those block "
            f"device-to-device traffic while the internet stays fast.")
    else:
        lines.append(f"On this laptop, open http://localhost:{target} for the "
                     f"{tname}. From your phone on the same WiFi, {lan}.")

    # Step 3 — did the ground move? Both symptoms are silent and both are common.
    if hotspot:
        lines.append("Heads up: this laptop is on an iPhone hotspot, so its "
                     "address changed and nothing else on the house WiFi can "
                     "reach it — localhost still works, the LAN URL won't.")
    if constrained:
        lines.append("Low Data Mode is on for this link, which starves "
                     "long-lived connections — worth turning off before "
                     "blaming anything else.")
    rhost = _robot_host()
    if rhost and ip and _subnet(rhost) and _subnet(rhost) != _subnet(ip):
        lines.append(f"Also, my saved robot address is on a different subnet "
                     f"than this laptop, so the network changed since it was "
                     f"set — vibey_doctor.py --find rescans for it.")

    # Clarifying mode: one question, only when the answer genuinely branches.
    if port is None and not where:
        lines.append("ASK EXACTLY ONE QUESTION, then stop and wait: are you "
                     "looking on this laptop, or on another device?")
    return "\n".join(lines)
