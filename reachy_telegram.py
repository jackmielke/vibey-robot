#!/usr/bin/env python3
"""
reachy_telegram.py — text Vibey from anywhere (@vibey_ai_bot).

A stdlib-only Telegram bridge:
- Messages go through whichever brain is active (Vibey/fast/🎮 Vibe agent) via the
  chat service's /ask — the reply comes back in Telegram AND is spoken aloud
  on the robot, so texting it makes the robot talk in the room.
- `say: something` speaks the text verbatim on the robot.
- /photo sends a live frame from Vibey's camera.
- /status sends a one-line health check of the whole stack.
- VibeVerse happenings (joins, mentions, greetings) are pushed to you as they
  happen, from the avatar's status feed.
- Vibey can also text OUT, to people who opted in: the person messages the bot
  and replies YES, the owner runs `/allow <id> <nickname>`, and from then on the
  voice brain's `send_text_message` tool can reach them by nickname. See
  `send_to_contact` below. Anyone can reply STOP to be forgotten.

Pairing: the FIRST person to message the bot becomes the owner (saved to
.telegram_state.json); everyone else gets a polite brush-off. Delete that
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


def _send(chat_id: int, text: str) -> None:
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


def _handle_guest(chat_id: int, msg: dict) -> None:
    """Anyone who isn't the owner. They can only ever opt in or opt out here —
    guests never reach the brain, and their words are never forwarded."""
    low = (msg.get("text") or "").strip().lower().strip("/ !.")
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
    if mine:
        _send(chat_id, "I'm Vibey, Jack's desk robot. I only send messages "
                       "here, I don't chat. Reply STOP any time to opt out.")
        return
    name = (msg["chat"].get("first_name") or msg["chat"].get("username")
            or "someone")
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
    d["pending"][str(chat_id)] = pend
    _save_contacts(d)
    if pend.get("consented"):
        _send(chat_id, "You've already said yes — I'm waiting on Jack to "
                       "approve it. Reply STOP to withdraw.")
        return
    _send(chat_id,
          "👋 I'm Vibey, a desk robot belonging to Jack. I'm a bot, not a "
          "person, and I don't chat here.\n\nIf you're happy for Jack's robot "
          "to send you the occasional short message, reply YES. Ignore this and "
          "nothing happens. You can reply STOP at any time.")


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


def _asleep() -> bool:
    st = _get_json(f"{CHAT_URL}/state") or {}
    return bool(st.get("asleep"))


def _power(chat_id: int, wake: bool) -> None:
    """Wake or sleep the whole robot.

    Posts to the CHAT service, not the viewer's /power. /power drives the motors
    and nothing else, so /wake used to leave the robot sitting up with its eyes
    open and no conversation running — awake in the only sense that does not
    matter. The chat endpoint is the one that enables motors, plays the chime and
    opens the realtime session.
    """
    try:
        _post_json(f"{CHAT_URL}/wake" if wake else f"{CHAT_URL}/sleep",
                   {}, timeout=30)
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
              "🤖 Vibey here — the actual robot in Jack's house.\n\n"
              "Just text me and I'll answer (and say it out loud in the room).\n"
              "say: <text> — I'll speak it verbatim\n"
              "/photo — see through my eyes right now\n"
              "/clip — an 8-second video through my eyes\n"
              "/timelapse — today so far, one frame a minute\n"
              "/status — stack health\n"
              "/alarm 07:30 [daily] — wake-up show (/alarm off clears)\n"
              "/sleep, /wake — or just say \"you awake?\" and I'll get up\n"
              "/code <task> — set Claude Code on this repo, I'll report back\n"
              "/jobs — what Claude Code is doing\n"
              "/voicenotes on|off — replies as voice messages too\n"
              "/contacts — who I'm allowed to text (and how to add someone)\n"
              "/verse — what's happening in my VibeVerse lobby")
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
        try:
            with urllib.request.urlopen(f"{CAM_URL}/frame.jpg", timeout=8) as r:
                jpeg = r.read()
            _send_photo(chat_id, jpeg, "what I'm seeing right now 👁️")
        except Exception as e:  # noqa: BLE001
            _send(chat_id, f"camera's not answering ({e})")
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

    # A text that arrives DURING a conversation is a text, not a new
    # conversation.
    #
    # /ask spins up a separate turn: it answers from a different context, and
    # the room hears an answer to a question nobody in it asked. When a realtime
    # session is live, the message is handed to that session instead, framed as
    # what it is — something that arrived on the phone — so Vibey brings it up
    # in the conversation already happening, in the voice already talking.
    st = _get_json(f"{CHAT_URL}/state") or {}
    if st.get("openai") and not st.get("asleep"):
        try:
            out = _post_json(f"{CHAT_URL}/sighting", {
                "text": f"[Jack just texted you: \"{text[:400]}\". Nobody in "
                        f"the room said this out loud. Answer him in the "
                        f"conversation.]"}, timeout=20)
            if (out or {}).get("delivered") == "realtime":
                _send(chat_id, "🗣️ told him out loud — listen in.")
                return
        except Exception:  # noqa: BLE001 — fall through to the text brain
            pass

    # normal chat → active brain; reply is also spoken in the room
    try:
        out = _post_json(f"{CHAT_URL}/ask", {"text": text})
        reply = (out or {}).get("reply") or "(no reply)"
        _send(chat_id, reply)
        if _state().get("voice_notes") and reply and not reply.startswith("("):
            _send_voice_note(chat_id, reply)
    except Exception as e:  # noqa: BLE001
        _send(chat_id, f"brain hiccup ({e}) — is the chat service up?")


def _verse_watcher() -> None:
    """Forward new notable VibeVerse events to the owner as they happen."""
    seen_ts = 0
    while True:
        time.sleep(20)
        owner = _state().get("owner")
        if not owner:
            continue
        v = _get_json(f"{VERSE_URL}/status")
        if not v:
            continue
        fresh = [e for e in (v.get("events") or [])
                 if e["ts"] > seen_ts and e["kind"] in
                 ("join", "mention", "report", "say")]
        if not fresh:
            continue
        seen_ts = max(e["ts"] for e in fresh)
        if len(fresh) > 5:
            fresh = fresh[-5:]
        body = "🌐 VibeVerse:\n" + "\n".join(
            f"· {e['kind']}: {e['text'][:100]}" for e in fresh)
        _send(owner, body)


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
    threading.Thread(target=_verse_watcher, daemon=True).start()

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
            msg = u.get("message") or u.get("edited_message")
            if not msg or "text" not in msg:
                continue
            chat_id = msg["chat"]["id"]
            st = _state()
            if not st.get("owner"):
                st["owner"] = chat_id
                st["owner_name"] = (msg["chat"].get("first_name") or
                                    msg["chat"].get("username") or "?")
                _save_state(st)
                print(f"[tg] paired with {st['owner_name']} ({chat_id})", flush=True)
                _send(chat_id, "👋 paired! You're my human now.")
            if chat_id != st.get("owner"):
                # Not the owner: the only conversation on offer is consent.
                threading.Thread(target=_handle_guest, args=(chat_id, msg),
                                 daemon=True).start()
                continue
            print(f"[tg] <- {msg['text'][:80]!r}", flush=True)
            threading.Thread(target=_handle, args=(chat_id, msg["text"]),
                             daemon=True).start()


if __name__ == "__main__":
    run()
