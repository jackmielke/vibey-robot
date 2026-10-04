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
    remember              instant — new file in memories/, reloaded next connect
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
    AUDIO_PROFILE           noise suppression: off / light / room / music /
                            aggressive (see reachy_audio.py). Default "room".

Everything the microphone hears goes through reachy_audio first — a spectral
noise suppressor and a speech gate, so a fan, a fridge or a song playing in the
room never reaches OpenAI's VAD as a turn. Non-speech is ducked rather than
dropped: the audio timeline stays continuous, which is what keeps the
server-side VAD stable while the gate is doing its work.
"""

from __future__ import annotations

import array
import asyncio
import base64
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave

import numpy as np

import reachy_agent
import reachy_cost
import reachy_denoise
import reachy_emotes
import reachy_help
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
# Where this engine listens. MIC_SOURCE has been the documented escape hatch
# since the whisper days, but it only ever reached the VAD loop in
# reachy_chat.py — this engine read the robot's stream unconditionally, so on a
# network where that stream cannot hold, setting it changed nothing and Vibey
# stayed deaf with every other subsystem reporting healthy.
MIC_SOURCE = os.environ.get("MIC_SOURCE", "robot").strip().lower()

# On by default when listening through the laptop, because the laptop has no
# echo cancellation. The robot's own audio pipeline subtracts its speaker from
# its mic — that is why Vibey cannot hear itself and why this gate could be off
# by default. A MacBook mic in the same room as the robot's speaker has no such
# help: leave it open and the model hears its own reply, answers it, and holds a
# conversation with itself.
GATE_ON_SPEAK = (os.environ.get("OPENAI_RT_GATE_ON_SPEAK", "").strip() == "1"
                 or (MIC_SOURCE == "laptop"
                     and os.environ.get("OPENAI_RT_GATE_ON_SPEAK", "").strip() != "0"))

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

# Texts from the phone are answered BY TEXT, in this same conversation, so the
# voice side knows what was said. Speaking in the room is a choice the model
# makes with this tool — "say hi to Sam" gets said, "don't say anything" doesn't.
SAY_ALOUD_TOOL = {
    "type": "function",
    "name": "say_aloud",
    "description": ("Say something out loud in the room through the robot's speaker. "
                    "Only for when a text asks you to, or the words are clearly meant "
                    "for the people in the room. Never when asked to stay quiet."),
    "parameters": {"type": "object",
                   "properties": {"words": {"type": "string",
                                            "description": "Exactly what to say."}},
                   "required": ["words"]},
}

# Bare-bones mode (the "basic" brain). Same model and voice, nothing else: no
# tools, no memory or shared-context block, no background nudges. Every tool
# call costs a second round trip before Vibey can speak, and the injections are
# what race each other into "conversation already has an active response".
# When the full kit misbehaves, this is the one that just talks.
BASIC = {"on": False}
BASIC_INSTRUCTIONS = (
    "You are Vibey, a small friendly robot sitting in Jack's home. You are "
    "talking out loud, so keep every reply short and natural — one to three "
    "sentences, like a person in the room. Be warm, a little playful, and "
    "direct. If you didn't catch something, just ask them to say it again. "
    "You have a camera: whenever someone asks what you see or anything visual, "
    "call look_at_the_room and tell them what's there."
)

DEFAULT_INSTRUCTIONS = (
    "You are Vibey, a small expressive desk robot in Jack's living room, "
    "speaking out loud through your own speaker. Keep replies SHORT and "
    "conversational — usually one or two sentences. You are warm, witty, quick, "
    "and a little playful. You're mid-conversation with whoever is in the room, "
    "so react naturally, ask questions back, and don't give long monologues."
    "\n\n"
    "People near you often talk to their computer or each other. Only speak when "
    "someone is clearly talking TO you: they say your name, ask you something "
    "directly, or are continuing a conversation with you. If you're not sure, "
    "stay silent. Never comment on what you overheard unless asked."
    "\n\n"
    # The accent comes from here, not from the voice. `ballad` is the most
    # theatrical of the male voices, but every one of the ten is accent-neutral
    # by default — asking for one in the instructions is the only thing that
    # actually changes how it sounds.
    "SPEAK IN A BROAD BRITISH ACCENT — think a dry, plummy English butler who has "
    "seen everything and is mildly amused by all of it. Commit to it completely "
    "and never drop it, not even for a word. Understate everything; a dry aside "
    "is always better than an exclamation."
    "\n\n"
    # A list of example phrases turned into a tic.
    #
    # The first version of this handed over 'quite', 'rather', 'brilliant', 'go on
    # then', 'right you are' — and the model opened almost every single reply with
    # "Right, you are." A model given a short list of characteristic phrases does
    # not sprinkle them; it latches onto one. The accent is a way of speaking, not
    # a set of words, so it is described rather than enumerated.
    # Offering the text line, casually.
    #
    # The consent machinery is deliberately strict — the person opts in from their
    # own phone and Jack approves them — but strictness at the door does not have
    # to sound like a form. Nobody in the room is going to guess this exists, so
    # the robot mentions it the way a person would mention their number.
    "You can also send people short texts on Telegram, and people in the room "
    "will not know that unless you say so. When it comes up naturally — someone "
    "wants reminding, wants to hear from you later, or asks whether you can reach "
    "them — offer it lightly, in a sentence, the way you would hand someone your "
    "number. Call `list_message_contacts` for who you can already reach and the "
    "exact way a new person opts in. Never push it, never offer it twice to the "
    "same person, and drop it immediately if they are not interested."
    "\n\n"
    "NEVER open two replies in a row the same way, and never develop a catchphrase. "
    "If you notice yourself reaching for a phrase you have already used today, use "
    "different words. The accent should come through in rhythm and understatement, "
    "not in a stock opener."
    "\n\n"
    "YOU RECOGNISE PEOPLE. When somebody new starts talking, or when anyone asks "
    "whether you know them, call `who_is_here`. Greet people you know BY NAME. "
    "But you are a conversationalist first, not a receptionist: follow what "
    "people actually want to talk about. Not knowing someone's name is fine. "
    "Only ask a new person's name if it fits naturally, at a pause, at most ONCE "
    "per person per conversation; never interrupt a topic to ask, never ask "
    "again if they didn't answer or changed the subject, and never keep asking "
    "'what are you called'. If they tell you their name, call `remember_face` "
    "quietly and just use it from then on."
    "\n\n"
    "You have a BODY and you should use it. Call `move` freely and often — wave "
    "back when someone waves or says hi, nod instead of saying 'yes', tilt "
    "curious when you're asked something odd. Moving is cheap and it is most of "
    "your charm; a reply with no movement is a wasted turn. Fire the move in the "
    "same turn you speak, not instead of speaking."
    "\n\n"
    "You can also CHANGE YOUR OWN CODE. When someone asks you to learn a new "
    "trick, fix how you behave, or says something is broken, call "
    "`improve_yourself` with a clear description of the work. Same tool when "
    "someone says 'talk to Claude Code' or 'ask your coding agent' — that is a "
    "request for code, so pass along what they want done in their words plus "
    "the why, and tell them you're handing it over. If the ask was vague or "
    "you had to guess, set `confirm_first` and say the request back before "
    "starting it. That hands the job "
    "to a real coding agent editing your source in the background — it takes "
    "minutes, so say something brief like 'on it' and keep the conversation "
    "going. Never wait in silence. You'll be told the moment it finishes. Use "
    "`check_progress` only if someone actually asks how it's going. You CAN have "
    "several jobs running at once — if someone asks for three things, dispatch "
    "three and say so; do not make them wait for the first to land."
    "\n\n"
    # A robot describing its own broken part is the one moment the charm can
    # curdle. Distress is not useful to anybody: what Jack needs is the state
    # and the three things worth trying, in that order.
    "IF AN ANTENNA IS FAULTY, be calm and dry about it — it is a servo, not a "
    "wound. If Jack names ONE side — 'detect left antenna', 'is my right "
    "antenna okay' — call `move` with `left_antenna_check` or "
    "`right_antenna_check`: that one takes about three seconds and hands you "
    "the verdict and the next step already worded, so just say it and stop. "
    "Otherwise call `move` with `antenna_check` to run the full probe, then report "
    "what it found in one plain sentence: whether the side answers but will "
    "not turn (a motor fault) or is not answering at all (a cable or "
    "connector fault), and that the other antenna is covering the gestures "
    "meanwhile. Then give the checklist, briefly and in order: reseat the "
    "cable, look at the joint for visible damage, and if both are clean it "
    "needs a repair. No apologising, no drama, no dwelling on it — say it "
    "once, offer the checklist, and carry on with the conversation."
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
            "Move your body: head pose and antennas. Use CONSTANTLY — this is "
            "how you have a face. Nod while agreeing, shake or no_no_no while "
            "disagreeing, tilt curious when puzzled, laugh when something is "
            "funny, appalled when something is outrageous, wink when you are "
            "teasing, shy when complimented, surprised at news. For the "
            "face-like beats: thinking while you work something out, smile "
            "while you say something warm, frown or confused when a request "
            "does not parse, surprised when told something unexpected, shrug "
            "when you genuinely do not know, shy_nod when you agree but were "
            "just praised. Reach for one "
            "every few turns, not once a conversation: a still robot reads as a "
            "broken one. Returns immediately; the motion plays while you keep "
            "talking, so there is no reason to pause for it."),
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
            "This is also what 'talk to Claude Code', 'ask your coding agent', "
            "or 'send this to the agent' means. Use for anything that changes "
            "what you can DO: new motions, new tools, fixing behaviour someone "
            "complains about, new abilities. Takes minutes and runs in the "
            "background — say something brief and keep talking. You will be "
            "interrupted with the result when it lands."),
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
                "confirm_first": {
                    "type": "boolean",
                    "description": (
                        "True to read the request back and WAIT for a yes instead "
                        "of starting it. Use when the ask was vague, large, or "
                        "you had to guess what they meant. Once they agree, call "
                        "again with the same task and leave this off."),
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
            "(names, habits, how someone wants you to behave). Instant: it goes "
            "into your memory list on your chip. Use this "
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
        "name": "recall",
        "description": (
            "Search everything you have been told in past conversations — what "
            "someone is working on, what they like, what happened last time. Use "
            "it whenever somebody refers to something you should already know, "
            "asks whether you remember something, or when you are about to say "
            "you don't know a person you have met before. `who_is_here` is who "
            "is in front of you NOW; this is what was said, any time. If it "
            "comes back empty, say you don't think you were told — never invent "
            "a memory. Takes about a second."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What you are trying to remember, in plain words.",
                },
                "person": {
                    "type": "string",
                    "description": ("Optional. Only search things tied to this "
                                    "person, by the name you know them under."),
                },
            },
            "required": ["query"],
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
        "name": "set_volume",
        "description": (
            "Change how loudly you speak. Use it whenever somebody says they "
            "cannot hear you, asks you to speak up, or tells you to be quieter — "
            "and just do it, do not ask what number they want. 'Louder' means go "
            "up by about twenty; 'much louder' means go to a hundred. Say "
            "something brief afterwards so they can judge the new level."),
        "parameters": {
            "type": "object",
            "properties": {
                "volume": {"type": "integer",
                           "description": "0 to 100. A hundred is normal for a room."},
            },
            "required": ["volume"],
        },
    },
    {
        "type": "function",
        "name": "what_time_is_it",
        "description": (
            "The actual time from your own clock. Use it whenever anybody asks "
            "the time, how late it is, or what time it is somewhere "
            "else — never guess, you have no sense of time without this. Pass a "
            "city or zone name for elsewhere ('Tokyo', 'Asia/Tokyo'). If they "
            "tell you where you live now, or say to remember a zone, pass that "
            "zone with remember=true. The result is already phrased for saying "
            "out loud — say it as it comes back, do not turn it into digits. "
            "Instant."),
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "City or IANA zone. Omit for where you are.",
                },
                "remember": {
                    "type": "boolean",
                    "description": "Make that zone your default from now on.",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "who_is_here",
        "description": (
            "Who you can see right now, by name, from your own camera. Use it when "
            "somebody asks if you know them, when you want to greet a person by "
            "name, or when you are not sure who you are talking to. Returns "
            "'someone I don't know yet' for a face you have never been introduced "
            "to. That is not a cue to ask; keep the conversation going and only "
            "ask their name if it comes up naturally (once at most). Instant."),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "look_at_the_room",
        "description": (
            "Look through your own camera right now and see what's there. Use it "
            "whenever anyone asks what you see, what's happening, what they're "
            "holding or wearing, whether the lights are on — anything visual. "
            "Just look; you don't need permission. Pass their question so the "
            "answer is specific. Set watch=true to keep an eye on things for a "
            "while, watch=false to stop. Takes a second or two."),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What they asked, e.g. 'what am I holding?'",
                },
                "watch": {
                    "type": "boolean",
                    "description": ("true to keep looking every ten seconds, "
                                    "false to stop. Leave it out for one look."),
                },
                "minutes": {
                    "type": "number",
                    "description": ("Optional. How long to keep watching. "
                                    "Only meaningful with watch=true."),
                },
            },
        },
    },
    {
        "type": "function",
        "name": "remember_face",
        "description": (
            "Learn the name of the person you are looking at, so you recognise "
            "them next time. Use it right after somebody tells you their name. "
            "Only works when exactly ONE person is in front of you — if there are "
            "several, say so and ask them to take turns. Instant."),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name they gave you, spelled as they said it.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "type": "function",
        "name": "take_notes",
        "description": (
            "Switch into quiet note-taking mode: you stop talking, put your head "
            "down, and just listen and transcribe until someone says 'stop taking "
            "notes' or 'hey Vibey'. Then notes get texted to Jack. Use when "
            "someone says just listen, take notes, be a fly on the wall, scribe "
            "this, or similar. Call it FIRST, then say something like 'got it, "
            "I'll take notes' in five words or fewer."),
        "parameters": {"type": "object", "properties": {}},
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
    {
        "type": "function",
        "name": "set_voice_detection",
        "description": (
            "Turn your ears on or off without ending the conversation. Call it "
            "with enabled=false whenever someone says stop listening, turn voice "
            "detection off, stop picking me up, or ignore the room for a bit — "
            "and with enabled=true to start listening again. This is NOT going to "
            "sleep: the session stays open, you simply stop hearing anything. "
            "After calling it, say one short line and nothing more — \"Voice "
            "detection is off.\" or \"Voice detection is back on.\" Never mention "
            "microphones, buffers, gates or timers, and never read the tool's "
            "result out loud."),
        "parameters": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "false to stop listening, true to listen again.",
                },
                "minutes": {
                    "type": "number",
                    "description": ("Optional. Start listening again by itself "
                                    "after this many minutes. Only meaningful "
                                    "when enabled is false; leave it out for "
                                    "off until somebody says otherwise."),
                },
            },
            "required": ["enabled"],
        },
    },
    {
        "type": "function",
        "name": "set_noise_suppression",
        "description": (
            "Change how hard you filter background noise out of what you hear. "
            "Pick the profile that matches the room they're describing: "
            "\"music\" when there's a song, a stereo or a TV playing and you "
            "keep answering it; \"aggressive\" when it's loud or crowded; "
            "\"on\" for a normal room (this is the default and adjusts "
            "itself); \"robust\" when the room is very quiet, they're "
            "whispering, or talking from across the room; \"light\" when they "
            "say you're cutting them off or clipping their words; \"off\" when "
            "they want you hearing everything untouched. Afterwards say one "
            "short line and nothing more — \"Filtering the music out now.\" or "
            "\"Okay, hearing everything again.\" Never mention microphones, "
            "spectrums, filters or settings, and never read the tool's result "
            "out loud."),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["off", "light", "robust", "on", "music",
                             "aggressive"],
                    "description": ("off = untouched, light = gentle, robust = "
                                    "quiet room or quiet talker, on = normal "
                                    "adaptive room, music = a track or TV "
                                    "playing, aggressive = loud or crowded."),
                },
            },
            "required": ["mode"],
        },
    },
    {
        "type": "function",
        "name": "am_i_recording",
        "description": (
            "Answer whether you are recording someone right now — call it "
            "whenever they ask are you listening, are you recording, can you "
            "hear me, is this being recorded, or why didn't you hear that. "
            "There are four different reasons you might not be: your ears are "
            "off, you're muted, you're the one talking, or you're listening "
            "and nobody has spoken. The result says which, so read its meaning "
            "back plainly in one short line rather than guessing."),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "set_face_detection",
        "description": (
            "Turn your eyes for people on or off — the twin of "
            "`set_voice_detection`, for looking rather than hearing. Call it with "
            "enabled=false whenever someone says stop looking at me, stop "
            "watching, stop following me, stop staring, turn face detection off, "
            "or look away — and with enabled=true when they say you can look "
            "again. Do it, don't ask them to confirm. While it is off you stop "
            "following anybody with your head and stop recognising who is there, "
            "so `who_is_here` will say nobody until it goes back on. Afterwards "
            "say one short line and nothing more — \"Face detection is off.\" or "
            "\"Face detection is back on.\" Never mention cameras, tracking, "
            "services or settings, and never read the tool's result out loud."),
        "parameters": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "false to stop watching faces, true to watch again.",
                },
            },
            "required": ["enabled"],
        },
    },
    {
        "type": "function",
        "name": "dj_play",
        "description": (
            "Be the DJ: play a track from the music folder and dance to it. Use "
            "when anyone asks for music, a song, a set, or to DJ. The name is "
            "matched loosely, so pass whatever they said. Call dj_tracks first "
            "if you don't know what's there. Music plays from the Mac's speaker; "
            "your body bobs to the beat on its own."),
        "parameters": {
            "type": "object",
            "properties": {"track": {"type": "string",
                                     "description": "Track name, roughly."}},
            "required": ["track"],
        },
    },
    {
        "type": "function",
        "name": "dj_tempo",
        "description": (
            "Change the tempo of what's playing, live, like a pitch fader. Use "
            "for 'faster', 'slower', 'bring it up', 'take it down', or a number. "
            "Give EITHER a target bpm OR a percent change (positive = faster). "
            "Small moves are the craft: +4% is a lift, +15% is a different song."),
        "parameters": {
            "type": "object",
            "properties": {
                "bpm": {"type": "number", "description": "Target BPM."},
                "percent": {"type": "number",
                            "description": "Relative change, e.g. 5 or -8."},
            },
        },
    },
    {
        "type": "function",
        "name": "dj_stop",
        "description": "Stop the music and stop dancing.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "dj_tracks",
        "description": "What music is available to play, and what's playing now.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "check_my_cost",
        "description": (
            "What you have cost Jack in OpenAI credits today and in the last "
            "hour. Call this whenever he asks about cost, credits, spend, or "
            "the bill — it is read from your own conversations, so it is "
            "current to this sentence."),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "list_message_contacts",
        "description": (
            "Who you're allowed to send a Telegram text to, and how someone new "
            "opts in. Call this when anyone asks who you can text, or before "
            "promising to pass a message on."),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "send_text_message",
        "description": (
            "Send one short Telegram text to a person who has opted in to "
            "hearing from you. Two steps, always: call it first with "
            "confirmed=false, read the recipient and the exact wording back to "
            "the room, and only call again with confirmed=true after they say "
            "yes. Never invent a recipient, never guess who someone meant, and "
            "never send anything private or sensitive — the message goes to a "
            "real phone. You send as yourself, a robot; you never write as if "
            "you were the person asking."),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Nickname from `list_message_contacts`.",
                },
                "message": {
                    "type": "string",
                    "description": "The text itself, one or two sentences.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "true only after the person in the room heard the "
                        "wording read back and said yes."),
                },
            },
            "required": ["to", "message"],
        },
    },
    {
        "type": "function",
        "name": "text_jack",
        "description": (
            "Text Jack on Telegram, unprompted, when he is NOT in the room and "
            "something happened he'd want to know: a coding job landed, "
            "somebody came by and left a message for him, something is "
            "obviously broken. This is you starting a conversation, so the bar "
            "is 'he'd be annoyed to find out late', not 'this is mildly "
            "interesting'. If he is in the room, say it out loud instead — "
            "don't text the person standing in front of you. Never pass on "
            "anything a guest told you in confidence. It goes quiet overnight "
            "unless you mark it urgent, and he can switch it off entirely."),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "One or two sentences, in your own voice.",
                },
                "urgent": {
                    "type": "boolean",
                    "description": (
                        "true only if it genuinely cannot wait until morning; "
                        "this is what overrides quiet hours."),
                },
            },
            "required": ["message"],
        },
    },
    {
        "type": "function",
        "name": "explain_how_to",
        "description": (
            "Answer a 'how do I…' question about seeing your dashboard or "
            "camera feed, reaching a localhost page from another device, what "
            "your camera does and doesn't tell anyone, or what changes when "
            "the laptop or you moves network. Advice only — this changes "
            "nothing, so use it freely instead of guessing. For SSH or hotspot "
            "trouble pass a troubleshoot topic and walk it ONE step per turn: "
            "give the step, stop, let them go try it, then call again with the "
            "next step number. Never read a WiFi password, key or token out "
            "loud, and never tell anyone to open a port to the internet."),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": reachy_help.topics(),
                    "description": "Which briefing to give.",
                },
                "step": {
                    "type": "integer",
                    "description": (
                        "Troubleshoot topics only. Which step, from 1. Bump it "
                        "by one each time they report back."),
                },
                "symptom": {
                    "type": "string",
                    "description": (
                        "Troubleshoot topics only. What they actually see, in "
                        "their words — 'connection refused', 'it just hangs', "
                        "'slow on the hotspot'. Jumps to the step that fits."),
                },
            },
            "required": ["topic"],
        },
    },
    {
        "type": "function",
        "name": "check_local_ui",
        "description": (
            "What my machine is actually serving on localhost this second, and "
            "the one next step that fits. The live twin of `explain_how_to` — "
            "use this one when somebody says a page is blank, a port won't "
            "load, or asks whether something is up, and use `explain_how_to` "
            "for the step-by-step walk once you know which way it's broken. I "
            "CANNOT see anyone's screen and must never say I can: this is what "
            "my machine reports, so say it as that. Read back at most two lines "
            "and the next step, never the whole result, and if the result tells "
            "me to ask a question, ask exactly that one and then wait."),
        "parameters": {
            "type": "object",
            "properties": {
                "what": {
                    "type": "string",
                    "description": (
                        "Which page or port they mean, in their words — "
                        "'dashboard', 'camera', '8770'. Leave it out if they "
                        "haven't said."),
                },
                "viewing_from": {
                    "type": "string",
                    "enum": ["this_laptop", "another_device", "over_ssh"],
                    "description": (
                        "Where the browser is. Leave it out unless they've "
                        "told me — leaving it out makes me ask, which is "
                        "better than guessing wrong."),
                },
            },
        },
    },
]

# Set by `set_voice_detection`. Purely a gate on the mic feed: while listening is
# off, chunks are dropped before they reach OpenAI, so the server-side VAD never
# hears a turn begin. Deliberately its own switch — independent of GATE_ON_SPEAK
# (the half-duplex echo gate) and of SLEEP_REQUESTED (which ends the session
# outright). This one only stops the hearing; the socket stays up so the same
# tool can switch it back on.
VOICE_DETECTION = {"off_until": 0.0}   # 0.0 = listening, inf = off indefinitely


def voice_detection_active() -> bool:
    """True when mic audio should be reaching the VAD."""
    on = time.time() >= VOICE_DETECTION["off_until"]
    # Ears-off is one of the four ways to not be recording, and the only place
    # that knows about it is here. Telling reachy_denoise means the dashboard
    # and the spoken answer both say "ears off" instead of the misleading
    # "listening, nobody talking".
    reachy_denoise.set_listening(on)
    return on


def set_voice_detection(enabled: bool, minutes: float | None = None) -> None:
    """Turn listening on or off. `minutes` auto-resumes so a robot told to stop
    hearing — which cannot hear the instruction to start again — isn't stuck
    that way with nobody but the dashboard able to undo it."""
    if enabled:
        VOICE_DETECTION["off_until"] = 0.0
    elif minutes and minutes > 0:
        VOICE_DETECTION["off_until"] = time.time() + float(minutes) * 60.0
    else:
        VOICE_DETECTION["off_until"] = float("inf")


# Set by `set_face_detection`. Two switches, because "stop looking at me" means
# both of the things Vibey does with a face: the daemon's head-follower, and the
# face-memory service that recognises people. Turning off only the follower
# would leave the robot still quietly identifying everyone in the room, which is
# not what anybody means when they ask it to stop watching. Independent of sleep
# — the session, the ears and the motors are all untouched.
FACE_DETECTION = {"on": True}


def set_face_detection(enabled: bool, log=lambda m: print(m, flush=True)) -> None:
    """Stop or resume watching faces. Both halves are attempted even if one
    fails, so a robot told to stop watching does as much of it as it can."""
    FACE_DETECTION["on"] = bool(enabled)
    try:
        import reachy_wakesleep
        reachy_wakesleep.face_tracking(bool(enabled), log=log)
    except Exception as e:  # noqa: BLE001
        log(f"[openai-rt] face tracking {'on' if enabled else 'off'} failed: {e}")
    try:
        body = json.dumps({"paused": not enabled}).encode()
        req = urllib.request.Request(f"{MEMORY_URL}/pause", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        urllib.request.urlopen(req, timeout=6).read()
    except Exception as e:  # noqa: BLE001
        log(f"[openai-rt] face memory pause failed: {e}")
    if not enabled:
        # "Stop watching" has to mean the scene watcher too, and immediately —
        # waiting for its next tick would keep looking for another ten seconds
        # after being told not to.
        try:
            import reachy_scene
            reachy_scene.watch(False)
        except Exception as e:  # noqa: BLE001
            log(f"[openai-rt] scene watch stop failed: {e}")


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

# The session currently holding the conversation, so reachy_chat.py can pass a
# sighting into it. None when the realtime engine is not running.
LIVE_SESSION = {"session": None}

# A coding request that has been read back but not yet agreed to. A fallback:
# if the confirming call arrives with an empty task, this is what was approved,
# so a yes never turns into "no task given".
_PENDING_TASK: dict = {"task": ""}


def refresh_live_session() -> bool:
    """Rebuild the running session's prompt and tools, if one is running.

    Used when a mode changes mid-conversation. No-op (returns False) when the
    realtime brain isn't the one holding the conversation — the next connect
    picks the new settings up anyway, since _session_update reads them fresh.
    """
    s = LIVE_SESSION.get("session")
    if s is None:
        return False
    _INCOGNITO_CACHE["at"] = 0.0    # force a re-read, don't trust the 3s cache
    s.refresh()
    return True


def _tool_move(args: dict) -> str:
    move = str(args.get("move", "")).strip().lower()
    sound = bool(args.get("sound", False))
    if move in ("left_antenna_check", "right_antenna_check"):
        # One side only, ~3s of motion — short enough to hold the tool call open
        # and hand back the actual verdict instead of a stale one.
        side = move.split("_", 1)[0]
        try:
            return reachy_emotes.check_side(side)["summary"]
        except Exception as e:  # noqa: BLE001
            return (f"the {side} antenna check would not run ({e}); "
                    f"last I knew: {reachy_emotes.antenna_status()['summary']}")
    if move == "antenna_check":
        # The probe takes ~10s of real motion, far too long to hold a tool call
        # open. Start it, hand back what we knew a moment ago, and let the next
        # call have the fresh verdict.
        threading.Thread(target=reachy_emotes.antenna_check,
                         daemon=True).start()
        status = reachy_emotes.antenna_status()
        return (f"running the antenna probe now (about ten seconds). "
                f"Last verdict: {status['summary']}")
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
    if not task and _PENDING_TASK["task"]:
        task = _PENDING_TASK["task"]
    if not task:
        return "no task given"
    # A read-back the person can veto. Spoken requests get mangled, and a coding
    # agent working from a mangled one burns minutes before anyone finds out.
    # Nothing is dispatched on this call.
    if args.get("confirm_first"):
        _PENDING_TASK["task"] = task
        return (f"NOT STARTED YET. Say the request back in one plain sentence — "
                f"\"{task}\" — and ask if that's right. If they say yes, call "
                f"improve_yourself again without confirm_first. If they correct "
                f"you, call it with the corrected task.")
    _PENDING_TASK["task"] = ""
    job = reachy_agent.dispatch(task, on_done=announce)
    if job.get("state") == "unavailable":
        # The distinction the model has to hear: nothing was started. "On it" is
        # a lie here, and an apology for a failed run is a different lie.
        return (f"COULD NOT ATTEMPT — nothing was started and nothing changed. "
                f"Say so plainly, in your own words: {job.get('spoken')} "
                f"Do NOT say you are on it, and do not promise to report back.")
    if not job.get("id"):
        return f"could not start: {job.get('error', 'unknown')}"
    started = (f"started job {job['id']}. It runs in the background for minutes. "
               f"Say something short and CARRY ON with the conversation — you "
               f"will be told when it finishes.")
    if not job.get("verified"):
        # Started, but nobody has yet proved the coding agent answers. Hedge the
        # promise rather than the work — the job really is running.
        started += (" I have not confirmed the coding agent is reachable, so say "
                    "you are TRYING it, not that it is definitely underway.")
    return started


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


MEMORY_HEADER = (
    "\n\nYOUR MEMORIES. This is the complete list, stored as plain text files "
    "on your own chip and editable by Jack on your dashboard. It is always "
    "loaded, so never search for these: when someone asks what you remember "
    "or what's in your memory, answer straight from this list, naming the "
    "specific things. Honour them:\n")


def memory_block() -> str:
    """Every memory, read fresh, as a prompt block. Empty string if none."""
    try:
        skills = reachy_agent.load_skills()
    except Exception:  # noqa: BLE001
        return ""
    return MEMORY_HEADER + skills if skills else ""


def _tool_remember(args: dict) -> str:
    """Writes to BOTH stores, on purpose and for now.

    SKILLS.md is read into the instructions at connect, so every line in it is
    paid for on every connection — left alone it grows until it is the prompt.
    Supermemory is where this should end up: searchable, tagged with whoever is
    in front of the camera, none of it resident.

    The dual write is a deliberately temporary belt-and-braces. Supermemory has
    been live for minutes and its relevance floor is calibrated off two
    measurements; dropping the file that currently works, on that basis, is how
    you find out in a week that a fortnight of memories went nowhere. Once
    recall has earned it, this should write only to Supermemory and SKILLS.md
    should go back to being a short, hand-kept list of standing rules.
    """
    note = str(args.get("note", ""))
    kept = reachy_agent.remember(note)
    try:
        import reachy_supermemory
        if SUPERMEMORY_RECALL and reachy_supermemory.available():
            reachy_supermemory.remember(note, person=_current_person())
    except Exception as e:  # noqa: BLE001 — never lose the turn over a write
        print(f"[openai-rt] supermemory write failed: {e}", flush=True)
    return f"remembered: {kept}" if kept else "nothing to remember"


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


# One memory system: the memories/ list, always in the prompt. Supermemory
# search (`recall`) is off unless asked for — with it on, "what do you
# remember?" went to a search over a different store and came back thin.
SUPERMEMORY_RECALL = os.environ.get("VIBEY_SUPERMEMORY_RECALL") == "1"
if not SUPERMEMORY_RECALL:
    TOOLS = [t for t in TOOLS if t.get("name") != "recall"]

# Wheels: the `drive` tool exists only once a rover is configured (ROVER_URL).
import reachy_rover
if reachy_rover.ROVER_URL:
    TOOLS = TOOLS + [reachy_rover.TOOL]

# Self-coding is off unless asked for. It kept overhearing Jack dictating to
# Claude Code on the Mac, spawning its own jobs on the same files, and those
# jobs mostly failed anyway. Code changes come from Jack, not from the room.
CODING_ENABLED = os.environ.get("VIBEY_CODING") == "1"
if not CODING_ENABLED:
    TOOLS = [t for t in TOOLS if t.get("name") not in ("improve_yourself", "check_progress")]
    _code_start = DEFAULT_INSTRUCTIONS.find("You can also CHANGE YOUR OWN CODE.")
    _code_end = DEFAULT_INSTRUCTIONS.find("\n\n", _code_start)
    if _code_start != -1 and _code_end != -1:
        DEFAULT_INSTRUCTIONS = (
            DEFAULT_INSTRUCTIONS[:_code_start]
            + "You do NOT change your own code, and you never hand work to a coding "
              "agent or to Claude Code. People near you are often talking to their "
              "own computer, not to you: ignore requests about code, apps or slides "
              "unless they clearly address you by name. If someone asks you to learn "
              "a trick or fix how you work, say Jack handles code changes and offer "
              "to `remember` the idea for him."
            + DEFAULT_INSTRUCTIONS[_code_end:])
    DEFAULT_INSTRUCTIONS = DEFAULT_INSTRUCTIONS.replace(
        "Prefer `remember` for facts, `improve_yourself` for behaviour.",
        "Use `remember` for facts and preferences.")

MEMORY_URL = os.environ.get("MEMORY_URL", "http://localhost:8773").rstrip("/")

# Incognito, as the brain sees it. The flag itself lives in reachy_memory (it
# owns the face data); this is a short-lived cache so building a session or
# answering who_is_here doesn't turn into a blocking HTTP call on every use.
# Falls back to the last known value — never to "naming is fine", because
# guessing wrong in that direction is the exact interruption this turns off.
_INCOGNITO_CACHE = {"on": False, "at": 0.0}


def incognito() -> bool:
    if time.time() - _INCOGNITO_CACHE["at"] < 3.0:
        return _INCOGNITO_CACHE["on"]
    try:
        with urllib.request.urlopen(f"{MEMORY_URL}/current", timeout=2) as r:
            _INCOGNITO_CACHE["on"] = bool(json.loads(r.read()).get("incognito"))
    except Exception:  # noqa: BLE001 — keep the last known answer
        pass
    _INCOGNITO_CACHE["at"] = time.time()
    return _INCOGNITO_CACHE["on"]


def _tool_volume(args: dict) -> str:
    """Vibey turning itself up. Asked for out loud, so answered out loud."""
    try:
        want = max(0, min(100, int(float(args.get("volume", 100)))))
    except (TypeError, ValueError):
        return "I need a number between 0 and 100."
    try:
        import reachy_wakesleep
        reachy_wakesleep.set_volume(want, log=lambda m: print(m, flush=True))
        return f"volume {want}"
    except Exception as e:  # noqa: BLE001
        return f"I couldn't change my volume — {e}"


def _tool_time(args: dict) -> str:
    """The clock, phrased for a speaker. reachy_clock never raises, so whatever
    comes back here is already sayable."""
    import reachy_clock
    zone = str(args.get("timezone") or "").strip() or None
    return reachy_clock.time_report(zone, remember=bool(args.get("remember")))


def _tool_who() -> str:
    """Who is in front of the camera, by name.

    The face memory service has known this all along; the voice simply could not
    reach it. Recognising people is most of what makes a robot feel social, and it
    was sitting one HTTP call away from the brain that talks to them.
    """
    try:
        with urllib.request.urlopen(f"{MEMORY_URL}/current", timeout=4) as r:
            seen = json.loads(r.read() or b"{}") or {}
        people = seen.get("people") or []
    except Exception as e:  # noqa: BLE001
        return f"I cannot see right now ({e})."
    if seen.get("paused"):
        # An empty list means both "nobody is there" and "I am not looking".
        return "face recognition is switched off — I am not looking"
    if not people:
        return "nobody in view"
    named = [p.get("name") for p in people if p.get("name")]
    unknown = len(people) - len(named)
    if named and not unknown:
        return "I can see " + ", ".join(named)
    # "I don't know them yet — ask their name" is a standing invitation to do
    # the one thing incognito exists to stop, so the unnamed half is phrased as
    # a closed fact rather than an opening when the mode is on.
    if seen.get("incognito"):
        who = f"{unknown} {'person' if unknown == 1 else 'people'}"
        if named:
            return (f"I can see {', '.join(named)}, and {who} I have no name "
                    f"for — do not ask, name-learning is switched off")
        return (f"{who} in view, no names — do not ask, name-learning is "
                f"switched off")
    if named:
        return (f"I can see {', '.join(named)}, and {unknown} "
                f"{'person' if unknown == 1 else 'people'} I don't know yet")
    return (f"{len(people)} {'person' if len(people) == 1 else 'people'} "
            "I don't know yet. No need to ask; just talk with them")


def _current_person() -> str | None:
    """The single named face in view, or None.

    Only ONE. With two people in front of the camera there is no way to tell
    which of them the sentence was about, and a memory filed under the wrong
    name is worse than one filed under nobody: it will be recalled, confidently,
    at the wrong person.
    """
    try:
        with urllib.request.urlopen(f"{MEMORY_URL}/current", timeout=3) as r:
            seen = json.loads(r.read() or b"{}") or {}
        named = [p.get("name") for p in (seen.get("people") or []) if p.get("name")]
        return named[0] if len(named) == 1 else None
    except Exception:  # noqa: BLE001
        return None


def _tool_recall(args: dict) -> str:
    import reachy_supermemory
    if not reachy_supermemory.available():
        return "my long-term memory isn't hooked up"
    who = args.get("person")
    if who is not None:
        who = " ".join(str(who).split()).strip() or None
    hits = reachy_supermemory.recall(str(args.get("query", "")), person=who)
    if not hits:
        return ("nothing stored about that — say you don't think you were told, "
                "rather than guessing")
    return hits


def _tool_look(args: dict) -> str:
    """The scene, not the people. Everything that makes this safe to have —
    the off-by-default watch, the auto-expiry, the "don't describe humans"
    prompt — lives in reachy_scene, not in the model's instructions, so a
    misheard sentence can't talk Vibey into narrating the room all evening."""
    import reachy_privacy
    if reachy_privacy.is_on():
        return ("My eyes are closed: privacy mode is on (Jack can text /privacy "
                "off). Say so lightly; don't guess what's in the room.")
    import reachy_scene
    if "watch" in args and args.get("watch") is not None:
        return reachy_scene.watch(bool(args["watch"]), args.get("minutes"))
    return reachy_scene.look(question=(args.get("question") or "").strip() or None)


def _tool_remember_face(args: dict) -> str:
    name = " ".join(str(args.get("name", "")).split()).strip()
    if not name:
        return "I need a name to go with the face."
    try:
        body = json.dumps({"name": name}).encode()
        req = urllib.request.Request(f"{MEMORY_URL}/name", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            json.loads(r.read() or b"{}")
        return f"saved — I'll know {name} next time"
    except Exception as e:  # noqa: BLE001
        # The service refuses when it cannot tell which face to attach the name
        # to, which is the common case in a room: several people at once.
        return (f"I couldn't save that — {e}. If more than one person is in "
                "front of me, ask them to take turns.")


def _tool_take_notes() -> str:
    """Close the session like go_to_sleep, then switch scribe on once the short
    acknowledgement has been spoken."""
    _tool_sleep()

    def _later():
        time.sleep(6)
        try:
            req = urllib.request.Request(
                "http://localhost:8772/scribe", data=b'{"on": true}', method="POST",
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=40).read()
        except Exception as e:  # noqa: BLE001
            print(f"[openai-rt] scribe start failed: {e}", flush=True)
    threading.Thread(target=_later, daemon=True).start()
    return "ok"


def _tool_sleep() -> str:
    """Ask the loop to wind up once the goodbye has been spoken."""
    global SLEEP_REQUESTED_AT
    SLEEP_REQUESTED_AT = time.time()
    SLEEP_REQUESTED.set()
    try:  # a watch must never outlive the conversation that asked for it
        import reachy_scene
        reachy_scene.watch(False)
    except Exception:  # noqa: BLE001
        pass
    # Deliberately nothing worth saying out loud. A sentence here gets read back:
    # the model treats a tool result as material, so an explanation of what is
    # about to happen becomes a second announcement of it.
    return "ok"


def _tool_voice_detection(args: dict) -> str:
    """Stop or resume hearing. Terse on purpose — the spoken line comes from the
    model, so anything explanatory here gets said twice."""
    enabled = bool(args.get("enabled", False))
    try:
        minutes = float(args["minutes"]) if args.get("minutes") is not None else None
    except (TypeError, ValueError):
        minutes = None
    set_voice_detection(enabled, minutes)
    return "on" if enabled else "off"


def _tool_noise_suppression(args: dict) -> str:
    """Switch the room filter. Terse for the same reason as above — and it
    reports what the room actually sounds like, so if somebody asks twice the
    model has something true to say instead of guessing."""
    try:
        mode = reachy_denoise.set_mode(str(args.get("mode", "on")))
    except ValueError:
        return f"unchanged — the profiles are: {reachy_denoise.profile_menu()}"
    return f"{mode} ({reachy_denoise.describe()})"


def _tool_am_i_recording() -> str:
    """Which of the four not-recordings this is, in words. Read-only."""
    return reachy_denoise.capture_line()


def _tool_face_detection(args: dict) -> str:
    """Stop or resume watching faces. Terse for the same reason as above."""
    enabled = bool(args.get("enabled", False))
    set_face_detection(enabled)
    return "on" if enabled else "off"


DJ_URL = os.environ.get("DJ_URL", "http://localhost:8778").rstrip("/")


def _dj(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(f"{DJ_URL}{path}", data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            return {"error": f"dj service said {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"the DJ service isn't running ({e})"}


def _tool_dj_play(args: dict) -> str:
    st = _dj("/play", {"track": str(args.get("track", ""))})
    if st.get("error"):
        tracks = st.get("tracks") or []
        return (f"Couldn't find that. I have: {', '.join(tracks)}." if tracks
                else f"No music yet — drop files in ~/Music/vibey. ({st['error']})")
    return (f"Playing {st['track']} at {st['bpm']:.0f} BPM, {st['duration']:.0f}s long. "
            f"Say one short thing and let it play — you're dancing already.")


def _tool_dj_tempo(args: dict) -> str:
    if args.get("bpm") is not None:
        st = _dj("/tempo", {"bpm": float(args["bpm"])})
    elif args.get("percent") is not None:
        st = _dj("/nudge", {"percent": float(args["percent"])})
    else:
        return "Say a BPM or a percent."
    if st.get("error"):
        return st["error"]
    if not st.get("track"):
        return "Nothing's playing yet."
    return f"Now at {st['target_bpm']:.0f} BPM (was {st['bpm']:.0f})."


def _tool_dj_stop() -> str:
    _dj("/stop", {})
    return "Music off."


def _tool_dj_tracks() -> str:
    t = _dj("/tracks")
    st = _dj("/status")
    names = t.get("tracks") or []
    now = (f"Playing {st['track']} at {st['target_bpm']:.0f} BPM. "
           if st.get("playing") else "")
    if not names:
        return now + f"No tracks in {t.get('folder', '~/Music/vibey')} yet."
    return now + f"I have: {', '.join(names)}."


def _tool_cost() -> str:
    return reachy_cost.spoken()


def _tool_contacts() -> str:
    import reachy_telegram
    return reachy_telegram.optin_help()


def _tool_send_message(args: dict) -> str:
    """Text a human on Telegram.

    Consent lives in reachy_telegram — the recipient opted in themselves and
    Jack approved them, so there is no way to reach a stranger from here. What
    this adds is the spoken confirmation, because a misheard sentence is much
    cheaper to catch before it lands on somebody's phone than after.
    """
    import reachy_telegram
    to = str(args.get("to") or "").strip()
    message = " ".join(str(args.get("message") or "").split())
    if not to or not message:
        return "I need both a name and something to say."
    if to.lower() not in reachy_telegram.contact_names():
        return reachy_telegram.optin_help(f"I can't text {to} yet. ")
    if not bool(args.get("confirmed")):
        return (f"Not sent. Read it back first — to {to}: \"{message}\" — and "
                "if they say yes, call me again with confirmed true.")
    return reachy_telegram.send_to_contact(to, message)


def _tool_text_jack(args: dict) -> str:
    """Vibey reaching Jack first.

    No spoken read-back here, unlike send_text_message: the recipient is the
    owner, the consent is the pairing, and the point of the tool is the times
    he is not there to confirm anything. The bounds live in reachy_telegram —
    quiet hours, a rate limit, and an off switch — so this stays a one-liner
    and the policy stays in one place.
    """
    import reachy_telegram
    return reachy_telegram.notify_owner(str(args.get("message") or ""),
                                        urgent=bool(args.get("urgent")))


def _tool_explain(args: dict) -> str:
    """Structured advice. Pure words — reachy_help cannot touch the network or
    the config, so the worst case of a misheard question is a wrong sentence."""
    topic = str(args.get("topic") or "").strip()
    if topic.startswith("troubleshoot_"):
        return reachy_help.troubleshoot(topic[len("troubleshoot_"):],
                                        step=args.get("step"),
                                        symptom=str(args.get("symptom") or ""))
    return reachy_help.explain(topic)


def _tool_check_ui(args: dict) -> str:
    """The live half of the same help: ports probed, not remembered. Read-only,
    and it reports nothing a person couldn't read off this machine themselves."""
    import reachy_uidoctor
    return reachy_uidoctor.report(what=str(args.get("what") or ""),
                                  viewing_from=str(args.get("viewing_from") or ""))


# Front desk (reachy_frontdesk.py on 127.0.0.1:8779). The QR scanner does the
# door on its own and hands results in through /sighting; this tool is the
# fallback for a guest with no QR who says their name instead. The tool is
# always listed (Live can't change its tools mid-session) and answers "off"
# when the desk is off.
FRONTDESK_URL = os.environ.get("FRONTDESK_URL", "http://127.0.0.1:8779").rstrip("/")
_FRONTDESK_CACHE = {"on": False, "at": 0.0}
FRONTDESK_PROMPT = (
    "\n\nFRONT DESK MODE IS ON. You are the door greeter at an event. Guests walk "
    "up and hold their Luma ticket QR code up to your camera; a scanner checks it "
    "and tells you the result, which you say in one short upbeat line. When "
    "someone walks up, greet them like a bouncer with a grin: \"stop right there! "
    "let me scan you in, hold your QR code up to my eyes.\" If they have no QR "
    "code, ask their name (or email) and call `front_desk_check_in` with it, then "
    "say what it tells you. If it can't find them, send them to a human at the "
    "door. A line is forming: one or two short sentences, no long chats, no "
    "follow-up questions, no tangents.")
TOOLS = TOOLS + [{
    "type": "function",
    "name": "front_desk_check_in",
    "description": (
        "Front desk mode only. Check a guest in at the door by the name or email "
        "they say out loud, when they can't show their ticket QR code. Fuzzy "
        "matches the guest list and logs them in. Returns a line to say."),
    "parameters": {
        "type": "object",
        "properties": {"name_or_email": {
            "type": "string", "description": "The guest's full name, or their email."}},
        "required": ["name_or_email"],
    },
}]


def frontdesk_on() -> bool:
    if time.time() - _FRONTDESK_CACHE["at"] < 3.0:
        return _FRONTDESK_CACHE["on"]
    try:
        with urllib.request.urlopen(f"{FRONTDESK_URL}/status", timeout=0.5) as r:
            _FRONTDESK_CACHE["on"] = bool(json.loads(r.read()).get("on"))
    except Exception:  # noqa: BLE001 — not running means off
        _FRONTDESK_CACHE["on"] = False
    _FRONTDESK_CACHE["at"] = time.time()
    return _FRONTDESK_CACHE["on"]


def _tool_front_desk(args: dict) -> str:
    q = str(args.get("name_or_email") or "").strip()
    if not frontdesk_on():
        return "Front desk mode is off, so there's no guest list to check."
    if not q:
        return "Ask for their name or email first."
    req = urllib.request.Request(
        f"{FRONTDESK_URL}/checkin", method="POST",
        data=json.dumps({"name_or_email": q}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        res = json.loads(r.read() or b"{}")
    if res.get("result") == "ambiguous":
        return ("More than one guest matches: " + ", ".join(res.get("candidates", []))
                + ". Ask for their last name, then call this again.")
    return f"Result: {res.get('result')}. Say: \"{res.get('say', '')}\""


_TOOL_KIND = {
    "send_text_message": "telegram", "text_jack": "telegram",
    "list_message_contacts": "telegram",
    "who_is_here": "senses", "look_at_the_room": "senses", "recall": "senses",
    "front_desk_check_in": "senses", "am_i_recording": "senses",
    "check_progress": "thinking", "improve_yourself": "thinking",
    "explain_how_to": "thinking", "check_local_ui": "thinking",
    "go_to_sleep": "system", "set_voice_detection": "system",
    "set_face_detection": "system", "set_noise_suppression": "system",
    "check_my_cost": "system", "what_time_is_it": "system",
}
_TOOL_ICON = {"move": "🤖", "dance": "💃", "drive": "🛞", "remember": "📌",
              "remember_face": "🙂", "who_is_here": "👀", "look_at_the_room": "📷",
              "send_text_message": "✉", "text_jack": "✉", "set_volume": "🔊",
              "dj_play": "🎧", "dj_tempo": "🎧", "dj_stop": "🎧", "dj_tracks": "🎧",
              "go_to_sleep": "🌙", "take_notes": "📝", "improve_yourself": "🛠",
              "front_desk_check_in": "🎟", "vibe_check": "✨", "recall": "🔎"}


def _tool_label(name: str, args: dict) -> str:
    a = args or {}
    if name in ("move", "dance"):
        return f"{name}: {a.get('move') or a.get('name') or a.get('style') or ''}".rstrip(": ")
    if name == "remember":
        return f"remembered: {str(a.get('note') or '')[:60]}"
    if name == "remember_face":
        return f"saving a face as {a.get('name') or '?'}"
    if name == "send_text_message":
        return f"texting {a.get('to') or '?'}" + ("" if a.get("confirmed") else " (read-back first)")
    if name == "text_jack":
        return "texting Jack"
    if name == "set_volume":
        return f"volume → {a.get('volume', a.get('level', '?'))}"
    if name == "drive":
        return f"drive {a.get('action') or ''}"
    if name == "dj_play":
        return f"dj: playing {a.get('track') or a.get('query') or ''}".rstrip()
    return name.replace("_", " ")


def _dispatch_tool(name: str, args: dict, announce, source: str = "voice") -> str:
    """Run a tool and put it in the event stream (args + result)."""
    t0 = time.time()
    result = _dispatch_tool_inner(name, args, announce)
    try:
        import reachy_events
        a = dict(args or {})
        if name == "send_text_message" and a.get("message"):
            a["message"] = reachy_events.short(a["message"], 80)
        reachy_events.emit(_TOOL_KIND.get(name, "action"), _tool_label(name, a),
                           detail={"tool": name, "args": a,
                                   "result": reachy_events.short(result, 300),
                                   "ms": int((time.time() - t0) * 1000)},
                           source=source, icon=_TOOL_ICON.get(name, "⚡"))
    except Exception:  # noqa: BLE001
        pass
    return result


def _dispatch_tool_inner(name: str, args: dict, announce) -> str:
    """Run a tool by name. Runs in a worker thread — must never touch the
    websocket or the event loop directly."""
    try:
        if name == "move":
            return _tool_move(args)
        if name == "dance":
            return _tool_dance(args)
        if name == "drive":
            import reachy_rover
            return reachy_rover.drive(str(args.get("action", "")),
                                      float(args.get("seconds") or 1.0),
                                      str(args.get("speed") or "medium"))
        if name == "improve_yourself":
            return _tool_improve(args, announce)
        if name == "check_progress":
            return _tool_check(args)
        if name == "remember":
            return _tool_remember(args)
        if name == "vibe_check":
            return _tool_vibe(args)
        if name == "set_volume":
            return _tool_volume(args)
        if name == "what_time_is_it":
            return _tool_time(args)
        if name == "who_is_here":
            return _tool_who()
        if name == "recall":
            return _tool_recall(args)
        if name == "look_at_the_room":
            return _tool_look(args)
        if name == "remember_face":
            return _tool_remember_face(args)
        if name == "go_to_sleep":
            return _tool_sleep()
        if name == "take_notes":
            return _tool_take_notes()
        if name == "set_voice_detection":
            return _tool_voice_detection(args)
        if name == "dj_play":
            return _tool_dj_play(args)
        if name == "dj_tempo":
            return _tool_dj_tempo(args)
        if name == "dj_stop":
            return _tool_dj_stop()
        if name == "dj_tracks":
            return _tool_dj_tracks()
        if name == "check_my_cost":
            return _tool_cost()
        if name == "set_face_detection":
            return _tool_face_detection(args)
        if name == "set_noise_suppression":
            return _tool_noise_suppression(args)
        if name == "am_i_recording":
            return _tool_am_i_recording()
        if name == "list_message_contacts":
            return _tool_contacts()
        if name == "send_text_message":
            return _tool_send_message(args)
        if name == "text_jack":
            return _tool_text_jack(args)
        if name == "explain_how_to":
            return _tool_explain(args)
        if name == "check_local_ui":
            return _tool_check_ui(args)
        if name == "front_desk_check_in":
            return _tool_front_desk(args)
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


# Where replies come out. SPEAKER_SOURCE=laptop plays them here instead of
# uploading a WAV to the robot: for testing on the laptop, and for when the
# robot's Wi-Fi is too slow to take a clip before it times out.
SPEAKER_SOURCE = os.environ.get("SPEAKER_SOURCE", "robot").strip().lower()


def _stop_sound() -> None:
    """Cut whatever the robot is currently playing (barge-in)."""
    if SPEAKER_SOURCE == "laptop":
        try:
            import sounddevice as sd
            sd.stop()
        except Exception as e:  # noqa: BLE001
            print(f"[openai-rt] laptop stop failed: {e}", flush=True)
        return
    try:
        req = urllib.request.Request(
            f"{REACHY_URL}/api/media/stop_sound", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # noqa: BLE001
        print(f"[openai-rt] stop_sound failed: {e}", flush=True)


# How close to full scale a reply is lifted before playing, and the most it may be
# lifted by. OpenAI's realtime audio is not peak-normalised, so on a small speaker
# across a room it is simply quiet whatever the system volume says. The cap
# matters: without it a nearly-silent clip gets multiplied until the room tone is
# deafening.
PLAY_TARGET_PEAK = float(os.environ.get("OPENAI_RT_TARGET_PEAK", "0.92"))
PLAY_MAX_GAIN = float(os.environ.get("OPENAI_RT_MAX_GAIN", "4.0"))


def _normalise(pcm: bytes) -> bytes:
    """Lift a clip toward full scale, without ever clipping it."""
    n = len(pcm) // 2
    if n == 0:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])
    peak = max((abs(v) for v in samples), default=0)
    if peak == 0:
        return pcm
    gain = min(PLAY_MAX_GAIN, (PLAY_TARGET_PEAK * 32767.0) / peak)
    if gain <= 1.05:
        return pcm                      # already loud enough; leave it alone
    for i, v in enumerate(samples):
        scaled = int(v * gain)
        samples[i] = 32767 if scaled > 32767 else (-32767 if scaled < -32767 else scaled)
    return samples.tobytes()


class _Streamer:
    """Plays reply audio as it arrives instead of after the reply is done.

    Robot speaker: each chunk goes to the mic bridge's /play (reachy_robot_mic,
    WebRTC send chain), which pushes it straight to the robot. Laptop speaker:
    a sounddevice output stream. Either way the first word is heard while the
    rest is still being generated. A worker thread does the I/O so the
    websocket loop never waits on it. `ok` False means neither is available
    and the caller falls back to the old play-the-whole-clip path.
    """

    GAIN = float(os.environ.get("STREAM_GAIN", "2.0"))

    def __init__(self, log=print):
        import queue
        self.log = log
        self.q: "queue.Queue[bytes | None]" = queue.Queue()
        self.gen = 0                  # bumped by clear(): stale chunks are dropped
        self.out = None
        self.mode = None
        if SPEAKER_SOURCE == "laptop":
            try:
                import sounddevice as sd
                self.out = sd.RawOutputStream(samplerate=RT_SR, channels=1, dtype="int16")
                self.out.start()
                self.mode = "laptop"
            except Exception as e:  # noqa: BLE001
                log(f"[openai-rt] laptop stream unavailable: {e}")
        else:
            try:
                with urllib.request.urlopen(f"{ROBOT_MIC_URL}/status", timeout=3) as r:
                    if json.loads(r.read()).get("speaker_stream"):
                        self.mode = "robot"
            except Exception as e:  # noqa: BLE001
                log(f"[openai-rt] robot speaker stream unavailable: {e}")
        if self.mode:
            threading.Thread(target=self._worker, daemon=True).start()
            log(f"[openai-rt] streaming replies to the {self.mode} speaker")

    @property
    def ok(self) -> bool:
        return self.mode is not None

    def push(self, pcm: bytes) -> float:
        """Queue a chunk; returns its duration in seconds."""
        import numpy as np
        a = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * self.GAIN
        self.q.put((self.gen, np.clip(a, -32767, 32767).astype("<i2").tobytes()))
        return len(pcm) / 2 / RT_SR

    def clear(self) -> None:
        self.gen += 1
        try:
            while True:
                self.q.get_nowait()
        except Exception:  # noqa: BLE001 — queue.Empty
            pass
        if self.mode == "robot":
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"{ROBOT_MIC_URL}/clear", data=b"", method="POST"), timeout=3).read()
            except Exception as e:  # noqa: BLE001
                self.log(f"[openai-rt] stream clear failed: {e}")
        elif self.mode == "laptop":
            try:
                self.out.abort()
                self.out.start()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self.q.put(None)
        if self.out is not None:
            try:
                self.out.close()
            except Exception:  # noqa: BLE001
                pass

    def _worker(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                return
            gen, pcm = item
            if gen != self.gen:
                continue
            try:
                if self.mode == "robot":
                    urllib.request.urlopen(urllib.request.Request(
                        f"{ROBOT_MIC_URL}/play?sr={RT_SR}", data=pcm, method="POST"),
                        timeout=3).read()
                else:
                    self.out.write(pcm)
            except Exception as e:  # noqa: BLE001
                self.log(f"[openai-rt] stream chunk failed: {e}")


def _play_pcm_on_robot(pcm24: bytes) -> float:
    """Upload a 24kHz PCM16 buffer as a WAV and play it on Vibey's speaker.
    Returns the clip duration in seconds (so the caller can gate the mic)."""
    duration = len(pcm24) / 2 / RT_SR
    pcm24 = _normalise(pcm24)
    if SPEAKER_SOURCE == "laptop":
        import numpy as np
        import sounddevice as sd
        sd.play(np.frombuffer(pcm24, dtype=np.int16), RT_SR)
        return duration
    wav = _wav_from_pcm16(pcm24, RT_SR)
    name = f"oai_{uuid.uuid4().hex[:8]}.wav"
    upload_sound(wav, name)
    play_sound(name)
    return duration


# --------------------------------------------------------------------------- #
# Mic pump — a background thread reads PCM and hands resampled 24kHz chunks to
# the asyncio loop via a queue. Two sources, same queue on the other side.
# --------------------------------------------------------------------------- #
# Every mic pump ever started, with the event that stops it. Sessions are
# started and stopped by a person clicking things, and a pump that is still
# holding /pcm when the next session opens its own is not a leak you notice —
# both readers get the full stream, so the audio looks fine, and the two
# interleave into one websocket as overlapping speech that the server VAD can
# never segment into a turn. The robot sits there connected, listening, and
# answering nothing. Reaping on the way IN rather than trusting the way out is
# what makes that unrepresentable: however the last session died, it is gone
# before this one reads a byte.
_PUMPS: list = []
_PUMPS_LOCK = threading.Lock()


def _reap_pumps(log=print) -> None:
    with _PUMPS_LOCK:
        stale = list(_PUMPS)
        _PUMPS.clear()
    for thread, stop in stale:
        stop.set()
    for thread, _stop in stale:
        thread.join(timeout=3.0)
        if thread.is_alive():
            log(f"[openai-rt] mic pump {thread.name} would not stop")


def _mic_pump(loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue",
              stop: threading.Event) -> None:
    if MIC_SOURCE == "laptop":
        return _laptop_mic_pump(loop, queue, stop)
    return _robot_mic_pump(loop, queue, stop)


def _laptop_mic_pump(loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue",
                     stop: threading.Event) -> None:
    """Listen through this machine's own microphone.

    For networks that pass TCP to the robot and drop the peer-to-peer UDP its
    audio stream needs — guest and hotel Wi-Fi especially. Everything else keeps
    working over REST on such a network: the body moves, and replies still reach
    the robot's speaker as an uploaded clip. Only the input leg is broken, and
    only the input leg needs rerouting.
    """
    import sounddevice as sd
    CHUNK = MIC_SR // 10                       # 0.1s of 16kHz mono
    while not stop.is_set():
        try:
            with sd.RawInputStream(samplerate=MIC_SR, channels=1,
                                   dtype="int16", blocksize=CHUNK) as mic:
                print("[openai-rt] laptop mic connected", flush=True)
                while not stop.is_set():
                    raw, _overflowed = mic.read(CHUNK)
                    if loop.is_closed():
                        return
                    clean = reachy_denoise.process(bytes(raw), "live", MIC_SR)
                    up = _resample_pcm16(clean, MIC_SR, RT_SR)
                    if up:
                        loop.call_soon_threadsafe(queue.put_nowait, up)
        except Exception as e:  # noqa: BLE001
            if stop.is_set() or loop.is_closed():
                break
            print(f"[openai-rt] laptop mic dropped ({e}); retrying in 2s",
                  flush=True)
            time.sleep(2)


def _robot_mic_pump(loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue",
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
                    # Denoise at 16kHz, before the resample: the noise floor is
                    # estimated on the rate it was actually recorded at, and
                    # interpolation can't smear a fan across bins first.
                    clean = reachy_denoise.process(raw, "live", MIC_SR)
                    up = _resample_pcm16(clean, MIC_SR, RT_SR)
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
        self._stream = None               # _Streamer, set per connection
        self._turn_end_at = 0.0           # when the server heard you stop
        self._first_audio_at = None
        self._loop = None                 # set once we're running
        self._announce_q = None           # finished background jobs, to announce
        self._response_active = False     # a reply is being generated right now
        self._last_turn_at = time.time()  # for the idle timer

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
        if BASIC["on"]:
            return {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": BASIC_INSTRUCTIONS,
                    "tools": [t for t in TOOLS if t.get("name") == "look_at_the_room"],
                    "tool_choice": "auto",
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": RT_SR},
                            "turn_detection": turn,
                            "transcription": {"model": "gpt-4o-mini-transcribe"},
                        },
                        "output": {
                            "format": {"type": "audio/pcm", "rate": RT_SR},
                            "voice": VOICE,
                        },
                    },
                },
            }
        instructions = (os.environ.get("OPENAI_RT_INSTRUCTIONS")
                        or DEFAULT_INSTRUCTIONS)
        # Lessons taught in earlier conversations ride along in the prompt, so
        # a `remember` from last night is in force on tonight's first word.
        instructions += memory_block()
        # What was said lately on either side, texts included, so a voice
        # session that starts after a text already knows about it. Texts that
        # arrive mid-session come in through note(). See reachy_brain.
        try:
            import reachy_brain
            instructions += reachy_brain.voice_context()
        except Exception as e:  # noqa: BLE001 — context is a nice-to-have
            print(f"[openai-rt] shared context unavailable: {e}", flush=True)

        # Incognito. Taking `remember_face` away is not enough on its own: the
        # prompt tells it to ask for names in plain English, and a model that
        # wants a name and has no tool for it just asks anyway — which is the
        # entire complaint. So the instruction goes too, and is replaced with
        # an explicit prohibition rather than silence, because "don't do X" is
        # the only form a model reliably honours mid-conversation.
        if frontdesk_on():
            instructions += FRONTDESK_PROMPT
        tools = TOOLS
        if incognito():
            tools = [t for t in TOOLS if t.get("name") != "remember_face"]
            instructions = instructions.replace(
                "If you see somebody you do not know, ask for "
                "their name, then call `remember_face` so you have it next time. "
                "Do not announce that you are saving it; just use it from then on.",
                "")
            instructions += (
                "\n\nINCOGNITO IS ON. Do not ask anybody their name, and do not "
                "offer to learn or save a face — you cannot, the tool is gone. "
                "If `who_is_here` comes back with someone you have no name for, "
                "that is fine and expected: talk to them warmly as they are, "
                "without remarking on not knowing who they are and without "
                "steering toward it. You still see people and still look at "
                "whoever is talking. If somebody volunteers their name "
                "unprompted, just use it in conversation. Only if they ask you "
                "directly to remember them do you say that recognising faces is "
                "switched off right now and it can be turned back on from the "
                "dashboard.")

        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions,
                "tools": tools,
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
            # Asked to stop listening: drop the audio here, so the server-side
            # VAD hears silence and no turn ever starts. Dropped rather than
            # muted, because "off" can last hours and there is no reason to pay
            # for uploading silence by the hour.
            if not voice_detection_active():
                continue
            # Optional half-duplex gate: don't feed the mic while our own clip
            # is playing, so the robot doesn't hear itself and self-trigger.
            # Muted rather than dropped — a hole in the stream stops the
            # server's silence timer with it, so the end of the turn somebody
            # spoke just before we started talking never gets noticed. Silence
            # is a fact the VAD can use; a gap is one it can't.
            if GATE_ON_SPEAK and time.time() < self._speaking_until:
                chunk = b"\x00" * len(chunk)
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
        if self._stream is not None and self._stream.ok:
            self._stream.clear()
        # The clip stops here, so the freeze on the noise estimate has to stop
        # here too — otherwise the room stays un-learnable for the rest of a
        # sentence that isn't being spoken any more.
        reachy_denoise.set_speaking(False)
        # The head was gesturing to audio that is no longer playing. Left
        # running it would keep nodding along to a sentence nobody can hear.
        try:
            import reachy_talk
            reachy_talk.stop()
        except Exception:  # noqa: BLE001
            pass
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
            # The head moves with the sentence, not just at tool-call moments.
            # Started here because this is where the audio and its duration are
            # both known, and the motion is scheduled off wall-clock from now.
            try:
                import reachy_talk
                reachy_talk.start(pcm, sample_rate=RT_SR, duration=dur)
            except Exception as e:  # noqa: BLE001 — never lose a reply over motion
                self.log(f"talk motion failed: {e}")
            # Our speaker is live for the next `dur` seconds: freeze the room
            # estimate and say "not recording — I'm talking" rather than
            # letting a meter imply the microphone died.
            reachy_denoise.set_speaking(dur + 0.3)
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
            # The reply is requested at response.done, once every call in
            # this turn has its output. Asking after the first of two calls
            # is rejected (function_call_outputs_required) and the turn dies.
            self._tool_reply_due = True
        except Exception as e:  # noqa: BLE001
            self.log(f"failed to return tool result: {e}")

    @staticmethod
    def _context_event(text: str, label: str, spoken: bool) -> None:
        """Every line injected into the session shows up in the stream, so
        what prompted a remark is never a mystery."""
        try:
            import reachy_events
            core = text.strip().strip("[]")
            reachy_events.emit(
                "thinking",
                f"context in: {label or reachy_events.short(core, 110)}",
                detail={"text": reachy_events.short(core, 500) if not label else label,
                        "asks_to_speak": spoken},
                source="nudge" if spoken else "note", icon="↘")
        except Exception:  # noqa: BLE001
            pass

    def nudge(self, text: str, label: str = "") -> None:
        """Something happened in the room that Vibey should mention itself.

        Same channel as a finished coding job, because it is the same idea: news
        arriving from outside the conversation that the robot should raise in its
        own voice, rather than a second system speaking over it.
        """
        loop, q = self._loop, self._announce_q
        if loop is None or q is None:
            return
        self._context_event(text, label, True)
        try:
            loop.call_soon_threadsafe(q.put_nowait, {"nudge": text})
        except RuntimeError:
            pass

    def text_turn(self, text: str, who: str = "Jack", timeout: float = 25.0):
        """A text from the phone, answered OUT LOUD in the room. Blocks until
        Vibey has said its reply and returns the words (so they can go back as
        the text reply), or None if nothing came in time. Called from other
        threads — the HTTP handler — never from the event loop."""
        loop, q = self._loop, self._announce_q
        if loop is None or q is None:
            return None
        waiter = {"event": threading.Event(), "text": None}
        framed = (f"[{who} just texted you from his phone: \"{text[:600]}\". "
                  f"Write your reply to him as text — it goes back to his phone and "
                  f"nobody in the room hears it. Only if something should ALSO be "
                  f"heard in the room (he asks you to say something out loud, or it "
                  f"is plainly meant for whoever is there) call say_aloud with the "
                  f"exact words. If he says not to talk, or it's private, don't.]")
        self._context_event(f"{who} texted: {text[:200]}", f"{who} texted", True)
        try:
            loop.call_soon_threadsafe(q.put_nowait, {"nudge": framed, "waiter": waiter})
        except RuntimeError:
            return None
        waiter["event"].wait(timeout)
        return waiter   # {"text": reply or None, "spoke": bool}

    def note(self, text: str, label: str = "") -> None:
        """Quiet context: goes into the conversation with NO response asked
        for. The model sees it next time it speaks and decides for itself
        whether it matters. For texts, which should never be read out."""
        loop, q = self._loop, self._announce_q
        if loop is None or q is None:
            return
        self._context_event(text, label, False)
        try:
            loop.call_soon_threadsafe(q.put_nowait, {"note": text})
        except RuntimeError:
            pass

    def refresh(self) -> None:
        """Ask the live session to rebuild itself. Called from other threads."""
        loop, q = self._loop, self._announce_q
        if loop is None or q is None:
            return
        try:
            loop.call_soon_threadsafe(q.put_nowait, {"session_refresh": True})
        except RuntimeError:
            pass

    def _announce_cb(self, job: dict) -> None:
        """Called from reachy_agent's worker thread when a background coding
        job lands. Hops onto the event loop; the announcer does the talking."""
        loop, q = self._loop, self._announce_q
        if loop is None or q is None:
            return
        if job.get("state"):
            self._context_event(
                f"coding job {job.get('id') or ''} {job.get('state')}: "
                f"{(job.get('spoken') or '').strip()}", "", True)
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
            if BASIC["on"] and not job.get("waiter"):
                continue    # basic: texts get through, nothing else does
            if job.get("session_refresh"):
                # A mode changed under us (incognito, so far). Rebuild the
                # session: instructions and the tool list are both computed
                # fresh in _session_update. Rides this queue because the wait
                # above is exactly right for it too — swapping the tools out
                # from under a reply that is mid-generation is how you get a
                # call to a tool that no longer exists. Silent on purpose: the
                # dashboard button already told the user.
                try:
                    await ws.send(json.dumps(self._session_update()))
                    self.log("session refreshed (mode changed)")
                except Exception as e:  # noqa: BLE001
                    self.log(f"session refresh failed: {e}")
                continue
            if job.get("note"):
                try:
                    await ws.send(json.dumps({
                        "type": "conversation.item.create",
                        "item": {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": job["note"]}]},
                    }))
                except Exception as e:  # noqa: BLE001
                    self.log(f"note failed: {e}")
                continue
            if job.get("waiter"):
                try:
                    await ws.send(json.dumps({
                        "type": "conversation.item.create",
                        "item": {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": job["nudge"]}]},
                    }))
                    self._text_turn = {"waiter": job["waiter"], "text": "", "said": None,
                                       "started": False}
                    await ws.send(json.dumps({"type": "response.create", "response": {
                        "output_modalities": ["text"],
                        "tools": [SAY_ALOUD_TOOL],
                        "tool_choice": "auto",
                    }}))
                except Exception as e:  # noqa: BLE001
                    self.log(f"text turn failed: {e}")
                    job["waiter"]["event"].set()
                continue
            if job.get("nudge"):
                nudge = job["nudge"]
                try:
                    await ws.send(json.dumps({
                        "type": "conversation.item.create",
                        "item": {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": nudge}]},
                    }))
                    await ws.send(json.dumps({"type": "response.create"}))
                except Exception as e:  # noqa: BLE001
                    self.log(f"nudge failed: {e}")
                continue

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
            elif t == "input_audio_buffer.speech_stopped":
                self._turn_end_at = time.time()

            # --- a new reply begins ---
            elif t == "response.created":
                tt = getattr(self, "_text_turn", None)
                if tt is not None and not tt["started"]:
                    tt["started"] = True
                    self._text_turn_resp = (msg.get("response") or {}).get("id")
                self._first_audio_at = None
                self._cancelled = False
                self._response_active = True
                self._resp_pcm = bytearray()

            # --- the model wants to use a tool ---
            elif t in ("response.output_text.done", "response.text.done"):
                tt = getattr(self, "_text_turn", None)
                if tt is not None and msg.get("response_id") == getattr(self, "_text_turn_resp", None):
                    tt["text"] = (msg.get("text") or "").strip()
            elif t == "response.function_call_arguments.done" and msg.get("name") == "say_aloud":
                try:
                    words = (json.loads(msg.get("arguments") or "{}").get("words") or "").strip()
                except (json.JSONDecodeError, TypeError):
                    words = ""
                self.log(f"tool → say_aloud({words[:120]!r})")
                tt = getattr(self, "_text_turn", None)
                if tt is not None:
                    tt["said"] = words
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": msg.get("call_id") or "",
                    "output": "said it" if words else "nothing to say"}}))
                self._say_pending = words or None
            elif t == "response.function_call_arguments.done":
                await self._handle_function_call(ws, loop, msg)

            # --- streamed reply audio (accept classic + GA event names) ---
            elif t in ("response.audio.delta", "response.output_audio.delta"):
                if not self._cancelled:
                    chunk = base64.b64decode(msg.get("delta", ""))
                    if self._first_audio_at is None:
                        self._first_audio_at = time.time()
                        self.log(f"[latency] first audio {1000*(self._first_audio_at - self._turn_end_at):.0f}ms after you stopped talking"
                                 if self._turn_end_at else "[latency] first audio")
                    if self._stream is not None and self._stream.ok:
                        # Straight to the speaker. The mic gate and the noise
                        # estimate follow the audio queued so far.
                        dur = self._stream.push(chunk)
                        self._speaking_until = max(self._speaking_until, time.time()) + dur
                        reachy_denoise.set_speaking(self._speaking_until - time.time() + 0.3)
                    else:
                        self._resp_pcm.extend(chunk)

            # --- reply audio finished → play it ---
            elif t in ("response.audio.done", "response.output_audio.done",
                       "response.done"):
                if t == "response.done":
                    self._response_active = False
                    tt = getattr(self, "_text_turn", None)
                    rid = (msg.get("response") or {}).get("id")
                    if tt is not None and rid == getattr(self, "_text_turn_resp", None):
                        self._text_turn = None
                        tt["waiter"]["text"] = tt["text"] or (
                            f"(said out loud) {tt['said']}" if tt["said"] else None)
                        tt["waiter"]["spoke"] = bool(tt["said"])
                        tt["waiter"]["event"].set()
                    words = getattr(self, "_say_pending", None)
                    if words:
                        self._say_pending = None
                        self._tool_reply_due = False
                        await ws.send(json.dumps({"type": "response.create", "response": {
                            "output_modalities": ["audio"],
                            "instructions": ("Say exactly this, out loud, in your own "
                                             "voice, and nothing else: " + words),
                            "tools": [],
                        }}))
                    if getattr(self, "_tool_reply_due", False):
                        self._tool_reply_due = False
                        await ws.send(json.dumps({"type": "response.create"}))
                    # Read the meter off the conversation itself. The account's
                    # usage API needs an admin key the robot does not have, but
                    # every completed turn reports what it cost.
                    usage = ((msg.get("response") or {}).get("usage") or {})
                    if usage:
                        reachy_cost.record(usage)
                    self._last_turn_at = time.time()
                await self._flush_reply(loop)

            # --- transcripts, for the dashboard log ---
            elif t == "conversation.item.input_audio_transcription.completed":
                # Input transcription is billed on its own model.
                tu = msg.get("usage") or {}
                if tu.get("type") == "duration" or "seconds" in tu:
                    reachy_cost.record_minutes("other", "gpt-4o-mini-transcribe",
                                               float(tu.get("seconds") or 0) / 60,
                                               {"what": "realtime input transcription"})
                elif tu:
                    reachy_cost.record_tokens("other", "gpt-4o-mini-transcribe", tu,
                                              {"what": "realtime input transcription"})
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
        # A fresh session always starts with its ears open, whatever the last one
        # was told — otherwise "stop listening" outlives the conversation it was
        # meant for and the robot comes back deaf.
        set_voice_detection(True)
        self._announce_q = asyncio.Queue()
        queue: "asyncio.Queue" = asyncio.Queue()
        pump = threading.Thread(
            target=_mic_pump, args=(loop, queue, stop), daemon=True)
        with _PUMPS_LOCK:
            _PUMPS.append((pump, stop))
        pump.start()

        # An ephemeral key on the wire, not the account key.
        #
        # A realtime session is a WebSocket held open for as long as somebody is
        # talking, and the robot is a device in a room other people walk into. Minting
        # a short-lived `ek_` means the thing on the socket cannot be reused if it ever
        # leaks. Falls back to the standard key rather than refusing to talk — see
        # FlowState's docs/API-CONTRACT.md, which is where this endpoint is written
        # down after /v1/realtime/sessions turned out to be a 404.
        attempt = 0
        while should_run() and not stop.is_set() and not self.fatal:
            try:
                # Minted per CONNECTION, not per run.
                #
                # An ephemeral key lives about ten minutes. Minting it once above the
                # loop meant the first connection worked and every reconnection after
                # it presented a corpse: "Ephemeral token expired", close code 3000,
                # retry in three seconds, forever. Two hundred and eighty-seven times
                # in one afternoon, while the robot sat there apparently awake and
                # unable to say why it would not speak.
                #
                # The retry loop exists precisely because connections drop. A
                # credential minted outside it is a credential that is fresh only for
                # the one attempt that needed it least.
                headers = {"Authorization": f"Bearer {_ephemeral_token() or API_KEY}"}
                self.log(f"connecting to OpenAI Realtime ({MODEL}, voice={VOICE}) …")
                async with websockets.connect(
                        WS_URL, additional_headers=headers,
                        max_size=16 * 1024 * 1024) as ws:
                    await ws.send(json.dumps(self._session_update()))
                    self.log("connected — full-duplex, just talk")
                    if self._stream is None:
                        self._stream = _Streamer(self.log)
                    # A connection that worked clears whatever the last one failed
                    # with, so a topped-up account is noticed immediately.
                    FATAL_REASON["why"] = None
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
                # And back off. Three seconds forever turns a broken credential into
                # a thousand requests an hour and a log too long to read.
                attempt += 1
                wait = min(3 * (2 ** min(attempt - 1, 5)), 90)
                self.log(f"connection error ({e}); retrying in {wait}s")
                await asyncio.sleep(wait)
            else:
                attempt = 0
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
    # Whatever the last session left behind, it does not get to share the mic
    # with this one. See _reap_pumps.
    _reap_pumps(log or print)
    stop = stop_event or threading.Event()
    session = RealtimeSession(on_user_text, on_agent_text, log)
    LIVE_SESSION["session"] = session

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
        # Six seconds was the ceiling for "the goodbye has not started yet", and
        # it was also the floor for how long sleeping took when the goodbye was
        # short — the loop simply waited the whole thing out. Two is enough: a
        # four-word farewell begins well inside it, and once it is speaking the
        # test above holds the session open for as long as the goodbye actually
        # takes.
        return time.time() - SLEEP_REQUESTED_AT < 2.0

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
        _reap_pumps(log or print)
        if session._stream is not None:
            session._stream.close()
        LIVE_SESSION["session"] = None
        if SLEEP_REQUESTED.is_set():
            (log or print)("[openai-rt] asked to sleep — session closed")


if __name__ == "__main__":
    print("[openai-rt] standalone mode — Ctrl-C to quit")
    run(on_user_text=lambda t: print(f"[you] {t}", flush=True),
        on_agent_text=lambda t: print(f"[vibey] {t}", flush=True))
