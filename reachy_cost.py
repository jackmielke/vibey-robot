"""What Vibey costs on OpenAI, per source, in dollars — and the budget guard.

Every call Vibey makes with OPENAI_API_KEY is recorded here, priced from ONE
table, into data/cost.db (SQLite). Sources:

    live_voice      GPT-Live voice layer, billed per second the socket is open
    live_backend    the gpt-5.5 model GPT-Live delegates thinking to (tokens)
    realtime_voice  gpt-realtime-2.1(-mini) sessions (tokens per response)
    text_brain      Jack's Telegram texts (Responses API, reachy_brain)
    guest_brain     strangers / group chats (chat completions, reachy_chat)
    vision          look_at_the_room (reachy_scene, chat completions + image)
    other           scribe notes, realtime input transcription, anything else

Not OpenAI, so not here: TTS is ElevenLabs (reachy_voice), speech-to-text for
wake words and scribe is local faster-whisper.

A price the table doesn't know is recorded with usd=0 and priced=0, and the
summary reports how many such calls there were. Never guessed silently.

The account's real bill (all apps on the key's org) comes from the admin costs
API when OPENAI_ADMIN_KEY is set — see refresh_billed().
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "cost.db"
LEGACY_JSON = ROOT / ".cost.json"
_LOCK = threading.Lock()

SOURCES = ("live_voice", "live_backend", "realtime_voice", "text_brain",
           "guest_brain", "vision", "other")

# --------------------------------------------------------------------------- #
# PRICES. Checked 2026-10-02 against https://developers.openai.com/api/docs/pricing
# (Standard tier). Dollars per 1M tokens unless a key says per_min.
# Update the date when you re-check. A model not listed here is UNPRICED.
# --------------------------------------------------------------------------- #
PRICES_CHECKED = "2026-10-02"
PRICES_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICES = {
    # GPT-Live voice layer: $0.05/min, billed per second, no rounding.
    # Backend model + tools billed separately (as gpt-5.5 tokens below).
    "gpt-live-1": {"per_min": 0.05},
    # gpt-5.5, <272K context. Reasoning tokens are billed as output.
    "gpt-5.5": {"in": 5.00, "cached_in": 0.50, "out": 30.00},
    "gpt-4o-mini": {"in": 0.15, "cached_in": 0.075, "out": 0.60},
    "gpt-realtime-2.1": {"text_in": 4.00, "text_cached": 0.40, "text_out": 24.00,
                         "audio_in": 32.00, "audio_cached": 0.40, "audio_out": 64.00,
                         "image_in": 5.00, "image_cached": 0.50},
    "gpt-realtime-2.1-mini": {"text_in": 0.60, "text_cached": 0.06, "text_out": 2.40,
                              "audio_in": 10.00, "audio_cached": 0.30, "audio_out": 20.00,
                              "image_in": 0.80, "image_cached": 0.08},
    # Realtime input transcription. ~$0.003/min per the same page.
    "gpt-4o-mini-transcribe": {"audio_in": 1.25, "text_in": 1.25, "text_out": 5.00,
                               "per_min": 0.003},
}


def _price(model: str) -> dict | None:
    m = (model or "").strip()
    if m in PRICES:
        return PRICES[m]
    # Dated snapshots ("gpt-5.5-2026-08-01") price like their family.
    for k in sorted(PRICES, key=len, reverse=True):
        if m.startswith(k + "-2"):
            return PRICES[k]
    return None


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS calls (
        at REAL NOT NULL, source TEXT NOT NULL, model TEXT, usd REAL NOT NULL,
        priced INTEGER NOT NULL DEFAULT 1, detail TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS calls_at ON calls(at)")
    c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    return c


def _migrate_legacy() -> None:
    """The old .cost.json (realtime turns only) moves in once."""
    if not LEGACY_JSON.exists():
        return
    try:
        turns = json.loads(LEGACY_JSON.read_text()).get("turns") or []
        with _LOCK, _db() as c:
            c.executemany(
                "INSERT INTO calls(at,source,model,usd,priced,detail) VALUES(?,?,?,?,1,?)",
                [(t["at"], "realtime_voice", "gpt-realtime-2.1-mini(legacy rates)",
                  float(t.get("usd") or 0), "migrated from .cost.json")
                 for t in turns if t.get("usd")])
        LEGACY_JSON.rename(LEGACY_JSON.with_suffix(".json.migrated"))
    except Exception:  # noqa: BLE001
        pass


def kv_get(k: str, default=None):
    try:
        with _LOCK, _db() as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default
    except Exception:  # noqa: BLE001
        return default


def kv_set(k: str, v) -> None:
    try:
        with _LOCK, _db() as c:
            c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))
    except Exception:  # noqa: BLE001
        pass


def _insert(source: str, model: str, usd: float, priced: bool, detail: dict) -> float:
    if source not in SOURCES:
        source = "other"
    try:
        with _LOCK, _db() as c:
            c.execute("INSERT INTO calls(at,source,model,usd,priced,detail) VALUES(?,?,?,?,?,?)",
                      (time.time(), source, model, float(usd), 1 if priced else 0,
                       json.dumps(detail)[:2000]))
    except Exception:  # noqa: BLE001 — a meter must never break a call
        pass
    return usd


# --------------------------------------------------------------------------- #
# Recording. All never raise.
# --------------------------------------------------------------------------- #
def record_minutes(source: str, model: str, minutes: float, detail: dict | None = None) -> float:
    if minutes <= 0:
        return 0.0
    p = _price(model)
    if not p or "per_min" not in p:
        return _insert(source, model, 0.0, False, {"minutes": minutes, **(detail or {})})
    return _insert(source, model, minutes * p["per_min"], True,
                   {"minutes": round(minutes, 4), **(detail or {})})


def record_tokens(source: str, model: str, usage: dict, detail: dict | None = None) -> float:
    """Takes any of the three usage shapes OpenAI returns:
    Responses (input_tokens / input_tokens_details.cached_tokens / output_tokens),
    Chat Completions (prompt_tokens / prompt_tokens_details / completion_tokens),
    Realtime (input_token_details.{text,audio,image}_tokens + cached_tokens_details)."""
    try:
        usage = usage or {}
        p = _price(model)
        d = dict(detail or {})
        if "input_token_details" in usage or "output_token_details" in usage:
            di = usage.get("input_token_details") or {}
            do = usage.get("output_token_details") or {}
            cd = di.get("cached_tokens_details") or {}
            n = {
                "text_in": di.get("text_tokens", 0) - cd.get("text_tokens", 0),
                "audio_in": di.get("audio_tokens", 0) - cd.get("audio_tokens", 0),
                "image_in": di.get("image_tokens", 0) - cd.get("image_tokens", 0),
                "text_cached": cd.get("text_tokens", 0),
                "audio_cached": cd.get("audio_tokens", 0),
                "image_cached": cd.get("image_tokens", 0),
                "text_out": do.get("text_tokens", 0),
                "audio_out": do.get("audio_tokens", 0),
            }
            # Transcription usage has an input split but a bare output count.
            if not do and usage.get("output_tokens"):
                n["text_out"] = usage.get("output_tokens", 0)
            if not any(n.values()):
                n["audio_in"] = usage.get("input_tokens", 0)
                n["text_out"] = usage.get("output_tokens", 0)
        else:
            inp = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
            det = (usage.get("input_tokens_details")
                   or usage.get("prompt_tokens_details") or {})
            cached = det.get("cached_tokens", 0) or 0
            out = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
            n = {"in": inp - cached, "cached_in": cached, "out": out}
        d["tokens"] = {k: v for k, v in n.items() if v}
        if not p:
            return _insert(source, model, 0.0, False, d)
        usd = 0.0
        missing = []
        for k, v in n.items():
            if not v:
                continue
            rate = p.get(k)
            if rate is None and k == "image_cached":
                rate = p.get("image_in")
            if rate is None:
                missing.append(k)
                continue
            usd += max(v, 0) * rate / 1_000_000
        if missing:
            d["unpriced_parts"] = missing
        return _insert(source, model, usd, not missing, d)
    except Exception:  # noqa: BLE001
        return 0.0


def record(usage: dict) -> None:
    """Back-compat: the realtime engine's response.done usage."""
    try:
        import reachy_openai_realtime as rt
        model = rt.MODEL
    except Exception:  # noqa: BLE001
        model = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
    record_tokens("realtime_voice", model, usage)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def _midnight() -> float:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _month_start() -> float:
    return datetime.now().replace(day=1, hour=0, minute=0, second=0,
                                  microsecond=0).timestamp()


def _by_source(since: float) -> tuple[dict, int]:
    with _LOCK, _db() as c:
        rows = c.execute("SELECT source, SUM(usd), SUM(1-priced) FROM calls "
                         "WHERE at>=? GROUP BY source", (since,)).fetchall()
    return ({r[0]: round(r[1] or 0, 4) for r in rows}, int(sum(r[2] or 0 for r in rows)))


def total(since: float) -> float:
    return round(sum(_by_source(since)[0].values()), 4)


def today() -> float:
    return total(_midnight())


def month() -> float:
    return total(_month_start())


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
def daily_cap() -> float:
    base = float(os.environ.get("VIBEY_DAILY_BUDGET", "3") or 3)
    ov = kv_get("override") or {}
    if ov.get("day") == datetime.now().strftime("%Y-%m-%d"):
        base += float(ov.get("raise") or 0)
    return base


def monthly_cap() -> float:
    base = float(os.environ.get("VIBEY_MONTHLY_BUDGET", "40") or 40)
    ov = kv_get("override") or {}
    if ov.get("month") == datetime.now().strftime("%Y-%m"):
        base += float(ov.get("month_raise") or 0)
    return base


def guard_off() -> bool:
    ov = kv_get("override") or {}
    return ov.get("off_day") == datetime.now().strftime("%Y-%m-%d")


def budget_state() -> dict:
    t, m = today(), month()
    dc, mc = daily_cap(), monthly_cap()
    frac = max(t / dc if dc else 0, m / mc if mc else 0)
    which = "month" if (mc and m / mc >= (t / dc if dc else 0)) else "day"
    level = "over" if frac >= 1 else "warn" if frac >= 0.8 else "ok"
    if guard_off():
        level = "off" if frac >= 0.8 else level
    return {"today": round(t, 2), "month": round(m, 2), "daily_cap": dc,
            "monthly_cap": mc, "fraction": round(frac, 3), "binding": which,
            "level": level, "guard_off": guard_off()}


def over_budget() -> bool:
    return budget_state()["level"] == "over"


def budget_raise(amount: float = 3.0) -> dict:
    """Owner override: more room today (and this month if the month cap binds)."""
    now = datetime.now()
    ov = kv_get("override") or {}
    day, mon = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    if ov.get("day") != day:
        ov["day"], ov["raise"] = day, 0
    ov["raise"] = float(ov.get("raise") or 0) + amount
    # If the month cap is the one pinching, a raise has to lift it too.
    if month() >= monthly_cap() * 0.8:
        if ov.get("month") != mon:
            ov["month"], ov["month_raise"] = mon, 0
        ov["month_raise"] = float(ov.get("month_raise") or 0) + amount
    kv_set("override", ov)
    return budget_state()


def budget_off() -> dict:
    ov = kv_get("override") or {}
    ov["off_day"] = datetime.now().strftime("%Y-%m-%d")
    kv_set("override", ov)
    return budget_state()


def budget_on() -> dict:
    ov = kv_get("override") or {}
    ov.pop("off_day", None)
    kv_set("override", ov)
    return budget_state()


OUT_OF_BUDGET_TEXT = ("I'm out of budget today, so I'm resting. "
                      "Jack can text /budget raise or /budget off.")


# --------------------------------------------------------------------------- #
# Ground truth: the account's own bill (admin key only)
# --------------------------------------------------------------------------- #
def _admin_key() -> str:
    return os.environ.get("OPENAI_ADMIN_KEY", "").strip()


def refresh_billed(force: bool = False) -> dict | None:
    """Pull /v1/organization/costs for this month, at most hourly. Read-only."""
    key = _admin_key()
    if not key:
        return None
    cached = kv_get("billed") or {}
    if not force and time.time() - float(cached.get("fetched_at") or 0) < 3600:
        return cached
    start = int(_month_start())
    url = (f"https://api.openai.com/v1/organization/costs?start_time={start}"
           f"&bucket_width=1d&limit=31&group_by=project_id")
    try:
        buckets = []
        page = None
        for _ in range(5):
            req = urllib.request.Request(url + (f"&page={page}" if page else ""),
                                         headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = json.loads(r.read())
            buckets += body.get("data") or []
            page = body.get("next_page")
            if not body.get("has_more") or not page:
                break
        mid = _midnight()
        t = m = 0.0
        projects: dict = {}
        for b in buckets:
            for res in b.get("results") or []:
                v = float(((res.get("amount") or {}).get("value")) or 0)
                m += v
                pid = res.get("project_id") or "(no project)"
                projects[pid] = projects.get(pid, 0) + v
                if float(b.get("start_time") or 0) >= mid - 1:
                    t += v
        out = {"today": round(t, 2), "month": round(m, 2),
               "projects": {k: round(v, 2) for k, v in
                            sorted(projects.items(), key=lambda kv: -kv[1])},
               "fetched_at": time.time(), "error": None}
    except Exception as e:  # noqa: BLE001
        out = {**cached, "error": str(e)[:200], "fetched_at": time.time()}
    kv_set("billed", out)
    return out


def billed() -> dict:
    if not _admin_key():
        return {"available": False,
                "hint": "Add OPENAI_ADMIN_KEY to .env (platform.openai.com → "
                        "Settings → Admin keys, read-only) to see the real bill."}
    b = kv_get("billed") or {}
    return {"available": True, **b}


# --------------------------------------------------------------------------- #
# Summary for dashboard / iOS / Telegram / voice
# --------------------------------------------------------------------------- #
def summary() -> dict:
    now = time.time()
    td, unpriced_today = _by_source(_midnight())
    wk, _ = _by_source(now - 7 * 86400)
    mo, unpriced_month = _by_source(_month_start())
    hr, _ = _by_source(now - 3600)
    with _LOCK, _db() as c:
        n_today = c.execute("SELECT COUNT(*) FROM calls WHERE at>=?",
                            (_midnight(),)).fetchone()[0]
    b = budget_state()
    return {
        "today": round(sum(td.values()), 2), "today_turns": n_today,
        "hour": round(sum(hr.values()), 3),
        "week": round(sum(wk.values()), 2), "month": round(sum(mo.values()), 2),
        "by_source_today": {k: round(v, 3) for k, v in td.items()},
        "by_source_week": {k: round(v, 3) for k, v in wk.items()},
        "by_source_month": {k: round(v, 3) for k, v in mo.items()},
        "unpriced_today": unpriced_today, "unpriced_month": unpriced_month,
        "budget": b, "billed": billed(),
        "prices_checked": PRICES_CHECKED,
    }


LABELS = {"live_voice": "live voice", "live_backend": "live brain",
          "realtime_voice": "realtime", "text_brain": "texts",
          "guest_brain": "guests", "vision": "vision", "other": "other"}


def detail(days: int = 7, recent: int = 40) -> dict:
    """Everything behind the "$ today" pill: per-day and per-hour spend, per
    model (calls, dollars, voice minutes where the meter knows them) and the
    latest individual charges."""
    now = time.time()
    mid = _midnight()
    start = mid - (days - 1) * 86400
    with _LOCK, _db() as c:
        rows = c.execute("SELECT at, source, model, usd, detail FROM calls WHERE at >= ? "
                         "ORDER BY at", (start,)).fetchall()
    daily = {}
    for i in range(days):
        d = datetime.fromtimestamp(start + i * 86400 + 3600)
        daily[d.strftime("%Y-%m-%d")] = {"day": d.strftime("%a"), "date": d.strftime("%Y-%m-%d"),
                                         "usd": 0.0, "calls": 0, "minutes": 0.0}
    hourly = [{"hour": h, "usd": 0.0} for h in range(24)]
    models: dict = {}
    for at, src, model, usd, det in rows:
        try:
            mins = float((json.loads(det or "{}") or {}).get("minutes") or 0)
        except Exception:  # noqa: BLE001
            mins = 0.0
        key = datetime.fromtimestamp(at).strftime("%Y-%m-%d")
        if key in daily:
            daily[key]["usd"] += usd
            daily[key]["calls"] += 1
            daily[key]["minutes"] += mins
        today = at >= mid
        if today:
            hourly[datetime.fromtimestamp(at).hour]["usd"] += usd
        name = (model or "?").split("(")[0]
        m = models.setdefault((name, src), {"model": name, "source": LABELS.get(src, src),
                                     "usd_today": 0.0, "usd_week": 0.0, "calls_today": 0,
                                     "calls_week": 0, "minutes_today": 0.0, "minutes_week": 0.0})
        m["usd_week"] += usd
        m["calls_week"] += 1
        m["minutes_week"] += mins
        if today:
            m["usd_today"] += usd
            m["calls_today"] += 1
            m["minutes_today"] += mins
    rnd = lambda d: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}
    recent_rows = [r for r in rows if r[0] >= now - 86400 * days][-recent:][::-1]
    return {
        "today": round(today_total := total(mid), 2),
        "days": [rnd(v) for v in daily.values()],
        "hours_today": [rnd(h) for h in hourly],
        "models": sorted((rnd(m) for m in models.values()),
                         key=lambda m: -m["usd_week"]),
        "voice_minutes_today": round(sum(m["minutes_today"] for m in models.values()), 1),
        "voice_replies_today": sum(m["calls_today"] for m in models.values()
                                   if m["source"] in ("realtime", "live voice")),
        "recent": [{"at": at, "source": LABELS.get(src, src), "model": (model or "?").split("(")[0],
                    "usd": round(usd, 4)} for at, src, model, usd, _ in recent_rows],
        "budget": budget_state(),
    }


def text_report() -> str:
    s = summary()
    b = s["budget"]
    lines = [f"💸 ${s['today']:.2f} today · ${s['week']:.2f} 7d · ${s['month']:.2f} this month"]
    parts = [f"{LABELS.get(k, k)} ${v:.2f}" for k, v in
             sorted(s["by_source_today"].items(), key=lambda kv: -kv[1]) if v >= 0.005]
    if parts:
        lines.append("today: " + ", ".join(parts))
    if s["unpriced_today"]:
        lines.append(f"+{s['unpriced_today']} calls with no known price")
    lines.append(f"budget: ${b['today']:.2f}/${b['daily_cap']:.2f} today, "
                 f"${b['month']:.2f}/${b['monthly_cap']:.2f} month"
                 + (" · guard OFF today" if b["guard_off"] else "")
                 + (" · OUT OF BUDGET" if b["level"] == "over" else ""))
    bl = s["billed"]
    if bl.get("available") and bl.get("fetched_at"):
        lines.append(f"OpenAI billed (all apps on the account): ${bl.get('today', 0):.2f} "
                     f"today, ${bl.get('month', 0):.2f} this month")
    elif not bl.get("available"):
        lines.append(bl["hint"])
    return "\n".join(lines)


def spoken() -> str:
    s = summary()
    if not s["today_turns"]:
        return "Nothing today, I haven't cost you a penny yet."
    top = max(s["by_source_today"].items(), key=lambda kv: kv[1], default=("", 0))
    return (f"About ${s['today']:.2f} today and ${s['month']:.2f} this month. "
            f"Most of today was {LABELS.get(top[0], top[0])}.")


_migrate_legacy()


if __name__ == "__main__":
    import sys
    from reachy_voice import load_env
    load_env()
    if "--billed" in sys.argv:
        print(json.dumps(refresh_billed(force=True), indent=1))
    print(text_report())
