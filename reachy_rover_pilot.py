"""Vibey drives the rover by looking: frame, decide, short move, repeat.

Each step grabs a fresh frame from the camera bridge (:8771), asks a vision
model for ONE short move toward the goal, drives it, and looks again. It stops
when the model says it's there, when it says stop (something in the way, lost
sight of the rover), after MAX_STEPS, or the moment anyone presses a control or
says stop (reachy_rover.interrupts changes).

Two points of view, picked by ROVER_RIDING like everything else:
  * table (default): Vibey sits still and watches the rover on the floor, so
    the model steers a car it sees from outside, judging the car's heading.
  * riding: the camera rides on the rover, so the frame IS the view ahead.

Same privacy rules as `look`: camera off or privacy on means no driving by eye.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request

import reachy_rover

PILOT_MODEL = os.environ.get("PILOT_MODEL", "gpt-4.1-mini")
MAX_STEPS = 10
STEP_MAX_SECONDS = 1.5

_busy = threading.Lock()
_state = {"goal": None, "step": 0, "last": "", "running": False}

_VIEW = {
    False: (
        "You are Vibey, a small robot sitting still on a table. You are remote-"
        "controlling a little black four-wheeled rover car (about shoebox size) "
        "on the floor in front of you, and this photo is from your own camera. "
        "Judge which way the rover is facing (its front is the end with the "
        "small screen and lights) and steer it toward the goal. 'left' and "
        "'right' are the ROVER's own left and right, not yours: if it faces you, "
        "they are mirrored. If you can't see the rover at all, answer stop."),
    True: (
        "You are Vibey, a small robot riding on top of a little rover car. This "
        "photo is from your camera, looking forward over the car: it is exactly "
        "what is ahead of you. Steer toward the goal. Things in the bottom of "
        "the frame are close."),
}

_RULES = (
    "\nGoal: {goal}\nStep {step} of {max}. Previous moves: {history}\n"
    "Pick ONE short move. Moves: forward, back, left (gentle arc), right "
    "(gentle arc), spin_left, spin_right (turn in place), stop. seconds is "
    "0.3 to 1.5: about 1 s at this speed rolls roughly 20 cm, and a 0.5 s spin "
    "turns roughly 45 degrees. Prefer turning to face the goal first, then "
    "short forward moves. If anything is close in the way, a person, a pet, a "
    "table edge or stairs, say stop. When the goal is reached, done=true and "
    "action=stop.\nAnswer ONLY JSON: {{\"action\": \"...\", \"seconds\": 0.8, "
    "\"done\": false, \"note\": \"what you see and why, under 15 words\"}}"
)


def _decide(jpeg: bytes, goal: str, step: int, history: list[str]) -> dict:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("no OPENAI_API_KEY for vision")
    prompt = _VIEW[reachy_rover.RIDING] + _RULES.format(
        goal=goal, step=step, max=MAX_STEPS,
        history=", ".join(history[-5:]) or "none yet")
    body = json.dumps({
        "model": PILOT_MODEL,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(),
                "detail": "low"}},
        ]}],
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read() or b"{}")
    try:
        import reachy_cost
        reachy_cost.record_tokens("vision", data.get("model") or PILOT_MODEL,
                                  data.get("usage") or {})
    except Exception:  # noqa: BLE001
        pass
    out = json.loads(data["choices"][0]["message"]["content"] or "{}")
    action = str(out.get("action", "stop")).strip().lower().replace(" ", "_")
    if action not in reachy_rover.ACTIONS:
        action = "stop"
    try:
        secs = max(0.3, min(float(out.get("seconds") or 0.8), STEP_MAX_SECONDS))
    except (TypeError, ValueError):
        secs = 0.8
    return {"action": action, "seconds": secs, "done": bool(out.get("done")),
            "note": " ".join(str(out.get("note") or "").split())[:120]}


def _emit(text: str, detail: dict | None = None) -> None:
    try:
        import reachy_events
        reachy_events.emit("action", text, detail=detail, source="pilot", icon="🛞")
    except Exception:  # noqa: BLE001
        pass


def go(goal: str, max_steps: int = MAX_STEPS) -> str:
    """Drive toward `goal` by eye. Blocks until done; returns a short summary."""
    goal = " ".join((goal or "").split())[:200]
    if not goal:
        return "tell me where to drive"
    if not reachy_rover.ROVER_URL:
        return "my wheels aren't connected yet"
    import reachy_scene
    ok, why = reachy_scene._eyes_open()
    if not ok:
        return why
    try:
        import reachy_privacy
        if reachy_privacy.is_on():
            return "privacy mode is on, so I can't drive by looking"
    except Exception:  # noqa: BLE001
        pass
    if not _busy.acquire(blocking=False):
        return f"I'm already driving toward {_state['goal']}; say stop first"
    try:
        start_mark = reachy_rover.interrupts
        _state.update(goal=goal, step=0, last="", running=True)
        _emit(f"driving by eye: {goal}")
        history: list[str] = []
        for step in range(1, max(1, min(max_steps, MAX_STEPS)) + 1):
            _state["step"] = step
            if reachy_rover.interrupts != start_mark:
                return f"stopped: someone took over after {len(history)} moves"
            jpeg, err = reachy_scene._grab_frame()
            if jpeg is None:
                return f"stopped after {len(history)} moves: {err}"
            try:
                d = _decide(jpeg, goal, step, history)
            except Exception as e:  # noqa: BLE001
                reachy_rover.stop()
                return f"stopped after {len(history)} moves: I couldn't think about the picture ({e})"
            _state["last"] = d["note"]
            _emit(f"{d['action'].replace('_', ' ')} {d['seconds']:.1f}s: {d['note']}", d)
            if reachy_rover.interrupts != start_mark:
                return f"stopped: someone took over after {len(history)} moves"
            if d["done"] or d["action"] == "stop":
                verdict = "made it" if d["done"] else "stopped"
                return f"{verdict} after {len(history)} moves: {d['note']}"
            reachy_rover.drive(d["action"], d["seconds"], "medium")
            history.append(f"{d['action']} {d['seconds']:.1f}s")
            time.sleep(0.3)          # let the frame settle after the stop
        return f"gave up after {len(history)} moves without getting there: {_state['last']}"
    finally:
        _state["running"] = False
        _busy.release()


def go_async(goal: str) -> str:
    """For the app: start a run and return at once."""
    if _busy.locked():
        return f"already driving toward {_state['goal']}"
    threading.Thread(target=lambda: _emit("pilot: " + go(goal)), daemon=True).start()
    return "driving"


def status() -> dict:
    return dict(_state)


TOOL = {
    "type": "function",
    "name": "drive_to",
    "description": (
        "Drive the rover somewhere using your camera: you look, make one short "
        "move, look again, up to ten times. Use it for goals like 'drive to the "
        "couch', 'bring the car to me', 'go around the chair', 'turn to face the "
        "door'. Takes a while (a few seconds per step), so say you're on it "
        "first. It stops the moment anyone says stop or grabs the controls. "
        "Use plain `drive` for simple moves like 'go forward a bit'."),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "Where to go, in plain words."},
            "max_steps": {"type": "integer", "description": "1 to 10, default 10."},
        },
        "required": ["goal"],
    },
}
