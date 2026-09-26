#!/usr/bin/env python3
"""
reachy_talk.py — the head moving *while* Vibey talks.

The gap this fills: Vibey had exactly two kinds of motion, and neither of them
happens during a sentence.

    reachy_idle    ±0.05 rad of breathing. Real, but invisible.
    reachy_emotes  big, punctual, and fired only when the model decides to
                   call `move` — which lands at tool-call moments, not across
                   the utterance.

Speech is continuous and gestures were punctual, so motion was uncorrelated
with what was being said. That is the precise definition of "sporadic": not too
little movement, movement in the wrong *places*.

This is the missing middle layer. While a reply is playing, the head moves with
the audio — leaning into emphasis, settling in the pauses — the way a person's
does when they are making a point.

IT USES THE REAL AUDIO. The realtime session accumulates the whole reply as PCM
before playing it (see `_flush_reply`), so the envelope is known up front rather
than guessed. Motion is scheduled against wall-clock from the moment playback
starts, so it stays in sync without needing a feedback signal from the speaker.

LAYERING. One writer on the neck at a time, the rule the rest of this repo
learned the hard way:

    emote  >  talk  >  idle

`start()` pauses the idle breathing; `stop()` resumes it. Emotes call `pause()`
here for their duration exactly as they already do for idle, so a wave fired
mid-sentence owns the head and the talking motion picks up after.

AMPLITUDES sit deliberately between the two neighbours — bigger than breathing
so it reads across a room, smaller than an emote so a gesture still punctuates.

    import reachy_talk
    reachy_talk.start(pcm_bytes, sample_rate=24000, duration=3.2)
    reachy_talk.stop()

Env:
    TALK_MOTION     "0" to disable entirely
    TALK_GAIN       overall amplitude multiplier (default 1.0)
"""

from __future__ import annotations

import array
import math
import os
import random
import threading
import time

from reachy_emotes import _goto, NEUTRAL

ENABLED = os.environ.get("TALK_MOTION", "1").strip() != "0"
GAIN = float(os.environ.get("TALK_GAIN", "1.0"))

# One pose every this often. Chosen against the round-trip: ~90ms to the robot
# on a literal IP, so 0.25s leaves comfortable headroom and still gives ~4
# poses a second, which is enough to track syllable-scale emphasis. Faster than
# this just floods the daemon for motion nobody can see.
STEP_S = 0.25

# Ceilings, radians. Breathing is 0.05 and an emote rolls 0.36 (0.15 x the 2.4
# head gain), so this sits between them on purpose.
# The nod axis carries most of the emphasis and is also the one the servo
# smooths hardest, because it oscillates fastest — measured travel came back at
# a third of the commanded amplitude. Set high to land right after smoothing.
PITCH_A = 0.20
ROLL_A = 0.11           # the lean
YAW_A = 0.15            # the drift, slowest of the three

# Envelope frames shorter than this are treated as silence rather than a very
# quiet syllable, so the head settles in pauses instead of jittering.
FLOOR = 0.06

_state = {"on": False, "paused": 0, "thread": None, "seq": 0}
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Envelope                                                                    #
# --------------------------------------------------------------------------- #
def envelope(pcm: bytes, sample_rate: int, step_s: float = STEP_S) -> list[float]:
    """Per-step loudness of a reply, normalised to 0..1.

    RMS rather than peak: peak tracks the loudest click in a window and makes a
    consonant look like a shout, which reads as a twitch. RMS follows the shape
    of the voice.
    """
    if not pcm:
        return []
    try:
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
    except Exception:  # noqa: BLE001
        return []
    per = max(1, int(sample_rate * step_s))
    out: list[float] = []
    for i in range(0, len(samples), per):
        w = samples[i:i + per]
        if not w:
            break
        out.append(math.sqrt(sum(float(s) * s for s in w) / len(w)))
    if not out:
        return []
    top = max(out) or 1.0
    # Normalise to this reply's own loudest moment, not to full scale. Vibey's
    # replies vary in level and the motion should read the same either way.
    return [min(1.0, v / top) for v in out]


# --------------------------------------------------------------------------- #
# Motion                                                                      #
# --------------------------------------------------------------------------- #
def _step(level: float, t: float, lean: float) -> None:
    """One pose. `level` is 0..1 loudness now, `t` seconds since speech began.

    Two things are summed: the envelope, which supplies the emphasis, and a
    slow continuous drift, which supplies the aliveness. Envelope alone reads
    as a machine reacting to volume — people move between words too, not only
    on them.
    """
    if level < FLOOR:
        level = 0.0
    # TIMESCALES ARE THE WHOLE TRICK. These were first written with the idle
    # loop's periods (7s, 11s) and the motion nearly vanished: a reply is 3-8
    # seconds, so a 14-second sine never completes a cycle inside one and reads
    # as a fixed offset rather than movement. Everything here oscillates fast
    # enough to go somewhere and come back *within a sentence*.
    #
    # Emphasis on the nod axis. Oscillates through zero rather than sitting at
    # a permanent chin-up, so there is real travel: a person nods while making
    # a point, they don't just tilt back and hold it.
    pitch = -PITCH_A * (0.45 * math.sin(t / 0.44) + 0.55 * level)
    # Lean, ~2.8s period — about one sway per clause.
    roll = ROLL_A * (0.6 * math.sin(t / 0.45) + 0.4 * lean * level)
    # Drift, ~4.5s — the slowest of the three, and the only one indifferent to
    # the audio, so the head keeps moving through a quiet passage.
    yaw = YAW_A * math.sin(t / 0.72 + lean)
    # Only slightly longer than the step, so each pose very nearly arrives
    # before the next supersedes it. Much longer and the moves blend into a
    # smooth average of themselves, which is how the motion got attenuated to
    # nothing on the first attempt.
    _goto(dict(NEUTRAL, pitch=pitch * GAIN, roll=roll * GAIN, yaw=yaw * GAIN),
          None, STEP_S * 1.15)


def _run(env: list[float], duration: float, seq: int) -> None:
    t0 = time.time()
    lean = random.uniform(-1.0, 1.0)
    try:
        import reachy_idle
        reachy_idle.pause()
    except Exception:  # noqa: BLE001
        reachy_idle = None
    try:
        while True:
            t = time.time() - t0
            # A newer reply has started, or stop() was called: drop this one
            # without touching the neck, so the new writer owns it cleanly.
            if _state["seq"] != seq or not _state["on"] or t >= duration:
                break
            if _state["paused"] <= 0:
                i = min(len(env) - 1, int(t / STEP_S)) if env else -1
                try:
                    _step(env[i] if i >= 0 else 0.4, t, lean)
                except Exception:  # noqa: BLE001 — a dropped pose is not an event
                    pass
            time.sleep(STEP_S)
    finally:
        # Settle, but not to dead centre: an exact NEUTRAL every time is what
        # makes the gaps between gestures read as "performance over, back to
        # statue". A small residual lean keeps it looking inhabited, and the
        # idle breathing takes it from here.
        if _state["seq"] == seq:
            try:
                _goto(dict(NEUTRAL, pitch=-0.02, roll=0.03 * lean,
                           yaw=0.04 * lean), None, 0.5)
            except Exception:  # noqa: BLE001
                pass
            _state["on"] = False
        if reachy_idle is not None:
            reachy_idle.resume()


def start(pcm: bytes | None = None, sample_rate: int = 24000,
          duration: float = 0.0) -> bool:
    """Begin talking motion for a reply that is starting to play now.

    Call it immediately before or after handing the audio to the speaker — the
    schedule is wall-clock from this moment.
    """
    if not ENABLED or duration <= 0.2:
        return False
    env = envelope(pcm, sample_rate) if pcm else []
    with _lock:
        _state["seq"] += 1
        seq = _state["seq"]
        _state["on"] = True
        _state["paused"] = 0
        _state["thread"] = threading.Thread(
            target=_run, args=(env, duration, seq), daemon=True)
        _state["thread"].start()
    return True


def stop() -> None:
    """Cut it short — barge-in, or the reply was cancelled."""
    with _lock:
        _state["on"] = False
        _state["seq"] += 1


def pause() -> None:
    """Yield the neck to an emote. Counted, like reachy_idle's."""
    with _lock:
        _state["paused"] += 1


def resume() -> None:
    with _lock:
        _state["paused"] = max(0, _state["paused"] - 1)


def status() -> dict:
    return {"on": _state["on"], "paused": _state["paused"], "enabled": ENABLED}
