#!/usr/bin/env python3
"""
reachy_watchdog.py — keep the Vibey stack alive without a human noticing.

Born from the 2026-07-18 incident: chat crashed at 2:44PM and the robot spent
the evening headless (daemon scanning the room = "crazy mode") because nothing
restarts dead services. This does.

Every CHECK_S seconds each service is health-checked (HTTP port, or process
presence for the portless ones). Two consecutive misses → restart it with the
exact interpreter start_wonder.sh uses, and DM Jack on Telegram. A service
that needs more than MAX_RESTARTS restarts in an hour is left down (crash
loop — a human should look) with one final Telegram note.

Run with system python (stdlib only):   python3 reachy_watchdog.py
Started automatically by start_wonder.sh; stop with the rest of the stack.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK_S = 30
MISSES_TO_RESTART = 2          # ~60s dead before we act (rides out slow starts)
MAX_RESTARTS = 4               # per service per rolling hour, then give up
STARTUP_GRACE_S = 45           # leave freshly (re)started services alone


def _load_env() -> None:
    try:
        with open(os.path.join(HERE, ".env")) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass
    url = os.environ.get("REACHY_URL", "")
    m = re.match(r"https?://([^:/]+)", url)
    if m:
        os.environ.setdefault("REACHY_HOST", m.group(1))
    # stale HF token 401s public whisper downloads (see start_wonder.sh)
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"


_load_env()

# name → (health url or None, script, interpreter relative to repo, logfile)
SERVICES = {
    "camera":    ("http://localhost:8771/status",  "reachy_camera.py",    "reachy_env/bin/python3", "/tmp/reachy_camera.log"),
    "viewer":    ("http://localhost:8770/perception", "reachy_viewer.py", "python3",                "/tmp/reachy_viewer.log"),
    "chat":      ("http://localhost:8772/state",   "reachy_chat.py",      ".venv/bin/python3",      "/tmp/reachy_chat.log"),
    "robot_mic": ("http://localhost:8775/status",  "reachy_robot_mic.py", "reachy_env/bin/python3", "/tmp/reachy_robot_mic.log"),
    "memory":    ("http://localhost:8773/current", "reachy_memory.py",    "reachy_env/bin/python3", "/tmp/reachy_memory.log"),
    "vibeverse": ("http://localhost:8774/status",  "reachy_vibeverse.py", "python3",                "/tmp/vibeverse.log"),
    # Re-enabled 2026-08-26. It no longer reads TELEGRAM_BOT_TOKEN at all: it
    # wants TELEGRAM_VIBEY_TOKEN, refuses to fall back to the shared one, and
    # exits on the spot if getMe comes back as @jack_mielke_bot. Without its own
    # token it prints why and returns, so the watchdog finds it dead and gives
    # up on it after MAX_RESTARTS rather than resurrecting a 409 forever.
    "telegram":  (None,                            "reachy_telegram.py",  "python3",                "/tmp/telegram.log"),
    "alarm":     (None,                            "reachy_alarm.py",     "python3",                "/tmp/reachy_alarm.log"),
    "dj":        ("http://localhost:8778/status",  "reachy_dj.py",        "reachy_env/bin/python3", "/tmp/reachy_dj.log"),
}

# --- Robot-native mode (VIBEY_ON_ROBOT=1, see ROBOT_NATIVE.md) --------------
# On the robot, systemd user units own the processes (Restart=always), so this
# checks health the same way but restarts through systemctl, and it also guards
# the one thing that matters most there: the motor control loop's rate.
ON_ROBOT = os.environ.get("VIBEY_ON_ROBOT", "").strip() == "1"
LOOP_MIN_HZ = float(os.environ.get("VIBEY_LOOP_MIN_HZ", "40"))
LOOP_BAD_CHECKS = 3            # consecutive low readings before it is news
if ON_ROBOT:
    # name → (health url or None, systemd unit). Only services deployed there;
    # a unit that is not installed simply reads as absent and is skipped.
    ROBOT_UNITS = {
        "robot_mic": ("http://localhost:8775/status",  "vibey-mic.service"),
        "chat":      ("http://localhost:8772/state",   "vibey-chat.service"),
        "telegram":  (None,                            "vibey-telegram.service"),
        "alarm":     (None,                            "vibey-alarm.service"),
        "viewer":    ("http://localhost:8770/perception", "vibey-viewer.service"),
        "camera":    ("http://localhost:8771/status",  "vibey-camera.service"),
        "memory":    ("http://localhost:8773/current", "vibey-memory.service"),
    }
    SERVICES = {n: (url, unit, None, f"journalctl --user -u {unit}")
                for n, (url, unit) in ROBOT_UNITS.items()}


def _on_robot_now() -> set:
    """Mac side of a partial move: services listed in .robot_services (written
    by robot/deploy.sh) now run on the robot, so the Mac must not resurrect its
    own copy (two Telegram pollers 409 each other; two brains fight over one
    speaker). Re-read every check, because deploy.sh moves a service while this
    process is running. No file = exactly the old behaviour."""
    if ON_ROBOT:
        return set()
    try:
        with open(os.path.join(HERE, ".robot_services")) as f:
            return {w for w in f.read().split() if not w.startswith("#")}
    except FileNotFoundError:
        return set()


def _unit_installed(unit: str) -> bool:
    r = subprocess.run(["systemctl", "--user", "is-enabled", unit],
                       capture_output=True, text=True)
    return r.stdout.strip() in ("enabled", "enabled-runtime", "static")


if ON_ROBOT:
    SERVICES = {n: v for n, v in SERVICES.items() if _unit_installed(v[1])}

_misses: dict[str, int] = {n: 0 for n in SERVICES}
_restarts: dict[str, list[float]] = {n: [] for n in SERVICES}
_gave_up: set[str] = set()
_started_at: dict[str, float] = {}


def _telegram(text: str) -> None:
    """Best-effort DM to the paired owner. Never raises."""
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        with open(os.path.join(HERE, ".telegram_state.json")) as f:
            chat_id = json.load(f).get("owner")
        if not (token and chat_id):
            return
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=10
        ).read()
    except Exception as e:  # noqa: BLE001
        print(f"[watchdog] telegram notify failed: {e}", flush=True)


def _alive(name: str) -> bool:
    url, script = SERVICES[name][0], SERVICES[name][1]
    if url:
        try:
            urllib.request.urlopen(url, timeout=5).read()
            return True
        except Exception:  # noqa: BLE001
            return False
    if ON_ROBOT:
        r = subprocess.run(["systemctl", "--user", "is-active", script],
                           capture_output=True, text=True)
        return r.stdout.strip() == "active"
    # portless services: a live process counts
    r = subprocess.run(["pgrep", "-f", script], capture_output=True)
    return r.returncode == 0


def _restart(name: str) -> None:
    if ON_ROBOT:
        unit = SERVICES[name][1]
        subprocess.run(["systemctl", "--user", "restart", unit], capture_output=True)
        _started_at[name] = time.time()
        print(f"[watchdog] restarted {unit}", flush=True)
        return
    _, script, interp, log = SERVICES[name]
    subprocess.run(["pkill", "-f", script], capture_output=True)
    time.sleep(1)
    interp_path = interp if interp == "python3" else os.path.join(HERE, interp)
    with open(log, "a") as lf:
        lf.write(f"\n--- [watchdog] restart {time.strftime('%F %T')} ---\n")
        lf.flush()
        subprocess.Popen([interp_path, os.path.join(HERE, script)],
                         cwd=HERE, stdout=lf, stderr=lf,
                         start_new_session=True)
    _started_at[name] = time.time()
    print(f"[watchdog] restarted {name}", flush=True)


# Services that only work while the robot itself is on. When Jack switches Vibey
# off these fail by design; that is not a crash and must not page him (23 Sep:
# two give-up alerts at 11:19pm for a robot he had turned off at 11:21 the night before).
NEEDS_ROBOT = {"camera", "viewer", "robot_mic"}


def _robot_up() -> bool:
    host = os.environ.get("REACHY_HOST", "reachy-mini.local")
    try:
        urllib.request.urlopen(f"http://{host}:8000/", timeout=4).read()
        return True
    except urllib.error.HTTPError:
        return True          # the daemon answered, just not with a 200
    except Exception:  # noqa: BLE001
        return False


_last_alert: dict = {}   # service -> date of last Telegram give-up alert
_loop_low = [0]


def _find(d, key):
    """First value for `key` anywhere in a nested dict (the daemon has moved
    control_loop_stats around between versions)."""
    if isinstance(d, dict):
        if key in d:
            return d[key]
        for v in d.values():
            hit = _find(v, key)
            if hit is not None:
                return hit
    return None


def _check_control_loop() -> None:
    """Robot mode only. The motor loop is nominally 50Hz; under ~30Hz the robot
    feels glitchy. Everything this repo runs on the CM4 is niced below the
    daemon, and this is the proof: log every low reading, alert once a day."""
    try:
        with urllib.request.urlopen("http://localhost:8000/api/daemon/status",
                                    timeout=4) as r:
            status = json.loads(r.read())
    except Exception:  # noqa: BLE001
        return
    hz = _find(status, "mean_control_loop_frequency")
    if not isinstance(hz, (int, float)):
        return
    if hz >= LOOP_MIN_HZ:
        _loop_low[0] = 0
        return
    _loop_low[0] += 1
    worst = _find(status, "max_control_loop_interval")
    print(f"[watchdog] control loop {hz:.1f}Hz < {LOOP_MIN_HZ:g}Hz "
          f"(max gap {worst}) [{_loop_low[0]}/{LOOP_BAD_CHECKS}]", flush=True)
    if _loop_low[0] == LOOP_BAD_CHECKS:
        today = time.strftime("%Y-%m-%d")
        if _last_alert.get("_loop") != today:
            _last_alert["_loop"] = today
            _telegram(f"⚠️ Vibey's motor loop is at {hz:.0f}Hz (want ≥{LOOP_MIN_HZ:g}). "
                      "Something on the robot is using too much CPU. "
                      "robot/load_check.sh shows what.")


def main() -> None:
    print(f"[watchdog] guarding {', '.join(SERVICES)} every {CHECK_S}s"
          f"{' (on the robot, via systemd)' if ON_ROBOT else ''}", flush=True)
    # everything just booted with the stack — give it all a grace window
    now = time.time()
    for n in SERVICES:
        _started_at[n] = now

    while True:
        time.sleep(CHECK_S)
        if ON_ROBOT:
            _check_control_loop()
        robot_up = _robot_up()
        moved = _on_robot_now()
        for name in SERVICES:
            if name in moved:
                _misses[name] = 0
                continue
            if name in NEEDS_ROBOT and not robot_up:
                # robot is off: forget any failure history so it starts clean
                # when he turns it back on, and say nothing.
                _misses[name] = 0
                _restarts[name] = []
                _gave_up.discard(name)
                continue
            if name in _gave_up:
                continue
            if time.time() - _started_at.get(name, 0) < STARTUP_GRACE_S:
                continue
            if _alive(name):
                _misses[name] = 0
                continue
            _misses[name] += 1
            print(f"[watchdog] {name} unhealthy ({_misses[name]}/{MISSES_TO_RESTART})",
                  flush=True)
            if _misses[name] < MISSES_TO_RESTART:
                continue
            _misses[name] = 0
            cutoff = time.time() - 3600
            _restarts[name] = [t for t in _restarts[name] if t > cutoff]
            if len(_restarts[name]) >= MAX_RESTARTS:
                _gave_up.add(name)
                msg = (f"🚨 Vibey watchdog: {name} crashed {MAX_RESTARTS}x in an "
                       f"hour — giving up on it. Check {SERVICES[name][3]}")
                print(f"[watchdog] {msg}", flush=True)
                today = time.strftime("%Y-%m-%d")
                if _last_alert.get(name) != today:
                    _last_alert[name] = today
                    _telegram(msg)
                continue
            _restarts[name].append(time.time())
            _restart(name)
            # Routine restarts are logged, not sent. Jack, 23 Sep 2026: "I just get
            # spammed every single day by Vibey Claw." A restart that worked is not
            # news; only the give-up below reaches Telegram, once per service per day.
            print(f"[watchdog] {name} restarted "
                  f"({len(_restarts[name])}/{MAX_RESTARTS} this hour)", flush=True)


if __name__ == "__main__":
    main()
