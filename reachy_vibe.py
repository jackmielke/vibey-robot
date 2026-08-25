#!/usr/bin/env python3
"""
reachy_vibe.py — the vibe check.

A playful, deliberately shallow read of *how a conversation is going right now*,
scored 1-100 with an honest uncertainty band, a friendly one-liner, and an emoji
label. Stdlib only, no model call, sub-millisecond — the realtime brain supplies
the observations, this module just turns them into a number it can say out loud.

WHAT IT IS NOT
--------------
This scores the CONVERSATION, not the person. It is a party trick, not an
assessment. Hard guardrails, enforced here rather than trusted to the prompt:

  * No sensitive traits, ever. Notes mentioning identity (race, religion,
    gender, orientation, nationality, disability) are dropped, not stored.
  * No mental-health inference or diagnosis. Notes reading as clinical
    ("depressed", "anxious", "bipolar", "on the spectrum") are dropped, and
    the reply carries a note saying so.
  * No persistence of who said what. Logging stores a salted hash, never a name.
  * Uncertainty is reported, not hidden — one exchange gets a ±20 band and a
    "low" confidence, and the friendly line says as much.

USAGE (python)
--------------
    import reachy_vibe
    r = reachy_vibe.vibe_check(energy=8, warmth=9, humor=7, engagement=8,
                               turns=12, notes="lots of laughing, fast replies")
    r["score"]        -> 81
    r["range"]        -> [73, 89]
    r["label"]        -> "buzzing"
    r["emoji"]        -> "⚡"
    r["confidence"]   -> "high"
    r["say"]          -> "I'd call it 81 out of 100 — buzzing ⚡. ..."

Signals are 0-10, all optional; whichever you leave out simply don't count.
`turns` is how many back-and-forths you're basing it on and only affects
confidence.

USAGE (cli)
-----------
    python3 reachy_vibe.py                       # demo
    python3 reachy_vibe.py '{"energy":3,"warmth":6,"turns":2}'

USAGE (voice)
-------------
The `vibe_check` tool in reachy_openai_realtime.py. Vibey fills in the signals
from what they just heard and reads `say` back.

LOGGING TO SUPABASE (off by default)
------------------------------------
Two locks, both of which must be open before a single byte leaves the robot:

  1. VIBE_LOG=1 in .env             — the operator's switch (default 0 = off,
                                      which is a complete kill switch: set it
                                      to 0 and no code path can send anything).
  2. consent=True on the call       — the human's switch, per vibe check. The
                                      voice tool is instructed to ask out loud
                                      first: "want me to save that?"

Credentials are the existing SUPABASE_URL / SUPABASE_KEY from .env (never
committed, never logged). Rows go to a `vibe_checks` table:

    create table vibe_checks (
      id         bigserial primary key,
      created_at timestamptz not null default now(),
      anon_id    text,          -- salted hash, 12 chars, not reversible
      score      int  not null,
      label      text,
      confidence text,
      notes      text           -- already scrubbed by the guardrails above
    );

`anon_id` is sha256(session + salt)[:12]. Set VIBE_ANON_SALT in .env to keep
IDs stable across restarts; leave it unset and the salt is random per process,
so rows can be grouped within one session and never across sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import urllib.request

# 0-10 observations and how much each moves the number. Engagement is weighted
# hardest because a conversation where nobody is really there reads flat no
# matter how polite it is.
_WEIGHTS = {
    "energy": 1.0,      # pace, volume, exclamation
    "warmth": 1.0,      # friendliness, generosity
    "humor": 0.8,       # jokes, laughing
    "engagement": 1.2,  # questions back, follow-ups, actually listening
}

# score floor -> (label, emoji)
_LADDER = [
    (90, "immaculate", "✨"),
    (75, "buzzing", "⚡"),
    (60, "good", "😄"),
    (45, "steady", "🙂"),
    (30, "low-key", "😌"),
    (15, "flat", "😐"),
    (0,  "running on fumes", "🫠"),
]

# Notes matching these never get scored, spoken back, or stored. Cheap and
# blunt on purpose: a false positive costs one dropped note, a false negative
# costs a robot guessing at someone's diagnosis or identity out loud.
_BLOCKED = re.compile(
    r"\b("
    r"depress\w*|anxi\w*|bipolar|adhd|autis\w*|neurodiver\w*|on the spectrum|"
    r"ocd|ptsd|trauma\w*|suicid\w*|self.harm|manic|psychot\w*|diagnos\w*|"
    r"disorder|therapy|medicat\w*|meds|"
    r"gay|straight|queer|trans|gender|sexuality|orientation|"
    r"race|racial|black|white|asian|latin\w*|hispanic|jewish|muslim|christian|"
    r"religio\w*|immigrant|nationality|ethnic\w*|disab\w*|pregnan\w*"
    r")\b",
    re.I,
)

_SALT = os.environ.get("VIBE_ANON_SALT") or secrets.token_hex(8)


def logging_enabled() -> bool:
    """The operator's kill switch. False means nothing can be sent, at all."""
    return (os.environ.get("VIBE_LOG", "0").strip().lower() in ("1", "true", "yes")
            and bool(os.environ.get("SUPABASE_URL"))
            and bool(os.environ.get("SUPABASE_KEY")))


def _clean_notes(notes: str) -> tuple[str, bool]:
    """Return (safe notes, blocked?). Whole note goes if any of it trips."""
    notes = " ".join(str(notes or "").split())[:200]
    if notes and _BLOCKED.search(notes):
        return "", True
    return notes, False


def _band(n_signals: int, turns: int) -> tuple[int, str]:
    """How wide the error bars are, and what to call that. Two numbers drive it:
    how many things I actually noticed, and how long I've had to notice them."""
    if n_signals >= 3 and turns >= 8:
        return 8, "high"
    if n_signals >= 2 and turns >= 3:
        return 14, "medium"
    return 20, "low"


def vibe_check(energy=None, warmth=None, humor=None, engagement=None,
               notes: str = "", turns: int = 0, consent: bool = False,
               session: str = "live", log: bool = False) -> dict:
    """Score the vibe of the conversation. Everything optional; see module docs."""
    given = {k: v for k, v in (("energy", energy), ("warmth", warmth),
                               ("humor", humor), ("engagement", engagement))
             if v is not None}
    signals = {}
    for k, v in given.items():
        try:
            signals[k] = max(0.0, min(10.0, float(v)))
        except (TypeError, ValueError):
            continue

    if not signals:
        return {"error": "no signals given — rate at least one of "
                         "energy, warmth, humor, engagement from 0 to 10",
                "say": "I need something to go on first — give me a minute of "
                       "talking and ask again."}

    num = sum(signals[k] * _WEIGHTS[k] for k in signals)
    den = sum(_WEIGHTS[k] for k in signals)
    score = int(round(max(1.0, min(100.0, num / den * 10.0))))

    label, emoji = next((l, e) for floor, l, e in _LADDER if score >= floor)
    spread, confidence = _band(len(signals), int(turns or 0))
    lo, hi = max(1, score - spread), min(100, score + spread)

    safe_notes, blocked = _clean_notes(notes)
    hedge = {"high": "and I'm fairly sure",
             "medium": "give or take",
             "low": "though that's a wild guess this early"}[confidence]

    say = (f"I'd call it {score} out of 100 — {label} {emoji}, {hedge} "
           f"(somewhere between {lo} and {hi}).")
    if safe_notes:
        say += f" Mostly because: {safe_notes}."
    say += " That's the vibe of this chat, not a verdict on you."

    out = {
        "score": score,
        "range": [lo, hi],
        "label": label,
        "emoji": emoji,
        "confidence": confidence,
        "signals": {k: round(v, 1) for k, v in signals.items()},
        "turns": int(turns or 0),
        "notes": safe_notes,
        "say": say,
        "disclaimer": ("A read on the conversation's tone, not the person. "
                       "Not a personality test, not a health assessment."),
        "logged": False,
    }
    if blocked:
        out["notes_dropped"] = ("Notes mentioned something sensitive, so I "
                                "dropped them. Vibe checks stay on the surface.")

    if log:
        out["logged"], out["log_status"] = _maybe_log(out, consent, session)
    return out


def _maybe_log(result: dict, consent: bool, session: str) -> tuple[bool, str]:
    """Both locks, then one small POST. Fails quiet and honest — a dead network
    must never cost the conversation more than a sentence."""
    if not logging_enabled():
        return False, "logging is switched off (VIBE_LOG=0 or no Supabase creds)"
    if not consent:
        return False, "no consent given — nothing was sent"

    anon = hashlib.sha256(f"{session}{_SALT}".encode()).hexdigest()[:12]
    row = {
        "anon_id": anon,
        "score": result["score"],
        "label": result["label"],
        "confidence": result["confidence"],
        "notes": result["notes"],
    }
    key = os.environ["SUPABASE_KEY"]
    req = urllib.request.Request(
        os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/vibe_checks",
        data=json.dumps(row).encode(),
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json", "Prefer": "return=minimal"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            if r.status < 300:
                return True, f"saved as {anon}"
            return False, f"supabase said {r.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"could not save: {e}"


if __name__ == "__main__":
    if len(sys.argv) > 1:
        kwargs = json.loads(sys.argv[1])
    else:
        kwargs = {"energy": 8, "warmth": 9, "humor": 7, "engagement": 8,
                  "turns": 12, "notes": "lots of laughing, fast replies"}
    print(json.dumps(vibe_check(**kwargs), indent=2))
