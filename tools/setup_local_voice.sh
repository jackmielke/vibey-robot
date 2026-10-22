#!/bin/zsh
# Installs everything the "local" voice brain (services/reachy_local.py) needs
# on this Mac: an Ollama model, the Kokoro voice, and mlx-whisper for speech
# recognition on the GPU. Safe to re-run; skips what is already there.
#
#   tools/setup_local_voice.sh            # then: python tools/local_voice_bench.py
set -e
cd "$(dirname "$0")/.."
LLM=${LOCAL_LLM:-qwen3:4b-instruct}
PY=.venv/bin/python3

if ! command -v ollama >/dev/null; then
  echo "Ollama is not installed. brew install ollama (or ollama.com), then re-run."; exit 1
fi
if ! curl -s localhost:11434/api/version >/dev/null; then
  (ollama serve >/tmp/ollama.log 2>&1 &)
  sleep 3
fi
ollama list | grep -q "^${LLM}" || ollama pull "$LLM"

arch -arm64 $PY -m pip install -q kokoro-onnx soundfile
[[ "$(uname -m)" == "arm64" || "$(sysctl -n sysctl.proc_translated 2>/dev/null)" == "1" ]] \
  && arch -arm64 $PY -m pip install -q mlx-whisper

mkdir -p models/kokoro
base=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
for f in kokoro-v1.0.onnx voices-v1.0.bin; do
  [[ -s models/kokoro/$f ]] || curl -fL --progress-bar -o models/kokoro/$f "$base/$f"
done
echo "local voice ready: $LLM + Kokoro + $( $PY -c 'import mlx_whisper' 2>/dev/null && echo mlx-whisper || echo faster-whisper)"
