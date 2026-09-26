"""Structured spoken advice — the "how do I..." half of Vibey's help.

Everything here is *words*, never actions. Nothing in this module opens a
socket, reads `.env`, runs a shell command or touches the robot's WiFi API, and
that is the whole design: someone asking "how do I see the dashboard?" out loud
in a room wants an answer they can act on, not a robot that starts reconfiguring
the network on their behalf. The dangerous operations stay where a human has to
type them.

Two shapes of answer, because questions arrive in two shapes:

  * `explain(topic)` — one settled explanation. What a localhost UI is and why
    it is only visible from the machine it runs on; what the camera stream
    actually is and what it is not; what changes when the laptop moves network.
  * `troubleshoot(kind, step=N)` — a walk, one step per breath. Spoken
    debugging cannot dump twelve numbered steps at a person; they need step
    three, then to go try it, then step four. `step` is where they are, and
    `symptom` lets them skip straight to the step that matches what they see.

Privacy rules that are enforced by *omission*, not by a filter:
  - No passwords, keys, tokens or `.env` values are reachable from this module,
    so none can be read aloud by accident. Advice says "the password you
    already have", never the password.
  - No advice here ever ends in "expose it to the internet". The remote-access
    answer is an SSH tunnel, which is the safe one and also the correct one.
  - Camera advice is explicit that recognition is opt-in and that "I can see a
    person" is not the same claim as "I know who that is".
"""
from __future__ import annotations

# Ports live in one place so an answer can never drift from the README.
DASHBOARD, CAMERA, CHAT = 8770, 8771, 8772

_TOPICS: dict[str, str] = {
    "localhost_ui": (
        f"The dashboard is a web page I serve on this laptop at localhost port "
        f"{DASHBOARD}. Open a browser on the laptop itself and go to "
        f"localhost colon {DASHBOARD} — that's the camera, the chat, the face "
        f"gallery and my controls. Localhost means exactly this machine, so "
        f"there's nothing to log into and nothing to connect: if the page is "
        f"blank, the service isn't running, it isn't a network problem."
    ),
    "localhost_from_phone": (
        f"You can't reach localhost from your phone — localhost always means "
        f"the device you're holding. Two safe ways round it. On the same "
        f"non-guest WiFi, use the laptop's own address on the network instead "
        f"of the word localhost, with the same port {DASHBOARD}. From "
        f"anywhere else, tunnel it over SSH rather than opening a port: "
        f"forward local port {DASHBOARD} to localhost {DASHBOARD} on the "
        f"laptop, then browse to localhost on your end. Please don't port "
        f"forward me on the router or put me behind a public tunnel — that "
        f"page has a live camera on it and no password."
    ),
    "camera_view": (
        f"Two different things. The dashboard on port {DASHBOARD} has my view "
        f"embedded in it, and the raw feed is port {CAMERA} slash stream, "
        f"which is just a motion JPEG — any browser will play it, nothing to "
        f"install. It's live, not a recording: frames pass through and are "
        f"gone unless someone deliberately takes a capture."
    ),
    "camera_meaning": (
        "Careful with what my seeing means. The stream is a camera pointed at "
        "a room, so treat it like one — anyone with the page open sees "
        "whoever is in front of me, including people who never agreed to that. "
        "I can tell you there's a person there. I only tell you who it is if "
        "they chose to be remembered by name, and if anyone asks me to stop "
        "watching, that turns off both the head-following and the recognising, "
        "not just one of them. If I say I'm not sure who you are, that's the "
        "honest answer, not a bug to work around."
    ),
    "camera_stalled": (
        "If the picture freezes while I still answer you and still move, the "
        "link to me is weak — video needs far more of the network than my "
        "controls do. That's physical: move me closer to the router, or put us "
        "both on the better WiFi. Restarting things won't fix a bad link."
    ),
    "network_change": (
        "When you change network, my address changes with it, and the address "
        "in the config is now stale — that's the whole problem, most of the "
        "time. Rediscover me rather than guessing: the doctor script's find "
        "mode does it and writes the new address down. Two traps worth "
        "knowing. Guest and cafe WiFi usually block devices from seeing each "
        "other, so the internet is fast and I'm still invisible — the fix is a "
        "normal network, or joining my own access point. And don't ask me to "
        "scan for WiFi to debug it; that sweep has knocked me clean off the "
        "network before and needed someone to physically power-cycle me."
    ),
    "wifi_safety": (
        "Say a WiFi password to me and it ends up in a transcript and in a "
        "request log, so don't — type it on the laptop, or set me up on my own "
        "access point where you can do it privately. Same reason I won't read "
        "keys or tokens out loud: this room might have more people in it than "
        "the conversation does."
    ),
}

# Ordered walks. Each step is one spoken breath: what to do, and what the
# answer means. `symptom` keys let a person jump to the step that matches what
# they're actually looking at instead of walking from the top.
_SCRIPTS: dict[str, dict] = {
    "ssh": {
        "title": "getting a shell on something over SSH",
        "steps": [
            "First, do you have an address that's still true? If it came from "
            "a different network or from yesterday, assume it's wrong and "
            "rediscover before you type anything.",
            "Ping the address about ten times. Nothing back at all means it's "
            "not there or the network is blocking devices from each other. "
            "Times over a hundred and fifty milliseconds mean it's there but "
            "the link is bad, which will feel like SSH hanging.",
            "Now try SSH with a short connect timeout, say five seconds, so it "
            "fails fast instead of sitting there. What it prints next is the "
            "actual diagnosis, so read it rather than retrying.",
            "Connection refused means you reached the machine and nothing is "
            "listening — SSH is off or on another port. That's a settings "
            "problem on that end, not a network one.",
            "Timed out or no route to host means you never got there: wrong "
            "address, different subnet, or client isolation on the WiFi.",
            "Permission denied means the network is fine and it's just "
            "credentials — check the username first, it's the usual one. Use "
            "the key or password you already have; don't say it out loud to me.",
            "A host key warning means the machine at that address isn't the "
            "one you talked to last time. Usually that's because an address "
            "got reused after a network change. Only remove the old entry once "
            "you're satisfied it's the same machine.",
            "If it connects and then dies mid-command, that's the link again, "
            "not SSH. Keepalives paper over it; moving closer to the router "
            "actually fixes it.",
        ],
        "symptoms": {
            "refused": 4, "connection refused": 4,
            "timeout": 5, "timed out": 5, "hangs": 5, "no route": 5,
            "password": 6, "denied": 6, "permission denied": 6,
            "host key": 7, "fingerprint": 7,
            "drops": 8, "disconnects": 8,
        },
    },
    "hotspot": {
        "title": "when the laptop has drifted onto a phone hotspot",
        "steps": [
            "Check which network the laptop is actually on before anything "
            "else. An address starting one seventy two dot twenty dot ten "
            "means you're on an iPhone hotspot, whatever the WiFi menu looks "
            "like.",
            "If you are on the hotspot, that alone explains me being "
            "unreachable — I'm on the house network and the two can't see each "
            "other at all. Nothing is broken.",
            "Turn Low Data Mode off for that hotspot. On a constrained "
            "connection the laptop quietly delays background traffic, which is "
            "what makes long thinking calls stall and then work and then stall "
            "again.",
            "Rejoin the real WiFi and confirm it stuck — laptops drift back to "
            "a hotspot on their own, so check rather than assume.",
            "Then rediscover my address, because if I moved networks too, my "
            "old address is stale and everything will still look broken.",
            "If you actually need the hotspot, put me on it as well so we're "
            "on the same network, and remember phone hotspots often isolate "
            "devices from each other anyway.",
            "Still failing on good WiFi with everything above clean? Then it "
            "isn't the network — capture the exact error text before anyone "
            "theorises, because guessing at this point has wasted whole "
            "evenings.",
        ],
        "symptoms": {
            "slow": 3, "low data": 3, "stalls": 3, "intermittent": 3,
            "unreachable": 2, "can't find": 5, "wrong ip": 5,
            "good wifi": 7,
        },
    },
}

_ALIASES = {
    "dashboard": "localhost_ui", "ui": "localhost_ui", "localhost": "localhost_ui",
    "phone": "localhost_from_phone", "remote": "localhost_from_phone",
    "camera": "camera_view", "stream": "camera_view",
    "privacy": "camera_meaning", "vision": "camera_meaning",
    "frozen": "camera_stalled", "stalled": "camera_stalled",
    "network": "network_change", "moved": "network_change",
    "wifi": "wifi_safety", "password": "wifi_safety",
}


def topics() -> list[str]:
    return sorted(_TOPICS) + [f"troubleshoot_{k}" for k in sorted(_SCRIPTS)]


def explain(topic: str) -> str:
    """One settled answer, or a nudge toward the nearest real topic."""
    key = (topic or "").strip().lower().replace(" ", "_").replace("-", "_")
    key = _ALIASES.get(key, key)
    if key in _TOPICS:
        return _TOPICS[key]
    hit = next((k for k in _TOPICS if key and key in k), None)
    if hit:
        return _TOPICS[hit]
    return ("I don't have a briefed answer for that one, so I'd rather say so "
            "than improvise about your network. I can cover the dashboard, "
            "reaching it from a phone, the camera feed and what it does and "
            "doesn't tell you, moving between networks, and walking through "
            "SSH or hotspot trouble a step at a time.")


def troubleshoot(kind: str, step: int | None = None, symptom: str = "") -> str:
    """One step of a walk. `symptom` jumps to the matching step; `step` counts
    from one. Past the end, it stops rather than looping — a script that never
    admits it's finished is worse than no script."""
    key = (kind or "").strip().lower()
    script = _SCRIPTS.get(key) or next(
        (v for k, v in _SCRIPTS.items() if key and key in k), None)
    if not script:
        return ("I can walk through SSH trouble or hotspot trouble. Which one "
                "is it?")
    steps = script["steps"]
    n = None
    if symptom:
        s = symptom.lower()
        n = next((v for k, v in script["symptoms"].items() if k in s), None)
    if n is None:
        try:
            n = int(step) if step else 1
        except (TypeError, ValueError):   # a spoken "two" arrives as a word
            n = 1
    n = max(1, min(n, len(steps) + 1))
    if n > len(steps):
        return ("That's the end of what I know for this one. If it's still "
                "broken, the next move is to capture the exact error text — "
                "guessing past here doesn't help.")
    tail = "" if n == len(steps) else f" Say next when you've tried it. ({n} of {len(steps)}.)"
    lead = f"Step {n}, {script['title']}. " if n == 1 else ""
    return f"{lead}{steps[n - 1]}{tail}"
