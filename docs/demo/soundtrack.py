"""soundtrack.py: the demo reel's lo-fi bed, synthesized from scratch.

No samples, no licenses: pads, bass, drums and an arp, all numpy, at 120 bpm
so every cut in demo.html lands on a beat. The robot voice is Vibey's own: the
chirps come straight from reachy_emotes._chirp, the same R2-style sounds the
robot plays through its speaker, used sparingly at a few key moments.

    python3 soundtrack.py out.wav
"""
import sys
import wave

import os
import io

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'services'))
import reachy_emotes  # noqa: E402  (services/: Vibey's real chirps)

SR = 44100
BPM = 120
BEAT = 60 / BPM
DUR = 47.5
N = int(SR * DUR)
out = np.zeros((N, 2))


def hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def env(n, a=0.01, r=0.3):
    t = np.arange(n) / SR
    return np.minimum(1, t / max(a, 1e-4)) * np.exp(-t / r)


def put(sig, at, gain=1.0, pan=0.0):
    i = int(at * SR)
    if i >= N:
        return
    sig = sig[: N - i] * gain
    out[i:i + len(sig), 0] += sig * (1 - pan) ** 0.5
    out[i:i + len(sig), 1] += sig * (1 + pan) ** 0.5


def tone(f, dur, harmonics=(1, .5, .25, .12), detune=0.0):
    t = np.arange(int(dur * SR)) / SR
    s = sum(a * np.sin(2 * np.pi * f * (k + 1) * t * (1 + detune)) for k, a in enumerate(harmonics))
    return s / sum(harmonics)


# Fmaj7, G6, Em7, Am7: two bars (4s) each
CHORDS = [[53, 57, 60, 64], [55, 59, 62, 64], [52, 55, 59, 62], [57, 60, 64, 67]]
ROOTS = [41, 43, 40, 45]

for bar2 in range(int(DUR // 4) + 1):
    at = bar2 * 4.0
    ch = CHORDS[bar2 % 4]
    # pad: slow attack, long tail, slightly detuned pair
    for n in ch:
        d = 4.4
        e = np.minimum(1, np.arange(int(d * SR)) / (0.6 * SR)) * np.exp(-np.arange(int(d * SR)) / (3.5 * SR))
        p = (tone(hz(n), d, (1, .3, .1)) + tone(hz(n), d, (1, .3, .1), detune=0.004)) * e
        put(p, at, 0.045, pan=(n % 5 - 2) * 0.15)
    # bass on beats 1 and 3 of each bar
    if 2 <= at < DUR - 2:
        for b in range(4):
            s = tone(hz(ROOTS[bar2 % 4]), 0.9, (1, .4, .1)) * env(int(0.9 * SR), 0.005, 0.35)
            put(s, at + b * 2 * BEAT, 0.22)


def kick():
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    f = 50 + 90 * np.exp(-t * 30)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 9)


def snare():
    n = int(0.25 * SR)
    rng = np.random.default_rng(1)
    t = np.arange(n) / SR
    return (rng.standard_normal(n) * 0.6 + np.sin(2 * np.pi * 190 * t) * 0.5) * np.exp(-t * 18)


def hat(seed):
    n = int(0.06 * SR)
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n)
    x = np.diff(x, prepend=0)          # crude high-pass so it ticks instead of hisses
    return x * np.exp(-np.arange(n) / SR * 70)


K, S = kick(), snare()
beat = 0
t = 2.0
while t < DUR - 2.0:
    put(K, t, 0.55)
    if beat % 2 == 1:
        put(S, t, 0.18, pan=0.05)
    for h in range(2):
        swing = 0.03 if h else 0
        put(hat(beat * 2 + h), t + h * BEAT / 2 + swing, 0.07 if h else 0.04, pan=0.3)
    t += BEAT
    beat += 1

# arp through the busy middle (feature blitz, apps), 16ths
t = 18.5
step = 0
while t < 42.5:
    ch = CHORDS[int(t // 4) % 4]
    n = ch[[0, 1, 2, 3, 2, 1][step % 6]] + 12
    s = tone(hz(n), 0.3, (1, .2)) * env(int(0.3 * SR), 0.003, 0.09)
    put(s, t, 0.05 + 0.02 * (step % 4 == 0), pan=0.4 * np.sin(step * 0.7))
    t += BEAT / 4
    step += 1



def vibey(emotion):
    """One of Vibey's real chirps (reachy_emotes), resampled to SR."""
    with wave.open(io.BytesIO(reachy_emotes._chirp(emotion))) as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float) / 32767
    t = np.arange(int(len(x) * SR / rate)) / SR
    return np.interp(t, np.arange(len(x)) / rate, x)


# A handful of moments, not every cut: hello, a Telegram reply landing,
# a question for the brains, the feature blitz, and the send-off.
for at, emotion, gain in [(0.5, "wave", .30), (10.7, "happy", .26),
                          (24.0, "curious", .26), (28.6, "excited", .24),
                          (43.6, "victory", .30)]:
    put(vibey(emotion), at, gain, pan=-0.1)

# fade out over the last 2.5s
fade = np.ones(N)
fade[-int(2.5 * SR):] = np.linspace(1, 0, int(2.5 * SR)) ** 2
out *= fade[:, None]
out /= np.max(np.abs(out)) / 0.89

with wave.open(sys.argv[1] if len(sys.argv) > 1 else "soundtrack.wav", "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes((out * 32767).astype("<i2").tobytes())
