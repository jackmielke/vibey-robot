#!/usr/bin/env python3
"""
reachy_voice.py — give Vibey a voice (ElevenLabs → the robot's own speaker).

Text goes to ElevenLabs TTS, the resulting MP3 is uploaded to the Reachy daemon,
and played on the robot's speaker — so the sound comes out of Vibey's body, not
your laptop. Stdlib only; no SDK/venv required.

CLI:
    python3 reachy_voice.py "Hey, I'm Vibey. Nice to meet you."

As a module:
    from reachy_voice import say
    say("Let's jam.")

Config comes from .env (see keys below) or the environment:
    ELEVENLABS_API_KEY   required
    ELEVEN_VOICE_ID      required — which ElevenLabs voice to speak in
    REACHY_URL           default http://192.168.1.120:8000
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import time
import urllib.request
import uuid
from pathlib import Path


def load_env(path: str = ".env") -> None:
    """Minimal .env loader — populates os.environ for any KEY=VALUE lines
    that aren't already set. Avoids a python-dotenv dependency."""
    p = Path(__file__).parent / path
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env()


def _pin_mdns() -> None:
    """Resolve a .local REACHY_URL to its IPv4 address once, for this process.

    macOS resolves `reachy-mini.local` per request and takes 2-4 s to do it,
    so every upload + play cost ~7 s before a reply was heard. start_wonder.sh
    exports the IP for this reason, but anything launched by hand from .env got
    the name. Every module imports this one early, so pinning here covers them.
    """
    import socket
    import urllib.parse
    url = os.environ.get("REACHY_URL", "")
    host = urllib.parse.urlparse(url).hostname or ""
    if not host.endswith(".local"):
        return
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return
    os.environ["REACHY_URL"] = url.replace(host, ip, 1)
    os.environ["REACHY_HOST"] = ip


_pin_mdns()

REACHY_URL = os.environ.get("REACHY_URL", "http://192.168.1.120:8000").rstrip("/")
ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_MODEL = os.environ.get("ELEVEN_MODEL", "eleven_multilingual_v2")

# Voice is switchable at runtime ("switch to the Australian accent").
# The dict is the mutable source of truth; VOICES maps accent names to
# ElevenLabs voice ids from Jack's library / the premade set.
VOICE = {"id": os.environ.get("ELEVEN_VOICE_ID", "")}
VOICES = {
    "british": os.environ.get("ELEVEN_VOICE_ID", "EQC5zQOuq9t6MrkD0MPT"),  # British R2
    "australian": "IKne3meq5aSn9XLyUdCD",   # Charlie — ElevenLabs premade, Aussie
    "aussie": "IKne3meq5aSn9XLyUdCD",
    "vibey": "5nKWJuFC6bX0w7HcS5KI",         # "this is vibey"
}


def set_voice(name_or_id: str) -> str | None:
    """Switch the speaking voice by accent name or raw voice id. Returns the
    id now in use, or None if the name is unknown."""
    key = name_or_id.strip().lower()
    vid = VOICES.get(key)
    if not vid and len(name_or_id) >= 15 and " " not in name_or_id:
        vid = name_or_id  # looks like a raw ElevenLabs voice id
    if not vid:
        return None
    VOICE["id"] = vid
    return vid


# Replies like to trail off into something nobody in the room can hear: a slash
# command to try next, a link to go read. Out loud those land as "slash photo"
# and "h t t p s colon slash slash", which is noise at the end of every sentence.
# Only the TAIL is trimmed, and only at a word boundary — a command named inside
# a sentence ("text me /photo and I'll look") is part of what was said, and
# "they/them" keeps its second half.
_TRAILING_NOISE = re.compile(
    r"(?:(?:^|[\s—–|])[\s(\[]*(?:/[a-zA-Z][\w-]*|(?:https?://|www\.)\S+)"
    r"[\s,.;:!?)\]]*)+$")


def spoken_text(text: str) -> str:
    """`text` with any trailing slash-commands or URLs stripped. Never returns
    empty: a line that is nothing but a command is left alone to be read."""
    cleaned = _TRAILING_NOISE.sub("", str(text or "")).strip(" \t\n—–-|·")
    return cleaned or str(text or "").strip()


def tts(text: str) -> bytes:
    """Return MP3 audio bytes for `text` from ElevenLabs."""
    text = spoken_text(text)
    if not ELEVEN_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY not set (see .env)")
    if not VOICE["id"]:
        raise RuntimeError("ELEVEN_VOICE_ID not set (see .env)")
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE['id']}"
           f"?output_format=mp3_44100_128")
    body = json.dumps({
        "text": text,
        "model_id": ELEVEN_MODEL,
        "voice_settings": {"stability": 0.4, "similarity_boost": 0.8},
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "xi-api-key": ELEVEN_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _multipart(field: str, filename: str, data: bytes, ctype: str):
    """Build a minimal multipart/form-data body (no external deps)."""
    boundary = f"----wonder{uuid.uuid4().hex}"
    pre = (f"--{boundary}\r\n"
           f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
           f"Content-Type: {ctype}\r\n\r\n").encode()
    post = f"\r\n--{boundary}--\r\n".encode()
    return pre + data + post, boundary


def upload_sound(mp3: bytes, name: str) -> str:
    """Upload MP3 to the daemon; returns the server-side filename to play."""
    payload, boundary = _multipart("file", name, mp3, "audio/mpeg")
    req = urllib.request.Request(
        f"{REACHY_URL}/api/media/sounds/upload", data=payload, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        json.loads(r.read() or b"null")  # {"status":"ok","path":...}
    return name


def play_sound(name: str) -> None:
    req = urllib.request.Request(
        f"{REACHY_URL}/api/media/play_sound",
        data=json.dumps({"file": name}).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def say(text: str, wait: bool = False) -> float:
    """Speak `text` on the robot. Returns the clip duration in seconds —
    accurate, from the MP3 byte length (128kbps CBR → 16000 bytes/sec), so
    callers can mute the mic for exactly as long as the robot is talking.
    If `wait`, also block for roughly that long."""
    mp3 = tts(text)
    duration = len(mp3) / 16000.0
    name = f"wonder_{uuid.uuid4().hex[:8]}.mp3"
    upload_sound(mp3, name)
    play_sound(name)
    if wait:
        time.sleep(max(1.0, duration))
    return duration


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) or "Hey, I'm Vibey. Nice to meet you."
    print(f"[voice] speaking: {msg!r}", flush=True)
    say(msg)
    print("[voice] done", flush=True)
