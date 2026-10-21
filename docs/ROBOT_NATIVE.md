# Vibey with the Mac switched off

Goal: every Vibey service that has to be up for Vibey to be Vibey runs on the
robot itself (Reachy Mini wireless: Raspberry Pi CM4, 4 cores, 3.7 GB RAM with
~0.65 GB used, ~4.6 GB free disk, Pollen daemon on `:8000`). The Mac becomes
optional: it adds Claude Code jobs and the heavy local models when it is on.

Status: **prepared, not deployed.** Nothing here has run on the robot yet. It is
meant to be deployed and load-tested with Jack, one phase at a time.

## What runs where, at the end

| Where | What |
|---|---|
| **Robot** | Pollen daemon (motors, audio, camera, onboard face tracking), mic bridge (local), voice brain `reachy_chat.py` (GPT-Live / Realtime session, clap wake, emotes, idle, sleep timer, scribe control), Telegram, alarms, watchdog + control-loop guard, memories on disk, dashboard, camera MJPEG. Faces last, if a CM4-sized model works. |
| **Cloud** (unchanged) | OpenAI GPT-Live / Realtime (the actual conversation), ElevenLabs TTS, Supabase (faces, journal), Supermemory, VibeVerse, Telegram API. |
| **Mac only** | Claude Code jobs (`reachy_agent.py`, "improve yourself", overnight runs): the robot already answers "can't reach my coding agent" when the CLI is missing. Whisper anything (wake *phrase*, scribe, whisper brain), gestures (mediapipe), DJ (sounddevice), handsfree bridge, Spotify control (osascript), `reachy_vibeverse.py` until someone checks its deps. |

## Media on the robot: no WebRTC

The SDK already supports this. `ReachyMini(connection_mode="localhost_only")`
on the same board as the daemon makes `media_backend="default"` pick
`MediaBackend.LOCAL`: GStreamer IPC camera + local audio, read straight from the
daemon, no WebRTC session to its own IP (checked in reachy_mini 1.9.0,
`ReachyMini._configure_mediamanager`).

So the two bridges stay, with one line changed each. They stop being
WebRTC-to-HTTP bridges and become local fan-outs. That is still the right
shape: the mic has three readers (voice engine, wake listener, scribe) and
the camera has four (dashboard, Telegram, vision tools, faces), and only one
process should hold each device.

- `reachy_robot_mic.py` with `VIBEY_ON_ROBOT=1`: LOCAL audio, served on
  `127.0.0.1:8775` only (`ROBOT_MIC_BIND`). A live room mic should not be
  readable by anyone else on the venue Wi-Fi.
- `reachy_camera.py` with `VIBEY_ON_ROBOT=1`: LOCAL camera, capped at
  `CAMERA_MAX_FPS=4`. On this path `read_jpeg()` is a software JPEG encode for
  every frame (the SDK itself says "for occasional stills only").
- **Speaker:** already works. The voice engines upload a WAV to
  `REACHY_URL/api/media/sounds/upload` and play it. With
  `REACHY_URL=http://localhost:8000` that is a loopback copy, not Wi-Fi.
  Later, if latency matters: stream into `mini.media.push_audio_sample()`.
- Echo: the robot's audio pipeline already subtracts its own speaker (that is
  why `GATE_ON_SPEAK` is off for the robot mic). Nothing changes there.

## Code changes (all opt-in through `VIBEY_ON_ROBOT=1`)

On the Mac nothing is set, so Mac mode runs exactly as before.

| File | Change |
|---|---|
| `reachy_robot_mic.py` | `localhost_only` on the robot, bind address from `ROBOT_MIC_BIND` |
| `reachy_camera.py` | `localhost_only` on the robot, optional `CAMERA_MAX_FPS` |
| `reachy_chat.py` | `sounddevice` import tolerated on the robot only; mic/speaker pinned to "robot" there, and the laptop toggles refuse |
| `reachy_wake.py` | Without faster-whisper, the phrase half turns itself off once (logged) and claps keep working, instead of a failed import every 2 s |
| `reachy_viewer.py` | On the robot, `localhost` URLs handed to the browser are rewritten to the host the page was opened from (the phone's localhost is the phone) |
| `reachy_watchdog.py` | Robot: health checks restart through `systemctl --user`, plus a control-loop guard (log every reading under 40 Hz, one Telegram alert a day after 3 in a row). Mac: skips services listed in `.robot_services` |
| `start_wonder.sh` | Does not start services listed in `.robot_services`; points the Mac's `CHAT_URL`/`CAM_URL`/`MEM_URL` at the robot for moved ones |

Robot config lives in `robot/robot.env` (no secrets, in git). Secrets stay in
`.env`, copied to `~/vibey/app/.env` with mode 600. `load_env()` never
overwrites a variable already set, so `robot.env` wins (e.g. `REACHY_URL`).

## The CPU budget, which decides everything

Measured 2026-09-22: the daemon's control loop is nominally 50 Hz, ~46 Hz with
tracking off, **~28 Hz with the daemon's own face tracking on** (85 ms stalls).
Under ~30 Hz the robot feels glitchy. Everything we add competes with that loop
on four slow cores.

Rules:

1. **Floor: control loop at or above 40 Hz**, read from
   `GET /api/daemon/status` → `backend_status.control_loop_stats.mean_control_loop_frequency`
   (and `max_control_loop_interval`). `robot/load_check.sh` samples it before
   and after every phase; `deploy.sh --phase N` refuses to call a phase done
   below it.
2. Every vibey unit: `Nice=10`, `CPUWeight=20`, `IOSchedulingPriority=7`, and a
   hard `CPUQuota` (per core, 400% = whole board): chat 60%, camera 30%,
   mic 20%, viewer 20%, telegram 15%, alarm 10%, watchdog 5%, faces 50%. That is
   at most ~2.1 cores, leaving ~1.9 for the daemon, even with everything on.
3. `CPUQuota` needs the cgroup `cpu` controller delegated to user services.
   `install.sh` checks it and prints the one root command if it is missing;
   without it only `Nice` applies.
4. If tracking-on is already at 28 Hz before we add anything, the honest
   options are: tracking off while talking (`_apply_tracking` bundles tracking
   with speech wobble, which is nearly free, so split them), or pin vibey units
   off the daemon's core with `AllowedCPUs=` once we know which core that is.
   Decide from the phase-0 numbers, not in advance.

## Phases

Each phase: `robot/deploy.sh --phase N` does load check → stop the Mac's copy
and record it in `.robot_services` → `systemctl --user enable --now` on the
robot → wait 20 s → load check. Under 40 Hz it stops and prints
`robot/deploy.sh --rollback N`.

| Phase | Services | Notes |
|---|---|---|
| 0 | none | `robot/deploy.sh --install`, then `robot/load_check.sh` with tracking on and off. Baseline. |
| 1 | `robot_mic`, `chat`, `watchdog` | The voice brain with GPT-Live. Wake word, emotes, idle, sleep timer are in-process. Wake is **clap-only** on the robot (see deps). Test: clap, talk, barge-in, "go to sleep". |
| 2 | `telegram`, `alarm` | ONE Telegram poller: the Mac's copy is killed and its `.telegram_state.json` (the update offset) handed over first. Alarms need chat on the same machine (hardcoded `localhost:8772`). |
| 3 | memories | No new process: units set `VIBEY_MEMORIES_DIR=~/vibey/memories`, so memories are simply local. When `reachy_memories.py` (uncommitted on `main` today) lands, add `if os.environ.get("VIBEY_ON_ROBOT") == "1": return True` at the top of `sync()`. Without it, it only wastes an ssh-to-itself attempt every 10 s. |
| 4 | `viewer`, `camera` | Dashboard at `http://<robot>:8770`. Until this phase, Telegram's `/photo` and emote commands fail: they go through `localhost:8770`. Do 2 and 4 the same day. |
| 5 | `memory` (faces) | Last, and blocked: `face_recognition` needs dlib (source build on ARM, ~1 h, >1 GB RAM). Candidates: OpenCV YuNet + SFace (onnx, small) or embeddings in the cloud. Alternatively the daemon's own tracking already finds faces. |

Rollback is always `robot/deploy.sh --rollback N`, then `vibey` on the Mac.

## ARM and heavy deps

Robot OS: aarch64 Raspberry Pi OS (Bookworm, Python 3.11). The SDK needs >= 3.11.
All touched code parses as 3.11 (no 3.12 f-string nesting).

| Dep | On the robot? |
|---|---|
| numpy, websockets (>=14, the engines use `additional_headers=`) | Yes, `~/vibey/.venv`, prebuilt aarch64 wheels (`--only-binary`) |
| reachy_mini + PyGObject/GStreamer | Yes, but **not installed by us**: `~/vibey/bin/sdk-python` wraps the daemon's own interpreter (exact version match, no GStreamer build) |
| faster-whisper / ctranslate2 | **No.** Wake *phrase*, scribe and the whisper brain stay on the Mac. Clap wake works without it. Later option: openWakeWord (onnx, tiny) for "hey vibey" |
| torch, mediapipe, opencv | **No.** Gestures stay on the Mac |
| face_recognition / dlib | **No** (see phase 5) |
| sounddevice / PortAudio | **No.** Only the "laptop" mic/speaker paths use it |

`install.sh` warns if any of the forbidden ones show up in the venv.

## Files

- `robot/systemd/vibey-{mic,chat,telegram,alarm,watchdog,viewer,camera,memory}.service`, `vibey.target`
- `robot/robot.env`: robot overrides, no secrets
- `robot/requirements-robot.txt`
- `robot/install.sh`: on the robot, idempotent. venv, sdk-python, cgroup + linger checks, units. Starts nothing.
- `robot/deploy.sh`: on the Mac. rsync code (secrets and runtime state excluded), `.env` as 600, install, restart running units, `--phase N`, `--rollback N`, `--status`.
- `robot/load_check.sh`: control-loop Hz, CPU, RAM, SoC temp/throttle, per-unit CPU. Exit 1 under the floor.

## Risks

- **Control loop.** The main one. Phase 0 tells us how much headroom exists.
- **LOCAL audio while the daemon also plays sound.** Untested on this unit.
  First thing in phase 1: `curl -s localhost:8775/status` on the robot and one
  spoken sentence. If LOCAL capture conflicts with the daemon, fall back to the
  daemon's WebRTC on loopback (`connection_mode="network"`, host `localhost`).
- **Two brains / two pollers.** Anything moved must not also run on the Mac.
  `.robot_services` + the Mac watchdog + `start_wonder.sh` enforce it; a hand
  launch on the Mac would still bypass it.
- **Hardcoded `localhost:877x`** in alarm, memory, telegram (see phases 2 and 4).
  Partial states break those cross-calls until their partner moves.
- **Venue Wi-Fi.** Dashboard (8770), camera (8771) and chat control (8772)
  listen on all interfaces, same as on the Mac today, but the robot moves
  between networks more. The mic is loopback-only.
- **Disk.** 4.6 GB free. Transcripts and journald grow. Cap journald
  (`SystemMaxUse=200M`) once as root.
- **Brownouts on battery** (seen 2026-08-31 and 09-26). More CPU = more draw.
  Load-test on the charger first.
- **The daemon wedges** under media churn (`/api/media/acquire`+`release`).
  LOCAL mode should churn less than WebRTC; watch it.

## Tomorrow, in order

```sh
robot/deploy.sh --install          # code + .env + venv + units; starts nothing
robot/load_check.sh                # baseline (repeat with tracking on and off)
robot/deploy.sh --phase 1          # voice on the robot; Mac mic/chat stopped
# test voice, then:
robot/deploy.sh --phase 2          # telegram + alarms
robot/deploy.sh --phase 4          # dashboard + camera
```
