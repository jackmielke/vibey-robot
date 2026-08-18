#!/usr/bin/env python3
"""
reachy_gestures.py — Vibey waves back.

Watches the camera for hand gestures and answers them with the body:

    you wave your LEFT hand   → Vibey waves its RIGHT antenna
    you wave your RIGHT hand  → Vibey waves its LEFT antenna
    you throw a peace sign    → both antennas snap up into a V

The side-swap is the point. Facing someone, the hand they raise is on the
same side of the shared space as the antenna that answers it, so it reads
like a mirror rather than like a robot playing back a recording.

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
    GESTURE_HAND_FLIP=0   set 1 if the sides come out backwards (see below)
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
HOLD = int(os.environ.get("GESTURE_HOLD", "3"))
MIN_SCORE = float(os.environ.get("GESTURE_MIN_SCORE", "0.6"))

# MediaPipe reports handedness assuming the image is MIRRORED (selfie view).
# The robot's camera is not mirrored — it sees you straight on — so the label
# it gives is already the opposite of the hand you actually raised. We then
# want to answer on the opposite side again (your left hand → Vibey's right
# antenna). The two inversions cancel, so the raw label maps straight through:
# mediapipe "Left" → wave_left. If it ever comes out backwards on a different
# camera or a mirrored feed, flip it with GESTURE_HAND_FLIP=1 rather than
# editing this logic.
HAND_FLIP = os.environ.get("GESTURE_HAND_FLIP", "0") not in ("0", "false", "no")

STATE = {
    "on": os.environ.get("GESTURE_ON", "1") not in ("0", "false", "no"),
    "last": None,          # last gesture fired
    "last_at": 0.0,
    "seen": 0,             # how many gestures fired since start
    "hands": 0,            # hands visible in the most recent frame
    "raw": None,           # most recent raw classification, for calibration
}


# --------------------------------------------------------------------------- #
# Gesture → move
# --------------------------------------------------------------------------- #
def _emote_for(gesture: str, handedness: str) -> str | None:
    """Map a mediapipe class + handedness onto one of Vibey's moves."""
    if gesture == "Victory":
        return "peace"
    if gesture == "Open_Palm":
        hand = handedness
        if HAND_FLIP:
            hand = "Left" if hand == "Right" else "Right"
        return "wave_left" if hand == "Left" else "wave_right"
    if gesture == "Thumb_Up":
        return "happy"
    return None


def _fire(emote: str) -> None:
    STATE["last"] = emote
    STATE["last_at"] = time.time()
    STATE["seen"] += 1
    print(f"[gesture] → {emote}", flush=True)
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
            if streak >= HOLD and time.time() - STATE["last_at"] >= COOLDOWN:
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
