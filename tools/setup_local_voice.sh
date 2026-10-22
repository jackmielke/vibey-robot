#!/bin/zsh
# Installs everything the "local" voice brain (services/reachy_local.py) needs
# on this Mac: an Ollama model, the Kokoro voice, and mlx-whisper for speech
# recognition on the GPU. Safe to re-run; skips what is already there.
#
#   tools/setup_local_voice.sh            # then: .venv/bin/python3 tools/local_voice_bench.py
#
# Kokoro and mlx-whisper need Python 3.10+. If .venv is older, a new .venv is
# built next to it with the same packages, checked, and swapped in; the old one
# stays as .venv-py<version> (swap back: rm -rf .venv && mv .venv-py39 .venv).
set -e
cd "$(dirname "$0")/.."
LLM=${LOCAL_LLM:-qwen3:4b-instruct}
MLX_MODEL=${LOCAL_MLX_WHISPER:-mlx-community/whisper-large-v3-turbo}

if ! command -v ollama >/dev/null; then
  echo "Ollama is not installed. brew install ollama (or ollama.com), then re-run."; exit 1
fi
if ! curl -s localhost:11434/api/version >/dev/null; then
  (ollama serve >/tmp/ollama.log 2>&1 &)
  sleep 3
fi
ollama list | grep -q "^${LLM}" || ollama pull "$LLM"

pyok() { arch -arm64 "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; }
if ! pyok .venv/bin/python3; then
  NEW=""
  for p in python3.12 ~/.local/bin/python3.12 python3.13 python3.11 python3.10 /opt/homebrew/bin/python3; do
    command -v $p >/dev/null && pyok "$(command -v $p)" && { NEW=$(command -v $p); break; }
  done
  [[ -n "$NEW" ]] || { echo "Need Python 3.10+ for Kokoro (brew install python@3.12)"; exit 1; }
  OLDV=$(.venv/bin/python3 -c 'import sys; print(f"{sys.version_info[0]}{sys.version_info[1]}")')
  echo "rebuilding .venv on $NEW (old one kept as .venv-py$OLDV)"
  rm -rf .venv-new
  arch -arm64 "$NEW" -m venv .venv-new
  .venv/bin/python3 -m pip freeze | grep -v "^-e " > /tmp/vibey-venv-freeze.txt
  arch -arm64 .venv-new/bin/python3 -m pip install -q --upgrade pip
  # Old pins may not exist for the new Python; install what resolves, then the core set.
  arch -arm64 .venv-new/bin/python3 -m pip install -q -r /tmp/vibey-venv-freeze.txt \
    || sed 's/==.*//' /tmp/vibey-venv-freeze.txt | xargs arch -arm64 .venv-new/bin/python3 -m pip install -q \
    || true
  arch -arm64 .venv-new/bin/python3 -m pip install -q -r requirements-chat.txt
  # Same imports reachy_chat.py needs before we trust it.
  (cd services && arch -arm64 ../.venv-new/bin/python3 -c "import reachy_chat" ) \
    || { echo "new venv can't import reachy_chat; leaving .venv alone (see .venv-new)"; exit 1; }
  mv .venv .venv-py$OLDV && mv .venv-new .venv
fi
PY=.venv/bin/python3

arch -arm64 $PY -m pip install -q kokoro-onnx soundfile mlx-whisper

mkdir -p models/kokoro
base=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
for f in kokoro-v1.0.onnx voices-v1.0.bin; do
  [[ -s models/kokoro/$f ]] || curl -fL --progress-bar -o models/kokoro/$f "$base/$f"
done
# Fetch the whisper weights now (a stale HF_TOKEN in the shell breaks public
# downloads, so leave it out), so the first conversation doesn't wait on it.
env -u HF_TOKEN -u HUGGING_FACE_HUB_TOKEN arch -arm64 $PY -c "
from huggingface_hub import snapshot_download; snapshot_download('$MLX_MODEL')"
echo "local voice ready: $LLM + Kokoro + mlx-whisper ($MLX_MODEL)"
