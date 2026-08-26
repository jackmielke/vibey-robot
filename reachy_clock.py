#!/usr/bin/env python3
"""
reachy_clock.py — what time it is, said out loud.

Vibey could set alarms but could not answer "what time is it?" — the brain had
no clock it could reach. This is that clock: system time, a configurable
timezone that survives restarts, and an answer shaped for a speaker rather than
a screen ("It's twenty past three in the afternoon"), never "15:20".

Timezone precedence, first hit wins:

    explicit argument  →  clock.json (set by voice)  →  $VIBEY_TZ / $TZ  →  system

Daylight saving is not special-cased: every datetime here is timezone-aware and
comes out of zoneinfo, so the offset is whatever the tz database says it is for
that instant. What IS handled is the human part — if the clocks move within the
next day, the answer says so, because that is the one day people ask.

    python3 reachy_clock.py                 # say the time here
    python3 reachy_clock.py Asia/Tokyo      # say the time there

stdlib only.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_PATH = Path(__file__).parent / "clock.json"

# 12-hour is the American default and Jack's; "auto" reads the shell locale so a
# robot carried anywhere else stops sounding foreign.
HOUR_CYCLES = ("12", "24", "auto")

_ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}


class UnknownZone(ValueError):
    """A timezone name the tz database has never heard of."""


def _system_zone() -> tzinfo:
    """The machine's own zone, as a live zoneinfo whenever we can name it.

    `datetime.now().astimezone()` only ever hands back the offset in force right
    now, which quietly stops being true the night the clocks move — so read the
    name off /etc/localtime first and keep a real zone.
    """
    try:
        link = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in link:
            return ZoneInfo(link.split("/zoneinfo/", 1)[1])
    except Exception:  # noqa: BLE001
        pass
    return datetime.now().astimezone().tzinfo


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _load_config() -> dict:
    """Whatever is in clock.json, or {}. A corrupt file is not worth a crash —
    the robot should still be able to tell you the time in system-local."""
    try:
        data = json.loads(CONFIG_PATH.read_text() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_config(patch: dict) -> None:
    cfg = _load_config()
    cfg.update(patch)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def resolve_zone(name: str | None) -> tzinfo:
    """A tzinfo for `name`, or the configured default when name is None.

    Raises UnknownZone for a name the tz database does not carry, so callers can
    say "I don't know that place" instead of quietly answering in the wrong one.
    """
    if name is None:
        name = (_load_config().get("timezone")
                or os.environ.get("VIBEY_TZ")
                or os.environ.get("TZ")
                or "")
        name = str(name).strip()
        if not name:
            return _system_zone()
    name = str(name).strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    except Exception:  # noqa: BLE001  — no tz database at all
        return _system_zone()
    # Nobody says "Asia/Tokyo" out loud. They say "Tokyo".
    full = _zone_for_city(name)
    if full is None:
        raise UnknownZone(name)
    return ZoneInfo(full)


_CITIES: dict[str, str] | None = None


def _zone_for_city(name: str) -> str | None:
    """"new york" → "America/New_York". None when no zone ends in that city."""
    global _CITIES
    if _CITIES is None:
        try:
            from zoneinfo import available_timezones
            zones = sorted(available_timezones())
        except Exception:  # noqa: BLE001
            zones = []
        # Sorted so a repeat city name ("America/…" before "Europe/…") resolves
        # the same way every run rather than however the set happened to hash.
        _CITIES = {}
        for z in zones:
            _CITIES.setdefault(z.split("/")[-1].replace("_", " ").lower(), z)
    return _CITIES.get(" ".join(name.replace("_", " ").split()).lower())


def set_zone(name: str) -> str:
    """Remember `name` as the default timezone. Returns the stored name."""
    resolve_zone(name)  # validate before persisting
    clean = str(name).strip()
    _save_config({"timezone": clean})
    return clean


def hour_cycle() -> str:
    """"12" or "24" — never "auto"; that gets resolved here."""
    want = str(_load_config().get("hour_cycle")
               or os.environ.get("VIBEY_HOUR_CYCLE") or "auto").strip()
    if want not in HOUR_CYCLES:
        want = "auto"
    if want != "auto":
        return want
    locale = (os.environ.get("LC_TIME") or os.environ.get("LC_ALL")
              or os.environ.get("LANG") or "en_US")
    # The English-speaking 12-hour holdouts, plus the C locale.
    twelve = ("en_US", "en_CA", "en_AU", "en_NZ", "en_PH", "en_IN", "C", "POSIX")
    return "12" if locale.split(".")[0] in twelve else "24"


def set_hour_cycle(cycle: str) -> str:
    cycle = str(cycle).strip()
    if cycle not in HOUR_CYCLES:
        raise ValueError(f"hour cycle must be one of {HOUR_CYCLES}")
    _save_config({"hour_cycle": cycle})
    return cycle


# --------------------------------------------------------------------------- #
# Saying it
# --------------------------------------------------------------------------- #
def _words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, rest = divmod(n, 10)
    return _TENS[tens] + (f"-{_ONES[rest]}" if rest else "")


def _daypart(hour: int) -> str:
    if 5 <= hour < 12:
        return "in the morning"
    if 12 <= hour < 17:
        return "in the afternoon"
    if 17 <= hour < 21:
        return "in the evening"
    return "at night"


def zone_label(zone: tzinfo, when: datetime | None = None) -> str:
    """A name a speaker can pronounce: "Tokyo", not "Asia/Tokyo" or "JST"."""
    key = getattr(zone, "key", None)
    if key:
        return key.split("/")[-1].replace("_", " ")
    if when is not None:
        return when.strftime("%Z") or "here"
    return "here"


def spoken_clock(when: datetime, cycle: str | None = None) -> str:
    """Just the clock face, in words. No zone, no date."""
    cycle = cycle if cycle in ("12", "24") else hour_cycle()
    h24, minute = when.hour, when.minute
    if cycle == "24":
        if minute == 0:
            return "midnight" if h24 == 0 else f"{_words(h24)} hundred"
        if minute < 10:
            return f"{_words(h24)} oh {_words(minute)}"
        return f"{_words(h24)} {_words(minute)}"
    if minute == 0 and h24 == 12:
        return "noon"
    if minute == 0 and h24 == 0:
        return "midnight"
    h12 = h24 % 12 or 12
    if minute == 0:
        return f"{_words(h12)} o'clock"
    if minute == 15:
        return f"quarter past {_words(h12)}"
    if minute == 30:
        return f"half past {_words(h12)}"
    if minute == 45:
        return f"quarter to {_words((h24 + 1) % 12 or 12)}"
    if minute < 10:
        return f"{_words(h12)} oh {_words(minute)}"
    return f"{_words(h12)} {_words(minute)}"


def dst_note(when: datetime) -> str:
    """"" unless the clocks move within a day of `when`, which is exactly when
    somebody asking the time wants to be told."""
    try:
        here, later = when.utcoffset(), (when + timedelta(days=1)).utcoffset()
    except Exception:  # noqa: BLE001
        return ""
    if here is None or later is None or here == later:
        return ""
    return ("The clocks go forward within the day."
            if later > here else "The clocks go back within the day.")


def spoken_time(when: datetime | None = None, zone: str | None = None,
                with_zone: bool | None = None, cycle: str | None = None) -> str:
    """The whole answer, ready to be read aloud.

    `when` may be naive (read as being in `zone`) or aware (converted into it),
    so callers can pass a UTC timestamp without thinking about it.
    """
    tz = resolve_zone(zone)
    if when is None:
        when = datetime.now(tz)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    else:
        when = when.astimezone(tz)
    if with_zone is None:
        with_zone = zone is not None
    cycle = cycle if cycle in ("12", "24") else hour_cycle()
    parts = [f"It's {spoken_clock(when, cycle)}"]
    # "fifteen twenty in the afternoon" is nobody's speech: a 24-hour clock
    # already carries the half of the day.
    if cycle == "12" and (when.minute or when.hour not in (0, 12)):
        parts.append(_daypart(when.hour))
    if with_zone:
        parts.append(f"in {zone_label(tz, when)}")
    line = " ".join(parts).strip() + "."
    note = dst_note(when)
    return f"{line} {note}".strip()


# --------------------------------------------------------------------------- #
# What the voice brain calls
# --------------------------------------------------------------------------- #
def time_report(zone: str | None = None, remember: bool = False) -> str:
    """Spoken time, plus a word about anything the caller changed. Never raises:
    a clock that throws mid-conversation is worse than a clock that apologises.
    """
    try:
        if remember and zone:
            set_zone(zone)
            return f"{spoken_time(zone=zone, with_zone=True)} I'll use that from now on."
        return spoken_time(zone=zone)
    except UnknownZone as e:
        return (f"I don't know a timezone called {e.args[0]!r}. "
                "Give me a city like Tokyo or Denver.")
    except Exception as e:  # noqa: BLE001
        return f"My clock isn't answering — {e}"


if __name__ == "__main__":
    import sys
    print(time_report(sys.argv[1] if len(sys.argv) > 1 else None))
