"""Vibey's wheels: a Waveshare WAVE ROVER driven over Wi-Fi.

The rover's ESP32 takes JSON commands at http://<rover>/js?json=<command>
(docs: https://www.waveshare.com/wiki/WAVE_ROVER). Wheel speed is
{"T":1,"L":x,"R":y} with x and y in -0.5..0.5, where 0.5 is full power. The
rover stops by itself if it hears nothing for 3 seconds, so a move here is
"keep re-sending for N seconds, then send zeros".

Set ROVER_URL in .env (e.g. http://192.168.1.50) once the rover is on Wi-Fi.
Until then every call says the rover isn't connected and nothing else breaks.

CLI, for testing from the Mac:
    python3 reachy_rover.py find            # sweep this network for the rover
    python3 reachy_rover.py status          # battery voltage and raw feedback
    python3 reachy_rover.py forward 1       # action, seconds
    python3 reachy_rover.py spin_left 0.5 slow
    python3 reachy_rover.py say "hi"        # text on the rover's little screen
    python3 reachy_rover.py wifi "SSID"     # join a network (asks for the password)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

ROVER_URL = os.environ.get("ROVER_URL", "").rstrip("/")
AP_URL = "http://192.168.4.1"   # the rover's own hotspot "UGV" (password 12345678)

# Fractions of full power. Kept gentle on purpose: Vibey is tall and heavy.
SPEEDS = {"slow": 0.12, "medium": 0.2, "fast": 0.3}
MAX_SECONDS = 4.0
RESEND_EVERY = 0.4

# (left, right) multipliers for each action.
ACTIONS = {
    "forward": (1, 1), "back": (-1, -1),
    "left": (0.35, 1), "right": (1, 0.35),           # gentle arcs
    "spin_left": (-1, 1), "spin_right": (1, -1),      # turn in place
    "stop": (0, 0),
}

_lock = threading.Lock()   # one move at a time; a new one cancels the old
_cancel = threading.Event()


def _send(cmd: dict, url: str | None = None, timeout: float = 2.0) -> str:
    base = url or ROVER_URL
    if not base:
        raise RuntimeError("rover not connected (ROVER_URL isn't set)")
    q = urllib.parse.quote(json.dumps(cmd, separators=(",", ":")))
    with urllib.request.urlopen(f"{base}/js?json={q}", timeout=timeout) as r:
        return r.read().decode(errors="replace")


def stop() -> str:
    _cancel.set()
    try:
        _send({"T": 1, "L": 0, "R": 0})
        return "stopped"
    except Exception as e:  # noqa: BLE001
        return f"couldn't reach the rover to stop it: {e}"


def drive(action: str, seconds: float = 1.0, speed: str = "medium") -> str:
    """Run one move and block until it's done. Returns a short result."""
    action = (action or "").strip().lower().replace(" ", "_")
    if action not in ACTIONS:
        return f"unknown move {action!r}; try {', '.join(ACTIONS)}"
    if action == "stop":
        return stop()
    if not ROVER_URL:
        return "my wheels aren't connected yet"
    p = SPEEDS.get(speed, SPEEDS["medium"])
    lm, rm = ACTIONS[action]
    cmd = {"T": 1, "L": round(lm * p, 3), "R": round(rm * p, 3)}
    seconds = max(0.2, min(float(seconds or 1.0), MAX_SECONDS))
    _cancel.set()                      # interrupt any move in flight
    with _lock:
        _cancel.clear()
        end = time.time() + seconds
        try:
            while time.time() < end and not _cancel.is_set():
                _send(cmd)
                _cancel.wait(min(RESEND_EVERY, max(0.0, end - time.time())))
        except Exception as e:  # noqa: BLE001
            return f"my wheels didn't answer: {e}"
        finally:
            try:
                _send({"T": 1, "L": 0, "R": 0})
            except Exception:  # noqa: BLE001
                pass
    return f"drove {action.replace('_', ' ')} for {seconds:.1f}s"


def status() -> dict:
    """Raw chassis feedback (includes battery voltage)."""
    raw = _send({"T": 130}, timeout=3)
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw}


def oled(text: str, line: int = 0) -> str:
    return _send({"T": 3, "lineNum": max(0, min(3, line)), "Text": text[:20]})


def join_wifi(ssid: str, password: str, via: str = AP_URL) -> str:
    """Tell the rover to join a network. Run while the Mac is on its "UGV" hotspot."""
    return _send({"T": 404, "ap_ssid": "UGV", "ap_password": "12345678",
                  "sta_ssid": ssid, "sta_password": password}, url=via, timeout=8)


def find(prefix: str | None = None) -> list[str]:
    """Look for the rover on this network by asking every address for feedback."""
    import concurrent.futures as cf
    import socket
    if not prefix:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            prefix = s.getsockname()[0].rsplit(".", 1)[0]
        finally:
            s.close()

    def probe(i: int) -> str | None:
        url = f"http://{prefix}.{i}"
        try:
            out = _send({"T": 130}, url=url, timeout=1.0)
            return url if "{" in out else None
        except Exception:  # noqa: BLE001
            return None

    with cf.ThreadPoolExecutor(64) as ex:
        return [u for u in ex.map(probe, range(1, 255)) if u]


TOOL = {
    "type": "function",
    "name": "drive",
    "description": (
        "Drive your wheels (you ride on a small rover). Use it when someone asks "
        "you to come here, go over there, back up, turn around, or do a little "
        "spin. Moves are short and gentle; chain a few if you need to. You can't "
        "see obstacles while driving, so keep moves short and say what you're doing."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "seconds": {"type": "number", "description": "0.2 to 4. About 1 second moves a hand's width at medium."},
            "speed": {"type": "string", "enum": list(SPEEDS)},
        },
        "required": ["action"],
    },
}


def _main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd = argv[0]
    if cmd == "find":
        hits = find(argv[1] if len(argv) > 1 else None)
        print("\n".join(hits) if hits else "no rover found on this network")
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "say":
        print(oled(" ".join(argv[1:])))
    elif cmd == "wifi":
        import getpass
        ssid = argv[1] if len(argv) > 1 else input("network name: ")
        print(join_wifi(ssid, getpass.getpass(f"password for {ssid}: ")))
        print("Now check the rover's screen: the ST line shows its new address.")
    else:
        secs = float(argv[1]) if len(argv) > 1 else 1.0
        spd = argv[2] if len(argv) > 2 else "medium"
        print(drive(cmd, secs, spd))


if __name__ == "__main__":
    _main(sys.argv[1:])
