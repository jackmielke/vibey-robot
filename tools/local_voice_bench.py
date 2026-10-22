"""Times each stage of the local voice brain on this Mac, without a mic.

    .venv/bin/python3 tools/local_voice_bench.py

Speaks a question with macOS `say`, then runs it through the same functions
services/reachy_local.py uses: whisper, Ollama, the voice. Prints how long each
took and the rough time from the end of your sentence to the first sound.
"""
import json
import os
import subprocess
import sys
import time
import wave

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services"))
import numpy as np  # noqa: E402

import reachy_local as L  # noqa: E402

Q = "Hey Vibey, what should I cook for dinner tonight?"
subprocess.run(["say", "-o", "/tmp/vibey_q.wav", "--data-format=LEI16@16000", Q], check=True)
with wave.open("/tmp/vibey_q.wav") as w:
    pcm = w.readframes(w.getnframes())

t = time.time(); L._model(); print(f"stt load      {time.time() - t:6.2f}s  ({L._stt_label()})")
t = time.time(); L._load_tts(); print(f"voice load    {time.time() - t:6.2f}s  ({L._tts['engine']})")
sess = L.LocalSession.__new__(L.LocalSession)
for _ in range(2):
    t = time.time(); heard = L.LocalSession._transcribe(sess, pcm); stt = time.time() - t
print(f"transcribe    {stt:6.2f}s  {heard!r}")

system = L.INSTRUCTIONS + L.rt._now_line()
L._warm_llm(system)
body = {"model": L.LLM, "stream": True, "think": False, "keep_alive": "30m",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": heard}],
        "options": {"num_predict": 140, "temperature": 0.7, "num_ctx": 4096}}
t, first_tok, first_chunk, chunk, buf, n = time.time(), None, None, None, "", 0
with L._post("/api/chat", body, 120) as r:
    for line in r:
        d = json.loads(line)
        piece = d.get("message", {}).get("content", "")
        if piece and first_tok is None:
            first_tok = time.time() - t
        buf += piece
        n += 1
        if first_chunk is None and L._cut(buf, True):
            first_chunk = time.time() - t
            chunk = buf[:L._cut(buf, True)]
        if d.get("done"):
            break
total = time.time() - t
first_chunk = first_chunk or total
chunk = chunk or buf
print(f"llm 1st token {first_tok:6.2f}s  1st chunk {first_chunk:.2f}s  all {total:.2f}s  ~{n / total:.0f} tok/s")
print(f"              {buf.strip()!r}")
L._voice_pcm(L._clean(chunk))
t = time.time(); pcm24 = L._voice_pcm(L._clean(chunk)); tts = time.time() - t
print(f"voice 1st chunk {tts:4.2f}s for {len(pcm24) / 2 / L.rt.RT_SR:.1f}s of audio  {L._clean(chunk)!r}")
end = L.END_BLOCKS * 0.1
print(f"\n≈ first sound {end + stt + first_chunk + tts:.2f}s after you stop talking "
      f"(endpoint {end:.2f} + stt {stt:.2f} + llm {first_chunk:.2f} + voice {tts:.2f})")
