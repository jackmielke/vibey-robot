"""The "local" voice brain: everything on this Mac, nothing in the cloud.

    mic → energy VAD → whisper → Ollama (qwen3) → Kokoro (or Piper) → speaker

Free, private and works offline. The pipeline is overlapped so it feels close
to the cloud brains:

  * the turn ends after ~0.55s of quiet, not a fixed long pause;
  * whisper runs greedy (beam 1) with no timestamps; on Apple Silicon it can
    run on the GPU through mlx-whisper instead (LOCAL_STT=mlx);
  * the reply is spoken a clause at a time: the first clause goes to the voice
    as soon as it has a few words, while the model is still writing;
  * the voice renders on its own thread, so sentence two is being synthesized
    while sentence one plays, and reading the model's stream never waits;
  * the system prompt is fixed for the whole session, so Ollama keeps its
    prompt cache warm and only the new words get processed each turn.

Audio goes out through the same _Streamer the Realtime engine uses (robot
speaker over WebRTC, or the laptop), so barge-in and the mic gate behave the
same. The model can open a reply with a body cue like [nod] or [laugh]; it is
stripped from the words and played as an emote.

Plugs into reachy_chat.py like the other brains: run(should_run, on_user_text,
on_agent_text, log) blocks until should_run() turns False.

Env (all optional):
  LOCAL_LLM        Ollama model                      qwen3:4b-instruct
  LOCAL_STT        faster | mlx                      auto (mlx if installed)
  LOCAL_WHISPER    faster-whisper model              small.en
  LOCAL_MLX_WHISPER  mlx-whisper repo                mlx-community/whisper-large-v3-turbo
  LOCAL_TTS        kokoro | piper | say              auto (first that loads)
  LOCAL_KOKORO_VOICE  Kokoro voice                   af_heart
  LOCAL_KOKORO_SPEED  1.0 is normal                  1.05
  LOCAL_PIPER      Piper .onnx                       models/piper/en_US-lessac-medium.onnx
  LOCAL_VOICE      macOS `say` voice (last resort)   Samantha
  LOCAL_END_MS     quiet that ends your turn         550
  OLLAMA_URL                                         http://localhost:11434

Setup: tools/setup_local_voice.sh installs all of it.
"""
from __future__ import annotations

import json
import os
import queue
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LLM = os.environ.get("LOCAL_LLM", "qwen3:4b-instruct").strip()
STT = os.environ.get("LOCAL_STT", "").strip().lower()
WHISPER = os.environ.get("LOCAL_WHISPER", "small.en").strip()
MLX_WHISPER = os.environ.get("LOCAL_MLX_WHISPER", "mlx-community/whisper-large-v3-turbo").strip()
TTS = os.environ.get("LOCAL_TTS", "").strip().lower()
KOKORO_DIR = os.environ.get("LOCAL_KOKORO_DIR", os.path.join(ROOT, "models/kokoro"))
KOKORO_VOICE = os.environ.get("LOCAL_KOKORO_VOICE", "af_heart").strip()
KOKORO_SPEED = float(os.environ.get("LOCAL_KOKORO_SPEED", "1.05"))
VOICE = os.environ.get("LOCAL_VOICE", "Samantha").strip()
PIPER = os.environ.get("LOCAL_PIPER", os.path.join(ROOT, "models/piper/en_US-lessac-medium.onnx"))
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
END_BLOCKS = max(3, int(os.environ.get("LOCAL_END_MS", "550")) // 100)
SR = 16000

# Body cues the model may open a reply with. Kept short: a 4B model follows a
# small menu, and every one of these is a quick emote that plays over speech.
CUES = ("nod", "laugh", "curious", "shrug", "smile", "surprised", "shy",
        "happy", "thinking", "wink", "excited", "confused")

INSTRUCTIONS = (
    "You are Vibey, a small expressive robot with a head and two antennas, "
    "sitting in Jack's home. Jack is a builder and roboticist who made you; he "
    "likes things playful and direct. You are running entirely on Jack's Mac "
    "with no internet, and you are talking out loud.\n\n"
    "How to talk: one or two short sentences, like a friend in the room. Plain "
    "spoken words only: no lists, no markdown, no emoji, no stage directions. "
    "Start with the answer, not with filler like 'Great question'. Have "
    "opinions. If you didn't catch something, ask them to say it again. If you "
    "don't know something current, say so instead of guessing.\n\n"
    "Your body: you may begin a reply with ONE cue in square brackets and the "
    f"robot will act it out. Cues: {', '.join(CUES)}. Use one when it fits, "
    "about every other reply, never more than one. Examples:\n"
    "[laugh] Okay, that's genuinely funny.\n"
    "[nod] Yep, the kettle's a good call.\n"
    "Good morning, Jack! What are we building today?"
)

# A cut point inside a streaming reply. Sentence ends need a space after them
# so "3.5" or "Dr." mid-stream is not cut; the end of the reply flushes the rest.
_END = re.compile(r"[.!?…]+[\"')\]]*\s")
_SOFT = re.compile(r"[,;:—–]\s")
_CUE = re.compile(r"\[\s*([a-z_ ]{2,20})\s*\]", re.I)
_whisper = None
_tts = {"engine": None, "obj": None}
_tts_lock = threading.Lock()
# The conversation outlives a session for a while, so swapping brains or a mic
# hiccup does not wipe what was just said.
_MEMORY = {"history": [], "at": 0.0}
_MEMORY_TTL = 15 * 60


# ---- ears ------------------------------------------------------------------ #
def _stt_engine() -> str:
    if STT in ("faster", "mlx"):
        return STT
    try:
        import mlx_whisper  # noqa: F401
        return "mlx"
    except Exception:  # noqa: BLE001
        return "faster"


def _model():
    global _whisper
    if _whisper is None:
        if _stt_engine() == "mlx":
            import mlx_whisper
            # Loading happens on first transcribe; do one on silence now.
            mlx_whisper.transcribe(np.zeros(SR // 2, dtype=np.float32),
                                   path_or_hf_repo=MLX_WHISPER, language="en")
            _whisper = ("mlx", mlx_whisper)
        else:
            from faster_whisper import WhisperModel
            _whisper = ("faster", WhisperModel(WHISPER, device="cpu", compute_type="int8"))
    return _whisper


def _stt_label() -> str:
    return (f"mlx-whisper {MLX_WHISPER.split('/')[-1]}" if _stt_engine() == "mlx"
            else f"faster-whisper {WHISPER}")


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


# ---- voice ----------------------------------------------------------------- #
def _load_tts() -> str:
    """Pick the best voice that loads: Kokoro (natural, 24kHz), Piper (fast,
    flatter), macOS `say` (always there, ~1.8s startup per call)."""
    with _tts_lock:
        if _tts["engine"]:
            return _tts["engine"]
        order = [TTS] if TTS in ("kokoro", "piper", "say") else ["kokoro", "piper", "say"]
        for name in order:
            try:
                if name == "kokoro":
                    from kokoro_onnx import Kokoro
                    _tts["obj"] = Kokoro(os.path.join(KOKORO_DIR, "kokoro-v1.0.onnx"),
                                         os.path.join(KOKORO_DIR, "voices-v1.0.bin"))
                elif name == "piper":
                    from piper import PiperVoice
                    _tts["obj"] = PiperVoice.load(PIPER)
                _tts["engine"] = name
                return name
            except Exception:  # noqa: BLE001
                continue
        _tts["engine"] = "say"
        return "say"


def _to_rt(a: np.ndarray, sr: int) -> bytes:
    """Any mono float or int16 array → 24kHz PCM16, the Streamer's native rate."""
    if a.dtype != np.int16:
        a = np.clip(a * 32767.0, -32767, 32767)
    a = a.astype(np.float32)
    if sr != rt.RT_SR and len(a):
        n = int(len(a) * rt.RT_SR / sr)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
    return a.astype("<i2").tobytes()


def _voice_pcm(text: str) -> bytes:
    engine = _load_tts()
    try:
        if engine == "kokoro":
            samples, sr = _tts["obj"].create(text, voice=KOKORO_VOICE,
                                             speed=KOKORO_SPEED, lang="en-us")
            return _to_rt(np.asarray(samples, dtype=np.float32), sr)
        if engine == "piper":
            chunks = list(_tts["obj"].synthesize(text))
            a = np.frombuffer(b"".join(c.audio_int16_bytes for c in chunks), dtype="<i2")
            return _to_rt(a, chunks[0].sample_rate if chunks else 22050)
    except Exception:  # noqa: BLE001
        pass
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


# ---- brain ----------------------------------------------------------------- #
def _post(path: str, body: dict, timeout: float):
    return urllib.request.urlopen(urllib.request.Request(
        f"{OLLAMA}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}), timeout=timeout)


def _warm_llm(system: str) -> None:
    """Load the model and process the system prompt now, not on the first
    question (~19s cold). Ollama keeps the prompt's cache, so the first real
    turn only pays for the new words."""
    try:
        _post("/api/chat", {"model": LLM, "stream": False, "think": False,
                            "keep_alive": "30m",
                            "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": "hi"}],
                            "options": {"num_predict": 1, "num_ctx": 4096}}, 120).read()
    except Exception:  # noqa: BLE001
        pass


def _clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = _CUE.sub("", text)
    text = re.sub(r"\([^)]*\b(laughs?|smiles?|nods?|sighs?|grins?)\b[^)]*\)", "", text, flags=re.I)
    text = re.sub(r"[*_#`]|[\U0001F300-\U0001FAFF☀-➿]", "", text)
    return " ".join(text.split())


def _cut(buf: str, first: bool) -> int:
    """Where to cut the next chunk to speak out of the streamed text, or 0.

    A sentence end always cuts. The first chunk also cuts at a comma once it
    has a handful of words, because time to first sound is what makes a voice
    feel quick; later chunks only do that when a sentence runs long, because
    Kokoro reads a whole sentence with better rhythm than its pieces."""
    m = _END.search(buf)
    if m:
        return m.end()
    need = 5 if first else 14
    for s in _SOFT.finditer(buf):
        if len(buf[:s.start()].split()) >= need:
            return s.end()
    return 0


class LocalSession:
    def __init__(self, on_user_text, on_agent_text, log):
        self.on_user = on_user_text or (lambda t: None)
        self.on_agent = on_agent_text or (lambda t: None)
        self.log = log or print
        if time.time() - _MEMORY["at"] > _MEMORY_TTL:
            _MEMORY["history"] = []
        self.history: list[dict] = _MEMORY["history"]
        # Fixed for the session so the prompt cache stays valid turn to turn.
        self.system = INSTRUCTIONS + rt._now_line()
        self.stream = rt._Streamer(self.log)
        self.speaking_until = 0.0
        self.gen = 0                   # bumped on barge-in: older audio is dropped
        self.voice_q: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._voice_worker, daemon=True).start()

    # ---- thinking -------------------------------------------------------- #
    def _reply(self, heard: str, t0: float) -> None:
        gen = self.gen
        self.history.append({"role": "user", "content": heard})
        msgs = [{"role": "system", "content": self.system}] + self.history[-16:]
        body = {"model": LLM, "stream": True, "think": False, "keep_alive": "30m",
                "messages": msgs,
                "options": {"num_predict": 140, "temperature": 0.7, "num_ctx": 4096}}
        said, buf, first = [], "", True
        try:
            with _post("/api/chat", body, 120) as r:
                for line in r:
                    if gen != self.gen:
                        break
                    d = json.loads(line)
                    buf += d.get("message", {}).get("content", "")
                    while (n := _cut(buf, first)):
                        self._say(buf[:n], first, t0, gen)
                        said.append(buf[:n])
                        buf, first = buf[n:], False
                    if d.get("done"):
                        break
            if buf.strip() and gen == self.gen:
                self._say(buf, first, t0, gen)
                said.append(buf)
        except Exception as e:  # noqa: BLE001
            self.log(f"[local] brain failed: {e}")
            self._say("Sorry, my local brain hiccuped.", True, t0, gen)
            self.history.pop()
            return
        text = _clean(" ".join(said))
        if text:
            # The cue goes back into history, so the model keeps using them.
            cue = _CUE.match("".join(said).strip())
            self.history.append({"role": "assistant",
                                 "content": (cue.group(0) + " " if cue else "") + text})
            _MEMORY["at"] = time.time()
            self.on_agent(text)
        else:
            self.history.pop()

    # ---- speaking -------------------------------------------------------- #
    def _say(self, chunk: str, first: bool, t0: float, gen: int) -> None:
        for cue in _CUE.findall(chunk):
            self._emote(cue.strip().lower())
        self.voice_q.put((gen, _clean(chunk), first, t0))

    def _emote(self, cue: str) -> None:
        if cue in CUES:
            threading.Thread(target=rt._tool_move, args=({"move": cue},), daemon=True).start()

    def _voice_worker(self) -> None:
        while True:
            item = self.voice_q.get()
            if item is None:
                return
            gen, text, first, t0 = item
            if not text or gen != self.gen:
                continue
            t1 = time.time()
            pcm = _voice_pcm(text)
            if gen != self.gen:
                continue
            if first:
                self.log(f"[latency] first audio {1000 * (time.time() - t0):.0f}ms after "
                         f"you stopped talking (local; voice {1000 * (time.time() - t1):.0f}ms)")
            dur = self.stream.push(pcm) if self.stream.ok else rt._play_pcm_on_robot(pcm)
            self.speaking_until = max(self.speaking_until, time.time()) + dur
            reachy_denoise.set_speaking(self.speaking_until - time.time() + 0.3)

    def _barge_in(self) -> None:
        self.gen += 1
        self.speaking_until = 0.0
        reachy_denoise.set_speaking(False)
        if self.stream.ok:
            self.stream.clear()

    # ---- listening ------------------------------------------------------- #
    def run(self, should_run, stop: threading.Event) -> None:
        # Warm everything before the first sentence needs it.
        threading.Thread(target=_model, daemon=True).start()
        threading.Thread(target=_voice_pcm, args=("hi",), daemon=True).start()
        threading.Thread(target=_warm_llm, args=(self.system,), daemon=True).start()
        self.log(f"[local] listening: {_stt_label()} → {LLM} → {_load_tts()}, all on this Mac")
        floor, voiced, quiet, speech, pre = 300.0, 0, 0, bytearray(), b""
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
                        if not speech:
                            # Keep the 0.2s before the trigger: it holds the
                            # first syllable, which whisper otherwise drops.
                            speech.extend(pre)
                            if talking_back:
                                self._barge_in()    # they talked over Vibey
                        speech.extend(block)
                    else:
                        pre = (pre + block)[-SR * 2 // 5:]
                    if speech and quiet >= END_BLOCKS:
                        audio, speech, voiced, pre = bytes(speech), bytearray(), 0, b""
                        if len(audio) < SR * 2 * 0.4:
                            continue                # a cough, not a sentence
                        t0 = time.time()
                        text = self._transcribe(audio)
                        if not text:
                            continue
                        self.log(f"[latency] heard in {1000 * (time.time() - t0):.0f}ms: {text!r}")
                        self.on_user(text)
                        if worker and worker.is_alive():
                            self._barge_in()
                            worker.join(timeout=2)
                        # Time from the end of their speech, not from the cut.
                        t_end = t0 - END_BLOCKS * 0.1
                        worker = threading.Thread(target=self._reply, args=(text, t_end), daemon=True)
                        worker.start()
            except Exception as e:  # noqa: BLE001
                if stop.is_set():
                    return
                self.log(f"[local] mic dropped ({e}); retrying in 2s")
                time.sleep(2)

    def _transcribe(self, pcm: bytes) -> str:
        a = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        kind, m = _model()
        if kind == "mlx":
            text = m.transcribe(a, path_or_hf_repo=MLX_WHISPER, language="en",
                                condition_on_previous_text=False).get("text", "").strip()
        else:
            segs, _ = m.transcribe(a, language="en", beam_size=1, vad_filter=True,
                                   without_timestamps=True,
                                   condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segs).strip()
        # Whisper's favourite hallucinations on silence.
        if text.lower().strip(" .!") in ("", "you", "thank you", "thanks for watching", "bye"):
            return ""
        return text

    def close(self) -> None:
        self.voice_q.put(None)
        self.stream.close()


def run(should_run=None, on_user_text=None, on_agent_text=None, log=None,
        stop_event: "threading.Event | None" = None) -> None:
    stop = stop_event or threading.Event()
    session = LocalSession(on_user_text, on_agent_text, log)
    try:
        session.run(should_run or (lambda: True), stop)
    finally:
        stop.set()
        session.close()
