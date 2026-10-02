#!/usr/bin/env python3
"""
reachy_scribe.py — Vibey as a quiet note-taker.

Scribe mode runs alongside whatever the robot is doing. Started while asleep,
the robot stays asleep with no realtime socket open, so nothing is paid per
minute and nothing talks back; started while awake, the conversation goes on
and ends up in the notes too. Either way it listens through its own mic, transcribes locally with faster-whisper,
and at the end turns the whole thing into Granola-style notes that get texted
to Jack on Telegram.

    start()            begin (idempotent)
    stop(reason)       end, summarise, text the notes, return them
    status()           {"on", "started", "minutes", "lines"}

Ways it ends: /scribe off on Telegram, the dashboard, saying "stop taking
notes". "Hey Vibey" wakes it for a conversation without ending the notes.

Lines go to the day's transcript (reachy_transcript) as well, so nothing said
while scribing is lost if the summary step fails. Notes are saved under
notes/ (gitignored) before they are sent.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request

ROBOT_MIC_URL = os.environ.get("ROBOT_MIC_URL", "http://localhost:8775").rstrip("/")
MIC_SR = 16000
MODEL_NAME = os.environ.get("VIBEY_SCRIBE_MODEL", "small.en")
NOTES_MODEL = os.environ.get("VIBEY_NOTES_MODEL", "gpt-5.5")
NOTES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes")

# Segmenting by voice activity rather than fixed windows: whole utterances
# transcribe far better than 2-second slices cut mid-word.
SPEECH_RMS = 0.010          # same floor the wake listener uses on this mic
END_SILENCE_S = 0.9         # this much quiet closes an utterance
MAX_UTTER_S = 25.0          # and nobody gets a segment longer than this
MIN_UTTER_S = 0.6

_STOP_RE = re.compile(
    r"\b(stop (taking )?notes|stop (listening|scribing|recording)|"
    r"that'?s (a )?wrap|end (the )?notes)\b", re.I)
_HALLUCINATIONS = {"thank you", "thanks for watching", "you", "bye",
                   "thank you very much", "", "so", "okay"}

_S = {"on": False, "started": 0.0, "lines": [], "thread": None,
      "stop": threading.Event(), "on_wake": None, "on_notes": None,
      "log": print}
_lock = threading.Lock()
_model = None


def _whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
        _S["log"](f"[scribe] whisper ready ({MODEL_NAME})")
    return _model


def active() -> bool:
    return _S["on"]


def status() -> dict:
    return {"on": _S["on"], "started": _S["started"] or None,
            "minutes": round((time.time() - _S["started"]) / 60, 1) if _S["on"] else 0,
            "lines": len(_S["lines"])}


def start(on_notes=None, on_wake=None, log=print) -> dict:
    """on_notes(text): where the finished notes go. on_wake(): called if
    somebody says "hey Vibey" mid-scribe."""
    with _lock:
        if _S["on"]:
            return status()
        _S.update(on=True, started=time.time(), lines=[], on_notes=on_notes,
                  on_wake=on_wake, log=log)
        _S["stop"] = threading.Event()
        t = threading.Thread(target=_run, args=(_S["stop"],), name="scribe", daemon=True)
        _S["thread"] = t
        t.start()
    log("[scribe] on — listening only, taking notes")
    return status()


def stop(reason: str = "stopped") -> str:
    with _lock:
        if not _S["on"]:
            return ""
        _S["on"] = False
        _S["stop"].set()
        lines, started = list(_S["lines"]), _S["started"]
    _S["log"](f"[scribe] off ({reason}) — {len(lines)} lines, summarising")
    notes = _summarise(lines, started)
    path = _save(notes, lines, started)
    if path:
        _S["log"](f"[scribe] notes saved to {path}")
    cb = _S.get("on_notes")
    if cb:
        try:
            cb(notes)
        except Exception as e:  # noqa: BLE001
            _S["log"](f"[scribe] delivering notes failed: {e}")
    return notes


# --------------------------------------------------------------------------- #
def _run(stop_ev: threading.Event) -> None:
    import numpy as np
    chunk = MIC_SR * 2 // 10            # 0.1 s of int16
    while not stop_ev.is_set():
        try:
            with urllib.request.urlopen(f"{ROBOT_MIC_URL}/pcm", timeout=10) as r:
                _S["log"]("[scribe] listening on the robot's microphone")
                buf, quiet, voiced = bytearray(), 0.0, False
                while not stop_ev.is_set():
                    raw = r.read(chunk)
                    if not raw:
                        break
                    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    loud = a.size and float(np.sqrt(np.mean(a * a))) >= SPEECH_RMS
                    if loud:
                        voiced, quiet = True, 0.0
                    elif voiced:
                        quiet += 0.1
                    if voiced:
                        buf += raw
                    dur = len(buf) / 2 / MIC_SR
                    if voiced and (quiet >= END_SILENCE_S or dur >= MAX_UTTER_S):
                        if dur >= MIN_UTTER_S:
                            _handle(bytes(buf))
                        buf, quiet, voiced = bytearray(), 0.0, False
        except Exception as e:  # noqa: BLE001
            if not stop_ev.is_set():
                _S["log"](f"[scribe] mic stream lost ({e}) — retrying")
                time.sleep(2)


def _handle(pcm: bytes) -> None:
    import numpy as np
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    try:
        segs, _ = _whisper().transcribe(audio, language="en", beam_size=3,
                                        vad_filter=True)
        text = " ".join(s.text for s in segs).strip()
    except Exception as e:  # noqa: BLE001
        _S["log"](f"[scribe] transcribe failed: {e}")
        return
    if re.sub(r"[^a-z ]", "", text.lower()).strip() in _HALLUCINATIONS:
        return
    _S["lines"].append((time.time(), text))
    _S["log"](f"[scribe] {text}")
    try:
        import reachy_transcript
        reachy_transcript.log("human", text)
    except Exception:  # noqa: BLE001
        pass
    if _STOP_RE.search(text):
        threading.Thread(target=stop, args=("asked out loud",), daemon=True).start()
        return
    try:
        import reachy_wake
        if reachy_wake.wake_score(text) >= reachy_wake.WAKE_THRESHOLD:
            def _wake_after():
                # "Hey Vibey" wakes it for a conversation; the notes carry on
                # through it rather than ending.
                if _S.get("on_wake"):
                    _S["on_wake"]()
            threading.Thread(target=_wake_after, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def _transcript_text(lines, started) -> str:
    return "\n".join(f"[{time.strftime('%H:%M', time.localtime(t))}] {txt}"
                     for t, txt in lines)


def _summarise(lines, started) -> str:
    mins = max(1, round((time.time() - started) / 60))
    head = f"📝 notes · {time.strftime('%a %-d %b, %H:%M', time.localtime(started))} · {mins} min"
    if not lines:
        return f"{head}\n\ndidn't catch anything worth writing down."
    prompt = (
        "You are Vibey, a desk robot that just sat in on a conversation as a "
        "silent note-taker. Write Granola-style notes from the raw transcript "
        "below. The transcript comes from one room mic with speech-to-text, so "
        "expect mis-hearings and no speaker names; don't invent names or facts.\n\n"
        "Format, plain text for Telegram, no markdown headers or bold:\n"
        "one-line gist\n\n"
        "Key points\n• ...\n\n"
        "Decisions\n• ... (omit the section if none)\n\n"
        "Action items\n• who (if clear): what (omit if none)\n\n"
        "Open questions\n• ... (omit if none)\n\n"
        "Keep it tight. Casual, lowercase-friendly tone is fine.\n\n"
        f"Transcript:\n{_transcript_text(lines, started)[:60000]}")
    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps({"model": NOTES_MODEL,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
        try:
            import reachy_cost
            reachy_cost.record_tokens("other", resp.get("model") or NOTES_MODEL,
                                      resp.get("usage") or {}, {"what": "scribe notes"})
        except Exception:  # noqa: BLE001
            pass
        body = resp["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        _S["log"](f"[scribe] summary failed: {e}")
        body = ("couldn't write the summary (" + str(e)[:80] + "), here's the raw bit:\n\n"
                + _transcript_text(lines, started)[-3000:])
    return f"{head}\n\n{body}"


def _save(notes, lines, started) -> str | None:
    try:
        os.makedirs(NOTES_DIR, exist_ok=True)
        path = os.path.join(NOTES_DIR, time.strftime("%Y-%m-%d-%H%M", time.localtime(started)) + ".md")
        with open(path, "w") as f:
            f.write(notes + "\n\n---\n\nTranscript\n\n" + _transcript_text(lines, started) + "\n")
        return path
    except Exception as e:  # noqa: BLE001
        _S["log"](f"[scribe] saving notes failed: {e}")
        return None
