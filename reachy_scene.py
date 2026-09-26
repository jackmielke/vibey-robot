#!/usr/bin/env python3
"""
reachy_scene.py — what the room is doing, in one sentence.

Vibey could already recognise faces (reachy_memory.py) but had no idea what was
actually HAPPENING in front of it: someone eating lunch, a box of parts open on
the desk, the lights off. This asks a vision model that question about a single
fresh camera frame and hands back one short spoken line.

Two ways in, both from the voice brain's tools — there is no HTTP surface here
and nothing runs on its own at startup:

    look()              one frame, one description, right now
    watch(True)         re-describe every POLL_SECONDS while it lasts

What this deliberately does NOT do, because a desk robot that quietly narrates a
room all day is a camera in a house, not a friend:

  * Watching is OFF until somebody asks for it, and it expires by itself
    (MAX_WATCH_MINUTES) so it can never be left running and forgotten.
  * It follows the same "stop looking at me" switch as face tracking — if
    FACE_DETECTION is off, this refuses too and any running watch stops.
  * Nothing is written to disk. The JPEG is used for one request and dropped;
    only the newest sentence (and the one before it, to notice change) is kept
    in memory, and both are cleared when watching stops.
  * The model is told to describe the scene, not the people: no identifying, no
    reading private text off screens or paper, no comments on appearance.
  * Frames go up at detail="low" — enough for "two people at a table", not
    enough to be a surveillance still, and cheap enough to poll.

Env:
    CAM_URL          default http://localhost:8771   (reachy_camera.py)
    OPENAI_API_KEY   required
    SCENE_MODEL      default gpt-4o-mini
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

CAM_URL = os.environ.get("CAM_URL", "http://localhost:8771").rstrip("/")
SCENE_MODEL = os.environ.get("SCENE_MODEL", "gpt-4o-mini")

POLL_SECONDS = 10.0
# How long a description is still "now" — a second look inside this window
# reuses the last answer instead of paying for a near-identical frame.
FRESH_FOR = 8.0
# A watch always ends. Asked for longer than this, it still ends at this.
MAX_WATCH_MINUTES = 15.0

PROMPT = (
    "You are the eyes of a small desk robot. In ONE short sentence, say what is "
    "happening in this room right now — the activity, the objects, the light, "
    "the mood. Speak plainly, as you would to the person sitting there.\n"
    "Rules you must not break:\n"
    "- Describe the scene, not the people. Never identify anyone, never guess "
    "age, gender, race, health or mood from a face, never describe what someone "
    "looks like or is wearing. 'Someone is at the desk' is enough.\n"
    "- Never read out text on a screen, phone, paper or whiteboard, and never "
    "repeat anything that looks private.\n"
    "- If the frame is dark, blurry or empty, just say that.\n"
    "No preamble, no 'I see', under twenty words."
)

_lock = threading.Lock()
_latest: dict | None = None      # {"text": str, "at": float}
_previous_text: str | None = None
_watch_until = 0.0
_watch_thread: threading.Thread | None = None


def _eyes_open() -> tuple[bool, str]:
    """The one switch that outranks everything here: if somebody has told Vibey
    to stop watching, looking harder is exactly the wrong thing to do."""
    try:
        import reachy_openai_realtime as rt
        if not rt.FACE_DETECTION.get("on", True):
            return False, "my eyes are switched off — ask me to look again first"
    except Exception:  # noqa: BLE001 - imported from the brain itself, usually fine
        pass
    return True, ""


def _grab_frame() -> tuple[bytes | None, str]:
    """A FRESH frame or nothing. reachy_camera answers 503 for a stale one, so a
    failure here really does mean Vibey cannot see, not that it saw earlier."""
    try:
        with urllib.request.urlopen(f"{CAM_URL}/frame.jpg", timeout=6) as r:
            return r.read(), ""
    except urllib.error.HTTPError:
        return None, "my camera isn't giving me a fresh picture right now"
    except Exception:  # noqa: BLE001
        return None, "my camera isn't running right now"


def _describe(jpeg: bytes) -> tuple[str | None, str]:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None, "I don't have a vision key set up"
    uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    body = json.dumps({
        "model": SCENE_MODEL,
        "max_tokens": 60,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                # detail=low: one small tile per frame. Cheap enough to poll,
                # coarse enough that it stays a description and not a photo.
                {"type": "image_url",
                 "image_url": {"url": uri, "detail": "low"}},
            ],
        }],
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read() or b"{}")
        text = " ".join(
            (data["choices"][0]["message"]["content"] or "").split()).strip()
        return (text or None), ("" if text else "I couldn't make sense of what I saw")
    except Exception as e:  # noqa: BLE001
        return None, f"I couldn't look properly ({e})"


def look(force: bool = False) -> str:
    """One sentence about the room, now. Never raises — the caller is a voice."""
    global _latest, _previous_text
    ok, why = _eyes_open()
    if not ok:
        return why
    with _lock:
        cached = _latest
    if cached and not force and time.time() - cached["at"] < FRESH_FOR:
        return cached["text"]
    jpeg, why = _grab_frame()
    if not jpeg:
        return why
    text, why = _describe(jpeg)
    if not text:
        return why
    with _lock:
        _previous_text = _latest["text"] if _latest else None
        _latest = {"text": text, "at": time.time()}
    return text


def _watch_loop():
    global _watch_thread
    while True:
        # Sleep first: `watch()` already took the opening look, and two frames a
        # second apart is a wasted call, not a second opinion.
        time.sleep(POLL_SECONDS)
        with _lock:
            running = time.time() < _watch_until
        if not running:
            break
        ok, _ = _eyes_open()
        if not ok:
            # Told to stop watching people while a watch was running: the watch
            # is the thing being objected to, so it goes, not just the faces.
            with _lock:
                _stop_watch_locked()
            break
        look(force=True)
    with _lock:
        if _watch_thread is threading.current_thread():
            _watch_thread = None


def _stop_watch_locked():
    """Caller holds _lock. Forgetting the descriptions is the point: when
    watching is over there is no reason to still be holding a picture of the
    room in a variable."""
    global _watch_until, _latest, _previous_text
    _watch_until = 0.0
    _latest = None
    _previous_text = None


def watch(enabled: bool, minutes: float | None = None) -> str:
    """Start or stop the every-ten-seconds look. Returns a sayable line."""
    global _watch_until, _watch_thread
    if not enabled:
        with _lock:
            was = _watch_until > time.time()
            _stop_watch_locked()
        return "I've stopped keeping an eye on the room" if was else "I wasn't watching"
    ok, why = _eyes_open()
    if not ok:
        return why
    try:
        mins = MAX_WATCH_MINUTES if minutes is None else max(1.0, float(minutes))
    except (TypeError, ValueError):  # a heard number that wasn't one
        mins = MAX_WATCH_MINUTES
    capped = mins > MAX_WATCH_MINUTES
    mins = min(mins, MAX_WATCH_MINUTES)
    with _lock:
        _watch_until = time.time() + mins * 60.0
        if _watch_thread is None:
            _watch_thread = threading.Thread(target=_watch_loop, daemon=True)
            _watch_thread.start()
    first = look(force=True)
    tail = (f" I'll keep looking for {mins:.0f} minutes, then stop on my own."
            if not capped else
            f" I'll keep looking for {mins:.0f} minutes — that's my limit — "
            "then stop on my own.")
    return first + tail


def status() -> dict:
    with _lock:
        left = max(0.0, _watch_until - time.time())
        return {
            "watching": left > 0,
            "minutes_left": round(left / 60.0, 1),
            "latest": _latest["text"] if _latest else None,
            "previous": _previous_text,
        }


if __name__ == "__main__":  # manual poke: python3 reachy_scene.py
    print(look(force=True))
