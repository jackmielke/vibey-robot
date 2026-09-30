#!/usr/bin/env python3
"""
reachy_wakesleep.py — the body language of waking up and falling asleep.

Turning on and off should be something you can see from across a room. The
firmware ships both animations already (`/api/move/play/wake_up` and
`goto_sleep`), and goto_sleep puts Vibey's head down — which is the whole reason
this is worth doing rather than just opening a socket quietly.

Each is paired with a sound, for the same reason FlowState has them: the moments
that matter most are the ones with no visual feedback, and from across a room a
robot that heard you and a robot that did not look identical for the second
before it moves. Two notes have a direction — up is arriving, down is leaving —
and direction is what makes a sound mean something rather than merely happen.

The tones are synthesised here rather than shipped as files: two sine notes with
a raised-cosine fade are a few lines and no assets. The fade is the whole job. A
tone that starts at full amplitude clicks, and a click sounds like a fault rather
than a design.
"""

from __future__ import annotations

import io
import json
import math
import os
import struct
import time
import urllib.request
import wave
from pathlib import Path

REACHY_URL = os.environ.get("REACHY_URL", "http://10.0.0.196:8000").rstrip("/")
SR = 24000

# E5 → A5. A rising fourth is the least ambiguous "I'm here" interval there is;
# leaving is deliberately the same interval backwards, so the pair reads as one
# idea rather than two unrelated beeps.
WAKE_NOTES = (659.25, 880.00)
SLEEP_NOTES = (880.00, 659.25)


def _tone(notes, note_length=0.10, level=0.28) -> bytes:
    """A two-note tone as 16-bit mono PCM."""
    per = int(note_length * SR)
    fade = max(1, min(per // 4, int(0.008 * SR)))
    out = []
    for freq in notes:
        phase = 0.0
        step = 2 * math.pi * freq / SR
        for i in range(per):
            if i < fade:
                env = 0.5 - 0.5 * math.cos(math.pi * i / fade)
            elif i >= per - fade:
                env = 0.5 - 0.5 * math.cos(math.pi * (per - 1 - i) / fade)
            else:
                env = 1.0
            out.append(int(max(-1.0, min(1.0, math.sin(phase) * env * level)) * 32767))
            phase += step
    return struct.pack(f"<{len(out)}h", *out)


def _wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return buf.getvalue()


def _post(path: str, payload=None, timeout=10):
    import json
    data = json.dumps(payload).encode() if payload is not None else b"{}"
    req = urllib.request.Request(f"{REACHY_URL}{path}", data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _upload_and_play(name: str, pcm: bytes, log=print):
    """Sounds are uploaded once and then played by name — the robot keeps them.

    Uses `reachy_voice`'s multipart builder rather than another hand-rolled one.
    The first version here rolled its own and got a 422 from the daemon for a body
    that looked identical; the shared one is known to work and is the only copy
    worth having.
    """
    try:
        from reachy_voice import _multipart
        payload, boundary = _multipart("file", name, _wav(pcm), "audio/wav")
        req = urllib.request.Request(
            f"{REACHY_URL}/api/media/sounds/upload", data=payload, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        urllib.request.urlopen(req, timeout=20).read()
        # The daemon calls the field `file`, not `sound`.
        _post("/api/media/play_sound", {"file": name})
    except Exception as e:  # noqa: BLE001
        # Never fatal. A robot that will not wake because a chime failed to upload
        # is worse than one that wakes silently.
        log(f"[wakesleep] sound {name} failed: {e}")


# How loud Vibey talks. The daemon boots at 70, which is fine in a quiet room and
# not enough in a room with people in it — and a social robot is, by definition,
# never in the quiet room. Every intermediate value has been tried and every one
# of them ended with somebody asking Vibey to speak up, so: full, every time.
WAKE_VOLUME = max(0, min(100, int(os.environ.get("VIBEY_VOLUME", "100"))))

# Two levels live in here, and conflating them is what made waking unpredictable.
#
#   "volume" — where the slider is right now. Moved by the dashboard, Telegram,
#              the voice brain. Changes all day.
#   "start"  — the level Vibey comes UP at, every wake. A deliberate setting,
#              changed only when somebody changes it.
#
# Waking used to restore "volume", so the level you ended a late-night session
# at was the level the robot greeted the room with the next morning — quiet
# when you wanted loud, and no way to see why. Now waking is a reset to "start",
# which is a number you can read off the dashboard before it happens.
# VIBEY_VOLUME seeds "start" the first time. The night clamp writes neither.
VOLUME_FILE = Path(__file__).resolve().parent / ".volume_state.json"


def _volume_state() -> dict:
    try:
        d = json.loads(VOLUME_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def saved_volume() -> int:
    """Where the slider was left."""
    try:
        return max(0, min(100, int(_volume_state()["volume"])))
    except Exception:  # noqa: BLE001
        return start_volume()


def start_volume() -> int:
    """What Vibey wakes up at, whatever last night left behind."""
    try:
        return max(0, min(100, int(_volume_state()["start"])))
    except Exception:  # noqa: BLE001
        return WAKE_VOLUME


def _write_volume(**fields) -> None:
    d = _volume_state()
    d.update({k: max(0, min(100, int(v))) for k, v in fields.items()})
    try:
        VOLUME_FILE.write_text(json.dumps(d))
    except Exception:  # noqa: BLE001
        pass


def remember_volume(level: int) -> None:
    _write_volume(volume=level)


def remember_start_volume(level: int) -> None:
    _write_volume(start=level)


# What the daemon last told us the volume is, so we can avoid setting it to the
# value it already has. Seeded from the saved level so the dashboard never
# shows a blank or 0 before the first set.
_last_volume = {"level": saved_volume()}


def set_volume(level: int, log=print, force: bool = False):
    """Set the speaker volume, and only when it actually needs setting.

    The daemon plays a test whistle on every volume change and pauses audio
    around it, so setting it to the value it already holds costs a whistle and a
    gap for nothing. Waking used to do exactly that every single time.
    """
    level = max(0, min(100, int(level)))
    try:
        if not force:
            with urllib.request.urlopen(f"{REACHY_URL}/api/volume/current",
                                        timeout=4) as r:
                current = json.loads(r.read() or b"{}").get("volume")
            _last_volume["level"] = current
            # A couple of points either way is not worth a whistle: the daemon
            # rounds, so asking for 100 and reading back 92 is normal.
            if current is not None and abs(int(current) - level) <= 8:
                return
        _post("/api/volume/set", {"volume": level})
        _last_volume["level"] = level
    except Exception as e:  # noqa: BLE001
        log(f"[wakesleep] volume failed: {e}")


def face_tracking(on: bool, weight: float = 0.6, log=print):
    """Follow whoever is in front of it.

    This is the thing that makes Vibey feel awake rather than merely switched on —
    it looks up when somebody walks in. Off while asleep, so a sleeping robot is
    not quietly moving its head at people.

    Weight 0.6 rather than 1.0: at full strength the head snaps to every detection
    and fights any gesture the model plays, which reads as twitchy rather than
    attentive. Following somebody is meant to look like interest, not like a
    servo.
    """
    try:
        if on:
            _post("/api/media/tracking/enable", {"weight": weight})
        else:
            _post("/api/media/tracking/disable")
    except Exception as e:  # noqa: BLE001
        log(f"[wakesleep] tracking failed: {e}")


def motors(enabled: bool, log=print):
    """Torque on or off.

    THIS is why Vibey would not move. Going to sleep leaves the motors disabled —
    correctly, a sleeping robot should not be holding its own head up — and
    nothing turned them back on. Every move after that returned 200 with a job id
    and did nothing at all, which is the worst way for a thing to fail: the API
    says yes and the body never twitches.
    """
    try:
        _post(f"/api/motors/set_mode/{'enabled' if enabled else 'disabled'}")
    except Exception as e:  # noqa: BLE001
        log(f"[wakesleep] motors {'on' if enabled else 'off'} failed: {e}")


def wake(log=print):
    """Torque on, stir, look up, and make the sound of arriving."""
    log("[wakesleep] waking")
    # First of all, or nothing below moves anything.
    motors(True, log)
    # force: skip the "close enough, don't whistle" check. Waking is exactly the
    # moment the old level should stop mattering — one whistle is cheaper than a
    # conversation held at whatever volume last night's clamp left behind.
    set_volume(start_volume(), log, force=True)
    _upload_and_play("vibey_wake.wav", _tone(WAKE_NOTES), log)
    try:
        _post("/api/move/play/wake_up", timeout=15)
    except Exception as e:  # noqa: BLE001
        log(f"[wakesleep] wake_up move failed: {e}")
    # After the animation, and after a beat.
    #
    # `wake_up` returns as soon as the move is QUEUED, not when it has finished
    # playing, so enabling tracking on the next line put a face-follower and an
    # animation on the same head at the same time. Together with whatever gesture
    # the model fires on its first turn, that is three things driving one neck —
    # which from the room looks like the robot spasming.
    time.sleep(1.6)
    face_tracking(True, log=log)


def sleep(log=print):
    """Head down, eyes off, and the same notes falling."""
    log("[wakesleep] going to sleep")
    # First, or it keeps chasing faces while trying to lie down.
    face_tracking(False, log=log)
    _upload_and_play("vibey_sleep.wav", _tone(SLEEP_NOTES, level=0.22), log)
    try:
        _post("/api/move/play/goto_sleep", timeout=15)
    except Exception as e:  # noqa: BLE001
        log(f"[wakesleep] goto_sleep move failed: {e}")


if __name__ == "__main__":
    import sys, time
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("wake", "both"):
        wake()
        time.sleep(4)
    if which in ("sleep", "both"):
        sleep()
