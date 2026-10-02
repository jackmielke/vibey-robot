#!/bin/zsh
# start_wonder.sh — boot the whole Wonder stack in one command.
#
#   ./start_wonder.sh          start everything (idempotent: restarts cleanly)
#   ./start_wonder.sh stop     stop everything
#
# Services:
#   :8771  reachy_camera.py  — robot camera → MJPEG   (SDK venv)
#   :8770  reachy_viewer.py  — dashboard              (system python)
#   :8772  reachy_chat.py    — voice-to-voice loop    (.venv, whisper)
#   :8773  reachy_memory.py  — face recognition       (SDK venv)

cd "$(dirname "$0")"

# Load .env and export everything in it (REACHY_URL, keys, …)
set -a; source .env 2>/dev/null; set +a
# reachy_camera.py takes a bare host, derive it from REACHY_URL
export REACHY_HOST=$(echo "${REACHY_URL:-http://reachy-mini.local:8000}" | sed -E 's|https?://([^:/]+).*|\1|')

# Discovery: the robot's DHCP lease moves, mDNS has died on its own, and after
# a brownout it can come back on its own hotspot. reachy_connect.py find tries,
# in order: what .env says (resolving a .local name), mDNS, the hotspot
# 10.42.0.1, then a parallel sweep of this Mac's subnet. It rewrites .env only
# when what .env says no longer leads to the robot. Every service inherits the
# literal address it prints: the SDK's WebRTC path is happier with an IP than
# with a .local name, and a per-request mDNS lookup makes everything sluggish.
if [[ "$1" != "stop" ]]; then
  echo "looking for the robot…"
  if _found=$(python3 reachy_connect.py find --write --current "$REACHY_URL"); then
    export REACHY_URL="$_found"
    export REACHY_HOST=$(echo "$_found" | sed -E 's|https?://([^:/]+).*|\1|')
  fi
fi
# A stale token in ~/.cache/huggingface/token 401s even PUBLIC model downloads
# (whisper small.en). Anonymous access works fine.
export HF_HUB_DISABLE_IMPLICIT_TOKEN=1

stop_all() {
  pkill -f "reachy_camera.py" 2>/dev/null
  pkill -f "reachy_viewer.py" 2>/dev/null
  pkill -f "reachy_chat.py"   2>/dev/null
  pkill -f "reachy_robot_mic.py" 2>/dev/null
  pkill -f "reachy_memory.py" 2>/dev/null
  pkill -f "reachy_vibeverse.py" 2>/dev/null
  pkill -f "reachy_telegram.py" 2>/dev/null
  pkill -f "reachy_bridge.py" 2>/dev/null
  pkill -f "reachy_alarm.py" 2>/dev/null
  pkill -f "reachy_dj.py" 2>/dev/null
  pkill -f "reachy_gestures.py" 2>/dev/null
  pkill -f "reachy_watchdog.py" 2>/dev/null
  pkill -f "reachy_heal.py" 2>/dev/null
  echo "wonder stack stopped."
}

if [[ "$1" == "stop" ]]; then stop_all; exit 0; fi

echo "robot: $REACHY_URL"
if ! curl -s -m 4 -o /dev/null "$REACHY_URL/api/daemon/status"; then
  echo "⚠️  robot unreachable at $REACHY_URL — check WiFi / update REACHY_URL in .env"
  echo "    not found by mDNS, the hotspot, or a sweep of this subnet. Is it powered and on this Wi-Fi?"
  # Fail LOUD: a silent exit here once left the stack down all evening.
  python3 - <<'PYEOF'
import json, os, urllib.parse, urllib.request
try:
    tok = os.environ.get("TELEGRAM_VIBEY_TOKEN", "")   # never TELEGRAM_BOT_TOKEN: that is OpenClaw's
    chat = json.load(open(".telegram_state.json")).get("owner")
    if tok and chat:
        msg = f"🚨 Vibey stack failed to start: robot unreachable at {os.environ.get('REACHY_URL')}. Check its power/WiFi, then rerun start_wonder.sh"
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage",
            urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode(), timeout=10).read()
except Exception:
    pass
PYEOF
  exit 1
fi

stop_all >/dev/null 2>&1
sleep 1

# Services that have moved onto the robot itself (robot/deploy.sh writes this
# file; see ROBOT_NATIVE.md). The Mac must not start its own copy. No file =
# everything starts here, exactly as before.
ROBOT_OWNS=" $(grep -v '^#' .robot_services 2>/dev/null | tr '\n' ' ') "
here() { [[ "$ROBOT_OWNS" != *" $1 "* ]]; }
[[ -n "${ROBOT_OWNS// /}" ]] && echo "on the robot, not here:${ROBOT_OWNS% }"
# Mac-side services that talk to a moved one follow it to the robot.
here chat   || export CHAT_URL="http://${REACHY_HOST}:8772"
here camera || export CAM_URL="http://${REACHY_HOST}:8771"
here memory || export MEM_URL="http://${REACHY_HOST}:8773"

here camera    && reachy_env/bin/python3 reachy_camera.py  > /tmp/reachy_camera.log 2>&1 &
here viewer    && python3                reachy_viewer.py  > /tmp/reachy_viewer.log 2>&1 &
# arch -arm64: .venv's python is universal, and a Rosetta parent (the Intel
# system python running the watchdog/healer) otherwise picks its x86 slice,
# which can't import the arm64 numpy.
here chat      && arch -arm64 .venv/bin/python3 reachy_chat.py >> /tmp/reachy_chat.log 2>&1 &
here robot_mic && reachy_env/bin/python3 reachy_robot_mic.py > /tmp/reachy_robot_mic.log 2>&1 &
here memory    && reachy_env/bin/python3 reachy_memory.py  > /tmp/reachy_memory.log 2>&1 &
python3                reachy_vibeverse.py > /tmp/vibeverse.log     2>&1 &
here telegram  && python3                reachy_telegram.py  > /tmp/telegram.log      2>&1 &
# Gesture bridge mirrors your handsfree session onto the robot's body —
# fun for parties, twitchy as an always-on behavior. Opt in with BRIDGE=1.
[[ "$BRIDGE" == "1" ]] && NO_WAKE=1 python3 reachy_bridge.py > /tmp/reachy_bridge.log 2>&1 &
here alarm     && python3                reachy_alarm.py     > /tmp/reachy_alarm.log  2>&1 &
reachy_env/bin/python3 reachy_dj.py        > /tmp/reachy_dj.log     2>&1 &   # music + beat-synced dancing
# Gesture watcher: waves back at you. Its own venv on purpose — mediapipe pins
# numpy<2 and the robot SDK needs numpy>=2.2.5, so they cannot share one.
[[ -x .venv-gestures/bin/python3 ]] && \
  .venv-gestures/bin/python3 reachy_gestures.py > /tmp/reachy_gestures.log 2>&1 &
python3                reachy_watchdog.py  > /tmp/reachy_watchdog.log 2>&1 &   # restarts anything that dies
python3                reachy_heal.py      >> /tmp/reachy_heal.log    2>&1 &   # robot drops, returns, moves
pgrep -x caffeinate >/dev/null || (caffeinate -dims > /dev/null 2>&1 &)   # alarms need an awake Mac

echo "starting… (camera takes ~10s to negotiate WebRTC)"
sleep 12

ok=0; want=0
for svc in "8771/status camera" "8770/perception viewer" "8772/state chat" "8773/current memory"; do
  port_path="${svc%% *}"; name="${svc##* }"
  here "$name" || continue
  want=$((want+1))
  # Retry rather than probe once: the camera's WebRTC handshake regularly
  # finishes a few seconds after the fixed sleep above, which used to report a
  # scary "❌ camera" for a service that was actually mid-negotiation and came
  # up fine seconds later. Up to ~24s of grace, exits early the moment it answers.
  up=0
  for _ in $(seq 1 12); do
    if curl -s -m 3 "http://localhost:$port_path" > /dev/null; then up=1; break; fi
    sleep 2
  done
  if [[ $up -eq 1 ]]; then
    echo "  ✅ $name  (:${port_path%%/*})"
    ok=$((ok+1))
  else
    echo "  ❌ $name  (:${port_path%%/*}) — see /tmp/reachy_${name}.log"
  fi
done

echo
dash="http://localhost:8770"; here viewer || dash="http://${REACHY_HOST}:8770"
[[ $ok -eq $want ]] && echo "🤖 Wonder is up → $dash" \
                    || echo "partial start ($ok/$want): check logs above"
