#!/usr/bin/env python3
"""
reachy_gestures.py — Vibey waves back.

Watches the camera for hand gestures and answers them with the body:

    open palm, LEFT hand    → Vibey waves its RIGHT antenna
    open palm, RIGHT hand   → Vibey waves its LEFT antenna
    peace sign              → both antennas snap up into a V
    "I love you" sign       → puts a beat on and dances to it
    thumbs up / down        → happy / sad

The side-swap is the point, and it goes the way it does because Vibey is
facing you, not standing beside you: the hand you raise is across from you,
so it comes back on the opposite antenna. Same-side would read like a
recording being played back; opposite-side reads like someone waving back.

Runs in its OWN venv (.venv-gestures) because mediapipe pins numpy<2 and the
robot SDK requires numpy>=2.2.5 — installing both in reachy_env breaks the
camera and mic bridges. Nothing here imports the SDK: frames come from the
existing camera bridge over HTTP, and motion goes out over the daemon's HTTP
API, so this process is decoupled from the robot connection entirely.

    .venv-gestures/bin/python3 reachy_gestures.py

    :8776/state    {"on":bool,"last":str,"seen":int,...}
    :8776/toggle   POST {"on":true|false}

Env:
    GESTURE_ON=1          start armed (default 1)
    GESTURE_FPS=8         frames per second to sample
    GESTURE_COOLDOWN=4.0  seconds to wait after firing before firing again
    GESTURE_HOLD=3        consecutive frames needed to accept a gesture
    GESTURE_MIN_SCORE=0.6 minimum classifier confidence
    GESTURE_HAND_FLIP=0   set 1 only for a mirrored/selfie feed (see below)
    GESTURE_DANCE_SECONDS=12  how long the fist-pump track runs
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

import reachy_emotes

CAM_URL = os.environ.get("CAM_URL", "http://localhost:8771")
PORT = int(os.environ.get("GESTURE_PORT", "8776"))
MODEL = os.environ.get("GESTURE_MODEL", "models/gesture_recognizer.task")
FPS = float(os.environ.get("GESTURE_FPS", "8"))
COOLDOWN = float(os.environ.get("GESTURE_COOLDOWN", "4.0"))
DANCE_SECONDS = float(os.environ.get("GESTURE_DANCE_SECONDS", "12"))
HOLD = int(os.environ.get("GESTURE_HOLD", "3"))
MIN_SCORE = float(os.environ.get("GESTURE_MIN_SCORE", "0.6"))

# Which antenna answers which hand.
#
# MediaPipe labels a hand as the person's OWN hand, and on this camera that
# label is simply correct — waving a right hand reports "Right". (An earlier
# version reasoned from MediaPipe's "assumes a mirrored image" note that the
# label would arrive inverted here and would cancel against the mirrored
# response. It doesn't: nothing inverts it, so the wave came back same-side.)
#
# Vibey answers like a person facing you rather than a recording played back:
# the hand you raise is on the far side of the shared space from where it sits
# on you, so your RIGHT hand comes back on Vibey's LEFT antenna.
_MIRROR = {"Right": "wave_left", "Left": "wave_right"}
# Set 1 only for a mirrored/selfie feed, where the label really is flipped.
HAND_FLIP = os.environ.get("GESTURE_HAND_FLIP", "0") not in ("0", "false", "no")

STATE = {
    "on": os.environ.get("GESTURE_ON", "1") not in ("0", "false", "no"),
    "last": None,          # last gesture fired
    "last_at": 0.0,
    "seen": 0,             # how many gestures fired since start
    "hands": 0,            # hands visible in the most recent frame
    "raw": None,           # most recent raw classification, for calibration
    "cooldown": COOLDOWN,  # of the move last fired — the dance needs longer
}


# --------------------------------------------------------------------------- #
# Gesture → move
# --------------------------------------------------------------------------- #
# How long to ignore new gestures after firing one. Mostly this just stops a
# held pose retriggering; the dance is long enough to need its own, or a fist
# held through the whole track restarts it repeatedly.
COOLDOWNS = {"dance": DANCE_SECONDS + 2.0}


def _emote_for(gesture: str, handedness: str) -> str | None:
    """Map a mediapipe class + handedness onto one of Vibey's moves."""
    if gesture == "Open_Palm":
        hand = handedness
        if HAND_FLIP:
            hand = "Left" if hand == "Right" else "Right"
        return _MIRROR.get(hand)
    if gesture == "Victory":
        return "peace"
    # The ASL "I love you" sign — pinky, index and thumb out. MediaPipe's own
    # name for it; it's the peace-and-love hand, and it starts the music.
    if gesture == "ILoveYou":
        return "dance"
    if gesture == "Thumb_Up":
        return "happy"
    if gesture == "Thumb_Down":
        return "sad"
    # Closed_Fist is deliberately unmapped: a resting hand on a desk or around
    # a mouse reads as a fist all day, and it used to start the track.
    return None


def _fire(emote: str) -> None:
    STATE["last"] = emote
    STATE["last_at"] = time.time()
    STATE["cooldown"] = COOLDOWNS.get(emote, COOLDOWN)
    STATE["seen"] += 1
    print(f"[gesture] → {emote}", flush=True)
    if emote == "dance":
        # Not an emote: a synthesized beat uploaded to the robot's speaker
        # plus a groove for as long as it plays.
        reachy_emotes.play_dance(DANCE_SECONDS)
    else:
        reachy_emotes.play(emote)


# --------------------------------------------------------------------------- #
# Watcher
# --------------------------------------------------------------------------- #
def _frame() -> "np.ndarray | None":
    try:
        raw = urllib.request.urlopen(f"{CAM_URL}/frame.jpg", timeout=4).read()
    except Exception:
        return None
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)


def watch() -> None:
    recognizer = vision.GestureRecognizer.create_from_options(
        vision.GestureRecognizerOptions(
            base_options=BaseOptions(model_asset_path=MODEL),
            running_mode=vision.RunningMode.IMAGE,
            num_hands=2))
    print(f"[gesture] watching {CAM_URL}/frame.jpg at {FPS:g}fps "
          f"(hold {HOLD} frames, cooldown {COOLDOWN:g}s)", flush=True)

    period = 1.0 / FPS
    streak_key: str | None = None
    streak = 0
    warned = False

    while True:
        t0 = time.time()
        if not STATE["on"]:
            time.sleep(0.3)
            continue

        bgr = _frame()
        if bgr is None:
            if not warned:
                print("[gesture] camera bridge unreachable — retrying", flush=True)
                warned = True
            time.sleep(1.5)
            continue
        warned = False

        img = mp.Image(image_format=mp.ImageFormat.SRGB,
                       data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        try:
            res = recognizer.recognize(img)
        except Exception as e:  # noqa: BLE001
            print(f"[gesture] recognize failed: {e}", flush=True)
            time.sleep(1.0)
            continue

        STATE["hands"] = len(res.gestures or [])

        # Pick the most confident recognised gesture in the frame.
        best = None
        for cats, hands in zip(res.gestures or [], res.handedness or []):
            if not cats or not hands:
                continue
            g, h = cats[0], hands[0]
            if g.category_name in ("None", "") or g.score < MIN_SCORE:
                continue
            if best is None or g.score > best[0].score:
                best = (g, h)

        if best is None:
            streak_key, streak = None, 0
            STATE["raw"] = None
        else:
            g, h = best
            STATE["raw"] = f"{g.category_name}/{h.category_name} {g.score:.2f}"
            key = f"{g.category_name}:{h.category_name}"
            streak = streak + 1 if key == streak_key else 1
            streak_key = key

            # Require the gesture to persist, so a hand passing through a
            # pose on its way somewhere else doesn't set the robot off.
            if streak >= HOLD and (time.time() - STATE["last_at"]
                                   >= STATE.get("cooldown", COOLDOWN)):
                emote = _emote_for(g.category_name, h.category_name)
                if emote:
                    _fire(emote)
                    streak_key, streak = None, 0

        time.sleep(max(0.0, period - (time.time() - t0)))


# --------------------------------------------------------------------------- #
# Control API
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/state"):
            self._json(STATE)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.startswith("/toggle"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n)) if n else {}
                STATE["on"] = bool(body.get("on", not STATE["on"]))
                self._json({"ok": True, "on": STATE["on"]})
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 400)
        else:
            self._json({"error": "not found"}, 404)


def main() -> None:
    if not os.path.exists(MODEL):
        raise SystemExit(f"[gesture] model missing: {MODEL}")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[gesture] control API on http://localhost:{PORT}", flush=True)
    watch()


if __name__ == "__main__":
    main()
