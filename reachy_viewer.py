#!/usr/bin/env python3
"""
reachy_viewer.py — "see what the robot sees" from your laptop.

A tiny zero-dependency web dashboard that shows what Vibey is perceiving in
real time: detected faces (position in frame), which direction it's hearing
sound from, its live head pose + antenna posture, and — folded in from the
handsfree daemon — whether voice commands are armed and the last one fired.

    python3 reachy_viewer.py      # then open http://localhost:8770

Why not raw camera video? The Reachy daemon's REST API does not expose camera
frames (they stream over WebRTC to on-robot apps only). What it *does* expose is
the derived perception: face target, sound direction-of-arrival, pose. That is
exactly "what the robot notices," which is what this view renders. A raw MJPEG
feed would need a small companion app running on the robot itself — a good
follow-up, but not required for this.

Env overrides:
    REACHY_URL      default http://192.168.1.120:8000
    HANDSFREE_URL   default http://localhost:8765
    PORT            default 8770
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from reachy_voice import say  # ElevenLabs → robot speaker (loads .env)

REACHY_URL = os.environ.get("REACHY_URL", "http://192.168.1.120:8000").rstrip("/")

# Finding the robot and moving it between networks, without the desktop app.
# Every _get/_post below reads the REACHY_URL global at call time, so
# repointing the dashboard at a new address is just a reassignment.
import reachy_connect
import reachy_modes
HANDSFREE_URL = os.environ.get("HANDSFREE_URL", "http://localhost:8765").rstrip("/")
# Live camera MJPEG feed served by reachy_camera.py (runs in the SDK venv).
CAM_URL = os.environ.get("CAM_URL", "http://localhost:8771").rstrip("/")
MIMIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mimic")
HANDSFREE_STREAM = os.environ.get("HANDSFREE_STREAM", "http://localhost:8765/stream")
# Voice-chat control API served by reachy_chat.py.
CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8772").rstrip("/")
# Face-memory control API served by reachy_memory.py.
MEM_URL = os.environ.get("MEM_URL", "http://localhost:8773").rstrip("/")
PORT = int(os.environ.get("PORT", "8770"))


def _get(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except Exception:
        return None


def _post(url: str, body: dict | None = None, timeout: float = 3.0):
    try:
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except Exception:
        return None


# Sleep switch — the dashboard power button. While asleep: chat is muted,
# memory is paused, the robot holds its sleep pose.
ASLEEP = {"on": False}


def _set_power(off: bool) -> None:
    ASLEEP["on"] = off
    _post(f"{CHAT_URL}/mute", {"muted": off})
    _post(f"{MEM_URL}/pause", {"paused": off})
    if off:
        _post(f"{REACHY_URL}/api/media/stop_sound")
        _post(f"{REACHY_URL}/api/move/play/goto_sleep", timeout=20.0)
    else:
        # goto_sleep leaves the motors disabled (that's what makes the sleep
        # pose limp) — they must be re-enabled or wake_up silently does
        # nothing and the robot stays face-down.
        _post(f"{REACHY_URL}/api/motors/set_mode/enabled", timeout=10.0)
        # Switched on = audible, but at the configured level rather than a
        # hardcoded 100. This path used to silently undo VIBEY_VOLUME, so the
        # volume you set stuck until the moment you used this button.
        _post(f"{REACHY_URL}/api/volume/set",
              {"volume": max(0, min(100, int(os.environ.get("VIBEY_VOLUME", "100"))))})
        _post(f"{REACHY_URL}/api/move/play/wake_up", timeout=20.0)
        # face-following + speech wobble are core to feeling alive — they can
        # get dropped by daemon restarts, so re-assert on every wake.
        _post(f"{REACHY_URL}/api/media/tracking/enable")
        _post(f"{REACHY_URL}/api/media/wobbling/enable")


def _reboot_robot() -> None:
    """Full daemon restart on the robot — the fix for a stuck backend
    (symptoms: motions/sounds ignored, camera WebRTC won't connect). Takes
    ~20s; motors are re-enabled and the robot woken once it's back."""
    ASLEEP["on"] = False
    _post(f"{REACHY_URL}/api/daemon/restart", timeout=30.0)
    deadline = time.time() + 90
    while time.time() < deadline:
        time.sleep(5)
        st = _get(f"{REACHY_URL}/api/daemon/status", timeout=4.0)
        if st and st.get("state") == "running":
            break
    _post(f"{REACHY_URL}/api/motors/set_mode/enabled", timeout=10.0)
    _post(f"{REACHY_URL}/api/move/play/wake_up", timeout=20.0)
    _post(f"{REACHY_URL}/api/media/tracking/enable")
    _post(f"{REACHY_URL}/api/media/wobbling/enable")
    _post(f"{CHAT_URL}/mute", {"muted": False})
    _post(f"{MEM_URL}/pause", {"paused": False})
    print("[viewer] robot reboot sequence finished", flush=True)


CAPTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
os.makedirs(CAPTURES_DIR, exist_ok=True)
CAPTURING = {"video": False}


def _capture_photo() -> str | None:
    try:
        with urllib.request.urlopen(f"{CAM_URL}/frame.jpg", timeout=8) as r:
            jpeg = r.read()
        name = time.strftime("photo_%Y%m%d_%H%M%S.jpg")
        with open(os.path.join(CAPTURES_DIR, name), "wb") as f:
            f.write(jpeg)
        return name
    except Exception as e:
        print(f"[capture] photo failed: {e}", flush=True)
        return None


def _capture_video(seconds: float = 10.0, fps: int = 8) -> str | None:
    """Pull frames from the camera bridge and assemble an mp4 with ffmpeg."""
    if CAPTURING["video"]:
        return None
    CAPTURING["video"] = True
    try:
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            n = int(seconds * fps)
            interval = 1.0 / fps
            for i in range(n):
                t0 = time.time()
                try:
                    with urllib.request.urlopen(f"{CAM_URL}/frame.jpg", timeout=5) as r:
                        with open(os.path.join(tmp, f"{i:05d}.jpg"), "wb") as f:
                            f.write(r.read())
                except Exception:
                    pass
                time.sleep(max(0, interval - (time.time() - t0)))
            name = time.strftime("clip_%Y%m%d_%H%M%S.mp4")
            out = os.path.join(CAPTURES_DIR, name)
            r = subprocess.run(
                ["ffmpeg", "-y", "-framerate", str(fps),
                 "-pattern_type", "glob", "-i", os.path.join(tmp, "*.jpg"),
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                 out], capture_output=True, timeout=120)
            if r.returncode != 0:
                print(f"[capture] ffmpeg: {r.stderr.decode()[-200:]}", flush=True)
                return None
            return name
    except Exception as e:
        print(f"[capture] video failed: {e}", flush=True)
        return None
    finally:
        CAPTURING["video"] = False


# Timelapse: one frame a minute into captures/timelapse_<date>/, assembled
# to mp4 on demand (POST /timelapse {"assemble": true}). Runs continuously —
# cheap (60 JPEGs/hour) and it means mornings come with a film of the night.
def _timelapse_loop():
    while True:
        try:
            day_dir = os.path.join(CAPTURES_DIR,
                                   time.strftime("timelapse_%Y%m%d"))
            os.makedirs(day_dir, exist_ok=True)
            with urllib.request.urlopen(f"{CAM_URL}/frame.jpg", timeout=8) as r:
                jpeg = r.read()
            with open(os.path.join(
                    day_dir, time.strftime("%H%M%S") + ".jpg"), "wb") as f:
                f.write(jpeg)
        except Exception:
            pass
        time.sleep(60)


def _timelapse_assemble(day: str | None = None) -> str | None:
    import subprocess
    day = day or time.strftime("%Y%m%d")
    day_dir = os.path.join(CAPTURES_DIR, f"timelapse_{day}")
    if not os.path.isdir(day_dir) or len(os.listdir(day_dir)) < 5:
        return None
    name = f"timelapse_{day}.mp4"
    out = os.path.join(CAPTURES_DIR, name)
    r = subprocess.run(
        ["ffmpeg", "-y", "-framerate", "12", "-pattern_type", "glob",
         "-i", os.path.join(day_dir, "*.jpg"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         out], capture_output=True, timeout=300)
    return name if r.returncode == 0 else None


def gather() -> dict:
    """One consolidated perception snapshot for the browser to render."""
    face = _get(f"{REACHY_URL}/api/media/tracking/face")
    state = _get(f"{REACHY_URL}/api/state/full?with_doa=true")
    current = _get(f"{MEM_URL}/current", timeout=1.0)
    online = face is not None or state is not None
    ft = (face or {}).get("face_target", {}) if face else {}
    doa = (state or {}).get("doa", {}) if state else {}
    # Everyone memory currently recognizes (0, 1, or several people at once).
    people = [p for p in (current or {}).get("people", []) if p.get("fresh")]
    return {
        "asleep": ASLEEP["on"],
        "people": people,
        "online": online,
        "face": {
            "detected": bool(ft.get("detected")),
            "x": ft.get("x"),          # normalized horizontal offset in frame
            "y": ft.get("y"),          # normalized vertical offset
            "roll": ft.get("roll"),
        },
        "pose": (state or {}).get("head_pose"),
        "antennas": (state or {}).get("antennas_position"),
        "body_yaw": (state or {}).get("body_yaw"),
        "doa": {
            "angle": doa.get("angle"),               # radians
            "speech": bool(doa.get("speech_detected")),
        },
    }


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Vibey — what the robot sees</title>
<style>
  /* ---------------------------------------------------------------------
     Theme tokens. Every colour in this sheet resolves through one of these,
     so a theme is a token swap and nothing else. Dark is the default (the
     robot mostly lives in a dim room); light is opt-in via the header
     toggle, and "system" — no data-theme attribute — follows the OS.
     --------------------------------------------------------------------- */
  :root{
    color-scheme:dark;
    --bg:#0d0f16;         --bg-wash:#131726;
    --panel:#161a25;      --panel-2:#1d2231;
    --well:#10131c;       --sunken:#070910;
    --line:#262c3d;       --line-soft:#1e2433;
    --txt:#e8ebf5;        --dim:#8b93ad;        --faint:#4c5470;
    --accent:#4cc4f5;     --accent-txt:#04141f;
    --accent-soft:#10334a; --accent-line:#1c4d6b;
    --good:#4ade80;       --good-soft:#123023;  --good-line:#1d5238;
    --warn:#fbbf24;       --warn-soft:#332612;  --warn-line:#5f4715;
    --bad:#f87171;        --bad-soft:#38141b;   --bad-line:#7a2531; --bad-txt:#fca5a5;
    --violet:#a78bfa;     --violet-soft:#221844; --violet-line:#4c2f9e;
    --openai:#10a37f;     --openai-soft:#0a2b23; --openai-line:#127a60;
    --shadow:0 10px 30px rgba(0,0,0,.45);
    --shadow-lg:0 24px 64px rgba(0,0,0,.6);
    --r-sm:8px; --r-md:11px; --r-lg:14px; --r-xl:18px;
  }
  /* System preference, only when the user hasn't explicitly chosen. */
  @media (prefers-color-scheme:light){
    :root:not([data-theme="dark"]){
      color-scheme:light;
      --bg:#f4f6fb;         --bg-wash:#e9eef8;
      --panel:#ffffff;      --panel-2:#f2f5fa;
      --well:#f7f9fc;       --sunken:#0d1017;
      --line:#dde3ee;       --line-soft:#e7ecf4;
      --txt:#141824;        --dim:#5d6579;        --faint:#a3abbd;
      --accent:#0a86c4;     --accent-txt:#ffffff;
      --accent-soft:#dbf0fb; --accent-line:#9ad6f2;
      --good:#15803d;       --good-soft:#dcfce7;  --good-line:#8ee0ab;
      --warn:#a16207;       --warn-soft:#fef3c7;  --warn-line:#ecca6a;
      --bad:#c0332f;        --bad-soft:#fee2e2;   --bad-line:#f0a7a7; --bad-txt:#a02725;
      --violet:#6d28d9;     --violet-soft:#ede7fd; --violet-line:#c0a9f5;
      --openai:#0b7f63;     --openai-soft:#d7f2ea; --openai-line:#7fcbb7;
      --shadow:0 8px 24px rgba(23,35,66,.10);
      --shadow-lg:0 24px 56px rgba(23,35,66,.18);
    }
  }
  /* Explicit choice always wins over the system preference. */
  :root[data-theme="light"]{
    color-scheme:light;
    --bg:#f4f6fb;         --bg-wash:#e9eef8;
    --panel:#ffffff;      --panel-2:#f2f5fa;
    --well:#f7f9fc;       --sunken:#0d1017;
    --line:#dde3ee;       --line-soft:#e7ecf4;
    --txt:#141824;        --dim:#5d6579;        --faint:#a3abbd;
    --accent:#0a86c4;     --accent-txt:#ffffff;
    --accent-soft:#dbf0fb; --accent-line:#9ad6f2;
    --good:#15803d;       --good-soft:#dcfce7;  --good-line:#8ee0ab;
    --warn:#a16207;       --warn-soft:#fef3c7;  --warn-line:#ecca6a;
    --bad:#c0332f;        --bad-soft:#fee2e2;   --bad-line:#f0a7a7; --bad-txt:#a02725;
    --violet:#6d28d9;     --violet-soft:#ede7fd; --violet-line:#c0a9f5;
    --openai:#0b7f63;     --openai-soft:#d7f2ea; --openai-line:#7fcbb7;
    --shadow:0 8px 24px rgba(23,35,66,.10);
    --shadow-lg:0 24px 56px rgba(23,35,66,.18);
  }

  *{box-sizing:border-box}
  body{margin:0;padding:22px 20px 40px;
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
       -webkit-font-smoothing:antialiased;
       background:var(--bg);color:var(--txt);min-height:100vh;
       background-image:radial-gradient(1100px 520px at 50% -12%,var(--bg-wash),transparent 70%);
       background-attachment:fixed;
       transition:background-color .25s ease,color .25s ease}
  h1{font-size:19px;margin:0 0 2px;letter-spacing:-.2px;font-weight:650}
  .sub{color:var(--dim);font-size:12px;margin-bottom:20px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;max-width:900px;margin:0 auto}
  @media (max-width:720px){
    body{padding:16px 14px 32px}
    .grid{grid-template-columns:1fr}
    .full{grid-column:1}
  }
  .hdr{max-width:900px;margin:0 auto}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--r-xl);
         padding:18px;box-shadow:var(--shadow)}
  .panel h2{font-size:10.5px;text-transform:uppercase;letter-spacing:1.3px;color:var(--dim);
            margin:0 0 13px;font-weight:650}
  .full{grid-column:1/3}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;flex:none;
       vertical-align:middle;background:var(--c,var(--dim));
       box-shadow:0 0 0 3px color-mix(in srgb,var(--c,var(--dim)) 20%,transparent)}
  /* Status line under the title. It used to be class="sub hdr", where .hdr's
     `margin:0 auto` silently cancelled .sub's bottom margin — hence the
     cramped gap to the first panel. Own class, own spacing, and the robot's
     address demoted to a quiet mono chip so it stops competing with the
     status text. */
  /* Off switch bar — deliberately the loudest thing on the page when off. */
  #offbar{max-width:900px;margin:14px auto 6px;display:flex;align-items:center;
    gap:16px;padding:14px 18px;border-radius:12px;
    border:1px solid var(--good);background:color-mix(in srgb,var(--good) 10%,transparent)}
  #offbar.is-off{border-color:var(--bad);
    background:color-mix(in srgb,var(--bad) 16%,transparent)}
  .offbar-txt{flex:1;min-width:0;line-height:1.35}
  .offbar-txt strong{display:block;font-size:1.05rem;letter-spacing:-.01em}
  .offbar-txt span{font-size:.85rem;color:var(--dim)}
  #offbtn{cursor:pointer;border-radius:9px;padding:10px 20px;font-size:.95rem;
    font-weight:700;letter-spacing:.01em;border:1px solid var(--bad);
    background:var(--bad);color:#fff;flex:none}
  #offbtn:hover{filter:brightness(1.08)}
  #offbtn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  #offbar.is-off #offbtn{border-color:var(--good);background:var(--good)}
  #offbar.is-off .offbar-txt strong{color:var(--bad)}
  /* Mimic, embedded. */
  .panel-hd{display:flex;align-items:center;justify-content:space-between;gap:12px}
  .ghostbtn{cursor:pointer;border-radius:8px;padding:6px 14px;font-size:.85rem;
    font-weight:600;border:1px solid var(--line);background:var(--panel-2);
    color:var(--fg)}
  .ghostbtn:hover{background:var(--line)}
  .ghostbtn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .mimicnote{font-size:.82rem;color:var(--dim);margin:2px 0 10px;line-height:1.45}
  #mimicframe{width:100%;height:min(78vh,900px);border:1px solid var(--line);
    border-radius:10px;background:var(--panel-2);display:block}
  /* Switch rows — named controls, not icons. */
  #switches{max-width:900px;margin:0 auto 6px;display:grid;
    grid-template-columns:repeat(auto-fit,minmax(238px,1fr));gap:2px 20px;
    padding:10px 18px;border-radius:12px;border:1px solid var(--line);
    background:var(--panel)}
  #switches.dimmed{opacity:.45;pointer-events:none}
  .sw{display:flex;align-items:center;gap:12px;padding:9px 2px;cursor:pointer;
    border-radius:8px}
  .sw:hover{background:var(--panel-2)}
  .sw input{position:absolute;opacity:0;width:0;height:0}
  .sw-ui{flex:none;width:38px;height:22px;border-radius:11px;background:var(--line);
    position:relative;transition:background .16s}
  .sw-ui::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;
    border-radius:50%;background:#fff;transition:transform .16s}
  .sw input:checked + .sw-ui{background:var(--good)}
  .sw input:checked + .sw-ui::after{transform:translateX(16px)}
  .sw input:focus-visible + .sw-ui{outline:2px solid var(--accent);outline-offset:2px}
  .sw-txt{display:flex;flex-direction:column;line-height:1.25;min-width:0}
  .sw-txt b{font-size:.92rem;font-weight:600}
  .sw-txt i{font-style:normal;font-size:.76rem;color:var(--dim)}
  .statusline{max-width:900px;margin:8px auto 22px;display:flex;align-items:center;
              gap:9px;flex-wrap:wrap;line-height:1;font-size:12.5px;color:var(--dim)}
  .statusline .st-label{font-weight:600;color:var(--txt);letter-spacing:-.1px}
  .statusline .st-url{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
              font-size:11px;color:var(--dim);background:var(--well);
              border:1px solid var(--line-soft);border-radius:999px;
              padding:3px 9px;letter-spacing:-.2px}
  .kv{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--line-soft)}
  .kv:last-child{border:0}
  .kv span:first-child{color:var(--dim)}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .big{font-size:20px;font-weight:650;letter-spacing:-.3px}
  .fov{position:relative;border-radius:var(--r-lg);overflow:hidden;background:var(--sunken);
       aspect-ratio:16/9;border:1px solid var(--line-soft)}
  .fov img{width:100%;height:100%;object-fit:cover;display:block}
  .fov canvas{position:absolute;inset:0;width:100%;height:100%}
  .fov .noc{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
            color:var(--faint);font-size:14px;text-align:center;padding:0 20px}
  .pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:650}
  /* Header action buttons (alarm / reboot / power / theme). */
  .power{width:40px;height:40px;border-radius:50%;border:1px solid var(--line);
         background:var(--panel);color:var(--good);font-size:18px;cursor:pointer;
         display:inline-flex;align-items:center;justify-content:center;
         box-shadow:var(--shadow);
         transition:transform .12s ease,border-color .15s,background .15s}
  .power:hover{transform:translateY(-1px);border-color:var(--accent-line)}
  .power:active{transform:translateY(0)}
  .power.off{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)}

  /* --- voice controls: two tidy rows of segmented, square icon buttons --- */
  .vc-controls{display:flex;flex-direction:column;gap:10px;margin-bottom:15px}
  .vc-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .seg{display:inline-flex;background:var(--well);border:1px solid var(--line);
       border-radius:var(--r-md);padding:3px;gap:2px}
  .icon-btn{width:34px;height:34px;border-radius:var(--r-sm);border:1px solid transparent;
            background:transparent;font-size:15px;cursor:pointer;line-height:1;
            display:inline-flex;align-items:center;justify-content:center;
            transition:background .12s,border-color .12s,transform .12s;color:var(--txt)}
  .icon-btn:hover{background:var(--panel-2);transform:translateY(-1px)}
  .icon-btn:active{transform:translateY(0)}
  .icon-btn:disabled{opacity:.3;cursor:not-allowed;transform:none}
  .icon-btn.muted{background:var(--bad-soft);border-color:var(--bad-line)}
  .icon-btn.fast-on{background:var(--warn-soft);border-color:var(--warn-line)}
  .icon-btn.vibe-on{background:var(--violet-soft);border-color:var(--violet-line)}
  .icon-btn.openai-on{background:var(--openai-soft);border-color:var(--openai-line)}
  .icon-btn.incognito-on{background:var(--violet-soft);border-color:var(--violet-line)}
  .micmeter{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--dim);
            text-transform:uppercase;letter-spacing:.8px}
  .micbar{position:relative;width:72px;height:8px;border-radius:4px;background:var(--well);
          border:1px solid var(--line);overflow:hidden;display:inline-block}
  #miclevel{position:absolute;left:0;top:0;bottom:0;width:0%;background:var(--good);
            transition:width .15s}
  #micnotch{position:absolute;top:-1px;bottom:-1px;width:2px;background:var(--warn)}
  #mclevel{position:absolute;left:0;top:0;bottom:0;width:0%;background:var(--good)}
  .mcwave{width:100%;height:90px;margin-top:10px;border-radius:var(--r-md);
          background:var(--well);border:1px solid var(--line);display:block}
  #mcrec.on{color:var(--bad);border-color:var(--bad-line);background:var(--bad-soft)}
  /* The level meter cannot say WHY a quiet bar is quiet. This can. */
  .recdot{font-size:10px;letter-spacing:.6px;padding:2px 7px;border-radius:20px;
          border:1px solid var(--line);background:var(--well);color:var(--dim);
          white-space:nowrap}
  .recdot.rec{color:var(--bad);border-color:var(--bad-line);background:var(--bad-soft)}
  .vc-chip{font-size:12px;color:var(--dim);background:var(--well);border:1px solid var(--line);
           border-radius:20px;padding:6px 12px;white-space:nowrap}
  .vc-chip.live{color:var(--good);border-color:var(--good-line);background:var(--good-soft)}
  .vc-chip.talk{color:var(--accent);border-color:var(--accent-line);background:var(--accent-soft)}
  .vc-vol{display:flex;align-items:center;gap:8px;color:var(--dim);font-size:11px;
          text-transform:uppercase;letter-spacing:.8px}
  .vc-vol input{width:120px;accent-color:var(--accent)}
  .sfxbar{display:grid;grid-template-columns:repeat(auto-fill,minmax(116px,1fr));gap:8px}
  .sfx-btn{height:42px;background:var(--well);border:1px solid var(--line);
           border-radius:var(--r-md);color:var(--txt);font:inherit;font-size:12px;
           cursor:pointer;padding:0 10px;
           display:flex;align-items:center;justify-content:space-between;gap:8px;
           transition:border-color .12s,background .12s,transform .12s}
  .sfx-btn:hover{border-color:var(--accent);background:var(--panel-2)}
  .sfx-btn:active{transform:translateY(1px)}
  .sfx-btn.playing{border-color:var(--good-line);background:var(--good-soft);color:var(--good)}
  .sfx-icon{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10px;
            color:var(--accent);border:1px solid var(--accent-line);border-radius:999px;
            padding:2px 6px;white-space:nowrap}
  .sfx-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .convo{max-height:260px;min-height:80px;overflow-y:auto;display:flex;flex-direction:column;
         gap:8px;padding:4px 2px;scroll-behavior:smooth}
  .convo-empty{color:var(--faint);font-size:13px;text-align:center;padding:24px 0}
  .msg{max-width:78%;padding:9px 13px;border-radius:16px;font-size:14px;line-height:1.45}
  .msg.you{align-self:flex-end;background:var(--accent-soft);border-bottom-right-radius:4px}
  .msg.wonder{align-self:flex-start;background:var(--panel-2);border:1px solid var(--line-soft);
              border-bottom-left-radius:4px}
  .msg .who{display:block;font-size:10px;text-transform:uppercase;letter-spacing:1px;
            color:var(--dim);margin-bottom:2px}
  .gallery{display:flex;flex-wrap:wrap;gap:14px}
  .gallery-empty{color:var(--faint);font-size:13px;padding:16px 0}
  .person{width:104px;text-align:center}
  .person .thumb{width:104px;height:104px;border-radius:var(--r-lg);object-fit:cover;
                 background:var(--well);border:2px solid var(--line);display:block}
  .person.named .thumb{border-color:var(--accent-line)}
  .person-del{position:absolute;top:-6px;right:-6px;width:22px;height:22px;border-radius:50%;
              background:var(--bad-soft);border:1px solid var(--bad-line);color:var(--bad-txt);
              font-size:14px;line-height:1;cursor:pointer;display:flex;align-items:center;
              justify-content:center}
  .person-del:hover{background:var(--bad);color:#fff}
  .pname-wrap{margin-top:6px}
  .pname-wrap .combo-input{width:104px;height:auto;font-size:13px;font-weight:650;
               color:var(--txt);background:transparent;border:1px solid transparent;
               border-radius:6px;text-align:center;padding:3px 4px;font-family:inherit}
  .pname-wrap .combo-input::placeholder{color:var(--dim);font-weight:400;font-style:italic}
  .pname-wrap .combo-input:hover,.pname-wrap .combo-input:focus{border-color:var(--line);
               background:var(--well)}
  .pname-wrap .combo-menu{min-width:150px}
  .person .pmeta{font-size:11px;color:var(--dim);margin-top:1px}
  .pstack{display:flex;justify-content:center;align-items:center;margin-top:5px}
  .pstack img{width:28px;height:28px;border-radius:7px;object-fit:cover;
              border:2px solid var(--panel);margin-left:-9px}
  .pstack img:first-child{margin-left:0}
  .pstack-more{font-size:10px;color:var(--dim);margin-left:4px}
  /* --- Vibey's head: the OpenClaw thought stream --- */
  .brainlog{max-height:280px;overflow-y:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
            font-size:12px;line-height:1.55;display:flex;flex-direction:column;gap:6px;
            scroll-behavior:smooth;background:var(--well);border:1px solid var(--line);
            border-radius:var(--r-lg);padding:12px}
  .brainlog-empty{color:var(--faint);text-align:center;padding:18px 0;font-family:inherit}
  .bl{display:flex;gap:8px;align-items:baseline}
  .bl .tag{flex-shrink:0;font-size:10px;text-transform:uppercase;letter-spacing:.8px;
           width:64px;text-align:right}
  .bl.thinking .tag{color:var(--violet)} .bl.thinking .tx{color:var(--dim);font-style:italic}
  .bl.tool .tag{color:var(--warn)}  .bl.tool .tx{color:var(--txt)}
  .bl.result .tag{color:var(--faint)}   .bl.result .tx{color:var(--faint)}
  .bl.say .tag{color:var(--good)}  .bl.say .tx{color:var(--txt)}
  .bl.user .tag{color:var(--accent)} .bl.user .tx{color:var(--accent)}
  .bl .tx{white-space:pre-wrap;word-break:break-word}
  .peoplerows{display:flex;flex-direction:column;gap:8px;margin:8px 0}
  .prow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .prow-known{background:var(--accent-soft);color:var(--accent);border-radius:20px;
              padding:4px 12px;font-size:13px;font-weight:650}
  /* --- unified name combobox: one input, a floating suggestion list that
     always tracks the input directly below it, never drifts off elsewhere --- */
  .combo{position:relative;display:inline-block}
  .combo-input{background:var(--well);border:1px solid var(--line);border-radius:var(--r-md);
               height:34px;padding:0 12px;color:var(--txt);font:inherit;font-size:13px;
               outline:none;transition:border-color .15s,box-shadow .15s;width:100%}
  .combo-input:focus{border-color:var(--accent);
               box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 22%,transparent)}
  .combo-menu{display:none;position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:30;
              max-height:200px;overflow-y:auto;background:var(--panel);
              border:1px solid var(--line);border-radius:var(--r-lg);padding:5px;
              box-shadow:var(--shadow-lg)}
  .combo-menu.open{display:block}
  .combo-item{padding:8px 12px;border-radius:var(--r-sm);font-size:13px;cursor:pointer;
              white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .combo-item:hover,.combo-item.hi{background:var(--panel-2)}
  .combo-empty{padding:8px 12px;font-size:12px;color:var(--dim);font-style:italic}
  /* --- per-person photo manager modal --- */
  .modal-backdrop{display:none;position:fixed;inset:0;background:rgba(6,9,16,.62);
                  backdrop-filter:blur(3px);
                  z-index:50;align-items:center;justify-content:center;padding:20px}
  .modal-backdrop.open{display:flex}
  .modal{background:var(--panel);border:1px solid var(--line);border-radius:var(--r-xl);
         padding:20px;max-width:560px;width:100%;max-height:80vh;overflow-y:auto;
         box-shadow:var(--shadow-lg)}
  .modal-head{display:flex;align-items:center;gap:10px;margin-bottom:14px}
  .pm-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:12px}
  .pm-cell{position:relative}
  .pm-cell img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:var(--r-md);
               border:2px solid var(--line);display:block}
  .pm-del{position:absolute;top:-7px;right:-7px;width:24px;height:24px;border-radius:50%;
          background:var(--bad-soft);border:1px solid var(--bad-line);color:var(--bad-txt);
          font-size:13px;cursor:pointer;display:flex;align-items:center;
          justify-content:center;line-height:1}
  .pm-del:hover{background:var(--bad);color:#fff}
  .pm-del:disabled{opacity:.3;cursor:not-allowed}
  .pm-cell .when{font-size:10px;color:var(--dim);margin-top:3px;text-align:center}
  .cap-btn{background:var(--well);border:1px solid var(--line);border-radius:var(--r-md);
           height:36px;padding:0 16px;color:var(--txt);font:inherit;font-size:13px;
           cursor:pointer;transition:border-color .12s,background .12s}
  .cap-btn:hover{border-color:var(--accent);background:var(--panel-2)}
  .cap-btn:disabled{opacity:.4}
  .capgrid{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
  .capgrid a{display:block;position:relative}
  .capgrid img,.capgrid video{width:120px;height:68px;object-fit:cover;border-radius:var(--r-sm);
             border:1px solid var(--line);display:block;background:var(--sunken)}
  .capgrid .cap-tag{position:absolute;bottom:4px;right:4px;font-size:9px;
             background:rgba(0,0,0,.7);padding:1px 5px;border-radius:4px;color:#cbd2e0}
  /* --- connection panel: find the robot, move it between networks --- */
  .conn-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
  .robot-card{display:flex;align-items:center;gap:12px;padding:11px 13px;
              border:1px solid var(--line);border-radius:var(--r-md);
              background:var(--well);margin-bottom:8px;flex-wrap:wrap}
  .robot-card.active{border-color:var(--accent-line);background:var(--accent-soft)}
  .robot-card .rc-main{flex:1;min-width:180px}
  .robot-card .rc-name{font-weight:650;font-size:13px}
  .robot-card .rc-meta{font-size:11px;color:var(--dim);
                       font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .net-list{display:flex;flex-direction:column;gap:5px;max-height:190px;
            overflow-y:auto;margin-top:8px}
  .net{display:flex;align-items:center;gap:9px;padding:7px 11px;border-radius:var(--r-sm);
       cursor:pointer;font-size:13px;border:1px solid transparent}
  .net:hover{background:var(--panel-2);border-color:var(--line)}
  .net.on{background:var(--good-soft);border-color:var(--good-line);color:var(--good);
          font-weight:650}
  .net .lock{margin-left:auto;font-size:11px;color:var(--dim)}
  /* Inputs that used to carry inline hex colours now share this. */
  .field{background:var(--well);border:1px solid var(--line);border-radius:var(--r-md);
         color:var(--txt);font:inherit;outline:none;transition:border-color .15s,box-shadow .15s}
  .field:focus{border-color:var(--accent);
         box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 22%,transparent)}
  .btn-accent{background:var(--accent);color:var(--accent-txt);border:0;
              border-radius:var(--r-md);font:inherit;font-weight:650;cursor:pointer;
              transition:filter .12s,transform .12s}
  .btn-accent:hover{filter:brightness(1.08)}
  .btn-accent:active{transform:translateY(1px)}
  /* Scrollbars, so the panels don't get a bright OS bar in dark mode. */
  .convo::-webkit-scrollbar,.brainlog::-webkit-scrollbar,
  .combo-menu::-webkit-scrollbar,.modal::-webkit-scrollbar{width:9px}
  .convo::-webkit-scrollbar-thumb,.brainlog::-webkit-scrollbar-thumb,
  .combo-menu::-webkit-scrollbar-thumb,.modal::-webkit-scrollbar-thumb{
    background:var(--line);border-radius:99px;border:2px solid transparent;
    background-clip:content-box}
  /* ---- Modes panel ----------------------------------------------------
     Three stacked tiers, so the buttons are a ladder rather than a radio
     group: picking "Pi + Mac" is picking everything below it too, and the
     fill on each button shows how much of the whole robot that is. */
  .mode-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
  .mode-btn{flex:1;min-width:150px;position:relative;overflow:hidden;
    padding:11px 13px;border:1px solid var(--line);border-radius:11px;
    background:var(--panel-2);color:var(--txt);cursor:pointer;text-align:left;
    font:inherit;transition:border-color .15s,background .15s}
  .mode-btn:hover{border-color:var(--accent-line)}
  .mode-btn.on{border-color:var(--accent);background:var(--accent-soft)}
  .mode-btn .mb-fill{position:absolute;inset:0 auto 0 0;background:var(--accent-soft);
    z-index:0;transition:width .3s}
  .mode-btn.on .mb-fill{background:var(--accent-soft);opacity:.85}
  .mode-btn>span{position:relative;z-index:1;display:block}
  .mode-btn .mb-name{font-weight:650;font-size:13px}
  .mode-btn .mb-sub{font-size:11px;color:var(--dim);margin-top:2px;line-height:1.35}
  .mode-btn .mb-pct{font-size:11px;color:var(--accent);margin-top:5px;font-variant-numeric:tabular-nums}
  .mode-tier{margin-top:13px}
  .mode-tier>h3{font-size:11px;letter-spacing:.08em;text-transform:uppercase;
    color:var(--faint);margin:0 0 6px;font-weight:600}
  .mode-cap{display:flex;gap:9px;align-items:flex-start;padding:5px 0;
    border-top:1px solid var(--line-soft);font-size:12px}
  .mode-tier>.mode-cap:first-of-type{border-top:0}
  .mode-cap .mc-dot{flex:none;width:8px;height:8px;border-radius:50%;margin-top:5px;
    background:var(--faint)}
  .mode-cap.live .mc-dot{background:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  .mode-cap.dim{opacity:.45}
  .mode-cap .mc-txt{flex:1;min-width:0}
  .mode-cap .mc-note{display:block;font-size:11px;color:var(--dim);line-height:1.4;margin-top:1px}
  .mode-cap .mc-port{flex:none;font-size:10px;padding:1px 6px;border-radius:20px;
    border:1px solid var(--line);color:var(--dim);margin-top:3px;white-space:nowrap}
  .mode-cap .mc-port[data-p=yes]{color:#63d68c;border-color:#2c5c3d}
  .mode-cap .mc-port[data-p=native]{color:var(--accent);border-color:var(--accent-line)}
  .mode-cap .mc-port[data-p=partial]{color:#e0b45c;border-color:#5c4a25}
  .mode-cap .mc-port[data-p=no]{color:#c96a6a;border-color:#5c2f2f}
</style>
<script>
  // Runs before first paint: without this a light-mode user gets a dark flash.
  try{
    var _t=localStorage.getItem('vibey-theme');
    if(_t==='light'||_t==='dark')document.documentElement.setAttribute('data-theme',_t);
  }catch(e){}
</script>
</head><body>
<div class=hdr style="display:flex;align-items:center;gap:14px">
  <h1 style="flex:1">🤖 Vibey — what the robot sees</h1>
  <button id=themebtn class=power title="Switch colour theme" style="color:var(--dim)">☀️</button>
  <button id=alarmbtn class=power title="Wake-up show: sunrise song + singing + dance" style="color:var(--accent)">🌅</button>
  <button id=rebootbtn class=power title="Reboot the robot (fixes stuck motors/sounds/camera, ~30s)" style="color:var(--warn)">⟳</button>
  <button id=powerbtn class=power title="Put Vibey to sleep / wake it up">⏻</button>
</div>
<!-- The real off switch. Its own bar above everything, because "is this thing
     listening to me right now" is the one question the page must answer before
     any other, and it was previously only answerable by reading an icon. -->
<div id=offbar>
  <div class=offbar-txt>
    <strong id=offtitle>Vibey is ON</strong>
    <span id=offsub>listening — wake phrase active</span>
  </div>
  <button id=offbtn type=button>Turn OFF</button>
</div>
<!-- One row per thing that can be on or off, each named in plain words.
     These were previously five unlabelled emoji buttons, which meant knowing
     what the robot was currently doing required remembering what 🅾️ meant. -->
<div id=switches>
  <label class=sw><input type=checkbox id=sw-wake><span class=sw-ui></span>
    <span class=sw-txt><b>Wake phrase</b><i>responds to "hey vibey"</i></span></label>
  <label class=sw><input type=checkbox id=sw-claps><span class=sw-ui></span>
    <span class=sw-txt><b>Clap to wake</b><i>two claps — also fires on doors, books</i></span></label>
  <label class=sw><input type=checkbox id=sw-tracking><span class=sw-ui></span>
    <span class=sw-txt><b>Face tracking</b><i>turns its head to follow you</i></span></label>
  <label class=sw><input type=checkbox id=sw-openai><span class=sw-ui></span>
    <span class=sw-txt><b>Realtime voice</b><i>full-duplex — just talk</i></span></label>
  <label class=sw><input type=checkbox id=sw-mic><span class=sw-ui></span>
    <span class=sw-txt><b>Microphone</b><i>off = hears nothing at all</i></span></label>
  <label class=sw><input type=checkbox id=sw-naming><span class=sw-ui></span>
    <span class=sw-txt><b>Learn names</b><i>off = sees you, won't ask who you are</i></span></label>
</div>
<div class=statusline id=status><span class=st-label>connecting…</span></div>
<div class=grid>
  <div class="panel full">
    <h2>Field of view · live camera + face</h2>
    <div class=fov>
      <img id=cam alt="" />
      <canvas id=fovc width=800 height=450></canvas>
      <div class=noc id=noc style=display:none>
        camera feed offline<br><small>start it with:
        <code>source reachy_env/bin/activate &amp;&amp; python3 reachy_camera.py</code></small>
      </div>
    </div>
  </div>
  <div class="panel full" id=djpanel>
    <h2 class=panel-hd><span>DJ · <span id=djtitle>nothing loaded</span></span>
      <span class=sub id=djbpm></span></h2>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0">
      <span id=djdot style="width:18px;height:18px;border-radius:50%;background:#333;display:inline-block;transition:transform .06s,background .06s"></span>
      <select id=djtrack style="min-width:180px"></select>
      <button type=button class=ghostbtn id=djplay>▶ Play</button>
      <button type=button class=ghostbtn id=djstop>■ Stop</button>
      <button type=button class=ghostbtn id=djdown>−4%</button>
      <input type=range id=djslider min=80 max=125 value=100 style="width:200px" title="tempo %">
      <button type=button class=ghostbtn id=djup>+4%</button>
      <span id=djpos class=sub></span>
    </div>
    <div class=sub>Say it instead: “play something”, “take it up”, “slower”, “stop the music”.</div>
  </div>
  <div class="panel full" id=modepanel>
    <h2>Modes · what's actually running
      <span id=modesub class=sub style="display:inline;margin-left:6px"></span></h2>
    <div class=mode-row id=moderow></div>
    <div id=modecaps></div>
  </div>
  <div class="panel full" id=mimicpanel>
    <h2 class=panel-hd>
      <span>Face mimicry · drive the head with your face</span>
      <button id=mimicbtn type=button class=ghostbtn>Show</button>
    </h2>
    <!-- Loaded into an iframe, and only once opened.
         An iframe rather than inlining: mimic is a self-contained app with its
         own MediaPipe pipeline, its own SDK and a documented set of locked
         layout invariants, so sharing a document with the dashboard would mean
         CSS and global-name collisions for no gain. Lazily, because opening it
         starts the webcam and downloads the vision model — neither of which
         should happen to somebody who just wanted to see the camera feed. -->
    <div id=mimicwrap style="display:none">
      <div class=mimicnote>Runs the camera in your browser and drives the neck at
        20&nbsp;Hz. Face tracking is suspended while it streams, and handed back
        when you stop. Nothing moves while Vibey is switched off.</div>
      <iframe id=mimicframe title="Face mimicry" allow="camera"></iframe>
    </div>
  </div>
  <div class="panel full">
    <h2>Voice · talk with Vibey</h2>
    <div class=vc-controls>
      <div class=vc-row>
        <span class=seg>
          <button id=mutebtn class=icon-btn title="Mute Vibey's ears">🎙️</button>
          <button id=fastbtn class=icon-btn title="Fast mode: ElevenLabs agent, skips Claude">⚡</button>
          <button id=vibebtn class=icon-btn title="Vibe mode: OpenClaw agent — can improve its own code">🎮</button>
          <button id=openaibtn class=icon-btn title="Realtime mode: OpenAI full-duplex voice — just talk, interrupt anytime">🅾️</button>
          <button id=incognitobtn class=icon-btn title="Incognito: keep watching and looking at people, but stop asking names, storing faces and taking snapshots">🕶️</button>
          <button id=resaybtn class=icon-btn title="Re-say the last thing Vibey said">🔁</button>
        </span>
        <span id=vcstatus class=vc-chip>connecting…</span>
        <span style="flex:1"></span>
        <span class=micmeter title="Mic level — bar past the notch means Vibey can hear it">
          mic <span class=micbar><span id=miclevel></span><span id=micnotch></span></span>
          <span id=micmode class=recdot title="Recording state">–</span>
        </span>
      </div>
      <div class=vc-row>
        <span id=emotes class=seg></span>
        <span style="flex:1"></span>
        <span class=vc-vol>vol
          <input id=vol type=range min=0 max=100 value=60>
          <b id=volval class=mono>–</b>
        </span>
      </div>
    </div>
    <div id=convo class=convo><div class=convo-empty>Say something — the conversation shows up here.</div></div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <input id=saytext class=field placeholder="…or message Vibey here (prefix with say: to speak text verbatim)"
        style="flex:1;padding:10px 12px">
      <button id=saybtn class=btn-accent style="padding:10px 18px">Send</button>
    </div>
    <div id=saystatus class=sub style="margin:8px 0 0"></div>
  </div>
  <div class="panel full">
    <h2>Friends · known faces <span id=peoplecount class=sub style="display:inline;margin-left:6px"></span></h2>
    <div id=gallery class=gallery>
      <div class=gallery-empty>Nobody learned yet — stand in front of Vibey and teach it a name above.</div>
    </div>
  </div>
  <div class="panel full">
    <h2>🧠 Vibey's head <span class=sub style="display:inline;margin-left:6px">OpenClaw agent — thinking, tool calls, code edits</span></h2>
    <div id=brainlog class=brainlog><div class=brainlog-empty>Turn on 🎮 Vibe mode and talk to it — its thought process streams here.</div></div>
  </div>
  <div class="panel full">
    <h2>Connection · robot link <span id=connsub class=sub
        style="display:inline;margin-left:6px;text-transform:none;letter-spacing:0"></span></h2>
    <div class=conn-row>
      <button id=findbtn class=cap-btn>🔍 Find robot</button>
      <button id=scanbtn class=cap-btn title="Also sweep every address on this network (slower)">Deep scan</button>
      <span id=connstatus class=vc-chip>idle</span>
    </div>
    <div id=robotlist></div>
    <div class=conn-row style="margin-top:14px">
      <button id=wifibtn class=cap-btn title="Scanning makes the robot sweep every Wi-Fi channel, which can briefly drop it off the network">📶 Robot Wi-Fi</button>
      <span id=wifinow class=sub style="margin:0"></span>
    </div>
    <div id=wifiwrap style="display:none">
      <div class=net-list id=netlist></div>
      <div class=conn-row style="margin-top:10px">
        <input id=wifissid class=field placeholder="network name" style="padding:8px 11px;flex:1;min-width:150px">
        <input id=wifipass class=field type=password placeholder="password" style="padding:8px 11px;flex:1;min-width:150px">
        <button id=wifijoin class=btn-accent style="padding:9px 16px">Join</button>
      </div>
      <div id=wifimsg class=sub style="margin:8px 0 0"></div>
    </div>
  </div>
  <div class="panel full">
    <h2>Sound effects <span id=sfxstatus class=sub style="display:inline;margin-left:6px"></span></h2>
    <div id=sfxbar class=sfxbar></div>
  </div>
  <div class=panel>
    <h2>Perception</h2>
    <div class=kv><span>Face detected</span><b id=facedet>—</b></div>
    <div class=kv><span>People in view</span><b id=personcount>—</b></div>
    <div id=peoplerows class=peoplerows></div>
    <div class=kv><span>Hearing sound at</span><b class=mono id=doa>—</b></div>
    <div class=kv><span>Speech now</span><b id=speech>—</b></div>
  </div>
  <div class=panel>
    <h2>Body · handsfree</h2>
    <div class=kv><span>Head pose (r/p/y)</span><b class=mono id=pose>—</b></div>
    <div class=kv><span>Antennas</span><b class=mono id=ant>—</b></div>
    <div class=kv><span>Voice</span><b id=voice>—</b></div>
    <div class=kv><span>Last command</span><b id=cmd>—</b></div>
  </div>
  <div class="panel full">
    <h2>📸 Capture <span class=sub style="display:inline;margin-left:6px">saved to captures/</span></h2>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <button id=snapbtn class=cap-btn>📷 Photo</button>
      <button id=clipbtn class=cap-btn>🎬 10s clip</button>
      <span id=capstatus class=sub style="margin:0"></span>
    </div>
    <div id=capgrid class=capgrid></div>
  </div>
  <div class="panel full">
    <h2>🎤 Mic check <span class=sub style="display:inline;margin-left:6px">record your own mic, hear it back</span></h2>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <button id=mcrec class=cap-btn>⏺ Record</button>
      <button id=mcplay class=cap-btn disabled>▶ Play back</button>
      <button id=mcdl class=cap-btn disabled>⬇ Download</button>
      <button id=mcclear class=cap-btn disabled>✕ Clear</button>
      <span id=mcstatus class=sub style="margin:0">idle</span>
    </div>
    <canvas id=mcwave class=mcwave width=900 height=90></canvas>
    <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
      <span class=micmeter style="flex:1">
        level <span class=micbar style="flex:1;width:auto"><span id=mclevel></span></span>
        <b class=mono id=mcpeak style="min-width:52px">–</b>
      </span>
      <b class=mono id=mcclock>0.0s</b>
    </div>
    <audio id=mcaudio style="display:none"></audio>
  </div>
  <div class=panel>
    <h2>⏰ Alarms <span class=sub style="display:inline;margin-left:6px">wake-up shows</span></h2>
    <div id=alarmlist style="display:flex;flex-direction:column;gap:6px"></div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <input id=alarmtime type=time value="07:00" class=field style="padding:8px 10px">
      <select id=alarmrepeat class=field style="padding:8px 10px">
        <option value=once>once</option><option value=daily>daily</option>
      </select>
      <button id=alarmadd class=cap-btn>＋ Add</button>
    </div>
  </div>
  <div class="panel full">
    <h2>🌐 VibeVerse <span class=sub style="display:inline;margin-left:6px">Vibey's avatar on Edge Island · <a href="https://myvibeverse.com/city?spawn=island" target=_blank style="color:var(--accent)">visit</a></span></h2>
    <div class=kv><span>In lobby with</span><b id=versewho>—</b></div>
    <div id=verselog class=brainlog style="max-height:170px;margin-top:10px"></div>
  </div>
</div>
<div id=photomodal class=modal-backdrop>
  <div class=modal>
    <div class=modal-head>
      <b id=pm-title>Photos</b>
      <span id=pm-sub class=sub style="margin:0"></span>
      <span style="flex:1"></span>
      <button id=pm-close class=icon-btn title="Close">✕</button>
    </div>
    <div id=pm-grid class=pm-grid></div>
    <div id=pm-hint class=sub style="margin:10px 0 0"></div>
  </div>
</div>
<script>
// --- colour theme: dark (default) / light, remembered across reloads -------
// Tokens live in CSS; JS only flips the data-theme attribute and reads
// computed values back out for the <canvas> face overlay.
const themeColor = v =>
  getComputedStyle(document.documentElement).getPropertyValue(v).trim() || '#4cc4f5';
(function(){
  const root = document.documentElement, KEY = 'vibey-theme';
  const sysLight = () => matchMedia('(prefers-color-scheme: light)').matches;
  const active   = () => root.getAttribute('data-theme') || (sysLight() ? 'light' : 'dark');
  function paint(){
    const b = document.getElementById('themebtn');
    if(!b) return;
    const light = active() === 'light';
    b.textContent = light ? '🌙' : '☀️';
    b.title = light ? 'Switch to dark mode' : 'Switch to light mode';
  }
  function toggle(){
    const next = active() === 'light' ? 'dark' : 'light';
    root.setAttribute('data-theme', next);
    try{ localStorage.setItem(KEY, next); }catch(e){}
    paint();
  }
  const btn = document.getElementById('themebtn');
  if(btn) btn.addEventListener('click', toggle);
  // Keep following the OS for as long as the user hasn't picked one.
  matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
    let picked = null;
    try{ picked = localStorage.getItem(KEY); }catch(e){}
    if(!picked) paint();
  });
  paint();
})();
const REACHY=%REACHY%, HANDSFREE=%HANDSFREE%, CAM=%CAM%;
const $=id=>document.getElementById(id);
const c=$('fovc'),g=c.getContext('2d');

// Camera MJPEG feed — <img> streams it; on error show the fallback note.
const cam=$('cam');
cam.onerror=()=>{$('noc').style.display='flex';cam.style.opacity=0;};
cam.onload =()=>{$('noc').style.display='none';cam.style.opacity=1;};
cam.src=CAM+'/stream';

// Names Vibey already knows, feeding every nameCombo's suggestion list —
// refreshed alongside the gallery so a newly-taught name shows up everywhere.
let knownNames=[];
async function fetchKnownNames(){
  try{ knownNames=await(await fetch('/knownnames')).json(); }catch(_){}
}

function drawFOV(people){
  // Transparent overlay on top of the video — a ring + label per person in
  // frame (there can be more than one), plus a faint crosshair.
  const W=c.width,H=c.height;
  g.clearRect(0,0,W,H);
  g.strokeStyle='rgba(255,255,255,.10)';g.lineWidth=1;
  g.beginPath();g.moveTo(W/2,0);g.lineTo(W/2,H);g.moveTo(0,H/2);g.lineTo(W,H/2);g.stroke();
  for(const p of (people||[])){
    // x,y are normalized offsets ~[-1,1]; center them into the frame
    const x=W/2+(p.x||0)*W/2, y=H/2+(p.y||0)*H/2;
    const known=!!p.name;
    // Read the live theme tokens so the overlay tracks light/dark.
    const col=known?themeColor('--accent'):themeColor('--good');
    g.strokeStyle=col;g.lineWidth=3;
    g.beginPath();g.arc(x,y,44,0,7);g.stroke();
    const label=p.name||'unknown';
    g.font='bold 14px system-ui';
    const tw=g.measureText(label).width;
    // The label sits on video, which is dark in both themes — keep the
    // plate dark and the text the accent colour for contrast.
    g.fillStyle='rgba(5,6,10,.78)';
    g.fillRect(x-tw/2-8,y-72,tw+16,24);
    g.fillStyle=col;
    g.fillText(label,x-tw/2,y-55);
  }
}

// Renders one row per currently-visible person: a pill for known names, or a
// name combobox (type or pick) for unknowns.
let peopleRowIds=[];
// One open dropdown at a time; closed on any outside click.
document.addEventListener('click',e=>{
  if(!e.target.closest('.combo'))
    document.querySelectorAll('.combo-menu.open').forEach(m=>m.classList.remove('open'));
});

async function teachFace(faceId,name,after){
  name=(name||'').trim(); if(!name)return;
  try{
    await fetch('/nameface',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,face_id:faceId})});
  }catch(_){}
  galleryLen=-1; fetchGallery(); fetchKnownNames();
  if(after)after();
}

// One combobox component used everywhere a face gets a name: a plain text
// input (type a new name, or start typing to filter) with a floating
// suggestion list anchored directly beneath *this* input — never detached,
// never rendered by the browser somewhere else on the page (that was the
// native <datalist> popup's problem). Same component for the "who is this?"
// row on a live in-frame face and for renaming someone in the gallery below.
function nameCombo(currentName,onSave,placeholder){
  const wrap=document.createElement('span');
  wrap.className='combo';
  const inp=document.createElement('input');
  inp.className='combo-input';
  inp.value=currentName||'';
  inp.placeholder=placeholder||'Who is this?';
  inp.autocomplete='off';
  const menu=document.createElement('div');
  menu.className='combo-menu';
  let hi=-1;
  const items=()=>Array.from(menu.querySelectorAll('.combo-item[data-name]'));

  function renderMenu(){
    const q=inp.value.trim().toLowerCase();
    const matches=knownNames.filter(n=>n.toLowerCase().includes(q)&&n!==currentName);
    menu.innerHTML='';
    hi=-1;
    if(!matches.length){
      const empty=document.createElement('div');
      empty.className='combo-empty';
      empty.textContent=q?'no match — Enter to teach a new name':'start typing or pick a known name';
      menu.appendChild(empty);
    }else{
      for(const n of matches){
        const it=document.createElement('div');
        it.className='combo-item'; it.textContent=n; it.dataset.name=n;
        it.onmousedown=e=>{  // mousedown fires before blur — beats the blur-save
          e.preventDefault();
          inp.value=n; menu.classList.remove('open'); commit();
        };
        menu.appendChild(it);
      }
    }
  }
  function openMenu(){renderMenu();menu.classList.add('open');}
  function commit(){
    const v=inp.value.trim();
    menu.classList.remove('open');
    if(v&&v!==currentName){currentName=v;onSave(v);}
  }
  inp.addEventListener('focus',()=>{inp.select();openMenu();});
  inp.addEventListener('input',renderMenu);
  inp.addEventListener('blur',commit);
  inp.addEventListener('keydown',e=>{
    const list=items();
    if(e.key==='Enter'){e.preventDefault();inp.blur();}
    else if(e.key==='Escape'){inp.value=currentName||'';menu.classList.remove('open');inp.blur();}
    else if(e.key==='ArrowDown'&&list.length){e.preventDefault();hi=Math.min(hi+1,list.length-1);
      list.forEach((it,i)=>it.classList.toggle('hi',i===hi)); inp.value=list[hi].dataset.name;}
    else if(e.key==='ArrowUp'&&list.length){e.preventDefault();hi=Math.max(hi-1,0);
      list.forEach((it,i)=>it.classList.toggle('hi',i===hi)); inp.value=list[hi].dataset.name;}
  });
  wrap.appendChild(inp); wrap.appendChild(menu);
  return {el:wrap,input:inp};
}

function renderPeopleRows(people){
  const ids=people.map(p=>p.face_id+'|'+(p.name||'')).join(',');
  if(ids===peopleRowIds.join(','))return;  // avoid nuking focus every 250ms
  // Never rebuild out from under someone actively typing a name.
  if(document.activeElement&&document.activeElement.closest('#peoplerows'))return;
  peopleRowIds=people.map(p=>p.face_id+'|'+(p.name||''));
  const box=$('peoplerows');
  box.innerHTML='';
  for(const p of people){
    const row=document.createElement('div');
    row.className='prow';
    if(p.name){
      const pill=document.createElement('span');
      pill.className='prow-known';
      pill.textContent=p.name;
      row.appendChild(pill);
    }else{
      row.appendChild(nameCombo(null,name=>teachFace(p.face_id,name),'Who is this?').el);
    }
    box.appendChild(row);
  }
}

async function tick(){
  try{
    const r=await fetch('/perception');const d=await r.json();
    $('powerbtn').classList.toggle('off',!!d.asleep);
    const st = d.asleep  ? ['--warn','Vibey is asleep 😴']
             : d.online  ? ['--good','robot online']
             :             ['--bad', 'robot offline'];
    $('status').innerHTML =
        '<span class=dot style="--c:var('+st[0]+')"></span>'
      + '<span class=st-label>'+st[1]+'</span>'
      + '<span class=st-url>'+REACHY.replace(/^https?:\/\//,'')+'</span>';
    const people=d.people||[];
    $('facedet').textContent=people.length?'yes ✅':'no';
    $('personcount').textContent=people.length
      ? people.length+' · '+people.map(p=>p.name||'unknown').join(', ')
      : '—';
    renderPeopleRows(people);
    const ang=d.doa&&d.doa.angle!=null?(d.doa.angle*180/Math.PI).toFixed(0)+'°':'—';
    $('doa').textContent=ang;
    $('speech').innerHTML=d.doa&&d.doa.speech
      ?'<span class=pill style="background:var(--good-soft);color:var(--good)">talking</span>':'quiet';
    const p=d.pose;
    $('pose').textContent=p?`${p.roll.toFixed(2)} ${p.pitch.toFixed(2)} ${p.yaw.toFixed(2)}`:'—';
    const a=d.antennas;
    $('ant').textContent=a?`${a[0].toFixed(2)}  ${a[1].toFixed(2)}`:'—';
    drawFOV(people);
  }catch(e){
    $('status').innerHTML='<span class=dot style="--c:var(--bad)"></span>'
      +'<span class=st-label>viewer error</span>';
  }
}
// handsfree voice state via its SSE stream
function connectHandsfree(){
  try{
    const es=new EventSource(HANDSFREE+'/events');
    es.onmessage=ev=>{
      try{const d=JSON.parse(ev.data);
        $('voice').innerHTML = d.v2Armed
          ? '<span class=pill style="background:var(--accent-soft);color:var(--accent)">armed 🟢</span>'
          : 'standby';
        $('cmd').textContent = d.voiceLastResult && d.voiceLastResult!=='(no match)'
          ? d.voiceLastResult : (d.voiceLastText||'—');
      }catch(_){}
    };
    es.onerror=()=>{$('voice').textContent='handsfree offline';};
  }catch(e){$('voice').textContent='handsfree offline';}
}
// ---- voice conversation panel ----
let vcMuted=false, vcFast=false, lastLen=-1;

$('mutebtn').onclick=async()=>{
  vcMuted=!vcMuted;
  $('mutebtn').classList.toggle('muted',vcMuted);
  $('mutebtn').textContent=vcMuted?'🔇':'🎙️';
  try{await fetch('/mute',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({muted:vcMuted})});}catch(_){}
};

$('fastbtn').onclick=async()=>{
  vcFast=!vcFast;
  $('fastbtn').classList.toggle('fast-on',vcFast);
  try{await fetch('/fastmode',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({fast:vcFast})});}catch(_){}
};

$('resaybtn').onclick=async()=>{
  try{
    const r=await fetch('/resay',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    if(!r.ok)$('saystatus').textContent='nothing to re-say yet';
  }catch(_){}
};

// Mimic: show/hide, and only build the iframe the first time it is shown.
// Hiding TEARS IT DOWN rather than just display:none — the page holds a live
// webcam and a 20 Hz send loop, and a hidden panel quietly driving the robot's
// neck is exactly the kind of thing you cannot find later.
$('mimicbtn').onclick=()=>{
  const wrap=$('mimicwrap'), f=$('mimicframe'), open=wrap.style.display!=='none';
  if(open){
    f.removeAttribute('src');           // stops the camera and the send loop
    wrap.style.display='none';
    $('mimicbtn').textContent='Show';
  }else{
    if(!f.getAttribute('src')) f.setAttribute('src','/mimic/');
    wrap.style.display='';
    $('mimicbtn').textContent='Hide';
    wrap.scrollIntoView({behavior:'smooth',block:'nearest'});
  }
};

// Each switch: how to read it out of /state, and where a click sends it.
// Kept as data so a new toggle is one row here plus one row of markup.
const SWITCHES={
  'sw-wake':     {read:d=>d.switches&&d.switches.wake,
                  post:v=>['/switch',{name:'wake',on:v}]},
  'sw-claps':    {read:d=>d.switches&&d.switches.claps,
                  post:v=>['/switch',{name:'claps',on:v}]},
  'sw-tracking': {read:d=>d.switches&&d.switches.tracking,
                  post:v=>['/switch',{name:'tracking',on:v}]},
  'sw-openai':   {read:d=>d.openai,        post:v=>['/openaimode',{openai:v}]},
  // Inverted on purpose: the control reads "Microphone", so ON must mean it
  // can hear. The service stores the opposite (muted), and a switch whose
  // label is the negation of its state is how you click the wrong one.
  'sw-mic':      {read:d=>!d.muted,        post:v=>['/mute',{muted:!v}]},
  'sw-naming':   {read:d=>!d.incognito,    post:v=>['/incognito',{on:!v}]},
};
let swBusy={};
for(const [id,cfg] of Object.entries(SWITCHES)){
  const el=$(id); if(!el) continue;
  el.addEventListener('change',async()=>{
    const v=el.checked; swBusy[id]=true;
    const [url,body]=cfg.post(v);
    try{
      const r=await fetch(url,{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(!r.ok){ el.checked=!v; }        // refused — put it back
    }catch(_){ el.checked=!v; }
    swBusy[id]=false;
  });
}
function paintSwitches(d){
  for(const [id,cfg] of Object.entries(SWITCHES)){
    const el=$(id); if(!el||swBusy[id]) continue;
    const v=cfg.read(d);
    if(typeof v==='boolean'&&el.checked!==v) el.checked=v;
  }
  // Off is the master switch: the rest are meaningless until it's back on.
  const box=$('switches'); if(box) box.classList.toggle('dimmed',!!d.off);
}

let vcOff=false;
function paintOff(off){
  vcOff=!!off;
  const bar=$('offbar');
  bar.classList.toggle('is-off',vcOff);
  $('offtitle').textContent = vcOff ? 'Vibey is OFF' : 'Vibey is ON';
  $('offsub').textContent = vcOff
    ? 'not listening — the wake phrase will not work'
    : 'listening — wake phrase active';
  $('offbtn').textContent = vcOff ? 'Turn ON' : 'Turn OFF';
  // Waking is refused while off, so don't offer a button that will just fail.
  ['powerbtn','alarmbtn'].forEach(id=>{const b=$(id); if(b) b.disabled=vcOff;});
}
$('offbtn').onclick=async()=>{
  const want=!vcOff;
  paintOff(want);                       // optimistic; /state corrects it
  $('offbtn').disabled=true;
  try{
    const r=await fetch('/off',
      {method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({off:want})});
    const d=await r.json();
    if(typeof d.off==='boolean') paintOff(d.off);
  }catch(_){}
  $('offbtn').disabled=false;
};

let vcVibe=false;
$('vibebtn').onclick=async()=>{
  vcVibe=!vcVibe;
  $('vibebtn').classList.toggle('vibe-on',vcVibe);
  try{await fetch('/vibemode',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({vibe:vcVibe})});}catch(_){}
};

let vcIncognito=false;
$('incognitobtn').onclick=async()=>{
  const want=!vcIncognito;
  // Not optimistic: this one is a privacy control, and a button that looks on
  // while the robot is still enrolling faces is the failure that matters. Only
  // paint it after the server confirms.
  try{
    const r=await fetch('/incognito',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({on:want})});
    if(!r.ok)throw new Error('rejected');
    vcIncognito=want;
    $('incognitobtn').classList.toggle('incognito-on',vcIncognito);
  }catch(_){
    $('saystatus').textContent='incognito toggle failed — faces unchanged';
  }
};

let vcOpenai=false;
$('openaibtn').onclick=async()=>{
  vcOpenai=!vcOpenai;
  $('openaibtn').classList.toggle('openai-on',vcOpenai);
  try{await fetch('/openaimode',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({openai:vcOpenai})});}catch(_){}
};

let volTimer=null;
$('vol').oninput=()=>{
  $('volval').textContent=$('vol').value;
  clearTimeout(volTimer);
  volTimer=setTimeout(async()=>{
    try{await fetch('/volume',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({volume:+$('vol').value})});}catch(_){}
  },250);
};
(async()=>{ // initial volume from robot
  try{const d=await(await fetch('/volume')).json();
    if(d&&d.volume!=null){$('vol').value=d.volume;$('volval').textContent=d.volume;}
  }catch(_){}
})();

function renderConvo(items){
  if(items.length===lastLen)return;
  lastLen=items.length;
  const c=$('convo');
  c.innerHTML='';
  if(!items.length){c.innerHTML='<div class=convo-empty>Say something — the conversation shows up here.</div>';return;}
  for(const m of items){
    const d=document.createElement('div');
    d.className='msg '+(m.who==='you'?'you':'wonder');
    d.innerHTML='<span class=who>'+(m.who==='you'?'You':'Vibey')+'</span>';
    d.appendChild(document.createTextNode(m.text));
    c.appendChild(d);
  }
  c.scrollTop=c.scrollHeight;
}

async function chatTick(){
  try{
    const d=await(await fetch('/chatstate')).json();
    const s=$('vcstatus');
    if(d.mode==='offline'){s.textContent='voice chat offline — start reachy_chat.py';s.className='vc-chip';}
    else if(d.speaking){s.textContent='🔊 Vibey is speaking…';s.className='vc-chip talk';}
    else if(d.muted){s.textContent='muted';s.className='vc-chip';}
    else if(d.openai){s.textContent='👂 🅾️ Realtime (OpenAI full-duplex — just talk, interrupt anytime)';s.className='vc-chip live';}
    else if(d.vibe){s.textContent='👂 listening · 🎮 Vibe (OpenClaw — self-improving)';s.className='vc-chip live';}
    else if(d.fast){s.textContent='👂 listening · ⚡ fast mode (ElevenLabs agent)';s.className='vc-chip live';}
    else{s.textContent='👂 listening · brain: '+d.mode+' ('+(d.model||'').replace('claude-','')+')';s.className='vc-chip live';}
    if(d.muted!==undefined&&d.muted!==vcMuted){
      vcMuted=d.muted;
      $('mutebtn').classList.toggle('muted',vcMuted);
      $('mutebtn').textContent=vcMuted?'🔇':'🎙️';
    }
    if(d.fast!==undefined&&d.fast!==vcFast){
      vcFast=d.fast;
      $('fastbtn').classList.toggle('fast-on',vcFast);
    }
    if(d.vibe!==undefined&&d.vibe!==vcVibe){
      vcVibe=d.vibe;
      $('vibebtn').classList.toggle('vibe-on',vcVibe);
    }
    if(d.openai!==undefined&&d.openai!==vcOpenai){
      vcOpenai=d.openai;
      $('openaibtn').classList.toggle('openai-on',vcOpenai);
    }
    if(d.incognito!==undefined&&d.incognito!==vcIncognito){
      vcIncognito=d.incognito;
      $('incognitobtn').classList.toggle('incognito-on',vcIncognito);
    }
    // The off state survives restarts and can be set from `vibey off`, so the
    // bar follows the service rather than only this tab's last click.
    if(d.off!==undefined&&d.off!==vcOff) paintOff(d.off);
    paintSwitches(d);
    $('openaibtn').disabled = d.openai_available===false;
    $('openaibtn').title = d.openai_available===false
      ? 'Realtime unavailable — OPENAI_API_KEY not configured in .env'
      : 'Realtime mode: OpenAI full-duplex voice — just talk, interrupt anytime';
    $('vibebtn').disabled = d.vibe_available===false;
    // mic meter: green fill vs the amber speech-threshold notch
    if(d.mic_level!==undefined){
      const scale=(d.mic_threshold||0.008)*3;   // notch lands at ~1/3 of the bar
      $('miclevel').style.width=Math.min(100,(d.mic_level/scale)*100)+'%';
      $('micnotch').style.left=Math.min(96,((d.mic_threshold||0.008)/scale)*100)+'%';
    }
    // Recording or not, and which kind of not — a flat meter looks the same
    // whether the ears are off, Vibey is talking, or the room is just quiet.
    if(d.mic_mode!==undefined){
      const short={recording:'● REC',speaking:'my turn',muted:'muted',
                   off:'ears off',idle:'not rec'};
      const el=$('micmode');
      el.textContent=short[d.mic_mode]||d.mic_mode;
      el.classList.toggle('rec',d.mic_mode==='recording');
      el.title=(d.mic_label||d.mic_mode)+' — noise profile: '+(d.noise_profile||'on');
    }
    $('fastbtn').disabled = d.fast_available===false;
    $('fastbtn').title = d.fast_available===false
      ? 'Fast mode unavailable — ELEVEN_AGENT_ID not configured'
      : 'Fast mode: ElevenLabs agent, skips Claude';
    renderConvo(d.transcript||[]);
  }catch(_){}
}
setInterval(chatTick,800);chatTick();

async function sendText(text){
  text=(text||'').trim(); if(!text)return;
  // "say: something" speaks the text verbatim; anything else is a chat
  // message routed through whichever brain is active (Vibey/fast/OpenClaw).
  const verbatim=text.toLowerCase().startsWith('say:');
  try{
    if(verbatim){
      $('saystatus').textContent='speaking…';
      const r=await fetch('/say',{method:'POST',headers:{'Content-Type':'application/json'},
                                  body:JSON.stringify({text:text.slice(4).trim()})});
      $('saystatus').textContent=r.ok?'🔊 said it':'error — check viewer logs';
    }else{
      $('saystatus').textContent='💬 thinking… (reply appears above and out loud)';
      const r=await fetch('/chatmsg',{method:'POST',headers:{'Content-Type':'application/json'},
                                      body:JSON.stringify({text})});
      if(!r.ok)$('saystatus').textContent='error — is the chat service running?';
      else setTimeout(()=>{if($('saystatus').textContent.startsWith('💬'))$('saystatus').textContent='';},60000);
    }
  }catch(e){$('saystatus').textContent='error: '+e;}
}
$('saybtn').onclick=()=>{sendText($('saytext').value);$('saytext').value='';};
$('saytext').addEventListener('keydown',e=>{
  if(e.key==='Enter'){sendText($('saytext').value);$('saytext').value='';}});

// ---- known-faces gallery ----
let galleryLen=-1;
async function fetchGallery(){
  try{
    const people=await(await fetch('/peoplelist')).json();
    if(!Array.isArray(people))return;
    $('peoplecount').textContent=people.length?('· '+people.length):'';
    if(people.length===galleryLen)return;  // cheap no-op guard
    // Never rebuild the gallery out from under someone actively renaming a
    // card — that was the "glitchy while typing" bug (a poll landing
    // mid-edit used to wipe the input and the browser's own datalist
    // popup along with it).
    if(document.activeElement&&document.activeElement.closest('#gallery'))return;
    galleryLen=people.length;
    const g=$('gallery');
    if(!people.length){
      g.innerHTML='<div class=gallery-empty>Nobody learned yet — stand in front of Vibey and teach it a name above.</div>';
      return;
    }
    g.innerHTML='';
    for(const p of people){
      const el=document.createElement('div');
      el.className='person'+(p.name?' named':'');

      const thumbWrap=document.createElement('div');
      thumbWrap.style.cssText='position:relative';
      const img=document.createElement('img');
      img.className='thumb';
      img.src=p.snapshot||'';
      img.alt=p.name||'unnamed';
      img.style.cursor='pointer';
      img.title='See all photos of '+(p.name||'this person');
      img.onclick=()=>openPhotoModal(p);
      const delBtn=document.createElement('button');
      delBtn.textContent='×';
      delBtn.title='Forget '+(p.name||'this person');
      delBtn.className='person-del';
      delBtn.onclick=async()=>{
        if(!confirm(`Forget ${p.name||'this unnamed person'}? This deletes all ${p.sample_count} learned photo(s).`))return;
        delBtn.disabled=true;
        try{
          await fetch('/deleteface',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({face_id:p.id})});
        }catch(_){}
        galleryLen=-1; fetchGallery();
      };
      thumbWrap.appendChild(img); thumbWrap.appendChild(delBtn);

      const nameWrap=document.createElement('div');
      nameWrap.className='pname-wrap';
      nameWrap.appendChild(nameCombo(p.name,async v=>{
        try{
          await fetch('/nameface',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({name:v,face_id:p.id})});
        }catch(_){}
        galleryLen=-1; fetchGallery(); fetchKnownNames();
      },'name…').el);

      const meta=document.createElement('div');
      meta.className='pmeta';
      meta.textContent=`seen ${p.times_seen}× · ${p.sample_count} photo${p.sample_count===1?'':'s'}`;

      el.appendChild(thumbWrap);
      // photo clump: the person's other learned angles, fanned under the main shot
      const extras=(p.photos||[]).slice(1);
      if(extras.length){
        const stack=document.createElement('div');
        stack.className='pstack';
        stack.style.cursor='pointer';
        stack.onclick=()=>openPhotoModal(p);
        for(const uri of extras){
          const s=document.createElement('img'); s.src=uri; stack.appendChild(s);
        }
        if(p.sample_count>3){
          const more=document.createElement('span');
          more.className='pstack-more'; more.textContent='+'+(p.sample_count-3);
          stack.appendChild(more);
        }
        el.appendChild(stack);
      }
      el.appendChild(nameWrap); el.appendChild(meta);
      g.appendChild(el);
    }
  }catch(_){}
}

// ---- capture panel ----
async function refreshCaptures(){
  try{
    const files=await(await fetch('/captures')).json();
    const g=$('capgrid'); g.innerHTML='';
    for(const f of files.slice(0,12)){
      const a=document.createElement('a');
      a.href='/captures/'+f.name; a.target='_blank';
      if(f.name.endsWith('.mp4')){
        const v=document.createElement('video'); v.src='/captures/'+f.name; v.muted=true;
        v.onmouseover=()=>v.play(); v.onmouseout=()=>v.pause();
        a.appendChild(v);
      }else{
        const im=document.createElement('img'); im.src='/captures/'+f.name;
        a.appendChild(im);
      }
      const tag=document.createElement('span');
      tag.className='cap-tag'; tag.textContent=f.name.endsWith('.mp4')?'clip':'photo';
      a.appendChild(tag);
      g.appendChild(a);
    }
  }catch(_){}
}
$('snapbtn').onclick=async()=>{
  $('capstatus').textContent='snapping…';
  try{
    const r=await(await fetch('/capture',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({type:'photo'})})).json();
    $('capstatus').textContent=r.ok?'saved '+r.name:'failed';
  }catch(e){$('capstatus').textContent='error';}
  refreshCaptures();
};
$('clipbtn').onclick=async()=>{
  $('clipbtn').disabled=true;
  $('capstatus').textContent='recording 10s…';
  try{
    const r=await(await fetch('/capture',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({type:'video',seconds:10})})).json();
    $('capstatus').textContent=r.ok?'saved '+r.name:'failed';
  }catch(e){$('capstatus').textContent='error';}
  $('clipbtn').disabled=false;
  refreshCaptures();
};
setInterval(refreshCaptures,30000);refreshCaptures();

// ---- wake-up show ----
$('alarmbtn').onclick=async()=>{
  $('alarmbtn').disabled=true;$('alarmbtn').style.opacity=.4;
  try{await fetch('/alarmnow',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}catch(_){}
  setTimeout(()=>{$('alarmbtn').disabled=false;$('alarmbtn').style.opacity=1;},45000);
};

// ---- reboot ----
$('rebootbtn').onclick=async()=>{
  if(!confirm('Reboot the robot? Takes ~30 seconds; it will wake up when done.'))return;
  $('rebootbtn').disabled=true;
  $('rebootbtn').style.opacity=.4;
  try{await fetch('/reboot',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}catch(_){}
  setTimeout(()=>{$('rebootbtn').disabled=false;$('rebootbtn').style.opacity=1;},45000);
};

// ---- power (sleep/wake) ----
let asleep=false;
$('powerbtn').onclick=async()=>{
  asleep=!$('powerbtn').classList.contains('off');
  $('powerbtn').classList.toggle('off',asleep);
  try{await fetch('/power',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({off:asleep})});}catch(_){}
};

// ---- emote buttons ----
const EMOTES={happy:'😊',excited:'⚡',curious:'🤔',sad:'😢',smug:'😏',thinking:'💭',victory:'🏆'};
for(const [name,icon] of Object.entries(EMOTES)){
  const b=document.createElement('button');
  b.className='icon-btn'; b.textContent=icon; b.title=name;
  b.onclick=()=>fetch('/emote',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name})}).catch(()=>{});
  $('emotes').appendChild(b);
}

// ---- soundboard ----
async function loadSfx(){
  try{
    const sounds=await(await fetch('/sfx')).json();
    const box=$('sfxbar');
    box.innerHTML='';
    for(const s of sounds){
      const b=document.createElement('button');
      b.className='sfx-btn';
      b.title=s.label;
      b.innerHTML='<span class=sfx-label></span><span class=sfx-icon></span>';
      b.querySelector('.sfx-label').textContent=s.label;
      b.querySelector('.sfx-icon').textContent=s.icon||'SFX';
      b.onclick=async()=>{
        b.classList.add('playing');
        $('sfxstatus').textContent=s.label;
        try{
          const r=await fetch('/sfx',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({name:s.name})});
          if(!r.ok)$('sfxstatus').textContent='sound failed';
        }catch(_){$('sfxstatus').textContent='sound failed';}
        setTimeout(()=>b.classList.remove('playing'),450);
      };
      box.appendChild(b);
    }
  }catch(_){$('sfxstatus').textContent='soundboard offline';}
}
loadSfx();

connectHandsfree();
setInterval(tick,250);tick();
setInterval(fetchGallery,5000);fetchGallery();
setInterval(fetchKnownNames,5000);fetchKnownNames();

// ---- per-person photo manager ----
$('pm-close').onclick=()=>$('photomodal').classList.remove('open');
$('photomodal').addEventListener('click',e=>{
  if(e.target.id==='photomodal')$('photomodal').classList.remove('open');
});

async function openPhotoModal(p){
  $('pm-title').textContent=p.name||'Unnamed person';
  $('pm-sub').textContent='seen '+p.times_seen+'×';
  $('pm-hint').textContent='';
  $('pm-grid').innerHTML='<div class=sub>loading…</div>';
  $('photomodal').classList.add('open');
  await renderPhotoModal(p);
}

async function renderPhotoModal(p){
  let samples=[];
  try{samples=await(await fetch('/personsamples?face_id='+p.id)).json();}catch(_){}
  if(!Array.isArray(samples))samples=[];
  const g=$('pm-grid');
  g.innerHTML='';
  for(const s of samples){
    const cell=document.createElement('div');
    cell.className='pm-cell';
    const img=document.createElement('img');
    img.src=s.snapshot||'';
    const del=document.createElement('button');
    del.className='pm-del'; del.textContent='×';
    del.title='Forget this photo';
    del.disabled=samples.length<=1;
    del.onclick=async()=>{
      del.disabled=true;
      try{
        const r=await fetch('/deletesample',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({sample_id:s.id})});
        const out=await r.json();
        if(out&&out.error)$('pm-hint').textContent=out.error;
      }catch(_){}
      await renderPhotoModal(p);
      galleryLen=-1; fetchGallery();
    };
    const when=document.createElement('div');
    when.className='when';
    when.textContent=(s.created_at||'').slice(0,10);
    cell.appendChild(img); cell.appendChild(del);
    g.appendChild(cell);
    cell.appendChild(when);
  }
  $('pm-hint').textContent = samples.length<=1
    ? 'Last photo — deleting it would make this person unrecognizable; use the ⊗ on their card to forget them entirely.'
    : samples.length+' photos — × forgets just that one.';
}

// ---- Vibey's head: OpenClaw thought stream ----
const TAGS={thinking:'think',tool:'tool',result:'result',say:'say',user:'heard',error:'error'};
let brainLen=-1;
async function fetchBrain(){
  try{
    const d=await(await fetch('/vibelog')).json();
    const ev=d.events||[];
    if(ev.length===brainLen)return;
    brainLen=ev.length;
    const b=$('brainlog');
    const stick=b.scrollTop+b.clientHeight>=b.scrollHeight-40;
    b.innerHTML='';
    if(!ev.length){
      b.innerHTML='<div class=brainlog-empty>Turn on 🎮 Vibe mode and talk to it — its thought process streams here.</div>';
      return;
    }
    for(const e of ev){
      const row=document.createElement('div');
      row.className='bl '+e.kind;
      const tag=document.createElement('span');
      tag.className='tag'; tag.textContent=TAGS[e.kind]||e.kind;
      const tx=document.createElement('span');
      tx.className='tx'; tx.textContent=e.text;
      row.appendChild(tag); row.appendChild(tx);
      b.appendChild(row);
    }
    if(stick)b.scrollTop=b.scrollHeight;
  }catch(_){}
}
setInterval(fetchBrain,2500);fetchBrain();

// ---- VibeVerse lobby panel ----
let verseLen=-1;
async function fetchVerse(){
  try{
    const d=await(await fetch('/verselog')).json();
    $('versewho').textContent=(d.agents&&d.agents.length)?d.agents.join(', '):'nobody else right now';
    const ev=d.events||[];
    if(ev.length===verseLen)return;
    verseLen=ev.length;
    const b=$('verselog');
    const stick=b.scrollTop+b.clientHeight>=b.scrollHeight-40;
    b.innerHTML='';
    if(!ev.length){b.innerHTML='<div class=brainlog-empty>lobby is quiet…</div>';return;}
    for(const e of ev.slice(-30)){
      const row=document.createElement('div');
      row.className='bl '+(e.kind==='mention'?'user':e.kind==='say'?'say':'result');
      const tag=document.createElement('span');tag.className='tag';tag.textContent=e.kind;
      const tx=document.createElement('span');tx.className='tx';tx.textContent=e.text;
      row.appendChild(tag);row.appendChild(tx);b.appendChild(row);
    }
    if(stick)b.scrollTop=b.scrollHeight;
  }catch(_){}
}
setInterval(fetchVerse,5000);fetchVerse();

// ---- alarm editor ----
let ALARMS=[];
async function fetchAlarms(){
  try{
    ALARMS=await(await fetch('/alarms')).json();
    const box=$('alarmlist'); box.innerHTML='';
    if(!ALARMS.length){box.innerHTML='<div class=sub style=margin:0>no alarms set</div>';}
    ALARMS.forEach((a,i)=>{
      const row=document.createElement('div');
      row.style.cssText='display:flex;align-items:center;gap:8px';
      row.innerHTML='<b class=mono>'+a.time+'</b><span class=sub style=margin:0>'+(a.repeat||'once')+'</span><span style=flex:1></span>';
      const del=document.createElement('button');
      del.className='person-del'; del.style.position='static'; del.textContent='×';
      del.onclick=async()=>{
        ALARMS.splice(i,1);
        await fetch('/setalarms',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(ALARMS)});
        fetchAlarms();
      };
      row.appendChild(del); box.appendChild(row);
    });
  }catch(_){}
}
$('alarmadd').onclick=async()=>{
  const t=$('alarmtime').value; if(!t)return;
  ALARMS.push({time:t,repeat:$('alarmrepeat').value,label:'dashboard alarm '+t,song:true});
  await fetch('/setalarms',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(ALARMS)});
  fetchAlarms();
};
setInterval(fetchAlarms,20000);fetchAlarms();

/* ---- Modes -------------------------------------------------------------
   Everything here reads back off the live system rather than off whatever was
   last clicked. Clicking a mode flips switches; it does not become the truth,
   and if the robot is unplugged the Pi tier goes dark no matter which button
   is lit. That gap is the whole point of the panel. */
let modeBusy=false;
const TIER_ORDER=['pi','mac','cloud'];
const TIER_NAME={pi:'On the Pi',mac:'On the Mac',cloud:'In the cloud'};
const PORT_TXT={native:'already on the Pi',yes:'could move to the Pi',
  partial:'could partly move',no:'cannot move',
  'n/a':'disappears on the Pi'};

async function setMode(m){
  if(modeBusy) return; modeBusy=true;
  document.querySelectorAll('.mode-btn').forEach(b=>b.disabled=true);
  try{
    const r=await fetch('/setmode',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})});
    renderModes(await r.json());
  }catch(_){}
  modeBusy=false;
  document.querySelectorAll('.mode-btn').forEach(b=>b.disabled=false);
}

function renderModes(d){
  if(!d||!d.modes) return;
  const row=document.getElementById('moderow');
  row.innerHTML='';
  for(const key of TIER_ORDER.map((_,i)=>['pi','mac','cloud'][i])){
    const m=d.modes[key]; if(!m) continue;
    const b=document.createElement('button');
    b.type='button';
    b.className='mode-btn'+(d.current===key?' on':'');
    b.innerHTML='<span class=mb-fill style="width:'+m.pct+'%"></span>'
      +'<span class=mb-name>'+m.label+'</span>'
      +'<span class=mb-sub>'+m.sub+'</span>'
      +'<span class=mb-pct>'+m.count+' of '+d.total+' capabilities · '+m.pct+'%</span>';
    b.onclick=()=>setMode(key);
    row.appendChild(b);
  }
  const cur=d.modes[d.current];
  document.getElementById('modesub').textContent=
    d.live+' of '+d.total+' up right now'+(d.reachable?'':' · robot unreachable');

  const wrap=document.getElementById('modecaps');
  wrap.innerHTML='';
  for(const t of TIER_ORDER){
    const caps=d.caps.filter(c=>c.tier===t);
    if(!caps.length) continue;
    const sec=document.createElement('div');
    sec.className='mode-tier';
    // A tier outside the current mode is dimmed rather than hidden: the
    // question "what would I get by going up a level" needs the answer to
    // stay on screen.
    const inMode=cur&&cur.tiers.includes(t);
    sec.innerHTML='<h3>'+TIER_NAME[t]+(inMode?'':' · not in this mode')+'</h3>';
    for(const c of caps){
      const el=document.createElement('div');
      el.className='mode-cap'+(c.live?' live':'')+(inMode?'':' dim');
      el.innerHTML='<span class=mc-dot></span><span class=mc-txt>'
        +'<b>'+c.label+'</b><span class=mc-note>'+c.note+'</span></span>'
        +'<span class=mc-port data-p="'+c.portable+'">'
        +(PORT_TXT[c.portable]||c.portable)+'</span>';
      sec.appendChild(el);
    }
    wrap.appendChild(sec);
  }
}

async function fetchModes(){
  if(modeBusy) return;
  try{ renderModes(await(await fetch('/modes')).json()); }catch(_){}
}
setInterval(fetchModes,6000);fetchModes();


// --- connection panel: find the robot, repoint the stack, move networks ----
// Everything here talks to the robot's own daemon API (proxied through this
// server), which is all the Reachy desktop app was ever doing.
(function(){
  const $$ = id => document.getElementById(id);
  const chip = (t, cls) => { const c=$$('connstatus'); c.textContent=t;
                             c.className = 'vc-chip' + (cls ? ' '+cls : ''); };

  function card(r, active){
    const el = document.createElement('div');
    el.className = 'robot-card' + (active ? ' active' : '');
    const addrs = (r.addresses||[]).join(' · ');
    el.innerHTML =
      '<span class=dot style="--c:var(--'+(r.state==='running'?'good':'warn')+')"></span>'
      + '<span class=rc-main><span class=rc-name>'+(r.name||'reachy')+'</span>'
      + '<div class=rc-meta>'+addrs+'  ·  '+(r.network||'?')+'  ·  '+(r.hardware_id||'')+'</div></span>';
    const btn = document.createElement('button');
    btn.className = active ? 'cap-btn' : 'btn-accent';
    btn.style.padding = '8px 14px';
    btn.textContent = active ? 'Connected' : 'Use this';
    btn.disabled = !!active;
    btn.onclick = async () => {
      // Persist + repoint. The other services read REACHY_URL once at
      // startup, so they need the restart to follow it.
      btn.disabled = true; chip('connecting…','talk');
      const res = await (await fetch('/robot/connect', {method:'POST',
        body: JSON.stringify({url: r.url, restart: true})})).json();
      if (res.error) { chip('failed: '+res.error,'') ; btn.disabled=false; return; }
      chip('restarting the stack — this page will reload','talk');
      setTimeout(()=>location.reload(), 22000);
    };
    el.appendChild(btn);
    return el;
  }

  async function find(deep){
    chip(deep ? 'sweeping the network…' : 'looking…','talk');
    try{
      const r = await (await fetch('/robot/discover?scan='+(deep?'1':'0'))).json();
      const list = $$('robotlist'); list.innerHTML='';
      (r.robots||[]).forEach(rb =>
        list.appendChild(card(rb, (rb.addresses||[]).some(a => (r.active||'').includes(a)))));
      $$('connsub').textContent = r.env_url ? '.env → '+r.env_url : '';
      if (!(r.robots||[]).length){
        list.innerHTML = '<div class=gallery-empty>No robot answered on this network.</div>';
        chip(deep ? 'nothing found' : 'nothing found — try Deep scan','');
      } else {
        chip((r.robots.length)+' found','live');
      }
      (r.notes||[]).forEach(n => { const d=document.createElement('div');
        d.className='sub'; d.style.margin='6px 0 0'; d.textContent=n; $$('robotlist').appendChild(d); });
    }catch(e){ chip('discovery failed','') }
  }

  $$('findbtn').onclick = () => find(false);
  $$('scanbtn').onclick = () => find(true);

  $$('wifibtn').onclick = async () => {
    const wrap=$$('wifiwrap');
    if (wrap.style.display === 'block'){ wrap.style.display='none'; return; }
    wrap.style.display='block';
    $$('wifimsg').textContent='scanning…';
    const st = await (await fetch('/robot/wifi')).json();
    $$('wifinow').textContent = st.connected_network
      ? 'on "'+st.connected_network+'"' : (st.error||'');
    const known = new Set(st.known_networks||[]);
    const res = await (await fetch('/robot/wifi/scan',{method:'POST'})).json();
    const list=$$('netlist'); list.innerHTML='';
    (res.networks||[]).forEach(ssid => {
      const d=document.createElement('div');
      d.className='net' + (ssid===st.connected_network ? ' on' : '');
      d.innerHTML = '<span>'+ssid+'</span>'
        + '<span class=lock>'+(ssid===st.connected_network ? 'connected'
            : known.has(ssid) ? 'saved' : '')+'</span>';
      d.onclick = () => { $$('wifissid').value = ssid; $$('wifipass').focus(); };
      list.appendChild(d);
    });
    $$('wifimsg').textContent = (res.networks||[]).length
      ? 'Pick a network, then enter its password. Note: scanning makes the robot '
        + 'sweep every channel, so it can drop off for a few seconds — use Find robot if it does.'
      : (res.error||'no networks seen');
  };

  $$('wifijoin').onclick = async () => {
    const ssid=$$('wifissid').value.trim(), pw=$$('wifipass').value;
    if(!ssid){ $$('wifimsg').textContent='Enter a network name.'; return; }
    $$('wifimsg').textContent='joining "'+ssid+'" — the robot may drop off for a moment…';
    const r = await (await fetch('/robot/wifi/join',{method:'POST',
      body: JSON.stringify({ssid, password: pw})})).json();
    $$('wifipass').value='';
    $$('wifimsg').textContent = r.error
      ? 'failed: '+r.error
      : 'sent. If it moved to a different network, hit Find robot to pick up its new address.';
  };

  find(false);   // quick look on load
})();

// --- mic check: record from THIS browser's mic, see it, hear it back -------
// Entirely client-side. Nothing is uploaded — the point is to judge a mic
// before trusting what Vibey transcribes from it.
(function(){
  const $ = id => document.getElementById(id);
  const MAX_MS = 15000;                       // short by design; this is a test
  const rec=$('mcrec'), play=$('mcplay'), dl=$('mcdl'), clr=$('mcclear');
  const status=$('mcstatus'), level=$('mclevel'), peakEl=$('mcpeak');
  const clock=$('mcclock'), audio=$('mcaudio'), cv=$('mcwave');
  if(!rec || !cv) return;
  const ctx2d = cv.getContext('2d');

  let stream=null, mr=null, ac=null, analyser=null, chunks=[], url=null;
  let raf=0, t0=0, stopTimer=0, peak=0;
  const trail = [];                           // recent levels -> waveform

  function paintWave(){
    const w=cv.width, h=cv.height, mid=h/2;
    ctx2d.clearRect(0,0,w,h);
    ctx2d.strokeStyle = themeColor('--line');
    ctx2d.beginPath(); ctx2d.moveTo(0,mid); ctx2d.lineTo(w,mid); ctx2d.stroke();
    if(!trail.length) return;
    ctx2d.fillStyle = themeColor('--accent');
    const step = w / 180, bar = Math.max(1, step-1);
    trail.forEach((v,i) => {
      const hh = Math.max(1, v*(h-8));
      ctx2d.fillRect(i*step, mid-hh/2, bar, hh);
    });
  }

  function tick(){
    if(!analyser) return;
    const buf = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(buf);
    let sum=0, pk=0;
    for(let i=0;i<buf.length;i++){ sum+=buf[i]*buf[i]; pk=Math.max(pk,Math.abs(buf[i])); }
    const rms = Math.sqrt(sum/buf.length);
    peak = Math.max(peak, pk);
    level.style.width = Math.min(100, rms*320) + '%';
    level.style.background = themeColor(pk>0.98 ? '--bad' : rms>0.02 ? '--good' : '--warn');
    peakEl.textContent = (20*Math.log10(Math.max(pk,1e-5))).toFixed(0)+' dB';
    trail.push(Math.min(1, rms*4));
    if(trail.length>180) trail.shift();
    paintWave();
    clock.textContent = ((performance.now()-t0)/1000).toFixed(1)+'s';
    raf = requestAnimationFrame(tick);
  }

  async function start(){
    if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
      status.textContent = 'this browser has no mic access (needs https or localhost)';
      return;
    }
    try{
      stream = await navigator.mediaDevices.getUserMedia({audio:true});
    }catch(e){
      status.textContent = e && e.name === 'NotAllowedError'
        ? 'mic permission denied — allow it in the browser address bar, then retry'
        : 'no mic available: ' + ((e&&e.name)||e);
      return;
    }
    ac = new (window.AudioContext||window.webkitAudioContext)();
    analyser = ac.createAnalyser(); analyser.fftSize = 1024;
    ac.createMediaStreamSource(stream).connect(analyser);
    chunks=[]; trail.length=0; peak=0;
    mr = new MediaRecorder(stream);
    mr.ondataavailable = e => { if(e.data && e.data.size) chunks.push(e.data); };
    mr.onstop = finish;
    mr.start();
    t0 = performance.now();
    stopTimer = setTimeout(stop, MAX_MS);
    raf = requestAnimationFrame(tick);
    rec.textContent='⏹ Stop'; rec.classList.add('on');
    play.disabled = dl.disabled = clr.disabled = true;
    status.textContent = 'recording — up to '+(MAX_MS/1000)+'s';
  }

  function stop(){ if(mr && mr.state !== 'inactive') mr.stop(); }

  function finish(){
    clearTimeout(stopTimer); cancelAnimationFrame(raf); raf=0;
    if(stream){ stream.getTracks().forEach(t => t.stop()); stream=null; }
    if(ac){ ac.close(); ac=null; } analyser=null;
    level.style.width='0%';
    rec.textContent='⏺ Record'; rec.classList.remove('on');
    if(!chunks.length){ status.textContent='nothing recorded'; return; }
    if(url) URL.revokeObjectURL(url);
    const blob = new Blob(chunks, {type: (mr && mr.mimeType) || 'audio/webm'});
    url = URL.createObjectURL(blob);
    audio.src = url;
    play.disabled = dl.disabled = clr.disabled = false;
    const dB = 20*Math.log10(Math.max(peak,1e-5));
    status.textContent = clock.textContent + ' recorded · peak ' + dB.toFixed(0) + ' dB'
      + (dB > -1.5 ? ' — clipping, move back' : dB < -30 ? ' — very quiet, move closer' : ' — looks healthy');
  }

  rec.onclick = () => (mr && mr.state === 'recording') ? stop() : start();
  play.onclick = () => { audio.currentTime = 0; audio.play(); };
  dl.onclick = () => {
    if(!url) return;
    const a = document.createElement('a');
    a.href = url; a.download = 'mic-check.webm'; a.click();
  };
  clr.onclick = () => {
    if(url){ URL.revokeObjectURL(url); url=null; }
    audio.pause(); audio.removeAttribute('src');
    chunks=[]; trail.length=0; peak=0; paintWave();
    play.disabled = dl.disabled = clr.disabled = true;
    clock.textContent='0.0s'; peakEl.textContent='–'; status.textContent='cleared';
  };
  paintWave();
})();

// --- DJ panel: talks straight to reachy_dj.py on :8778 -----------------------
(function(){
  const DJ='http://localhost:8778';
  const $=id=>document.getElementById(id);
  const post=(p,b)=>fetch(DJ+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json()).catch(()=>null);
  let st=null, lastBeat=0, sliding=false;
  async function tracks(){
    try{ const t=await(await fetch(DJ+'/tracks')).json();
      const sel=$('djtrack'); const cur=sel.value;
      sel.innerHTML=(t.tracks||[]).map(n=>`<option>${n}</option>`).join('')||'<option>(drop files in ~/Music/vibey)</option>';
      if(cur) sel.value=cur;
    }catch(_){}
  }
  async function poll(){
    try{ st=await(await fetch(DJ+'/status')).json(); }catch(_){ st=null; }
    if(!st){ $('djtitle').textContent='dj service offline'; return; }
    $('djtitle').textContent=st.track||'nothing loaded';
    $('djbpm').textContent=st.bpm?`${st.target_bpm} BPM`+(st.rate!==1?` (track ${st.bpm})`:''):'';
    $('djpos').textContent=st.track?`${st.position}s / ${st.duration}s`:'';
    $('djplay').textContent=st.playing?'❚❚ Pause':'▶ Play';
    if(!sliding) $('djslider').value=Math.round((st.rate||1)*100);
  }
  // Beat pulse: derived from position and BPM, so it lines up with the audio
  // without the page needing the beat grid.
  function pulse(){
    if(st&&st.playing&&st.target_bpm){
      const spb=60/st.target_bpm; const now=performance.now()/1000;
      if(now-lastBeat>=spb){ lastBeat=now; const d=$('djdot');
        d.style.background='#9f6'; d.style.transform='scale(1.6)';
        setTimeout(()=>{d.style.background='#333';d.style.transform='scale(1)';},90); }
    }
    requestAnimationFrame(pulse);
  }
  $('djplay').onclick=async()=>{ if(st&&st.playing) await post('/pause'); else await post('/play',{track:$('djtrack').value}); poll(); };
  $('djstop').onclick=async()=>{ await post('/stop'); poll(); };
  $('djup').onclick=async()=>{ await post('/nudge',{percent:4}); poll(); };
  $('djdown').onclick=async()=>{ await post('/nudge',{percent:-4}); poll(); };
  $('djslider').oninput=()=>{ sliding=true; };
  $('djslider').onchange=async e=>{ sliding=false; if(st&&st.bpm) await post('/tempo',{bpm:st.bpm*e.target.value/100}); poll(); };
  $('djtrack').onchange=async e=>{ await post('/load',{track:e.target.value}); poll(); };
  tracks(); poll(); setInterval(poll,700); setInterval(tracks,10000); pulse();
})();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/perception"):
            self._send(json.dumps(gather()).encode(), "application/json")
        elif self.path.startswith("/modes"):
            self._send(json.dumps(reachy_modes.status()).encode(),
                       "application/json")
        elif self.path.startswith("/chatstate"):
            self._send(json.dumps(
                _get(f"{CHAT_URL}/state") or {"mode": "offline"}
            ).encode(), "application/json")
        elif self.path.startswith("/volume"):
            self._send(json.dumps(
                _get(f"{REACHY_URL}/api/volume/current") or {}
            ).encode(), "application/json")
        elif self.path.startswith("/sfx"):
            from reachy_sfx import catalog
            self._send(json.dumps(catalog()).encode(), "application/json")
        elif self.path.startswith("/peoplelist"):
            self._send(json.dumps(
                _get(f"{MEM_URL}/people", timeout=8.0) or []
            ).encode(), "application/json")
        elif self.path.startswith("/knownnames"):
            self._send(json.dumps(
                _get(f"{MEM_URL}/names", timeout=8.0) or []
            ).encode(), "application/json")
        elif self.path == "/alarms":
            try:
                alarms = json.loads(open(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "alarms.json")).read())
            except Exception:
                alarms = []
            self._send(json.dumps(alarms).encode(), "application/json")
        elif self.path.startswith("/verselog"):
            self._send(json.dumps(
                _get("http://localhost:8774/status", timeout=4.0) or {}
            ).encode(), "application/json")
        elif self.path.startswith("/vibelog"):
            self._send(json.dumps(
                _get(f"{CHAT_URL}/vibelog", timeout=6.0) or {"events": []}
            ).encode(), "application/json")
        elif self.path.startswith("/robot/discover"):
            # ?scan=0 skips the 254-host sweep when you only want the quick
            # lookups (mDNS / .env / AP).
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[-1]
                                       if "?" in self.path else "")
            scan = qs.get("scan", ["1"])[0] not in ("0", "false")
            try:
                res = reachy_connect.discover(scan=scan)
            except Exception as e:  # noqa: BLE001
                res = {"robots": [], "notes": [f"discovery failed: {e}"]}
            res["active"] = REACHY_URL
            self._send(json.dumps(res).encode(), "application/json")
        elif self.path.startswith("/robot/wifi"):
            try:
                out = reachy_connect.wifi_status(REACHY_URL)
            except Exception as e:  # noqa: BLE001
                out = {"error": str(e)}
            self._send(json.dumps(out).encode(), "application/json")
        elif self.path == "/captures":
            # Only real media files. This used to list bare os.listdir(), which
            # swept up the timelapse_YYYYMMDD/ subdirectories too — every one
            # of them rendered as an <img> that 404'd on /captures/<dir>,
            # giving a grid of broken thumbnails.
            MEDIA = (".jpg", ".jpeg", ".png", ".mp4")
            names = sorted(os.listdir(CAPTURES_DIR), reverse=True)
            out = []
            for f in names:
                if f.startswith(".") or not f.lower().endswith(MEDIA):
                    continue
                p = os.path.join(CAPTURES_DIR, f)
                if not os.path.isfile(p):
                    continue
                out.append({"name": f, "size": os.path.getsize(p)})
                if len(out) >= 60:
                    break
            self._send(json.dumps(out).encode(), "application/json")
        elif self.path.split("?", 1)[0] == "/mimic/handsfree.mjpg":
            # Pipe handsfree's MJPEG through this origin.
            #
            # handsfree (browser_viewer.py, :8765) owns the MacBook camera from
            # login onward, so the mimic page cannot open the device itself. It
            # can read handsfree's stream instead — but only same-origin: an
            # <img> from :8765 taints the canvas it is drawn into, and
            # captureStream() on a tainted canvas throws. Proxying here avoids
            # touching handsfree at all, which is the point — it stays a
            # separate project that happens to own the camera.
            try:
                up = urllib.request.urlopen(HANDSFREE_STREAM, timeout=6)
            except Exception:
                self.send_response(502); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", up.headers.get(
                "Content-Type", "multipart/x-mixed-replace; boundary=handsfree-frame"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    chunk = up.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except Exception:
                pass  # browser navigated away; nothing to say about it
            finally:
                up.close()
        elif self.path.split("?", 1)[0] == "/mimic/robot.json":
            # The page is served from this host; the robot is on another. The
            # browser cannot guess the second from the first, and defaulting to
            # location.hostname silently pointed the whole app at localhost:8000.
            # Prefer REACHY_HOST: start_wonder.sh resolves the mDNS name to an
            # address once at boot and exports it. Handing the browser
            # "reachy-mini.local" instead makes it pay an mDNS lookup on every
            # request, and this app makes ~30 of them a second.
            host = os.environ.get("REACHY_HOST", "").strip()
            url = f"http://{host}:8000" if host else REACHY_URL
            self._send(json.dumps({"url": url}).encode(), "application/json")
        elif self.path.split("?", 1)[0] == "/mimic":
            # Redirect to the trailing slash, or every relative href in
            # index.html resolves against the site root: /style.css, /main.js.
            # The page then renders unstyled with no script, which looks exactly
            # like the app being broken rather than the URL being wrong.
            self.send_response(301)
            self.send_header("Location", "/mimic/")
            self.end_headers()
        elif self.path.startswith("/mimic"):
            # The mime_bot app (RemiFabre/mime_bot on Hugging Face), served from
            # here rather than from the Space so it is same-origin with the rest
            # of the dashboard and so its transport can be the local one.
            rel = self.path.split("?", 1)[0][len("/mimic"):].lstrip("/") or "index.html"
            path = os.path.normpath(os.path.join(MIMIC_DIR, rel))
            if not path.startswith(MIMIC_DIR) or not os.path.isfile(path):
                self.send_response(404); self.end_headers(); return
            ctype = {"html": "text/html", "js": "text/javascript",
                     "css": "text/css", "md": "text/plain"}.get(
                         path.rsplit(".", 1)[-1], "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            # No caching. These files are edited live, and a stale ES module is
            # invisible: the page loads, looks right, and silently runs last
            # week's logic. Cost of re-fetching 100KB over localhost is nil.
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.end_headers()
            with open(path, "rb") as f:
                self.wfile.write(f.read())
        elif self.path.startswith("/captures/"):
            name = os.path.basename(urllib.parse.unquote(self.path.split("/captures/", 1)[1]))
            path = os.path.join(CAPTURES_DIR, name)
            if not os.path.isfile(path):
                self.send_response(404); self.end_headers(); return
            ctype = "video/mp4" if name.endswith(".mp4") else "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.end_headers()
            with open(path, "rb") as f:
                self.wfile.write(f.read())
        elif self.path.startswith("/personsamples"):
            qs = self.path.split("?", 1)[-1] if "?" in self.path else ""
            self._send(json.dumps(
                _get(f"{MEM_URL}/samples?{qs}", timeout=10.0) or []
            ).encode(), "application/json")
        elif self.path.startswith("/about"):
            # Who is Vibey? Rendered from its own identity file — the robot
            # maintains IDENTITY.md itself, so this page is self-describing.
            try:
                md = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "IDENTITY.md")).read()
            except Exception:
                md = "# Vibey\n(identity file missing)"
            import html as _html
            body = _html.escape(md)
            page = ("<!doctype html><html><head><meta charset=utf-8>"
                    "<meta name=viewport content='width=device-width,initial-scale=1'>"
                    "<title>About Vibey</title><style>"
                    "body{background:#0a0b10;color:#e6e9f2;font:15px/1.6 "
                    "-apple-system,system-ui,sans-serif;max-width:680px;"
                    "margin:0 auto;padding:40px 20px}"
                    "pre{white-space:pre-wrap;font:inherit}"
                    "a{color:#5ac8fa}</style></head><body>"
                    "<p><a href='/'>← dashboard</a></p>"
                    f"<pre>{body}</pre>"
                    "<p style='color:#8b90a6;font-size:12px'>This page renders "
                    "IDENTITY.md — a file the robot writes itself.</p>"
                    "</body></html>")
            self._send(page.encode(), "text/html; charset=utf-8")
        elif self.path == "/" or self.path.startswith("/index"):
            html = (PAGE
                    .replace("%REACHY%", json.dumps(REACHY_URL))
                    .replace("%HANDSFREE%", json.dumps(HANDSFREE_URL))
                    .replace("%CAM%", json.dumps(CAM_URL)))
            self._send(html.encode(), "text/html; charset=utf-8")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/robot/connect"):
            # Point the whole stack at a robot: persist to .env (so the next
            # start sticks) and repoint this process immediately. The other
            # services latch REACHY_URL at startup, so they need a restart —
            # start_wonder.sh is the tested path for that and re-resolves
            # mDNS itself, so we just hand off to it.
            global REACHY_URL
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n)) if n else {}
                url = reachy_connect.set_env_url(str(body.get("url", "")))
                REACHY_URL = url
                restart = bool(body.get("restart"))
                self._send(json.dumps({"ok": True, "url": url,
                                       "restarting": restart}).encode(),
                           "application/json")
                if restart:
                    # Detached, and only AFTER responding: start_wonder.sh
                    # kills this very process on its way through.
                    here = os.path.dirname(os.path.abspath(__file__))
                    subprocess.Popen(["/bin/zsh", os.path.join(here, "start_wonder.sh")],
                                     cwd=here, start_new_session=True,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            except Exception as e:  # noqa: BLE001
                self._send(json.dumps({"error": str(e)}).encode(),
                           "application/json", code=400)
        elif self.path.startswith("/robot/wifi/scan"):
            try:
                out = {"networks": reachy_connect.wifi_scan(REACHY_URL)}
            except Exception as e:  # noqa: BLE001
                out = {"error": str(e)}
            self._send(json.dumps(out).encode(), "application/json")
        elif self.path.startswith("/robot/wifi/join"):
            # Password arrives in the body, never in our query string, so it
            # stays out of this server's URLs and logs.
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n)) if n else {}
                ssid = str(body.get("ssid", "")).strip()
                if not ssid:
                    raise ValueError("ssid required")
                out = reachy_connect.wifi_join(REACHY_URL, ssid,
                                               str(body.get("password", "")))
            except Exception as e:  # noqa: BLE001
                out = {"error": str(e)}
            self._send(json.dumps(out).encode(), "application/json")
        elif self.path.startswith("/setmode"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n)) if n else {}
                self._send(json.dumps(
                    reachy_modes.apply(body.get("mode", "mac"))).encode(),
                    "application/json")
            except Exception as e:  # noqa: BLE001
                self._send(json.dumps({"error": str(e)}).encode(),
                           "application/json", 400)
        elif self.path.startswith("/power"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                off = bool(body.get("off"))
                # goto_sleep/wake_up take seconds — run off-thread so the UI
                # gets an immediate response.
                threading.Thread(target=_set_power, args=(off,), daemon=True).start()
                self._send(json.dumps({"ok": True, "off": off}).encode(),
                           "application/json")
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.startswith("/setalarms"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                alarms = json.loads(self.rfile.read(n))
                assert isinstance(alarms, list)
                for a in alarms:
                    assert isinstance(a.get("time"), str) and ":" in a["time"]
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "alarms.json")
                with open(path, "w") as f:
                    json.dump(alarms, f, indent=2)
                self._send(json.dumps({"ok": True, "count": len(alarms)}).encode(),
                           "application/json")
            except Exception as e:
                self.send_response(400); self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.startswith("/timelapse"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n)) if n else {}
                name = _timelapse_assemble(body.get("day"))
                self._send(json.dumps({"ok": bool(name), "name": name}).encode(),
                           "application/json")
            except Exception as e:
                self.send_response(400); self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.startswith("/capture"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n)) if n else {}
                if body.get("type") == "video":
                    secs = min(30, max(2, float(body.get("seconds", 10))))
                    name = _capture_video(secs)
                else:
                    name = _capture_photo()
                self._send(json.dumps({"ok": bool(name), "name": name}).encode(),
                           "application/json")
            except Exception as e:
                self.send_response(400); self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.startswith("/alarmnow"):
            def _show():
                try:
                    from reachy_alarm import fire
                    fire({"label": "on-demand wake-up show", "song": True})
                except Exception as e:
                    print(f"[viewer] alarm show failed: {e}", flush=True)
            threading.Thread(target=_show, daemon=True).start()
            self._send(json.dumps({"ok": True}).encode(), "application/json")
            return
        if self.path.startswith("/reboot"):
            threading.Thread(target=_reboot_robot, daemon=True).start()
            self._send(json.dumps({"ok": True, "rebooting": True}).encode(),
                       "application/json")
            return
        if self.path.startswith("/emote"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                from reachy_emotes import play as play_emote
                ok = play_emote(body.get("name", ""), sound=True)
                self._send(json.dumps({"ok": ok}).encode(), "application/json")
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.startswith("/sfx"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                from reachy_sfx import play as play_sfx
                ok = play_sfx(body.get("name", ""))
                self._send(json.dumps({"ok": ok}).encode(), "application/json")
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if (self.path.startswith("/mute") or self.path.startswith("/volume")
                or self.path.startswith("/nameface") or self.path.startswith("/fastmode")
                or self.path.startswith("/vibemode") or self.path.startswith("/openaimode")
                or self.path.startswith("/incognito") or self.path.startswith("/off")
                or self.path.startswith("/switch")
                or self.path.startswith("/chatmsg")
                or self.path.startswith("/resay") or self.path.startswith("/deletesample")
                or self.path.startswith("/deleteface")):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                if self.path.startswith("/mute"):
                    out = _post(f"{CHAT_URL}/mute", {"muted": bool(body.get("muted"))})
                elif self.path.startswith("/fastmode"):
                    out = _post(f"{CHAT_URL}/fastmode", {"fast": bool(body.get("fast"))})
                elif self.path.startswith("/vibemode"):
                    out = _post(f"{CHAT_URL}/vibemode", {"vibe": bool(body.get("vibe"))})
                elif self.path.startswith("/openaimode"):
                    out = _post(f"{CHAT_URL}/openaimode", {"openai": bool(body.get("openai"))})
                elif self.path.startswith("/switch"):
                    out = _post(f"{CHAT_URL}/switch",
                                {"name": body.get("name"),
                                 "on": bool(body.get("on"))}, timeout=15.0)
                    # _post returns None for any failure, including a refusal
                    # (switching something on while Vibey is OFF). Raising here
                    # turns that into a non-200 the page can see, so the toggle
                    # springs back instead of showing a state that never took.
                    if out is None:
                        raise ValueError("switch refused — is Vibey off?")
                elif self.path.startswith("/off"):
                    out = _post(f"{CHAT_URL}/off", {"off": bool(body.get("off"))},
                                timeout=25.0)
                elif self.path.startswith("/incognito"):
                    # Via the chat service, not straight to the face service:
                    # it flips the brain's prompt and tools in the same call.
                    out = _post(f"{CHAT_URL}/incognito", {"on": bool(body.get("on"))},
                                timeout=10.0)
                elif self.path.startswith("/chatmsg"):
                    out = _post(f"{CHAT_URL}/message", {"text": body.get("text", "")},
                                timeout=10.0)
                elif self.path.startswith("/resay"):
                    out = _post(f"{CHAT_URL}/resay", {}, timeout=10.0)
                elif self.path.startswith("/nameface"):
                    out = _post(f"{MEM_URL}/name",
                                {"name": body.get("name", ""),
                                 "face_id": body.get("face_id")}, timeout=20.0)
                elif self.path.startswith("/deleteface"):
                    out = _post(f"{MEM_URL}/deleteface",
                                {"face_id": body.get("face_id")}, timeout=10.0)
                elif self.path.startswith("/deletesample"):
                    out = _post(f"{MEM_URL}/deletesample",
                                {"sample_id": body.get("sample_id")}, timeout=10.0)
                else:
                    vol = max(0, min(100, int(body.get("volume", 50))))
                    out = _post(f"{REACHY_URL}/api/volume/set", {"volume": vol})
                self._send(json.dumps(out or {}).encode(), "application/json")
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if not self.path.startswith("/say"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            text = json.loads(self.rfile.read(n)).get("text", "").strip()[:500]
            if not text:
                raise ValueError("empty text")
            # TTS+upload takes a few seconds — do it off-thread so the
            # dashboard's polling never stalls behind a speak request.
            threading.Thread(target=say, args=(text,), daemon=True).start()
            self._send(b'{"ok":true}', "application/json")
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())


def main():
    threading.Thread(target=_timelapse_loop, daemon=True).start()
    # Make sure face tracking is on so the view has data to show.
    # Nudge the robot into its usual live state, but NEVER on the startup
    # path: these are blocking HTTP calls, and when the robot is offline they
    # each sit out their full timeout before the server binds. That made the
    # dashboard unreachable for ~16s exactly when the robot was down — i.e.
    # precisely when you need the connection panel to go find it.
    # ...and never when Vibey has been switched off. The watchdog restarts this
    # service on its own, so an unconditional enable here meant a robot the user
    # had explicitly turned off started following them around the room again a
    # few minutes later, with the dashboard still truthfully reporting "OFF".
    # The off state is the chat service's, read over HTTP rather than duplicated.
    def _nudge_live():
        try:
            with urllib.request.urlopen(f"{CHAT_URL}/state", timeout=4) as r:
                if json.loads(r.read()).get("off"):
                    print("[viewer] Vibey is OFF — not enabling tracking", flush=True)
                    return
        except Exception:  # noqa: BLE001 — chat down: leave the robot alone
            return
        _post(f"{REACHY_URL}/api/media/tracking/enable")
        _post(f"{REACHY_URL}/api/media/wobbling/enable")

    threading.Thread(target=_nudge_live, daemon=True).start()
    print(f"[viewer] reachy    = {REACHY_URL}")
    print(f"[viewer] handsfree = {HANDSFREE_URL}")
    print(f"[viewer] open       http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
