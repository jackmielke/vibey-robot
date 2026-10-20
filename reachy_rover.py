"""Vibey's wheels: a Waveshare WAVE ROVER driven over Wi-Fi.

The rover's ESP32 takes JSON commands at http://<rover>/js?json=<command>
(docs: https://www.waveshare.com/wiki/WAVE_ROVER). Wheel speed is
{"T":1,"L":x,"R":y} with x and y in -0.5..0.5, where 0.5 is full power. The
rover stops by itself if it hears nothing for 3 seconds, so a move here is
"keep re-sending for N seconds, then send zeros".

Set ROVER_URL in .env (e.g. http://192.168.1.50) once the rover is on Wi-Fi.
Until then every call says the rover isn't connected and nothing else breaks.

ROVER_RIDING=1 in .env means Vibey is mounted on the rover: speeds stay very
gentle and Vibey talks about the rover as its own wheels. Unset (the default),
Vibey sits on the table and drives the rover for you as a separate little car.

First-time setup (the rover ships as its own "UGV" hotspot, which is how a
phone drives it; Vibey needs it on the same Wi-Fi as the Mac instead):
    1. Join the Mac to the "UGV" network (password 12345678)
    2. python3 reachy_rover.py wifi "YourHomeWifi"
    3. Rejoin the home Wi-Fi, then: python3 reachy_rover.py find --save

CLI, for testing from the Mac:
    python3 reachy_rover.py find            # sweep this network for the rover
    python3 reachy_rover.py find --save     # ...and write ROVER_URL into .env
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

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _env(key: str, default: str = "") -> str:
    """os.environ first (start_wonder.sh exports .env), else read .env directly
    so the CLI works from a bare shell."""
    if key in os.environ:
        return os.environ[key]
    try:
        with open(ENV_PATH) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return default


ROVER_URL = _env("ROVER_URL").rstrip("/")
RIDING = _env("ROVER_RIDING", "0") == "1"
AP_URL = "http://192.168.4.1"   # the rover's own hotspot "UGV" (password 12345678)

# Fractions of full power. With Vibey on top they stay very gentle: it is tall
# and top-heavy. On its own the rover can go a bit quicker.
SPEEDS = ({"slow": 0.12, "medium": 0.2, "fast": 0.3} if RIDING
          else {"slow": 0.15, "medium": 0.25, "fast": 0.4})
MAX_SECONDS = 4.0
RESEND_EVERY = 0.4
# Soft start and stop. Jumping straight to full speed (or to zero) is what
# tips a top-heavy load; ramping over half a second keeps braking around
# 1 m/s^2 instead of several (tipping starts around 4 with Vibey on top).
RAMP_SECONDS = 0.5 if RIDING else 0.2
RAMP_TICK = 0.1

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
    # Let a running move ease down to zero first (it holds the lock while it
    # ramps), then send zeros anyway in case nothing was moving.
    if _lock.acquire(timeout=RAMP_SECONDS + 1.0):
        _lock.release()
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
    cmd = {"T": 1, "L": lm * p, "R": rm * p}
    seconds = max(0.2, min(float(seconds or 1.0), MAX_SECONDS))
    _cancel.set()                      # interrupt any move in flight
    def at(level: float) -> dict:
        return {"T": 1, "L": round(cmd["L"] * level, 3), "R": round(cmd["R"] * level, 3)}

    with _lock:
        _cancel.clear()
        start = time.time()
        end = start + seconds
        level = 0.0
        try:
            while time.time() < end and not _cancel.is_set():
                now = time.time()
                # Trapezoid: ramp up, cruise, ramp down so the move ends gently.
                level = max(0.0, min(1.0, (now - start + RAMP_TICK) / RAMP_SECONDS,
                                     (end - now) / RAMP_SECONDS))
                _send(at(level))
                tick = RAMP_TICK
                if level >= 1.0:   # cruising: resend slowly, but wake for the ramp down
                    tick = max(RAMP_TICK, min(RESEND_EVERY, end - RAMP_SECONDS - now))
                _cancel.wait(min(tick, max(0.0, end - time.time())))
        except Exception as e:  # noqa: BLE001
            return f"my wheels didn't answer: {e}"
        finally:
            try:
                # Cut short by stop or a new move: ease down from where we are.
                while level > 0.05:
                    level = max(0.0, level - RAMP_TICK / RAMP_SECONDS)
                    _send(at(level))
                    time.sleep(RAMP_TICK)
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


def save_url(url: str) -> None:
    """Write ROVER_URL into .env, replacing an existing line."""
    try:
        with open(ENV_PATH) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    lines = [ln for ln in lines if not ln.startswith("ROVER_URL=")]
    lines.append(f"ROVER_URL={url}")
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


_RIDING_DESC = (
    "Drive your wheels (you ride on a small rover). Use it when someone asks "
    "you to come here, go over there, back up, turn around, or do a little "
    "spin. Moves are short and gentle; chain a few if you need to. You can't "
    "see obstacles while driving, so keep moves short and say what you're doing.")
_REMOTE_DESC = (
    "Drive the little rover car (a Waveshare Wave Rover) that sits near you. "
    "You stay put on the table; the rover is a separate toy you remote-control. "
    "Use it when someone asks you to drive the car, send it somewhere, bring it "
    "back, turn it, or spin it. Moves are short and timed; chain a few for a "
    "longer trip, and use stop the moment anyone says stop. You can use look "
    "between moves to see where the rover ended up if it's in front of you.")

TOOL = {
    "type": "function",
    "name": "drive",
    "description": _RIDING_DESC if RIDING else _REMOTE_DESC,
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
        rest = [a for a in argv[1:] if a != "--save"]
        hits = find(rest[0] if rest else None)
        print("\n".join(hits) if hits else "no rover found on this network")
        if "--save" in argv:
            if len(hits) == 1:
                save_url(hits[0])
                print(f"saved ROVER_URL={hits[0]} to .env; restart Vibey to pick it up")
            elif hits:
                print("found more than one; set ROVER_URL in .env by hand")
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
