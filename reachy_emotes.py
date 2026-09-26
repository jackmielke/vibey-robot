#!/usr/bin/env python3
"""
reachy_emotes.py — Vibey's emotional vocabulary: 5 emotes, motion + sound.

Each emote is a short head/antenna choreography (via the daemon's /move/goto)
plus a matching R2-style chirp. The chirps are synthesized right here (sine
sweeps, stdlib `wave` only) and uploaded to the robot's speaker on first use —
no audio assets to download or commit.

    happy    perk + side-to-side wiggle          rising major arpeggio
    excited  double head-bounce, antennas flared  fast double up-sweep
    curious  head tilt, one antenna up            "hmm?" bend with vibrato
    sad      slow droop, antennas fall            long falling sweep
    smug     look away + up-tilt, half antenna    two low deadpan blips

Use as a module (non-blocking):
    from reachy_emotes import play
    play("happy")                # motion only (safe while Vibey is speaking)
    play("happy", sound=True)    # motion + chirp

Or from the CLI:
    python3 reachy_emotes.py happy
"""

from __future__ import annotations

import io
import json
import math
import os
import random
import struct
import threading
import time
import urllib.request
import uuid
import wave

from reachy_voice import load_env

load_env()

REACHY_URL = os.environ.get("REACHY_URL", "http://192.168.12.240:8000").rstrip("/")

NEUTRAL = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}

EMOTIONS = ["happy", "excited", "curious", "sad", "smug", "thinking", "victory",
            "wave", "nod", "shake", "wave_left", "wave_right", "peace",
            "smile", "tilt_left", "tilt_right", "tilt_left_big",
            "tilt_right_big", "dance", "antenna_check", "left_antenna_check",
            "right_antenna_check", "whistle"]


# --------------------------------------------------------------------------- #
# Robot REST helpers                                                          #
# --------------------------------------------------------------------------- #
def _post(path: str, body: dict | None = None, timeout: float = 8.0):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(f"{REACHY_URL}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except Exception as e:  # noqa: BLE001 - robot hiccups shouldn't crash callers
        print(f"[emote] POST {path} failed: {e}", flush=True)
        return None


def _get(path: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(f"{REACHY_URL}{path}", timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except Exception as e:  # noqa: BLE001
        print(f"[antenna] GET {path} failed: {e}", flush=True)
        return None


def _goto(head: dict, antennas: list[float], duration: float):
    _post("/api/move/goto",
          {"head_pose": head, "antennas": _compensate(antennas),
           "duration": duration})


# --------------------------------------------------------------------------- #
# Antenna health — one servo can go quiet while the other is fine.            #
#                                                                             #
# Every emote below sends BOTH antennas in one vector, so a one-sided failure #
# is never a bug in the choreography: either the daemon dropped that joint    #
# (motors off / torque disabled after an overload) or the servo itself isn't  #
# answering. This block commands each side alone, reads the joint back from   #
# /api/state/full, and says which of the two it is. If a side is genuinely    #
# dead, _compensate() mirrors its command onto the live antenna so gestures   #
# still read instead of silently playing half a face.                         #
# --------------------------------------------------------------------------- #
STUCK_RAD = 0.15        # travel below this, on a ~0.9 rad command, is "didn't move"
_DEAD = {"right": False, "left": False}
# What the last check concluded, so the voice brain can answer "is your antenna
# broken?" instantly instead of standing there probing for ten seconds.
_LAST_CHECK = {"right": "unknown", "left": "unknown"}


def _compensate(antennas: list[float] | None) -> list[float] | None:
    """Mirror a dead antenna's command onto the live one.

    Antennas are commanded mirrored (right positive, left negative for the same
    visual shape), so the stand-in for a left value L is -L on the right."""
    if not antennas or len(antennas) != 2:
        return antennas
    if _DEAD["right"] == _DEAD["left"]:   # both fine, or both gone — nothing to do
        return antennas
    right, left = antennas
    if _DEAD["left"]:
        return [right if abs(right) >= abs(left) else -left, left]
    return [right, left if abs(left) >= abs(right) else -right]


def _debug_state(state: str, detail: str) -> None:
    """One grep-able line per fault state — `left_motor_fault`,
    `antenna_disconnected` — so a log tail says which failure this was."""
    print(f"[antenna] state={state} :: {detail}", flush=True)


def _antennas_now() -> dict | None:
    """Measured antenna angles as {'right': x|None, 'left': y|None}, or None if
    the daemon has no antenna feedback at all (robot down / field missing).

    A single side reading None is the useful case: the bus enumerated one servo
    and not the other, which is what a pulled or broken connector looks like
    from up here — distinct from a servo that answers but will not turn."""
    pos = (_get("/api/state/full") or {}).get("antennas_position")
    if isinstance(pos, dict):
        pos = [pos.get("right_antenna"), pos.get("left_antenna")]
    if not isinstance(pos, (list, tuple)) or len(pos) < 2:
        return None
    return {side: (None if pos[i] is None else float(pos[i]))
            for i, side in enumerate(("right", "left"))}


def _probe(side: str, amount: float = 0.9) -> tuple[str, float | None]:
    """Command ONE antenna. Returns (why, travel):
       ('moved', rad)     the joint answered — rad is how far it actually went
       ('no_feedback', None)  the joint is missing from the state report
       ('unreachable', None)  no antenna feedback at all; nothing to conclude"""
    _post("/api/move/goto", {"head_pose": NEUTRAL, "antennas": [0.0, 0.0],
                             "duration": 0.5})
    time.sleep(0.7)
    base = _antennas_now()
    target = [amount, 0.0] if side == "right" else [0.0, -amount]
    _post("/api/move/goto", {"head_pose": NEUTRAL, "antennas": target,
                             "duration": 0.6})
    time.sleep(0.9)
    moved = _antennas_now()
    _post("/api/move/goto", {"head_pose": NEUTRAL, "antennas": [0.0, 0.0],
                             "duration": 0.5})
    time.sleep(0.6)
    if base is None or moved is None:
        return ("unreachable", None)
    if base[side] is None or moved[side] is None:
        return ("no_feedback", None)
    return ("moved", abs(moved[side] - base[side]))


def antenna_check(recover: bool = True) -> dict:
    """Diagnose both antennas. Returns {'right': verdict, 'left': verdict}:

        'ok'            commanded and measured to move
        'motor_fault'   the servo answers but will not turn — jam or dead motor
        'disconnected'  the joint isn't on the bus at all — cable or connector
        'unknown'       no feedback to judge from (robot down)

    On a motor fault we first try the cheap recovery — re-enable the motors,
    which is what a torque cutoff after an overload actually needs — and
    re-probe once before calling it dead. Never raises: a broken antenna must
    not take the voice down with it."""
    if _get("/api/state/full") is None:
        print("[antenna] robot unreachable — check is inconclusive", flush=True)
        _LAST_CHECK.update(right="unknown", left="unknown")
        return {"right": "unknown", "left": "unknown"}

    # The idle breathing loop drifts the antennas by up to ~0.3 rad on its own,
    # which is more than STUCK_RAD — a live idle would read as a healthy servo.
    try:
        import reachy_idle
        reachy_idle.pause()
    except Exception:  # noqa: BLE001
        reachy_idle = None

    result = {}
    for side in ("right", "left"):
        why, travel = _probe(side)
        if why == "no_feedback":
            _debug_state("antenna_disconnected",
                         f"{side}: the daemon reports no angle for this joint — "
                         "the servo is not answering on the bus, which is a "
                         "cable or connector fault, not a motor one")
            result[side] = "disconnected"
            continue
        if why == "unreachable":
            print(f"[antenna] {side}: no position feedback from the daemon",
                  flush=True)
            result[side] = "unknown"
            continue
        print(f"[antenna] {side}: commanded 0.90 rad, measured "
              f"{travel:.3f} rad", flush=True)
        if travel >= STUCK_RAD:
            result[side] = "ok"
            continue
        if recover:
            print(f"[antenna] {side} did not move — re-enabling motors and "
                  "retrying once", flush=True)
            _post("/api/motors/set_mode/enabled", timeout=10.0)
            time.sleep(1.0)
            why, travel = _probe(side)
            if why == "no_feedback":
                _debug_state("antenna_disconnected",
                             f"{side}: dropped off the bus during recovery")
                result[side] = "disconnected"
                continue
            if why == "unreachable":
                # Lost the robot mid-recovery. Don't convict a servo on that.
                print(f"[antenna] {side}: lost feedback during recovery",
                      flush=True)
                result[side] = "unknown"
                continue
            print(f"[antenna] {side}: after recovery, measured "
                  f"{travel:.3f} rad", flush=True)
        if (travel or 0.0) >= STUCK_RAD:
            result[side] = "ok"
        else:
            result[side] = "motor_fault"
            _debug_state(f"{side}_motor_fault",
                         f"commanded 0.90 rad, moved {(travel or 0.0):.3f} rad "
                         "after a motor re-enable — jammed or dead servo")

    if reachy_idle is not None:
        reachy_idle.resume()

    for side in ("right", "left"):
        _DEAD[side] = result[side] in ("motor_fault", "disconnected")
    if _DEAD["right"] != _DEAD["left"]:
        live = "right" if _DEAD["left"] else "left"
        print(f"[antenna] fallback on: mirroring gestures onto the {live} "
              "antenna until this passes", flush=True)
    _LAST_CHECK.update(result)
    print(f"[antenna] verdict: {result}", flush=True)
    return result


_CHECKLIST = ("Worth three things, in order: reseat the antenna cable, look at "
              "the joint for visible damage, and if both are clean it wants a "
              "repair.")

_VERDICT_WORDS = {
    "motor_fault": "answers but will not turn — jammed or a dead motor",
    "disconnected": "is not on the bus at all — that reads as a cable or "
                    "connector fault",
}


_NEXT_STEP = {
    "motor_fault": "Power-cycle me once; if it still will not turn, it wants a "
                   "repair.",
    "disconnected": "Reseat the antenna cable at the connector, then ask me "
                    "again.",
}


def check_side(side: str, recover: bool = False) -> dict:
    """Probe ONE antenna. The quick counterpart to antenna_check(): about three
    seconds instead of ten, one side, and no motor-re-enable retry unless asked.

    Returns {'side', 'verdict', 'summary'} where verdict is one of the
    antenna_check() verdicts, or 'unavailable' if the probe itself could not
    run. Never raises — the point of this is to answer "is my left antenna
    dead" without being the thing that kills the voice."""
    side = str(side or "").strip().lower()
    side = {"l": "left", "r": "right"}.get(side, side)
    if side not in ("left", "right"):
        return {"side": side, "verdict": "unavailable",
                "summary": f"I do not have an antenna called {side!r}."}

    idle = None
    try:
        # Idle breathing drifts the antennas further than STUCK_RAD, so a live
        # idle loop would read as a healthy servo. Same reason as antenna_check.
        try:
            import reachy_idle as idle
            idle.pause()
        except Exception:  # noqa: BLE001
            idle = None

        if _get("/api/state/full") is None:
            verdict = "unknown"
        else:
            why, travel = _probe(side)
            if why == "no_feedback":
                _debug_state("antenna_disconnected",
                             f"{side}: no angle reported for this joint")
                verdict = "disconnected"
            elif why == "unreachable":
                verdict = "unknown"
            elif travel >= STUCK_RAD:
                verdict = "ok"
            elif recover:
                _post("/api/motors/set_mode/enabled", timeout=10.0)
                time.sleep(1.0)
                why, travel = _probe(side)
                verdict = ("ok" if why == "moved" and travel >= STUCK_RAD
                           else "disconnected" if why == "no_feedback"
                           else "unknown" if why == "unreachable"
                           else "motor_fault")
            else:
                verdict = "motor_fault"
            if verdict == "motor_fault":
                _debug_state(f"{side}_motor_fault",
                             f"commanded 0.90 rad, moved {(travel or 0.0):.3f}")
    except Exception as e:  # noqa: BLE001
        return {"side": side, "verdict": "unavailable",
                "summary": f"The {side} antenna probe would not run ({e}). "
                           "Nothing concluded — worth trying the full check."}
    finally:
        if idle is not None:
            try:
                idle.resume()
            except Exception:  # noqa: BLE001
                pass

    if verdict in ("motor_fault", "disconnected"):
        _DEAD[side] = True
    elif verdict == "ok":
        _DEAD[side] = False
    _LAST_CHECK[side] = verdict
    print(f"[antenna] quick {side} check: {verdict}", flush=True)

    if verdict == "ok":
        summary = f"{side.capitalize()} antenna moves fine."
    elif verdict == "unknown":
        summary = (f"No position feedback for the {side} antenna, so that is "
                   "inconclusive — check that the robot is actually up.")
    else:
        summary = (f"{side.capitalize()} antenna {_VERDICT_WORDS[verdict]}. "
                   f"{_NEXT_STEP[verdict]}")
    return {"side": side, "verdict": verdict, "summary": summary}


def _do_left_antenna_check():
    check_side("left")


def _do_right_antenna_check():
    check_side("right")


def antenna_status() -> dict:
    """Last known antenna health, for anything that needs to TALK about it.

    Returns {'right', 'left', 'fallback', 'summary'} and never raises — a bad
    antenna is something Vibey reports, not something that ends the sentence."""
    try:
        state = dict(_LAST_CHECK)
        bad = [s for s in ("right", "left")
               if state[s] in ("motor_fault", "disconnected")]
        state["fallback"] = bool(bad) and len(bad) == 1
        if not bad:
            state["summary"] = (
                "Both antennas checked out." if "unknown" not in state.values()
                else "I have not got a clean read on the antennas yet.")
        else:
            parts = [f"My {s} antenna {_VERDICT_WORDS[state[s]]}" for s in bad]
            tail = (" The other side is covering the gestures in the meantime."
                    if state["fallback"] else "")
            state["summary"] = ". ".join(parts) + f".{tail} {_CHECKLIST}"
        return state
    except Exception as e:  # noqa: BLE001 - status must never be the thing that breaks
        return {"right": "unknown", "left": "unknown", "fallback": False,
                "summary": f"I could not read my own antenna state ({e})."}


def _do_antenna_check():
    antenna_check()


# How far the head moves, as a multiplier on every choreography below.
#
# The emotes were each tuned in isolation and the imbalance only shows when you
# watch one: antennas swing 0.9–1.2 rad while the head rolls 0.15, so the
# antennas read as the whole performance and the head reads as switched off.
# Measured across happy/smile/wave the ratio is 5–10x. Nobody chose that; it
# accumulated one emote at a time.
#
# Scaling here rather than editing 28 functions keeps every choreography's
# *shape* — the timings, the phase relationships, which way it leans — and
# changes only the depth. HEAD_GAIN=1 restores the old behaviour exactly.
HEAD_GAIN = float(os.environ.get("HEAD_GAIN", "2.4"))

# Past this the neck runs out of travel and the daemon clamps, which turns a
# lean into a jerk. Well inside the mechanism, comfortably past anything the
# emotes ask for.
_HEAD_LIMIT = {"roll": 0.42, "pitch": 0.38, "yaw": 0.55,
               "x": 0.03, "y": 0.03, "z": 0.025}


def _pose(**kw) -> dict:
    if HEAD_GAIN != 1.0:
        kw = {k: (max(-_HEAD_LIMIT[k], min(_HEAD_LIMIT[k], v * HEAD_GAIN))
                  if k in _HEAD_LIMIT and isinstance(v, (int, float)) else v)
              for k, v in kw.items()}
    return dict(NEUTRAL, **kw)


# --------------------------------------------------------------------------- #
# Chirp synthesis — tiny sine sweeps with a soft envelope, 16-bit mono WAV.   #
# --------------------------------------------------------------------------- #
SR = 22050


def _sweep(f0: float, f1: float, dur: float, vol: float = 0.5,
           vibrato: float = 0.0) -> list[float]:
    n = int(SR * dur)
    out = []
    for i in range(n):
        t = i / SR
        frac = i / max(1, n - 1)
        f = f0 + (f1 - f0) * frac
        if vibrato:
            f += vibrato * math.sin(2 * math.pi * 18 * t)
        # soft attack/release so chirps don't click
        env = min(1.0, i / (SR * 0.01), (n - i) / (SR * 0.03))
        out.append(vol * env * math.sin(2 * math.pi * f * t))
    return out


def _silence(dur: float) -> list[float]:
    return [0.0] * int(SR * dur)


def _to_wav(samples: list[float]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples))
    return buf.getvalue()


def _chirp(emotion: str) -> bytes:
    if emotion == "happy":       # rising major arpeggio
        s = _sweep(523, 523, .09) + _sweep(659, 659, .09) + _sweep(784, 784, .14)
    elif emotion == "excited":   # fast double up-sweep
        s = _sweep(500, 1400, .16) + _silence(.05) + _sweep(600, 1600, .18, .6)
    elif emotion == "curious":   # questioning bend with vibrato
        s = _sweep(600, 500, .12) + _sweep(500, 950, .28, .45, vibrato=25)
    elif emotion == "sad":       # long falling sweep
        s = _sweep(700, 220, .7, .4)
    elif emotion == "thinking":   # slow "hmm" wobble
        s = (_sweep(440, 480, .18, .35, vibrato=8) + _silence(.07)
             + _sweep(480, 420, .22, .3, vibrato=12) + _silence(.06)
             + _sweep(420, 460, .18, .25, vibrato=10))
    elif emotion == "victory":    # ascending fanfare + triumphant burst
        s = (_sweep(523, 784, .12) + _sweep(784, 1046, .12)
             + _sweep(1046, 1046, .08) + _silence(.04)
             + _sweep(880, 1318, .2, .65) + _silence(.05)
             + _sweep(1046, 1568, .25, .7))
    elif emotion == "whistle":    # wolf whistle, then a little three-note tune
        s = (_sweep(900, 2300, .22, .55) + _silence(.06)
             + _sweep(1500, 650, .5, .5, vibrato=6) + _silence(.25)
             + _sweep(1175, 1175, .16, .45, vibrato=10) + _silence(.03)
             + _sweep(1318, 1318, .16, .45, vibrato=10) + _silence(.03)
             + _sweep(1568, 1480, .38, .45, vibrato=14))
    elif emotion == "wave":       # a chirpy two-tone "hi!"
        s = _sweep(660, 880, .12, .5) + _silence(.04) + _sweep(880, 1100, .16, .5)
    elif emotion == "laugh":      # belly laugh: four "ha"s falling and fading
        s = (_sweep(760, 700, .07, .5) + _silence(.05)
             + _sweep(700, 640, .07, .45) + _silence(.06)
             + _sweep(640, 570, .08, .38) + _silence(.07)
             + _sweep(570, 480, .11, .3))
    elif emotion == "laugh_wheeze":
        # the silent laugh: thin, high, almost no body, and it runs out of air
        # rather than finishing. Short blips with heavy vibrato read as gasps;
        # the long climb at the end is the breath that finally gets in.
        s = []
        for f in (1180, 1240, 1150, 1220, 1100, 1160):
            s += _sweep(f, f + 40, .045, .22, vibrato=38) + _silence(.045)
        s += _silence(.06) + _sweep(760, 1320, .34, .26, vibrato=16)
    elif emotion == "laugh_cackle":
        # the unhinged one: every "ha" starts higher than the last and the
        # gaps close up, so it accelerates into a squeal instead of settling.
        s = []
        f, gap, vol = 520.0, .075, .5
        for _ in range(6):
            s += _sweep(f, f + 90, .055, vol) + _silence(gap)
            f *= 1.16; gap *= 0.86; vol = max(.34, vol - .02)
        s += _sweep(1250, 1600, .18, .42, vibrato=45) + _sweep(1600, 900, .16, .3)
    elif emotion == "nod":        # short affirmative blip
        s = _sweep(520, 700, .1, .45)
    elif emotion == "smile":      # soft warm major third, quiet
        s = _sweep(587, 587, .13, .32) + _sweep(740, 740, .22, .3)
    elif emotion.startswith("tilt"):
        # a small questioning lilt; the big versions get a wider, louder bend
        big = emotion.endswith("_big")
        s = _sweep(620, 780 if big else 700, .18 if big else .12,
                   .45 if big else .3, vibrato=14 if big else 0)
    elif emotion == "shake":      # descending "nuh-uh"
        s = _sweep(500, 380, .11, .45) + _silence(.05) + _sweep(420, 300, .13, .45)
    else:                        # smug — two low deadpan blips
        s = _sweep(330, 320, .1, .45) + _silence(.09) + _sweep(280, 270, .14, .45)
    return _to_wav(s)


_uploaded: set[str] = set()
_upload_lock = threading.Lock()


def _sound_name(emotion: str) -> str:
    return f"wonder_sfx_{emotion}.wav"


def _ensure_sound(emotion: str) -> bool:
    with _upload_lock:
        if emotion in _uploaded:
            return True
        wav = _chirp(emotion)
        boundary = f"----emote{uuid.uuid4().hex}"
        name = _sound_name(emotion)
        payload = ((f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="file"; filename="{name}"\r\n'
                    f"Content-Type: audio/wav\r\n\r\n").encode()
                   + wav + f"\r\n--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            f"{REACHY_URL}/api/media/sounds/upload", data=payload, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
            _uploaded.add(emotion)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[emote] sfx upload failed: {e}", flush=True)
            return False


# --------------------------------------------------------------------------- #
# The 5 choreographies                                                        #
# --------------------------------------------------------------------------- #
def _do_happy():
    _goto(_pose(roll=0.15), [0.9, -0.9], 0.25); time.sleep(0.27)
    _goto(_pose(roll=-0.15), [0.7, -0.7], 0.25); time.sleep(0.27)
    _goto(_pose(roll=0.12), [0.9, -0.9], 0.22); time.sleep(0.24)
    _goto(NEUTRAL, [0.2, -0.2], 0.3)


def _do_excited():
    for _ in range(2):
        _goto(_pose(pitch=-0.25, z=0.01), [1.2, -1.2], 0.18); time.sleep(0.2)
        _goto(_pose(pitch=0.1), [0.5, -0.5], 0.18); time.sleep(0.2)
    _goto(NEUTRAL, [0.3, -0.3], 0.25)


def _do_curious():
    _goto(_pose(roll=0.35, pitch=-0.1), [1.0, 0.0], 0.5); time.sleep(0.9)
    _goto(_pose(roll=0.3, pitch=-0.15), [1.0, -0.3], 0.3); time.sleep(0.5)
    _goto(NEUTRAL, [0.0, 0.0], 0.4)


def _do_sad():
    _goto(_pose(pitch=0.35, z=-0.01), [-0.8, 0.8], 1.1); time.sleep(1.4)
    _goto(_pose(pitch=0.3), [-1.0, 1.0], 0.8); time.sleep(1.0)
    _goto(NEUTRAL, [0.0, 0.0], 0.9)


def _do_thinking():
    _goto(_pose(roll=0.38, pitch=-0.08), [0.9, 0.1], 0.6); time.sleep(1.0)
    _goto(_pose(roll=0.42, pitch=-0.1), [1.0, 0.05], 0.4); time.sleep(0.5)
    _goto(_pose(roll=0.35, pitch=-0.06), [0.85, 0.15], 0.35); time.sleep(0.6)
    _goto(NEUTRAL, [0.2, -0.2], 0.5)


def _do_victory():
    for _ in range(3):
        _goto(_pose(pitch=-0.28, z=0.015), [1.3, -1.3], 0.14); time.sleep(0.16)
        _goto(_pose(pitch=0.08), [0.3, -0.3], 0.14); time.sleep(0.16)
    _goto(_pose(roll=0.2, pitch=-0.2), [1.4, -0.2], 0.2); time.sleep(0.28)
    _goto(_pose(roll=-0.2, pitch=-0.2), [0.2, -1.4], 0.2); time.sleep(0.28)
    _goto(_pose(pitch=-0.3), [1.4, -1.4], 0.2); time.sleep(0.35)
    _goto(NEUTRAL, [0.3, -0.3], 0.35)


def _do_smug():
    _goto(_pose(yaw=0.5, pitch=-0.12), [0.6, 0.0], 0.6); time.sleep(1.0)
    _goto(_pose(yaw=0.45, pitch=-0.15), [0.75, 0.0], 0.3); time.sleep(0.6)
    _goto(NEUTRAL, [0.0, 0.0], 0.5)


def _do_wave():
    """Waving back, with no arms to wave. Both antennas swing the same
    direction (in phase — unlike the happy/excited moves, where they mirror)
    so they read as one hand rocking side to side, and the head leans into
    each swing the way a person's does."""
    _goto(_pose(roll=-0.2, pitch=-0.12), [1.2, 1.2], 0.28); time.sleep(0.3)
    for _ in range(2):
        _goto(_pose(roll=0.22, pitch=-0.12), [-1.0, -1.0], 0.26); time.sleep(0.28)
        _goto(_pose(roll=-0.22, pitch=-0.12), [1.2, 1.2], 0.26); time.sleep(0.28)
    _goto(_pose(pitch=-0.08), [0.4, 0.4], 0.3); time.sleep(0.32)
    _goto(NEUTRAL, [0.0, 0.0], 0.35)


def _ANT(right: float, left: float) -> list[float]:
    """Antenna vector in the daemon's order. The SDK joint list names
    `right_antenna` before `left_antenna`, so index 0 is Vibey's RIGHT."""
    return [right, left]


def _wave_one(side: str):
    """Wave with ONE antenna, the other held still — so the wave has a side.

    Used to wave back at a person mirror-wise: they raise their left hand,
    Vibey answers with the antenna on that same side of the shared space,
    which is Vibey's right. The head leans toward the waving antenna the way
    you tilt toward the hand you're waving with."""
    lean = 0.18 if side == "right" else -0.18
    for i in range(3):
        up = 1.15 if i < 2 else 0.5
        a = _ANT(up, 0.0) if side == "right" else _ANT(0.0, up)
        b = _ANT(-0.5, 0.0) if side == "right" else _ANT(0.0, -0.5)
        _goto(_pose(roll=lean, pitch=-0.12), a, 0.24); time.sleep(0.26)
        _goto(_pose(roll=lean * 0.4, pitch=-0.12), b, 0.22); time.sleep(0.24)
    _goto(NEUTRAL, _ANT(0.0, 0.0), 0.35)


def _do_wave_right():
    """Vibey's RIGHT antenna — answers a person's LEFT hand."""
    _wave_one("right")


def _do_wave_left():
    """Vibey's LEFT antenna — answers a person's RIGHT hand."""
    _wave_one("left")


def _do_peace():
    """Answer to a peace sign: both antennas snap up into a V and hold,
    with a pleased little double-bounce."""
    _goto(_pose(pitch=-0.16), _ANT(1.25, -1.25), 0.26); time.sleep(0.32)
    for _ in range(2):
        _goto(_pose(pitch=-0.10), _ANT(1.0, -1.0), 0.16); time.sleep(0.18)
        _goto(_pose(pitch=-0.18), _ANT(1.3, -1.3), 0.16); time.sleep(0.18)
    time.sleep(0.25)
    _goto(NEUTRAL, _ANT(0.0, 0.0), 0.4)


def _do_smile():
    """A warm smile — the calm cousin of `happy`.

    Where happy wiggles, this one just beams: the head lifts and settles into
    a soft tilt while both antennas curve up together and HOLD there, with one
    small pleased bob. Slow and steady, so it reads as friendly rather than
    excited, and it's short enough to fire under a positive sentence."""
    _goto(_pose(pitch=-0.12, z=0.008), _ANT(0.95, -0.95), 0.45); time.sleep(0.5)
    _goto(_pose(roll=0.10, pitch=-0.14), _ANT(1.1, -1.1), 0.3); time.sleep(0.38)
    _goto(_pose(roll=0.08, pitch=-0.10), _ANT(0.9, -0.9), 0.22); time.sleep(0.28)
    _goto(_pose(roll=0.10, pitch=-0.14), _ANT(1.1, -1.1), 0.22); time.sleep(0.45)
    _goto(NEUTRAL, _ANT(0.25, -0.25), 0.5)


def _tilt(side: str, big: bool):
    """Head tilt toward one side. Positive roll leans toward Vibey's RIGHT,
    matching the lean in `_wave_one`, so left/right stay consistent everywhere.

    Subtle: a quick lean-and-hold you can drop under a sentence without
    stealing attention. Big: a deep floppy tilt with a pitch drop and the
    antennas spilling the same way, held long enough to read as a bit."""
    sign = 1.0 if side == "right" else -1.0
    roll = 0.5 if big else 0.18
    pitch = -0.12 if big else -0.05
    # antennas flop the same direction as the tilt (in phase), like hair
    ant = _ANT(sign * (1.1 if big else 0.5), sign * (1.1 if big else 0.5))
    _goto(_pose(roll=sign * roll, pitch=pitch), ant, 0.45 if big else 0.3)
    time.sleep(0.6 if big else 0.38)
    if big:
        # one extra lean, deeper, so the exaggerated version has a punchline
        _goto(_pose(roll=sign * 0.6, pitch=-0.16), ant, 0.25); time.sleep(0.55)
    _goto(NEUTRAL, _ANT(0.0, 0.0), 0.45 if big else 0.35)


def _do_tilt_left():
    _tilt("left", big=False)


def _do_tilt_right():
    _tilt("right", big=False)


def _do_tilt_left_big():
    _tilt("left", big=True)


def _do_tilt_right_big():
    _tilt("right", big=True)


def _do_nod():
    for _ in range(2):
        _goto(_pose(pitch=0.22), [0.5, -0.5], 0.18); time.sleep(0.2)
        _goto(_pose(pitch=-0.1), [0.7, -0.7], 0.18); time.sleep(0.2)
    _goto(NEUTRAL, [0.2, -0.2], 0.25)


def _do_shake():
    for _ in range(2):
        _goto(_pose(yaw=0.3), [-0.3, 0.3], 0.2); time.sleep(0.22)
        _goto(_pose(yaw=-0.3), [-0.3, 0.3], 0.2); time.sleep(0.22)
    _goto(NEUTRAL, [0.0, 0.0], 0.3)



# --------------------------------------------------------------------------- #
# Expressions added from the mime_bot mapping (RemiFabre/mime_bot).
#
# Its calibration is the useful part: antennas live in roughly ±1.05 rad with a
# ±0.17 idle bias, brows-up reads at ≈±1.0, and a frown crosses them INWARD past
# zero rather than just relaxing. Head roll/pitch stay inside ±0.7 rad. Those
# numbers are what make a pose read as a face rather than as a servo moving.
# --------------------------------------------------------------------------- #

def _do_laugh():
    """Head thrown back, antennas shaking on each beat.

    The shake is deliberately uneven — four bursts of decreasing size rather
    than a regular oscillation. A metronome reads as a machine vibrating; a
    laugh runs out of breath.
    """
    for amp, dwell in ((1.05, 0.13), (0.95, 0.12), (0.8, 0.13), (0.6, 0.15)):
        _goto(_pose(pitch=-0.32, roll=0.06, z=0.012), [amp, -amp], 0.11)
        time.sleep(dwell)
        _goto(_pose(pitch=-0.22, roll=-0.05), [amp * 0.45, -amp * 0.45], 0.11)
        time.sleep(dwell)
    _goto(_pose(pitch=-0.08), [0.35, -0.35], 0.3); time.sleep(0.3)
    _goto(NEUTRAL, [0.2, -0.2], 0.35)


def _do_laugh_wheeze():
    """The silent one. Head goes back and STAYS back — no recovery between
    beats — while the antennas stutter in tiny high-frequency gasps.

    The whole read is that nothing big is moving. A laugh you can't get a
    breath out of is small and fast, so the head holds its position and only
    the antennas twitch; the moment the neck starts swinging it turns back
    into the belly laugh.
    """
    _goto(_pose(pitch=-0.34, roll=0.03), [0.75, -0.75], 0.18)
    time.sleep(0.2)
    for amp in (0.85, 0.7, 0.8, 0.6, 0.7, 0.45, 0.55, 0.3):
        _goto(_pose(pitch=-0.34, roll=0.03, z=0.008), [amp, -amp], 0.06)
        time.sleep(0.075)
    # the breath finally arrives — one slow sag back to level
    _goto(_pose(pitch=-0.2), [0.3, -0.3], 0.4); time.sleep(0.4)
    _goto(NEUTRAL, [0.15, -0.15], 0.45)


def _do_laugh_cackle():
    """The unhinged one. Head thrown back and rolling side to side, antennas
    swinging out of phase, and the whole thing ACCELERATES.

    Out of phase is what separates it from the belly laugh: there both
    antennas mirror each other, which is tidy. Here one leads the other by
    half a beat, so the head reads as being shaken by the laugh rather than
    performing it. The speed-up is the punchline — it should sound like it is
    getting away from itself.
    """
    beats = ((0.22, 0.16), (-0.26, 0.14), (0.3, 0.12), (-0.32, 0.1),
             (0.34, 0.09), (-0.3, 0.085))
    lead = 1.0
    for roll, dwell in beats:
        _goto(_pose(pitch=-0.3, roll=roll, yaw=roll * 0.5, z=0.014),
              [lead, -lead * 0.35], 0.08)
        time.sleep(dwell)
        lead = min(1.1, lead + 0.06)
    # tips over the top and has to collect itself
    _goto(_pose(pitch=-0.36, roll=0.0), [1.05, -1.05], 0.12); time.sleep(0.22)
    _goto(_pose(pitch=0.12, roll=-0.04), [0.25, -0.25], 0.45); time.sleep(0.45)
    _goto(NEUTRAL, [0.15, -0.15], 0.4)


# The three laughs, in the order they get picked from. `laugh` is the belly
# laugh, and it stays the default for anything that just asks to "laugh".
LAUGHS = ("laugh", "laugh_wheeze", "laugh_cackle")
_last_laugh: str | None = None


def laugh_any(sound: bool = True) -> str:
    """Play one of the three laughs, never the same one twice in a row.

    Never-twice matters more than true randomness here: hearing the identical
    wheeze back to back is the moment it stops reading as a reaction and
    starts reading as a sound file.
    """
    global _last_laugh
    choices = [l for l in LAUGHS if l != _last_laugh] or list(LAUGHS)
    pick = random.choice(choices)
    _last_laugh = pick
    play(pick, sound=sound)
    return pick


def _do_appalled():
    """Recoil, then a slow disbelieving return. Brows up and HELD — the hold is
    the whole gesture, since a fast recovery reads as a flinch instead."""
    _goto(_pose(pitch=0.18, yaw=-0.12, x=-0.02), [1.05, -1.05], 0.16)
    time.sleep(0.55)
    _goto(_pose(pitch=0.14, yaw=0.1, x=-0.015), [1.0, -1.0], 0.5)
    time.sleep(0.7)
    _goto(_pose(pitch=0.05), [0.7, -0.7], 0.6); time.sleep(0.4)
    _goto(NEUTRAL, [0.2, -0.2], 0.5)


def _do_confused():
    """Tilt one way, antennas asymmetric, then tilt the other. The asymmetry is
    mime_bot's brow_asym term: one antenna up and one down reads as a raised
    eyebrow, which is most of what confusion looks like."""
    _goto(_pose(roll=0.4, pitch=-0.08), [1.0, -0.15], 0.4); time.sleep(0.7)
    _goto(_pose(roll=-0.35, pitch=-0.05), [-0.15, 1.0], 0.45); time.sleep(0.7)
    _goto(NEUTRAL, [0.2, -0.2], 0.4)


def _do_frown():
    """Antennas crossed inward past zero — mime_bot measured browDown landing at
    ∓10°, i.e. the opposite side of neutral, not merely lowered."""
    _goto(_pose(pitch=0.16, roll=-0.04), [-0.2, 0.2], 0.45); time.sleep(0.8)
    _goto(NEUTRAL, [0.2, -0.2], 0.5)


def _do_surprised():
    _goto(_pose(pitch=-0.2, z=0.015, x=-0.015), [1.05, -1.05], 0.13)
    time.sleep(0.45)
    _goto(_pose(pitch=-0.05), [0.8, -0.8], 0.35); time.sleep(0.25)
    _goto(NEUTRAL, [0.2, -0.2], 0.4)


def _do_no_no_no():
    """Three fast shakes with the antennas swinging against the head — a bigger,
    more emphatic 'no' than the plain `shake`."""
    for i in range(3):
        _goto(_pose(yaw=0.34, roll=-0.06), [0.15, -0.85], 0.14); time.sleep(0.15)
        _goto(_pose(yaw=-0.34, roll=0.06), [-0.85, 0.15], 0.14); time.sleep(0.15)
    _goto(NEUTRAL, [0.2, -0.2], 0.3)


def _do_yes_yes_yes():
    for _ in range(3):
        _goto(_pose(pitch=0.26), [0.7, -0.7], 0.13); time.sleep(0.14)
        _goto(_pose(pitch=-0.16), [0.9, -0.9], 0.13); time.sleep(0.14)
    _goto(NEUTRAL, [0.2, -0.2], 0.3)


def _do_shy():
    """Look down and away, antennas dropped. Slow on purpose."""
    _goto(_pose(pitch=0.28, yaw=0.3, roll=0.12), [0.05, -0.5], 0.7)
    time.sleep(0.9)
    _goto(_pose(pitch=0.2, yaw=0.18), [0.15, -0.35], 0.5); time.sleep(0.5)
    _goto(NEUTRAL, [0.2, -0.2], 0.6)


def _do_wink():
    """One antenna down, a small head tilt. The asymmetry does the work."""
    _goto(_pose(roll=0.14), [0.85, -0.85], 0.2); time.sleep(0.22)
    _goto(_pose(roll=0.16), [0.9, 0.15], 0.14); time.sleep(0.3)
    _goto(NEUTRAL, [0.2, -0.2], 0.3)


def _do_shrug():
    """A gentle 'who knows' — antennas rise together and drop, with a small
    rock from side to side. No head lift at the end: a shrug that recovers
    briskly reads as a nod, so it settles back down slightly deflated."""
    _goto(_pose(pitch=0.1, roll=0.12), _ANT(0.9, -0.9), 0.3); time.sleep(0.35)
    _goto(_pose(pitch=0.12, roll=-0.12), _ANT(0.95, -0.95), 0.3); time.sleep(0.4)
    _goto(_pose(pitch=0.18), _ANT(0.15, -0.15), 0.35); time.sleep(0.35)
    _goto(NEUTRAL, _ANT(0.2, -0.2), 0.45)


def _do_shy_nod():
    """Agreement from a robot that would rather not be looked at: a small nod
    made while already turned away and low, antennas half-dropped. The nod is
    shallow on purpose — a full-amplitude one cancels the shyness."""
    _goto(_pose(pitch=0.24, yaw=0.22, roll=0.1), _ANT(0.1, -0.45), 0.5)
    time.sleep(0.45)
    for _ in range(2):
        _goto(_pose(pitch=0.32, yaw=0.22, roll=0.1), _ANT(0.05, -0.4), 0.2)
        time.sleep(0.22)
        _goto(_pose(pitch=0.2, yaw=0.22, roll=0.1), _ANT(0.15, -0.5), 0.2)
        time.sleep(0.22)
    _goto(NEUTRAL, _ANT(0.2, -0.2), 0.55)


def _do_whistle():
    # chin up and a little sway, antennas perked, in time with the tune
    _goto(_pose(pitch=-0.18, roll=0.08), _ANT(0.5, 0.5), 0.3)
    time.sleep(0.8)
    for r in (0.12, -0.12, 0.1):
        _goto(_pose(pitch=-0.12, roll=r), _ANT(0.35, 0.35), 0.35)
        time.sleep(0.36)
    _goto(NEUTRAL, _ANT(0.2, -0.2), 0.5)


_MOVES = {"happy": _do_happy, "whistle": _do_whistle, "excited": _do_excited, "curious": _do_curious,
          "sad": _do_sad, "smug": _do_smug, "thinking": _do_thinking,
          "victory": _do_victory, "wave": _do_wave, "nod": _do_nod,
          "shake": _do_shake, "wave_left": _do_wave_left,
          "wave_right": _do_wave_right, "peace": _do_peace,
          "smile": _do_smile,
          "tilt_left": _do_tilt_left, "tilt_right": _do_tilt_right,
          "tilt_left_big": _do_tilt_left_big,
          "tilt_right_big": _do_tilt_right_big,
          # dance is defined further down; the lambda defers the lookup so the
          # name is callable from the voice brain like any other emote.
          "laugh": _do_laugh, "laugh_wheeze": _do_laugh_wheeze,
          "laugh_cackle": _do_laugh_cackle, "appalled": _do_appalled,
          "confused": _do_confused, "frown": _do_frown,
          "surprised": _do_surprised, "no_no_no": _do_no_no_no,
          "yes_yes_yes": _do_yes_yes_yes, "shy": _do_shy, "wink": _do_wink,
          "shrug": _do_shrug, "shy_nod": _do_shy_nod,
          # not an emote — a self-test, but callable by name like one
          "antenna_check": _do_antenna_check,
          "left_antenna_check": _do_left_antenna_check,
          "right_antenna_check": _do_right_antenna_check,
          # dance is defined further down; the lambda defers the lookup so the
          # name is callable from the voice brain like any other emote.
          "dance": lambda: dance()}


def play(emotion: str, sound: bool = False) -> bool:
    """Fire an emote (non-blocking). Returns False for unknown emotions."""
    move = _MOVES.get(emotion)
    if not move:
        return False

    def _run():
        # Hold the idle drift off the neck for the duration. Without this the
        # breathing loop's next step lands mid-gesture and drags the head
        # somewhere the emote did not ask for — which reads as the robot
        # losing its nerve halfway through.
        try:
            import reachy_idle
            reachy_idle.pause()
        except Exception:  # noqa: BLE001
            reachy_idle = None
        # Same deal for the talking motion, which is a second writer on the
        # neck now. An emote fired mid-sentence should own the head outright —
        # otherwise the two blend into a gesture that never quite arrives.
        try:
            import reachy_talk
            reachy_talk.pause()
        except Exception:  # noqa: BLE001
            reachy_talk = None
        # dance brings its own backing beat — a chirp here would cut it off
        if sound and emotion != "dance" and _ensure_sound(emotion):
            _post("/api/media/play_sound", {"file": _sound_name(emotion)})
        try:
            move()
        except Exception as e:  # noqa: BLE001
            print(f"[emote] {emotion} failed: {e}", flush=True)
        finally:
            if reachy_idle is not None:
                reachy_idle.resume()
            if reachy_talk is not None:
                reachy_talk.resume()

    threading.Thread(target=_run, daemon=True).start()
    return True


# --------------------------------------------------------------------------- #
# Dance mode — a synthesized beat + a looping full-body groove.               #
# --------------------------------------------------------------------------- #
def _beat_track(seconds: float = 12.0, bpm: int = 118) -> bytes:
    """A tiny synthesized dance beat: sine-kick four-on-the-floor with an
    off-beat blip. No samples, no downloads — pure math, very robot."""
    spb = 60.0 / bpm
    total = int(SR * seconds)
    buf = [0.0] * total
    t = 0.0
    beat_i = 0
    while t < seconds:
        start = int(t * SR)
        # kick: 150→50 Hz thump
        for i, s in enumerate(_sweep(150, 50, 0.11, 0.85)):
            j = start + i
            if j < total:
                buf[j] += s
        # off-beat blip every other beat
        if beat_i % 2 == 1:
            for i, s in enumerate(_sweep(880, 860, 0.04, 0.18)):
                j = start + int(SR * spb / 2) + i
                if j < total:
                    buf[j] += s
        t += spb
        beat_i += 1
    return _to_wav([max(-1, min(1, s)) for s in buf])


def dance(seconds: float = 12.0, bpm: int = 118) -> None:
    """Dance mode: play the beat on the robot and groove until it ends.
    Blocking — call from a thread (play(..) style) if you need async."""
    name = "wonder_sfx_beat.wav"
    with _upload_lock:
        if "beat" not in _uploaded:
            wav = _beat_track(seconds, bpm)
            boundary = f"----emote{uuid.uuid4().hex}"
            payload = ((f"--{boundary}\r\nContent-Disposition: form-data; "
                        f'name="file"; filename="{name}"\r\n'
                        f"Content-Type: audio/wav\r\n\r\n").encode()
                       + wav + f"\r\n--{boundary}--\r\n".encode())
            req = urllib.request.Request(
                f"{REACHY_URL}/api/media/sounds/upload", data=payload,
                method="POST",
                headers={"Content-Type":
                         f"multipart/form-data; boundary={boundary}"})
            urllib.request.urlopen(req, timeout=15).read()
            _uploaded.add("beat")
    _post("/api/media/play_sound", {"file": name})

    spb = 60.0 / bpm
    end = time.time() + seconds
    moves = [
        lambda: _goto(_pose(roll=0.22, pitch=0.1), [0.9, -0.4], spb * 0.9),
        lambda: _goto(_pose(roll=-0.22, pitch=-0.08), [-0.4, 0.9], spb * 0.9),
        lambda: _goto(_pose(pitch=0.18, yaw=0.25), [1.0, -1.0], spb * 0.9),
        lambda: _goto(_pose(pitch=-0.15, yaw=-0.25), [0.5, 0.5], spb * 0.9),
    ]
    i = 0
    while time.time() < end:
        moves[i % len(moves)]()
        time.sleep(spb)
        i += 1
    _goto(NEUTRAL, [0.2, -0.2], 0.6)


def play_dance(seconds: float = 12.0) -> None:
    """Non-blocking dance mode."""
    threading.Thread(target=dance, args=(seconds,), daemon=True).start()


# --------------------------------------------------------------------------- #
# Karaoke mode — an original synthesized backing track with a call-and-
# response structure. Lyrics come from the caller (usually haiku-written on
# the spot); Vibey performs verse 1, hands verse 2 to the human, then a big
# shared chorus and a victory finish.
# --------------------------------------------------------------------------- #
def _backing_track(seconds: float = 40.0, bpm: int = 100) -> bytes:
    """Kick + bassline + sparkly arpeggio, loops until `seconds`."""
    spb = 60.0 / bpm
    total = int(SR * seconds)
    buf = [0.0] * total

    def mix(samples, at_s):
        start = int(at_s * SR)
        for i, s in enumerate(samples):
            j = start + i
            if j < total:
                buf[j] += s

    bass_line = [131, 131, 165, 196]           # C3 C3 E3 G3
    arp = [523, 659, 784, 1046]                # C5 E5 G5 C6
    t = 0.0
    bar = 0
    while t < seconds:
        mix(_sweep(150, 48, 0.12, 0.8), t)                       # kick
        mix(_sweep(bass_line[bar % 4], bass_line[bar % 4] * 0.99,
                   spb * 0.9, 0.28), t)                          # bass
        if bar % 2 == 1:
            mix(_sweep(900, 880, 0.05, 0.12), t + spb / 2)       # offbeat tick
        for k, f in enumerate(arp):
            mix(_sweep(f, f, 0.09, 0.10), t + k * spb / 4)       # sparkle
        t += spb
        bar += 1
    return _to_wav([max(-1, min(1, s)) for s in buf])


def start_karaoke_track(seconds: float = 40.0) -> float:
    """Upload (once) + start the backing track. Returns its duration."""
    name = "wonder_sfx_karaoke.wav"
    with _upload_lock:
        if "karaoke" not in _uploaded:
            wav = _backing_track(seconds)
            boundary = f"----emote{uuid.uuid4().hex}"
            payload = ((f"--{boundary}\r\nContent-Disposition: form-data; "
                        f'name="file"; filename="{name}"\r\n'
                        f"Content-Type: audio/wav\r\n\r\n").encode()
                       + wav + f"\r\n--{boundary}--\r\n".encode())
            req = urllib.request.Request(
                f"{REACHY_URL}/api/media/sounds/upload", data=payload,
                method="POST",
                headers={"Content-Type":
                         f"multipart/form-data; boundary={boundary}"})
            urllib.request.urlopen(req, timeout=20).read()
            _uploaded.add("karaoke")
    _post("/api/media/play_sound", {"file": name})
    return seconds


if __name__ == "__main__":
    import sys
    emotion = sys.argv[1] if len(sys.argv) > 1 else "happy"
    if emotion == "dance":
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
        print(f"[emote] dance mode for {secs:.0f}s 🕺")
        dance(secs)
        sys.exit(0)
    if emotion not in _MOVES:
        print(f"unknown emotion {emotion!r}; pick from {EMOTIONS} or 'dance'")
        sys.exit(1)
    print(f"[emote] playing {emotion} (with sound)")
    play(emotion, sound=True)
    time.sleep(5)  # let the daemon thread finish before the CLI exits
