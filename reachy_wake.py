#!/usr/bin/env python3
"""
reachy_wake.py — how Vibey wakes up and goes to sleep.

Vibey asleep is a robot with its head down and no socket open. Waking it should
not mean walking to a laptop, so this listens to the robot's OWN microphone —
the same PCM bridge the realtime engine drinks from — and watches for two things:

    "hey vibey"   spoken, transcribed on this machine by faster-whisper
    clap-clap     two sharp claps, a quarter of a second apart, no model at all

Either one calls the callback. Nothing leaves the machine while it is asleep.

The clap detector is a port of the one in FlowState (~/dev/vibe-voice), rule for
rule, because getting it right took three attempts and the reasons are all
written down there. The rules, in the order they do work:

  1. It has to ARRIVE, not arise. A clap reaches its peak inside one 10 ms
     frame; a spoken syllable takes three to five however loud it ends up. This
     is the only rule that survives a processed microphone, and it is the
     strongest one here.
  2. It has to be short.
  3. It has to come out of quiet — EXCEPT when the thing before it was the first
     clap of the pair. Without that exception the second clap of every pair is
     rejected for arriving too soon after the first, and clapping never works at
     all. That bug cost an evening in the other codebase.
  4. It has to fall away immediately.
  5. The pair has to match: two claps from the same hands are within a few dB.

Run standalone to watch it without waking anything:

    .venv/bin/python3 reachy_wake.py --listen
"""

from __future__ import annotations

import difflib
import math
import os
import re
import struct
import threading
import time
import urllib.request

ROBOT_MIC_URL = os.environ.get("ROBOT_MIC_URL", "http://localhost:8775").rstrip("/")
MIC_SR = 16000
SUB_FRAME = MIC_SR // 100          # 10 ms — fine enough to see an attack


class ClapDetector:
    """Two claps, and it wakes up. See the module docstring for the rules."""

    def __init__(self, sensitivity: float = 0.55):
        self.sensitivity = sensitivity
        self.background = 0.02
        self.previous_peak = 0.0
        self.transient_start = None
        self.transient_peak = 0.0
        self.transient_rise = 0.0
        self.transient_quiet_run = float("inf")
        self.last_loud_at = None
        self.last_clap_at = None
        self.last_clap_peak = 0.0
        self.previous_run_was_clap = False
        self.last_wake_at = None

    # The dial. One number moves all of them, so there is one control and not six.
    #
    # These ranges are the ROBOT's, not the laptop's, and they are much lower.
    # Measured on Vibey's own microphone: a clap played through its own speaker
    # peaked at 0.072 against a room floor of 0.035 — where a MacBook sees 0.12
    # against 0.005. The mic is quieter and the robot's own fans raise the floor,
    # so a ratio tuned for a laptop is unreachable here and a floor tuned for one
    # rejects everything.
    @property
    def attack_ratio(self):  return 9 - 6 * self.sensitivity
    @property
    def floor(self):         return 0.14 - 0.11 * self.sensitivity
    @property
    def max_length(self):    return 0.06 + 0.04 * self.sensitivity
    @property
    def quiet_before(self):  return 0.34 - 0.14 * self.sensitivity
    @property
    def decay_to(self):      return 0.22 + 0.18 * self.sensitivity
    @property
    def min_gap(self):       return 0.10
    @property
    def max_gap(self):       return 0.55 + 0.25 * self.sensitivity
    @property
    def pair_tolerance(self):return 2.2 + 1.8 * self.sensitivity
    @property
    def rise_ratio(self):    return 5 - 2 * self.sensitivity
    @property
    def cooldown(self):      return 2.5

    @property
    def threshold(self):
        return max(self.background * self.attack_ratio, self.floor)

    def feed(self, peak: float, at: float):
        """One 10 ms frame. Returns (event, detail) — event is one of
        'nothing', 'armed', 'rejected', 'wake'."""
        loud = peak > self.threshold

        try:
            if self.last_wake_at is not None and at - self.last_wake_at < self.cooldown:
                return ("nothing", "")

            if loud:
                if self.transient_start is None:
                    self.transient_start = at
                    self.transient_peak = peak
                    self.transient_rise = peak / max(self.previous_peak, 0.0005)
                    self.transient_quiet_run = (
                        float("inf") if self.last_loud_at is None
                        else at - self.last_loud_at)
                else:
                    self.transient_peak = max(self.transient_peak, peak)
                return ("nothing", "")

            if self.transient_start is None:
                return ("nothing", "")

            top = self.transient_peak
            length = at - self.transient_start
            start = self.transient_start
            self.transient_start = None
            self.transient_peak = 0.0

            if self.transient_rise < self.rise_ratio:
                self.last_clap_at = None
                self.previous_run_was_clap = False
                return ("rejected", "came up too gradually — a voice, not a clap")

            if length > self.max_length:
                self.last_clap_at = None
                self.previous_run_was_clap = False
                return ("rejected", "too long to be a clap")

            if peak > top * self.decay_to:
                self.last_clap_at = None
                self.previous_run_was_clap = False
                return ("rejected", "did not fall away fast enough")

            if self.transient_quiet_run < self.quiet_before and not self.previous_run_was_clap:
                self.last_clap_at = None
                self.previous_run_was_clap = False
                return ("rejected", "came in the middle of other sound")

            if self.last_clap_at is None:
                self.last_clap_at = start
                self.last_clap_peak = top
                self.previous_run_was_clap = True
                return ("armed", "one clap — waiting for a second")

            gap = start - self.last_clap_at
            if not (self.min_gap <= gap <= self.max_gap):
                self.last_clap_at = start
                self.last_clap_peak = top
                self.previous_run_was_clap = True
                return ("rejected",
                        "too close together" if gap < self.min_gap else "too long since the first")

            ratio = max(top, self.last_clap_peak) / max(1e-4, min(top, self.last_clap_peak))
            if ratio > self.pair_tolerance:
                self.last_clap_at = start
                self.last_clap_peak = top
                self.previous_run_was_clap = True
                return ("rejected", "the two did not sound alike")

            self.last_clap_at = None
            self.previous_run_was_clap = False
            self.last_wake_at = at
            return ("wake", "two claps")
        finally:
            self.previous_peak = peak
            if loud:
                self.last_loud_at = at
            else:
                # Only quiet frames teach the background what quiet is, so a clap
                # cannot raise the bar against its own second half.
                self.background += (peak - self.background) * 0.05
                self.background = max(self.background, 0.004)


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


# What the recogniser actually produces for this sound, not just the spelling.
# On-device transcription is confidently wrong in specific ways, and listing them
# is cheaper than lowering the bar for everything.
WAKE_VARIANTS = [
    "hey vibey", "hey vibe", "hey viby", "hey vibi", "hey vibee",
    "hey vibey", "a vibey", "hey baby",   # "hey vibey" → "hey baby" is the common one
    "hey vibey", "hey five bee", "hey vibee", "hey by b", "hey bibi",
]

# NOTE ON TESTING THIS.
#
# You cannot check the wake phrase by playing it through the robot's own speaker.
# The Reachy audio pipeline cancels the robot's own output from its own input —
# the same reason FlowState runs voice processing — so Vibey is deliberately deaf
# to itself. Measured: speaking "hey vibey" through its speaker produced a LOWER
# mic RMS than the room did. It needs a person in the room, or nothing.


# How close a stretch of transcript has to be to count. 0.80 catches what the
# recogniser ACTUALLY produced for "hey vibey" in the room — "hey, if I be" — and
# leaves every other line it produced that evening under 0.50.
WAKE_THRESHOLD = 0.78

_WAKE_TARGETS = ("heyvibey", "heyvibe", "hivibey")


def wake_score(text: str) -> float:
    """How much a transcript sounds like the wake phrase, 0..1.

    A list of spellings is not enough. `tiny.en` heard "hey vibey" as "hey, if I
    be" — which no variant list would ever contain — so this compares the SHAPE of
    the words instead, sliding a window over the transcript with the letters run
    together.

    Windows may only start on an "h". That one constraint is what separates "hey
    vibey" from "talking about Vibey": the first scores 1.00 and the second 0.00,
    because the name on its own is somebody discussing the robot, not addressing
    it.
    """
    words = normalise(text).split()
    s = "".join(words)
    # Where each word begins in the run-together string. A match may only
    # start there: "t-h-ey've already been" put an "h" mid-word in front of
    # "eyvealreadyb", scored 0.83 and woke the robot for a podcast.
    starts, pos = set(), 0
    for w in words:
        starts.add(pos)
        pos += len(w)
    # "wake up", said plainly, wakes it.
    #
    # The shape-matching below only ever fires on windows starting with "h",
    # because it is built to recognise "hey vibey" and to ignore people merely
    # talking ABOUT Vibey. That is right for the name and wrong for this: told
    # "go to sleep", the obvious way back is "wake up", and it scored 0.00 while
    # the robot sat there with its head down. Somebody saying "wake up" in a room
    # with a sleeping robot means the robot.
    if "wakeup" in s:
        return 1.0
    best = 0.0
    for i, ch in enumerate(s):
        if ch != "h" or i not in starts:
            continue
        for target in _WAKE_TARGETS:
            w = len(target)
            for span in (w - 2, w - 1, w, w + 1, w + 2):
                seg = s[i:i + span]
                if len(seg) < 5:
                    continue
                best = max(best, difflib.SequenceMatcher(None, seg, target).ratio())
    return best


def matches_wake(text: str, variants=None) -> bool:
    return wake_score(text) >= WAKE_THRESHOLD


def peaks(pcm: bytes, sub=SUB_FRAME):
    """Per-sub-frame peak, 0..1. Never one peak per network read: a clap landing
    across two reads would measure far longer than it is."""
    n = len(pcm) // 2
    if n == 0:
        return
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    for i in range(0, n, sub):
        chunk = samples[i : i + sub]
        if chunk:
            yield max(abs(s) for s in chunk) / 32767.0, len(chunk)


class WakeListener(threading.Thread):
    """Reads the robot's mic and calls `on_wake(reason)` when it should wake."""

    daemon = True

    def __init__(self, on_wake, should_listen=None, log=print,
                 sensitivity: float = 0.55, phrase: bool = True,
                 verbose: bool = False):
        super().__init__(name="wake-listener")
        self.on_wake = on_wake
        self.should_listen = should_listen or (lambda: True)
        self.log = log
        self.clap = ClapDetector(sensitivity)
        # Both wake routes are callables rather than flags, so the dashboard can
        # turn either off mid-run without restarting this thread. They are two
        # switches because they fail differently: the phrase needs a sentence,
        # while two claps is any pair of transients — a dropped book, a door,
        # applause on a video, or somebody just clapping in conversation.
        self.phrase_enabled = phrase if callable(phrase) else (lambda p=phrase: p)
        self.clap_enabled = lambda: True
        # Prints every transient and why it was refused. "It did not hear me" is
        # not a fact anybody can act on; "you were 6 dB under" is.
        self.verbose = verbose
        self.stop_event = threading.Event()
        self.samples_seen = 0
        self.last_heard = ""
        self.last_rms = 0.0
        self.last_score = 0.0
        self._pcm_for_phrase = bytearray()
        self._model = None
        # The robot build ships without faster-whisper (too heavy for the CM4),
        # so there the phrase half switches itself off once and claps carry on.
        self._no_whisper = False

    # --- the phrase half -------------------------------------------------

    def _whisper(self):
        """Loaded lazily and once: importing faster-whisper costs seconds, and a
        robot that is only ever clapped at should not pay for it."""
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                self._no_whisper = True
                self.log("[wake] faster-whisper not installed: 'hey vibey' off, "
                         "clap-clap still wakes")
                raise
            # base.en, not tiny.en. tiny is what turned "hey vibey" into "hey, if
            # I be" — the fuzzy matcher rescues that, but a model that hears the
            # name is better than a matcher that forgives it not being heard.
            name = os.environ.get("VIBEY_WAKE_MODEL", "base.en")
            self._model = WhisperModel(name, device="cpu", compute_type="int8")
            self.log(f"[wake] whisper ready ({name})")
        return self._model

    # Whether a window is worth transcribing, as RMS.
    #
    # RMS and not peak. Measured on this microphone: impulsive noise puts the peak
    # over 0.18 while the room is quiet, so a peak gate transcribes constantly and
    # whisper fills the log with invented phrases. `reachy_chat.py`'s own VAD has
    # used an RMS threshold of 0.008 successfully for months, so this sits just
    # above it.
    SPEECH_RMS = 0.010

    def _try_phrase(self, pcm: bytes) -> bool:
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size == 0:
            return False
        rms = float(np.sqrt(np.mean(audio * audio)))
        self.last_rms = rms
        if rms < self.SPEECH_RMS:
            return False                      # room tone; do not pay for a transcribe
        segments, _ = self._whisper().transcribe(audio, language="en", beam_size=1)
        text = " ".join(s.text for s in segments).strip()
        # Whisper invents these on near-silence, every few seconds, forever. They
        # are not what the room said and they must not fill the log.
        if normalise(text) in {"thank you", "thanks for watching", "you",
                               "bye", "thank you very much", ""}:
            return False
        if text:
            score = wake_score(text)
            self.last_heard = text
            self.last_score = score
            # The score, always. "It heard me and did nothing" and "it never heard
            # me" look identical in a log that only prints the words.
            self.log(f"[wake] heard {text!r} (wake score {score:.2f})")
            return score >= WAKE_THRESHOLD
        return False

    # --- the loop --------------------------------------------------------

    def run(self):
        chunk = MIC_SR * 2 // 10              # 0.1 s
        window = MIC_SR * 2 * 2               # 2 s of audio for the phrase check
        while not self.stop_event.is_set():
            try:
                with urllib.request.urlopen(f"{ROBOT_MIC_URL}/pcm", timeout=10) as r:
                    self.log("[wake] listening on the robot's microphone")
                    while not self.stop_event.is_set():
                        raw = r.read(chunk)
                        if not raw:
                            break
                        if not self.should_listen():
                            self._pcm_for_phrase.clear()
                            time.sleep(0.2)
                            continue

                        claps_on = self.clap_enabled()
                        for peak, n in peaks(raw):
                            at = self.samples_seen / MIC_SR
                            self.samples_seen += n
                            if not claps_on:
                                # Keep the running background estimate fed even
                                # while disabled, so re-enabling doesn't start
                                # from a cold threshold and fire on the first
                                # loud thing it hears.
                                self.clap.feed(peak, at)
                                continue
                            event, detail = self.clap.feed(peak, at)
                            if event == "wake":
                                self.log("[wake] clap-clap")
                                self._fire("clap")
                            elif event in ("armed", "rejected") and self.verbose:
                                self.log(f"[wake] {event}: {detail} "
                                         f"(peak {peak:.3f}, needs {self.clap.threshold:.3f}, "
                                         f"room {self.clap.background:.4f})")

                        if self.phrase_enabled() and not self._no_whisper:
                            self._pcm_for_phrase += raw
                            if len(self._pcm_for_phrase) >= window:
                                buf = bytes(self._pcm_for_phrase)
                                # Keep the last half second, so a phrase spoken
                                # across a boundary is not cut in two.
                                self._pcm_for_phrase = bytearray(buf[-MIC_SR:])
                                try:
                                    if self._try_phrase(buf):
                                        self.log("[wake] heard the phrase")
                                        self._fire("phrase")
                                except Exception as e:  # noqa: BLE001
                                    self.log(f"[wake] transcribe failed: {e}")
            except Exception as e:  # noqa: BLE001
                if not self.stop_event.is_set():
                    self.log(f"[wake] mic stream lost ({e}) — retrying")
                    time.sleep(2)

    def _fire(self, reason: str):
        self._pcm_for_phrase.clear()
        try:
            self.on_wake(reason)
        except Exception as e:  # noqa: BLE001
            self.log(f"[wake] on_wake failed: {e}")

    def stop(self):
        self.stop_event.set()


if __name__ == "__main__":
    import sys
    print("[wake] standalone — clap twice or say 'hey vibey'. Ctrl-C to quit.")
    listener = WakeListener(on_wake=lambda why: print(f"\n*** WAKE ({why}) ***\n"),
                            phrase="--no-phrase" not in sys.argv,
                            verbose="--quiet" not in sys.argv)
    listener.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        listener.stop()
