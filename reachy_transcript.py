#!/usr/bin/env python3
"""
reachy_transcript.py — the conversation, kept.

Until this existed the only record of talking to Vibey was `TRANSCRIPT` in
reachy_chat: a 40-line deque in RAM, thrown away on every restart. The daily
journal (reachy_memory) writes a *summary* paragraph every half hour, and
Supermemory holds the handful of things somebody said "remember this" about.
None of that is the conversation. This is: every line, both sides, on disk,
with a name on it wherever the camera can tell us one.

    transcripts/YYYY-MM-DD.jsonl      one JSON object per line, append-only

Each line:

    {"ts": "2026-08-28T17:40:11-07:00",   # local time, offset included
     "who": "vibey" | "human",
     "speaker": "Jack" | null,            # null = nobody nameable in view
     "text": "...",
     "by": "sole-face" | "doa" | null,    # HOW the speaker was decided
     "present": ["Jack", "Ada"]}          # everyone visible, named or not

`by` matters more than it looks. A transcript that silently guesses is worse
than one that admits it doesn't know, because a wrong name is indistinguishable
from a right one when you read it back in a month. So attribution is recorded
with its own provenance:

    sole-face  exactly one named person was in frame. Safe.
    doa        several people in frame; the robot's direction-of-arrival picked
               this one. A guess, and labelled as such.
    null       nobody named in view, or the room was too ambiguous to call.

WRITES ARE OFF THE HOT PATH. _log_turn is called from the realtime receive loop,
which is also feeding audio to the speaker; a disk write and an HTTP lookup in
there is a stutter you can hear. Everything here goes onto a queue and a daemon
thread does the slow parts.

Local only, on purpose. A robot in a room logging everything said near it is the
exact data that should not leave the house — see MEMORY_PLAN.md. Nothing here
posts to Supermemory or Supabase; `transcripts/` is gitignored.

    import reachy_transcript
    reachy_transcript.log("human", "hey vibey")
    reachy_transcript.read(day="2026-08-28")     # -> list of entries
    reachy_transcript.days()                     # -> ["2026-08-27", ...]

CLI:
    python3 reachy_transcript.py            # today, human-readable
    python3 reachy_transcript.py 2026-08-27
    python3 reachy_transcript.py --days
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.environ.get("TRANSCRIPT_DIR") or os.path.join(HERE, "transcripts")
MEM_URL = os.environ.get("MEM_URL", "http://localhost:8773").rstrip("/")
REACHY_URL = os.environ.get("REACHY_URL", "").rstrip("/")
ENABLED = os.environ.get("TRANSCRIPT", "1").strip() != "0"

# Direction-of-arrival lives on the robot and matches reachy_memory's glance
# logic. Same constants, same meaning — a speaker at angle DOA_FRONT is dead
# ahead, and DOA_SIGN flips the axis if the mic array is mounted mirrored.
DOA_FRONT = float(os.environ.get("DOA_FRONT", "180"))
DOA_SIGN = float(os.environ.get("DOA_SIGN", "1"))

_q: "queue.Queue[dict]" = queue.Queue(maxsize=512)
_writer: threading.Thread | None = None
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Who is in the room                                                          #
# --------------------------------------------------------------------------- #
def _people() -> list[dict]:
    try:
        with urllib.request.urlopen(f"{MEM_URL}/current", timeout=2) as r:
            d = json.loads(r.read() or b"{}") or {}
        if d.get("paused"):
            return []
        return d.get("people") or []
    except Exception:  # noqa: BLE001 — no camera is not a reason to lose the line
        return []


def _doa_side() -> float | None:
    """Where the current speech is coming from, as a -1..1 left/right position
    in the same units reachy_memory uses for a face's `x`. None when the robot
    can't say — no speech detected, or no answer."""
    if not REACHY_URL:
        return None
    try:
        with urllib.request.urlopen(
                f"{REACHY_URL}/api/state/full?with_doa=true", timeout=2) as r:
            st = json.loads(r.read())
        doa = st.get("doa") or {}
        if not doa.get("speech_detected"):
            return None
        side = DOA_SIGN * (float(doa.get("angle", DOA_FRONT)) - DOA_FRONT) / 90.0
        return max(-1.0, min(1.0, side))
    except Exception:  # noqa: BLE001
        return None


def _attribute(who: str) -> tuple[str | None, str | None, list[str]]:
    """(speaker, how, everyone_present) for a line about to be written."""
    if who == "vibey":
        return "Vibey", None, []
    people = _people()
    present = [p.get("name") or "?" for p in people]
    named = [p for p in people if p.get("name")]
    if not named:
        return None, None, present
    if len(named) == 1:
        # One nameable face in the room. The only case worth being sure about.
        return named[0]["name"], "sole-face", present
    # Several known faces. Let the mic array break the tie, and say that it did.
    side = _doa_side()
    if side is None:
        return None, None, present
    best = min(named, key=lambda p: abs(float(p.get("x", 0.0)) - side))
    return best["name"], "doa", present


# --------------------------------------------------------------------------- #
# Writing                                                                     #
# --------------------------------------------------------------------------- #
def _path_for(day: str) -> str:
    return os.path.join(DIR, f"{day}.jsonl")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _drain() -> None:
    while True:
        item = _q.get()
        try:
            who, text, ts = item["who"], item["text"], item["ts"]
            speaker, by, present = _attribute(who)
            row = {"ts": ts, "who": who, "speaker": speaker, "text": text,
                   "by": by, "present": present}
            os.makedirs(DIR, exist_ok=True)
            # Line-buffered append with a trailing newline per row: a crash
            # mid-session costs the current line, never the file.
            with open(_path_for(_today()), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001 — never take the conversation down
            print(f"[transcript] write failed: {e}", flush=True)
        finally:
            _q.task_done()


def log(who: str, text: str) -> None:
    """Queue one line. Returns immediately; safe from any thread.

    `who` is normalised here rather than at the call sites, because the rest of
    the repo says "wonder"/"you" for historical reasons and a transcript read
    back in a year should not need that footnote.
    """
    if not ENABLED:
        return
    text = (text or "").strip()
    if not text:
        return
    who = {"wonder": "vibey", "you": "human"}.get(who, who)
    global _writer
    with _lock:
        if _writer is None or not _writer.is_alive():
            _writer = threading.Thread(target=_drain, daemon=True)
            _writer.start()
    try:
        _q.put_nowait({"who": who, "text": text,
                       "ts": datetime.now(timezone.utc).astimezone().isoformat(
                           timespec="seconds")})
    except queue.Full:
        # Dropping a line beats blocking the audio loop. Say so; a silent gap
        # in a transcript is the one failure that can't be noticed later.
        print("[transcript] queue full — dropped a line", flush=True)


# --------------------------------------------------------------------------- #
# Reading                                                                     #
# --------------------------------------------------------------------------- #
def days() -> list[str]:
    try:
        return sorted(f[:-6] for f in os.listdir(DIR) if f.endswith(".jsonl"))
    except FileNotFoundError:
        return []


def read(day: str | None = None, limit: int | None = None) -> list[dict]:
    day = day or _today()
    try:
        with open(_path_for(day), encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []
    return rows[-limit:] if limit else rows


def render(day: str | None = None) -> str:
    """The transcript as something a person would want to read."""
    rows = read(day)
    if not rows:
        return f"nothing recorded for {day or _today()}"
    out = []
    for r in rows:
        clock = (r.get("ts") or "")[11:16]
        name = r.get("speaker") or ("Vibey" if r.get("who") == "vibey" else "someone")
        # A guessed name is marked in the rendering too, not just the data.
        mark = "?" if r.get("by") == "doa" else ""
        out.append(f"{clock}  {name}{mark}: {r.get('text','')}")
    return "\n".join(out)


if __name__ == "__main__":
    if "--days" in sys.argv:
        d = days()
        print("\n".join(d) if d else "no transcripts yet")
    else:
        arg = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
        print(render(arg))
