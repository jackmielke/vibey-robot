"""Vibey's stream of consciousness: one small shared event log.

    emit("action", "waved", detail={...}, source="voice")

Anything that enters or leaves Vibey's head lands here: a tool call, a text
arriving on Telegram, a face, a clap, the brain handing a question to its
backend. The dashboard's Live chat and the phone's Chat tab show them inline
between the chat bubbles (GET :8772/events?since=<id>).

One owner, many writers. The chat service (reachy_chat.py) holds the ring of
the last ~500 events and is the only process that writes data/events.jsonl.
Every other process calls the same emit(); it is posted to the chat service on
a background thread (localhost only, dropped if the chat service is down), so
emitting never blocks whatever is happening. Incognito is enforced in one
place: while it is on, events live in memory only.

Kinds, and their colour on screen:
    chat      heard / said (the transcript itself; rarely emitted)
    thinking  violet: delegation to the backend, reasoning, context injected
    action    yellow: moves, emotes, memory, DJ, volume, driving
    telegram  blue:   texts in and out, photos sent, commands
    senses    cyan:   faces, gestures, claps, wake word, front-desk scans
    system    grey:   wake, sleep, on/off, privacy, brain switch, healer

Nothing secret goes in: tokens and long digit runs (phone numbers, chat ids)
are scrubbed from every string, and callers keep guest message bodies short.
Python 3.9 compatible on purpose: the gesture service runs on Xcode's python.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

KINDS = ("chat", "thinking", "action", "telegram", "senses", "system")
CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8772").rstrip("/")
PATH = Path(__file__).resolve().parent / "data" / "events.jsonl"
ROTATE_BYTES = 2 * 1024 * 1024
RING_N = 500

_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}"              # OpenAI-style keys
    r"|\b\d{6,}:[A-Za-z0-9_\-]{20,}"        # Telegram bot tokens
    r"|[Bb]earer\s+\S+"
    r"|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-.]+"  # JWTs
    r"|\+?\d(?:[ \-]?\d){8,})")              # phone numbers, chat ids (9+ digits)


def scrub(v, limit: int = 600):
    """Strip secrets and long numbers from any JSON-ish value, cap strings."""
    if isinstance(v, str):
        s = _SECRET.sub("•••", v)
        return s if len(s) <= limit else s[: limit - 1] + "…"
    if isinstance(v, dict):
        return {str(k)[:40]: scrub(x, limit) for k, x in list(v.items())[:30]
                if not re.search(r"token|secret|key|password|chat_id|phone", str(k), re.I)}
    if isinstance(v, (list, tuple)):
        return [scrub(x, limit) for x in list(v)[:30]]
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return scrub(str(v), limit)


def short(text, n: int = 70) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# The owner side: only the chat service calls serve()                         #
# --------------------------------------------------------------------------- #
_ring: deque = deque(maxlen=RING_N)
_lock = threading.Lock()
_next = {"id": 0}
_serving = {"on": False, "persist": lambda: True, "turn": lambda: None}


def serve(persist_ok=None, current_turn=None) -> None:
    """Make this process the owner. persist_ok() False = incognito (memory
    only). current_turn() returns the ts (ms) of the line that was last heard,
    so an event can point back at the turn that caused it."""
    _serving["on"] = True
    if persist_ok:
        _serving["persist"] = persist_ok
    if current_turn:
        _serving["turn"] = current_turn
    # Seed from disk so a restart doesn't blank the timeline.
    try:
        lines = PATH.read_text().splitlines()[-RING_N:]
        for ln in lines:
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            _ring.append(ev)
        if _ring:
            _next["id"] = max(int(e.get("id") or 0) for e in _ring)
    except OSError:
        pass


def ingest(ev: dict) -> dict:
    """Owner only: stamp, ring, persist. Returns the stored event."""
    kind = ev.get("kind") if ev.get("kind") in KINDS else "system"
    out = {"kind": kind,
           "text": scrub(short(ev.get("text"), 160)),
           "ts": int(ev.get("ts") or time.time() * 1000),
           "source": scrub(short(ev.get("source") or "", 24))}
    if ev.get("icon"):
        out["icon"] = short(ev["icon"], 4)
    if ev.get("detail"):
        out["detail"] = scrub(ev["detail"])
    try:
        turn = ev.get("turn") or _serving["turn"]()
        if turn:
            out["turn"] = int(turn)
    except Exception:  # noqa: BLE001
        pass
    with _lock:
        _next["id"] += 1
        out["id"] = _next["id"]
        _ring.append(out)
    try:
        persist = bool(_serving["persist"]())
    except Exception:  # noqa: BLE001
        persist = False          # unsure means incognito
    if persist:
        _append(out)
    return out


def _append(ev: dict) -> None:
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        if PATH.exists() and PATH.stat().st_size > ROTATE_BYTES:
            PATH.replace(PATH.with_suffix(".1.jsonl"))
        with open(PATH, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[events] write failed: {e}", flush=True)


def since(after: int = 0, limit: int = 300) -> list:
    with _lock:
        out = [e for e in _ring if e["id"] > after]
    return out[-limit:]


def last_id() -> int:
    return _next["id"]


# --------------------------------------------------------------------------- #
# Everyone: emit()                                                            #
# --------------------------------------------------------------------------- #
_outbox: "queue.Queue" = queue.Queue(maxsize=200)
_sender = {"t": None}


def _send_loop() -> None:
    while True:
        ev = _outbox.get()
        try:
            req = urllib.request.Request(
                f"{CHAT_URL}/events", data=json.dumps(ev).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2).read()
        except Exception:  # noqa: BLE001 — chat service down: the event is lost
            pass


def emit(kind: str, text: str, detail: dict | None = None, source: str = "",
         icon: str = "", turn: int | None = None) -> None:
    """Never raises, never blocks."""
    try:
        ev = {"kind": kind, "text": text, "detail": detail, "source": source,
              "icon": icon, "ts": int(time.time() * 1000), "turn": turn}
        if _serving["on"]:
            ingest(ev)
            return
        if _sender["t"] is None:
            _sender["t"] = threading.Thread(target=_send_loop, daemon=True)
            _sender["t"].start()
        _outbox.put_nowait(ev)
    except Exception:  # noqa: BLE001
        pass
