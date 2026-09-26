#!/usr/bin/env python3
"""Vibey on GPT-Live: the voice layer speaks, a backend model thinks and calls tools.

    mic ── PCM16 24kHz ──▶  wss://api.openai.com/v1/live/sessions  ──▶ robot speaker
                                    │
                                    └── delegation ──▶ Responses backend (gpt-5.5 by
                                                        default) which calls OUR tools,
                                                        answered over the same socket

Measured against gpt-realtime-2.1-mini on the same question, same voice, same
prompt: first audio in 0.5s instead of 1.28s, and a reply that stops after one
sentence instead of running to thirteen seconds. Priced per minute of voice
(about $3/hour) plus the backend's tokens.

It is a different API, not a different model string. Its own endpoint, its own
event names, and — the part that shapes this file — it does not run tools. It
delegates to a backend model you name, and that model calls the functions. So
everything here is the Realtime session with the wire format swapped: the mic
pump, the speaker path, the 26 tools, the nudges and the dispatch are all
inherited from reachy_openai_realtime.

Two things learned by probing rather than reading:

The voice layer will happily say "certainly, consider it done" and do nothing.
With a plain persona prompt it never delegated at all. The instructions have to
say, in so many words, that it has no hands — see LIVE_RULES.

And it never speaks unprompted. A `response.create` with no voice turn behind
it runs the backend and produces text nobody hears. Room events therefore go
in as `session.commentary.append`, which is the channel built for exactly that:
speakable context the model brings up itself.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time

import reachy_openai_realtime as rt
from reachy_openai_realtime import (  # noqa: F401 — re-exported on purpose
    API_KEY, VOICE, RT_SR, TOOLS, DEFAULT_INSTRUCTIONS, RealtimeSession,
    LIVE_SESSION, SLEEP_REQUESTED, FATAL_REASON, _mic_pump, _dispatch_tool,
    _stop_sound, voice_detection_active, GATE_ON_SPEAK,
)

LIVE_URL = "wss://api.openai.com/v1/live/sessions"
LIVE_MODEL = os.environ.get("OPENAI_LIVE_MODEL", "gpt-live-1").strip()
# The model that thinks. Live only speaks.
BACKEND_MODEL = os.environ.get("OPENAI_LIVE_BACKEND", "gpt-5.5").strip()

LIVE_RULES = (
    "\n\nIMPORTANT — HOW YOU WORK: you are the voice only. You have no hands and "
    "no tools of your own. You cannot play music, move, look around, remember "
    "anything, check anything, or change anything yourself. Whenever someone asks "
    "you to DO anything at all, delegate it to your backend and wait for its "
    "result. Never say something is done, playing, remembered or checked unless "
    "the backend has told you so. While you wait you may say a brief holding "
    "word — 'just a tick' — and nothing more."
)

# How long the output stream may go quiet before what has arrived is played.
# The robot's speaker takes whole clips (upload + play), not a stream, and Live
# streams continuously with no end-of-reply event, so gaps are the boundaries.
FLUSH_GAP_S = 0.28


class LiveSession(RealtimeSession):
    """The Realtime session, on the Live wire format."""

    def __init__(self, on_user_text=None, on_agent_text=None, log=None):
        super().__init__(on_user_text, on_agent_text, log)
        self._heard = []      # user transcript deltas for the current turn
        self._said = []       # agent transcript deltas for the current clip
        self._last_delta_at = 0.0
        self._minutes = 0.0

    # ----------------------------------------------------------------- #
    # Session
    # ----------------------------------------------------------------- #
    def _session_start(self) -> dict:
        base = self._session_update()          # reuse: instructions with lessons etc.
        instructions = (base.get("session", {}).get("instructions")
                        or DEFAULT_INSTRUCTIONS) + LIVE_RULES
        tools = base.get("session", {}).get("tools") or TOOLS
        # Realtime tool specs are already the Responses function shape.
        fn_tools = [{"type": "function", "name": t["name"],
                     "description": t.get("description", ""),
                     "parameters": t.get("parameters", {"type": "object", "properties": {}})}
                    for t in tools if t.get("type") == "function"]
        return {"type": "session.start", "session": {
            "model": LIVE_MODEL,
            "instructions": instructions,
            "audio": {"output": {"voice": VOICE}},
            "delegation": {"type": "responses", "responses": {
                "model": BACKEND_MODEL,
                "instructions": (
                    "You are Vibey's brain, working for the voice in the room. Use "
                    "the tools to do what was asked, then answer in one or two short "
                    "sentences the voice can say. Never invent a result — if a tool "
                    "says it could not do something, say that."),
                "tools": fn_tools,
            }},
        }}

    async def run_async(self, should_run, stop):
        import websockets

        loop = asyncio.get_running_loop()
        self._loop = loop
        rt.set_voice_detection(True)
        self._announce_q = asyncio.Queue()
        queue: "asyncio.Queue" = asyncio.Queue()
        import threading
        pump = threading.Thread(target=_mic_pump, args=(loop, queue, stop), daemon=True)
        pump.start()

        attempt = 0
        while should_run() and not stop.is_set() and not self.fatal:
            try:
                self.log(f"connecting to GPT-Live ({LIVE_MODEL} → {BACKEND_MODEL}, voice={VOICE}) …")
                async with websockets.connect(
                        LIVE_URL,
                        additional_headers={"Authorization": f"Bearer {API_KEY}"},
                        max_size=16 * 1024 * 1024) as ws:
                    await ws.send(json.dumps(self._session_start()))
                    FATAL_REASON["why"] = None
                    sender = asyncio.ensure_future(self._sender(ws, queue, should_run, stop))
                    announcer = asyncio.ensure_future(self._announcer(ws, should_run, stop))
                    flusher = asyncio.ensure_future(self._flusher(ws, loop, should_run, stop))
                    try:
                        await self._receiver(ws, loop, should_run, stop)
                    finally:
                        for t in (sender, announcer, flusher):
                            t.cancel()
            except Exception as e:  # noqa: BLE001
                if not should_run() or stop.is_set():
                    break
                text = str(e)
                if any(k in text for k in ("insufficient_quota", "credit_balance_exhausted",
                                           "invalid_api_key")):
                    self.fatal = ("OpenAI has no credit left on this account."
                                  if "credit" in text or "quota" in text
                                  else "OpenAI rejected the API key.")
                    break
                attempt += 1
                wait = min(3 * (2 ** min(attempt - 1, 5)), 90)
                self.log(f"connection error ({e}); retrying in {wait}s")
                await asyncio.sleep(wait)
            else:
                attempt = 0
        _stop_sound()
        self.log("stopped")

    # ----------------------------------------------------------------- #
    # Mic → socket
    # ----------------------------------------------------------------- #
    async def _sender(self, ws, queue, should_run, stop):
        while should_run() and not stop.is_set():
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if not voice_detection_active():
                continue
            if GATE_ON_SPEAK and time.time() < self._speaking_until:
                chunk = b"\x00" * len(chunk)
            try:
                await ws.send(json.dumps({"type": "session.input_audio.append",
                                          "audio": base64.b64encode(chunk).decode()}))
            except Exception:
                return

    # ----------------------------------------------------------------- #
    # Socket → speaker
    # ----------------------------------------------------------------- #
    async def _flusher(self, ws, loop, should_run, stop):
        """Plays what has arrived once the stream pauses. Live has no
        response.done; the gap between sentences is the only boundary there is."""
        while should_run() and not stop.is_set():
            await asyncio.sleep(0.05)
            if self._resp_pcm and time.time() - self._last_delta_at > FLUSH_GAP_S:
                said = "".join(self._said).strip()
                self._said = []
                self._response_active = False
                await self._flush_reply(loop)
                if said:
                    self.on_agent_text(said)

    async def _receiver(self, ws, loop, should_run, stop):
        while should_run() and not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.4)
            except asyncio.TimeoutError:
                continue
            except Exception:
                return
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")

            if t == "session.started":
                self.log("connected — GPT-Live, full-duplex, just talk")

            elif t == "session.output_audio.delta":
                # Audio arriving while our last clip is still playing is a new
                # sentence, not a barge-in — Live decides turns itself.
                self._cancelled = False
                self._response_active = True
                self._resp_pcm.extend(base64.b64decode(msg.get("delta", "")))
                self._last_delta_at = time.time()

            elif t == "session.output_transcript.delta":
                self._said.append(msg.get("delta", ""))

            elif t == "session.input_transcript.delta":
                d = msg.get("delta", "")
                self._heard.append(d)
                # The person is talking over the clip: Live has already stopped
                # generating, so stop the speaker too.
                if time.time() < self._speaking_until and self._heard and len("".join(self._heard)) > 12:
                    self._on_barge_in()
                if d.rstrip().endswith((".", "?", "!")):
                    text = "".join(self._heard).strip()
                    self._heard = []
                    if text:
                        self.on_user_text(text)

            elif t == "session.delegation.created":
                # Whatever was heard is now a turn, punctuation or not.
                text = "".join(self._heard).strip()
                self._heard = []
                if text:
                    self.on_user_text(text)

            elif t == "response.event":
                ev = msg.get("event") or {}
                if (ev.get("type") == "response.output_item.done"
                        and (ev.get("item") or {}).get("type") == "function_call"):
                    await self._handle_function_call(ws, loop, ev["item"])

            elif t == "session.usage.updated":
                u = msg.get("usage") or {}
                self._minutes = float(u.get("total_minutes") or u.get("minutes") or self._minutes)

            elif t == "error":
                err = msg.get("error") or {}
                self.log(f"server error: {err}")
                if err.get("code") in ("invalid_api_key", "insufficient_quota"):
                    self.fatal = f"OpenAI: {err.get('message')}"
                    return

    # ----------------------------------------------------------------- #
    # Tools: same dispatcher, different envelope
    # ----------------------------------------------------------------- #
    async def _handle_function_call(self, ws, loop, item) -> None:
        name = item.get("name") or ""
        call_id = item.get("call_id") or ""
        raw = item.get("arguments") or "{}"
        try:
            args = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            args = {}
        self.log(f"tool → {name}({raw[:160]})")
        result = await loop.run_in_executor(None, _dispatch_tool, name, args, self._announce_cb)
        self.log(f"tool ← {name}: {str(result)[:160]}")
        try:
            await ws.send(json.dumps({"type": "response.item.create", "item": {
                "type": "function_call_output", "call_id": call_id, "output": str(result)}}))
            await ws.send(json.dumps({"type": "response.create"}))
        except Exception as e:  # noqa: BLE001
            self.log(f"failed to return tool result: {e}")

    # ----------------------------------------------------------------- #
    # Room events: the channel Live built for them
    # ----------------------------------------------------------------- #
    async def _announcer(self, ws, should_run, stop):
        while should_run() and not stop.is_set():
            try:
                job = await asyncio.wait_for(self._announce_q.get(), timeout=0.3)
            except asyncio.TimeoutError:
                continue
            except Exception:
                return
            for _ in range(100):
                if not self._response_active and time.time() >= self._speaking_until:
                    break
                await asyncio.sleep(0.1)
            if job.get("nudge"):
                text = job["nudge"]
            elif job.get("note"):
                text = job["note"]
            else:
                spoken = (job.get("spoken") or "").strip() or "I finished that one."
                ok = job.get("state") == "done"
                text = (f"Your coding job just {'finished' if ok else 'failed'}. "
                        f"The agent said: \"{spoken}\". Mention it in one sentence.")
            try:
                await ws.send(json.dumps({"type": "session.commentary.append",
                                          "content": text[:1800], "delegation_id": None}))
            except Exception as e:  # noqa: BLE001
                self.log(f"commentary failed: {e}")


def run(should_run=None, on_user_text=None, on_agent_text=None, log=None,
        stop_event=None) -> None:
    """Same contract as reachy_openai_realtime.run, so reachy_chat can pick
    either by name."""
    import threading
    if not API_KEY:
        (log or print)("[openai-live] OPENAI_API_KEY not set — cannot start")
        return
    caller_should_run = should_run or (lambda: True)
    SLEEP_REQUESTED.clear()
    stop = stop_event or threading.Event()
    session = LiveSession(on_user_text, on_agent_text, log)
    LIVE_SESSION["session"] = session

    def should_run_now() -> bool:
        if not caller_should_run():
            return False
        if not SLEEP_REQUESTED.is_set():
            return True
        if session.is_speaking():
            return True
        return time.time() - rt.SLEEP_REQUESTED_AT < 2.0

    try:
        asyncio.run(session.run_async(should_run_now, stop))
        if session.fatal:
            (log or print)(f"[openai-live] giving up: {session.fatal}")
            FATAL_REASON["why"] = session.fatal
    except KeyboardInterrupt:
        _stop_sound()
    finally:
        stop.set()
        LIVE_SESSION["session"] = None


if __name__ == "__main__":
    run(log=lambda m: print(m, flush=True))
