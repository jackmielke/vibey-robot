#!/usr/bin/env python3
"""
antenna_test.py — is the left antenna dead, or is something else?

Written because "the left antenna isn't responding" has at least five causes and
only one of them is a broken part. Opening the head to look for a loose wire is
the LAST step, not the first — this walks the cheaper ones first and tells you
which it is.

WHAT THIS MOVES: the antennas, and nothing else. `/api/move/goto` treats every
field except `duration` as optional, so this omits `head_pose` and `body_yaw`
entirely rather than sending the current values back. Nothing commands the neck,
which matters when the shell is open and you don't want the head jostled.

THE CONTROL GROUP IS THE POINT. This robot has a documented failure where the
daemon wedges: `/api/move/*` returns 200 with a job id and the body never
twitches, while every readback still looks alive. If you test the left antenna
during one of those, it looks exactly like a dead motor and you go find a
screwdriver for no reason. So the right antenna is always tested too, as a
control. Both dead means the daemon, not the hardware.

WHAT FIGHTS YOU: reachy_idle's breathing and the memory service's glance both
write to the antennas continuously while Vibey is awake — at 2.2s intervals, so
they will overwrite a test pose between the command and the readback and make a
working antenna look intermittent. Put Vibey to sleep first (`vibey sleep`) or
pass --force to test anyway.

    python3 antenna_test.py             # full diagnosis, both sides
    python3 antenna_test.py --watch     # stream positions, move them by hand
    python3 antenna_test.py --left      # just the left one, repeatedly
    python3 antenna_test.py --force     # skip the "put it to sleep" check

Readback is `/api/state/present_antenna_joint_positions` -> [left, right] in
radians. Emotes drive them mirrored (`[a, -a]`), so a symmetric pose has
opposite signs. Rest is near 0; +-0.8 is a big visible flick.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

from reachy_voice import load_env

load_env()

URL = os.environ.get("REACHY_URL", "http://reachy-mini.local:8000").rstrip("/")

# How far to drive an antenna during the test, in radians. Big enough to see
# across a room and to clear any encoder noise floor, well inside the range the
# emotes already use every day.
SWING = 0.7
SETTLE = 1.6          # seconds to let a move finish before believing a readback
MOVED = 0.08          # radians of change that counts as "it moved"

# Set when this script turned the motors on itself, so it can turn them back.
_RESTORE = {"disable_after": False}


def restore_motors() -> None:
    if not _RESTORE["disable_after"]:
        return
    try:
        _post("/api/motors/set_mode/disabled", {})
        print("  motors back to disabled (as they were)")
    except Exception:  # noqa: BLE001
        print("  ! could not disable motors again — `vibey sleep` will")


def _get(path: str, timeout: float = 4.0):
    with urllib.request.urlopen(f"{URL}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def _post(path: str, body: dict, timeout: float = 6.0):
    req = urllib.request.Request(
        f"{URL}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def antennas() -> list[float] | None:
    try:
        v = _get("/api/state/present_antenna_joint_positions")
        return [float(x) for x in v]
    except Exception:  # noqa: BLE001
        return None


def move(left: float, right: float, duration: float = 1.0):
    """Antennas only. No head_pose key at all — see the module docstring."""
    return _post("/api/move/goto",
                 {"antennas": [left, right], "duration": duration})


# --------------------------------------------------------------------------- #
def preflight() -> tuple[bool, list[float]]:
    print(f"robot   {URL}")
    try:
        st = _get("/api/daemon/status", timeout=6)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ unreachable — {e}")
        print("    power it on and let it join Wi-Fi, then run this again.")
        return False, []
    b = st.get("backend_status") or {}
    mode = b.get("motor_control_mode")
    print(f"  ✓ daemon {st.get('state')}   motors: {mode}")
    if mode != "enabled":
        # `vibey sleep` quiets the idle-breathing and the glance — exactly what
        # this test needs — but it also disables the motors, so "quiet enough to
        # test" and "able to move" used to be mutually exclusive and this bailed
        # every time. Enable them here instead of sending the user away; the
        # writers stay asleep, and _RESTORE puts the motors back afterwards so
        # a sleeping robot doesn't sit here holding torque.
        print("    motors were off (that's what `vibey sleep` does) — enabling")
        print("    them for the test; they go back off at the end.")
        try:
            _post("/api/motors/set_mode/enabled", {})
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ could not enable motors — {e}")
            return False, []
        _RESTORE["disable_after"] = True
        time.sleep(1.5)
    pos = antennas()
    if pos is None:
        print("  ✗ cannot read antenna positions at all")
        return False, []
    print(f"  ✓ readback  left={pos[0]:+.3f}  right={pos[1]:+.3f}")
    return True, pos


def is_quiet() -> bool:
    """True when nothing else is driving the body."""
    try:
        with urllib.request.urlopen("http://localhost:8772/state", timeout=2) as r:
            st = json.loads(r.read())
        return bool(st.get("asleep"))
    except Exception:  # noqa: BLE001 — chat not running is itself quiet
        return True


def test_side(name: str, idx: int, base: list[float]) -> dict:
    """Drive one antenna, hold the other where it is, and see what happened."""
    other = 1 - idx
    target = [base[0], base[1]]
    # Away from wherever it is now, so a resting antenna and a stuck-at-limit
    # one both produce a real commanded delta.
    target[idx] = -SWING if base[idx] > 0 else SWING
    target[other] = base[other]

    print(f"\n  {name}: {base[idx]:+.3f} → {target[idx]:+.3f} "
          f"(holding {'right' if idx == 0 else 'left'} at {base[other]:+.3f})")
    try:
        move(target[0], target[1])
    except Exception as e:  # noqa: BLE001
        print(f"    ✗ command rejected — {e}")
        return {"name": name, "moved": False, "delta": 0.0, "error": str(e)}
    time.sleep(SETTLE)

    now = antennas() or base
    delta = abs(now[idx] - base[idx])
    other_delta = abs(now[other] - base[other])
    moved = delta >= MOVED
    print(f"    readback {now[idx]:+.3f}   moved {delta:.3f} rad  "
          f"{'✓ MOVED' if moved else '✗ did not move'}")
    if other_delta >= MOVED:
        # Worth knowing: it means the two are not independently addressable,
        # which is a wiring/config answer rather than a dead-motor one.
        print(f"    ! the other antenna moved too ({other_delta:.3f} rad)")
    return {"name": name, "moved": moved, "delta": delta,
            "cross": other_delta >= MOVED}


def diagnose(force: bool = False) -> int:
    ok, base = preflight()
    if not ok:
        return 1

    if not force and not is_quiet():
        print("\n  ! Vibey is awake, so idle-breathing and the face-glance are")
        print("    both writing to the antennas every couple of seconds. They")
        print("    will overwrite this test and make a good antenna look")
        print("    intermittent. Run `vibey sleep` first, or pass --force.")
        return 2

    left = test_side("LEFT ", 0, base)
    base2 = antennas() or base
    right = test_side("RIGHT", 1, base2)

    print("\n  returning to rest…")
    try:
        move(0.0, 0.0, duration=1.2)
        time.sleep(1.4)
    except Exception:  # noqa: BLE001
        pass
    restore_motors()

    print("\n" + "─" * 58)
    if left["moved"] and right["moved"]:
        print("  BOTH ANTENNAS MOVE.")
        print("  The hardware is fine. If it looked stuck during a conversation,")
        print("  suspect what was driving it, not the motor: an emote and the")
        print("  idle breathing fighting for the same joint, or a pose that")
        print("  happens to sit near where it already was.")
        return 0
    if not left["moved"] and not right["moved"]:
        print("  NEITHER ANTENNA MOVED — and that points away from the left one.")
        print("  Two motors do not fail at once. This is the known daemon wedge:")
        print("  commands accepted, nothing actuated. Fix and re-test:")
        print("      vibey fix   &&   python3 antenna_test.py")
        return 3
    dead = "LEFT" if not left["moved"] else "RIGHT"
    good = "RIGHT" if dead == "LEFT" else "LEFT"
    print(f"  {good} MOVES, {dead} DOES NOT.")
    print("  The daemon, the motors as a whole, and the command path are all")
    print("  proven working by the side that moved — so this is isolated to the")
    print(f"  {dead.lower()} antenna. In rough order of likelihood:")
    print(f"    1. the {dead.lower()} servo's connector, unseated at either end")
    print("    2. the antenna hub set-screw slipping on the shaft — the motor")
    print("       turns, the antenna doesn't (readback would still look dead)")
    print("    3. the servo itself")
    print(f"  Check with --watch: turn the {dead.lower()} antenna by hand. If the")
    print("  number moves, the encoder and wiring are alive and it is mechanical")
    print("  (2). If it stays frozen, it is electrical (1 or 3).")
    return 4


def watch() -> None:
    """Live positions. Back-drive an antenna by hand and watch the number.

    This is the one test that separates 'the motor cannot move it' from 'the
    motor moves and the antenna does not' — the second still reads back.
    """
    print(f"watching {URL} — turn an antenna by hand. ctrl-c to stop.")
    print("  (motors off = free to back-drive. PEAK is the biggest deviation")
    print("   seen, so a turn that springs back still shows up.)\n")
    base = antennas()
    if base is None:
        print("  ✗ no readback at all — encoders or comms are down, not one antenna")
        return
    peak = [0.0, 0.0]
    try:
        while True:
            p = antennas()
            if p is None:
                print("  (no readback)          ", end="\r")
            else:
                dl, dr = p[0] - base[0], p[1] - base[1]
                peak[0] = max(peak[0], abs(dl))
                peak[1] = max(peak[1], abs(dr))
                print(f"  left {p[0]:+.3f} (Δ{dl:+.3f} peak {peak[0]:.3f})   "
                      f"right {p[1]:+.3f} (Δ{dr:+.3f} peak {peak[1]:.3f})  ",
                      end="\r", flush=True)
            time.sleep(0.15)
    except KeyboardInterrupt:
        print("\n")
        for i, side in enumerate(("left", "right")):
            if peak[i] >= MOVED:
                print(f"  {side}: encoder tracked {peak[i]:.3f} rad of hand movement")
                print(f"        → wiring and sensor are ALIVE. If it won't move")
                print(f"          under power, suspect the hub set-screw slipping")
                print(f"          on the shaft, or the servo itself.")
            else:
                print(f"  {side}: never moved ({peak[i]:.3f} rad)")
                print(f"        → if you actually turned it, the encoder is not")
                print(f"          reporting: connector or servo, not mechanical.")


def wiggle(idx: int, times: int = 6) -> None:
    """Drive one antenna back and forth so you can watch the linkage."""
    name = "left" if idx == 0 else "right"
    ok, base = preflight()
    if not ok:
        return
    print(f"\nwiggling the {name} antenna {times}x — watch the horn and the hub")
    for i in range(times):
        t = [base[0], base[1]]
        t[idx] = SWING if i % 2 == 0 else -SWING
        try:
            move(t[0], t[1], duration=0.6)
        except Exception as e:  # noqa: BLE001
            print(f"  command failed: {e}")
            return
        time.sleep(0.75)
        p = antennas()
        if p:
            print(f"  {i+1}: commanded {t[idx]:+.2f}  read {p[idx]:+.3f}")
    move(0.0, 0.0, duration=1.0)


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--watch" in a:
        watch()
    elif "--left" in a:
        wiggle(0)
    elif "--right" in a:
        wiggle(1)
    else:
        sys.exit(diagnose(force="--force" in a))
