# Vibey 🤖

Vibey is a physical [Reachy Mini](https://www.pollen-robotics.com/) robot with a
personality: camera eyes, mic ears, antenna eyebrows, a British-robot voice, face
memory, games, and — unusually — write access to its own source code. Lately it
has wheels too: it remote-controls a little Waveshare Wave Rover car, by hand,
by voice, or by watching it through its own camera.

[![Watch the Vibey demo](docs/demo/poster.jpg)](https://jack-clips.vercel.app/0164fc63)

▶️ **[Watch the demo (48s)](https://jack-clips.vercel.app/0164fc63)**: the whole thing in one reel.

Four ways in:

- 💬 **Text it on Telegram.** It's a bot too: ask what it can see, get a photo
  back, tell it something to remember (`reachy_telegram.py`).
- 📱 **An iPhone app.** Wake it, drive it, make it talk, DJ, browse its memory
  (`ios/Vibey`, SwiftUI).
- 💻 **A Mac app.** Menu bar status item plus the full live dashboard
  (`mac/`), or just type `./vibey`.
- 🛞 **Drive it around.** The Wheels panel in the dashboard, the Drive screen
  on the phone, a game controller, `/drive` on Telegram, or just ask Vibey
  ("drive to the couch"). See [The rover](#the-rover) below.
- 🛠 **Program it to do anything.** Every service is a small readable Python
  file. Clone the repo, add a skill, and the robot does it.

The reel itself is code: an animated page (`docs/demo/demo.html`), a
synthesized soundtrack (`docs/demo/soundtrack.py`), and
`node docs/demo/render.mjs` to re-render it.

This repo is the whole robot: its eyes, voice, memory, body language, dashboard,
and the voice "brains" it can think with. It was split out of the
[`handsfree`](https://github.com/jackmielke/handsfree) gesture-OS repo, where it
started life; the only remaining tie is an optional gesture bridge that reads
handsfree's motion stream over HTTP (see `reachy_bridge.py`).

## The services

Each is a small standalone program that talks to the others over HTTP on a fixed
port. Boot them all with `./start_wonder.sh` (shell-launched — the chat mic needs
a real terminal's mic permission).

| Port | Service | What it does |
|------|---------|--------------|
| 8770 | `reachy_viewer.py`   | **Dashboard** — camera, chat, face gallery, controls |
| 8771 | `reachy_camera.py`   | Robot camera → MJPEG bridge (SDK venv) |
| 8772 | `reachy_chat.py`     | **The mind** — hears, thinks, speaks; picks the voice brain |
| 8773 | `reachy_memory.py`   | Face recognition + greetings + journal (SDK venv) |
| 8774 | `reachy_vibeverse.py`| Vibey's avatar in the VibeVerse 3D world |
| 8775 | `reachy_robot_mic.py`| Robot's mic → PCM stream, and replies streamed to its speaker (SDK venv) |
| 8776 | `reachy_gestures.py` | Hand gestures from the camera → body (`.venv-gestures`) |
| 8778 | `reachy_dj.py`       | DJ mode: local tracks, live tempo, bobs to the beat |
| —    | `reachy_rover.py`    | The Wave Rover's wheels (owned by the chat service, served at `:8772/rover`) |
| —    | `reachy_rover_pilot.py` | Drive by eye: look, pick one short move, drive, look again |
| —    | `reachy_telegram.py` | Text Vibey from anywhere |
| —    | `reachy_alarm.py`    | Wake-up shows (7am song + dance) |
| —    | `reachy_watchdog.py` | Restarts any dead service, pings Telegram |
| —    | `reachy_bridge.py`   | Optional: handsfree gestures → robot body (`BRIDGE=1`) |

Supporting modules: `reachy_voice.py` (ElevenLabs TTS), `reachy_emotes.py`
(synthesized chirps + dances), `reachy_sfx.py` (sci-fi soundboard),
`reachy_vibe.py` (the `vibe_check` voice tool — scores the *conversation* 1-100
with an uncertainty band; sensitive-topic and health inference are filtered in
code, and saving to Supabase needs both `VIBE_LOG=1` and a spoken yes. Its
docstring has the schema and the privacy notes).

## The voice brains

Pick one from the iOS app's Brain pop-up (tap the brain pill on Home), the
dashboard, or `/brain` on Telegram:

- **Basic** — Realtime 2.1 with nothing extra: no tools, memory or nudges. Just
  talks, and can look through the camera.
- **Realtime 2.1** (the default) — one OpenAI model hears, thinks and speaks, with every tool
  (body, camera, DJ, Spotify, memory, coding jobs).
- **GPT-Live 1** — OpenAI's voice layer in front of a gpt-5.5 backend.
- **Local** — everything on the Mac, nothing in the cloud: faster-whisper →
  Ollama (`qwen3:4b-instruct`) → Piper voice. Free, private, works offline
  (`reachy_local.py`; needs `ollama pull qwen3:4b-instruct` and the Piper voice
  in `models/piper/`).

There is also an older Claude CLI brain (`OPENAI_MODE=0`), and the
`improve_yourself` tool always shells out to Claude Code (`reachy_agent.py`) so
Vibey can edit its own source mid-conversation.

Replies stream to the speaker as they arrive. Every session is told the time of
day. Texts sent while Vibey is awake join the same conversation: answered by
text, and said out loud only if Vibey decides the room should hear it
(`say_aloud`).

## The rover

Vibey's wheels are a [Waveshare WAVE ROVER](https://www.waveshare.com/wiki/WAVE_ROVER):
a shoebox-sized ESP32 car that takes JSON commands over Wi-Fi. Right now Vibey
sits on the table and drives the rover as a separate little car; riding on top
is the next phase (see [docs/ROVER_PLAN.md](docs/ROVER_PLAN.md)).

- **By hand:** the Wheels panel in the dashboard (`control.html` on `:8770`,
  also what the Mac app shows): hold-to-drive pad, joystick, WASD/arrow keys,
  Q/E to spin, a game controller, and a speed picker. The iPhone app has the
  same controls on its Drive screen.
- **By voice:** the realtime brain gets a `drive` tool ("back up a bit") as
  soon as `ROVER_URL` is set.
- **By eye:** `drive_to` ("drive to the couch") or the "Drive by eye" box:
  `reachy_rover_pilot.py` grabs a camera frame, asks a vision model for one
  short move, drives it, and looks again, up to 10 steps. Any stop or manual
  press ends it, and it obeys the same camera/privacy switches as `look`.

Safety is built into the protocol: every move is re-sent every fraction of a
second and the rover stops by itself when the commands stop, so a dropped
connection, a closed app or a locked phone all end in a stop. Only the chat
service talks to the rover, so the pad and Vibey's voice never fight.

Setup, once: the rover ships as its own "UGV" hotspot. Join the Mac to it, run
`./vibey rover wifi "YourHomeWifi"` (2.4 GHz only), rejoin your Wi-Fi, then
`./vibey rover find --save` writes `ROVER_URL` into `.env`. After that, if the
rover gets a new address after a power cycle, Vibey finds it again by itself.
`./vibey rover status` shows battery voltage.

## Day-to-day

- **Stages** — 1 robot alone · 2 + Mac (no cloud, no spend) · 3 + cloud.
- **Camera switch** — `/camera off` (or the app) closes the video session, which
  also frees the robot's CPU and smooths its motion.
- **Budget** — crossing `VIBEY_DAILY_BUDGET` sends a loud Telegram warning and
  keeps running (`VIBEY_BUDGET_HARD=1` restores sleep-and-refuse). Tap the
  "$ today" pill in the app for the breakdown.
- **Asleep is idle** — face memory and gestures pause while Vibey sleeps, and the
  wake listener throttles itself when a TV is talking.
- **Spotify** — Vibey drives the Mac's Spotify app; searching by name needs
  `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET`.
- **iOS app** — `ios/Vibey` (`./build.sh` installs on a paired iPhone): live
  camera, chat, soundboard, friend profiles, controls, spend.

## Setup

You need the robot (a Reachy Mini, wireless or lite) on the same network as a
Mac. Everything else runs on the Mac today; [ROBOT_NATIVE.md](ROBOT_NATIVE.md)
is the plan for moving the services onto the robot itself (prepared, not yet
deployed).

Two Python environments (both git-ignored):

- **`reachy_env`** — the Reachy Mini SDK venv (camera, mic, face recognition).
  Needs `reachy_mini`, `face_recognition` (via `dlib-bin`), GStreamer. This is
  the heavy one.
- **`.venv`** — a lean venv for the chat service: `faster-whisper`,
  `sounddevice`, `numpy`, `websockets`.

Copy `.env.example` to `.env` and fill in your keys (at minimum
`OPENAI_API_KEY` and `REACHY_URL`; most other features stay off until their
key is set), then `./start_wonder.sh` and open http://localhost:8770.

`./vibey` is the everyday remote: `vibey` (start), `vibey stop`, `vibey status`
(one plain sentence on what's wrong), `vibey wake` / `sleep`, `vibey fix`
(restarts a wedged robot daemon), `vibey logs`, `vibey look` (one camera
frame), `vibey rover ...`, `vibey wifi ...`. `reachy_watchdog.py` restarts any
service that dies.

The apps: `mac/build.sh` builds the menu-bar Vibey.app, and `ios/Vibey/build.sh`
(xcodegen) installs the iPhone app on a paired phone. The Telegram webhook is a
Supabase edge function in `supabase/functions/`.

## Finding your way around

The repo grew one feature at a time, so most of it is flat at the top level:

- `reachy_*.py`: the services and the modules they share. The ones to read
  first: `reachy_chat.py` (the mind), `reachy_openai_realtime.py` (the default
  brain and its tools), `reachy_viewer.py` (dashboard server), `reachy_rover.py`.
- `vibey_*.py`: small shared helpers (auth, safe file writes, a doctor script).
- `control.html`: the dashboard UI.
- `mac/`, `ios/`: the native apps. `mimic/`: a separate browser face-mimic toy.
- `robot/`: deploy scripts and systemd units for running on the robot itself.
- `supabase/`: the Telegram webhook function and its one migration.
- `docs/`: the rover plan and the demo reel source.
- Top-level `*.md`: mostly Vibey's own brain, see below; `ROBOT_NATIVE.md`,
  `MEMORY_PLAN.md` and `VIBEVERSE_IDEAS.md` are plans.

## Identity

Vibey's personality and self-knowledge live in the markdown files it can edit
itself: `IDENTITY.md`, `SOUL.md`, `USER.md`, `TOOLS.md`, `AGENTS.md`,
`HEARTBEAT.md`, and `SKILLS.md` (things it was taught in conversation).
`OVERNIGHT.md` is the diary of one all-night build session. Private things
(transcripts, faces, memories, `.env`) are git-ignored and never leave the
machine.
