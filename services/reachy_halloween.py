#!/usr/bin/env python3
"""Vibey's Haunted Candy Hunt: the phone AR game's link to the real robot.

Serves services/halloween.html and turns game events into Vibey's voice,
face, sounds and wheels, by calling the chat service (:8772) the same way the
phone's Play tab does. The page decides what happened; this file decides how
Vibey reacts, so the jokes live in one place and a mashed screen can't spam
the robot (every kind of reaction has its own cooldown).

    python3 services/reachy_halloween.py          # http://<mac>:8781
    ./vibey party                                 # same, plus an https link + QR for phones

Only game reactions are reachable from here: no dashboard, no settings, no
free driving. Wheel moves are short slow wiggles, and only when ROVER_URL is
set and HALLOWEEN_WHEELS isn't 0.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env() -> None:
    path = os.path.join(HERE, "..", ".env")
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env()
PORT = int(os.environ.get("HALLOWEEN_PORT", "8781"))
CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8772").rstrip("/")
WHEELS = bool(os.environ.get("ROVER_URL")) and os.environ.get("HALLOWEEN_WHEELS", "1") != "0"
DRY = os.environ.get("HALLOWEEN_DRY") == "1"          # log reactions, touch nothing (testing)

# What Vibey says. Short, because speech takes a couple of seconds to start
# and the game moves fast. {score} is filled in where it appears.
LINES = {
    "start": ["Trick or treat! Grab the candy before the ghosts do.",
              "The house is haunted. Get the candy. Zap the ghosts. Go!",
              "Ghosts everywhere! Quick, fill the bag."],
    "combo": ["Sugar rush!", "Combo! Look at you go.", "Candy machine!", "You're a candy vacuum."],
    "steal": ["Nooo, the ghost took the good stuff!", "Hey! That was my favourite one.",
              "Ghost tax. Ouch.", "Zap them before they get close!"],
    "zap": ["Got one!", "Ghost busted.", "Boo yourself!"],
    "boss_start": ["Hah hah hah. I am the Vibeygeist. Your candy is mine now.",
                   "Something is inside me. It wants. Your. Candy.",
                   "Vibey isn't here right now. Only the Vibeygeist."],
    "boss_hit": ["Ow! That tickles.", "Missed me! Oh wait, no you didn't.", "Is that all you've got?",
                 "My circuits! My beautiful circuits!", "You can't zap a ghost robot. Can you?"],
    "boss_hit_low": ["No, no, I'm fading!", "One more and I'm done for!"],
    "freed": ["Whoa. I'm free! What happened? Why is there candy everywhere? Let's split it."],
    "end_win": ["{score} pieces of candy. Legendary haul. Happy Halloween!"],
    "end_lose": ["The Vibeygeist keeps the candy. Muahaha. Rematch?"],
}

# kind -> (seconds between, fn). Voice shares one cooldown so lines never stack.
_last: dict[str, float] = {}
_say_until = [0.0]


def _post(path: str, body: dict, timeout: float = 6.0):
    if DRY:
        print(f"[halloween] {path} {body}", flush=True)
        return {"ok": True}
    try:
        req = urllib.request.Request(f"{CHAT_URL}{path}", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:  # noqa: BLE001 - the robot being away is normal at a party
        print(f"[halloween] {path} failed: {e}", flush=True)
        return None


def _bg(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()


def _ready(kind: str, every: float) -> bool:
    now = time.time()
    if now - _last.get(kind, 0) < every:
        return False
    _last[kind] = now
    return True


def say(key: str, force: bool = False, **fmt) -> None:
    now = time.time()
    if not force and now < _say_until[0]:
        return
    text = random.choice(LINES[key]).format(**fmt)
    _say_until[0] = now + max(3.0, len(text.split()) / 2.4 + 1.5)
    _bg(_post, "/say", {"text": text})


def emote(name: str, every: float = 1.2) -> None:
    if _ready("emote", every):
        _bg(_post, "/emote", {"name": name, "sound": False})


def sound(name: str, every: float = 0.5) -> None:
    if _ready("sfx:" + name, every):
        _bg(_post, "/sfx", {"name": name})


def wheels(ok: bool, *moves: tuple[str, float]) -> None:
    """A little sequence of slow moves, e.g. a dodge. Skipped if a move is
    still going or the wheels are off."""
    if not (ok and WHEELS) or not _ready("wheels", 1.6):
        return

    def run():
        for action, secs in moves:
            _post("/drive", {"action": action, "seconds": secs, "speed": "slow"}, timeout=secs + 4)
    _bg(run)


def react(ev: dict) -> dict:
    kind = str(ev.get("type", ""))
    w = bool(ev.get("wheels", True))
    score = int(ev.get("score", 0) or 0)
    if kind == "start":
        say("start", force=True)
        emote("excited", 0)
    elif kind == "calibrated":
        emote("curious", 0)
    elif kind == "candy":
        combo = int(ev.get("combo", 1) or 1)
        if combo >= 4 and _ready("combo", 6):
            say("combo"); emote("happy")
        elif _ready("candy_ding", 2.5):
            emote(random.choice(["tilt_left", "tilt_right"]), 2.5)
    elif kind == "ghost_zap":
        sound("laser_pew", 0.6)
        if _ready("zap_line", 9):
            say("zap")
    elif kind == "ghost_steal":
        sound("astro_worried", 2)
        emote("sad", 2)
        if _ready("steal_line", 7):
            say("steal")
    elif kind == "boss_start":
        sound("dark_breath", 0)
        emote("smug", 0)
        say("boss_start", force=True)
        wheels(w, ("spin_left", 0.5), ("spin_right", 0.5))
    elif kind == "boss_dodge":
        side = "left" if ev.get("dir") == "left" else "right"
        emote(f"tilt_{side}_big", 0.8)
        wheels(w, (f"spin_{side}", 0.4), (f"spin_{'right' if side == 'left' else 'left'}", 0.4))
    elif kind == "boss_hit":
        sound("astro_zapped", 0.7)
        hp = int(ev.get("hp", 9) or 0)
        if hp <= 3 and _ready("low", 5):
            say("boss_hit_low")
        elif _ready("hit_line", 6):
            say("boss_hit")
        emote(random.choice(["surprised", "appalled", "no_no_no"]), 1.5)
    elif kind == "boss_defeated":
        sound("success", 0)
        say("freed", force=True)
        emote("victory", 0)
        wheels(w, ("spin_left", 1.2))
    elif kind == "end":
        if ev.get("won"):
            _bg(lambda: (time.sleep(4.5), say("end_win", force=True, score=score)))
        else:
            sound("dark_sting", 0)
            say("end_lose", force=True)
            emote("smug", 0)
    else:
        return {"ok": False, "error": f"unknown event {kind!r}"}
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(b"", "text/plain", 204)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/game", "/halloween"):
            with open(os.path.join(HERE, "halloween.html"), "rb") as f:
                self._send(f.read(), "text/html; charset=utf-8")
        elif p == "/state":
            self._send(json.dumps({"ok": True, "wheels": WHEELS, "dry": DRY}).encode(), "application/json")
        else:
            self._send(b"not here", "text/plain", 404)

    def do_POST(self):
        if self.path.split("?")[0] != "/event":
            self._send(b"not here", "text/plain", 404)
            return
        try:
            n = min(int(self.headers.get("Content-Length", 0) or 0), 4096)
            ev = json.loads(self.rfile.read(n) or b"{}")
            out = react(ev)
            print(f"[halloween] {ev.get('type')} -> {out}", flush=True)
            self._send(json.dumps(out).encode(), "application/json")
        except Exception as e:  # noqa: BLE001
            self._send(json.dumps({"ok": False, "error": str(e)}).encode(), "application/json", 400)


if __name__ == "__main__":
    print(f"[halloween] Haunted Candy Hunt on http://0.0.0.0:{PORT} (wheels {'on' if WHEELS else 'off'})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
