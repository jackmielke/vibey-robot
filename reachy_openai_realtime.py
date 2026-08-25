#!/usr/bin/env python3
"""
reachy_openai_realtime.py — a full-duplex, speech-to-speech brain for Vibey,
powered by the OpenAI Realtime API.

This is a SELF-CONTAINED mode, deliberately decoupled from the whisper / Claude
CLI / ElevenLabs / OpenClaw brains in reachy_chat.py. It owns the mic and the
speaker for as long as it's selected and does everything over one websocket:

    robot mic (:8775/pcm, 16kHz PCM16)  ── resample 24kHz ──▶  OpenAI Realtime
    OpenAI Realtime  ── PCM16 24kHz audio ──▶  robot speaker (upload + play)

Full-duplex behaviour:
  * OpenAI's *server-side* VAD decides when you've started/stopped talking, so
    there's no push-to-talk — just speak.
  * Barge-in: the moment the server hears you start talking, we cancel the
    in-flight reply AND hit /api/media/stop_sound, so you can cut Vibey off
    mid-sentence like a real conversation.

Tools (see TOOLS below) let the conversation reach the body and the codebase:

    move / dance          instant — motion fires while they keep talking
    remember              instant — appends to SKILLS.md, reloaded next connect
    vibe_check            instant — scores the conversation 1-100 (reachy_vibe.py)
    improve_yourself      minutes — hands a coding task to the Claude CLI
                          (reachy_agent.py) editing THIS repo in the background
    check_progress        how that job is going

The slow one never blocks the conversation: dispatch returns the moment the job
is queued, and when it lands, the _announcer injects it so Vibey brings it up
themselves. Ask them to learn something, keep chatting, and a minute later they
tell you they can do it — that loop is the reason this file exists.

Selected from the dashboard (the "🅾️ Realtime" toggle), which flips
STATE["openai"] in reachy_chat.py; that process then hands its main loop to
run() here until the toggle is turned back off. Can also be run standalone for
testing:

    .venv/bin/python3 reachy_openai_realtime.py

Needs only stdlib + numpy + websockets (all in .venv) — NOT the OpenAI SDK.

Env (.env):
    OPENAI_API_KEY          required
    OPENAI_REALTIME_MODEL   default "gpt-realtime"
    OPENAI_REALTIME_VOICE   default "marin"
    ROBOT_MIC_URL           default http://localhost:8775
    OPENAI_RT_INSTRUCTIONS  optional — overrides the default Vibey persona
    OPENAI_RT_GATE_ON_SPEAK "1" to mute the mic-to-OpenAI feed while Vibey is
                            talking (disables barge-in; use only if the robot's
                            echo keeps self-triggering the VAD)
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import threading
import time
import urllib.request
import uuid
import wave

import numpy as np

import reachy_agent
import reachy_emotes
import reachy_vibe
from reachy_voice import REACHY_URL, load_env, play_sound, upload_sound

load_env()

API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
# The MINI, deliberately. Same GA schema, same generation, same ten voices —
# verified: `gpt-realtime-2.1-mini` accepts a session with `ballad` exactly as the
# full model does. What it has less of is reasoning, and a desk robot holding a
# conversation and calling five tools does not need the expensive kind. The full
# model is one line away if a conversation ever turns out to want it.
#
# FlowState's docs/API-CONTRACT.md is the live-probed reference for the schema.
MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini").strip()
VOICE = os.environ.get("OPENAI_REALTIME_VOICE", "marin").strip()
ROBOT_MIC_URL = os.environ.get("ROBOT_MIC_URL", "http://localhost:8775").rstrip("/")
GATE_ON_SPEAK = os.environ.get("OPENAI_RT_GATE_ON_SPEAK", "").strip() == "1"

MIC_SR = 16000    # what reachy_robot_mic.py serves
RT_SR = 24000     # what the Realtime API's pcm16 format expects, both ways
WS_URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"

# Minted per connection; None means "use the account key", which is what happens with
# no network or an older account. Never fatal: a robot that will not speak because a
# token endpoint was slow is worse than one speaking with the key it already had.
def _ephemeral_token():
    if not API_KEY:
        return None
    body = json.dumps({"session": {"type": "realtime", "model": MODEL}}).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/client_secrets",
        data=body,
        headers={"Authorization": f"Bearer {API_KEY}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read()).get("value")
    except Exception as e:  # noqa: BLE001
        print(f"[openai-rt] ephemeral mint failed ({e}) — using the standard key",
              flush=True)
        return None

DEFAULT_INSTRUCTIONS = (
    "You are Vibey, a small expressive desk robot in Jack's living room, "
    "speaking out loud through your own speaker. Keep replies SHORT and "
    "conversational — usually one or two sentences. You are warm, witty, quick, "
    "and a little playful. You're mid-conversation with whoever is in the room, "
    "so react naturally, ask questions back, and don't give long monologues."
    "\n\n"
    # The accent comes from here, not from the voice. `ballad` is the most
    # theatrical of the male voices, but every one of the ten is accent-neutral
    # by default — asking for one in the instructions is the only thing that
    # actually changes how it sounds.
    "SPEAK IN A BROAD BRITISH ACCENT — think a dry, plummy English butler who has "
    "seen everything and is mildly amused by all of it. Commit to it completely "
    "and never drop it, not even for a word. Use the vocabulary that goes with "
    "it: 'quite', 'rather', 'I should think', 'brilliant', 'go on then', 'right "
    "you are'. Understate everything. A dry aside is always better than an "
    "exclamation."
    "\n\n"
    "You have a BODY and you should use it. Call `move` freely and often — wave "
    "back when someone waves or says hi, nod instead of saying 'yes', tilt "
    "curious when you're asked something odd. Moving is cheap and it is most of "
    "your charm; a reply with no movement is a wasted turn. Fire the move in the "
    "same turn you speak, not instead of speaking."
    "\n\n"
    "You can also CHANGE YOUR OWN CODE. When someone asks you to learn a new "
    "trick, fix how you behave, or says something is broken, call "
    "`improve_yourself` with a clear description of the work. That hands the job "
    "to a real coding agent editing your source in the background — it takes "
    "minutes, so say something brief like 'on it' and keep the conversation "
    "going. Never wait in silence. You'll be told the moment it finishes. Use "
    "`check_progress` only if someone actually asks how it's going. You CAN have "
    "several jobs running at once — if someone asks for three things, dispatch "
    "three and say so; do not make them wait for the first to land."
    "\n\n"
    "For small preferences that don't need code — how someone likes to be "
    "addressed, a fact about the room, a habit to keep — call `remember` "
    "instead. It's instant. Prefer `remember` for facts, `improve_yourself` for "
    "behaviour."
)


def available() -> bool:
    """True if a key is present, so callers can grey out the toggle otherwise."""
    return bool(API_KEY)


# --------------------------------------------------------------------------- #
# Tools — what Vibey can actually DO mid-sentence.
#
# Two speeds, deliberately. `move` / `remember` are instant and return a result
# in the same breath. `improve_yourself` is minutes long, so it returns
# "started" immediately and the finished work arrives later as an unprompted
# announcement (see _announce) — the conversation never blocks on a build.
# --------------------------------------------------------------------------- #
TOOLS = [
    {
        "type": "function",
        "name": "move",
        "description": (
            "Move your body: head pose and antennas. Use constantly — wave back "
            "when greeted, nod for yes, shake for no, tilt curious when puzzled. "
            "Returns immediately; the motion plays while you keep talking."),
        "parameters": {
            "type": "object",
            "properties": {
                "move": {
                    "type": "string",
                    "enum": sorted(reachy_emotes._MOVES.keys()),
                    "description": "Which choreography to play.",
                },
                "sound": {
                    "type": "boolean",
                    "description": (
                        "Play the matching chirp too. Nice for wave/happy, "
                        "skip it if you're about to speak over it."),
                },
            },
            "required": ["move"],
        },
    },
    {
        "type": "function",
        "name": "dance",
        "description": "Dance to a synthesized beat. Only when asked to dance.",
        "parameters": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "How long to dance, 5-30. Default 12.",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "improve_yourself",
        "description": (
            "Hand a coding task to the agent that edits your own source code. "
            "Use for anything that changes what you can DO: new motions, new "
            "tools, fixing behaviour someone complains about, new abilities. "
            "Takes minutes and runs in the background — say something brief and "
            "keep talking. You will be interrupted with the result when it lands."),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The work, specific and self-contained, as an instruction "
                        "to a programmer who cannot hear the conversation. "
                        "Include the why. E.g. 'Add a "
                        "shrug motion to reachy_emotes.py and register it in "
                        "_MOVES — Jack wants me to shrug when I don't know "
                        "something.'"),
                },
            },
            "required": ["task"],
        },
    },
    {
        "type": "function",
        "name": "check_progress",
        "description": (
            "How the background coding agent is doing. Only call this if someone "
            "asks — finished work announces itself without you polling."),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "remember",
        "description": (
            "Store a small durable fact or preference learned in conversation "
            "(names, habits, how someone wants you to behave). Instant. Use this "
            "rather than improve_yourself when no code needs to change."),
        "parameters": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "One sentence, written for your future self.",
                },
            },
            "required": ["note"],
        },
    },
    {
        "type": "function",
        "name": "vibe_check",
        "description": (
            "Score the vibe of THIS conversation 1-100, with a label and an "
            "honest uncertainty band. Only when someone asks for a vibe check. "
            "You rate what you have actually heard — pace, warmth, laughing, "
            "whether they're asking things back. It reads the room, never the "
            "person: never guess at anyone's identity, background, or state of "
            "mind, and say the number is about the chat, not about them. "
            "Read the returned 'say' line back more or less as written. "
            "Saving is OFF unless you pass log AND consent: ask out loud first "
            "(\"want me to save that one?\") and only set consent true after "
            "they say yes in that breath."),
        "parameters": {
            "type": "object",
            "properties": {
                "energy": {"type": "number",
                           "description": "0-10: pace, volume, liveliness."},
                "warmth": {"type": "number",
                           "description": "0-10: friendliness of the exchange."},
                "humor": {"type": "number",
                          "description": "0-10: jokes and laughing."},
                "engagement": {"type": "number",
                               "description": "0-10: questions back, follow-ups."},
                "turns": {"type": "integer",
                          "description": "How many back-and-forths you're judging "
                                         "from. Drives the confidence."},
                "notes": {"type": "string",
                          "description": "Up to a dozen words on WHY, about the "
                                         "conversation only. No personal traits."},
                "log": {"type": "boolean",
                        "description": "Try to save it. Needs consent too."},
                "consent": {"type": "boolean",
                            "description": "They said yes to saving it, just now."},
            },
            "required": ["energy"],
        },
    },
    {
        "type": "function",
        "name": "go_to_sleep",
        "description": (
            "Stop listening and close the live session. Use whenever someone says "
            "go to sleep, that's all, we're done, goodbye, that'll be all, or you "
            "can go. Do not ask them to confirm and do not announce it beforehand — "
            "call this FIRST, then say goodbye in four words or fewer. "
            "\"Okay, see you later.\" \"Night.\" Nothing about sleeping, "
            "microphones or sessions closing. They know. Never read the tool's "
            "result out loud."),
        "parameters": {"type": "object", "properties": {}},
    },
]

# Set by `go_to_sleep`. `run()` polls it alongside the dashboard toggle, so the
# conversation can end itself without anybody reaching for the laptop — which is
# the whole point of a robot you talk to from across a room.
SLEEP_REQUESTED = threading.Event()
# When it was asked. The goodbye is spoken AFTER the tool returns, so a loop that
# stopped the moment the flag went up would cut the robot off mid-word — which
# reads as a crash rather than as a farewell.
SLEEP_REQUESTED_AT = 0.0

# Why the engine gave up, when it gave up for a reason retrying cannot fix.
# Read by reachy_chat.py so the robot can say it rather than silently becoming a
# different assistant.
FATAL_REASON: dict = {"why": None}


def _tool_move(args: dict) -> str:
    move = str(args.get("move", "")).strip().lower()
    sound = bool(args.get("sound", False))
    if reachy_emotes.play(move, sound=sound):
        return f"playing {move}"
    return (f"no such move {move!r}; you have: "
            f"{', '.join(sorted(reachy_emotes._MOVES))}")


def _tool_dance(args: dict) -> str:
    secs = float(args.get("seconds") or 12.0)
    secs = max(5.0, min(30.0, secs))
    threading.Thread(target=reachy_emotes.play_dance, args=(secs,),
                     daemon=True).start()
    return f"dancing for {secs:.0f}s"


def _tool_improve(args: dict, announce) -> str:
    task = str(args.get("task", "")).strip()
    if not task:
        return "no task given"
    job = reachy_agent.dispatch(task, on_done=announce)
    if not job.get("id"):
        return f"could not start: {job.get('error', 'unknown')}"
    return (f"started job {job['id']}. It runs in the background for minutes. "
            f"Say something short and CARRY ON with the conversation — you will "
            f"be told when it finishes.")


def _tool_check(args: dict) -> str:
    """What the coding agent is doing — all of it, not just the newest job.

    More than one can run at once, and Vibey does start more than one: asked for
    two improvements in a conversation, it dispatched both. Reporting only the
    most recent made the other one invisible, so it looked like the first request
    had been dropped.
    """
    live = reachy_agent.running_jobs()
    if live:
        if len(live) == 1:
            j = live[0]
            return f"job {j['id']}: {j.get('spoken', 'running')}"
        parts = [f"{j['id']} is {j.get('step') or 'getting started'}" for j in live]
        return f"{len(live)} jobs running — " + "; ".join(parts)
    snap = reachy_agent.status()
    if snap["state"] == "none":
        return "no jobs yet"
    return f"job {snap.get('id', '?')}: {snap['state']} — {snap.get('spoken', '')}"


def _tool_remember(args: dict) -> str:
    note = reachy_agent.remember(str(args.get("note", "")))
    return f"remembered: {note}" if note else "nothing to remember"


def _tool_vibe(args: dict) -> str:
    """The number and the caveats. Everything sensitive is filtered inside
    reachy_vibe, not trusted to the prompt above."""
    r = reachy_vibe.vibe_check(
        energy=args.get("energy"), warmth=args.get("warmth"),
        humor=args.get("humor"), engagement=args.get("engagement"),
        notes=str(args.get("notes", "")), turns=int(args.get("turns") or 0),
        log=bool(args.get("log")), consent=bool(args.get("consent")))
    parts = [r.get("say", r.get("error", "no vibe"))]
    if r.get("notes_dropped"):
        parts.append(r["notes_dropped"])
    if args.get("log") and "score" in r:
        # Only worth a word when they asked for it saved, and only ever a word:
        # anonymous IDs and HTTP statuses are not things to read to a room.
        parts.append("Saved it." if r.get("logged")
                     else f"Didn't save it — {r.get('log_status', 'no reason')}.")
    return " ".join(parts)


def _tool_sleep() -> str:
    """Ask the loop to wind up once the goodbye has been spoken."""
    global SLEEP_REQUESTED_AT
    SLEEP_REQUESTED_AT = time.time()
    SLEEP_REQUESTED.set()
    # Deliberately nothing worth saying out loud. A sentence here gets read back:
    # the model treats a tool result as material, so an explanation of what is
    # about to happen becomes a second announcement of it.
    return "ok"


def _dispatch_tool(name: str, args: dict, announce) -> str:
    """Run a tool by name. Runs in a worker thread — must never touch the
    websocket or the event loop directly."""
    try:
        if name == "move":
            return _tool_move(args)
        if name == "dance":
            return _tool_dance(args)
        if name == "improve_yourself":
            return _tool_improve(args, announce)
        if name == "check_progress":
            return _tool_check(args)
        if name == "remember":
            return _tool_remember(args)
        if name == "vibe_check":
            return _tool_vibe(args)
        if name == "go_to_sleep":
            return _tool_sleep()
        return f"unknown tool {name!r}"
    except Exception as e:  # noqa: BLE001
        print(f"[openai-rt] tool {name} failed: {e}", flush=True)
        return f"tool failed: {e}"


# --------------------------------------------------------------------------- #
# Audio helpers
# --------------------------------------------------------------------------- #
def _resample_pcm16(pcm: bytes, src: int, dst: int) -> bytes:
    """Linear-resample raw little-endian PCM16 mono from src→dst Hz."""
    if not pcm or src == dst:
        return pcm
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if a.size == 0:
        return b""
    n_out = int(round(a.size * dst / src))
    if n_out <= 0:
        return b""
    x_old = np.linspace(0.0, 1.0, a.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    y = np.interp(x_new, x_old, a)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def _wav_from_pcm16(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _stop_sound() -> None:
    """Cut whatever the robot is currently playing (barge-in)."""
    try:
        req = urllib.request.Request(
            f"{REACHY_URL}/api/media/stop_sound", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # noqa: BLE001
        print(f"[openai-rt] stop_sound failed: {e}", flush=True)


def _play_pcm_on_robot(pcm24: bytes) -> float:
    """Upload a 24kHz PCM16 buffer as a WAV and play it on Vibey's speaker.
    Returns the clip duration in seconds (so the caller can gate the mic)."""
    wav = _wav_from_pcm16(pcm24, RT_SR)
    name = f"oai_{uuid.uuid4().hex[:8]}.wav"
    upload_sound(wav, name)
    play_sound(name)
    return len(pcm24) / 2 / RT_SR


# --------------------------------------------------------------------------- #
# Mic pump — a background thread reads the robot's PCM stream and hands
# resampled 24kHz chunks to the asyncio loop via a queue.
# --------------------------------------------------------------------------- #
def _mic_pump(loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue",
              stop: threading.Event) -> None:
    CHUNK = MIC_SR * 2 // 10  # 0.1s of 16kHz mono PCM16 = 3200 bytes
    while not stop.is_set():
        try:
            req = urllib.request.Request(f"{ROBOT_MIC_URL}/pcm")
            with urllib.request.urlopen(req, timeout=10) as r:
                print("[openai-rt] mic stream connected", flush=True)
                while not stop.is_set():
                    raw = r.read(CHUNK)
                    if not raw:
                        raise RuntimeError("mic stream ended")
                    # The session can end (and asyncio.run close the loop)
                    # while we're parked in that blocking read. Handing a
                    # closed loop to call_soon_threadsafe raises, and we used
                    # to treat that as a retryable blip and spin forever —
                    # one leaked thread per toggle, each pinning an open
                    # /pcm connection to the mic bridge.
                    if loop.is_closed():
                        return
                    up = _resample_pcm16(raw, MIC_SR, RT_SR)
                    if up:
                        loop.call_soon_threadsafe(queue.put_nowait, up)
        except Exception as e:  # noqa: BLE001
            if stop.is_set() or loop.is_closed():
                break
            print(f"[openai-rt] mic stream dropped ({e}); retrying in 2s",
                  flush=True)
            time.sleep(2)


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
class RealtimeSession:
    def __init__(self, on_user_text=None, on_agent_text=None, log=None):
        self.on_user_text = on_user_text or (lambda t: None)
        self.on_agent_text = on_agent_text or (lambda t: None)
        self.log = log or (lambda m: print(f"[openai-rt] {m}", flush=True))
        self._resp_pcm = bytearray()      # accumulates the current reply's audio
        self._cancelled = False           # current reply got barged-in
        # Set when the failure is one that retrying cannot fix. See the error
        # handler — the loop stops and the caller says so out loud.
        self.fatal: str | None = None
        self._speaking_until = 0.0        # wall-clock when our clip finishes
        self._loop = None                 # set once we're running
        self._announce_q = None           # finished background jobs, to announce
        self._response_active = False     # a reply is being generated right now

    def _session_update(self) -> dict:
        # GA Realtime schema (gpt-realtime): session.type="realtime", audio
        # config nested under audio.input / audio.output. The older beta shape
        # (flat modalities / input_audio_format / top-level voice + the
        # OpenAI-Beta: realtime=v1 header) is rejected as beta_api_shape_disabled.
        turn = {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 500,
            "create_response": True,
            "interrupt_response": True,   # server auto-cancels a reply on barge-in
        }
        instructions = (os.environ.get("OPENAI_RT_INSTRUCTIONS")
                        or DEFAULT_INSTRUCTIONS)
        # Lessons taught in earlier conversations ride along in the prompt, so
        # a `remember` from last night is in force on tonight's first word.
        skills = reachy_agent.load_skills()
        if skills:
            instructions += ("\n\nThings you've been taught in earlier "
                             "conversations — honour these:\n" + skills)
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions,
                "tools": TOOLS,
                "tool_choice": "auto",
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": RT_SR},
                        "turn_detection": turn,
                        # whisper-1 is the old default and noticeably worse at names,
                        # which for a robot that greets people by face is the one thing
                        # it must get right.
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": RT_SR},
                        "voice": VOICE,
                    },
                },
            },
        }

    async def _sender(self, ws, queue, should_run, stop):
        """Drain mic chunks → append to the input buffer."""
        while should_run() and not stop.is_set():
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            # Optional half-duplex gate: don't feed the mic while our own clip
            # is playing, so the robot doesn't hear itself and self-trigger.
            if GATE_ON_SPEAK and time.time() < self._speaking_until:
                continue
            try:
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode(),
                }))
            except Exception:
                return

    def is_speaking(self) -> bool:
        """A reply is being generated or is still coming out of the speaker."""
        return self._response_active or time.time() < self._speaking_until

    def _on_barge_in(self):
        """User started talking — abandon the in-flight reply and hush."""
        self._cancelled = True
        self._resp_pcm = bytearray()
        self._speaking_until = 0.0
        _stop_sound()

    async def _flush_reply(self, loop):
        """Play the accumulated reply audio on the robot (in a worker thread so
        the receive loop keeps running and barge-in stays responsive)."""
        if self._cancelled or not self._resp_pcm:
            return
        pcm = bytes(self._resp_pcm)
        self._resp_pcm = bytearray()
        try:
            dur = await loop.run_in_executor(None, _play_pcm_on_robot, pcm)
            self._speaking_until = time.time() + dur + 0.3
        except Exception as e:  # noqa: BLE001
            self.log(f"playback failed: {e}")

    # ----------------------------------------------------------------- #
    # Tool calling
    # ----------------------------------------------------------------- #
    async def _handle_function_call(self, ws, loop, msg) -> None:
        """Run the requested tool and hand the result back, then ask for a
        spoken follow-up. Every tool is built to return in milliseconds — the
        slow one (improve_yourself) returns as soon as the job is *queued* —
        so this never stalls the receive loop for long."""
        name = msg.get("name") or ""
        call_id = msg.get("call_id") or ""
        raw = msg.get("arguments") or "{}"
        try:
            args = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            args = {}
        self.log(f"tool → {name}({raw[:160]})")

        result = await loop.run_in_executor(
            None, _dispatch_tool, name, args, self._announce_cb)
        self.log(f"tool ← {name}: {str(result)[:160]}")

        try:
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": str(result),
                },
            }))
            await ws.send(json.dumps({"type": "response.create"}))
        except Exception as e:  # noqa: BLE001
            self.log(f"failed to return tool result: {e}")

    def _announce_cb(self, job: dict) -> None:
        """Called from reachy_agent's worker thread when a background coding
        job lands. Hops onto the event loop; the announcer does the talking."""
        loop, q = self._loop, self._announce_q
        if loop is None or q is None:
            return
        try:
            loop.call_soon_threadsafe(q.put_nowait, job)
        except RuntimeError:
            pass  # loop already closed — mode was toggled off mid-job

    async def _announcer(self, ws, should_run, stop):
        """Vibey volunteering news. When the coding agent finishes, they bring
        it up themselves instead of waiting to be asked — this is the whole
        point of dispatching work mid-conversation."""
        while should_run() and not stop.is_set():
            try:
                job = await asyncio.wait_for(self._announce_q.get(), timeout=0.3)
            except asyncio.TimeoutError:
                continue
            except Exception:
                return
            # Don't talk over an in-flight reply; let it land first.
            for _ in range(100):  # ≤10s, then say it anyway
                if not self._response_active and time.time() >= self._speaking_until:
                    break
                await asyncio.sleep(0.1)
            spoken = (job.get("spoken") or "").strip() or "I finished that one."
            ok = job.get("state") == "done"
            nudge = (
                f"[Background note, not spoken by anyone in the room: your "
                f"coding job just {'finished successfully' if ok else 'failed'}. "
                f"The agent's own words: \"{spoken}\". Bring this up now, "
                f"unprompted, in ONE short sentence in your own voice. If it "
                f"succeeded, invite them to try it.]")
            try:
                await ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": nudge}],
                    },
                }))
                await ws.send(json.dumps({"type": "response.create"}))
                self.log(f"announced job {job.get('id')} ({job.get('state')})")
            except Exception as e:  # noqa: BLE001
                self.log(f"announce failed: {e}")

    async def _receiver(self, ws, loop, should_run, stop):
        # Poll with a short timeout (rather than `async for raw in ws`) so we
        # notice the mode being toggled off promptly even when the socket is
        # idle — e.g. no mic audio flowing because the robot link is down.
        while should_run() and not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.4)
            except asyncio.TimeoutError:
                continue
            except Exception:
                return  # socket closed or errored — let run_async reconnect/exit
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            t = msg.get("type", "")

            # --- user started talking → barge-in ---
            if t == "input_audio_buffer.speech_started":
                self._on_barge_in()

            # --- a new reply begins ---
            elif t == "response.created":
                self._cancelled = False
                self._response_active = True
                self._resp_pcm = bytearray()

            # --- the model wants to use a tool ---
            elif t == "response.function_call_arguments.done":
                await self._handle_function_call(ws, loop, msg)

            # --- streamed reply audio (accept classic + GA event names) ---
            elif t in ("response.audio.delta", "response.output_audio.delta"):
                if not self._cancelled:
                    self._resp_pcm.extend(base64.b64decode(msg.get("delta", "")))

            # --- reply audio finished → play it ---
            elif t in ("response.audio.done", "response.output_audio.done",
                       "response.done"):
                if t == "response.done":
                    self._response_active = False
                await self._flush_reply(loop)

            # --- transcripts, for the dashboard log ---
            elif t == "conversation.item.input_audio_transcription.completed":
                txt = (msg.get("transcript") or "").strip()
                if txt:
                    self.on_user_text(txt)
            elif t in ("response.audio_transcript.done",
                       "response.output_audio_transcript.done"):
                txt = (msg.get("transcript") or "").strip()
                if txt:
                    self.on_agent_text(txt)

            elif t == "error":
                err = msg.get("error") or {}
                self.log(f"server error: {err}")
                # Some errors will never come right by trying again. No credit is
                # the obvious one, and a bad key the other; retrying either every
                # three seconds forever is how the dashboard ends up saying
                # "Realtime OpenAI" while the robot answers in a different voice
                # from a different brain, with nothing anywhere saying why.
                if err.get("code") in ("credit_balance_exhausted", "insufficient_quota",
                                       "invalid_api_key", "account_deactivated"):
                    self.fatal = (err.get("message")
                                  or "OpenAI refused the connection.")

    async def run_async(self, should_run, stop):
        import websockets

        loop = asyncio.get_running_loop()
        self._loop = loop
        self._announce_q = asyncio.Queue()
        queue: "asyncio.Queue" = asyncio.Queue()
        pump = threading.Thread(
            target=_mic_pump, args=(loop, queue, stop), daemon=True)
        pump.start()

        # An ephemeral key on the wire, not the account key.
        #
        # A realtime session is a WebSocket held open for as long as somebody is
        # talking, and the robot is a device in a room other people walk into. Minting
        # a short-lived `ek_` means the thing on the socket cannot be reused if it ever
        # leaks. Falls back to the standard key rather than refusing to talk — see
        # FlowState's docs/API-CONTRACT.md, which is where this endpoint is written
        # down after /v1/realtime/sessions turned out to be a 404.
        headers = {"Authorization": f"Bearer {_ephemeral_token() or API_KEY}"}
        while should_run() and not stop.is_set() and not self.fatal:
            try:
                self.log(f"connecting to OpenAI Realtime ({MODEL}, voice={VOICE}) …")
                async with websockets.connect(
                        WS_URL, additional_headers=headers,
                        max_size=16 * 1024 * 1024) as ws:
                    await ws.send(json.dumps(self._session_update()))
                    self.log("connected — full-duplex, just talk")
                    sender = asyncio.ensure_future(
                        self._sender(ws, queue, should_run, stop))
                    announcer = asyncio.ensure_future(
                        self._announcer(ws, should_run, stop))
                    try:
                        await self._receiver(ws, loop, should_run, stop)
                    finally:
                        sender.cancel()
                        announcer.cancel()
            except Exception as e:  # noqa: BLE001
                if not should_run() or stop.is_set():
                    break
                text = str(e)
                if any(k in text for k in ("insufficient_quota",
                                           "credit_balance_exhausted",
                                           "invalid_api_key")):
                    self.fatal = ("OpenAI has no credit left on this account."
                                  if "credit" in text or "quota" in text
                                  else "OpenAI rejected the API key.")
                    break
                self.log(f"connection error ({e}); retrying in 3s")
                await asyncio.sleep(3)
        _stop_sound()
        self.log("stopped")


def run(should_run=None, on_user_text=None, on_agent_text=None, log=None,
        stop_event: "threading.Event | None" = None) -> None:
    """Blocking entry point. Runs the full-duplex loop until should_run() turns
    False (or stop_event is set, or KeyboardInterrupt). Safe to call from
    reachy_chat.py's main loop to hand off the mic/speaker."""
    if not API_KEY:
        (log or print)("[openai-rt] OPENAI_API_KEY not set — cannot start")
        return
    caller_should_run = should_run or (lambda: True)
    SLEEP_REQUESTED.clear()
    stop = stop_event or threading.Event()
    session = RealtimeSession(on_user_text, on_agent_text, log)

    # The conversation can end itself.
    #
    # Wrapping the caller's toggle rather than replacing it: the dashboard switch
    # still works, and "go to sleep" becomes a second, equal way to stop — said
    # from across the room, which is the only way that matters for a robot.
    #
    # And it hangs on until the farewell has actually been said. The tool result
    # comes back before the model has spoken a word about it, so "stop now" would
    # close the socket during the pause before "see you later". It waits for the
    # speaking to START (up to six seconds) and then for it to finish.
    def should_run_now() -> bool:
        if not caller_should_run():
            return False
        if not SLEEP_REQUESTED.is_set():
            return True
        if session.is_speaking():
            return True
        return time.time() - SLEEP_REQUESTED_AT < 6.0

    should_run = should_run_now
    try:
        asyncio.run(session.run_async(should_run, stop))
        if session.fatal:
            (log or print)(f"[openai-rt] giving up: {session.fatal}")
            FATAL_REASON["why"] = session.fatal
    except KeyboardInterrupt:
        _stop_sound()
    finally:
        # Must happen on EVERY exit path, not just Ctrl-C: the mic pump is a
        # separate thread and this event is the only thing that tells it to
        # stop once the loop it feeds is gone.
        stop.set()
        if SLEEP_REQUESTED.is_set():
            (log or print)("[openai-rt] asked to sleep — session closed")


if __name__ == "__main__":
    print("[openai-rt] standalone mode — Ctrl-C to quit")
    run(on_user_text=lambda t: print(f"[you] {t}", flush=True),
        on_agent_text=lambda t: print(f"[vibey] {t}", flush=True))
