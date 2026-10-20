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
    CAMERA_MAX_FPS  0 = as fast as frames come (the Mac default). The robot sets
                  a low cap: in LOCAL mode every frame is a software JPEG encode
                  on the CM4, competing with the motor control loop.
    VIBEY_ON_ROBOT=1  running ON the robot: read the daemon's local camera
                  (SDK LOCAL backend), no WebRTC. See ROBOT_NATIVE.md.
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


# JPEG straight out of the GStreamer pipeline.
#
# The SDK's video path is decoder -> videoconvert -> videorate -> appsink(BGR),
# and get_frame_jpeg() then copies each 2.7MB BGR frame into a SECOND pipeline
# that it flips PLAYING -> PAUSED around every single frame ("for occasional
# stills only", says its own docstring). Measured 2026-10-02: the robot sends
# 30fps, this bridge got ~6 distinct frames a second out of that, with stalls
# up to 3s. That was the dashboard's stutter.
#
# Instead, encode inside the receiving pipeline: every queue is leaky and one
# buffer deep, every appsink drops and never syncs to the clock, so a slow
# reader always gets the newest frame and nothing ever backs up. Two outputs:
# full size for the dashboard, face memory and captures, and a 640px one for
# the phone, which can't pull 30 full frames a second over Wi-Fi.
JPEG_QUALITY = int(os.environ.get("CAMERA_JPEG_QUALITY", "80"))
SMALL_W, SMALL_H = 640, 360
_sinks: dict = {}   # "full" / "small" -> appsink, filled in when video arrives
_PATCHED = False

try:
    from reachy_mini.media.webrtc_client_gstreamer import GstWebRTCClient as _Client
    from gi.repository import Gst as _Gst

    _orig_pad_added = _Client._webrtcsrc_pad_added_cb

    def _mk(kind, **props):
        e = _Gst.ElementFactory.make(kind)
        for k, v in props.items():
            e.set_property(k.replace("_", "-"), v)
        return e

    def _fresh_queue():
        # leaky=downstream: drop the OLD buffer when a new one arrives.
        return _mk("queue", leaky=2, max_size_buffers=1, max_size_bytes=0,
                   max_size_time=0)

    def _jpeg_sink():
        return _mk("appsink", drop=True, max_buffers=1, sync=False,
                   emit_signals=False)

    def _pad_added(self, webrtcsrc, pad):  # noqa: ANN001
        if not pad.get_name().startswith("video"):
            return _orig_pad_added(self, webrtcsrc, pad)
        self._configure_webrtcbin(webrtcsrc)
        p = self._pipeline_record
        q0, conv, tee = _fresh_queue(), _mk("videoconvert"), _mk("tee")
        q1, enc1, sink1 = _fresh_queue(), _mk("jpegenc", quality=JPEG_QUALITY), _jpeg_sink()
        q2, scale = _fresh_queue(), _mk("videoscale")
        caps = _mk("capsfilter", caps=_Gst.Caps.from_string(
            f"video/x-raw,width={SMALL_W},height={SMALL_H}"))
        enc2, sink2 = _mk("jpegenc", quality=70), _jpeg_sink()
        els = [q0, conv, tee, q1, enc1, sink1, q2, scale, caps, enc2, sink2]
        for e in els:
            p.add(e)
        pad.link(q0.get_static_pad("sink"))
        q0.link(conv); conv.link(tee)
        tee.link(q1); q1.link(enc1); enc1.link(sink1)
        tee.link(q2); q2.link(scale); scale.link(caps); caps.link(enc2); enc2.link(sink2)
        for e in els:
            e.sync_state_with_parent()
        _sinks["full"], _sinks["small"] = sink1, sink2
        print("[camera] in-pipeline JPEG branch up (full + small)", flush=True)

    _Client._webrtcsrc_pad_added_cb = _pad_added
    _PATCHED = True
except Exception as _e:  # noqa: BLE001
    print(f"[camera] WARNING: in-pipeline JPEG unavailable, using SDK path: {_e}",
          flush=True)


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
ON_ROBOT = os.environ.get("VIBEY_ON_ROBOT", "").strip() == "1"
CONNECTION_MODE = "localhost_only" if ON_ROBOT else "network"
_MAX_FPS = float(os.environ.get("CAMERA_MAX_FPS", "0") or 0)
_MIN_GAP = 1.0 / _MAX_FPS if _MAX_FPS > 0 else 0.0

# Shared latest frame — one producer thread fills it, any number of HTTP
# clients read it. A Condition lets streamers block until the next frame
# instead of busy-looping.
_frame_lock = threading.Condition()
_latest_jpeg: bytes | None = None
_frame_seq = 0
# The 640px copy for the phone, same lock, its own sequence.
_small_jpeg: bytes | None = None
_small_seq = 0
_connected = False
# Frames actually received per second, for /status (rolling 2s window).
_arrivals: list = []
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


# How long the video can go quiet before we give up and reconnect. Generous on
# purpose: see the note in the capture loop.
STALL_AFTER_S = float(os.environ.get("CAMERA_STALL_S", "10"))


_mini: list = [None]


def _pull(which: str, timeout_ms: int = 100) -> bytes | None:
    """Newest JPEG from the in-pipeline branch. Falls back to the SDK's slow
    still path only if the branch never came up (SDK layout changed)."""
    sink = _sinks.get(which)
    if sink is None:
        if which == "full" and _mini[0] is not None and not _PATCHED:
            return _mini[0].media.get_frame_jpeg()
        time.sleep(timeout_ms / 1000)
        return None
    sample = sink.try_pull_sample(timeout_ms * 1_000_000)
    if sample is None:
        return None
    buf = sample.get_buffer()
    return buf.extract_dup(0, buf.get_size()) if buf is not None else None


def _small_loop():
    """The phone's 640px copy comes off its own appsink, on its own thread, so
    neither size waits on the other."""
    global _small_jpeg, _small_seq
    while True:
        jpg = _pull("small", 200)
        if jpg:
            with _frame_lock:
                _small_jpeg = jpg
                _small_seq += 1
                _frame_lock.notify_all()


def _cam_off() -> bool:
    try:
        import reachy_privacy
        return not reachy_privacy.camera_on()
    except Exception:  # noqa: BLE001
        return False


def _hang_up(mini) -> None:
    """Close the WebRTC session so the robot stops sending video."""
    global _connected, _latest_jpeg
    _connected = False
    _mini[0] = None
    _sinks.clear()
    with _frame_lock:
        _latest_jpeg = None
    try:
        mini.__exit__(None, None, None)
    except Exception as e:  # noqa: BLE001
        print(f"[camera] hang-up: {e}", flush=True)
    print("[camera] camera switched off — session closed", flush=True)


def _capture_loop():
    """Connect (with retry) and continuously publish the newest JPEG frame."""
    global _latest_jpeg, _frame_seq, _connected, _frame_at, _small_jpeg, _small_seq
    while True:
        # Camera off: don't hold a session at all. Privacy blocking frames
        # while still streaming keeps the robot recording and encoding video
        # the whole time — eyes "closed" in software only, and over a core of
        # the robot's CPU spent on it.
        if _cam_off():
            _connected = False
            print("[camera] switched off — not connected", flush=True)
            while _cam_off():
                time.sleep(1)
        try:
            print(f"[camera] connecting to {REACHY_HOST} …", flush=True)
            _sinks.clear()
            mini = ReachyMini(host=REACHY_HOST, connection_mode=CONNECTION_MODE)
            _mini[0] = mini
            _connected = True
            print("[camera] connected — streaming", flush=True)
            last_frame = time.time()
            checked = time.time()
            while True:
                if time.time() - checked > 1.0:
                    checked = time.time()
                    if _cam_off():
                        _hang_up(mini)
                        break
                jpg = _pull("full")
                if not jpg:
                    # Wait STALL_AFTER_S of real silence before tearing the
                    # session down, and measure it in seconds rather than in
                    # missed polls.
                    #
                    # This used to be `misses > 50` with a 20ms sleep — one
                    # second. Renegotiating WebRTC costs ten to twenty, so a
                    # one-second hiccup on the wifi bought twenty seconds of
                    # black screen, and the camera read as broken when the
                    # stream underneath it was fine. The robot's MIC bridge
                    # holds the same kind of session over the same wifi and had
                    # reconnected once in the time this reconnected nine times;
                    # the difference was entirely this number.
                    #
                    # The bound to stay under is the cost of being wrong: give
                    # it well under the reconnect it is trying to avoid.
                    if time.time() - last_frame > STALL_AFTER_S:
                        raise RuntimeError(
                            f"frame stream stalled ({STALL_AFTER_S:g}s)")
                    time.sleep(0.02)
                    continue
                last_frame = time.time()
                with _frame_lock:
                    _latest_jpeg = jpg
                    _frame_at = last_frame
                    _frame_seq += 1
                    _arrivals.append(last_frame)
                    while _arrivals and _arrivals[0] < last_frame - 2:
                        _arrivals.pop(0)
                    _frame_lock.notify_all()
                if _MIN_GAP:
                    time.sleep(_MIN_GAP)
        except Exception as e:  # noqa: BLE001 - keep retrying forever
            _connected = False
            print(f"[camera] connection lost ({e}); retrying in 3s", flush=True)
            time.sleep(3)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _eyes_closed(self) -> bool:
        """Privacy mode keeps every frame on this Mac. Off-Mac clients (the
        phone app, anything on the LAN) get a 403 instead of a picture; local
        consumers like the face service are untouched, the same split the
        token uses."""
        import reachy_privacy
        if self._local():
            return False
        return reachy_privacy.is_on()

    def _local(self) -> bool:
        import vibey_auth
        host = (self.client_address or ("",))[0]
        return host in vibey_auth._LOCAL or host.startswith("127.")

    def _refuse(self):
        import reachy_privacy
        body = json.dumps({"error": "eyes closed", "privacy": True,
                           "message": reachy_privacy.CLOSED}).encode()
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith(("/stream", "/frame")) and self._eyes_closed():
            self._refuse()
        elif self.path.startswith("/stream"):
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
                "fps": round(len(_arrivals) / 2, 1),
                # The dashboard on this Mac skips the bridge entirely and
                # opens its own WebRTC session to the robot (see control.html);
                # this tells it where. Off-Mac pages never get it, so privacy
                # mode can't be walked around through here.
                "robot": None if self._eyes_closed() or not self._local()
                         else {"host": REACHY_HOST, "signalling": 8443},
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
            if "size=small" in self.path and _small_jpeg:
                jpg = _small_jpeg
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
        """multipart MJPEG, always the newest frame: a client that falls
        behind skips frames instead of watching the past.
        ?size=small -> 640x360 (the phone); ?fps=N caps this client's rate."""
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        small = q.get("size", [""])[0] == "small"
        try:
            gap = 1.0 / float(q.get("fps", ["0"])[0])
        except (ValueError, ZeroDivisionError):
            gap = 0.0
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        # The lag that grows to seconds lives HERE, not in the camera: macOS
        # gives a socket a send buffer of several MB, and when a viewer (phone
        # on Wi-Fi, a busy webview) reads slower than frames arrive, a dozen
        # frames queue up in the kernel and the picture plays back the past.
        # Room for about two frames instead: a slow reader then blocks the
        # write, and the loop below hands it the newest frame, skipping the
        # rest. Plus no Nagle, so a frame leaves the moment it is written.
        try:
            import socket
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                       96 * 1024 if small else 256 * 1024)
        except OSError:
            pass
        last, sent = -1, 0.0
        try:
            while True:
                with _frame_lock:
                    # wait for a frame newer than the one we last sent
                    while True:
                        seq, jpg = ((_small_seq, _small_jpeg) if small
                                    else (_frame_seq, _latest_jpeg))
                        if jpg is not None and seq != last:
                            break
                        _frame_lock.wait(timeout=5)
                    last, at = seq, _frame_at
                if self._eyes_closed():
                    break   # privacy switched on mid-stream: stop sending
                sent = time.time()
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpg)}\r\nX-Timestamp: {at:.3f}\r\n\r\n".encode()
                    + jpg + b"\r\n")
                self.wfile.flush()
                if gap:   # then skip ahead to whatever is newest
                    time.sleep(max(0.0, sent + gap - time.time()))
        except (BrokenPipeError, ConnectionResetError):
            pass  # client closed the tab — normal


def main():
    threading.Thread(target=_capture_loop, daemon=True).start()
    threading.Thread(target=_small_loop, daemon=True).start()
    print(f"[camera] MJPEG  http://localhost:{CAM_PORT}/stream", flush=True)
    import vibey_auth
    vibey_auth.protect(Handler)   # LAN needs the app token; localhost is free
    ThreadingHTTPServer(("0.0.0.0", CAM_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
