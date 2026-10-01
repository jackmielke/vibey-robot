"""Privacy mode: Vibey keeps talking and texting, but its eyes stay shut.

While it's on, nothing leaves the camera: no /photo, /clip or /timelapse on
Telegram (for anyone, Jack included), and the voice brain can't look at the
room. One small file so every process (chat, telegram, viewer) agrees without
having to call each other. ON by default: a missing file means private.
"""
from __future__ import annotations

from pathlib import Path

PATH = Path(__file__).resolve().parent / ".privacy_mode"
CLOSED = "eyes closed rn 🙈 (privacy mode)"


def is_on() -> bool:
    try:
        return PATH.read_text().strip() != "off"
    except OSError:
        return True


def set_on(on: bool) -> bool:
    PATH.write_text("on\n" if on else "off\n")
    return on
