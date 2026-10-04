#!/usr/bin/env python3
"""
reachy_telegram.py — text Vibey from anywhere (@vibey_ai_bot).

A stdlib-only Telegram bridge:
- Messages go to the chat service's /ask, which answers with the same brain as
  the voice (reachy_brain: same persona, lessons, tools and recent conversation,
  spoken or texted). The reply is a text, never spoken; a live voice session is
  told quietly. Texting works while Vibey is asleep, without waking it.
- `say: something` speaks the text verbatim on the robot.
- /photo sends a live frame from Vibey's camera.
- /status sends a one-line health check of the whole stack.
- VibeVerse happenings (joins, mentions, greetings) are pushed to you as they
  happen, from the avatar's status feed.
- Vibey can also text OUT, to people who opted in: the person messages the bot
  and replies YES, the owner runs `/allow <id> <nickname>`, and from then on the
  voice brain's `send_text_message` tool can reach them by nickname. See
  `send_to_contact` below. Anyone can reply STOP to be forgotten.
- Vibey can start a conversation with its owner rather than only answering one:
  auto-sleep notices, VibeVerse happenings, and whatever the voice brain
  decides is worth a text. Bounded and switchable — see `notify_owner` and
  `/proactive`.

Pairing: the FIRST person to message the bot becomes the owner (saved to
.telegram_state.json); everyone else gets a walled-off guest chat. Delete that
file to re-pair.

    python3 reachy_telegram.py
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid

import reachy_events
import reachy_privacy
from pathlib import Path

from reachy_voice import load_env, say

load_env()

# Vibey's OWN bot token, never the OpenClaw one.
#
# Telegram allows exactly one long-poller per token. Pointed at
# TELEGRAM_BOT_TOKEN this polled the same @jack_mielke_bot as the Vibey Claw
# OpenClaw gateway, both sides got 409 Conflict, and both died — which is why
# this file has been switched off in the watchdog since 2026-08-25.
#
# So it reads its own variable and refuses to fall back. Falling back is the
# behaviour that caused the outage: the shared token is always present in .env,
# so a fallback silently re-creates the conflict every time someone starts this
# without thinking about it. Absent its own token, this service does nothing at
# all, loudly.
TOKEN = os.environ.get("TELEGRAM_VIBEY_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
# Filled in by run() from getMe, so the opt-in instructions name the bot people
# actually have to message rather than a handle written down months ago.
BOT_HANDLE = os.environ.get("TELEGRAM_VIBEY_HANDLE", "").strip().lstrip("@")
CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8772").rstrip("/")
CAM_URL = os.environ.get("CAM_URL", "http://localhost:8771").rstrip("/")
VERSE_URL = os.environ.get("VERSE_URL", "http://localhost:8774").rstrip("/")
STATE_PATH = Path(__file__).parent / ".telegram_state.json"

SERVICES = {  # name → health URL, for /status
    "camera": f"{CAM_URL}/status",
    "chat": f"{CHAT_URL}/state",
    "memory": "http://localhost:8773/current",
    "dashboard": "http://localhost:8770/perception",
    "vibeverse": f"{VERSE_URL}/status",
}


def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    STATE_PATH.write_text(json.dumps(d))


def _tg(method: str, params: dict, timeout: float = 65.0):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# Set per handler thread when the webhook already told this sender "i'm not
# awake right now". The first thing sent back in that thread gets the prefix,
# so the late answer reads as a follow-up rather than a non sequitur.
_tl = threading.local()
WOKE_PREFIX = "ok i'm up! "


def _take_prefix(chat_id) -> str:
    """Only the chat that got the offline note, and only once."""
    if getattr(_tl, "prefix_chat", None) != chat_id:
        return ""
    _tl.prefix_chat = None
    return WOKE_PREFIX


def _who(chat_id) -> str:
    """A display name for the stream; never the raw id."""
    st = _state()
    if chat_id == st.get("owner"):
        return st.get("owner_name") or "Jack"
    return (st.get("guests") or {}).get(str(chat_id)) or "someone"


def _ev(text: str, detail: dict | None = None, icon: str = "✈") -> None:
    reachy_events.emit("telegram", text, detail=detail, source="telegram", icon=icon)


def _send(chat_id: int, text: str) -> None:
    text = _take_prefix(chat_id) + text
    owner = chat_id == _state().get("owner")
    _ev(f"replied to {_who(chat_id)}: {reachy_events.short(text, 90 if owner else 50)}", icon="↗")
    try:
        for chunk in [text[i:i + 3800] for i in range(0, max(len(text), 1), 3800)]:
            _tg("sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"[tg] send failed: {e}", flush=True)


def _send_photo(chat_id: int, jpeg: bytes, caption: str) -> None:
    boundary = f"----tg{uuid.uuid4().hex}"
    parts = []
    for k, v in (("chat_id", str(chat_id)), ("caption", caption)):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                  f'name="photo"; filename="vibey.jpg"\r\n'
                  f"Content-Type: image/jpeg\r\n\r\n").encode())
    body = b"".join(parts) + jpeg + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{API}/sendPhoto", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=30).read()


def _send_video(chat_id: int, mp4: bytes, caption: str) -> None:
    boundary = f"----tg{uuid.uuid4().hex}"
    parts = []
    for k, v in (("chat_id", str(chat_id)), ("caption", caption)):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                  f'name="video"; filename="vibey.mp4"\r\n'
                  f"Content-Type: video/mp4\r\n\r\n").encode())
    body = b"".join(parts) + mp4 + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{API}/sendVideo", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=60).read()


def _send_voice_note(chat_id: int, text: str) -> bool:
    """Speak the reply INTO TELEGRAM as a voice note (does not play in the
    room): ElevenLabs mp3 → ogg/opus via ffmpeg → sendVoice."""
    import subprocess
    import tempfile
    try:
        from reachy_voice import tts
        mp3 = tts(text[:600])
        with tempfile.TemporaryDirectory() as tmp:
            src_p, ogg_p = os.path.join(tmp, "v.mp3"), os.path.join(tmp, "v.ogg")
            open(src_p, "wb").write(mp3)
            r = subprocess.run(["ffmpeg", "-y", "-i", src_p, "-c:a", "libopus",
                                "-b:a", "32k", ogg_p],
                               capture_output=True, timeout=60)
            if r.returncode != 0:
                return False
            ogg = open(ogg_p, "rb").read()
        boundary = f"----tg{uuid.uuid4().hex}"
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="chat_id"\r\n\r\n{chat_id}\r\n'.encode()]
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                      f'name="voice"; filename="vibey.ogg"\r\n'
                      f"Content-Type: audio/ogg\r\n\r\n").encode())
        body = b"".join(parts) + ogg + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{API}/sendVoice", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        urllib.request.urlopen(req, timeout=60).read()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[tg] voice note failed: {e}", flush=True)
        return False


def _post_json(url: str, body: dict, timeout: float = 300.0):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def _get_json(url: str, timeout: float = 6.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Outbound: Vibey texting a human who asked to be textable.
#
# Telegram already enforces half of consent — a bot cannot open a chat, the
# person has to message @vibey_ai_bot first. We add the other half: they say
# YES, and the owner approves them with a nickname. So nobody gets texted by a
# robot just because someone in the room said their name out loud.
#
# Stored per contact: a chat id, a nickname, an approval time, and send
# timestamps for rate limiting. Never the message text, never a phone number.
# --------------------------------------------------------------------------- #
CONTACTS_PATH = Path(__file__).parent / ".telegram_contacts.json"
MSG_MAX_CHARS = 600
MSG_PER_HOUR = 6
MSG_SIGNATURE = "\n\n— sent by Vibey 🤖, Jack's desk robot. Reply STOP to stop."


def _contacts() -> dict:
    try:
        d = json.loads(CONTACTS_PATH.read_text())
    except Exception:
        d = {}
    d.setdefault("contacts", {})
    d.setdefault("pending", {})
    return d


def _save_contacts(d: dict) -> None:
    CONTACTS_PATH.write_text(json.dumps(d, indent=2))


def contact_names() -> list:
    """Nicknames Vibey is allowed to text, for the voice brain to offer."""
    return sorted(_contacts()["contacts"])


def optin_help(prefix: str = "") -> str:
    """The fallback: what to do when the person isn't linked yet."""
    names = contact_names()
    who = ("I can text: " + ", ".join(names) + "."
           if names else "Nobody has opted in yet.")
    handle = f"@{BOT_HANDLE}" if BOT_HANDLE else "my Telegram bot"
    return (f"{prefix}{who} To add someone, they message {handle} on "
            "Telegram themselves and reply YES — I can't open a chat with a "
            "stranger and I won't try. Then Jack approves them from his Telegram.")


def send_to_contact(name: str, text: str) -> str:
    """Send one short text to an approved contact. Returns a sayable result."""
    body = " ".join(str(text or "").split())
    if not body:
        return "There's nothing to send."
    if not TOKEN:
        return "I'm not linked to Telegram at all, so I can't text anyone yet."
    slug = str(name or "").strip().lower()
    d = _contacts()
    entry = d["contacts"].get(slug)
    if not entry:
        return optin_help(f"I don't have anyone called {name} to text. ")
    now = time.time()
    recent = [t for t in entry.get("sent", []) if now - t < 3600]
    if len(recent) >= MSG_PER_HOUR:
        return (f"I've already sent {slug} {MSG_PER_HOUR} messages this hour, "
                "so I'm holding off rather than pestering them.")
    trimmed = body[:MSG_MAX_CHARS]
    try:
        _tg("sendMessage", {"chat_id": entry["chat_id"],
                            "text": trimmed + MSG_SIGNATURE}, timeout=15)
    except Exception as e:  # noqa: BLE001 — blocked, deleted account, no network
        return f"That didn't go through — {e}"
    entry["sent"] = recent + [now]
    _save_contacts(d)
    print(f"[tg] sent {len(trimmed)} chars to {slug}", flush=True)  # never the text
    return (f"Sent to {slug}."
            + (" I trimmed it to fit." if len(body) > MSG_MAX_CHARS else ""))


# --------------------------------------------------------------------------- #
# Proactive: Vibey starting the conversation instead of answering it.
#
# Everything else in this file replies to something. This is the other
# direction — Vibey deciding on its own that Jack wants to know a thing while
# he is nowhere near the robot. That is a notification nobody installed, so it
# gets three bounds:
#
#   opt-in  pairing IS the consent — Telegram won't let a bot open a chat, the
#           owner messaged it first — and `/proactive off` revokes it in one
#           word, the way STOP does for guests. Only the paired owner is ever
#           pushed to; contacts keep the stricter approval flow above.
#   quiet   nothing overnight unless it is marked urgent.
#   rate    a handful an hour, then it holds its tongue.
#
# Privacy: only things the owner could already read off his own dashboard go
# out here. Room transcripts and anything a guest said never do.
# --------------------------------------------------------------------------- #
QUIET_HOURS = (23, 8)       # from 23:00 until 08:00, local time
PROACTIVE_PER_HOUR = 6
_proactive_sent: list = []


def proactive_on() -> bool:
    """Default on: the owner paired with the bot, and the sleep and VibeVerse
    notices have always worked this way. The switch is here to turn it off."""
    return bool(_state().get("proactive", True))


def notify_owner(text: str, urgent: bool = False) -> str:
    """Text the owner unprompted. Returns a line that is safe to say out loud."""
    body = " ".join(str(text or "").split())[:MSG_MAX_CHARS]
    if not body:
        return "There's nothing to send."
    owner = _state().get("owner")
    if not owner:
        return "Nobody's paired with my Telegram bot, so there's no one to text."
    if not proactive_on():
        return "Jack's switched my unprompted texts off, so I'll keep it to myself."
    hour = time.localtime().tm_hour
    if not urgent and (hour >= QUIET_HOURS[0] or hour < QUIET_HOURS[1]):
        return "It's the middle of the night — that can wait until morning."
    now = time.time()
    _proactive_sent[:] = [t for t in _proactive_sent if now - t < 3600]
    if len(_proactive_sent) >= PROACTIVE_PER_HOUR:
        return (f"I've already texted Jack {PROACTIVE_PER_HOUR} times this hour, "
                "so I'm holding off rather than pestering him.")
    try:
        _tg("sendMessage", {"chat_id": owner, "text": body}, timeout=15)
    except Exception as e:  # noqa: BLE001
        return f"That didn't go through — {e}"
    _proactive_sent.append(now)
    print(f"[tg] proactive note to owner, {len(body)} chars", flush=True)  # never the text
    return "Texted Jack."


GUEST_PER_HOUR = 30
GUEST_PHOTOS_PER_HOUR = 5
GUEST_ACTIONS_PER_HOUR = 10
_guest_actions: dict = {}
_guest_photos: dict = {}
_guest_sent: dict = {}

GUEST_WELCOME = (
    "hey 👋 i'm vibey, a little robot who lives on jack's desk.\n\n"
    "text me whatever, i'm down to chat. /photo shows you what i'm looking at, "
    "/wave and /whistle and i'll do it in the room.\n\n"
    "my whole brain is open source: https://github.com/jackmielke/vibey-robot\n\n"
    "if you're cool with me sending you the odd message later, reply YES. "
    "STOP any time and i'll leave you alone."
)


def _handle_guest(chat_id: int, msg: dict) -> None:
    """Anyone who isn't the owner. They can chat, and opt in or out of being
    texted. Guest turns go to a walled-off brain (see _guest_turn in
    reachy_chat.py): no tools, nothing said in the room, no commands."""
    if msg["chat"].get("type") != "private":
        return  # in a group it would answer every message anyone sends
    raw = (msg.get("text") or "").strip()
    low = raw.lower().strip("/ !.")
    d = _contacts()
    mine = [s for s, e in d["contacts"].items() if e.get("chat_id") == chat_id]
    owner = _state().get("owner")
    if low in ("stop", "unsubscribe", "forget me"):
        for s in mine:
            d["contacts"].pop(s, None)
        d["pending"].pop(str(chat_id), None)
        _save_contacts(d)
        _send(chat_id, "Done — I won't message you again. 🤖")
        if owner and mine:
            _send(owner, f"📵 {mine[0]} opted out of my messages.")
        return
    name = (msg["chat"].get("first_name") or msg["chat"].get("username")
            or "someone")
    if low == "start":
        _send(chat_id, GUEST_WELCOME)
        return
    st = _state()
    g = st.setdefault("guests", {})
    if g.get(str(chat_id)) != name:
        g[str(chat_id)] = name
        _save_state(st)
    pend = d["pending"].get(str(chat_id)) or {"name": name}
    if low in ("yes", "y", "yes please", "i consent", "start yes"):
        pend["consented"] = True
        d["pending"][str(chat_id)] = pend
        _save_contacts(d)
        _send(chat_id, "Thank you — noted. Jack approves it too, and then I may "
                       "occasionally text you. Reply STOP any time.")
        if owner:
            _send(owner, f"✅ {name} consented to being texted by me.\n"
                         f"Approve with:  /allow {chat_id} <nickname>")
        return
    if low == "photo" and reachy_privacy.is_on():
        _send(chat_id, reachy_privacy.CLOSED)
        return
    if low == "photo":
        # Anyone can peek, but it's Jack's room: rate limited, never while
        # incognito, and Jack is told every time.
        dials = _get_json(f"{CHAT_URL}/dials") or {}
        if dials.get("incognito"):
            _send(chat_id, "eyes closed rn 🙈 try later")
            return
        now = time.time()
        recent = [t for t in _guest_photos.get(chat_id, []) if now - t < 3600]
        if len(recent) >= GUEST_PHOTOS_PER_HOUR:
            _send(chat_id, "that's enough peeking for now 👀 try in a bit")
            return
        _guest_photos[chat_id] = recent + [now]
        _photo(chat_id, "hi from vibey's desk 👋")
        if owner:
            _send(owner, f"👀 {name} grabbed a /photo")
        return
    if low in ACTIONS:
        now = time.time()
        recent = [t for t in _guest_actions.get(chat_id, []) if now - t < 3600]
        if len(recent) >= GUEST_ACTIONS_PER_HOUR:
            _send(chat_id, "i'm all waved out, try later 😅")
            return
        if _act(chat_id, low):
            _guest_actions[chat_id] = recent + [now]
            _note_voice_session(f"/{low}", f"(you did a {low} in the room)",
                                who=f"{name} (a guest, via Telegram)")
        return
    if not raw or raw.startswith("/"):
        _send(chat_id, "here you can /photo, /wave or /whistle. otherwise just text me")
        return
    # Asleep is fine: a guest turn is text only and touches nothing in the room.
    now = time.time()
    recent = [t for t in _guest_sent.get(chat_id, []) if now - t < 3600]
    if len(recent) >= GUEST_PER_HOUR:
        _send(chat_id, "ok i need a breather, talk in a bit?")
        return
    _guest_sent[chat_id] = recent + [now]
    try:
        _tg("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:  # noqa: BLE001
        pass
    try:
        out = _post_json(f"{CHAT_URL}/ask", {"text": raw[:800], "channel": "guest",
                                             "chat_id": chat_id, "name": name})
        reply = (out or {}).get("reply") or "hmm, lost my train of thought"
        _send(chat_id, reply)
        # So the robot in the room knows who's been texting it and can bring
        # it up: "Sam just told me it's her birthday".
        _note_voice_session(raw[:400], reply, who=f"{name} (a guest, via Telegram)")
    except Exception as e:  # noqa: BLE001
        print(f"[tg] guest turn failed: {e}", flush=True)
        _send(chat_id, "brain's lagging, try me again in a sec")


# Phrases, not keywords.
#
# A bare \bawake\b matched "how awake are the neighbours", and the price of a
# false positive is the robot standing up and opening a paid realtime session
# because of a passing remark. Every alternative here is addressed AT it.
_WAKE_INTENT = re.compile(
    r"(wake up|wakey|you awake|are you awake|you up\b|are you up\b|"
    r"you there\b|get up\b|^good morning|morning vibey|come back|"
    r"turn (yourself )?on\b|switch (yourself )?on\b|boot up|"
    r"rise and shine|^wake\b|^awake\b)")


def _sfx(chat_id: int, name: str) -> None:
    """Owner only. Plays on the robot speaker even while asleep: asking for a
    sound by name is as explicit as it gets. Goes through the dashboard's
    /sfx, which owns the one speaker channel (queueing, ducking music)."""
    import reachy_sfx
    if not name:
        groups: dict = {}
        for e in reachy_sfx.catalog():
            groups.setdefault(e.get("group") or "other", []).append(e["name"])
        _send(chat_id, "🔊 sound effects — /sfx <name> (loose names work, "
              "\"vader\", \"pew\")\n\n" + "\n".join(
                  f"{g}: {', '.join(n)}" for g, n in groups.items()))
        return
    key = reachy_sfx.resolve(name)
    if not key:
        _send(chat_id, f"no sound called \"{name}\". /sfx for the list")
        return
    out = _post_json("http://localhost:8770/sfx", {"name": key}, timeout=15)
    if (out or {}).get("ok"):
        _send(chat_id, f"🔊 {key}")
    else:
        _send(chat_id, f"couldn't play {key} ({(out or {}).get('error', 'dashboard not answering')})")


def _asleep() -> bool:
    """Asleep, or on stage 1-2, where the cloud (and so texting) is off."""
    st = _get_json(f"{CHAT_URL}/state") or {}
    return bool(st.get("asleep")) or (st.get("stage") or 3) < 3


def _photo(chat_id: int, caption: str = "what I'm seeing right now 👁️") -> None:
    if reachy_privacy.is_on():
        _send(chat_id, reachy_privacy.CLOSED + ". /privacy off to open them")
        return
    try:
        with urllib.request.urlopen(f"{CAM_URL}/frame.jpg", timeout=8) as r:
            jpeg = r.read()
        _send_photo(chat_id, jpeg, caption)
        _ev(f"sent a photo to {_who(chat_id)}", detail={"caption": caption}, icon="📷")
    except urllib.error.HTTPError as e:
        # The camera now refuses to pass off an old frame as a photo, so
        # this is the honest branch rather than the broken one. Say which
        # kind of nothing it is: a wedged capture loop and a dark room look
        # identical in a photo, and only one of them is worth restarting.
        detail = ""
        try:
            info = json.loads(e.read() or b"{}")
            if info.get("age") is not None:
                detail = f" — last frame was {info['age']:.0f}s ago"
        except Exception:  # noqa: BLE001
            pass
        _send(chat_id, f"📷 my eyes aren't giving me anything fresh{detail}. "
                       "I'll try to get them back — ask again in a minute.")
    except Exception as e:  # noqa: BLE001
        _send(chat_id, f"camera's not answering ({e})")


ACTIONS = {"wave": "👋", "whistle": "🎶"}


def _act(chat_id: int, emote: str) -> bool:
    """Wave or whistle in the room. Stage 1-2 keep the body up, so only a real
    sleep (motors off) is in the way — and asking for a wave is asking it to
    get up, so it wakes first rather than refusing."""
    st = _get_json(f"{CHAT_URL}/state") or {}
    if st.get("asleep"):
        try:
            _post_json(f"{CHAT_URL}/wake", {}, timeout=30)
        except Exception as e:  # noqa: BLE001
            _send(chat_id, f"couldn't wake up to {emote} ({e})")
            return False
        for _ in range(20):   # wait out the wake-up animation
            time.sleep(1)
            if not (_get_json(f"{CHAT_URL}/state") or {}).get("asleep"):
                break
        time.sleep(2)
    try:
        out = _post_json("http://localhost:8770/emote", {"name": emote}, timeout=15)
        if not (out or {}).get("ok"):
            raise RuntimeError("didn't take")
        _send(chat_id, ACTIONS[emote])
        return True
    except Exception as e:  # noqa: BLE001
        _send(chat_id, f"couldn't {emote} ({e})")
        return False


def _dials(chat_id: int, change: dict, ok: str) -> None:
    try:
        out = _post_json(f"{CHAT_URL}/dials", change, timeout=20)
        _send(chat_id, f"couldn't: {out['error']}" if out.get("error") else ok)
    except Exception as e:  # noqa: BLE001
        _send(chat_id, f"couldn't reach the chat service ({e})")


def _now_line() -> str:
    """What the dashboard's status pill says, plus the switches."""
    st = _get_json(f"{CHAT_URL}/state") or {}
    d = _get_json(f"{CHAT_URL}/dials") or {}
    if not st:
        return "chat service is down, try /status"
    brain = "GPT-Live" if d.get("voice_brain") == "live" else "Realtime 2.1"
    sc = st.get("scribe") or {}
    if st.get("off"):
        head = "⚫ off"
    elif sc.get("on"):
        head = f"📝 taking notes, {sc.get('minutes', 0)} min in, not talking"
    elif st.get("asleep"):
        head = "😴 asleep"
    else:
        head = f"🟢 voice on · {brain}"
    yn = lambda k: "on" if d.get(k) else "off"
    return (f"{head}\n\n"
            f"mic {'muted' if d.get('muted') else 'live'} · volume {d.get('volume', '?')}\n"
            f"listening {yn('listening')} · tracking {yn('face_tracking')}\n"
            f"incognito {yn('incognito')} · think aloud {yn('think_aloud')}\n"
            f"brain {brain}")


def _power(chat_id: int, wake: bool) -> None:
    """Wake or sleep the whole robot.

    Posts to the CHAT service, not the viewer's /power. /power drives the motors
    and nothing else, so /wake used to leave the robot sitting up with its eyes
    open and no conversation running — awake in the only sense that does not
    matter. The chat endpoint is the one that enables motors, plays the chime and
    opens the realtime session.
    """
    try:
        if wake and (_get_json(f"{CHAT_URL}/state") or {}).get("off"):
            # Switched OFF: "turn on" from the phone means the switch, and
            # switching on brings the body up by itself.
            _post_json(f"{CHAT_URL}/off", {"off": False}, timeout=30)
            time.sleep(3)
            st = _get_json(f"{CHAT_URL}/state") or {}
            _send(chat_id, "🌅 switched on — coming up." if not st.get("asleep") else
                  "🔌 switched on, but I can't reach my body — battery dead or "
                  "off the wifi? Not starting voice.")
            return
        _post_json(f"{CHAT_URL}/wake" if wake else f"{CHAT_URL}/sleep",
                   {}, timeout=30)
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read() or b"{}").get("error") or str(e)
        except Exception:  # noqa: BLE001
            why = str(e)
        _send(chat_id, f"🔌 {why}" if e.code == 503 else
              f"couldn't {'wake' if wake else 'sleep'} ({why})")
        return
    except Exception as e:  # noqa: BLE001
        _send(chat_id, f"couldn't {'wake' if wake else 'sleep'} ({e})")
        return
    _send(chat_id, "🌅 waking up — give me a few seconds, then just talk."
          if wake else "😴 going to sleep.")


def _dispatch_code(chat_id: int, task: str) -> None:
    """Hand a task to Claude Code and report back here when it lands."""
    import reachy_agent

    def done(job: dict) -> None:
        # Runs on the agent's worker thread. A send that throws here would kill
        # that thread silently, so it never gets to.
        try:
            _send(chat_id, "🛠️ " + reachy_agent._describe(job))
        except Exception as e:  # noqa: BLE001
            print(f"[tg] job report failed: {e}", flush=True)

    job = reachy_agent.dispatch(task, on_done=done)
    # dispatch() distinguishes "could not attempt" from "failed", and the
    # difference matters more over text than out loud: nothing was started and
    # nothing changed, so promising to report back would be a lie you would only
    # discover hours later when no message arrived.
    if job.get("state") == "unavailable" or not job.get("id"):
        _send(chat_id, "couldn't start it — "
                       + (job.get("spoken") or job.get("error")
                          or "Claude Code is unreachable from here."))
        return
    caveat = ("" if job.get("verified")
              else "\n(I haven't confirmed the agent is reachable, so this may "
                   "not get anywhere.)")
    _send(chat_id, f"🛠️ on it — job {job['id']}. It runs for a few minutes; "
                   f"I'll text you when it lands.{caveat}")


def _handle(chat_id: int, text: str) -> None:
    text = text.strip()
    low = text.lower()
    if low in ("/start", "/help"):
        _send(chat_id,
              "hey it's vibey, the actual robot on your desk 🤖\n\n"
              "just text me and i'll text back, same brain as when we talk out "
              "loud, and i remember both. texts aren't said in the room. "
              "anyone else who finds me can chat too, but they only get "
              "texts: no commands, nothing said in the room.\n\n"
              "say: <text> — I'll speak it verbatim\n"
              "/sfx — list my sound effects · /sfx <name> plays one in the room\n"
              "/now — what I'm doing + every switch\n"
              "/talk, /talk off — voice on/off · /mute, /unmute\n"
              "/volume 0-100|up|down (/volume start N = the level I wake at)\n"
              "/brain live|realtime\n"
              "/listening, /tracking, /incognito, /thinkaloud on|off\n"
              "/frontdesk on|off|status — scan Luma tickets at the door (load <csv>, export)\n"
              "/photo — see through my eyes right now\n"
              "/clip — an 8-second video through my eyes\n"
              "/timelapse — today so far, one frame a minute\n"
              "/status — stack health\n"
              "/alarm 07:30 [daily] — wake-up show (/alarm off clears)\n"
              "/sleep, /wake — or just say \"you awake?\" and I'll get up\n"
              "/scribe — listen quietly and text you notes (/scribe off to finish)\n"
              "/code <task> — set Claude Code on this repo, I'll report back\n"
              "/jobs — what Claude Code is doing\n"
              "/voicenotes on|off — replies as voice messages too\n"
              "/contacts — who I'm allowed to text (and how to add someone)\n"
              "/proactive on|off — whether I start conversations or only reply\n"
              "/cost — what I've cost you, by source, vs budget\n"
              "/budget raise [n] | off | on — override the spend cap\n"
              "/verse — what's happening in my VibeVerse lobby")
        return
    if low.startswith("/privacy"):
        arg = low.replace("/privacy", "").strip()
        if arg in ("on", "off"):
            reachy_privacy.set_on(arg == "on")
        _send(chat_id, "🙈 privacy mode ON: I still talk and text, but no photos, "
                       "clips or looking around" if reachy_privacy.is_on()
                       else "👀 privacy mode OFF: /photo works, for guests too")
        return
    if low.startswith("/frontdesk"):
        # /frontdesk on|off|status · /frontdesk load <csv path> · /frontdesk export
        parts = text.split(None, 2)
        arg = parts[1].lower() if len(parts) > 1 else "status"
        try:
            if arg in ("on", "off"):
                req = urllib.request.Request(f"{CHAT_URL}/frontdesk/{arg}", data=b"{}",
                                             headers={"Content-Type": "application/json"},
                                             method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        st = json.loads(r.read() or b"{}")
                except urllib.error.HTTPError as e:
                    st = json.loads(e.read() or b"{}")
                if st.get("error"):
                    _send(chat_id, "🚪 " + st["error"])
                    return
            elif arg == "load" and len(parts) > 2:
                st = _post_json(f"{CHAT_URL}/frontdesk/load", {"path": parts[2]}, timeout=20)
                _send(chat_id, f"🚪 loaded {st.get('guests', 0)} guests" if not st.get("error")
                      else "🚪 " + st["error"])
                return
            elif arg == "export":
                st = _post_json(f"{CHAT_URL}/frontdesk/export", {}, timeout=20)
                _send(chat_id, f"🚪 exported to {st.get('path')}")
                return
            st = _get_json(f"{CHAT_URL}/frontdesk/status", timeout=20) or {}
        except Exception as e:  # noqa: BLE001
            _send(chat_id, f"🚪 front desk unreachable: {e}")
            return
        last = "\n".join(f"· {x['name']} {x['at'][11:16]} ({x['method']})"
                         for x in st.get("last", []))
        _send(chat_id, f"🚪 front desk {'ON' if st.get('on') else 'off'} · "
                       f"{st.get('checked_in', 0)}/{st.get('guests', 0)} checked in"
                       + (f"\n{last}" if last else "")
                       + (f"\n⚠️ {st['error']}" if st.get("error") else ""))
        return
    if low.startswith("/drive"):
        # /drive forward 1 slow  ·  /drive stop
        import reachy_rover
        parts = low.split()
        if len(parts) < 2:
            _send(chat_id, "usage: /drive forward|back|left|right|spin_left|spin_right|stop [seconds] [slow|medium|fast]")
            return
        secs = float(parts[2]) if len(parts) > 2 and parts[2].replace(".", "", 1).isdigit() else 1.0
        spd = parts[-1] if parts[-1] in reachy_rover.SPEEDS else "medium"
        _send(chat_id, "🛞 " + reachy_rover.drive(parts[1], secs, spd))
        return
    if low.startswith("/guests"):
        g = _state().get("guests", {})
        _send(chat_id, "people who've texted me:\n" + "\n".join(sorted(set(g.values())))
              if g else "nobody yet")
        return
    if low.startswith("/tell "):
        # /tell Sam this is hilarious  ->  Sam gets "jack says: this is hilarious"
        parts = text.split(None, 2)
        if len(parts) < 3:
            _send(chat_id, "usage: /tell <name> <message>")
            return
        who, msg = parts[1].lower(), parts[2]
        g = _state().get("guests", {})
        hits = [(cid, n) for cid, n in g.items() if n.lower().startswith(who)]
        if len(hits) != 1:
            _send(chat_id, f"{'no one' if not hits else 'more than one person'} called "
                           f"{parts[1]}. /guests lists them")
            return
        cid, n = hits[0]
        _send(int(cid), f"jack says: {msg}")
        _send(chat_id, f"sent to {n} ✅")
        return
    if low.startswith("/scribe") or low in ("take notes", "just listen"):
        arg = low.replace("/scribe", "").strip()
        st = _get_json(f"{CHAT_URL}/state") or {}
        sc = st.get("scribe") or {}
        if arg in ("", "status") and low.startswith("/scribe") and sc.get("on"):
            _send(chat_id, f"📝 taking notes, {sc.get('minutes', 0)} min in, "
                           f"{sc.get('lines', 0)} lines. /scribe off to wrap up")
            return
        on = arg not in ("off", "stop", "done")
        try:
            out = _post_json(f"{CHAT_URL}/scribe", {"on": on}, timeout=40)
            if out.get("error"):
                _send(chat_id, f"couldn't: {out['error']}")
            elif on:
                _send(chat_id, "📝 ok, head down, just listening. /scribe off "
                               "(or say \"stop taking notes\") and i'll text you the notes")
            else:
                _send(chat_id, "wrapping up, notes coming in a sec")
        except Exception as e:  # noqa: BLE001
            _send(chat_id, f"couldn't reach the chat service ({e})")
        return
    # ---- remote control: everything the dashboard does, as commands ----
    cmd, _, arg = low.partition(" ")
    arg = arg.strip()
    onoff = {"on": True, "off": False, "yes": True, "no": False}
    if cmd in ("/talk", "/voice"):
        _power(chat_id, wake=(arg != "off"))
        return
    if cmd in ("/mute", "/unmute"):
        _dials(chat_id, {"muted": cmd == "/mute"},
               "🔇 mic muted, i can't hear anything. /unmute to undo"
               if cmd == "/mute" else "🎙️ mic's back on")
        return
    if cmd == "/volume":
        d = _get_json(f"{CHAT_URL}/dials") or {}
        cur, start = d.get("volume") or 70, d.get("start_volume")
        # `/volume start 85` sets what every WAKE comes back to, which is a
        # different question from how loud it is right now and used to have no
        # answer at all — you found out the next morning.
        head, _, tail = arg.partition(" ")
        if head == "start":
            if not tail.strip().isdigit():
                _send(chat_id, f"🔊 I wake up at {start if start is not None else '?'}. "
                               "/volume start 0-100 to change that")
                return
            v = max(0, min(100, int(tail.strip())))
            _dials(chat_id, {"start_volume": v},
                   f"🔊 I'll wake up at {v} from now on (I'm at {cur} right now)")
            return
        if arg in ("up", "+"):
            v = cur + 15
        elif arg in ("down", "-"):
            v = cur - 15
        elif arg.isdigit():
            v = int(arg)
        else:
            _send(chat_id, f"🔊 volume's at {cur}, and I wake up at "
                           f"{start if start is not None else '?'}. "
                           "/volume 0-100, up or down · /volume start 0-100")
            return
        v = max(0, min(100, v))
        _dials(chat_id, {"volume": v}, f"🔊 volume {v}")
        return
    if cmd == "/brain":
        pick = {"live": "live", "gpt-live": "live", "realtime": "realtime", "rt": "realtime"}.get(arg)
        if not pick:
            cur = (_get_json(f"{CHAT_URL}/brain") or {}).get("brain")
            _send(chat_id, f"🧠 on {cur}. /brain live or /brain realtime")
            return
        try:
            out = _post_json(f"{CHAT_URL}/brain", {"brain": pick}, timeout=20)
            _send(chat_id, f"🧠 switched to {pick}"
                  + (", restarting the conversation" if out.get("restarting") else ", used next time i wake"))
        except Exception as e:  # noqa: BLE001
            _send(chat_id, f"couldn't switch ({e})")
        return
    dial_cmds = {"/listening": ("listening", "listening to the room"),
                 "/tracking": ("face_tracking", "face tracking"),
                 "/incognito": ("incognito", "incognito (remembers nothing)"),
                 "/thinkaloud": ("think_aloud", "thinking out loud")}
    if cmd in dial_cmds:
        key, label = dial_cmds[cmd]
        if arg not in onoff:
            cur = (_get_json(f"{CHAT_URL}/dials") or {}).get(key)
            _send(chat_id, f"{label}: {'on' if cur else 'off'}. {cmd} on|off")
            return
        _dials(chat_id, {key: onoff[arg]}, f"{label}: {arg}")
        return
    if cmd in ("/battery", "/power"):
        _send(chat_id, _power_report()[1])
        return
    if cmd in ("/wave", "/whistle"):
        _act(chat_id, cmd[1:])
        return
    if cmd == "/speaker":
        pick = {"robot": "robot", "mac": "laptop", "laptop": "laptop", "macbook": "laptop"}.get(arg)
        if not pick:
            cur = (_get_json(f"{CHAT_URL}/dials") or {}).get("speaker_source")
            _send(chat_id, f"🔈 talking through the {'macbook' if cur == 'laptop' else 'robot'}. /speaker robot or /speaker mac")
            return
        _dials(chat_id, {"speaker_source": pick},
               f"🔈 now talking through the {'macbook' if pick == 'laptop' else 'robot'}")
        return
    if cmd == "/mic":
        pick = {"robot": "robot", "mac": "laptop", "laptop": "laptop", "macbook": "laptop"}.get(arg)
        if not pick:
            cur = (_get_json(f"{CHAT_URL}/dials") or {}).get("mic_source")
            _send(chat_id, f"👂 listening with the {'macbook' if cur == 'laptop' else 'robot'} mic. /mic robot or /mic mac")
            return
        _dials(chat_id, {"mic_source": pick},
               f"👂 now listening with the {'macbook' if pick == 'laptop' else 'robot'} mic")
        return
    if cmd == "/stage":
        if arg not in ("1", "2", "3"):
            st = _get_json(f"{CHAT_URL}/state") or {}
            _send(chat_id, f"on stage {st.get('stage', 3)}.\n/stage 1 robot alone\n"
                           "/stage 2 + mac\n/stage 3 + cloud (voice, telegram)")
            return
        try:
            out = _post_json(f"{CHAT_URL}/stage", {"stage": int(arg)}, timeout=60)
            _send(chat_id, f"couldn't: {out['error']}" if out.get("error") else
                  {"1": "1️⃣ robot alone. just the pi, onboard tracking",
                   "2": "2️⃣ + mac. wake word, faces, moves. no cloud",
                   "3": "3️⃣ + cloud. voice on, texts on"}[arg])
        except Exception as e:  # noqa: BLE001
            _send(chat_id, f"couldn't reach the chat service ({e})")
        return
    if cmd == "/now":
        _send(chat_id, _now_line())
        return
    if cmd == "/proactive":
        if arg not in onoff:
            _send(chat_id,
                  f"🔔 unprompted texts: {'on' if proactive_on() else 'off'}. "
                  f"/proactive on|off.\n\nOn, I'll start a conversation when "
                  f"something happens you'd want to know — at most "
                  f"{PROACTIVE_PER_HOUR} an hour, and nothing between "
                  f"{QUIET_HOURS[0]}:00 and 0{QUIET_HOURS[1]}:00 unless it "
                  f"can't wait. Off, I only ever reply.")
            return
        st = _state()
        st["proactive"] = onoff[arg]
        _save_state(st)
        _send(chat_id, "🔔 ok, i'll bring things up as they happen"
              if onoff[arg] else "🔕 ok, i'll only speak when spoken to")
        return
    if low.startswith("/contacts"):
        d = _contacts()
        lines = [f"· {s}" for s in sorted(d["contacts"])] or ["· nobody yet"]
        body = "📇 I'm allowed to text:\n" + "\n".join(lines)
        waiting = [f"· {p.get('name', '?')} → /allow {cid} <nickname>"
                   for cid, p in d["pending"].items() if p.get("consented")]
        if waiting:
            body += "\n\nsaid yes, waiting on you:\n" + "\n".join(waiting)
        body += ("\n\nTo add someone: they message me here and reply YES, then "
                 "you /allow them. /forget <nickname> unlinks them.")
        _send(chat_id, body)
        return
    if low.startswith("/allow"):
        parts = text.split()
        if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
            _send(chat_id, "usage: /allow <id> <nickname> — see /contacts")
            return
        cid, nick = parts[1], parts[2].lower()
        d = _contacts()
        pend = d["pending"].get(cid)
        if not pend or not pend.get("consented"):
            _send(chat_id, "They haven't said yes to me yet, so I won't add "
                           "them. Ask them to message me and reply YES.")
            return
        # The first name was only ever held to tell you who was asking.
        d["contacts"][nick] = {"chat_id": int(cid), "approved": time.time(),
                               "sent": []}
        d["pending"].pop(cid, None)
        _save_contacts(d)
        _send(chat_id, f"👍 I can now text {nick}.")
        _send(int(cid), "Approved — Jack's robot may text you now. "
                        "Reply STOP any time.")
        return
    if low.startswith("/forget"):
        parts = text.split()
        d = _contacts()
        entry = d["contacts"].pop(parts[1].lower(), None) if len(parts) > 1 else None
        if not entry:
            _send(chat_id, "usage: /forget <nickname> — see /contacts")
            return
        _save_contacts(d)
        _send(chat_id, f"forgot {parts[1].lower()}")
        _send(entry["chat_id"], "Jack unlinked you — I won't message you again.")
        return
    if low == "/photo":
        _photo(chat_id)
        return
    if low.startswith("/clip") and reachy_privacy.is_on():
        _send(chat_id, reachy_privacy.CLOSED + ". /privacy off to open them")
        return
    if low.startswith("/clip"):
        _send(chat_id, "🎬 recording 8 seconds…")
        try:
            out = _post_json("http://localhost:8770/capture",
                             {"type": "video", "seconds": 8}, timeout=90)
            name = (out or {}).get("name")
            if not name:
                raise RuntimeError("capture failed")
            with urllib.request.urlopen(
                    f"http://localhost:8770/captures/{name}", timeout=30) as r:
                mp4 = r.read()
            _send_video(chat_id, mp4, "8 seconds through my eyes 🎥")
        except Exception as e:  # noqa: BLE001
            _send(chat_id, f"clip failed ({e})")
        return
    if low.startswith("/alarm"):
        # /alarm 07:30 [daily] sets a wake-up show; /alarm off clears all
        import re as _re
        parts = text.split()
        if len(parts) >= 2 and parts[1].lower() == "off":
            Path(__file__).parent.joinpath("alarms.json").write_text("[]")
            _send(chat_id, "alarms cleared")
            return
        m = _re.search(r"([01]?\d|2[0-3]):([0-5]\d)", text)
        if not m:
            _send(chat_id, "usage: /alarm 07:30  or  /alarm 07:30 daily  or  /alarm off")
            return
        hhmm = f"{int(m.group(1)):02d}:{m.group(2)}"
        repeat = "daily" if "daily" in low else "once"
        p = Path(__file__).parent / "alarms.json"
        try:
            alarms = json.loads(p.read_text())
        except Exception:
            alarms = []
        alarms.append({"time": hhmm, "repeat": repeat,
                       "label": f"telegram alarm {hhmm}", "song": True})
        p.write_text(json.dumps(alarms, indent=2))
        _send(chat_id, f"wake-up show set for {hhmm} ({repeat})")
        return
    if low in ("/sleep", "/wake"):
        _power(chat_id, wake=low == "/wake")
        return
    if low.startswith("/code") or low.startswith("/claude"):
        task = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        if not task:
            _send(chat_id, "usage: /code <what you want changed>")
            return
        _dispatch_code(chat_id, task)
        return
    if low in ("/jobs", "/job"):
        import reachy_agent
        live = reachy_agent.running_jobs()
        if live:
            _send(chat_id, "🛠️ running:\n" + "\n".join(
                f"· {j.get('id', '?')} — {str(j.get('task', ''))[:70]}"
                if isinstance(j, dict) else f"· {j}" for j in live))
            return
        snap = reachy_agent.status() or {}
        _send(chat_id, "🛠️ " + (reachy_agent._describe(snap) if snap.get("id")
                                 else "nothing running."))
        return
    if low.startswith("/timelapse") and reachy_privacy.is_on():
        _send(chat_id, reachy_privacy.CLOSED + ". /privacy off to open them")
        return
    if low.startswith("/timelapse"):
        _send(chat_id, "🎞️ assembling the day's timelapse…")
        try:
            out = _post_json("http://localhost:8770/timelapse", {}, timeout=300)
            name = (out or {}).get("name")
            if not name:
                raise RuntimeError("not enough frames yet")
            with urllib.request.urlopen(
                    f"http://localhost:8770/captures/{name}", timeout=60) as r:
                mp4 = r.read()
            _send_video(chat_id, mp4, "the day through my eyes, one frame a minute 🎞️")
        except Exception as e:  # noqa: BLE001
            _send(chat_id, f"timelapse failed ({e})")
        return
    if low in ("/cost", "/credits", "/spend"):
        try:
            import reachy_cost
            reachy_cost.refresh_billed()
            report = reachy_cost.text_report()
        except Exception as e:  # noqa: BLE001
            _send(chat_id, f"can't read the meter ({e})")
            return
        c = _get_json(f"{CHAT_URL}/cost") or {}
        _send(chat_id, report + f"\nI sleep after {c.get('idle_sleep_minutes', 10):.0f} min "
              f"of quiet, or {c.get('voice_session_max_minutes', 20):.0f} min awake.")
        return
    if low.startswith("/budget"):
        import reachy_cost
        parts = low.split()
        arg = parts[1] if len(parts) > 1 else ""
        if arg == "raise":
            try:
                amt = float(parts[2].lstrip("$")) if len(parts) > 2 else 3.0
            except ValueError:
                amt = 3.0
            reachy_cost.budget_raise(amt)
            _send(chat_id, f"ok, +${amt:.2f} for today. /wake when you want me.\n\n"
                  + reachy_cost.text_report())
        elif arg == "off":
            reachy_cost.budget_off()
            _send(chat_id, "budget guard off until midnight. /budget on to re-arm.")
        elif arg == "on":
            reachy_cost.budget_on()
            _send(chat_id, "budget guard back on.\n\n" + reachy_cost.text_report())
        else:
            _send(chat_id, reachy_cost.text_report()
                  + "\n\n/budget raise [n] · /budget off · /budget on")
        return
    if low == "/status":
        lines = []
        for name, url in SERVICES.items():
            ok = _get_json(url) is not None
            lines.append(f"{'✅' if ok else '❌'} {name}")
        st = _get_json(f"{CHAT_URL}/state") or {}
        mode = "🎮 vibe" if st.get("vibe") else ("⚡ fast" if st.get("fast")
                                                 else st.get("mode", "?"))
        lines.append(f"🧠 brain: {mode}")
        _send(chat_id, "\n".join(lines))
        return
    if low == "/verse":
        v = _get_json(f"{VERSE_URL}/status") or {}
        ev = (v.get("events") or [])[-6:]
        who = ", ".join(v.get("agents") or []) or "nobody else around"
        body = f"📍 lobby pos {v.get('pos')} · with: {who}\n"
        body += "\n".join(f"· {e['kind']}: {e['text'][:90]}" for e in ev) \
            or "quiet so far"
        _send(chat_id, body)
        return
    if low.startswith("/voicenotes"):
        st = _state()
        st["voice_notes"] = "off" not in low
        _save_state(st)
        _send(chat_id, "voice notes " + ("ON — replies come as audio too" if st["voice_notes"] else "off"))
        return
    if low == "/sfx" or low.startswith("/sfx "):
        _sfx(chat_id, text[4:].strip())
        return
    if low.startswith("say:"):
        line = text[4:].strip()
        try:
            say(line)
            _send(chat_id, "🔊 said it")
        except Exception as e:  # noqa: BLE001
            _send(chat_id, f"couldn't speak ({e})")
        return
    # "wake up" is a thing you say, not a command you remember.
    #
    # /wake existed, but nobody asleep in another country types a slash command —
    # they type "you awake?". And the robot cannot answer that itself: while it
    # is asleep the realtime session is closed, so there is no brain listening to
    # notice it was asked. The text has to be understood here, before it is
    # forwarded to a brain that is not running.
    if _asleep() and _WAKE_INTENT.search(low):
        _power(chat_id, wake=True)
        return
    # Asleep (or stage 1-2) still texts back. The text brain runs in the chat
    # service, not the voice session, and is told the robot is asleep so it
    # won't reach for the body, the speaker or the camera. Only "wake up"
    # above wakes it.

    # A text that arrives DURING a conversation is a text, not a new
    # conversation.
    #
    # /ask spins up a separate turn: it answers from a different context, and
    # the room hears an answer to a question nobody in it asked. When a realtime
    # session is live, the message is handed to that session instead, framed as
    # what it is — something that arrived on the phone — so Vibey brings it up
    # in the conversation already happening, in the voice already talking.
    # A text gets a text back, always. If a voice session is live it is told
    # quietly afterwards (see _note_voice_session) and decides for itself
    # whether any of it is worth saying in the room.
    try:
        try:
            _tg("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=5)
        except Exception:  # noqa: BLE001 — cosmetic
            pass
        out = _post_json(f"{CHAT_URL}/ask", {"text": text, "channel": "telegram",
                                             "name": _state().get("owner_name") or "Jack"})
        reply = (out or {}).get("reply") or "(no reply)"
        _send(chat_id, reply)
        _note_voice_session(text, reply)
        if _state().get("voice_notes") and reply and not reply.startswith("("):
            _send_voice_note(chat_id, reply)
    except Exception as e:  # noqa: BLE001
        _send(chat_id, "ugh my brain's being slow, try me again in a sec")


def _note_voice_session(text: str, reply: str, who: str = "Jack") -> None:
    st = _get_json(f"{CHAT_URL}/state") or {}
    if not (st.get("openai") and not st.get("asleep")):
        return
    try:
        _post_json(f"{CHAT_URL}/sighting", {"silent": True,
                   "label": f"{who} texted (+ my reply)", "text": (
            f"[Context only, nobody in the room heard this: {who} texted you "
            f"\"{text[:400]}\" and you texted back \"{reply[:400]}\". Do not "
            f"read either out. Only bring it up if it naturally fits what is "
            f"happening in the room.]")}, timeout=10)
    except Exception:  # noqa: BLE001 — context is a nice-to-have
        pass


# The "/" menu in Telegram. Owner only: guests get no commands at all.
OWNER_COMMANDS = [
    ("now", "what i'm doing + all switches"),
    ("tell", "message someone who texted me: /tell sam lol"),
    ("drive", "wheels: /drive forward 1 · /drive stop"),
    ("guests", "who's texted me"),
    ("privacy", "eyes closed, still chats: on|off (default on)"),
    ("stage", "1 robot alone · 2 + mac · 3 + cloud"),
    ("mic", "listen with the robot or macbook mic"),
    ("speaker", "talk through the robot or macbook"),
    ("talk", "wake up and start voice (/talk off to stop)"),
    ("mute", "mute my mic"),
    ("unmute", "unmute my mic"),
    ("volume", "0-100, up or down · start N = the level I wake at"),
    ("brain", "live or realtime"),
    ("listening", "hear the room: on|off"),
    ("tracking", "follow faces: on|off"),
    ("incognito", "remember nothing: on|off"),
    ("thinkaloud", "narrate thoughts: on|off"),
    ("wave", "wave in the room"),
    ("whistle", "whistle a little tune"),
    ("sfx", "sound effects: /sfx lists, /sfx <name> plays"),
    ("photo", "see through my eyes right now"),
    ("clip", "an 8-second video through my eyes"),
    ("timelapse", "today so far, one frame a minute"),
    ("status", "stack health"),
    ("battery", "is my body powered and reachable"),
    ("alarm", "wake-up show: /alarm 07:30 [daily], /alarm off"),
    ("sleep", "put me to bed"),
    ("wake", "get me up"),
    ("scribe", "just listen and take notes: /scribe, /scribe off"),
    ("code", "set Claude Code on a task in my repo"),
    ("jobs", "what Claude Code is doing"),
    ("voicenotes", "replies as voice messages too: on|off"),
    ("contacts", "who I'm allowed to text"),
    ("proactive", "do I start conversations: on|off"),
    ("cost", "what I've cost you, by source, vs budget"),
    ("budget", "spend cap: /budget raise [n], off, on"),
    ("verse", "what's happening in my VibeVerse lobby"),
    ("help", "everything I can do"),
]


def _set_command_menu(owner) -> None:
    try:
        # Default scope is what everyone else sees in their "/" menu.
        _tg("setMyCommands", {"commands": json.dumps([
            {"command": "photo", "description": "see what i'm looking at"},
            {"command": "wave", "description": "i'll wave in the room"},
            {"command": "whistle", "description": "i'll whistle a tune"},
        ])}, timeout=10)
        if owner:
            _tg("setMyCommands", {
                "commands": json.dumps([{"command": c, "description": d}
                                        for c, d in OWNER_COMMANDS]),
                "scope": json.dumps({"type": "chat", "chat_id": owner}),
            }, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"[tg] command menu failed: {e}", flush=True)


def _sleep_watcher() -> None:
    """Tell the owner when the robot puts itself to bed, and what it cost.

    An auto-sleep that happens silently is indistinguishable from a crash, and
    the difference matters at two in the morning when the thing you are worried
    about is the bill.
    """
    was_asleep = None
    last_notice = 0.0
    while True:
        time.sleep(30)
        owner = _state().get("owner")
        st = _get_json(f"{CHAT_URL}/state")
        if not owner or st is None:
            continue
        now_asleep = bool(st.get("asleep"))
        if was_asleep is None:
            was_asleep = now_asleep
            continue
        if now_asleep and not was_asleep and time.time() - last_notice > 3 * 3600:
            last_notice = time.time()
            c = _get_json(f"{CHAT_URL}/cost") or {}
            # urgent: this one is worth MORE at 2am, not less — a silent
            # auto-sleep is indistinguishable from a crash, and the bill is
            # exactly what you are awake worrying about.
            notify_owner(f"😴 nobody said anything for a while, so I've gone to "
                         f"sleep. ${c.get('today', 0):.2f} today. "
                         f"Text me to wake me.", urgent=True)
        was_asleep = now_asleep


# ── Group chats ───────────────────────────────────────────────────────────
# Vibey reads along in groups and answers only when spoken to: an @mention, a
# reply to one of its messages, or its name in the text. Everything said is
# kept (last GROUP_KEEP lines per group) so it can bring earlier messages up.
# Same walled-off guest brain as strangers' DMs: no tools, none of Jack's
# memory. Telegram only delivers every group message if the bot's privacy
# mode is OFF (@BotFather → /setprivacy → Disable) or it's a group admin;
# otherwise it sees just mentions, replies and commands, which still works.
GROUP_KEEP = 200
GROUP_CONTEXT_LINES = 30
GROUP_PER_HOUR = 60
GROUP_DIR = Path(__file__).parent / ".telegram_groups"
_group_sent: dict = {}
_group_lock = threading.Lock()


def _group_log(chat_id: int) -> list:
    try:
        return json.loads((GROUP_DIR / f"{chat_id}.json").read_text())
    except Exception:
        return []


def _group_append(chat_id: int, who: str, text: str) -> None:
    with _group_lock:
        GROUP_DIR.mkdir(exist_ok=True)
        log = _group_log(chat_id)
        log.append({"who": who[:40], "text": text[:600], "ts": int(time.time())})
        (GROUP_DIR / f"{chat_id}.json").write_text(json.dumps(log[-GROUP_KEEP:]))


def _addressed(msg: dict) -> bool:
    text = (msg.get("text") or "")
    low = text.lower()
    handle = (BOT_HANDLE or "vibey_robot").lower()
    if f"@{handle}" in low or re.search(r"\bvibey\b", low):
        return True
    rep = (msg.get("reply_to_message") or {}).get("from") or {}
    return bool(rep.get("is_bot") and (rep.get("username") or "").lower() == handle)


_group_seen: set = set()


def _handle_group(chat_id: int, msg: dict) -> None:
    key = (chat_id, msg.get("message_id"))
    with _group_lock:
        if key in _group_seen:
            return  # an edit or a redelivery: answered once already
        _group_seen.add(key)
    raw = (msg.get("text") or "").strip()
    frm = msg.get("from") or {}
    name = frm.get("first_name") or frm.get("username") or "someone"
    title = msg["chat"].get("title") or "the group chat"
    _group_append(chat_id, name, raw)
    if not raw or not _addressed(msg):
        return
    low = raw.lower().split("@")[0].strip("/ !.")
    if raw.startswith("/") and low in ("photo", *ACTIONS):
        # Reuse the guest rules (rate limits, incognito, Jack pinged).
        m = dict(msg); m["text"] = "/" + low
        m["chat"] = dict(msg["chat"], type="private", first_name=name)
        _handle_guest(chat_id, m)
        return
    now = time.time()
    recent = [t for t in _group_sent.get(chat_id, []) if now - t < 3600]
    if len(recent) >= GROUP_PER_HOUR:
        return
    _group_sent[chat_id] = recent + [now]
    try:
        _tg("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:  # noqa: BLE001
        pass
    lines = _group_log(chat_id)[-GROUP_CONTEXT_LINES - 1:-1]
    context = (f"You're in a Telegram group chat called \"{title}\". Keep replies "
               "short and group-chat casual; only answer the person talking to you. "
               "Recent messages, oldest first:\n" +
               "\n".join(f"{l['who']}: {l['text']}" for l in lines))
    out = _post_json(f"{CHAT_URL}/ask", {"text": raw[:800], "channel": "guest",
                                         "chat_id": f"group:{chat_id}", "name": name,
                                         "context": context[:4000]})
    reply = (out or {}).get("reply") or "hmm, lost my train of thought"
    try:
        _tg("sendMessage", {"chat_id": chat_id, "text": _take_prefix(chat_id) + reply,
                            "reply_to_message_id": msg["message_id"]}, timeout=15)
        _ev(f"replied in {title}: {reachy_events.short(reply, 50)}", icon="↗")
    except Exception:  # noqa: BLE001
        _send(chat_id, reply)
    _group_append(chat_id, "Vibey", reply)
    _note_voice_session(raw[:400], reply, who=f"{name} (in the {title} group chat)")


# --------------------------------------------------------------------------- #
# Power. The Reachy Mini reports no battery percentage — not in its API, not in
# /sys/class/power_supply on the Pi. What there is: whether the body answers
# at all, and the Pi's own under-voltage flag, which trips as the battery sags.
# --------------------------------------------------------------------------- #
def _robot_host() -> str:
    url = os.environ.get("REACHY_URL", "http://192.168.12.240:8000")
    return urllib.parse.urlparse(url).hostname or ""


def _power_report() -> tuple[str, str]:
    """(level, line): level is ok | low | sagged | unreachable."""
    import subprocess
    host = _robot_host()
    if not (_get_json(f"http://{host}:8000/api/daemon/status", timeout=4)):
        return "unreachable", ("🔌 can't reach my body — battery's probably flat, "
                               "or it's off the wifi. Plug me in?")
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
             # The robot's name and IP move between networks; a read-only
             # voltage check on the LAN isn't worth a host-key prompt.
             "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "LogLevel=ERROR",
             f"pollen@{host}", "vcgencmd get_throttled; cut -d. -f1 /proc/uptime"],
            capture_output=True, text=True, timeout=10).stdout.split()
        flags = int(out[0].split("=")[1], 16)
        up_h = int(out[1]) / 3600
    except Exception:  # noqa: BLE001
        return "ok", "🔋 body's up and answering (couldn't read the voltage flags)."
    if flags & 0x1:
        return "low", ("🪫 battery's low — the voltage is sagging right now. "
                       "Plug me in soon.")
    if flags & 0x10000:
        return "sagged", (f"🔋 up and answering, but the voltage dipped at some point "
                          f"in the last {up_h:.0f}h — battery's getting low.")
    return "ok", f"🔋 body's up, voltage is fine (on for {up_h:.0f}h)."


def _power_watcher() -> None:
    """Text the owner when the battery sags or the body drops off. Once per
    change, not every check."""
    last = None
    while True:
        time.sleep(120)
        if not _state().get("owner"):
            continue
        level, line = _power_report()
        if level != last and level in ("low", "unreachable") and last is not None:
            notify_owner(line, urgent=level == "low")
        last = level


def run() -> None:
    global BOT_HANDLE
    if not TOKEN:
        print("[tg] TELEGRAM_VIBEY_TOKEN not set — Vibey needs its OWN bot, "
              "separate from the OpenClaw gateway's. Create one with @BotFather "
              "and put the token in .env as TELEGRAM_VIBEY_TOKEN.", flush=True)
        return
    me = _tg("getMe", {}, timeout=15)
    BOT_HANDLE = me["result"]["username"]
    # The check that keeps the 409 from coming back. If this ever ends up
    # holding the gateway's bot again, it says so and stops rather than
    # fighting it for updates and taking both down.
    if BOT_HANDLE == "jack_mielke_bot":
        print("[tg] REFUSING to start: TELEGRAM_VIBEY_TOKEN is the OpenClaw "
              "gateway's bot (@jack_mielke_bot). Two pollers on one token is "
              "the 409 that killed both. Vibey needs its own bot.", flush=True)
        return
    print(f"[tg] up as @{BOT_HANDLE}", flush=True)
    _set_command_menu(_state().get("owner"))
    threading.Thread(target=_sleep_watcher, daemon=True).start()
    threading.Thread(target=_power_watcher, daemon=True).start()

    if MODE == "poll":
        print("[tg] TELEGRAM_MODE=poll: long-polling getUpdates (the webhook "
              "must be deleted first, or this gets 409)", flush=True)
        _run_poll()
    else:
        _run_inbox()


def _spawn(target, args, offline: bool = False) -> None:
    def go():
        if offline:
            _tl.prefix_chat = args[0]
        target(*args)
    threading.Thread(target=go, daemon=True).start()


def _inbound_event(msg: dict, chat_id, st: dict, offline: bool) -> None:
    """What just arrived, for the stream. Guests and groups get a short
    snippet only; the full text stays in their chat."""
    try:
        text = msg.get("text") or ""
        frm = msg.get("from") or msg.get("chat") or {}
        name = frm.get("first_name") or frm.get("username") or "someone"
        ctype = msg["chat"].get("type")
        if ctype in ("group", "supergroup"):
            if not _addressed(msg):
                return       # overheard group chatter is not a ping
            title = msg["chat"].get("title") or "a group"
            _ev(f"pinged in {title} by {name}: {reachy_events.short(text, 50)}",
                detail={"chat": "group", "group": title, "from": name}, icon="💬")
        elif chat_id == st.get("owner"):
            label = "command" if text.startswith("/") else "DM"
            _ev(f"{label} from {name}: {reachy_events.short(text, 90)}",
                detail={"chat": "dm", "from": name, "owner": True},
                icon="⌘" if text.startswith("/") else "💬")
        else:
            _ev(f"DM from {name} (guest): {reachy_events.short(text, 50)}",
                detail={"chat": "dm", "from": name, "owner": False}, icon="💬")
        if offline:
            _ev(f"{name} got the offline auto-reply first", icon="😴")
    except Exception:  # noqa: BLE001
        pass


def _route(u: dict, offline: bool = False) -> None:
    """One Telegram update into the owner / guest / group handlers. Shared by
    both transports so the routing is identical whichever one is running."""
    msg = u.get("message") or u.get("edited_message")
    if not msg or "text" not in msg:
        return
    chat_id = msg["chat"]["id"]
    st = _state()
    if not st.get("owner"):
        st["owner"] = chat_id
        st["owner_name"] = (msg["chat"].get("first_name") or
                            msg["chat"].get("username") or "?")
        _save_state(st)
        print(f"[tg] paired with {st['owner_name']} ({chat_id})", flush=True)
        _send(chat_id, "👋 paired! You're my human now.")
    _inbound_event(msg, chat_id, st, offline)
    if msg["chat"].get("type") in ("group", "supergroup"):
        _spawn(_handle_group, (chat_id, msg), offline)
        return
    if chat_id != st.get("owner"):
        # Not the owner: a walled-off guest chat, plus consent.
        _spawn(_handle_guest, (chat_id, msg), offline)
        return
    print(f"[tg] <- {msg['text'][:80]!r}", flush=True)
    _spawn(_handle, (chat_id, msg["text"]), offline)


def _run_poll() -> None:
    """The old transport: Telegram long-poll. Fallback only (TELEGRAM_MODE=poll)."""
    offset = 0
    while True:
        try:
            upd = _tg("getUpdates", {"offset": offset, "timeout": 50})
        except Exception as e:  # noqa: BLE001
            print(f"[tg] poll error: {e}", flush=True)
            time.sleep(5)
            continue
        for u in upd.get("result", []):
            offset = u["update_id"] + 1
            _route(u)


# ── Inbox transport (default) ─────────────────────────────────────────────
# Telegram posts to the Supabase edge function vibey-telegram-webhook, which
# stores every update in vibey_telegram_inbox and, if this process hasn't
# beaten vibey_heartbeat for 2 minutes, tells the sender Vibey is asleep. Here
# we drain the inbox in update_id order and keep the heartbeat fresh. Rows the
# webhook already answered get "ok i'm up!" on the real reply.
MODE = os.environ.get("TELEGRAM_MODE", "inbox").strip().lower()
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
INBOX_EVERY_S = 1.5
HEARTBEAT_EVERY_S = 30


def _sb(path: str, method: str = "GET", body=None, prefer: str = ""):
    headers = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
               "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(
        f"{SB_URL}/rest/v1/{path}", method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def _beat() -> None:
    _sb("vibey_heartbeat?on_conflict=id", "POST",
        {"id": "bot", "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        prefer="resolution=merge-duplicates,return=minimal")


def _heartbeat_loop() -> None:
    while True:
        try:
            _beat()
        except Exception as e:  # noqa: BLE001
            print(f"[tg] heartbeat failed: {e}", flush=True)
        time.sleep(HEARTBEAT_EVERY_S)


def _run_inbox() -> None:
    if not (SB_URL and SB_KEY):
        print("[tg] inbox mode needs SUPABASE_URL and SUPABASE_SERVICE_KEY in "
              ".env (or set TELEGRAM_MODE=poll after deleteWebhook)", flush=True)
        return
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    print("[tg] draining the Supabase inbox", flush=True)
    done: set = set()   # dispatched but not yet marked, so a failed PATCH can't double-answer
    while True:
        try:
            rows = _sb("vibey_telegram_inbox?processed_at=is.null&order=update_id.asc"
                       "&limit=50&select=update_id,payload,offline_replied_at") or []
            fresh = [r for r in rows if r["update_id"] not in done]
            for r in fresh:
                try:
                    _route(r["payload"] or {}, offline=bool(r.get("offline_replied_at")))
                except Exception as e:  # noqa: BLE001 — one bad update never blocks the queue
                    print(f"[tg] route failed for {r['update_id']}: {e}", flush=True)
                done.add(r["update_id"])
            pending = [r["update_id"] for r in rows if r["update_id"] in done]
            if pending:
                _sb("vibey_telegram_inbox?update_id=in.("
                    + ",".join(str(i) for i in pending) + ")", "PATCH",
                    {"processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                    prefer="return=minimal")
                done.difference_update(pending)
        except Exception as e:  # noqa: BLE001
            print(f"[tg] inbox error: {e}", flush=True)
            time.sleep(5)
            continue
        time.sleep(INBOX_EVERY_S)


if __name__ == "__main__":
    run()
