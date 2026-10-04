"""The "local" voice brain: everything on this Mac, nothing in the cloud.

    mic → energy VAD → faster-whisper → Ollama (qwen3) → Piper → speaker

Free, private and works offline. Slower than the Realtime models, so the reply
is spoken a sentence at a time: the first sentence plays while the model is
still writing the second. Audio goes out through the same _Streamer the
Realtime engine uses (robot speaker over WebRTC, or the laptop), so barge-in
and the mic gate behave the same.

Plugs into reachy_chat.py like the other brains: run(should_run, on_user_text,
on_agent_text, log) blocks until should_run() turns False.

Env: LOCAL_LLM (default qwen3:14b), LOCAL_WHISPER (small.en), LOCAL_PIPER
(models/piper/en_US-lessac-medium.onnx), LOCAL_VOICE (macOS fallback voice,
Samantha), OLLAMA_URL (http://localhost:11434).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
import wave

import numpy as np

import reachy_denoise
import reachy_openai_realtime as rt

LLM = os.environ.get("LOCAL_LLM", "qwen3:14b").strip()
WHISPER = os.environ.get("LOCAL_WHISPER", "small.en").strip()
VOICE = os.environ.get("LOCAL_VOICE", "Samantha").strip()
PIPER = os.environ.get("LOCAL_PIPER", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models/piper/en_US-lessac-medium.onnx"))
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
SR = 16000

INSTRUCTIONS = (
    "You are Vibey, a small friendly robot sitting in Jack's home, running "
    "entirely on Jack's Mac with no internet. You are talking out loud, so keep "
    "every reply short and natural: one to three sentences, plain words, no "
    "lists, no markdown, no emoji. Be warm, a little playful, and direct. If you "
    "didn't catch something, ask them to say it again."
)

_SENTENCE = re.compile(r"(.+?[.!?…])(\s+|$)", re.S)
_whisper = None
_piper = None


def _model():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(WHISPER, device="cpu", compute_type="int8")
    return _whisper


def _mic(stop: threading.Event):
    """0.1s blocks of 16kHz mono PCM16 from whichever mic is selected."""
    if rt.MIC_SOURCE == "laptop":
        import sounddevice as sd
        with sd.RawInputStream(samplerate=SR, channels=1, dtype="int16",
                               blocksize=SR // 10) as m:
            while not stop.is_set():
                raw, _ = m.read(SR // 10)
                yield reachy_denoise.process(bytes(raw), "live", SR)
        return
    with urllib.request.urlopen(f"{rt.ROBOT_MIC_URL}/pcm", timeout=10) as r:
        while not stop.is_set():
            chunk = r.read(SR * 2 // 10)
            if not chunk:
                return
            yield reachy_denoise.process(chunk, "live", SR)


def _voice_pcm(text: str) -> bytes:
    """Speech as 24kHz PCM16 — the Streamer's native rate. Piper renders a
    sentence in ~0.1s; macOS `say` costs ~1.8s of startup per call, so it is
    only the fallback for when the Piper voice isn't there."""
    global _piper
    try:
        if _piper is None:
            from piper import PiperVoice
            _piper = PiperVoice.load(PIPER)
        chunks = list(_piper.synthesize(text))
        a = np.frombuffer(b"".join(c.audio_int16_bytes for c in chunks), dtype="<i2")
        sr = chunks[0].sample_rate if chunks else 22050
        if sr != rt.RT_SR and len(a):
            n = int(len(a) * rt.RT_SR / sr)
            a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
        return a.astype("<i2").tobytes()
    except Exception:  # noqa: BLE001
        return _say_pcm(text)


def _say_pcm(text: str) -> bytes:
    """macOS speech as 24kHz PCM16 — the Streamer's native rate."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    try:
        subprocess.run(["say", "-v", VOICE, "-o", path, "--file-format=WAVE",
                        "--data-format=LEI16@24000", text],
                       check=True, timeout=30, capture_output=True)
        with wave.open(path, "rb") as w:
            return w.readframes(w.getnframes())
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _warm_llm() -> None:
    """Load the model into memory now, not on the first question (~19s cold)."""
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{OLLAMA}/api/generate", data=json.dumps(
                {"model": LLM, "prompt": "", "keep_alive": "30m"}).encode(),
            headers={"Content-Type": "application/json"}), timeout=90).read()
    except Exception:  # noqa: BLE001
        pass


def _clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"[*_#`]|[\U0001F300-\U0001FAFF☀-➿]", "", text)
    return " ".join(text.split())


class LocalSession:
    def __init__(self, on_user_text, on_agent_text, log):
        self.on_user = on_user_text or (lambda t: None)
        self.on_agent = on_agent_text or (lambda t: None)
        self.log = log or print
        self.history: list[dict] = []
        self.stream = rt._Streamer(self.log)
        self.speaking_until = 0.0
        self.cancel = threading.Event()

    # ---- thinking + speaking -------------------------------------------- #
    def _reply(self, heard: str) -> None:
        self.cancel.clear()
        self.history.append({"role": "user", "content": heard})
        msgs = ([{"role": "system", "content": INSTRUCTIONS + rt._now_line()}]
                + self.history[-16:])
        body = json.dumps({"model": LLM, "stream": True, "think": False,
                           "keep_alive": "30m", "messages": msgs,
                           "options": {"num_predict": 160, "temperature": 0.7}}).encode()
        t0, said, buf = time.time(), [], ""
        first = True
        try:
            req = urllib.request.Request(f"{OLLAMA}/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                for line in r:
                    if self.cancel.is_set():
                        break
                    d = json.loads(line)
                    buf += d.get("message", {}).get("content", "")
                    while (m := _SENTENCE.match(buf)):
                        self._speak(m.group(1), first, t0)
                        first = False
                        said.append(m.group(1))
                        buf = buf[m.end():]
                    if d.get("done"):
                        break
            if buf.strip() and not self.cancel.is_set():
                self._speak(buf, first, t0)
                said.append(buf)
        except Exception as e:  # noqa: BLE001
            self.log(f"[local] brain failed: {e}")
            self._speak("Sorry, my local brain hiccuped.", True, t0)
            return
        text = _clean(" ".join(said))
        if text:
            self.history.append({"role": "assistant", "content": text})
            self.on_agent(text)

    def _speak(self, sentence: str, first: bool, t0: float) -> None:
        sentence = _clean(sentence)
        if not sentence or self.cancel.is_set():
            return
        pcm = _voice_pcm(sentence)
        if first:
            self.log(f"[latency] first audio {1000 * (time.time() - t0):.0f}ms after you "
                     f"stopped talking (local)")
        dur = self.stream.push(pcm) if self.stream.ok else 0.0
        if not self.stream.ok:
            dur = rt._play_pcm_on_robot(pcm)
        self.speaking_until = max(self.speaking_until, time.time()) + dur
        reachy_denoise.set_speaking(self.speaking_until - time.time() + 0.3)

    def _barge_in(self) -> None:
        self.cancel.set()
        self.speaking_until = 0.0
        reachy_denoise.set_speaking(False)
        if self.stream.ok:
            self.stream.clear()

    # ---- listening ------------------------------------------------------- #
    def run(self, should_run, stop: threading.Event) -> None:
        self.log(f"[local] listening — {WHISPER} → {LLM} → piper, all on this Mac")
        # Warm everything before the first sentence needs it.
        threading.Thread(target=_model, daemon=True).start()
        threading.Thread(target=_voice_pcm, args=("hi",), daemon=True).start()
        threading.Thread(target=_warm_llm, daemon=True).start()
        floor, voiced, quiet, speech = 300.0, 0, 0, bytearray()
        worker: threading.Thread | None = None
        while should_run() and not stop.is_set():
            try:
                for block in _mic(stop):
                    if not should_run() or stop.is_set():
                        return
                    a = np.frombuffer(block, dtype="<i2").astype(np.float32)
                    rms = float(np.sqrt(np.mean(a * a))) if len(a) else 0.0
                    talking_back = time.time() < self.speaking_until
                    thr = max(floor * 3.0, 500.0) * (2.0 if talking_back else 1.0)
                    if rms > thr:
                        voiced += 1
                        quiet = 0
                    else:
                        quiet += 1
                        if not speech:
                            floor = floor * 0.95 + rms * 0.05
                            voiced = max(0, voiced - 1)
                    if voiced >= 3 or speech:
                        if not speech and talking_back:
                            self._barge_in()        # they talked over Vibey
                        speech.extend(block)
                    if speech and quiet >= 7:       # ~0.7s of quiet ends the turn
                        audio, speech, voiced = bytes(speech), bytearray(), 0
                        if len(audio) < SR * 2 * 0.4:
                            continue                # a cough, not a sentence
                        text = self._transcribe(audio)
                        if not text:
                            continue
                        self.on_user(text)
                        if worker and worker.is_alive():
                            self._barge_in()
                            worker.join(timeout=2)
                        worker = threading.Thread(target=self._reply, args=(text,), daemon=True)
                        worker.start()
            except Exception as e:  # noqa: BLE001
                if stop.is_set():
                    return
                self.log(f"[local] mic dropped ({e}); retrying in 2s")
                time.sleep(2)

    def _transcribe(self, pcm: bytes) -> str:
        a = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        segs, _ = _model().transcribe(a, language="en", beam_size=3, vad_filter=True,
                                      condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segs).strip()
        # Whisper's favourite hallucinations on silence.
        if text.lower().strip(" .!") in ("", "you", "thank you", "thanks for watching", "bye"):
            return ""
        return text


def run(should_run=None, on_user_text=None, on_agent_text=None, log=None,
        stop_event: "threading.Event | None" = None) -> None:
    stop = stop_event or threading.Event()
    session = LocalSession(on_user_text, on_agent_text, log)
    try:
        session.run(should_run or (lambda: True), stop)
    finally:
        stop.set()
        session.stream.close()
