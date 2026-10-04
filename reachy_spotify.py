"""Vibey drives the Spotify app on this Mac — the same speaker DJ mode uses.

Playback (play, pause, skip, volume, what's on) is AppleScript against the
desktop app: no keys, no login beyond the app's own. Playing something BY NAME
("put on some Daft Punk") needs a search, which is Spotify's Web API with an
app key — client credentials only, no user login:

    SPOTIFY_CLIENT_ID=…  SPOTIFY_CLIENT_SECRET=…   in .env
    (developer.spotify.com/dashboard → Create app; any redirect URI)

Without them a named request opens Spotify's search for it instead, and says so.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

from reachy_voice import load_env

load_env()

_TOKEN = {"value": None, "until": 0.0}


def _osa(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


def _app(cmd: str) -> str:
    return _osa(f'tell application "Spotify" to {cmd}')


def has_search() -> bool:
    return bool(os.environ.get("SPOTIFY_CLIENT_ID") and os.environ.get("SPOTIFY_CLIENT_SECRET"))


def _token() -> str:
    if _TOKEN["value"] and time.time() < _TOKEN["until"] - 60:
        return _TOKEN["value"]
    cid, sec = os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=b"grant_type=client_credentials", method="POST",
        headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    _TOKEN.update(value=d["access_token"], until=time.time() + d.get("expires_in", 3600))
    return _TOKEN["value"]


def search(query: str, kind: str = "track") -> dict | None:
    """Best match as {uri, name, by}. kind: track | playlist | album | artist."""
    q = urllib.parse.urlencode({"q": query, "type": kind, "limit": 1})
    req = urllib.request.Request(f"https://api.spotify.com/v1/search?{q}",
                                 headers={"Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        items = (json.loads(r.read()).get(kind + "s") or {}).get("items") or []
    items = [i for i in items if i]
    if not items:
        return None
    it = items[0]
    by = ", ".join(a["name"] for a in it.get("artists") or []) or \
        (it.get("owner") or {}).get("display_name", "")
    return {"uri": it["uri"], "name": it["name"], "by": by}


def now_playing() -> str:
    try:
        if _app("player state as string") != "playing":
            return "Spotify's not playing anything."
        name = _app("name of current track")
        artist = _app("artist of current track")
        return f"Playing {name} by {artist} on Spotify."
    except Exception as e:  # noqa: BLE001
        return f"Spotify isn't answering ({e})."


def play(query: str = "", kind: str = "track") -> str:
    """Resume, or find `query` and play it. Never raises — the caller is a voice."""
    try:
        if not query.strip():
            _app("play")
            time.sleep(0.4)
            return now_playing()
        if not has_search():
            subprocess.run(["open", "spotify:search:" + urllib.parse.quote(query)], timeout=10)
            return ("I can control Spotify but can't search it yet — I've opened the "
                    "search for it. Jack needs to add a Spotify app key to my .env.")
        hit = search(query, kind)
        # App keys can't see most user playlists; a mood still finds a track.
        for other in ("playlist", "track"):
            if not hit and kind != other:
                hit = search(query, other)
        if not hit:
            return f"Spotify has nothing for \"{query}\"."
        _app(f'play track "{hit["uri"]}"')
        return f"Playing {hit['name']}" + (f" by {hit['by']}" if hit["by"] else "") + " on Spotify."
    except Exception as e:  # noqa: BLE001
        return f"Spotify didn't take that ({e})."


def control(action: str, level: int | None = None, query: str = "", kind: str = "track") -> str:
    action = (action or "").lower()
    try:
        if action == "play":
            return play(query, kind)
        if action == "pause":
            _app("pause")
            return "Paused Spotify."
        if action in ("next", "skip"):
            _app("next track")
            time.sleep(0.5)
            return now_playing()
        if action in ("previous", "back"):
            _app("previous track")
            time.sleep(0.5)
            return now_playing()
        if action == "volume" and level is not None:
            _app(f"set sound volume to {max(0, min(100, int(level)))}")
            return f"Spotify volume {int(level)}."
        return now_playing()
    except Exception as e:  # noqa: BLE001
        return f"Spotify didn't take that ({e})."
