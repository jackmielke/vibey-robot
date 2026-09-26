#!/usr/bin/env python3
"""
reachy_modes.py — three stacked runtime modes, and an honest bill of what
each one actually buys you.

    pi      the robot alone. Nothing on the laptop, nothing on the internet.
    mac     pi + this laptop. Reacts to you, moves, remembers faces, texts.
    cloud   pi + laptop + the APIs. Conversation, and memory of it.

The point is not the switching — most of these knobs already existed. The
point is being able to SEE the difference: which capabilities light up at
each tier, what fraction of the whole robot that is, and, for anything
currently stuck on the laptop, whether it could move down onto the Pi.

That last column is the interesting one. "Runs locally" is easy to believe
and hard to check, and right now the honest answer is that the Pi is close to
a peripheral: it holds the motors, the camera and the speaker, and every
decision is made somewhere else.

Switching a mode never kills a process. It disarms capabilities through the
control APIs the services already expose, so every transition is reversible
from the dashboard itself — stopping reachy_viewer.py to enter "pi mode"
would take the button with it.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.request

REACHY_URL = os.environ.get("REACHY_URL", "http://reachy-mini.local:8000").rstrip("/")
CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8772").rstrip("/")
GESTURE_URL = os.environ.get("GESTURE_URL", "http://localhost:8776").rstrip("/")

TIERS = ("pi", "mac", "cloud")
TIER_LABEL = {"pi": "On the Pi", "mac": "On the Mac", "cloud": "In the cloud"}

# Which tiers each mode has available. Stacked, not exclusive: you cannot have
# the Mac without the robot, or the cloud without the Mac holding the socket.
MODES = {
    "pi":    {"label": "Pi only",
              "sub": "Laptop closed, wifi to nowhere. Just the robot.",
              "tiers": ("pi",)},
    "mac":   {"label": "Pi + Mac",
              "sub": "Sees you, reacts, remembers. No internet, no talking.",
              "tiers": ("pi", "mac")},
    "cloud": {"label": "Pi + Mac + Cloud",
              "sub": "All of it, including the conversation.",
              "tiers": ("pi", "mac", "cloud")},
}

# --------------------------------------------------------------------------- #
# The bill of capabilities.
#
# `portable` answers one question and only one: could this move onto the Pi and
# stop needing the laptop?
#   native   already runs there
#   yes      plain Python + HTTP to localhost; nothing stops it but packaging
#   partial  it can move, but something has to shrink or get slower first
#   no       it is a call to somebody else's server
#   n/a      it only exists to bridge Pi→Mac; on the Pi there is nothing to bridge
# --------------------------------------------------------------------------- #
CAPS = [
    # ---- pi ---------------------------------------------------------------
    dict(id="face_track", tier="pi", label="Follows your face with its head",
         probe=("robot", "/api/media/tracking/face"), portable="native",
         note="The daemon's own tracker, on the robot's camera. This is the "
              "one thing that already needs nothing else."),
    dict(id="body", tier="pi", label="Moves — head, antennas, wake, sleep",
         probe=("robot", "/api/motors/status"), portable="native",
         note="Motors have to be enabled; they come up disabled at boot."),
    dict(id="speaker", tier="pi", label="Plays sounds through its own speaker",
         probe=("robot", "/api/daemon/status"), portable="native",
         note="Sounds are uploaded and played by file name."),
    dict(id="rest", tier="pi", label="Answers HTTP on the network",
         probe=("robot", "/api/daemon/status"), portable="native",
         note="93 endpoints, no desktop app needed."),

    # ---- mac --------------------------------------------------------------
    dict(id="camera", tier="mac", label="Camera frames anything can read",
         probe=("port", 8771), portable="n/a",
         note="Pulls the WebRTC stream and re-serves it as MJPEG. On the Pi "
              "the camera is already local — this bridge stops existing."),
    dict(id="gestures", tier="mac", label="Reacts to hand gestures",
         probe=("gestures", None), portable="yes",
         note="MediaPipe hand landmarks at 8fps. A Pi 5 handles this — it is "
              "the single best candidate to move down, and it is what makes "
              "waving work with the laptop shut."),
    dict(id="emotes", tier="mac", label="30 choreographies + synthesized sound",
         probe=("port", 8770), portable="yes",
         note="Pure maths and HTTP calls. Would get faster on the Pi: no "
              "network hop between deciding to move and moving."),
    dict(id="idle", tier="mac", label="Breathes and drifts when idle",
         probe=("port", 8772), portable="yes", note="A timer and a pose."),
    dict(id="wake_word", tier="mac", label="Listens for 'hey vibey'",
         probe=("chat", "listening"), portable="partial",
         note="Needs a small keyword model on-device instead of the Mac's mic."),
    dict(id="robot_mic", tier="mac", label="Robot's mic reaches the laptop",
         probe=("port", 8775), portable="n/a",
         note="Another bridge that disappears if the brain runs on the Pi."),
    dict(id="alarms", tier="mac", label="Scheduled wake-up shows",
         probe=("port", 8770), portable="yes", note="A cron and a playlist."),
    dict(id="telegram", tier="mac", label="Texts you, sends photos",
         probe=("proc", "reachy_telegram.py"), portable="yes",
         note="One long-poll to Telegram. Cheap enough to run on the Pi all day."),
    dict(id="dashboard", tier="mac", label="This dashboard",
         probe=("port", 8770), portable="yes", note="Zero-dependency HTTP server."),

    # ---- cloud ------------------------------------------------------------
    dict(id="brain", tier="cloud", label="Talks with you — OpenAI Realtime",
         probe=("chat", "openai_live"), portable="no",
         note="Somebody else's server, and the reason an idle session costs "
              "money. Nothing on a Pi 5 replaces this at conversational speed."),
    dict(id="voice", tier="cloud", label="Fallback voice — ElevenLabs",
         probe=("env", "ELEVENLABS_API_KEY"), portable="no", note="An API call."),
    dict(id="faces", tier="cloud", label="Remembers faces by name",
         probe=("port", 8773), portable="partial",
         note="Recognition could run on the Pi; the Supabase row it writes "
              "afterwards is the part that stays remote."),
    dict(id="memory", tier="cloud", label="Remembers what was said",
         probe=("env", "SUPERMEMORY_API_KEY"), portable="partial",
         note="Embedding and search are hosted. A local vector store is "
              "possible and much worse at recall."),
    dict(id="vibeverse", tier="cloud", label="Its avatar on Edge Island",
         probe=("port", 8774), portable="no", note="A hosted world."),
]

TOTAL = len(CAPS)


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #
def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """A connect test rather than an HTTP request: some of these services block
    a moment on a real GET, and this runs for every capability on every poll."""
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def _json(url: str, timeout: float = 2.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except Exception:
        return None


def _chat_state() -> dict | None:
    return _json(f"{CHAT_URL}/state")


def _live(cap: dict, ctx: dict) -> bool:
    kind, arg = cap["probe"]
    if kind == "port":
        return _port_open(arg)
    if kind == "proc":
        return arg in ctx["procs"]
    if kind == "env":
        return bool(os.environ.get(arg))
    if kind == "robot":
        if not ctx["robot"]:
            return False
        if arg == "/api/motors/status":
            return "enabled" in json.dumps(ctx["robot"].get("motors") or "")
        if arg == "/api/media/tracking/face":
            return bool(ctx["tracking"])
        return True
    if kind == "gestures":
        g = ctx["gestures"]
        return bool(g and g.get("on"))
    if kind == "chat":
        c = ctx["chat"]
        if not c:
            return False
        if arg == "openai_live":
            return bool(c.get("openai")) and not c.get("asleep")
        if arg == "listening":
            return not c.get("asleep")
        return True
    return False


def _context() -> dict:
    import subprocess
    try:
        procs = subprocess.run(["pgrep", "-fl", "reachy_"], capture_output=True,
                               text=True, timeout=3).stdout
    except Exception:
        procs = ""
    robot = _json(f"{REACHY_URL}/api/daemon/status", 2.0)
    motors = _json(f"{REACHY_URL}/api/motors/status", 2.0) if robot else None
    chat = _chat_state()
    return {
        "procs": procs,
        "robot": dict(robot or {}, motors=motors) if robot else None,
        "tracking": (chat or {}).get("switches", {}).get("tracking", bool(robot)),
        "chat": chat,
        "gestures": _json(f"{GESTURE_URL}/state", 1.5),
    }


def _detect(caps: list[dict]) -> str:
    """Read the mode back off the world rather than off a stored setting.

    A file saying "cloud" while the brain is asleep is worse than no file:
    the whole panel exists to show what is actually true.
    """
    live = {c["id"] for c in caps if c["live"]}
    if "brain" in live:
        return "cloud"
    if live & {"gestures", "camera", "telegram"}:
        return "mac"
    return "pi"


def status() -> dict:
    ctx = _context()
    caps = []
    for c in CAPS:
        caps.append({k: v for k, v in c.items() if k != "probe"}
                    | {"live": _live(c, ctx)})
    modes = {}
    for name, m in MODES.items():
        avail = [c for c in caps if c["tier"] in m["tiers"]]
        modes[name] = dict(m, tiers=list(m["tiers"]), count=len(avail),
                           pct=round(100 * len(avail) / TOTAL))
    return {
        "current": _detect(caps),
        "reachable": ctx["robot"] is not None,
        "caps": caps,
        "modes": modes,
        "total": TOTAL,
        # of everything this mode COULD do, how much is actually up right now
        "live": sum(1 for c in caps if c["live"]),
    }


# --------------------------------------------------------------------------- #
# Switching
# --------------------------------------------------------------------------- #
def _post(url: str, body: dict | None = None, timeout: float = 10.0):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        print(f"[modes] {url} failed: {e}", flush=True)
        return None


def apply(mode: str) -> dict:
    """Move to a mode. Ordering matters in one place: going UP, the brain is
    woken last, because waking it while the gestures are still disarmed gives
    you a robot that will talk to you but not wave back."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    want_mac = "mac" in MODES[mode]["tiers"]
    want_cloud = "cloud" in MODES[mode]["tiers"]

    # Coming down out of the cloud, put the conversation away first — leaving a
    # realtime socket open is what quietly bills for a night of nothing.
    if not want_cloud:
        _post(f"{CHAT_URL}/openaimode", {"openai": False})
        _post(f"{CHAT_URL}/sleep", timeout=25)

    _post(f"{GESTURE_URL}/toggle", {"on": want_mac})

    # Face tracking is the robot's own and stays on in every mode: it is the
    # only thing that still works when everything else is off, which is exactly
    # what "pi mode" is meant to demonstrate.
    _post(f"{REACHY_URL}/api/motors/set_mode/enabled")
    _post(f"{REACHY_URL}/api/media/tracking/enable", {"weight": 0.6})

    if want_cloud:
        _post(f"{CHAT_URL}/openaimode", {"openai": True})
        _post(f"{CHAT_URL}/wake", timeout=25)

    return status()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(json.dumps(apply(sys.argv[1]), indent=2))
    else:
        st = status()
        print(f"current: {st['current']}  ({st['live']}/{st['total']} live)")
        for t in TIERS:
            print(f"\n{TIER_LABEL[t]}")
            for c in st["caps"]:
                if c["tier"] == t:
                    print(f"  {'●' if c['live'] else '○'} {c['label']:<44}"
                          f" portable: {c['portable']}")
