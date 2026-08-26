"""What the realtime session has actually cost, in dollars, since midnight.

The account's own usage API needs an admin-scoped key, which the robot does not
have and should not have. But every `response.done` carries the token counts for
the turn that just happened, so the meter can be read from the conversation
itself — and unlike the dashboard, it is current to the last sentence spoken.

Kept in a file rather than in memory because the point of the number is to
survive the thing that produced it: the question is asked in the morning, after
a night that may have included a crash, a restart, or a session nobody meant to
leave open.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

PATH = Path(__file__).parent / ".cost.json"
_LOCK = threading.Lock()

# gpt-realtime-mini, dollars per million tokens, from OpenAI's pricing page.
# Audio is the one that matters — text is rounding error next to it.
RATES = {
    "text_in": 0.60, "text_out": 2.40,
    "audio_in": 10.00, "audio_out": 20.00,
    "cached_in": 0.30,
}


def _load() -> dict:
    try:
        d = json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        d = {}
    d.setdefault("turns", [])
    return d


def record(usage: dict) -> None:
    """Log one turn's usage. Called from the realtime receiver; never raises."""
    try:
        det_in = (usage.get("input_token_details") or {})
        det_out = (usage.get("output_token_details") or {})
        cached = (det_in.get("cached_tokens_details") or {})
        turn = {
            "at": time.time(),
            "text_in": det_in.get("text_tokens", 0) - cached.get("text_tokens", 0),
            "audio_in": det_in.get("audio_tokens", 0) - cached.get("audio_tokens", 0),
            "cached_in": det_in.get("cached_tokens", 0),
            "text_out": det_out.get("text_tokens", 0),
            "audio_out": det_out.get("audio_tokens", 0),
        }
        turn["usd"] = sum(max(turn.get(k, 0), 0) * v / 1_000_000
                          for k, v in RATES.items())
        with _LOCK:
            d = _load()
            d["turns"].append(turn)
            # A week is enough to answer "what did last night cost" and small
            # enough that the file never needs thinking about again.
            cutoff = time.time() - 7 * 86400
            d["turns"] = [t for t in d["turns"] if t["at"] > cutoff]
            PATH.write_text(json.dumps(d))
    except Exception:  # noqa: BLE001 — a billing meter must never break a call
        pass


def _sum(since: float) -> tuple[float, int]:
    with _LOCK:
        turns = [t for t in _load()["turns"] if t["at"] > since]
    return sum(t.get("usd", 0.0) for t in turns), len(turns)


def summary() -> dict:
    now = time.time()
    midnight = datetime.now().replace(hour=0, minute=0, second=0,
                                      microsecond=0).timestamp()
    hour_usd, hour_turns = _sum(now - 3600)
    today_usd, today_turns = _sum(midnight)
    week_usd, _ = _sum(now - 7 * 86400)
    # Last night specifically, because that is the question actually being
    # asked when somebody checks this in the morning.
    last_night = (datetime.now().replace(hour=0, minute=0, second=0,
                                         microsecond=0) - timedelta(hours=6)
                  ).timestamp()
    night_usd, _ = _sum(last_night)
    return {"hour": round(hour_usd, 3), "hour_turns": hour_turns,
            "today": round(today_usd, 2), "today_turns": today_turns,
            "since_midnight_minus_6h": round(night_usd, 2),
            "week": round(week_usd, 2)}


def spoken() -> str:
    s = summary()
    if not s["today_turns"]:
        return "Nothing today — I haven't cost you a penny yet."
    return (f"About ${s['today']:.2f} today across {s['today_turns']} turns, "
            f"${s['hour']:.2f} in the last hour.")
