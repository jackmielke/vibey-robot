#!/usr/bin/env python3
"""Vibey as a DJ: plays a track, changes its tempo live, and bobs to the beat.

    http://localhost:8776/status         what's playing, its BPM, the target BPM
    POST /load     {"track": "name"}     load by fuzzy name from the music folder
    POST /play                           play (or resume)
    POST /pause
    POST /stop
    POST /tempo    {"bpm": 128}          set the target tempo, live
    POST /nudge    {"percent": 4}        tempo up or down by a percentage
    POST /volume   {"level": 0.8}
    GET  /tracks                         what's in the music folder

Audio plays out of THIS machine, not the robot's speaker. The robot's speaker is
reached by uploading a whole clip and asking the daemon to play it, which is
fine for a sentence and useless for a DJ set: every tempo change would mean
stop, re-render, re-upload, restart from the top. The Mac is Vibey's brain
anyway, and it plugs into a real speaker. The robot's body does the dancing.

Tempo is a pitch fader, not a time-stretch. Speeding up raises the pitch, the
way a turntable does, and that is the sound people expect from "take it up".
It is also the only approach that changes instantly, per audio block, with no
artefacts and no re-rendering. Key-lock can come later if anyone misses it.

The head bob comes from the beat grid: librosa finds the beats once at load,
and while playing, the grid is walked at the current rate — so the nod stays on
the beat whatever the tempo, because it is the same clock the audio is on.

MUST run inside the SDK venv (librosa lives there):

    reachy_env/bin/python3 reachy_dj.py
"""
from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reachy_emotes import _goto, _pose, NEUTRAL  # noqa: E402

PORT = int(os.environ.get("DJ_PORT", "8778"))
MUSIC_DIR = Path(os.environ.get("VIBEY_MUSIC", str(Path.home() / "Music" / "vibey")))
SR = 44100
BLOCK = 1024
EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".aiff"}

STATE = {
    "track": None, "path": None, "bpm": None, "target_bpm": None,
    "playing": False, "position": 0.0, "duration": 0.0, "volume": 0.8,
    "rate": 1.0, "error": None,
}
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Loading and analysis
# --------------------------------------------------------------------------- #
class Track:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem
        # ffmpeg decodes anything; soundfile alone chokes on m4a and some mp3s.
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1",
             "-ar", str(SR), "-"], capture_output=True, check=True).stdout
        self.audio = np.frombuffer(raw, dtype=np.float32).copy()
        self.duration = len(self.audio) / SR
        import librosa
        # Beats once, at load. Walking the grid at the live rate afterwards is
        # what keeps the nod on the beat through tempo changes.
        tempo, frames = librosa.beat.beat_track(y=self.audio, sr=SR, units="frames")
        self.bpm = float(np.atleast_1d(tempo)[0])
        self.beats = librosa.frames_to_time(frames, sr=SR)
        # Downbeats are not detected — every fourth beat is called one, which is
        # right for nearly everything anyone would DJ with.
        self.downbeat_every = 4


def list_tracks() -> list[str]:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.stem for p in MUSIC_DIR.iterdir() if p.suffix.lower() in EXTS)


def find_track(name: str) -> Path | None:
    tracks = list_tracks()
    if not tracks:
        return None
    hit = difflib.get_close_matches(name.lower(), [t.lower() for t in tracks], n=1, cutoff=0.3)
    if not hit:
        # Substring is a good enough second try for "play the daft punk one".
        hit = [t.lower() for t in tracks if name.lower() in t.lower()][:1]
    if not hit:
        return None
    stem = next(t for t in tracks if t.lower() == hit[0])
    return next(p for p in MUSIC_DIR.iterdir() if p.stem == stem and p.suffix.lower() in EXTS)


# --------------------------------------------------------------------------- #
# Playback — variable rate, changes take effect on the next block
# --------------------------------------------------------------------------- #
class Player:
    def __init__(self):
        self.track: Track | None = None
        self.pos = 0.0          # sample position, fractional
        self.rate = 1.0
        self.stream = None
        self._beat_thread = None
        self._beat_stop = threading.Event()

    def load(self, track: Track):
        self.stop()
        self.track = track
        self.pos = 0.0
        self.rate = 1.0
        with _lock:
            STATE.update(track=track.name, path=str(track.path), bpm=round(track.bpm, 1),
                         target_bpm=round(track.bpm, 1), duration=round(track.duration, 1),
                         position=0.0, rate=1.0, playing=False, error=None)

    def _callback(self, out, frames, _time, status):
        t = self.track
        if t is None or not STATE["playing"]:
            out[:] = 0
            return
        # Read `frames` output samples from the source at the current rate by
        # linear interpolation. Cheap, artefact-free at DJ-range rates, and the
        # rate can change between blocks with nothing to rebuild.
        idx = self.pos + np.arange(frames) * self.rate
        i0 = np.floor(idx).astype(np.int64)
        frac = (idx - i0).astype(np.float32)
        end = len(t.audio) - 2
        if i0[0] >= end:
            out[:] = 0
            with _lock:
                STATE["playing"] = False
                STATE["position"] = round(t.duration, 1)
            return
        i0 = np.clip(i0, 0, end)
        a = t.audio[i0]
        b = t.audio[i0 + 1]
        block = (a + (b - a) * frac) * STATE["volume"]
        out[:, 0] = block
        self.pos += frames * self.rate
        STATE["position"] = round(self.pos / SR, 1)

    def play(self):
        if self.track is None:
            return
        import sounddevice as sd
        if self.stream is None:
            self.stream = sd.OutputStream(samplerate=SR, channels=1, blocksize=BLOCK,
                                          dtype="float32", callback=self._callback)
            self.stream.start()
        with _lock:
            STATE["playing"] = True
        self._start_bob()

    def pause(self):
        with _lock:
            STATE["playing"] = False
        self._stop_bob()

    def stop(self):
        self.pause()
        if self.stream is not None:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:  # noqa: BLE001
                pass
            self.stream = None
        self.pos = 0.0
        with _lock:
            STATE["position"] = 0.0

    def set_bpm(self, bpm: float):
        if self.track is None:
            return
        # Vinyl range. Past ±20% it stops sounding like a DJ and starts sounding
        # like a mistake, and the model will happily ask for 300 if allowed.
        rate = max(0.8, min(1.25, bpm / self.track.bpm))
        self.rate = rate
        with _lock:
            STATE["rate"] = round(rate, 3)
            STATE["target_bpm"] = round(self.track.bpm * rate, 1)

    # --- the body ---------------------------------------------------------- #
    def _start_bob(self):
        self._stop_bob()
        self._beat_stop.clear()
        self._beat_thread = threading.Thread(target=self._bob, daemon=True)
        self._beat_thread.start()

    def _stop_bob(self):
        self._beat_stop.set()
        if self._beat_thread and self._beat_thread.is_alive():
            self._beat_thread.join(timeout=1.0)
        try:
            _goto(NEUTRAL, [0.0, 0.0], 0.6)
        except Exception:  # noqa: BLE001
            pass

    def _bob(self):
        """Nod on the beat, bigger on the downbeat, reading the same clock as the
        audio — the sample position — so it cannot drift from what is heard."""
        t = self.track
        if t is None or len(t.beats) == 0:
            return
        beats = t.beats
        k = int(np.searchsorted(beats, self.pos / SR))
        side = 1
        while not self._beat_stop.is_set() and STATE["playing"]:
            now_s = self.pos / SR
            k = int(np.searchsorted(beats, now_s))
            if k >= len(beats):
                return
            wait = (beats[k] - now_s) / max(self.rate, 0.01)
            if wait > 0:
                if self._beat_stop.wait(min(wait, 0.5)):
                    return
                if wait > 0.5:
                    continue
            spb = (60.0 / t.bpm) / self.rate
            down = (k % t.downbeat_every == 0)
            side = -side
            # A nod is down-then-up inside one beat. The downbeat gets the roll
            # and the antennas; ordinary beats are a smaller dip so the groove
            # has a shape rather than a metronome.
            if down:
                _goto(_pose(pitch=0.16, roll=0.18 * side), [0.9 * side, -0.4 * side], spb * 0.45)
            else:
                _goto(_pose(pitch=0.09, roll=0.06 * side), [0.3 * side, -0.1 * side], spb * 0.45)
            # Ride out the beat before the next one, checking for stop.
            if self._beat_stop.wait(spb * 0.5):
                return


PLAYER = Player()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # The dashboard on :8770 drives this directly from the browser.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        if self.path.startswith("/status"):
            with _lock:
                self._json(dict(STATE))
        elif self.path.startswith("/tracks"):
            self._json({"tracks": list_tracks(), "folder": str(MUSIC_DIR)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            body = self._body()
            if self.path.startswith("/load"):
                path = find_track(str(body.get("track", "")))
                if path is None:
                    return self._json({"error": "no such track",
                                       "tracks": list_tracks()}, 404)
                PLAYER.load(Track(path))
                with _lock:
                    return self._json(dict(STATE))
            if self.path.startswith("/play"):
                if "track" in body:
                    path = find_track(str(body["track"]))
                    if path is None:
                        return self._json({"error": "no such track",
                                           "tracks": list_tracks()}, 404)
                    PLAYER.load(Track(path))
                PLAYER.play()
            elif self.path.startswith("/pause"):
                PLAYER.pause()
            elif self.path.startswith("/stop"):
                PLAYER.stop()
            elif self.path.startswith("/tempo"):
                PLAYER.set_bpm(float(body["bpm"]))
            elif self.path.startswith("/nudge"):
                if PLAYER.track:
                    cur = STATE["target_bpm"] or PLAYER.track.bpm
                    PLAYER.set_bpm(cur * (1 + float(body.get("percent", 0)) / 100))
            elif self.path.startswith("/volume"):
                with _lock:
                    STATE["volume"] = max(0.0, min(1.0, float(body["level"])))
            else:
                return self._json({"error": "not found"}, 404)
            with _lock:
                self._json(dict(STATE))
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)


def main():
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[dj] music folder: {MUSIC_DIR} ({len(list_tracks())} tracks)", flush=True)
    print(f"[dj] listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
