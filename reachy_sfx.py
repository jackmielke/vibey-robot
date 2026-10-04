#!/usr/bin/env python3
"""
reachy_sfx.py - Vibey's soundboard and background music beds.

Short, original, synthesized robot/space-opera effects plus looping music
patterns. No samples, no downloaded assets, no franchise audio, no borrowed
melodies: just sine/noise spells written out here in maths and uploaded to the
robot speaker on first use.

Two kinds of sound:
  * effects  — one-shots, a fraction of a second (`play("laser chirp")`)
  * music    — a short original loop replayed until a timer runs out
               (`start_music("spacey march", 60)` / `stop_music()`)

The robot daemon plays one clip at a time, so this module owns that one channel
on everyone's behalf: overlapping effects QUEUE (one after another, in order),
and an effect fired while music is playing DUCKS the bed — the music pauses,
the effect lands, the loop picks back up. `set_volume(level, muted)` clamps
everything into 0..1, and music is capped lower still so a bed never shouts
over Vibey's voice.

Names are matched loosely (see `resolve`), because these are asked for out
loud: "laser chirp", "robot boop", "playful cartoon", "spacey march".
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

REACHY_URL = os.environ.get("REACHY_URL", "http://192.168.1.120:8000").rstrip("/")
SR = 22050

SFX = [
    {"name": "laser_pew", "label": "Laser Pew", "icon": "Pew", "group": "space"},
    {"name": "laser_burst", "label": "Triple Blaster", "icon": "3x", "group": "space"},
    {"name": "saber_on", "label": "Light Blade On", "icon": "On", "group": "space"},
    {"name": "saber_swing", "label": "Light Blade Swing", "icon": "Swish", "group": "space"},
    {"name": "hyperjump", "label": "Hyperjump", "icon": "Warp", "group": "space"},
    {"name": "shield_up", "label": "Shield Up", "icon": "Shield", "group": "space"},
    {"name": "tractor_beam", "label": "Tractor Beam", "icon": "Beam", "group": "space"},
    {"name": "airlock", "label": "Airlock Door", "icon": "Door", "group": "space"},
    {"name": "saber_off", "label": "Light Blade Off", "icon": "Off", "group": "space"},
    {"name": "saber_hum", "label": "Light Blade Hum", "icon": "Hum", "group": "space"},
    {"name": "saber_clash", "label": "Light Blade Clash", "icon": "Clash", "group": "space"},
    {"name": "blaster_stun", "label": "Stun Blast", "icon": "Stun", "group": "space"},
    {"name": "ion_scream", "label": "Ion Fighter Flyby", "icon": "Fly", "group": "space"},
    {"name": "dark_breath", "label": "Dark Lord Breath", "icon": "Hhh", "group": "dark"},
    {"name": "dark_sting", "label": "Dark Side Sting", "icon": "Doom", "group": "dark"},
    {"name": "droid_yes", "label": "Droid Yes", "icon": "Yes", "group": "droid"},
    {"name": "droid_no", "label": "Droid No", "icon": "No", "group": "droid"},
    {"name": "droid_gossip", "label": "Droid Gossip", "icon": "Talk", "group": "droid"},
    {"name": "scanner", "label": "Scanner Sweep", "icon": "Scan", "group": "droid"},
    {"name": "cantina", "label": "Tiny Cantina", "icon": "Band", "group": "music"},
    {"name": "success", "label": "Quest Complete", "icon": "Win", "group": "mood"},
    {"name": "fail", "label": "Sad Trombone Bot", "icon": "Womp", "group": "mood"},
    {"name": "mischief", "label": "Mischief", "icon": "Hmm", "group": "mood"},
    {"name": "robot_boop", "label": "Robot Boop", "icon": "Boop", "group": "droid"},
    {"name": "droid_excited", "label": "Droid Excited", "icon": "Woo", "group": "droid"},
    {"name": "droid_sad", "label": "Droid Sad", "icon": "Aww", "group": "droid"},
    {"name": "droid_scream", "label": "Droid Scream", "icon": "Eek", "group": "droid"},
    {"name": "droid_whistle", "label": "Droid Whistle", "icon": "Wheet", "group": "droid"},
    {"name": "droid_curious", "label": "Droid Curious", "icon": "Hm?", "group": "droid"},
    {"name": "droid_giggle", "label": "Droid Giggle", "icon": "Hehe", "group": "droid"},
    {"name": "droid_alarm", "label": "Droid Alarm", "icon": "Alert", "group": "droid"},
    {"name": "mouse_droid", "label": "Mouse Droid", "icon": "Squeak", "group": "droid"},
    {"name": "probe_droid", "label": "Probe Droid", "icon": "Probe", "group": "droid"},
    {"name": "thermal_detonator", "label": "Thermal Detonator", "icon": "Boom", "group": "space"},
    {"name": "seismic_charge", "label": "Seismic Charge", "icon": "Wham", "group": "space"},
    {"name": "carbon_freeze", "label": "Carbon Freeze", "icon": "Frost", "group": "space"},
    {"name": "force_push", "label": "Force Push", "icon": "Push", "group": "space"},
    {"name": "hologram", "label": "Hologram Message", "icon": "Holo", "group": "space"},
    {"name": "hyperdrive_fail", "label": "Hyperdrive Fail", "icon": "Fizzle", "group": "space"},
    {"name": "imperial_alarm", "label": "Imperial Alarm", "icon": "Klaxon", "group": "dark"},
    {"name": "red_blade", "label": "Crackling Red Blade", "icon": "Crack", "group": "dark"},
    {"name": "cartoon_boing", "label": "Cartoon Boing", "icon": "Boing", "group": "mood"},
    {"name": "sparkle_up", "label": "Sparkle Up", "icon": "Ping", "group": "mood"},
]

# Looping music beds. Original patterns in a genre — cartoon energy, space
# opera swagger — never anybody's tune. Each entry renders one short loop that
# gets replayed until the requested duration is up.
MUSIC = [
    {"name": "playful_cartoon", "label": "Playful Cartoon", "icon": "Loop", "group": "bed"},
    {"name": "spacey_march", "label": "Spacey March", "icon": "Loop", "group": "bed"},
    {"name": "dreamy_drift", "label": "Dreamy Drift", "icon": "Loop", "group": "bed"},
    {"name": "chase_scene", "label": "Cartoon Chase", "icon": "Loop", "group": "bed"},
]
STOP_ENTRY = {"name": "stop_audio", "label": "Stop Audio", "icon": "Stop", "group": "bed"}

MUSIC_DEFAULT_SECONDS = 45.0
MUSIC_MAX_SECONDS = 300.0
# Volume limits. Everything is rendered at `level` of nominal (0..1, clamped),
# and a music bed is additionally capped — background means background.
MAX_LEVEL = 1.0
MUSIC_LEVEL_CAP = 0.6

# Spoken shorthands. Whatever isn't here still has a chance through the
# substring pass in resolve(), this is just for the phrasings people reach for.
_ALIASES = {
    "laser": "laser_pew", "laser chirp": "laser_pew", "pew": "laser_pew",
    "zap": "laser_pew", "blaster": "laser_burst", "blasters": "laser_burst",
    "lightsaber": "saber_on", "light saber": "saber_on", "sword": "saber_on",
    "swing": "saber_swing", "swish": "saber_swing",
    "warp": "hyperjump", "hyperspace": "hyperjump", "jump": "hyperjump",
    "shield": "shield_up", "shields": "shield_up",
    "beam": "tractor_beam", "door": "airlock", "hiss": "airlock",
    "scan": "scanner", "radar": "scanner",
    "vader": "dark_breath", "darth vader": "dark_breath", "darth": "dark_breath",
    "breathing": "dark_breath", "breath": "dark_breath", "respirator": "dark_breath",
    "dark side": "dark_sting", "sith": "dark_sting", "doom": "dark_sting",
    "ominous": "dark_sting", "empire": "dark_sting", "imperial": "dark_sting",
    "saber off": "saber_off", "lightsaber off": "saber_off",
    "hum": "saber_hum", "saber hum": "saber_hum",
    "clash": "saber_clash", "duel": "saber_clash", "fight": "saber_clash",
    "stun": "blaster_stun", "tie fighter": "ion_scream", "tie": "ion_scream",
    "flyby": "ion_scream", "fighter": "ion_scream",
    "yes": "droid_yes", "affirmative": "droid_yes",
    "no": "droid_no", "negative": "droid_no",
    "chatter": "droid_gossip", "gossip": "droid_gossip", "beeping": "droid_gossip",
    "boop": "robot_boop", "beep": "robot_boop", "robot beep": "robot_boop",
    "excited": "droid_excited", "happy droid": "droid_excited", "sad": "droid_sad",
    "scream": "droid_scream", "whistle": "droid_whistle", "curious": "droid_curious",
    "giggle": "droid_giggle", "laugh": "droid_giggle", "alarm": "droid_alarm",
    "mouse": "mouse_droid", "probe": "probe_droid", "detonator": "thermal_detonator",
    "grenade": "thermal_detonator", "seismic": "seismic_charge", "freeze": "carbon_freeze",
    "carbonite": "carbon_freeze", "force": "force_push", "hologram": "hologram",
    "holo": "hologram", "fizzle": "hyperdrive_fail", "klaxon": "imperial_alarm",
    "red saber": "red_blade", "crackle": "red_blade",
    "boing": "cartoon_boing", "sproing": "cartoon_boing", "bounce": "cartoon_boing",
    "sparkle": "sparkle_up", "twinkle": "sparkle_up", "magic": "sparkle_up",
    "win": "success", "ta da": "success", "tada": "success", "fanfare": "success",
    "womp": "fail", "sad": "fail", "oops": "fail", "trombone": "fail",
    "band": "cantina", "lounge": "cantina",
    "playful": "playful_cartoon", "cartoon": "playful_cartoon",
    "cartoony": "playful_cartoon", "bouncy": "playful_cartoon",
    "spacey": "spacey_march", "space": "spacey_march", "march": "spacey_march",
    "space opera": "spacey_march", "epic": "spacey_march",
    "dreamy": "dreamy_drift", "ambient": "dreamy_drift", "chill": "dreamy_drift",
    "drift": "dreamy_drift", "calm": "dreamy_drift",
    "chase": "chase_scene", "chase music": "chase_scene", "frantic": "chase_scene",
    "hurry": "chase_scene", "panic": "chase_scene",
    "stop": "stop_audio", "silence": "stop_audio", "quiet": "stop_audio",
    "shush": "stop_audio", "enough": "stop_audio",
}


def catalog() -> list[dict]:
    """Everything the soundboard can fire — effects, beds, and the stop button."""
    return SFX + MUSIC + [STOP_ENTRY]


def sfx_names() -> list[str]:
    return [s["name"] for s in SFX]


def music_names() -> list[str]:
    return [m["name"] for m in MUSIC]


def _norm(text: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def resolve(spoken: str) -> str | None:
    """Turn a spoken request into a catalog name, or None if nothing fits.
    'laser chirp' → laser_pew, 'playful cartoon' → playful_cartoon."""
    q = _norm(spoken or "")
    if not q:
        return None
    known = {}
    for entry in catalog():
        known[_norm(entry["name"])] = entry["name"]
        known[_norm(entry["label"])] = entry["name"]
    if q in known:
        return known[q]
    if q in _ALIASES:
        return _ALIASES[q]
    for phrase in sorted((a for a in _ALIASES if " " in a), key=len, reverse=True):
        if f" {phrase} " in f" {q} ":
            return _ALIASES[phrase]
    for word in q.split():
        if word in _ALIASES:
            return _ALIASES[word]
    for phrase, name in known.items():
        if phrase in q or q in phrase:
            return name
    return None


# Volume + mute apply to everything this module plays. Baked into the rendered
# WAV (the daemon has no mixer), so the level is part of the upload's identity.
_AUDIO = {"level": 0.7, "muted": False}


def set_volume(level: float | None = None, muted: bool | None = None) -> dict:
    """Clamp the level into 0..1 and/or flip mute. Muting stops what's playing."""
    if level is not None:
        _AUDIO["level"] = max(0.0, min(MAX_LEVEL, float(level)))
    if muted is not None:
        _AUDIO["muted"] = bool(muted)
        if _AUDIO["muted"]:
            stop_all()
    return status()


def status() -> dict:
    playing = _MUSIC["vibe"] if _music_alive() else None
    return {
        "level": round(_AUDIO["level"], 2),
        "muted": _AUDIO["muted"],
        "music": playing,
        "seconds_left": (round(max(0.0, _MUSIC["until"] - time.time()), 1)
                         if playing else 0.0),
    }


def _bucket(level: float) -> int:
    """Quantise the level to four steps, so we cache four uploads not a hundred."""
    return max(1, min(4, int(round(level * 4))))


def _sine(freq: float, dur: float, vol: float = 0.35,
          bend: float = 0.0, vibrato: float = 0.0) -> list[float]:
    n = int(SR * dur)
    phase = 0.0
    out = []
    for i in range(n):
        t = i / SR
        frac = i / max(1, n - 1)
        f = freq + bend * frac
        if vibrato:
            f += vibrato * math.sin(2 * math.pi * 12 * t)
        phase += 2 * math.pi * f / SR
        env = min(1.0, i / (SR * 0.008), (n - i) / (SR * 0.025))
        out.append(vol * env * math.sin(phase))
    return out


def _noise(dur: float, vol: float = 0.18, seed: int = 1) -> list[float]:
    rnd = random.Random(seed)
    n = int(SR * dur)
    out = []
    last = 0.0
    for i in range(n):
        last = last * 0.72 + rnd.uniform(-1, 1) * 0.28
        env = min(1.0, i / (SR * 0.01), (n - i) / (SR * 0.04))
        out.append(last * vol * env)
    return out


def _breath(dur: float, vol: float, rise: bool, seed: int) -> list[float]:
    """Air through a mask: dark, band-limited noise under a slow swell, with a
    faint low buzz riding it so it reads as a respirator, not wind."""
    rnd = random.Random(seed)
    n = int(SR * dur)
    out, lo, hi = [], 0.0, 0.0
    for i in range(n):
        frac = i / max(1, n - 1)
        env = math.sin(math.pi * frac) ** (0.6 if rise else 1.4)
        x = rnd.uniform(-1, 1)
        lo = lo * 0.93 + x * 0.07          # low-pass
        hi = hi * 0.6 + lo * 0.4           # soften the top again
        buzz = 0.25 * math.sin(2 * math.pi * (58 if rise else 52) * i / SR)
        out.append(vol * env * (hi * 3.2 + buzz * hi * 6))
    return out


def _silence(dur: float) -> list[float]:
    return [0.0] * int(SR * dur)


def _mix(*tracks: list[float]) -> list[float]:
    n = max((len(t) for t in tracks), default=0)
    out = [0.0] * n
    for t in tracks:
        for i, s in enumerate(t):
            out[i] += s
    return [max(-1.0, min(1.0, s)) for s in out]


def _overlay(base: list[float], sound: list[float], at: float) -> list[float]:
    start = int(SR * at)
    need = start + len(sound)
    if len(base) < need:
        base.extend([0.0] * (need - len(base)))
    for i, s in enumerate(sound):
        base[start + i] += s
    return base


def _to_wav(samples: list[float]) -> bytes:
    samples = [max(-1.0, min(1.0, s)) for s in samples]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", int(s * 32767)) for s in samples))
    return buf.getvalue()


def _effect(name: str) -> list[float]:
    if name == "laser_pew":
        s = _mix(_sine(1550, .16, .45, bend=-1150), _noise(.12, .06, 11))
    elif name == "laser_burst":
        s = []
        for at in (0.0, .14, .28):
            _overlay(s, _mix(_sine(1450, .11, .38, bend=-950), _noise(.09, .05, 12)), at)
    elif name == "saber_on":
        s = _mix(_sine(90, .65, .34, bend=30), _sine(180, .65, .16, bend=50),
                 _noise(.65, .07, 21))
        s = _overlay(s, _sine(740, .22, .22, bend=220), .03)
    elif name == "saber_swing":
        s = _mix(_sine(120, .42, .30, bend=85), _sine(380, .34, .18, bend=-160),
                 _noise(.36, .10, 22))
    elif name == "hyperjump":
        s = _mix(_sine(120, .75, .24, bend=1600), _sine(240, .72, .16, bend=2100),
                 _noise(.7, .07, 31))
        s = _overlay(s, _sine(2100, .16, .32, bend=-1400), .62)
    elif name == "shield_up":
        s = _mix(_sine(260, .55, .25, bend=320), _sine(520, .55, .16, bend=480),
                 _sine(1040, .42, .10, vibrato=35))
    elif name == "tractor_beam":
        s = _mix(_sine(85, .9, .30, bend=-18, vibrato=9),
                 _sine(170, .9, .13, bend=-35, vibrato=14), _noise(.9, .05, 41))
    elif name == "airlock":
        s = _mix(_sine(70, .55, .26, bend=-18), _noise(.7, .12, 51))
        s = _overlay(s, _sine(410, .09, .23, bend=-80), .58)
    elif name == "saber_off":
        s = _mix(_sine(120, .5, .32, bend=-80), _sine(240, .45, .14, bend=-170),
                 _noise(.45, .06, 23))
        s = _overlay(s, _sine(900, .16, .18, bend=-600), 0.0)
    elif name == "saber_hum":
        s = _mix(_sine(92, 1.6, .30, vibrato=2.5), _sine(184, 1.6, .12, vibrato=4),
                 _sine(95, 1.6, .10), _noise(1.6, .03, 24))
    elif name == "saber_clash":
        s = _mix(_noise(.35, .40, 25), _sine(2400, .08, .30, bend=-1600),
                 _sine(140, .45, .30, bend=40, vibrato=20))
        s = _overlay(s, _sine(3200, .05, .20, bend=-2000), .06)
    elif name == "blaster_stun":
        s = []
        for at in (0.0, .03, .06):
            _overlay(s, _sine(900, .35, .16, bend=-500, vibrato=180), at)
        s = _overlay(s, _noise(.3, .05, 26), 0.0)
    elif name == "ion_scream":
        s = _mix(_sine(320, 1.3, .20, bend=520, vibrato=40),
                 _sine(655, 1.3, .12, bend=-380, vibrato=65), _noise(1.3, .10, 27))
        n = len(s)
        s = [v * math.sin(math.pi * i / n) ** 2 for i, v in enumerate(s)]
    elif name == "dark_breath":
        s = []
        _overlay(s, _breath(1.35, .45, True, 61), 0.0)
        _overlay(s, _sine(1900, .018, .10), 1.38)      # the valve
        _overlay(s, _breath(1.7, .45, False, 62), 1.45)
    elif name == "dark_sting":
        # An original, ominous minor stab: low octaves, a flat second on top.
        s = _mix(_sine(55, 1.6, .30), _sine(110, 1.6, .20), _sine(130.8, 1.6, .14),
                 _sine(116.5, 1.6, .10, vibrato=3), _noise(1.6, .04, 63))
        s = _overlay(s, _mix(_sine(41.2, 1.2, .30), _sine(82.4, 1.2, .16)), .9)
        n = len(s)
        s = [v * min(1.0, i / (SR * .02)) * (1 - i / n) ** .5 for i, v in enumerate(s)]
    elif name == "droid_yes":
        s = _sine(520, .08, .35) + _sine(760, .09, .36) + _sine(1060, .12, .35)
    elif name == "droid_no":
        s = _sine(480, .12, .33) + _silence(.04) + _sine(300, .22, .34, bend=-30)
    elif name == "droid_gossip":
        s = []
        notes = [720, 980, 610, 1210, 840, 560, 1040, 700, 1320]
        for i, f in enumerate(notes):
            _overlay(s, _sine(f, .055, .24, bend=random.Random(i).choice([-90, 80])), i * .07)
    elif name == "scanner":
        s = _mix(_sine(420, .9, .18, bend=760, vibrato=10), _sine(1180, .9, .08, bend=-500))
        for at in (.12, .28, .44, .60, .76):
            _overlay(s, _sine(1800, .035, .18, bend=-250), at)
    # --- more droids: every one an original pattern of whistles and blips ---
    elif name == "droid_excited":
        s = []
        for i, (f, b) in enumerate([(700, 500), (1100, -300), (900, 700), (1300, 400)]):
            _overlay(s, _sine(f, .09, .28, bend=b, vibrato=40), i * .08)
        _overlay(s, _sine(1500, .22, .26, bend=900, vibrato=60), .34)
    elif name == "droid_sad":
        s = _sine(900, .35, .28, bend=-450, vibrato=8) + _silence(.06) + \
            _sine(520, .55, .26, bend=-260, vibrato=5)
    elif name == "droid_scream":
        s = []
        for i in range(6):
            _overlay(s, _sine(1700 + 260 * (i % 2), .14, .30, bend=-700, vibrato=110), i * .1)
        _overlay(s, _sine(2300, .35, .26, bend=-1600), .62)
    elif name == "droid_whistle":
        s = _sine(900, .16, .30, bend=1100) + _silence(.05) + _sine(1100, .3, .30, bend=-700)
    elif name == "droid_curious":
        s = _sine(600, .08, .26) + _silence(.03) + _sine(640, .08, .26) + _silence(.04) + \
            _sine(700, .28, .28, bend=700, vibrato=20)
    elif name == "droid_giggle":
        s = []
        for i, f in enumerate((1200, 1050, 1250, 1000, 1300, 950, 1350)):
            _overlay(s, _sine(f, .045, .26, bend=-150), i * .06)
    elif name == "droid_alarm":
        s = []
        for i in range(8):
            _overlay(s, _sine(1400 if i % 2 else 950, .09, .28, vibrato=30), i * .1)
    elif name == "mouse_droid":
        s = []
        rnd = random.Random(81)
        for i in range(12):
            _overlay(s, _sine(rnd.choice([2200, 2600, 3000, 2400]), .03, .20,
                              bend=rnd.choice([-600, 500])), i * .045)
    elif name == "probe_droid":
        s = _mix(_noise(1.4, .05, 82), _sine(140, 1.4, .10, vibrato=6))
        for at, f in ((.1, 880), (.35, 660), (.5, 990), (.85, 520), (1.05, 740)):
            _overlay(s, _sine(f, .12, .18, vibrato=90), at)
    # --- more space ---
    elif name == "thermal_detonator":
        s = []
        for i in range(6):
            _overlay(s, _sine(1650, .05, .26), i * (.32 - i * .04))
        at = sum(.32 - i * .04 for i in range(6)) + .05
        _overlay(s, _mix(_noise(.9, .45, 83), _sine(60, .9, .4, bend=-30)), at)
        n = len(s)
        start = int(SR * at)
        s = [v if i < start else v * (1 - (i - start) / max(1, n - start)) ** 1.5
             for i, v in enumerate(s)]
    elif name == "seismic_charge":
        s = _silence(.45)
        boom = _mix(_sine(48, 2.2, .5, bend=-14), _sine(96, 2.2, .18, bend=-30),
                    _noise(2.2, .06, 84))
        boom = [v * (1 - i / len(boom)) ** 1.2 for i, v in enumerate(boom)]
        s = _overlay(s, boom, .45)
        s = _overlay(s, _sine(3100, 1.9, .05, bend=-900), .5)      # the ringing
    elif name == "carbon_freeze":
        s = _mix(_noise(1.6, .14, 85), _sine(400, 1.6, .16, bend=-330, vibrato=4))
        n = len(s)
        s = [v * (1 - i / n) ** .7 for i, v in enumerate(s)]
        s = _overlay(s, _sine(120, .4, .3, bend=-50), 1.4)          # clunk
    elif name == "force_push":
        s = _mix(_sine(80, .7, .4, bend=-35), _noise(.7, .22, 86))
        n = len(s)
        s = [v * math.sin(math.pi * min(1.0, i / (n * .35))) ** .5 * (1 - i / n)
             for i, v in enumerate(s)]
    elif name == "hologram":
        s = _mix(_sine(660, 1.3, .12, vibrato=25), _sine(990, 1.3, .07, vibrato=40),
                 _noise(1.3, .05, 87))
        rnd = random.Random(88)
        s = [v * (0.4 if rnd.random() < .08 else 1.0) for v in s]     # flicker
    elif name == "hyperdrive_fail":
        s = _mix(_sine(200, .6, .22, bend=900), _noise(.6, .05, 89))
        s = s + _mix(_sine(1100, 1.0, .22, bend=-1000, vibrato=20), _noise(1.0, .1, 90))
        s = _overlay(s, _sine(90, .25, .3, bend=-40), 1.55)
    elif name == "imperial_alarm":
        s = []
        for i in range(4):
            _overlay(s, _mix(_sine(620, .32, .22), _sine(930, .32, .10)), i * .7)
            _overlay(s, _mix(_sine(465, .32, .22), _sine(698, .32, .10)), i * .7 + .35)
    elif name == "red_blade":
        rnd = random.Random(91)
        hum = _mix(_sine(78, 1.4, .30, bend=25, vibrato=3), _sine(156, 1.4, .14, bend=40))
        crack = [rnd.uniform(-1, 1) * .22 if rnd.random() < .12 else 0.0 for _ in hum]
        s = _mix(hum, crack)
        s = _overlay(s, _sine(700, .2, .2, bend=250), .02)
    elif name == "cantina":
        s = []
        melody = [(440, .12), (554, .12), (659, .12), (554, .12),
                  (440, .12), (370, .12), (440, .22)]
        at = 0.0
        for f, d in melody:
            _overlay(s, _mix(_sine(f, d, .22), _sine(f * 2, d, .06)), at)
            at += d + .025
    elif name == "success":
        s = _sine(523, .09, .28) + _sine(659, .09, .28) + _sine(784, .09, .30) + _sine(1046, .20, .34)
    elif name == "fail":
        s = _sine(330, .18, .30, bend=-20) + _sine(294, .18, .30, bend=-20) + _sine(262, .32, .32, bend=-70)
    elif name == "mischief":
        s = _sine(300, .09, .22) + _silence(.05) + _sine(450, .11, .25, bend=70) + _silence(.04) + _sine(390, .18, .24, vibrato=18)
    elif name == "robot_boop":
        s = _sine(700, .07, .32) + _sine(520, .11, .30, bend=-40)
    elif name == "cartoon_boing":
        s = _sine(560, .34, .30, bend=-360, vibrato=55)
    elif name == "sparkle_up":
        s = []
        for i, f in enumerate((660, 880, 1170, 1560)):
            _overlay(s, _mix(_sine(f, .07, .22), _sine(f * 2, .05, .06)), i * .045)
    else:
        raise KeyError(name)
    return s


def _music(vibe: str) -> list[float]:
    """One loop of a background bed. Written from scratch: scale patterns and
    rhythms, in the spirit of cartoon chases and space-opera marches, but not
    quoting anyone's melody."""
    s: list[float] = []
    if vibe == "playful_cartoon":
        beat = 60 / 138.0
        skip = [523, 659, 587, 784, 880, 784, 659, 587]     # bouncy pentatonic
        for bar in range(2):
            for i, f in enumerate(skip):
                at = (bar * 8 + i) * beat / 2
                _overlay(s, _sine(f * (2 if bar and i == 7 else 1), beat * .34, .15), at)
            for b in range(4):
                at = (bar * 4 + b) * beat
                _overlay(s, _sine(131 if b % 2 == 0 else 175, beat * .42, .20, bend=-8), at)
                _overlay(s, _noise(.03, .09, 60 + b + bar * 4), at + beat * .5)
    elif vibe == "spacey_march":
        beat = 60 / 100.0
        bass = [110, 110, 165, 110, 110, 98, 147, 110]      # a stomping A minor
        for i, f in enumerate(bass):
            at = i * beat
            _overlay(s, _mix(_sine(f, beat * .5, .24, bend=-4),
                             _sine(f * 2, beat * .45, .09)), at)
            if i % 2 == 1:
                _overlay(s, _noise(.09, .11, 70 + i), at)   # snare-ish crack
        for i, root in enumerate((220, 196)):               # sustained fifths
            _overlay(s, _mix(_sine(root, beat * 4, .09, vibrato=3),
                             _sine(root * 1.5, beat * 4, .06, vibrato=4)), i * beat * 4)
        _overlay(s, _sine(660, beat * 1.2, .09, bend=340), beat * 6)
    elif vibe == "dreamy_drift":
        chords = [(196, 294, 392), (175, 262, 349), (147, 220, 330), (165, 247, 330)]
        span = 2.2
        for i, chord in enumerate(chords):
            at = i * span
            for k, f in enumerate(chord):
                _overlay(s, _sine(f, span * 1.02, .11 - k * .02, vibrato=2.5), at)
            _overlay(s, _sine(chord[2] * 2, .5, .06, bend=60), at + span * .55)
    elif vibe == "chase_scene":
        beat = 60 / 168.0
        run = [147, 175, 196, 233, 196, 175, 147, 131]      # scurrying bassline
        for bar in range(2):
            for i, f in enumerate(run):
                at = (bar * 8 + i) * beat / 2
                _overlay(s, _sine(f * 2, beat * .28, .20), at)
                if i % 2 == 0:
                    _overlay(s, _noise(.025, .08, 90 + i + bar * 8), at)
            _overlay(s, _sine(880 if bar == 0 else 988, beat * .6, .09, bend=-140),
                     (bar * 8 + 6) * beat / 2)
    else:
        raise KeyError(vibe)
    return s


def _render(key: str) -> list[float]:
    return _music(key) if key in set(music_names()) else _effect(key)


# Rendered-and-uploaded clips, keyed by (name, volume bucket) → (file, seconds).
_uploaded: dict[tuple[str, int], tuple[str, float]] = {}
_lock = threading.Lock()


def _ensure(key: str, bucket: int) -> tuple[str, float]:
    """Render at this volume, upload once, and remember the clip's length."""
    with _lock:
        hit = _uploaded.get((key, bucket))
        if hit:
            return hit
        samples = _render(key)
        gain = bucket / 4.0
        wav = _to_wav([s * gain for s in samples])
        filename = f"wonder_snd_{key}_v{bucket}.wav"
        boundary = f"----sfx{uuid.uuid4().hex}"
        payload = ((f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="file"; filename="{filename}"\r\n'
                    f"Content-Type: audio/wav\r\n\r\n").encode()
                   + wav + f"\r\n--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            f"{REACHY_URL}/api/media/sounds/upload", data=payload, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        entry = (filename, len(samples) / SR)
        _uploaded[(key, bucket)] = entry
        return entry


def _speaker(path: str, body: dict) -> None:
    req = urllib.request.Request(
        f"{REACHY_URL}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


# The daemon has exactly one speaker channel, so this module arbitrates it:
# `_gate` queues overlapping effects, `_duck` tells the music loop to hold.
_gate = threading.Lock()
_duck = threading.Event()
_MUSIC: dict = {"vibe": None, "until": 0.0, "stop": threading.Event(), "thread": None}


def _run_effect(key: str) -> None:
    with _gate:                       # queued: overlapping effects play in order
        try:
            filename, dur = _ensure(key, _bucket(_AUDIO["level"]))
        except Exception as e:  # noqa: BLE001
            print(f"[sfx] {key} failed: {e}", flush=True)
            return
        ducking = _music_alive()
        try:
            if ducking:
                _duck.set()
                _speaker("/api/media/stop_sound", {})   # take the channel
            _speaker("/api/media/play_sound", {"file": filename})
            time.sleep(dur + 0.08)
        except Exception as e:  # noqa: BLE001
            print(f"[sfx] {key} failed: {e}", flush=True)
        finally:
            _duck.clear()             # the bed resumes on its next tick


# A held (or mashed) soundboard button sends one request per repeat, and every
# one of them used to QUEUE — so a half-second press bought a stomping little
# effect that kept firing over the top of the conversation long after the finger
# came off. Same effect asked for again inside this window is dropped, not
# queued. A deliberate double-tap is slower than this; an auto-repeat is not.
_REPEAT_S = 0.7
_last_fired: dict[str, float] = {}


def play(name: str) -> bool:
    """Fire anything in the catalog by name or spoken shorthand. Non-blocking.
    Music names start the bed; 'stop' stops everything. False = no such sound.
    Returns True while muted — the request is honoured, just silently."""
    key = resolve(name)
    if not key:
        return False
    if key == "stop_audio":
        stop_all()
        return True
    if key in set(music_names()):
        return start_music(key) is not None
    if _AUDIO["muted"]:
        return True
    now = time.time()
    if now - _last_fired.get(key, 0.0) < _REPEAT_S:
        return True                   # a repeat of a held button: honoured, silently
    _last_fired[key] = now
    threading.Thread(target=_run_effect, args=(key,), daemon=True).start()
    return True


def _music_alive() -> bool:
    t = _MUSIC["thread"]
    return bool(t and t.is_alive())


def _music_worker(key: str, until: float, stop: threading.Event) -> None:
    try:
        filename, clip = _ensure(key, _bucket(min(_AUDIO["level"], MUSIC_LEVEL_CAP)))
    except Exception as e:  # noqa: BLE001
        print(f"[sfx] music {key} failed: {e}", flush=True)
        return
    while not stop.is_set() and time.time() < until:
        if _duck.is_set() or _AUDIO["muted"]:
            time.sleep(0.1)
            continue
        try:
            _speaker("/api/media/play_sound", {"file": filename})
        except Exception as e:  # noqa: BLE001
            print(f"[sfx] music {key} failed: {e}", flush=True)
            return
        end = min(until, time.time() + clip)
        while time.time() < end and not stop.is_set() and not _duck.is_set():
            time.sleep(0.08)
    if not stop.is_set() and not _duck.is_set():
        try:                          # timer ran out mid-loop: trim the tail
            _speaker("/api/media/stop_sound", {})
        except Exception:  # noqa: BLE001
            pass


def start_music(vibe: str, seconds: float | None = None) -> str | None:
    """Loop a music bed for `seconds` (default 45, capped at 5 minutes).
    Replaces whatever was playing. Returns the vibe, or None if unknown."""
    key = resolve(vibe)
    if key not in set(music_names()):
        return None
    secs = MUSIC_DEFAULT_SECONDS if seconds is None else float(seconds)
    secs = max(5.0, min(MUSIC_MAX_SECONDS, secs))
    stop_music()
    if _AUDIO["muted"]:
        return key
    stop = threading.Event()
    _MUSIC.update({"vibe": key, "until": time.time() + secs, "stop": stop})
    _MUSIC["thread"] = threading.Thread(
        target=_music_worker, args=(key, _MUSIC["until"], stop), daemon=True)
    _MUSIC["thread"].start()
    return key


def stop_music() -> bool:
    """Stop the bed. True if something was actually playing."""
    t = _MUSIC["thread"]
    was = bool(t and t.is_alive())
    _MUSIC["stop"].set()
    if was:
        t.join(timeout=1.5)
    _MUSIC.update({"vibe": None, "until": 0.0, "thread": None})
    if was and not _duck.is_set():
        try:
            _speaker("/api/media/stop_sound", {})
        except Exception:  # noqa: BLE001
            pass
    return was


def stop_all() -> None:
    """Music off and the speaker cut — the panic button."""
    stop_music()
    try:
        _speaker("/api/media/stop_sound", {})
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    import sys
    choice = " ".join(sys.argv[1:]) or "laser_pew"
    key = resolve(choice)
    print(f"[sfx] {choice!r} → {key}")
    if key in set(music_names()):
        secs = 12.0
        start_music(key, secs)
        time.sleep(secs + 1)
    else:
        play(choice)
        time.sleep(2)
