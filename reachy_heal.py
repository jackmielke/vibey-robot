#!/usr/bin/env python3
"""
reachy_heal.py — notice when the robot falls off the network, and put the
stack back together when it returns, without anyone typing `vibey stop && vibey`.

Born 2026-10-02 ("this whole process is so annoying, I'm just trying to get
this damn robot on"). The failure modes it handles, all seen for real:

  away     The robot browns out when its motors start (even on the charger) and
           drops off Wi-Fi for a minute. While it is gone: say nothing, restart
           nothing, just remember when it was last seen.
  return   Its WebRTC peers (robot mic, camera) are stale, so restart those two,
           and if the chat service still thinks it is awake, re-arm the body
           through POST :8772/wake (motors, wake pose, tracking).
  stale ip The daemon computes `wlan_ip` ONCE at start. After a reboot it often
           starts while Wi-Fi is still in hotspot mode, so it advertises
           10.42.0.1 forever and every WebRTC stream times out while REST works
           fine. Fix is a daemon restart (rate limited), then re-arm.
  moved    The robot came back at a different address (DHCP, new network, mDNS
           dead). Write .env and re-run start_wonder.sh, which stops and starts
           everything (this process included) in the right order.
  limp     Chat says awake and not OFF, the robot answers, but motors are
           disabled for more than 15s: re-arm.

Brownout guard: if the robot drops within 3 minutes of its motors coming on,
twice in a row, stop auto-waking it and send Jack ONE Telegram note. A stable
10 minutes clears the guard. Everything else is log-only (quiet_alerts).

State for `vibey status` is written to /tmp/vibey_heal.json every check.

Stdlib only, system python. Started by start_wonder.sh; log /tmp/reachy_heal.log.
    HEAL_URL=http://10.0.0.1:8000 python3 reachy_heal.py   # point at a wrong
                                                            # address to test a drop
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import reachy_connect  # noqa: E402

CHECK_S = float(os.environ.get("HEAL_CHECK_S", "10"))
MISSES_AWAY = 2                 # two failed checks (~20s) = away
BROWNOUT_WINDOW_S = 180         # a drop this soon after motors came on is a brownout
BROWNOUT_STRIKES = 2
STABLE_RESET_S = 600            # up this long clears the guard
LIMP_GRACE_S = 15
REARM_MIN_GAP_S = 30            # never re-arm more often than this
DAEMON_RESTART_GAP_S = 20 * 60
FIND_EVERY_S = 30               # while away, look for it elsewhere this often
CHAT = os.environ.get("CHAT_URL", "http://localhost:8772")
STATE_PATH = "/tmp/vibey_heal.json"
BROWNOUT_MSG = ("I keep browning out when my motors start. Check the charger "
                "cable is fully seated, then power-cycle me.")

# The two services that hold a WebRTC session to the robot.
MEDIA = {
    "robot_mic": ("reachy_robot_mic.py", "reachy_env/bin/python3", "/tmp/reachy_robot_mic.log"),
    "camera":    ("reachy_camera.py",    "reachy_env/bin/python3", "/tmp/reachy_camera.log"),
}


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [heal] {msg}", flush=True)


def ev(text: str) -> None:
    """Healer actions into Vibey's event stream (dashboard Live chat)."""
    try:
        import reachy_events
        reachy_events.emit("system", f"healer: {text}", source="heal", icon="🩹")
    except Exception:  # noqa: BLE001
        pass


def _load_env() -> None:
    try:
        for line in open(os.path.join(HERE, ".env")):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


_load_env()


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""


def _probe(url: str, timeout: float = 3.0):
    u = urllib.parse.urlparse(url)
    return reachy_connect.is_robot(u.hostname or "", timeout, u.port or reachy_connect.PORT)

# Test switches: HEAL_NO_TELEGRAM=1 logs the note instead of sending it;
# HEAL_NO_FIND=1 never goes looking elsewhere (so a fake URL stays fake).
NO_TELEGRAM = os.environ.get("HEAL_NO_TELEGRAM") == "1"
NO_FIND = os.environ.get("HEAL_NO_FIND") == "1"


def _get(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _post(url: str, body: dict | None = None, timeout: float = 30.0):
    try:
        req = urllib.request.Request(url, data=json.dumps(body or {}).encode(),
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def notify_once(text: str) -> None:
    """DM the paired owner through @Vibey_Robot. Never the OpenClaw token."""
    if NO_TELEGRAM:
        log(f"(telegram suppressed) would send: {text}")
        return
    try:
        token = os.environ.get("TELEGRAM_VIBEY_TOKEN", "")
        owner = json.load(open(os.path.join(HERE, ".telegram_state.json"))).get("owner")
        if not (token and owner):
            log("no Vibey token/owner; not sending")
            return
        data = urllib.parse.urlencode({"chat_id": owner, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                               data, timeout=10).read()
        log("telegram note sent")
    except Exception as e:  # noqa: BLE001
        log(f"telegram failed: {e}")


def restart_media() -> None:
    """Fresh WebRTC sessions. Same interpreters start_wonder.sh uses."""
    for name, (script, interp, logf) in MEDIA.items():
        subprocess.run(["pkill", "-f", script], capture_output=True)
    time.sleep(1)
    for name, (script, interp, logf) in MEDIA.items():
        with open(logf, "a") as lf:
            lf.write(f"\n--- [heal] restart {time.strftime('%F %T')} ---\n")
            lf.flush()
            subprocess.Popen([os.path.join(HERE, interp), os.path.join(HERE, script)],
                             cwd=HERE, stdout=lf, stderr=lf, start_new_session=True)
    log("restarted robot mic + camera")
    ev("restarted robot mic + camera")


def restart_stack(url: str) -> None:
    """New address: every service latched the old one at startup. start_wonder.sh
    stops everything (this process too) and starts it all against the new URL.
    arch -arm64 because this python is Intel under Rosetta, and a translated
    parent makes the universal .venv python pick its x86 slice (numpy breaks)."""
    log(f"robot moved → {url}; restarting the stack")
    ev("robot moved to a new address, restarting the stack")
    subprocess.Popen(["arch", "-arm64", "/bin/zsh", os.path.join(HERE, "start_wonder.sh")],
                     cwd=HERE, env={**os.environ, "REACHY_URL": url, "HEAL_REWAKE": S["rewake"] and "1" or ""},
                     stdout=open("/tmp/start_wonder.log", "a"), stderr=subprocess.STDOUT,
                     start_new_session=True)


S = {
    "url": os.environ.get("HEAL_URL") or os.environ.get("REACHY_URL", ""),
    "status": "unknown",      # home | away
    "misses": 0,
    "last_seen": 0.0,
    "last_seen_url": "",
    "up_since": 0.0,
    "away_since": 0.0,
    "motors_on_at": 0.0,      # last time motors came on (seen or caused)
    "motors": None,
    "limp_since": 0.0,
    "last_rearm": 0.0,
    "last_daemon_restart": 0.0,
    "last_find": 0.0,
    "strikes": 0,
    "guard": False,           # brownout guard tripped: no auto-waking
    "noted": False,           # the one Telegram note has gone out
    "rewake": False,          # chat was awake when we last looked
    "wlan_ip": None,
    "note": "",
}


def save() -> None:
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({**S, "pid": os.getpid(), "at": time.time()}, f)
        os.replace(tmp, STATE_PATH)
    except Exception:  # noqa: BLE001
        pass


def chat_state() -> dict | None:
    return _get(f"{CHAT}/state", 4)


def wants_body(cs: dict | None) -> bool:
    return bool(cs) and not cs.get("asleep", True) and not cs.get("off", False)


def rearm(why: str) -> None:
    if S["guard"]:
        log(f"not re-arming ({why}): brownout guard is on")
        return
    if time.time() - S["last_rearm"] < REARM_MIN_GAP_S:
        return
    S["last_rearm"] = time.time()
    S["motors_on_at"] = time.time()
    log(f"re-arming the body via :8772/wake ({why})")
    ev(f"re-arming motors ({why})")
    _post(f"{CHAT}/wake", {}, timeout=40)


def on_drop() -> None:
    now = time.time()
    S["status"], S["away_since"], S["up_since"] = "away", now, 0.0
    S["limp_since"] = 0.0
    since_motors = now - S["motors_on_at"] if S["motors_on_at"] else None
    if since_motors is not None and since_motors < BROWNOUT_WINDOW_S:
        S["strikes"] += 1
        log(f"robot away, {since_motors:.0f}s after its motors came on "
            f"(brownout strike {S['strikes']}/{BROWNOUT_STRIKES})")
        if S["strikes"] >= BROWNOUT_STRIKES and not S["guard"]:
            S["guard"] = True
            log("brownout guard ON: no more auto-waking until it is stable for 10 min")
            ev("brownout guard on, no more auto-waking")
            if not S["noted"]:
                S["noted"] = True
                notify_once(BROWNOUT_MSG)
    else:
        S["strikes"] = 0
        log(f"robot away (last seen {S['last_seen_url']})")
        ev("robot dropped off the network")
    S["motors_on_at"] = 0.0


def on_return(st: dict) -> None:
    S["status"], S["up_since"] = "home", time.time()
    log(f"robot back at {S['url']} after {time.time() - S['away_since']:.0f}s")
    ev(f"robot back after {time.time() - S['away_since']:.0f}s")
    if stale_wlan_ip(st):
        return          # the daemon restart path re-arms and restarts media itself
    restart_media()
    cs = chat_state()
    if wants_body(cs):
        time.sleep(3)
        rearm("robot came back while chat was awake")


def stale_wlan_ip(st: dict) -> bool:
    """Daemon advertising an address it no longer has → restart it, once in a while."""
    adv, real = st.get("wlan_ip"), _host(S["url"])
    S["wlan_ip"] = adv
    if not adv or not re.match(r"^\d+\.\d+\.\d+\.\d+$", real) or adv == real:
        return False
    if time.time() - S["last_daemon_restart"] < DAEMON_RESTART_GAP_S:
        return False
    S["last_daemon_restart"] = time.time()
    was_awake = wants_body(chat_state())
    log(f"daemon advertises {adv} but answers at {real}: WebRTC can't connect. "
        "Restarting the daemon.")
    ev("daemon advertising a stale address, restarting it")
    _post(f"{S['url']}/api/daemon/restart", timeout=30)
    # "running" comes back within seconds, but the motor backend finishes
    # starting after that and comes up DISABLED, silently undoing a re-arm sent
    # too early (seen live: re-armed at +7s, limp a second later). Wait for the
    # backend to report a motor mode, then let it settle.
    time.sleep(5)
    for _ in range(20):
        st2 = _probe(S["url"], 2) or {}
        if st2.get("state") == "running" and \
                (st2.get("backend_status") or {}).get("motor_control_mode"):
            break
        time.sleep(3)
    time.sleep(6)
    log(f"daemon back, now advertising {(_probe(S['url'], 2) or {}).get('wlan_ip')}")
    restart_media()
    if was_awake:
        time.sleep(3)
        S["last_rearm"] = 0.0
        rearm("daemon restart left the motors off")
    return True


def check_body(st: dict) -> None:
    m = (_get(f"{S['url']}/api/motors/status", 3) or {}).get("mode")
    if m == "enabled" and S["motors"] not in (None, "enabled"):
        S["motors_on_at"] = time.time()
    if m == "enabled" and S["motors"] is None and not S["motors_on_at"]:
        S["motors_on_at"] = time.time()
    S["motors"] = m
    cs = chat_state()
    S["rewake"] = wants_body(cs)
    if m is None or not wants_body(cs):
        S["limp_since"] = 0.0
        return
    if m == "enabled":
        S["limp_since"] = 0.0
        return
    if not S["limp_since"]:
        S["limp_since"] = time.time()
        return
    if time.time() - S["limp_since"] > LIMP_GRACE_S:
        rearm(f"chat awake but motors {m} for {time.time() - S['limp_since']:.0f}s")
        S["limp_since"] = 0.0


def tick() -> None:
    now = time.time()
    st = _probe(S["url"], 3) if S["url"] else None
    if st:
        S["misses"] = 0
        S["last_seen"], S["last_seen_url"] = now, S["url"]
        if S["status"] != "home":
            if S["status"] == "away":
                on_return(st)
            else:
                S["status"], S["up_since"] = "home", now
                log(f"robot reachable at {S['url']}")
                stale_wlan_ip(st)
        else:
            stale_wlan_ip(st)
        if S["guard"] and S["up_since"] and now - S["up_since"] > STABLE_RESET_S:
            S["guard"], S["strikes"], S["noted"] = False, 0, False
            log("stable for 10 min: brownout guard cleared")
        elif S["strikes"] and S["up_since"] and now - S["up_since"] > STABLE_RESET_S:
            S["strikes"] = 0
        check_body(st)
        return
    S["misses"] += 1
    if S["status"] != "away" and S["misses"] >= MISSES_AWAY:
        on_drop()
    if S["status"] == "away" and not NO_FIND and now - S["last_find"] > FIND_EVERY_S:
        S["last_find"] = now
        found = reachy_connect.find_robot(None, scan=True, log=log)
        if found and _host(found) != _host(S["url"]):
            if not reachy_connect.env_points_at(found):
                reachy_connect.set_env_url(found)
                log(f".env REACHY_URL → {found}")
            S["url"] = found
            save()
            restart_stack(found)


def main() -> None:
    log(f"watching {S['url'] or '(no url)'} every {CHECK_S:g}s")
    # A restart_stack() from the previous instance asks us to put the body back.
    if os.environ.get("HEAL_REWAKE") == "1":
        for _ in range(30):
            time.sleep(3)
            cs = chat_state()
            if cs and cs.get("mode") not in (None, "starting"):
                if not cs.get("off"):
                    log("re-waking after the address change")
                    _post(f"{CHAT}/wake", {}, timeout=40)
                    S["motors_on_at"] = time.time()
                break
    while True:
        try:
            tick()
        except Exception as e:  # noqa: BLE001
            log(f"tick failed: {e}")
        save()
        time.sleep(CHECK_S)


if __name__ == "__main__":
    main()
