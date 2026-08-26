#!/usr/bin/env python3
"""
reachy_camera.py — stream Vibey's camera to your laptop as MJPEG.

The Reachy daemon only shares camera frames over WebRTC (not plain HTTP), so a
browser can't display them directly. This bridges that gap: it connects to the
robot with the Reachy SDK, pulls JPEG frames over WebRTC, and re-serves them as
a dead-simple MJPEG stream any <img> tag can show.

    http://localhost:8771/stream    → live MJPEG (multipart)
    http://localhost:8771/frame.jpg → single snapshot

MUST run inside the SDK venv (it needs reachy_mini + GStreamer):

    source reachy_env/bin/activate
    python3 reachy_camera.py

Env overrides:
    REACHY_HOST   default 192.168.1.120
    CAM_PORT      default 8771
"""

from __future__ import annotations

import os
import re
import threading
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from reachy_mini import ReachyMini
from reachy_voice import load_env  # .env loader — keeps robot IP in one place

load_env()

# This bridge only wants VIDEO. The SDK's WebRTC client also builds an audio
# send chain whose silent `audiotestsrc` streams continuous audio to the
# robot — and the daemon gives incoming client audio barge-in priority over
# the speaker, which was cutting off Vibey's speech moments after it started.
# Neuter the chain before any client is constructed.
try:
    from reachy_mini.media.webrtc_client_gstreamer import GstWebRTCClient

    def _no_audio_send(self):  # noqa: ANN001
        self.logger.info("audio send chain disabled (video-only bridge)")

    GstWebRTCClient._setup_audio_send_chain = _no_audio_send
except Exception as _e:  # noqa: BLE001 - SDK layout changed; better loud than broken
    print(f"[camera] WARNING: could not disable audio send chain: {_e}", flush=True)


def _default_host() -> str:
    """REACHY_HOST if set, else the host part of REACHY_URL (the variable the
    rest of the stack uses), so the camera doesn't need its own IP config."""
    host = os.environ.get("REACHY_HOST")
    if host:
        return host
    url = os.environ.get("REACHY_URL", "")
    m = re.match(r"https?://([^:/]+)", url)
    return m.group(1) if m else "192.168.12.240"


REACHY_HOST = _default_host()
CAM_PORT = int(os.environ.get("CAM_PORT", "8771"))

# Shared latest frame — one producer thread fills it, any number of HTTP
# clients read it. A Condition lets streamers block until the next frame
# instead of busy-looping.
_frame_lock = threading.Condition()
_latest_jpeg: bytes | None = None
_frame_seq = 0
_connected = False
# When the newest frame arrived. Without this the server cannot tell a photo
# from a memory: /frame.jpg served whatever it last managed to capture, with no
# indication of when, so a request during a six-hour outage returned a
# six-hour-old picture of the room and looked like it had worked.
_frame_at = 0.0
# How old a frame may be and still be called a photo.
STALE_AFTER = 10.0
# And how long without a frame before this process should be considered broken
# rather than slow. Longer, because a brief WebRTC renegotiation is normal.
DEAD_AFTER = 45.0


def _capture_loop():
    """Connect (with retry) and continuously publish the newest JPEG frame."""
    global _latest_jpeg, _frame_seq, _connected, _frame_at
    while True:
        try:
            print(f"[camera] connecting to {REACHY_HOST} …", flush=True)
            mini = ReachyMini(host=REACHY_HOST, connection_mode="network")
            _connected = True
            print("[camera] connected — streaming", flush=True)
            misses = 0
            while True:
                jpg = mini.media.get_frame_jpeg()
                if not jpg:
                    misses += 1
                    if misses > 50:
                        raise RuntimeError("frame stream stalled")
                    time.sleep(0.02)
                    continue
                misses = 0
                with _frame_lock:
                    _latest_jpeg = jpg
                    _frame_at = time.time()
                    _frame_seq += 1
                    _frame_lock.notify_all()
        except Exception as e:  # noqa: BLE001 - keep retrying forever
            _connected = False
            print(f"[camera] connection lost ({e}); retrying in 3s", flush=True)
            time.sleep(3)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path.startswith("/stream"):
            self._stream()
        elif self.path.startswith("/frame"):
            self._snapshot()
        elif self.path.startswith("/status"):
            age = (time.time() - _frame_at) if _frame_at else None
            dead = age is None or age > DEAD_AFTER
            body = json.dumps({
                "connected": _connected,
                "age": round(age, 1) if age is not None else None,
                "streaming": not dead,
            }).encode()
            # 503, not 200-with-a-sad-field.
            #
            # The watchdog's health check is "did this return JSON", so a
            # process wedged inside a WebRTC connect — no frames for six hours,
            # one core pinned — answered 200 and was left alone all day. A
            # service that cannot do the one thing it exists for has to say so
            # in the status line, where the supervisor is actually looking.
            self.send_response(503 if dead else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _snapshot(self):
        with _frame_lock:
            jpg, at = _latest_jpeg, _frame_at
        age = (time.time() - at) if at else None
        # A stale frame is not a photo. Refuse it and say how old it was, so the
        # caller can tell the difference between "the room is dark" and "the
        # camera stopped talking to us at three o'clock".
        if not jpg or age is None or age > STALE_AFTER:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "no fresh frame",
                "age": round(age, 1) if age is not None else None,
            }).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(jpg)))
        self.end_headers()
        self.wfile.write(jpg)

    def _stream(self):
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        last = -1
        try:
            while True:
                with _frame_lock:
                    # wait for a frame newer than the one we last sent
                    while _frame_seq == last or _latest_jpeg is None:
                        _frame_lock.wait(timeout=5)
                    jpg = _latest_jpeg
                    last = _frame_seq
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client closed the tab — normal


def main():
    threading.Thread(target=_capture_loop, daemon=True).start()
    print(f"[camera] MJPEG  http://localhost:{CAM_PORT}/stream", flush=True)
    ThreadingHTTPServer(("0.0.0.0", CAM_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
