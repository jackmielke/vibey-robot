"""soundtrack.py: the demo reel's 42 second lo-fi bed, synthesized from scratch.

No samples, no licenses: pads, bass, drums, an arp and little robot chirps on
every scene cut, all numpy. 120 bpm so every cut in demo.html lands on a beat.

    python3 soundtrack.py out.wav
"""
import sys
import wave

import numpy as np

SR = 44100
BPM = 120
BEAT = 60 / BPM
DUR = 42.0
CUTS = [5, 10, 16, 21, 27, 32.5, 37.5]          # scene starts in demo.html
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
    if 2 <= at < 40:
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
while t < 40.0:
    put(K, t, 0.55)
    if beat % 2 == 1:
        put(S, t, 0.18, pan=0.05)
    for h in range(2):
        swing = 0.03 if h else 0
        put(hat(beat * 2 + h), t + h * BEAT / 2 + swing, 0.07 if h else 0.04, pan=0.3)
    t += BEAT
    beat += 1

# arp through the busy middle (feature blitz, apps), 16ths
t = 16.0
step = 0
while t < 37.5:
    ch = CHORDS[int(t // 4) % 4]
    n = ch[[0, 1, 2, 3, 2, 1][step % 6]] + 12
    s = tone(hz(n), 0.3, (1, .2)) * env(int(0.3 * SR), 0.003, 0.09)
    put(s, t, 0.05 + 0.02 * (step % 4 == 0), pan=0.4 * np.sin(step * 0.7))
    t += BEAT / 4
    step += 1

# robot chirps on every cut, and a little hello at the start and the end
for c in [0.4] + CUTS + [39.5]:
    n = int(0.18 * SR)
    tt = np.arange(n) / SR
    f = 900 + 1400 * tt / tt[-1]
    s = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.005, 0.06)
    put(s, c - 0.02, 0.12, pan=-0.2)
    put(s[::-1].copy() * 0.6, c + 0.14, 0.08, pan=0.2)

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
