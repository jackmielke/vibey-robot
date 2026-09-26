#!/usr/bin/env python3
"""
reachy_idle.py — the small motion that makes Vibey look alive between gestures.

A robot that holds perfectly still between emotes reads as switched off, and the
gap is most of the conversation: an emote lasts a second or two and then there
is nothing until the next one. This fills that with breathing-scale motion —
a slow sway, an occasional blink of the antennas, a weight shift.

Deliberately BENEATH everything else:

  * It never runs while an emote is playing, while Vibey is speaking, or while
    something else has taken the neck (mimic, face tracking with a face in view).
    One writer at a time is the rule the rest of this repo already learned the
    hard way — two things driving the neck at 30Hz looks like a fault.
  * Amplitudes are a fraction of an emote's. If you can tell which movement is
    the idle, it is too big: ±0.05 rad here against ±0.3 for a real gesture.
  * It yields instantly. `pause()` is called before an emote and `resume()`
    after, so the idle never has to be waited for.

    import reachy_idle
    reachy_idle.start()          # begin breathing
    reachy_idle.pause()          # someone else wants the neck
    reachy_idle.resume()

Env:
    REACHY_URL          the robot
    IDLE_ENABLED        "0" to disable entirely
"""

from __future__ import annotations

import math
import os
import random
import threading
import time

from reachy_emotes import _goto, _pose  # same transport as every other motion

ENABLED = os.environ.get("IDLE_ENABLED", "1").strip() != "0"

# One step every this often. Slow: this is breathing, not animation. Each step
# is a `goto` with a duration slightly longer than the interval, so the moves
# overlap into a continuous drift rather than a sequence of little arrivals.
STEP_S = 2.2

# Ceilings, in radians / metres. A tenth of what an emote uses.
SWAY_ROLL = 0.05
SWAY_YAW = 0.07
BREATH_Z = 0.006
ANTENNA_DRIFT = 0.12
ANTENNA_BIAS = 0.2          # the SDK's resting antenna position

_state = {"on": False, "paused": 0, "thread": None}
_lock = threading.Lock()


def _breathing_step(t: float) -> None:
    """One slow drift toward a nearby pose.

    Two sine terms at incommensurate periods (7s and 11s) rather than one, so
    the motion never visibly repeats — a single sine is a pendulum, and a
    pendulum is the most machine-like thing a robot can do.
    """
    roll = SWAY_ROLL * math.sin(t / 7.0)
    yaw = SWAY_YAW * math.sin(t / 11.0)
    z = BREATH_Z * math.sin(t / 4.0)
    # The antennas get their own second sine rather than a random multiplier.
    # The multiplier was re-rolled every step, so while the head glided along a
    # continuous curve the antenna target jumped to a new random place every
    # two seconds — and because each goto is longer than the interval, the next
    # jump landed mid-flight. Read from across the room that is not breathing,
    # it is a twitch, and it was the most machine-like thing left in the idle.
    # Two incommensurate periods still never visibly repeat, which is all the
    # randomness was there for.
    ant = ANTENNA_BIAS + ANTENNA_DRIFT * (
        0.7 * math.sin(t / 9.0) + 0.3 * math.sin(t / 5.5 + 1.3))
    _goto(_pose(roll=roll, yaw=yaw, z=z), [ant, -ant], STEP_S * 1.35)


def _loop() -> None:
    t0 = time.time()
    while _state["on"]:
        try:
            if _state["paused"] <= 0:
                _breathing_step(time.time() - t0)
        except Exception:  # noqa: BLE001 — a failed idle step is not an event
            pass
        # Vary the interval. A fixed tick is audible in the servos as a rhythm.
        time.sleep(STEP_S * random.uniform(0.8, 1.25))


def start() -> bool:
    if not ENABLED:
        return False
    with _lock:
        if _state["on"]:
            return True
        _state["on"] = True
        _state["thread"] = threading.Thread(target=_loop, daemon=True)
        _state["thread"].start()
    return True


def stop() -> None:
    _state["on"] = False


def pause() -> None:
    """Yield the neck. Counted, not boolean: two overlapping emotes each
    pausing and resuming would otherwise have the first resume undo the
    second's pause, and the idle would start drifting mid-gesture."""
    with _lock:
        _state["paused"] += 1


def resume() -> None:
    with _lock:
        _state["paused"] = max(0, _state["paused"] - 1)


def status() -> dict:
    return {"on": _state["on"], "paused": _state["paused"], "enabled": ENABLED}


if __name__ == "__main__":
    start()
    print("idling — ctrl-c to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop()
