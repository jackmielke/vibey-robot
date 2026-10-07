#!/usr/bin/env python3
"""
reachy_memory.py — Vibey remembers faces (stored locally, data/vibey.db).

Polls the camera on a fixed interval, detects *every* face in frame (not just
one), and checks each against everyone Vibey already knows — so two people
in view get recognized simultaneously. Returning people get greeted by name in
Wonder's voice; new people get added to memory.

Earlier versions gated capture behind the robot daemon's own single-target
face tracker (/api/media/tracking/face): wait for it to report "detected",
wait for that to hold steady, then look once. That tracker only follows one
face and drops lock on a pose change — which is why recognition would "lose"
someone who just turned their head. This version ignores it entirely and does
its own full-frame multi-face detection on a steady poll instead.

Each person is modelled as several face-embedding *samples* (table
face_samples), not one reference photo — a single photo is fragile to angle
and lighting, and in practice caused the same person to get re-enrolled as a
"new" face repeatedly (measured: same-person distance ~0.565, just over the
old 0.55 cutoff). Matching is nearest-sample-across-everyone, and every
confident recognition (well inside the match tolerance) automatically banks
a new sample of that person, so the model gets more accurate the more Wonder
sees you — no separate enrollment step needed.

Run in the SDK venv (it shares the camera + needs numpy):

    source reachy_env/bin/activate
    python3 reachy_memory.py

Requires (one-time):
    uv pip install face_recognition       # 128-d face encodings (pulls dlib)

Config (from .env / environment):
    REACHY_URL       default http://192.168.1.120:8000
    CAM_URL          default http://localhost:8771   (reachy_camera.py MJPEG server)
    (storage)        local only: reachy_faces_store.py -> data/vibey.db +
                     data/snapshots/. Supabase is not used for faces any more.
    MATCH_TOLERANCE  default 0.58  (lower = stricter match; measured gap
                                    between same-person and different-person
                                    distances sits around 0.57–0.62)
    LEARN_TOLERANCE  default 0.45  (only auto-bank a new sample when this
                                    confident — keeps the model from drifting)
    MAX_SAMPLES      default 8     (oldest sample is dropped past this cap)
    POLL_INTERVAL    default 1.2   (seconds between full-frame face scans)
    MAX_FACES        default 4     (largest N faces considered per frame)
    MIN_FACE_PX      default 70    (enrollment quality gate: reject faces
                                    shorter than this — too small to embed well)
    MIN_BLUR_VAR     default 45    (enrollment quality gate: reject faces
                                    blurrier than this Laplacian variance)
    SAMPLES_CACHE_TTL default 20   (seconds the in-memory face gallery is
                                    reused before re-reading the local DB)

Name someone Wonder has met:
    python3 reachy_memory.py --name <face_id> "Jack"
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import warnings
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# face_recognition_models still imports the deprecated pkg_resources; quiet it.
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

from reachy_voice import load_env, say  # reuse .env loader + TTS

load_env()

REACHY_URL = os.environ.get("REACHY_URL", "http://192.168.1.120:8000").rstrip("/")
CAM_URL = os.environ.get("CAM_URL", "http://localhost:8771").rstrip("/")


def _tell_the_conversation(line: str, silent: bool = False) -> bool:
    """Push a sighting into the live conversation. True if it was taken.

    The point is that Vibey mentions it, in its own voice, in the middle of
    whatever is being said — not that a second system reads out a fixed sentence
    in a different accent while the first one is mid-reply.
    """
    try:
        body = json.dumps({"text": f"[Nobody said this out loud. {line}]",
                           "silent": silent}).encode()
        req = urllib.request.Request("http://localhost:8772/sighting", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read() or b"{}").get("delivered") == "realtime"
    except Exception:  # noqa: BLE001
        return False


def _realtime_has_the_floor() -> bool:
    """True when the OpenAI realtime engine is holding the conversation, or the
    robot is asleep. Either way this service must not speak: two minds answering
    in two voices is what it sounded like from the room."""
    try:
        with urllib.request.urlopen("http://localhost:8772/state", timeout=2) as r:
            st = json.loads(r.read())
        return bool(st.get("openai") or st.get("asleep"))
    except Exception:
        return False
import reachy_faces_store as store  # faces, samples, journal: local SQLite
# 0.50, not 0.58. Measured against every sample in the database, using the live
# rule (nearest sample across everyone):
#
#   tol 0.50 -> 94% correct, 0% misidentified, 6% "someone new"
#   tol 0.58 -> 95% correct, 2% MISIDENTIFIED, 3% "someone new"
#
# One extra percent of recognition is not worth calling somebody by the wrong
# name. In a room full of people those two failures are not comparable: being
# asked your name again is nothing, and being greeted as somebody else is the
# thing everyone remembers. The gap in the data is real — same-person distances
# sit at a median of 0.44 and different-person distances start at 0.535 — so
# 0.50 lands inside it rather than on top of it.
MATCH_TOLERANCE = float(os.environ.get("MATCH_TOLERANCE", "0.50"))
LEARN_TOLERANCE = float(os.environ.get("LEARN_TOLERANCE", "0.45"))
MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "8"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.2"))
MAX_FACES = int(os.environ.get("MAX_FACES", "4"))
# Enrollment quality gate — a new identity (and every auto-banked sample) must
# clear these, so a blurry, dark, or tiny detection never becomes a permanent
# "person" that then widens the false-match net for everyone else. These gate
# ENROLLMENT only; recognition of already-known people is unaffected, so a
# known face far across the room still gets greeted — it just won't spawn a
# new identity or bank a junk sample.
# Sizes are tuned to THIS rig: measured live, a real person at normal desk
# interaction distance renders ~36px tall and still embeds/matches confidently
# (d~0.39), so the floor sits just under that — enough to reject sub-30px
# background specks and faces-on-a-TV-across-the-room without blocking organic
# enrollment of someone actually talking to the robot. Blur is the primary
# junk filter (a sharp interaction-distance face measures ~140+ variance).
# 70, which is what this file's own documentation has always claimed. The code
# said 32, and a 32-pixel face is a handful of blurred pixels — the embedding
# from one is nearly meaningless, and banking it as a sample poisons that
# person's identity for every future match. Enrol from good looks at somebody or
# do not enrol.
MIN_FACE_PX = int(os.environ.get("MIN_FACE_PX", "70"))
MIN_BLUR_VAR = float(os.environ.get("MIN_BLUR_VAR", "45"))
MAX_BLOCKINESS = float(os.environ.get("MAX_BLOCKINESS", "2.5"))

GREET_COOLDOWN = 600.0    # don't re-greet a named person within this window (10 min)
LEARN_COOLDOWN = 45.0     # min time between auto-banked samples per person
PERSON_FRESH_S = 6.0      # how long a detection stays "currently visible"
MEM_PORT = int(os.environ.get("MEM_PORT", "8773"))

# Live recognition state, shared with the dashboard via the control API.
# A list because more than one face can be in frame at once — each entry is
# {face_id, name, x, y, ts}, x/y normalized to roughly [-1, 1] around center
# (same convention the robot's own tracker used, so the dashboard overlay
# math didn't need to change).
CURRENT_PEOPLE: list[dict] = []
_current_lock = threading.Lock()

# Sleep switch — while paused the loop skips detection and greetings entirely
# (set from the dashboard's power button via POST /pause).
PAUSED = {"on": False}

# Incognito — stop *identifying* people without going blind.
#
# PAUSED is the wrong tool for "don't ask my name": it short-circuits the whole
# loop, which also kills the glance-toward-the-speaker that makes Vibey feel
# present. The complaint this exists for is narrower — the camera is unreliable
# enough that a bad frame reads as a stranger, and the robot interrupts whoever
# is mid-sentence to ask a name it already has. So this gates only the three
# things that turn a face into an identity:
#
#   * enrolling a face nobody has seen before (and the snapshot that stores it)
#   * banking extra sample photos of people already known
#   * the greetings whose whole purpose is to fish for a name
#
# Detection, CURRENT_PEOPLE, greeting people who ALREADY have names, and the
# glance all keep running. Vibey still watches the room and looks at whoever is
# talking; it just stops trying to file everyone.
#
# FACE_NAMING=0 in .env makes it the default at boot.
INCOGNITO = {"on": os.environ.get("FACE_NAMING", "1").strip() == "0"}


def _set_current_people(people: list[dict]):
    with _current_lock:
        CURRENT_PEOPLE[:] = people


def _current_people_fresh() -> list[dict]:
    now = time.time()
    with _current_lock:
        return [dict(p, fresh=(now - p["ts"]) < PERSON_FRESH_S) for p in CURRENT_PEOPLE]


# --------------------------------------------------------------------------- #
# Storage: local SQLite + JPEG files (reachy_faces_store, data/vibey.db).      #
# Moved off Supabase 2026-09-30; nothing here touches the network any more.   #
# --------------------------------------------------------------------------- #
def sb_get_faces() -> list[dict]:
    return store.get_faces()


def sb_get_samples() -> list[dict]:
    return store.get_samples()


# The full "face model" is small enough to hold in memory. Refetching every
# ~250-vector gallery on every 1.2s poll was pure waste and grew with each new
# person; instead keep it cached and refresh at most every SAMPLES_CACHE_TTL,
# updating it in place when we bank/insert so within-session recognition is
# immediate. Mutations from the API thread (name/merge/delete) invalidate it.
SAMPLES_CACHE_TTL = float(os.environ.get("SAMPLES_CACHE_TTL", "20"))
_SAMPLES_CACHE = {"rows": None, "at": 0.0}
_samples_lock = threading.Lock()


def get_samples_cached(force: bool = False) -> list[dict]:
    now = time.time()
    with _samples_lock:
        rows = _SAMPLES_CACHE["rows"]
        fresh = rows is not None and (now - _SAMPLES_CACHE["at"] < SAMPLES_CACHE_TTL)
    if fresh and not force:
        return rows
    try:
        new_rows = sb_get_samples()  # network — outside the lock
    except Exception as e:
        if rows is not None:
            print(f"[memory] samples refetch failed ({e}); using cached", flush=True)
            return rows
        raise
    with _samples_lock:
        _SAMPLES_CACHE["rows"] = new_rows
        _SAMPLES_CACHE["at"] = time.time()
    return new_rows


def _cache_add_sample(face_id: str, embedding: list[float],
                      name: str | None = None, times_seen: int = 1) -> None:
    """Append a just-banked sample to the live cache so the next frame matches
    it without waiting for the TTL refetch."""
    with _samples_lock:
        if _SAMPLES_CACHE["rows"] is None:
            return
        _SAMPLES_CACHE["rows"].append({
            "sample_id": None, "face_id": face_id, "embedding": embedding,
            "created_at": None, "name": name, "times_seen": times_seen})


def _invalidate_samples_cache() -> None:
    """Force the next get_samples_cached() to refetch — call after any mutation
    that reshapes identities (name/merge/delete/prune)."""
    with _samples_lock:
        _SAMPLES_CACHE["at"] = 0.0


def sb_insert_face(snapshot: str) -> dict:
    return store.insert_face(snapshot)


def sb_add_sample(face_id: str, embedding: list[float], snapshot: str) -> None:
    store.add_sample(face_id, embedding, snapshot, MAX_SAMPLES)


def sb_touch_face(face_id: str, times_seen: int) -> None:
    store.touch_face(face_id, times_seen)


def sb_name_face(face_id: str, name: str) -> None:
    store.name_face(face_id, name)


def sb_delete_face(face_id: str) -> None:
    store.delete_face(face_id)


def sb_find_face_by_name(name: str) -> dict | None:
    return store.find_face_by_name(name)


def sb_merge_faces(src_id: str, dst_id: str) -> None:
    store.merge_faces(src_id, dst_id, MAX_SAMPLES)


# --------------------------------------------------------------------------- #
# Perception helpers                                                           #
# --------------------------------------------------------------------------- #
def snapshot_jpeg() -> bytes | None:
    try:
        with urllib.request.urlopen(f"{CAM_URL}/frame.jpg", timeout=5) as r:
            return r.read()
    except Exception:
        return None


def encode_faces(jpeg: bytes) -> list[dict]:
    """Every face in the frame (up to MAX_FACES, largest first): embedding for
    matching, a per-face crop as the snapshot to store, and a normalized
    (x, y) center in roughly [-1, 1] for the dashboard overlay. Lazily imports
    face_recognition so the rest of the module loads without it."""
    try:
        import numpy as np
        import face_recognition
        from PIL import Image
    except ImportError:
        print("[memory] face_recognition/numpy/Pillow missing — "
              "run: uv pip install face_recognition", flush=True)
        return []
    import base64
    img = face_recognition.load_image_file(io.BytesIO(jpeg))
    h, w = img.shape[0], img.shape[1]
    boxes = face_recognition.face_locations(img)
    if not boxes:
        return []
    boxes.sort(key=lambda b: (b[2] - b[0]) * (b[1] - b[3]), reverse=True)
    boxes = boxes[:MAX_FACES]
    encodings = face_recognition.face_encodings(img, boxes)

    out = []
    pil_img = Image.fromarray(img)
    for (top, right, bottom, left), enc in zip(boxes, encodings):
        cx, cy = (left + right) / 2, (top + bottom) / 2
        # quality signals for the enrollment gate — measured on the TIGHT face
        # region (not the padded crop) so background sharpness can't fake it:
        #   face_px  = face height in pixels (proxy for distance/resolution)
        #   blur     = variance of the Laplacian (low = out of focus/motion)
        face_px = int(bottom - top)
        region = img[max(0, top):bottom, max(0, left):right]
        blur = 0.0
        if region.shape[0] >= 3 and region.shape[1] >= 3:
            gray = region.astype(np.float64).mean(axis=2)
            lap = (-4 * gray[1:-1, 1:-1]
                   + gray[:-2, 1:-1] + gray[2:, 1:-1]
                   + gray[1:-1, :-2] + gray[1:-1, 2:])
            blur = float(lap.var())
        # pad the crop a bit so the stored photo isn't a tight, awkward box
        pad_x, pad_y = (right - left) * 0.4, (bottom - top) * 0.4
        crop = pil_img.crop((
            max(0, int(left - pad_x)), max(0, int(top - pad_y)),
            min(w, int(right + pad_x)), min(h, int(bottom + pad_y)),
        ))
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=85)
        uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        # Corruption, measured directly. The camera arrives over WebRTC and a
        # dropped packet leaves the decoder painting 16-pixel macroblocks from
        # the wrong frame. The blur gate cannot catch that — block edges are
        # SHARP, so a corrupted face scores as unusually crisp and sails through.
        # Of 35 saved "people", about ten were the same person enrolled again
        # from smeared frames. So: how much of the image's edge energy sits
        # exactly on 16-pixel column boundaries. Natural faces: ~1x. Torn
        # frames: 2-4x.
        blocky = 0.0
        if region.shape[0] >= 48 and region.shape[1] >= 48:
            g = region.astype(np.float64).mean(axis=2)
            dx = np.abs(np.diff(g, axis=1))
            cols = np.arange(dx.shape[1]) + max(0, left) + 1
            on_edge = dx[:, (cols % 16) == 0].mean() if ((cols % 16) == 0).any() else 0.0
            off_edge = dx[:, (cols % 16) != 0].mean() + 1e-6
            blocky = float(on_edge / off_edge)
        # And whether it is a face at all. HOG fires on dogs and t-shirt
        # logos; the landmark model does not find two eyes and a mouth on either.
        parts = 0
        try:
            lm = face_recognition.face_landmarks(img, [(top, right, bottom, left)])
            if lm:
                parts = sum(1 for k in ("left_eye", "right_eye", "nose_tip", "top_lip") if lm[0].get(k))
        except Exception:  # noqa: BLE001
            parts = 4   # landmark model unavailable: do not block on it
        out.append({
            "embedding": enc.tolist(),
            "snapshot": uri,
            "x": (cx / w - 0.5) * 2,
            "y": (cy / h - 0.5) * 2,
            "face_px": face_px,
            "blur": blur,
            "blocky": blocky,
            "parts": parts,
        })
    return out


def _passes_quality(face: dict) -> tuple[bool, str]:
    """Enrollment gate: is this detection clean enough to become a stored
    sample? Returns (ok, reason-if-not)."""
    if face.get("face_px", 0) < MIN_FACE_PX:
        return False, f"too small ({face.get('face_px', 0)}px < {MIN_FACE_PX})"
    if face.get("blur", 0.0) < MIN_BLUR_VAR:
        return False, f"too blurry (var {face.get('blur', 0.0):.0f} < {MIN_BLUR_VAR})"
    if face.get("blocky", 0.0) > MAX_BLOCKINESS:
        return False, f"torn frame (blockiness {face.get('blocky', 0.0):.1f} > {MAX_BLOCKINESS})"
    if face.get("parts", 4) < 4:
        return False, f"not a face ({face.get('parts', 0)}/4 landmarks)"
    return True, ""


# A stranger has to be seen more than once before they exist.
#
# The strongest guard of the three, because it needs no image heuristics at all:
# corruption is different on every frame, so garbage embeddings never agree with
# each other, while a real new face produces near-identical embeddings cycle
# after cycle. Enrollment waits for STRANGER_CONFIRM sightings inside
# STRANGER_WINDOW_S whose embeddings all sit within STRANGER_AGREE of the first.
STRANGER_CONFIRM = int(os.environ.get("STRANGER_CONFIRM", "3"))
STRANGER_WINDOW_S = float(os.environ.get("STRANGER_WINDOW_S", "12"))
STRANGER_AGREE = float(os.environ.get("STRANGER_AGREE", "0.45"))
_PENDING: list[dict] = []     # {"emb": np.array, "first": t, "count": n}


def _stranger_confirmed(embedding: list[float]) -> bool:
    """Counts this sighting against recent unmatched ones; True once the same
    face has been seen enough times to be believed."""
    import numpy as np
    now = time.time()
    emb = np.asarray(embedding)
    _PENDING[:] = [p for p in _PENDING if now - p["first"] < STRANGER_WINDOW_S]
    for p in _PENDING:
        if float(np.linalg.norm(p["emb"] - emb)) < STRANGER_AGREE:
            p["count"] += 1
            if p["count"] >= STRANGER_CONFIRM:
                _PENDING.remove(p)
                return True
            return False
    _PENDING.append({"emb": emb, "first": now, "count": 1})
    return False


def best_match(embedding: list[float], samples: list[dict]):
    """Nearest sample across everyone Wonder knows, within tolerance.
    Matching against every sample (not one photo per person) is what makes
    this robust to angle/lighting — some sample will happen to be close."""
    try:
        import numpy as np
    except ImportError:
        return None
    if not samples:
        return None
    e = np.array(embedding)
    best, best_d = None, 1e9
    for row in samples:
        emb = row.get("embedding")
        if not emb:
            continue
        d = float(np.linalg.norm(e - np.array(emb)))
        if d < best_d:
            best, best_d = row, d
    if best is not None and best_d <= MATCH_TOLERANCE:
        return best, best_d
    return None


# --------------------------------------------------------------------------- #
# Auto-grouping: periodically compare identity clusters and merge the ones
# that are confidently the same person, so duplicates stop piling up for
# Jack to clean manually. Conservative by design:
#   - merge only when the closest sample pair between two identities is
#     within AUTO_MERGE_TOLERANCE (default 0.44 — confident-match range;
#     same-person pairs measured 0.37-0.57, different people 0.62+)
#   - NEVER auto-merge two identities that carry different names
#   - unnamed folds into named; otherwise smaller folds into bigger
# --------------------------------------------------------------------------- #
# 0.50: measured floor between DIFFERENT people in this house is 0.538
# (Jack<->Kai — family resemblance), so 0.50 groups true duplicates while
# never reaching the sibling zone. The different-names guard is the second
# safety net.
AUTO_MERGE_TOLERANCE = float(os.environ.get("AUTO_MERGE_TOLERANCE", "0.50"))
# Unnamed identities glimpsed once and never again are almost always
# mis-detections or passers-by; they get pruned after a few days.
AUTO_PRUNE_DAYS = float(os.environ.get("AUTO_PRUNE_DAYS", "3"))
AUTO_MERGE_EVERY_S = float(os.environ.get("AUTO_MERGE_EVERY_S", "900"))
_auto_merge = {"last": 0.0}


def _auto_merge_pass(verbose: bool = False) -> int:
    """One grouping sweep. Returns how many merges happened."""
    try:
        import numpy as np
    except ImportError:
        return 0
    try:
        samples = sb_get_samples()
    except Exception as e:
        print(f"[memory] auto-merge read failed: {e}", flush=True)
        return 0

    clusters: dict[str, dict] = {}
    for s in samples:
        if not s.get("embedding"):
            continue
        c = clusters.setdefault(s["face_id"], {
            "name": s.get("name"), "times_seen": s.get("times_seen", 1),
            "embs": []})
        c["embs"].append(np.array(s["embedding"]))
    ids = list(clusters)
    merges = 0
    merged_away: set[str] = set()
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            if a in merged_away or b in merged_away:
                continue
            ca, cb = clusters[a], clusters[b]
            # different explicit names → hands off, always
            if ca["name"] and cb["name"] and ca["name"] != cb["name"]:
                continue
            dmin = min(float(np.linalg.norm(ea - eb))
                       for ea in ca["embs"] for eb in cb["embs"])
            if dmin > AUTO_MERGE_TOLERANCE:
                continue
            # keeper: named beats unnamed; then most-seen
            if ca["name"] and not cb["name"]:
                keep, drop = a, b
            elif cb["name"] and not ca["name"]:
                keep, drop = b, a
            elif ca["times_seen"] >= cb["times_seen"]:
                keep, drop = a, b
            else:
                keep, drop = b, a
            try:
                sb_merge_faces(drop, keep)
                merged_away.add(drop)
                clusters[keep]["embs"].extend(clusters[drop]["embs"])
                merges += 1
                who = clusters[keep]["name"] or keep[:8]
                print(f"[memory] auto-merged {drop[:8]} into {who} "
                      f"(d={dmin:.2f})", flush=True)
            except Exception as e:
                print(f"[memory] auto-merge failed: {e}", flush=True)
    if verbose and merges == 0:
        print("[memory] auto-merge: nothing close enough to group", flush=True)
    return merges


def _auto_prune_pass() -> int:
    """Delete unnamed one-sighting identities not seen in AUTO_PRUNE_DAYS."""
    try:
        faces = store.faces_for_prune()
    except Exception:
        return 0
    from datetime import datetime as _dt, timezone as _tz
    cutoff = time.time() - AUTO_PRUNE_DAYS * 86400
    pruned = 0
    for f in faces:
        if f.get("name") or f.get("times_seen", 0) > 1:
            continue
        try:
            seen = _dt.fromisoformat(
                f["last_seen"].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if seen < cutoff:
            try:
                sb_delete_face(f["id"])
                pruned += 1
                print(f"[memory] pruned stale one-off {f['id'][:8]}", flush=True)
            except Exception:
                pass
    return pruned


def _maybe_auto_merge() -> None:
    if time.time() - _auto_merge["last"] < AUTO_MERGE_EVERY_S:
        return
    _auto_merge["last"] = time.time()
    merged = _auto_merge_pass()
    pruned = _auto_prune_pass()
    if merged or pruned:
        _invalidate_samples_cache()  # identities changed under us


# --------------------------------------------------------------------------- #
# Shared journal: every ~30 min with activity, the robot writes a short
# first-person entry into the local journal table (data/vibey.db). Until
# 2026-09-30 this went to Supabase vibey_journal_entries, shared with the
# Telegram Vibey; it is private and local now.
# --------------------------------------------------------------------------- #
JOURNAL_COMMUNITY = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
JOURNAL_EVERY_S = 1800.0
_journal = {"last": time.time(), "people": set(), "count": 0}


def _journal_note(name: str | None) -> None:
    if name:
        _journal["people"].add(name)
    _journal["count"] += 1


def _journal_flush() -> None:
    if time.time() - _journal["last"] < JOURNAL_EVERY_S:
        return
    if _journal["count"] == 0:
        _journal["last"] = time.time()
        return
    people = ", ".join(sorted(_journal["people"])) or "someone I don't know yet"
    facts = (f"In the last half hour my camera saw {_journal['count']} "
             f"face sightings. People recognized: {people}.")
    body = None
    try:
        import subprocess
        r = subprocess.run(
            ["claude", "-p", "--model", "haiku",
             "You are Vibey, a small physical robot writing one short "
             "first-person journal paragraph (2-3 sentences, warm, a little "
             "wry, no markdown) about your last half hour. Facts: " + facts],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            body = r.stdout.strip()[:600]
    except Exception:
        pass
    if not body:
        body = f"Robot log: {facts}"
    try:
        store.add_journal(body, _journal["count"], "reachy-robot", JOURNAL_COMMUNITY)
        print(f"[memory] journaled: {body[:80]!r}", flush=True)
    except Exception as e:
        print(f"[memory] journal failed: {e}", flush=True)
    _journal.update({"last": time.time(), "people": set(), "count": 0})


# --------------------------------------------------------------------------- #
# Conversation starters: if someone's been hanging out in frame for a while
# with no chat happening, Vibey says something curious. Heavily rate-limited,
# daytime only (08:00-22:00), and off entirely while chat is muted.
# --------------------------------------------------------------------------- #
STARTER_AFTER_S = 300.0       # someone visible this long with no chat
STARTER_COOLDOWN_S = 1800.0   # at most one opener per half hour
_starter = {"since": 0.0, "last": 0.0}



def _maybe_start_conversation(any_face: bool) -> None:
    import random
    from datetime import datetime as _dt
    now = time.time()
    if not any_face:
        _starter["since"] = 0.0
        return
    if _starter["since"] == 0.0:
        _starter["since"] = now
        return
    hour = _dt.now().hour
    if not (8 <= hour < 22):
        return
    if now - _starter["since"] < STARTER_AFTER_S:
        return
    if now - _starter["last"] < STARTER_COOLDOWN_S:
        return
    try:
        req = urllib.request.Request("http://localhost:8772/state")
        with urllib.request.urlopen(req, timeout=3) as r:
            st = json.loads(r.read())
        if st.get("muted") or st.get("speaking"):
            return
        # Not while the realtime engine has the conversation.
        #
        # This service greets and prompts through ElevenLabs while the realtime
        # engine speaks in its own voice, and they do not know about each other —
        # so the robot answered in two different voices, from two different minds,
        # sometimes at once. Whoever is holding the conversation should be the only
        # one talking in it.
        if st.get("openai") or st.get("asleep"):
            return
        transcript = st.get("transcript") or []
        last_chat = (transcript[-1]["ts"] / 1000) if transcript else 0
        if now - last_chat < STARTER_AFTER_S:
            return
    except Exception:
        return
    _starter["last"] = now
    _starter["since"] = now
    print("[memory] nudging the brain to start a conversation", flush=True)
    try:
        from reachy_emotes import play as _pe
        _pe("curious")
    except Exception:
        pass
    # Prompt the brain to open its mouth rather than reading a line at somebody.
    # A canned opener in a different voice is the tell that there are two systems
    # in the room; the same idea handed to the model comes out as Vibey.
    # And if no brain is holding the conversation, nothing is said. The canned
    # line this used to fall back to was the tell that there were two systems in
    # the room — and at boot, before anyone has turned it on, it was the robot
    # opening with a scripted greeting nobody asked for.
    # Off by default (2026-10-02): unprompted openers read as Vibey "saying
    # random stuff". VIBEY_CHATTY=1 brings them back.
    if os.environ.get("VIBEY_CHATTY") != "1":
        return
    _tell_the_conversation(
        "The room has gone quiet and somebody is still in front of you. "
        "Start a conversation — one short, curious line in your own voice.")


# --------------------------------------------------------------------------- #
# Look-at-speaker (EXPERIMENTAL — set LOOK_AT_SPEAKER=1 to enable).
# When the mics detect speech and we can see faces, nudge the head toward
# the face nearest the sound direction. DOA sign/offset conventions need a
# live human to calibrate against (DOA_FRONT, DOA_SIGN), so this ships OFF.
# --------------------------------------------------------------------------- #
LOOK_AT_SPEAKER = os.environ.get("LOOK_AT_SPEAKER", "") == "1"
DOA_FRONT = float(os.environ.get("DOA_FRONT", "1.5708"))   # radians = "ahead"
DOA_SIGN = float(os.environ.get("DOA_SIGN", "1"))          # flip if backwards
_last_glance = {"at": 0.0}


def _maybe_glance(faces: list[dict]) -> None:
    if not LOOK_AT_SPEAKER or not faces:
        return
    if time.time() - _last_glance["at"] < 5.0:
        return
    try:
        req = urllib.request.Request(
            f"{REACHY_URL}/api/state/full?with_doa=true")
        with urllib.request.urlopen(req, timeout=2) as r:
            st = json.loads(r.read())
        doa = st.get("doa") or {}
        if not doa.get("speech_detected"):
            return
        side = DOA_SIGN * (doa.get("angle", DOA_FRONT) - DOA_FRONT)
        # pick the face whose x-position best matches the sound side
        target = min(faces, key=lambda f: abs(f["x"] - max(-1, min(1, side))))
        yaw = st.get("head_pose", {}).get("yaw", 0.0)
        nudge = max(-0.25, min(0.25, -target["x"] * 0.4))
        body = {"head_pose": {"x": 0, "y": 0, "z": 0, "roll": 0,
                              "pitch": st.get("head_pose", {}).get("pitch", 0),
                              "yaw": yaw + nudge},
                "antennas": None, "duration": 0.5}
        req = urllib.request.Request(
            f"{REACHY_URL}/api/move/goto", data=json.dumps(body).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=4).read()
        _last_glance["at"] = time.time()
        print(f"[memory] glanced toward speaker (nudge {nudge:+.2f})", flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Control API — lets the dashboard see who Wonder recognizes and name them.   #
# --------------------------------------------------------------------------- #
class _MemHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/current"):
            # List, not a single object — zero, one, or several faces can be
            # in frame at once. Kept the /current name for compatibility.
            #
            # `paused` rides along because an empty list means both "nobody is
            # there" and "I am not looking", and those are very different things
            # for the robot to say out loud.
            # `incognito` rides along for the same reason as `paused`: an
            # unnamed face means "we have never been introduced" normally, but
            # "I was told not to ask" in incognito, and the brain phrases those
            # two very differently.
            self._json({"people": _current_people_fresh(), "paused": PAUSED["on"],
                        "incognito": INCOGNITO["on"]})
        elif self.path.startswith("/names"):
            try:
                faces = sb_get_faces()
                names = sorted({r["name"] for r in faces if r.get("name")})
                self._json(names)
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path.startswith("/samples"):
            # All photos for one person: /samples?face_id=<uuid>
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                face_id = (q.get("face_id") or [""])[0]
                if not face_id:
                    raise ValueError("face_id required")
                self._json(store.samples_for(face_id))
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path.startswith("/person"):
            # One friend's profile for the phone: /person?face_id=<uuid> →
            # basics (first/last seen) + every sample photo with its id.
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                face = store.get_face((q.get("face_id") or [""])[0])
                if not face:
                    self._json({"error": "no such person"}, 404)
                else:
                    self._json(face)
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path.startswith("/people"):
            try:
                faces = sb_get_faces()
                # sample photos (no embeddings — keep the payload sane):
                # newest first, up to 3 shown per person as a photo clump
                all_samples = store.all_sample_photos()
                counts: dict[str, int] = {}
                photos: dict[str, list] = {}
                for s in all_samples:
                    fid = s["face_id"]
                    counts[fid] = counts.get(fid, 0) + 1
                    if s.get("snapshot") and len(photos.setdefault(fid, [])) < 3:
                        photos[fid].append(s["snapshot"])
                people = [{
                    "id": r["id"],
                    "name": r.get("name"),
                    "times_seen": r.get("times_seen", 1),
                    "snapshot": (photos.get(r["id"]) or [r.get("snapshot")])[0],
                    "photos": photos.get(r["id"], []),
                    "sample_count": counts.get(r["id"], 0),
                } for r in faces]
                people.sort(key=lambda p: p["times_seen"], reverse=True)
                self._json(people)
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.startswith("/name"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                name = (body.get("name") or "").strip()[:60]
                face_id = body.get("face_id")
                if not face_id:
                    with _current_lock:
                        if len(CURRENT_PEOPLE) == 1:
                            face_id = CURRENT_PEOPLE[0]["face_id"]
                if not (name and face_id):
                    raise ValueError("need name (and a face_id, or exactly "
                                      "one face currently in view)")
                merged = False
                existing = sb_find_face_by_name(name)
                if existing and existing["id"] != face_id:
                    # Same name = same person: fold this face into the
                    # existing identity instead of keeping a duplicate.
                    sb_merge_faces(face_id, existing["id"])
                    merged = True
                    old_id, face_id = face_id, existing["id"]
                    print(f"[memory] merged {old_id} into {name} ({face_id})",
                          flush=True)
                else:
                    sb_name_face(face_id, name)
                live = False
                with _current_lock:
                    for p in CURRENT_PEOPLE:
                        if p["face_id"] == face_id or (merged and p["face_id"] == old_id):
                            p["face_id"] = face_id
                            p["name"] = name
                            live = time.time() - p["ts"] < PERSON_FRESH_S
                print(f"[memory] named {face_id} -> {name}", flush=True)
                _invalidate_samples_cache()  # name/merge reshaped identities
                # Only greet out loud if this is someone actually standing in
                # front of the camera right now — renaming an old gallery
                # entry from across the room shouldn't make Wonder talk.
                if live:
                    try:
                        _tell_the_conversation(
                            f"You have just learned that this person is called "
                            f"{name}. Say something warm and brief using their "
                            f"name — do not mention saving or remembering it.")
                    except Exception:
                        pass
                self._json({"ok": True, "face_id": face_id, "name": name})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        elif self.path.startswith("/pause"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                PAUSED["on"] = bool(body.get("paused"))
                if PAUSED["on"]:
                    _set_current_people([])
                print(f"[memory] {'paused' if PAUSED['on'] else 'resumed'}", flush=True)
                self._json({"ok": True, "paused": PAUSED["on"]})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        elif self.path.startswith("/incognito"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                INCOGNITO["on"] = bool(body.get("on"))
                state = "ON — seeing but not naming" if INCOGNITO["on"] else "off"
                print(f"[memory] incognito {state}", flush=True)
                # Only half the switch lives here. The brain carries its own
                # "ask people their name" line in the prompt and owns the
                # remember_face tool, and it runs in the chat process — so the
                # dashboard toggle goes through the chat service, which flips
                # both. Nothing to call from here.
                self._json({"ok": True, "on": INCOGNITO["on"]})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        elif self.path.startswith("/deletesample"):
            # Forget ONE photo (sample) — the person and their other photos
            # stay. The last remaining photo is protected: deleting it would
            # leave the person unrecognizable, so that path is the full
            # delete-person flow instead.
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                sample_id = body.get("sample_id")
                if not sample_id:
                    raise ValueError("sample_id required")
                face_id = store.sample_face(sample_id)
                if not face_id:
                    raise ValueError("sample not found")
                total = store.count_samples(face_id)
                if total <= 1:
                    raise ValueError("last photo — delete the person instead")
                store.delete_sample(sample_id)
                _invalidate_samples_cache()
                print(f"[memory] deleted sample {sample_id} of {face_id}", flush=True)
                self._json({"ok": True, "remaining": total - 1})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        elif self.path.startswith("/reassignsample"):
            # "This photo is someone else": move ONE sample, embedding and all,
            # to another person ({face_id}) or a name ({name}: an existing
            # person by that name, else a new one). Recognition reads samples,
            # so the correction takes effect on the next cache refresh.
            # Moving the last photo of a stranger is just naming them (merge);
            # the last photo of a named person is refused like /deletesample.
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                sample_id = body.get("sample_id")
                if not sample_id:
                    raise ValueError("sample_id required")
                src = store.sample_face(sample_id)
                if not src:
                    raise ValueError("sample not found")
                dst, name = body.get("face_id"), (body.get("name") or "").strip()[:60]
                if dst:
                    target = store.get_face(dst)
                    if not target:
                        raise ValueError("no such person")
                    name = target.get("name")
                elif name:
                    existing = store.find_face_by_name_ci(name)
                    dst = existing["id"] if existing else None
                    name = existing["name"] if existing else name
                else:
                    raise ValueError("face_id or name required")
                if dst == src:
                    raise ValueError("that photo is already filed under them")
                last = store.count_samples(src) <= 1
                if last and (store.get_face(src) or {}).get("name"):
                    raise ValueError("last photo — delete or rename the person instead")
                if not dst:
                    dst = store.insert_face(store.sample_snapshot(sample_id))["id"]
                    store.name_face(dst, name)
                if last:
                    sb_merge_faces(src, dst)
                else:
                    store.move_sample(sample_id, dst)
                _invalidate_samples_cache()
                print(f"[memory] moved sample {sample_id} {src} -> {dst} ({name})"
                      f"{' (merged)' if last else ''}", flush=True)
                self._json({"ok": True, "face_id": dst, "name": name, "merged": last})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        elif self.path.startswith("/deleteface"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                face_id = body.get("face_id")
                if not face_id:
                    raise ValueError("face_id required")
                sb_delete_face(face_id)
                _invalidate_samples_cache()
                with _current_lock:
                    CURRENT_PEOPLE[:] = [p for p in CURRENT_PEOPLE
                                         if p["face_id"] != face_id]
                print(f"[memory] deleted {face_id}", flush=True)
                self._json({"ok": True, "face_id": face_id})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        else:
            self._json({"error": "not found"}, 404)


def _start_mem_server():
    import vibey_auth
    vibey_auth.protect(_MemHandler)   # LAN needs the app token; localhost is free
    srv = ThreadingHTTPServer(("0.0.0.0", MEM_PORT), _MemHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[memory] control API on http://localhost:{MEM_PORT}", flush=True)


# --------------------------------------------------------------------------- #
# Main loop                                                                    #
# --------------------------------------------------------------------------- #
def run():
    _start_mem_server()
    print(f"[memory] watching for faces (multi-face, {POLL_INTERVAL}s poll) "
          f"· local db={store.DB_PATH} ({store.counts()['faces']} friends) "
          f"· match<={MATCH_TOLERANCE} learn<={LEARN_TOLERANCE}", flush=True)
    last_greet: dict[str, float] = {}
    last_learn: dict[str, float] = {}
    asked_name: set[str] = set()   # unnamed faces we've already asked — once per run

    while True:
        time.sleep(POLL_INTERVAL)
        if PAUSED["on"]:
            continue
        jpg = snapshot_jpeg()
        if not jpg:
            continue
        faces = encode_faces(jpg)
        if not faces:
            _set_current_people([])
            _maybe_start_conversation(False)
            _journal_flush()
            continue

        try:
            samples = get_samples_cached()
        except Exception as e:
            print(f"[memory] face db read failed: {e}", flush=True)
            time.sleep(2)
            continue

        now = time.time()
        seen_now: list[dict] = []
        to_greet: list[tuple[str, str | None]] = []  # (fid, name) — spoken after the loop

        for f in faces:
            embedding, snap, x, y = f["embedding"], f["snapshot"], f["x"], f["y"]
            m = best_match(embedding, samples)

            if m:
                row, dist = m
                fid, name = row["face_id"], row.get("name")
                seen_now.append({"face_id": fid, "name": name, "x": x, "y": y, "ts": now})

                # A confident match auto-banks this sighting as a new sample —
                # the model gets more accurate just from normal use — but only
                # if the frame is clean enough (same gate as new enrollment),
                # so we never bank a blurry/tiny shot that dilutes the person.
                if (not INCOGNITO["on"]
                        and dist <= LEARN_TOLERANCE and _passes_quality(f)[0]
                        and now - last_learn.get(fid, 0) > LEARN_COOLDOWN):
                    last_learn[fid] = now
                    try:
                        sb_add_sample(fid, embedding, snap)
                        _cache_add_sample(fid, embedding, name, row.get("times_seen", 1))
                        print(f"[memory] learned a new sample for "
                              f"{name or fid} (d={dist:.2f})", flush=True)
                    except Exception as e:
                        print(f"[memory] learn failed: {e}", flush=True)

                if now - last_greet.get(fid, 0) > GREET_COOLDOWN:
                    last_greet[fid] = now
                    try:
                        sb_touch_face(fid, row.get("times_seen", 1))
                    except Exception:
                        pass
                    if name:
                        print(f"[memory] recognized {name} (d={dist:.2f})", flush=True)
                        to_greet.append((fid, name))
                    elif INCOGNITO["on"]:
                        # Known face, no name, and we are not fishing for one.
                        # Seen and tracked, just not asked about.
                        print(f"[memory] unnamed {fid} (d={dist:.2f}) — incognito, "
                              f"not asking", flush=True)
                    elif fid not in asked_name:
                        asked_name.add(fid)
                        print(f"[memory] unnamed {fid} (d={dist:.2f}) — asking once", flush=True)
                        to_greet.append((fid, None))
            elif INCOGNITO["on"]:
                # A stranger stays a stranger: no row, no snapshot, no greeting.
                # They are still in `faces`, so the glance below can look at them.
                pass
            else:
                # Quality gate: never turn a blurry / tiny / bad detection into
                # a permanent identity. A poor frame of a stranger is just
                # ignored this cycle; they'll enroll on a cleaner look.
                ok, why = _passes_quality(f)
                if not ok:
                    print(f"[memory] skip enroll — {why}", flush=True)
                    continue
                if not _stranger_confirmed(embedding):
                    print("[memory] stranger seen — waiting to see them again before enrolling", flush=True)
                    continue
                try:
                    row = sb_insert_face(snap)
                    fid = row.get("id", "?")
                    sb_add_sample(fid, embedding, snap)
                    # add to the cache AND this frame's working set, so a second
                    # face in the same frame matches rather than re-enrolling.
                    _cache_add_sample(fid, embedding, None, 1)
                    samples.append({"sample_id": None, "face_id": fid,
                                    "embedding": embedding, "created_at": None,
                                    "name": None, "times_seen": 1})
                    last_greet[fid] = now
                    asked_name.add(fid)
                    seen_now.append({"face_id": fid, "name": None, "x": x, "y": y, "ts": now})
                    print(f"[memory] NEW face stored: {fid}", flush=True)
                    to_greet.append((fid, "__new__"))
                except Exception as e:
                    print(f"[memory] face db insert failed: {e}", flush=True)

        _set_current_people(seen_now)
        for p in seen_now:
            _journal_note(p.get("name"))
        _journal_flush()
        _maybe_auto_merge()
        _maybe_glance(faces)
        _maybe_start_conversation(bool(faces))

        # Speak after processing every face this cycle, so two people walking
        # up together each get acknowledged instead of only the first.
        for fid, name in to_greet:
            try:
                import reachy_events
                if name and name != "__new__":
                    reachy_events.emit("senses", f"recognized {name}", source="faces", icon="🙂")
                else:
                    reachy_events.emit("senses", "a new face" if name == "__new__" else "an unnamed face",
                                       source="faces", icon="❔")
            except Exception:  # noqa: BLE001
                pass
            # Only a known person, by name, is worth an out-loud greeting.
            # Unknown or unnamed faces go in as quiet context: telling the brain
            # to "ask what they are called" on every sighting is why Vibey kept
            # interrupting with "what're you called?" (2026-10-02).
            if name and name != "__new__":
                line = (f"{name}, who you already know, just walked into view. "
                        f"If nobody is mid-conversation, greet them by name, "
                        f"briefly. Otherwise just carry on.")
                silent = False
            else:
                line = ("Someone you don't have a name for is in view. Context only: "
                        "do not ask their name or comment on it; keep doing what "
                        "you were doing.")
                silent = True

            # Hand it to whichever brain is holding the conversation, so the robot
            # reacts in its own voice. No fallback: if nobody is holding it, the
            # robot is asleep or off, and a scripted "nice to meet you" from a
            # service that is not the robot is exactly what got removed here.
            _tell_the_conversation(line, silent=silent)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--group":
        n = _auto_merge_pass(verbose=True)
        print(f"merged {n} duplicate identit{'y' if n == 1 else 'ies'}")
    elif len(sys.argv) >= 3 and sys.argv[1] == "--name":
        sb_name_face(sys.argv[2], " ".join(sys.argv[3:]))
        print(f"named {sys.argv[2]} -> {' '.join(sys.argv[3:])}")
    else:
        run()
