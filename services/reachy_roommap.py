"""Vibey's room map: a rough top-down picture of where things are.

Vibey sits on the table like a lighthouse. A scan turns its head and body
through a few angles, grabs a frame at each, and asks a vision model what it
sees, where across the frame, and roughly how far. Angle plus distance puts
each thing on a flat map with Vibey at the middle. The rover's spot comes from
its own wheel commands (dead reckoning, drifts) and snaps to where a scan
actually sees it.

Map frame, in metres: Vibey's base is (0, 0), +y is straight ahead of Vibey
when its body is centred, +x is Vibey's right. Bearings and headings are
degrees clockwise from +y, so 90 is Vibey's right.

The map lives in data/roommap.json (private, like faces). Same privacy rules
as `look`: camera off or privacy on means no scanning.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import threading
import time
import urllib.request

import reachy_rover

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(ROOT, "data", "roommap.json")
REACHY_URL = os.environ.get("REACHY_URL", "http://192.168.12.240:8000").rstrip("/")
MAP_MODEL = os.environ.get("MAP_MODEL", "gpt-4.1-mini")
# Camera's horizontal field of view. A guess until measured: tape two things
# at the frame edges and check the angle between them.
HFOV = float(os.environ.get("MAP_HFOV", "70"))
# Where a scan looks: body+head yaw in degrees (positive = Vibey's left).
SCAN_YAWS = [80, 40, 0, -40, -80]
SCAN_PITCH = 0.12        # radians, a little down so the floor is in view
SETTLE = 1.1             # seconds after a turn before the frame is trusted
MERGE_M = 0.8            # same name within this distance = the same thing
DROP_AFTER = 2           # scans that miss a thing before it's forgotten

# Dead reckoning, measured roughly on the real rover (2026-10-21): about 0.2 m/s
# at medium (wheel power 0.30), and in-place spins ~25 deg/s at power 0.4.
M_PER_S_PER_POWER = float(os.environ.get("MAP_M_PER_POWER", "0.67"))
DEG_PER_S_PER_POWER = float(os.environ.get("MAP_DEG_PER_POWER", "62"))

_mu = threading.Lock()
_scan_busy = threading.Lock()
_state = {"scanning": False, "step": "", "last": ""}


def _blank() -> dict:
    return {"objects": [], "rover": None, "scans": 0, "updated": 0.0,
            "hfov": HFOV}


def _load() -> dict:
    try:
        with open(MAP_PATH) as f:
            m = json.load(f)
        if isinstance(m, dict) and isinstance(m.get("objects"), list):
            return {**_blank(), **m}
    except (OSError, ValueError):
        pass
    return _blank()


_map = _load()


def _save() -> None:
    try:
        import vibey_files
        os.makedirs(os.path.dirname(MAP_PATH), exist_ok=True)
        vibey_files.atomic_write_json(MAP_PATH, _map, indent=1)
    except Exception as e:  # noqa: BLE001
        print(f"[roommap] save failed: {e}", flush=True)


def get() -> dict:
    with _mu:
        return {**json.loads(json.dumps(_map)), "status": dict(_state),
                "riding": reachy_rover.RIDING}


def clear() -> str:
    global _map
    with _mu:
        _map = _blank()
        _save()
    return "forgot the room map"


def _norm(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


# ---- the rover's spot ----------------------------------------------------------
def _on_motion(left: float, right: float, seconds: float) -> None:
    """reachy_rover calls this for every stretch of wheel power."""
    if not (left or right) or seconds <= 0:
        return
    with _mu:
        r = _map.get("rover")
        if not r:
            return                        # nowhere to start from until a scan sees it
        fwd = (left + right) / 2 * M_PER_S_PER_POWER * seconds
        # Right wheel faster turns left, which is counter-clockwise.
        turn = -(right - left) / 2 * DEG_PER_S_PER_POWER * seconds
        h0 = r["heading"]
        h = _norm(h0 + turn)
        mid = math.radians(h0 + turn / 2)
        r["x"] = round(r["x"] + fwd * math.sin(mid), 3)
        r["y"] = round(r["y"] + fwd * math.cos(mid), 3)
        r["heading"] = round(h, 1)
        r["source"] = "odometry"
        r["at"] = time.time()
        r["drift_m"] = round(r.get("drift_m", 0) + abs(fwd) + abs(turn) / 90 * 0.1, 2)


if _on_motion not in reachy_rover.on_motion:
    reachy_rover.on_motion.append(_on_motion)


def _observer() -> tuple[float, float, float]:
    """Where the camera stands and faces: Vibey's base, or the rover if riding."""
    r = _map.get("rover")
    if reachy_rover.RIDING and r:
        return r["x"], r["y"], r["heading"]
    return 0.0, 0.0, 0.0


# ---- looking ----------------------------------------------------------------
_PROMPT = (
    "You are Vibey, a small robot{where}. This photo is from your camera. "
    "List the main things in view that someone might drive to or ask about: "
    "furniture, doors, windows, plants, people, pets, bags, boxes, screens. "
    "Skip walls, floor and ceiling. For each: a short plain name a person "
    "would say (\"couch\", \"blue chair\", \"door\"), x = where its middle is "
    "across the photo from 0 (left edge) to 1 (right edge), and dist_m = your "
    "best guess of how far it is from the camera in metres, judged from its "
    "size and where it meets the floor (a door is 2 m tall, a chair seat is "
    "45 cm high). At most 8 things, the clearest first.\n"
    "{rover}"
    "Answer ONLY JSON: {{\"objects\": [{{\"name\": \"couch\", \"x\": 0.3, "
    "\"dist_m\": 2.5}}], \"rover\": null}}"
)
_ROVER_Q = (
    "Also look for a little black four-wheeled rover car (shoebox size, small "
    "screen and lights at its front). If you see it, set rover to {\"x\": 0.5, "
    "\"dist_m\": 1.2, \"facing\": \"away\"} where facing is which way its front "
    "points as you see it: away, toward (you), left or right (of the photo). "
    "Otherwise rover is null. Don't list the rover in objects.\n")


def _goto(yaw_deg: float, duration: float = 0.8, pitch: float = SCAN_PITCH) -> None:
    y = math.radians(yaw_deg)
    body = {"head_pose": {"x": 0, "y": 0, "z": 0, "roll": 0, "pitch": pitch, "yaw": y},
            "body_yaw": y, "antennas": None, "duration": duration}
    req = urllib.request.Request(
        f"{REACHY_URL}/api/move/goto", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=6).read()


def _tracking(on: bool) -> None:
    try:
        req = urllib.request.Request(
            f"{REACHY_URL}/api/media/tracking/{'enable' if on else 'disable'}",
            data=b"", method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=6).read()
    except Exception:  # noqa: BLE001
        pass


def _tracking_wanted() -> bool:
    try:
        import reachy_privacy
        if not reachy_privacy.camera_on():
            return False
        import reachy_openai_realtime as rt
        return bool(rt.FACE_DETECTION.get("on", True))
    except Exception:  # noqa: BLE001
        return True


def _ask(jpeg: bytes) -> dict:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("no OPENAI_API_KEY for vision")
    riding = reachy_rover.RIDING
    prompt = _PROMPT.format(
        where=" riding on a little rover car" if riding else " sitting on a table",
        rover="" if riding else _ROVER_Q)
    body = json.dumps({
        "model": MAP_MODEL,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(),
                "detail": "auto"}},
        ]}],
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read() or b"{}")
    try:
        import reachy_cost
        reachy_cost.record_tokens("vision", data.get("model") or MAP_MODEL,
                                  data.get("usage") or {})
    except Exception:  # noqa: BLE001
        pass
    return json.loads(data["choices"][0]["message"]["content"] or "{}")


def _place(x_frac, dist, look_deg: float, ox: float, oy: float):
    """Frame position + distance -> map point and bearing from the observer."""
    try:
        xf = max(0.0, min(1.0, float(x_frac)))
        d = max(0.2, min(12.0, float(dist)))
    except (TypeError, ValueError):
        return None
    bearing = _norm(look_deg + (xf - 0.5) * HFOV)
    b = math.radians(bearing)
    return round(ox + d * math.sin(b), 2), round(oy + d * math.cos(b), 2), bearing


def _merge(seen: list[dict]) -> None:
    """Fold one scan into the map: average repeats, age out what's gone."""
    objs = _map["objects"]
    hit = set()
    for s in seen:
        best, best_d = None, MERGE_M
        for i, o in enumerate(objs):
            if i in hit or _slug(o["name"]) != _slug(s["name"]):
                continue
            dd = math.hypot(o["x"] - s["x"], o["y"] - s["y"])
            if dd <= best_d:
                best, best_d = i, dd
        if best is None:
            objs.append({**s, "seen": 1, "missed": 0})
            hit.add(len(objs) - 1)
        else:
            o = objs[best]
            w = min(o["seen"], 4)            # older sightings count, but not forever
            o["x"] = round((o["x"] * w + s["x"]) / (w + 1), 2)
            o["y"] = round((o["y"] * w + s["y"]) / (w + 1), 2)
            o.update(seen=o["seen"] + 1, missed=0, at=s["at"])
            hit.add(best)
    for i, o in enumerate(objs):
        if i not in hit:
            o["missed"] = o.get("missed", 0) + 1
    _map["objects"] = [o for o in objs if o["missed"] < DROP_AFTER][:40]


def scan() -> str:
    """Look around, update the map, return a short summary. Blocks ~10-20 s."""
    import reachy_scene
    ok, why = reachy_scene._eyes_open()
    if not ok:
        return why
    try:
        import reachy_privacy
        if reachy_privacy.is_on():
            return "privacy mode is on, so I can't map the room"
    except Exception:  # noqa: BLE001
        pass
    if not _scan_busy.acquire(blocking=False):
        return "I'm already looking around"
    try:
        _state.update(scanning=True, step="turning", last="")
        _emit("scanning the room")
        with _mu:
            ox, oy, oh = _observer()
        frames: list[tuple[float, bytes]] = []
        _tracking(False)
        try:
            for yaw in SCAN_YAWS:
                _state["step"] = f"looking {'left' if yaw > 0 else 'right' if yaw < 0 else 'ahead'}"
                _goto(yaw)
                time.sleep(SETTLE)
                jpeg, err = reachy_scene._grab_frame()
                if jpeg:
                    frames.append((_norm(oh - yaw), jpeg))
        finally:
            try:
                _goto(0, 1.0, pitch=0.0)
            except Exception:  # noqa: BLE001
                pass
            if _tracking_wanted():
                _tracking(True)
        if not frames:
            return "my camera didn't give me any pictures to map with"
        _state["step"] = f"thinking about {len(frames)} pictures"
        answers: list[dict | None] = [None] * len(frames)
        errors: list[str] = []

        def work(i: int, jpeg: bytes) -> None:
            try:
                answers[i] = _ask(jpeg)
            except Exception as e:  # noqa: BLE001
                errors.append(str(e)[:80])
        ts = [threading.Thread(target=work, args=(i, f[1]), daemon=True)
              for i, f in enumerate(frames)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(40)
        now = time.time()
        seen: list[dict] = []
        rover_hits: list[dict] = []
        for (look, _), ans in zip(frames, answers):
            if not isinstance(ans, dict):
                continue
            for o in (ans.get("objects") or [])[:8]:
                if not isinstance(o, dict) or not str(o.get("name") or "").strip():
                    continue
                p = _place(o.get("x"), o.get("dist_m"), look, ox, oy)
                if p:
                    seen.append({"name": " ".join(str(o["name"]).split())[:40].lower(),
                                 "x": p[0], "y": p[1], "at": now})
            rv = ans.get("rover")
            if isinstance(rv, dict) and not reachy_rover.RIDING:
                p = _place(rv.get("x"), rv.get("dist_m"), look, ox, oy)
                if p:
                    face = str(rv.get("facing") or "away").lower()
                    turn = {"away": 0, "toward": 180, "left": -90, "right": 90}.get(face, 0)
                    rover_hits.append({"x": p[0], "y": p[1],
                                       "heading": round(_norm(p[2] + turn), 1)})
        # Neighbouring views overlap, so one thing can show up twice in a scan.
        uniq: list[dict] = []
        for s in seen:
            if not any(_slug(u["name"]) == _slug(s["name"]) and
                       math.hypot(u["x"] - s["x"], u["y"] - s["y"]) < MERGE_M for u in uniq):
                uniq.append(s)
        with _mu:
            _merge(uniq)
            if rover_hits:
                rv = rover_hits[0]
                _map["rover"] = {**rv, "source": "seen", "at": now, "drift_m": 0.0}
            _map["scans"] = _map.get("scans", 0) + 1
            _map["updated"] = now
            _map["hfov"] = HFOV
            _save()
            names = [o["name"] for o in _map["objects"]]
        bits = [f"{len(uniq)} things this time"]
        if rover_hits:
            bits.append("spotted the rover")
        if errors and len(errors) == len(frames):
            return f"I looked around but couldn't think about the pictures ({errors[0]})"
        summary = (f"mapped the room: {', '.join(names[:12]) or 'nothing clear'}"
                   f" ({'; '.join(bits)})")
        _state["last"] = summary
        _emit(summary)
        return summary
    finally:
        _state.update(scanning=False, step="")
        _scan_busy.release()


def scan_async() -> str:
    if _scan_busy.locked():
        return "already scanning"
    threading.Thread(target=scan, daemon=True).start()
    return "scanning"


# ---- using the map --------------------------------------------------------------
def find(goal: str) -> dict | None:
    """The mapped thing a goal like 'drive to the blue chair' is about."""
    g = " " + _slug(goal) + " "
    with _mu:
        objs = list(_map["objects"])
    best, score = None, 0
    for o in objs:
        n = _slug(o["name"])
        if f" {n} " in g:
            s = len(n) * 10 + o.get("seen", 1)
        else:
            words = [w for w in n.split() if len(w) > 2]
            s = sum(len(w) for w in words if f" {w} " in g)
        if s > score:
            best, score = o, s
    return best


def _clock(deg: float) -> str:
    a = abs(deg)
    if a < 15:
        return "straight ahead"
    if a > 160:
        return "right behind it"
    side = "right" if deg > 0 else "left"
    return f"about {round(a / 5) * 5:.0f} degrees to its {side}"


def hint_for(goal: str) -> str:
    """A line the camera pilot can use: where the goal is from the rover."""
    o = find(goal)
    if not o:
        return ""
    with _mu:
        r = dict(_map["rover"]) if _map.get("rover") else None
    if not r:
        return (f"Map hint: the {o['name']} is about "
                f"{math.hypot(o['x'], o['y']):.1f} m from you (Vibey).")
    dx, dy = o["x"] - r["x"], o["y"] - r["y"]
    d = math.hypot(dx, dy)
    rel = _norm(math.degrees(math.atan2(dx, dy)) - r["heading"])
    sure = "rough" if r.get("source") != "seen" or r.get("drift_m", 0) > 1 else "fair"
    return (f"Map hint ({sure} guess): the {o['name']} is about {d:.1f} m from the "
            f"rover, {_clock(rel)}. Trust what you see over this.")


def describe() -> str:
    """The map in words, for the voice."""
    with _mu:
        objs = list(_map["objects"])
        r = _map.get("rover")
        scans = _map.get("scans", 0)
    if not objs:
        return "I haven't mapped the room yet; ask me to look around first"
    parts = []
    for o in sorted(objs, key=lambda o: math.hypot(o["x"], o["y"]))[:12]:
        b = math.degrees(math.atan2(o["x"], o["y"]))
        where = ("ahead" if abs(b) < 20 else "behind" if abs(b) > 150 else
                 ("to my right" if b > 0 else "to my left"))
        parts.append(f"{o['name']} {where}, {math.hypot(o['x'], o['y']):.1f} m")
    out = "; ".join(parts)
    if r:
        out += (f". The rover is about {math.hypot(r['x'], r['y']):.1f} m away"
                f" ({'seen' if r.get('source') == 'seen' else 'estimated from its moves'})")
    return f"From {scans} look-around{'s' if scans != 1 else ''}: {out}."


def _emit(text: str) -> None:
    try:
        import reachy_events
        reachy_events.emit("action", text, source="roommap", icon="🗺️")
    except Exception:  # noqa: BLE001
        pass


TOOL = {
    "type": "function",
    "name": "room_map",
    "description": (
        "Your map of the room. action=scan: turn your head and body to look "
        "all around and update the map (takes ~15 s and moves you, so say so "
        "first). action=list: say what's on the map and roughly where, from "
        "memory, without moving. action=forget: wipe the map. Use scan when "
        "asked to look around, learn the room or find where things are; list "
        "for 'where's the couch' if the map has it. drive_to already uses the "
        "map when the goal is on it."),
    "parameters": {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["scan", "list", "forget"]}},
        "required": ["action"],
    },
}


if __name__ == "__main__":
    import sys
    cmd = (sys.argv[1:] or ["show"])[0]
    if cmd == "scan":
        print(scan())
    elif cmd == "forget":
        print(clear())
    else:
        print(describe())
        print(json.dumps(get(), indent=1))
