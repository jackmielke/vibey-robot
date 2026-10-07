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


# The camera switch: harder than privacy. Privacy keeps frames on the Mac;
# camera off means the camera service holds no session at all, so the robot
# stops capturing and encoding video. Default ON: a missing file means on.
CAMERA_PATH = Path(__file__).resolve().parent / ".camera_off"
CAMERA_OFF = "camera's switched off 📷🚫"


def camera_on() -> bool:
    return not CAMERA_PATH.exists()


def set_camera(on: bool) -> bool:
    if on:
        try:
            CAMERA_PATH.unlink()
        except FileNotFoundError:
            pass
    else:
        CAMERA_PATH.write_text("off\n")
    return on
