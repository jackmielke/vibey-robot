#!/usr/bin/env python3
"""
reachy_supermemory.py — what Vibey remembers about conversations.

Vibey could already remember two things: a FACE (reachy_memory.py, Supabase) and
a short list of rules about how to behave (SKILLS.md). Nothing held what was
actually SAID. So it could greet you by name and have no idea what you talked
about last time, which is the part people mean by "it remembers me".

The rules stay in SKILLS.md because they belong in every prompt. Everything else
lives here, searchable, because it does not fit in a prompt and should not try:
SKILLS.md is loaded whole into the realtime brain's instructions at connect, so
every line ever remembered was a line paid for on every single connection.

    remember("Jack is training for a half marathon", person="Jack")
    recall("what has Jack been training for")      -> a few short lines
    recall("the trip", person="Jack")              -> only things tied to Jack

Faces stay in Supabase. This stores what was said, tagged with who said it, so
the two halves join on a name.

Env:
    SUPERMEMORY_API_KEY   required — nothing here runs without it
    SUPERMEMORY_SPACE     default "vibey"
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

API = "https://api.supermemory.ai"

# Stored bare in .env, not as "Bearer sm_…". A value with a space in it breaks
# `source .env`, which is how every service in this repo gets its config — the
# shell reads the second word as a command and the key silently arrives empty.
KEY = os.environ.get("SUPERMEMORY_API_KEY", "").strip()
_AUTH = f"Bearer {KEY}" if KEY and not KEY.lower().startswith("bearer ") else KEY

# Vibey's own space, deliberately NOT the account default.
#
# The default space holds Jack's own notes — profile, projects, travel, the
# things he has saved for himself over months. A desk robot that can be asked
# out loud, in a room with other people in it, should not be able to read those
# back. It gets a space it filled itself.
SPACE = os.environ.get("SUPERMEMORY_SPACE", "vibey").strip() or "vibey"

# ALWAYS the plural `containerTags`, never the singular `containerTag`.
#
# Both are documented for /v3/search. The singular one is ignored: searching a
# container tag that does not exist at all still returned Jack's personal
# profile, his sports and his travel history — measured, not assumed. The plural
# form filters correctly (a nonsense tag returns zero results). Getting this
# wrong is not a bug that shows up as an error; it shows up as the robot knowing
# things nobody told it, in front of guests.
_TAGS = [SPACE]

TIMEOUT = 8.0

# Below this, a hit is dropped rather than spoken. Calibrated from exactly two
# measurements (a real match at 0.681, an unrelated one at 0.527) on a space
# holding a single document, which is not much of a calibration — revisit once
# there is a real corpus in here. Raise it if Vibey starts "remembering" things
# nobody said; lower it if it keeps claiming it was never told.
MIN_SCORE = float(os.environ.get("SUPERMEMORY_MIN_SCORE", "0.6"))


def available() -> bool:
    return bool(KEY)


def _post(path: str, payload: dict, timeout: float = TIMEOUT) -> dict:
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(payload).encode(),
        headers={"Authorization": _AUTH, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def remember(note: str, person: str | None = None) -> str:
    """Store one thing. Returns the note, or "" if it went nowhere.

    Fire-and-forget on a thread: the write is queued server-side anyway, and a
    conversation should never stall on a memory being filed. A remembered thing
    that takes two seconds to acknowledge reads, out loud, as the robot losing
    its train of thought.
    """
    note = " ".join(str(note or "").split()).strip()
    if not note or not available():
        return ""

    def _write() -> None:
        try:
            meta = {"source": "vibey"}
            if person:
                meta["person"] = person
            _post("/v3/documents", {
                "content": note,
                "containerTags": _TAGS,
                "metadata": meta,
                # When it was said, not when it was filed — they differ whenever
                # a write is retried, and "last Tuesday" should mean last Tuesday.
                "documentDate": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime()),
            })
        except Exception as e:  # noqa: BLE001
            print(f"[supermemory] write failed: {e}", flush=True)

    threading.Thread(target=_write, daemon=True).start()
    return note


def recall(query: str, person: str | None = None, limit: int = 5) -> str:
    """Search what Vibey has been told. Returns short lines, or "" for nothing.

    Blocking, because the answer is the point — but capped: the room is waiting
    while this runs. Measured at ~0.9s against this account.
    """
    query = " ".join(str(query or "").split()).strip()
    if not query or not available():
        return ""
    body = {"q": query, "containerTags": _TAGS, "limit": max(1, min(int(limit), 10)),
            "rerank": True}
    if person:
        # Narrow to one person when asked. Kept as a filter rather than glued
        # into the query text: "what did Sam say" should not also match a
        # sentence that merely mentions Sam.
        body["filters"] = {"AND": [{"key": "person", "value": person,
                                    "negate": False}]}
    try:
        d = _post("/v3/search", body)
    except Exception as e:  # noqa: BLE001
        print(f"[supermemory] search failed: {e}", flush=True)
        return ""

    lines, seen = [], set()
    for r in d.get("results") or []:
        for c in r.get("chunks") or []:
            # Semantic search always hands back its best in-space match, however
            # bad. Asked "what sports does Jack play" with nothing about sports
            # stored, it returned a note about the camera at 0.527 while a real
            # match of the same note scored 0.681 — and `isRelevant` came back
            # True for both, so that flag decides nothing. Out loud, a weak match
            # is worse than nothing: "I don't think you've told me" is a fine
            # answer, and confidently recalling the wrong thing is not.
            if float(c.get("score") or 0.0) < MIN_SCORE:
                continue
            t = " ".join(str(c.get("content") or "").split()).strip()
            if t and t not in seen:
                seen.add(t)
                lines.append(t)
    return "\n".join(f"- {l}" for l in lines[:limit])


def forget_all() -> int:
    """Empty Vibey's space. Nothing calls this — it is here so that "delete
    everything you know about me" is one function and not an afternoon."""
    if not available():
        return 0
    n = 0
    try:
        d = _post("/v3/documents/list", {"containerTags": _TAGS, "limit": 200})
        for doc in d.get("memories") or d.get("documents") or []:
            did = doc.get("id")
            if not did:
                continue
            req = urllib.request.Request(f"{API}/v3/documents/{did}",
                                         headers={"Authorization": _AUTH},
                                         method="DELETE")
            try:
                urllib.request.urlopen(req, timeout=TIMEOUT).read()
                n += 1
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        print(f"[supermemory] forget_all failed: {e}", flush=True)
    return n


if __name__ == "__main__":
    import sys
    if not available():
        sys.exit("SUPERMEMORY_API_KEY not set (see .env)")
    if len(sys.argv) > 2 and sys.argv[1] == "remember":
        print(remember(" ".join(sys.argv[2:])) or "(nothing stored)")
        time.sleep(2)  # the write is on a thread; let it land before exiting
    elif len(sys.argv) > 2 and sys.argv[1] == "recall":
        print(recall(" ".join(sys.argv[2:])) or "(nothing found)")
    else:
        print(__doc__)
