"""Background-noise suppression for Vibey's ears.

The problem this solves: a fan, an air conditioner, a TV or — worst of all —
music playing in the room does not stop when somebody starts talking, so the
server-side VAD keeps hearing "sound", the transcriber keeps hearing lyrics,
and Vibey answers the stereo. This is the same job Zoom and Meet do on the way
into the call: estimate what the room sounds like when nobody is speaking, and
subtract it from what the microphone hears.

How it works, briefly: a 32ms sqrt-Hann STFT with 50% overlap-add, a per-bin
noise floor that only learns while no speech is present, and a
decision-directed Wiener gain (Ephraim–Malah style) smoothed across frequency.
Two things keep it from chewing up the voice, which is the usual failure of
naive spectral subtraction:

  * the gain is floored, never zeroed. Bins are attenuated toward the floor,
    not annihilated, so consonants survive and there's no musical-noise
    warble where a bin flickers between full and nothing.
  * the floor moves with the measured SNR. In a clean room the floor rises
    toward 1.0 and the whole thing is nearly a bypass — there is nothing to
    subtract, so it doesn't subtract anything. It only gets assertive when
    the room is genuinely loud relative to the voice.

Music gets special handling. It is loud, non-speech and tonal, so a flatness
test picks it out; when it's playing the noise floor is allowed to adapt faster
and is over-subtracted slightly, which is what lets a steady backing track fade
while a voice on top of it stays.

RECORDING vs LIVE LISTENING are deliberately different paths:

  * purpose="live"   — the stream feeding the VAD/model. Suppressed.
  * purpose="record" — anything being kept: a memo, a clip, a capture. Analysed
    (so the stats stay honest) but returned untouched, because a recording is
    an artifact somebody may listen to later and processing it is destructive.

PROFILES, in order of how hard they lean on the room:

    off         bypass — nothing is touched
    light       quiet room; suppress gently, never gate
    robust      quiet room, quiet person: gentle + makeup gain, so a
                half-whispered sentence from across the room still gets through
    on / room   DEFAULT — adaptive suppression, non-speech ducked a little
    music       music or a TV playing; deeper floor, and non-speech is ducked
                hard so a backing track cannot start a turn
    aggressive  loud or crowded — voice only, everything else to near-silence

`on` and `room` are the same profile under two names, because "on" is what the
preference file has always said and "room" is what people say out loud.

The GATE is what the deeper profiles add on top of subtraction. Suppression
alone lowers the music; it does not stop the VAD hearing it, and a model that
hears a chorus still takes a turn. So non-speech frames are additionally ducked
— ducked, never dropped: the samples keep flowing at a lower level, so the
far end's VAD keeps a continuous timeline instead of a hole where a chunk used
to be, which is the difference between a quiet room and a broken stream.

RECORDING vs NOT RECORDING has one answer, here, and `capture_status()` is it.
There are four different ways to not be recording — ears off, muted, Vibey
talking, and listening-but-nobody-spoke — and on a level meter they look
identical, which is exactly the confusion this ends. Every transition is logged
once and shown on the dashboard.

While Vibey's own speaker is live the noise estimate is FROZEN (see
`set_speaking`). Without that freeze the robot's own voice is learned as "the
room", and for seconds afterwards a real person gets subtracted away as if they
were it. Speech detection keeps running through it, so interrupting Vibey
mid-sentence still works.

Needs only numpy, which every mic path here already imports.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

import numpy as np

_PREFS_PATH = Path(__file__).parent / ".audio_prefs.json"

# The knobs each profile turns. `floor` None means "work it out from the
# measured SNR" — the adaptive behaviour that keeps a clean room a near-bypass.
# `duck_db` is how far a non-speech frame is pushed down AFTER subtraction; 0
# means the gate never closes, which is right for the gentle profiles where a
# false "that wasn't speech" would cost more than the noise does.
PROFILES: dict[str, dict] = {
    "off":        {"floor": 1.00, "duck_db": 0.0,   "voice_ratio": 0.00,
                   "blurb": "off — nothing touched"},
    "light":      {"floor": 0.60, "duck_db": 0.0,   "voice_ratio": 0.00,
                   "blurb": "light — quiet room, gentle"},
    "robust":     {"floor": 0.55, "duck_db": 0.0,   "voice_ratio": 0.00,
                   "blurb": "robust — quiet room, quiet voice, lifted"},
    "on":         {"floor": None, "duck_db": -12.0, "voice_ratio": 0.25,
                   "blurb": "room — normal room noise"},
    "music":      {"floor": 0.10, "duck_db": -30.0, "voice_ratio": 0.45,
                   "blurb": "music — a track or a TV playing"},
    "aggressive": {"floor": 0.07, "duck_db": -55.0, "voice_ratio": 0.55,
                   "blurb": "aggressive — loud room, voice only"},
}
MODES = tuple(PROFILES)

# What people say, mapped to what the table calls it.
_ALIASES = {
    "room": "on", "normal": "on", "default": "on", "auto": "on", "home": "on",
    "office": "on", "medium": "on", "adaptive": "on",
    "none": "off", "raw": "off", "bypass": "off", "disabled": "off",
    "quiet": "light", "gentle": "light", "low": "light", "minimal": "light",
    "soft": "light",
    "whisper": "robust", "far": "robust", "sensitive": "robust",
    "tv": "music", "song": "music", "songs": "music", "radio": "music",
    "cafe": "music", "café": "music", "stereo": "music", "speakers": "music",
    "loud": "aggressive", "party": "aggressive", "crowd": "aggressive",
    "crowded": "aggressive", "max": "aggressive", "strong": "aggressive",
    "noisy": "aggressive", "high": "aggressive",
}


def resolve(name: str | None) -> str | None:
    """Spoken words in, a profile name out. None if it means nothing here."""
    key = (name or "").strip().lower()
    if key in PROFILES:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    if key in ("true", "1", "yes"):
        return "on"
    if key in ("false", "0", "no"):
        return "off"
    return None


# --------------------------------------------------------------------------- #
# Preference
# --------------------------------------------------------------------------- #
def _load_mode() -> str:
    try:
        mode = resolve(json.loads(_PREFS_PATH.read_text())
                       .get("noise_suppression"))
        if mode:
            return mode
    except Exception:  # noqa: BLE001
        pass
    return resolve(os.environ.get("VIBEY_NOISE_SUPPRESSION")) or "on"


_PREF = {"mode": _load_mode()}
_LOCK = threading.Lock()


def get_mode() -> str:
    return _PREF["mode"]


def knobs(mode: str | None = None) -> dict:
    return PROFILES[mode or _PREF["mode"]]


def set_mode(mode: str) -> str:
    """Switch to a profile by name (or by what somebody called it), and
    remember it, so a room tuned tonight is still tuned after a restart."""
    resolved = resolve(mode)
    if not resolved:
        raise ValueError(f"mode must be one of {MODES}")
    mode = _PREF["mode"] = resolved
    try:
        data = {}
        if _PREFS_PATH.exists():
            data = json.loads(_PREFS_PATH.read_text())
        data["noise_suppression"] = mode
        _PREFS_PATH.write_text(json.dumps(data, indent=2))
    except Exception:  # noqa: BLE001
        pass       # a preference that didn't persist is better than a crash
    return mode


# --------------------------------------------------------------------------- #
# Recording vs not recording
#
# One flag would not do. "Nothing is being recorded" is true in four different
# situations that need four different answers when somebody asks why Vibey
# didn't hear them — and on a level meter all four look the same, which is how
# an hour gets lost to a microphone that was working perfectly.
# --------------------------------------------------------------------------- #
_LABELS = {
    "off": "not recording — ears off",
    "speaking": "not recording — I'm talking (interrupt me)",
    "muted": "not recording — muted",
    "idle": "not recording — listening, nobody talking",
    "recording": "recording you",
}

CAPTURE = {
    "mode": "idle",
    "recording": False,
    "label": _LABELS["idle"],
    "since": time.time(),
    # Inputs, set by whichever brain owns the mic.
    "listening": True,
    "muted": False,
    "speaking_until": 0.0,
}


def set_listening(on: bool) -> None:
    """Ears open at all, or switched off entirely."""
    CAPTURE["listening"] = bool(on)


def set_muted(on: bool) -> None:
    CAPTURE["muted"] = bool(on)


def set_speaking(seconds: float | bool) -> None:
    """Vibey's speaker is live for this long. Freezes the noise estimate — our
    own voice must never be learned as the room — and reads out as "speaking"
    rather than as a silent microphone."""
    if seconds is True:
        CAPTURE["speaking_until"] = time.time() + 3600.0
    elif not seconds:
        CAPTURE["speaking_until"] = 0.0
    else:
        CAPTURE["speaking_until"] = time.time() + float(seconds)


def is_speaking() -> bool:
    return time.time() < CAPTURE["speaking_until"]


def _set_capture(mode: str) -> str:
    if mode != CAPTURE["mode"]:
        CAPTURE["mode"] = mode
        CAPTURE["since"] = time.time()
        print(f"[audio] {_LABELS[mode]}", flush=True)
    CAPTURE["recording"] = (mode == "recording")
    CAPTURE["label"] = _LABELS[mode]
    return mode


def update_capture(speech: bool) -> str:
    """Recompute the recording state. Called from inside the suppressor with
    its own speech verdict, and directly by a VAD loop that has its own — the
    dashboard should show what that loop actually acted on, not a second
    opinion about the same audio."""
    if not CAPTURE["listening"]:
        return _set_capture("off")
    if is_speaking():
        return _set_capture("speaking")
    if CAPTURE["muted"]:
        return _set_capture("muted")
    return _set_capture("recording" if speech else "idle")


def capture_status() -> dict:
    """What the dashboard shows and what `capture_line()` says out loud."""
    return {"mode": CAPTURE["mode"], "recording": CAPTURE["recording"],
            "label": CAPTURE["label"], "since": CAPTURE["since"],
            "profile": get_mode()}


def capture_line() -> str:
    """How Vibey answers "are you recording me?" — plainly, in one line."""
    if CAPTURE["mode"] == "recording":
        return f"Recording you right now, on the {get_mode()} profile."
    return f"{CAPTURE['label'].capitalize()}. Profile is {get_mode()}."


# --------------------------------------------------------------------------- #
# The suppressor
# --------------------------------------------------------------------------- #
_EPS = 1e-10


class NoiseSuppressor:
    """One instance per audio stream — it carries the room's noise estimate."""

    def __init__(self, sample_rate: int = 16000, frame: int = 512):
        self.sr = sample_rate
        self.frame = frame
        self.hop = frame // 2
        # sqrt-Hann on both analysis and synthesis: periodic Hann at 50%
        # overlap sums to exactly 1, so the two square roots reconstruct
        # unity gain and an untouched signal comes back bit-for-bit flat.
        self._win = np.sqrt(np.hanning(frame + 1)[:frame]).astype(np.float32)
        # Window energy, so a bin's power can be read back as a per-sample
        # level — without it the reported noise floor sits ~23dB too high and
        # every threshold below is quietly wrong.
        self._win_energy = float(np.sum(self._win ** 2))
        self._pending = np.zeros(0, dtype=np.float32)
        self._tail = np.zeros(self.hop, dtype=np.float32)
        self._fifo = None            # equal-length output queue, see process_block
        bins = frame // 2 + 1
        self._noise = None                       # per-bin noise power
        self._prev_gain = np.ones(bins, np.float32)
        self._prev_pow = np.zeros(bins, np.float32)
        # Only bins where a voice actually lives are allowed to decide whether
        # someone is speaking — a rumbling fan at 60Hz must not read as speech.
        self._band = slice(max(1, int(200 * frame / sample_rate)),
                           max(2, int(3800 * frame / sample_rate)))
        self._snr_db = 0.0
        self._noise_dbfs = -90.0
        self._music_score = 0.0
        self._music = False
        self._speech = False
        self._voice_share = 0.0     # how much of the energy is voice-shaped
        self._speech_until = 0.0    # hang, so a breath doesn't close the gate
        self._open_run = 0          # frames of speech before the gate opens
        self._gate = 1.0            # the duck, ramped rather than switched

    # -- statistics the rest of the system can ask about -------------------- #
    def stats(self) -> dict:
        return {
            "mode": get_mode(),
            "snr_db": round(self._snr_db, 1),
            "noise_dbfs": round(self._noise_dbfs, 1),
            "music": self._music,
            "speech": self._speech,
            "voice_share": round(self._voice_share, 2),
            "gate_db": round(20.0 * math.log10(max(self._gate, 1e-6)), 1),
            "capture": CAPTURE["mode"],
        }

    # -- the work ----------------------------------------------------------- #
    def process(self, pcm: bytes, purpose: str = "live") -> bytes:
        """Take little-endian PCM16 mono, return it denoised (or verbatim).

        Analysis always runs, even when suppression is off or the caller is
        recording, so the noise estimate is warm the moment it's switched on
        and `stats()` never reports a stale room.
        """
        if not pcm:
            return pcm
        try:
            x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            self._pending = np.concatenate((self._pending, x))
            out = []
            while self._pending.size >= self.frame:
                seg = self._pending[:self.frame]
                self._pending = self._pending[self.hop:]
                out.append(self._frame(seg, purpose))
            passthrough = get_mode() == "off" or purpose != "live"
            if passthrough or not out:
                return pcm
            y = np.concatenate(out)
            return np.clip(y, -32768, 32767).astype("<i2").tobytes()
        except Exception:  # noqa: BLE001
            return pcm     # deaf-but-noisy beats deaf

    def process_block(self, x: np.ndarray, purpose: str = "live") -> np.ndarray:
        """Same job for float32 in [-1, 1], and — unlike `process` — exactly as
        many samples out as in, so a block-based VAD loop can drop it in
        without its arithmetic changing. The price is one frame (32ms) of
        latency, paid once by priming the queue with a frame of silence.
        """
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if get_mode() == "off" or purpose != "live":
            self._fifo = None          # re-primes when it's switched back on
            return x
        if self._fifo is None:
            self._fifo = np.zeros(self.frame, np.float32)
        raw = np.clip(x * 32768.0, -32768, 32767).astype("<i2").tobytes()
        cleaned = self.process(raw, purpose)
        if cleaned is raw:
            # Nothing came back changed — no whole frame ready yet, or the
            # suppressor caught something and fell back to the input. Either
            # way the queue's accounting no longer holds, so re-prime it and
            # hand the caller its own audio rather than a drifting copy.
            self._fifo = None
            return x
        y = np.frombuffer(cleaned, "<i2")
        self._fifo = np.concatenate((self._fifo,
                                     y.astype(np.float32) / 32768.0))
        if self._fifo.size < x.size:   # unreachable once primed; cheap insurance
            self._fifo = np.concatenate(
                (self._fifo, np.zeros(x.size - self._fifo.size, np.float32)))
        out, self._fifo = self._fifo[:x.size], self._fifo[x.size:]
        return out

    def _frame(self, seg: np.ndarray, purpose: str) -> np.ndarray:
        spec = np.fft.rfft(seg * self._win)
        power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
        gain = self._gain_for(power, seg)
        if get_mode() == "off" or purpose != "live":
            gain = np.ones_like(gain)
        y = np.fft.irfft(spec * gain, n=self.frame).astype(np.float32) * self._win
        block = y[:self.hop] + self._tail
        self._tail = y[self.hop:]
        # The gate rides on top of subtraction: suppression makes the music
        # quieter, the duck is what stops it counting as somebody talking.
        return block * self._makeup() * self._gate

    def _makeup(self) -> float:
        """Robust mode lifts quiet rooms a little; nothing else touches level."""
        if get_mode() != "robust":
            return 1.0
        if self._noise_dbfs < -55 and self._snr_db < 18:
            return 1.6
        return 1.0

    def _gain_for(self, power: np.ndarray, seg: np.ndarray) -> np.ndarray:
        if self._noise is None:
            self._noise = power + _EPS
            return np.ones_like(power)

        band = self._band
        noise_band = float(self._noise[band].mean()) + _EPS
        speech_band = float(power[band].mean()) + _EPS
        snr_db = 10.0 * math.log10(speech_band / noise_band)
        self._snr_db = 0.7 * self._snr_db + 0.3 * snr_db

        rms = float(np.sqrt(np.mean(seg ** 2))) + _EPS
        level_dbfs = 20.0 * math.log10(rms / 32768.0)
        # How much of this frame lives where a voice lives. This is the test
        # that separates a person from a stereo: both are loud and neither is
        # the room tone, but a mix spreads its energy across the bass and the
        # top end in a way that speech simply doesn't.
        self._voice_share = float(power[band].sum() / (float(power.sum()) + _EPS))
        speech_now = (snr_db > 6.0 and level_dbfs > -60.0
                      and self._voice_share >= knobs()["voice_ratio"])
        # Two frames to open (a door slam is one), then a hang so the gap
        # between two words doesn't chop a sentence into pieces.
        now = time.time()
        self._open_run = self._open_run + 1 if speech_now else 0
        if self._open_run >= 2:
            self._speech_until = now + 0.4
        self._speech = now < self._speech_until

        # Tonal + loud + not speech = something is playing. Speech has a much
        # flatter, more restless spectrum than a mix does.
        flatness = float(math.exp(np.mean(np.log(power + _EPS)))
                         / (float(power.mean()) + _EPS))
        musical_now = (not self._speech) and level_dbfs > -50.0 and flatness < 0.12
        # ~1s to latch and ~1s to let go, at 16ms a frame: a single tonal
        # syllable must not read as a stereo, and a gap between tracks must
        # not un-read one.
        self._music_score = min(1.0, max(0.0, self._music_score
                                         + (0.02 if musical_now else -0.008)))
        self._music = self._music_score > 0.5

        # Learn the room only when nobody is talking. Downward moves are always
        # allowed and always fast: over-estimating the floor is what eats
        # speech, so the estimate is quick to shrink and slow to grow.
        #
        # And never while our own speaker is live: the one thing that must not
        # end up in the estimate of "the room" is Vibey's own voice, or for
        # several seconds afterwards a real person gets subtracted away as if
        # they were it.
        if not is_speaking():
            if self._speech:
                alpha = 0.998
            elif self._music:
                alpha = 0.90   # music is stationary enough to be learned
            else:
                alpha = 0.95
            grown = alpha * self._noise + (1.0 - alpha) * power
            self._noise = np.where(power < self._noise,
                                   0.80 * self._noise + 0.20 * power, grown)
        self._noise_dbfs = 10.0 * math.log10(
            float(self._noise[band].mean()) / self._win_energy
            / (32768.0 ** 2) + _EPS)

        # Over-subtract a touch under music, where a little residual tone is
        # more annoying than a little dulling.
        noise_eff = self._noise * (1.4 if self._music else 1.0)
        post = power / (noise_eff + _EPS)
        prior = (0.96 * (self._prev_gain ** 2) * self._prev_pow / (noise_eff + _EPS)
                 + 0.04 * np.maximum(post - 1.0, 0.0))
        prior = np.maximum(prior, 1e-3)
        gain = prior / (1.0 + prior)

        floor = self._floor()
        gain = np.maximum(gain, floor)
        # Smooth across frequency — an unsmoothed gain curve is exactly what
        # makes cheap denoisers sound like a swarm of bees.
        gain = np.convolve(gain, np.array([0.25, 0.5, 0.25], np.float32),
                           mode="same")
        gain = np.clip(gain, floor, 1.0).astype(np.float32)

        self._prev_gain = gain
        self._prev_pow = power
        self._ramp_gate()
        return gain

    def _ramp_gate(self) -> None:
        """Move the duck toward where it should be, one frame at a time.

        Asymmetric on purpose: open fast, so the first consonant of a sentence
        survives; close slowly, so a pause doesn't slam. And ramped rather than
        switched, because a hard multiply change at a frame boundary is a
        click, and a click is exactly the transient a far-end VAD mistakes for
        the start of a word.
        """
        duck_db = knobs()["duck_db"]
        target = 1.0 if (self._speech or duck_db == 0.0) else 10.0 ** (duck_db / 20.0)
        coef = 0.9 if target >= self._gate else 0.15
        self._gate += coef * (target - self._gate)

    def _floor(self) -> float:
        """How much attenuation is allowed, given how bad the room actually is.

        Clean room → floor near 1.0, i.e. do essentially nothing. This is the
        knob that keeps speech from being chewed up: suppression depth is
        earned by measured noise, never applied on principle. A profile with a
        fixed floor says so instead; only the adaptive one works it out.
        """
        fixed = knobs()["floor"]
        if fixed is not None:
            return fixed
        snr = self._snr_db
        if snr >= 20.0:
            floor = 0.80
        elif snr <= 5.0:
            floor = 0.18
        else:
            floor = 0.18 + (snr - 5.0) * (0.80 - 0.18) / 15.0
        if self._music:
            floor = max(0.12, floor - 0.08)
        return floor


# --------------------------------------------------------------------------- #
# Module-level convenience: one suppressor per (purpose, rate)
# --------------------------------------------------------------------------- #
_STREAMS: dict[tuple, NoiseSuppressor] = {}


def _stream(purpose: str, sample_rate: int) -> NoiseSuppressor:
    key = (purpose, sample_rate)
    with _LOCK:
        s = _STREAMS.get(key)
        if s is None:
            s = _STREAMS[key] = NoiseSuppressor(sample_rate)
        return s


def process(pcm: bytes, purpose: str = "live", sample_rate: int = 16000) -> bytes:
    """Denoise a chunk of PCM16. purpose="record" analyses but never alters.

    This is also where the recording state gets refreshed for callers that
    have no VAD of their own — the realtime brain leans on the server's, so
    without this its dashboard indicator would never move.
    """
    s = _stream(purpose, sample_rate)
    out = s.process(pcm, purpose)
    if purpose == "live":
        update_capture(s._speech)
    return out


def process_block(x, purpose: str = "live", sample_rate: int = 16000):
    """Denoise a float32 block, same length out as in.

    Deliberately does NOT touch the recording state: every caller of this one
    has its own VAD, and two writers disagreeing about the same audio would
    flicker the indicator ten times a second.
    """
    return _stream(purpose, sample_rate).process_block(x, purpose)


def stats(purpose: str = "live", sample_rate: int = 16000) -> dict:
    return _stream(purpose, sample_rate).stats()


def describe() -> str:
    """One spoken-length line about what the room sounds like right now."""
    s = stats()
    if s["mode"] == "off":
        return "noise suppression off"
    room = ("music playing" if s["music"]
            else "quiet room" if s["noise_dbfs"] < -55
            else "noisy room")
    return (f"{PROFILES[s['mode']]['blurb']}, {room}, "
            f"signal to noise {s['snr_db']:.0f} dB, {CAPTURE['label']}")


def profile_menu() -> str:
    """The list, for when somebody asks what the choices are."""
    return "; ".join(p["blurb"] for p in PROFILES.values())


if __name__ == "__main__":
    # Self-check: four seconds of hiss, with a harmonic "voice" in the middle
    # two. What should come out is the gaps much quieter and the voice barely
    # touched — the ratio between those two numbers is the whole point, and it
    # is printed per profile so the table above can be judged rather than
    # trusted.
    sr = 16000
    t = np.arange(sr * 4) / sr
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(t.size).astype(np.float32) * 700
    voiced = ((t > 1.0) & (t < 3.0)).astype(np.float32)
    voice = sum(np.sin(2 * np.pi * f * t) * a
                for f, a in ((180, 3000), (360, 1500), (720, 700)))
    mix = np.clip(noise + voice.astype(np.float32) * voiced,
                  -32768, 32767).astype("<i2").tobytes()

    def _db(x):
        return 20 * math.log10(float(np.sqrt(np.mean(x ** 2))) + _EPS)

    a_in = np.frombuffer(mix, "<i2").astype(np.float32)
    gap = slice(int(3.2 * sr), a_in.size)      # noise only, after the voice
    spk = slice(int(1.5 * sr), int(2.5 * sr))  # voice + noise
    print(f"input       {_db(a_in[gap]):6.1f} dB room, "
          f"{_db(a_in[spk]):6.1f} dB with a voice on top")
    for name in MODES:
        _PREF["mode"] = name
        _STREAMS.clear()                       # a fresh room per profile
        out = b""
        for i in range(0, len(mix), 3200):     # same 0.1s chunks the mic serves
            out += process(mix[i:i + 3200])
        a_out = np.frombuffer(out, "<i2").astype(np.float32)
        n = min(a_in.size, a_out.size)
        room, spoke = _db(a_out[gap.start:n]), _db(a_out[spk])
        print(f"{name:>11}  room {room:6.1f} dB   voice {spoke:6.1f} dB   "
              f"separation {spoke - room:5.1f} dB")
    _PREF["mode"] = _load_mode()
    print(describe())
