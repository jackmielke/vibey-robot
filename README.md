# Vibey 🤖

Vibey is a physical [Reachy Mini](https://www.pollen-robotics.com/) robot with a
personality: camera eyes, mic ears, antenna eyebrows, a British-robot voice, face
memory, games, and — unusually — write access to its own source code.

This repo is the whole robot: its eyes, voice, memory, body language, dashboard,
and the three "brains" it can think with. It was split out of the
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
| 8772 | `reachy_chat.py`     | **The mind** — hears, thinks, speaks; 3 brains |
| 8773 | `reachy_memory.py`   | Face recognition + greetings + journal (SDK venv) |
| 8774 | `reachy_vibeverse.py`| Vibey's avatar in the VibeVerse 3D world |
| 8775 | `reachy_robot_mic.py`| Robot's own mic → PCM stream (SDK venv) |
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

## The three brains

Switch on the dashboard or by voice; the antennas change posture to show which:

- **Claude CLI** (default) — thoughtful, `claude-sonnet-5` via the `claude` CLI.
- **⚡ Fast** — ElevenLabs Conversational AI (STT+LLM+TTS in one realtime call).
- **🎮 Vibe** — an OpenClaw agent whose workspace is *this repo*, so it can
  improve the robot's own code mid-conversation.

## Setup

Two Python environments (both git-ignored):

- **`reachy_env`** — the Reachy Mini SDK venv (camera, mic, face recognition).
  Needs `reachy_mini`, `face_recognition` (via `dlib-bin`), GStreamer. This is
  the heavy one.
- **`.venv`** — a lean venv for the chat service: `faster-whisper`,
  `sounddevice`, `numpy`, `websockets`.

Copy `.env.example` to `.env` and fill in your keys, then `./start_wonder.sh`.

## Identity

Vibey's personality and self-knowledge live in the markdown files it can edit
itself: `IDENTITY.md`, `SOUL.md`, `USER.md`, `TOOLS.md`, `AGENTS.md`,
`HEARTBEAT.md`. `OVERNIGHT.md` is its build diary.
