#!/usr/bin/env bash
# robot/deploy.sh: push Vibey's code to the robot and move services onto it.
# Runs on the MAC. See ROBOT_NATIVE.md for the phases and why in that order.
#
#   robot/deploy.sh                 rsync code + restart whatever already runs there
#   robot/deploy.sh --install       ...and run robot/install.sh on the robot (idempotent)
#   robot/deploy.sh --phase 1       ...and move phase 1's services to the robot
#   robot/deploy.sh --rollback 1    stop phase 1 on the robot, hand it back to the Mac
#   robot/deploy.sh --status        what runs where, plus a 20s load check
#   --host 10.0.0.196               override the robot address (default: REACHY_URL in .env)
#   --no-check                      skip the before/after control-loop checks
#
# Moving a service = stop the Mac's copy, record it in .robot_services (so
# start_wonder.sh and the Mac watchdog leave it alone), enable the unit on the
# robot, then prove the control loop still holds >= 40Hz. If it does not, the
# script says so and prints the rollback command; it does not guess.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

HOST=""; INSTALL=0; PHASE=""; ROLLBACK=""; STATUS=0; CHECK=1
while (( $# )); do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --install) INSTALL=1; shift ;;
    --phase) PHASE="$2"; shift 2 ;;
    --rollback) ROLLBACK="$2"; shift 2 ;;
    --status) STATUS=1; shift ;;
    --no-check) CHECK=0; shift ;;
    -h|--help) sed -n 2,17p "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$HOST" ]]; then
  HOST=$(grep -E '^REACHY_URL=' .env 2>/dev/null | tail -1 | sed -E 's|.*https?://([^:/]+).*|\1|')
  HOST="${HOST:-reachy-mini.local}"
fi
ROBOT="pollen@$HOST"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=5)
rsh() { "${SSH[@]}" "$ROBOT" "$@"; }

# Phases. Names are the watchdog's service names; units are vibey-<unit>.service.
# Phase 3 (memories on the robot's disk) adds no process: the env already points
# chat/telegram/viewer at ~/vibey/memories, see ROBOT_NATIVE.md.
phase_services() {
  case "$1" in
    1) echo "robot_mic chat watchdog" ;;     # voice brain + clap wake + emotes/idle, loop guard
    2) echo "telegram alarm" ;;               # texting + timers
    3) echo "" ;;                             # memories: local disk, no new process
    4) echo "viewer camera" ;;                # dashboard + camera (fps-capped)
    5) echo "memory" ;;                       # faces, last
    *) echo "no phase $1" >&2; exit 2 ;;
  esac
}
unit_of() { case "$1" in robot_mic) echo vibey-mic.service ;; *) echo "vibey-$1.service" ;; esac; }
script_of() {
  case "$1" in
    robot_mic) echo reachy_robot_mic.py ;; chat) echo reachy_chat.py ;;
    telegram) echo reachy_telegram.py ;; alarm) echo reachy_alarm.py ;;
    viewer) echo reachy_viewer.py ;; camera) echo reachy_camera.py ;;
    memory) echo reachy_memory.py ;; watchdog) echo "" ;;  # the Mac keeps its own watchdog
  esac
}

echo "robot: $ROBOT"
rsh true || { echo "cannot ssh to $ROBOT (robot off, other network, or key not loaded)"; exit 1; }

if (( STATUS )); then
  echo "Mac leaves these to the robot: $(grep -v '^#' .robot_services 2>/dev/null | tr '\n' ' ' || true)"
  rsh "systemctl --user list-units 'vibey-*' --all --no-legend" || true
  bash robot/load_check.sh "$HOST" 20 || true
  exit 0
fi

# --- 1. compile locally before shipping anything -----------------------------
python3 -m py_compile reachy_*.py

# --- 2. code (secrets and runtime state excluded; --delete only touches code) --
rsh 'mkdir -p ~/vibey/app ~/vibey/memories'
rsync -az --delete -e "${SSH[*]}" \
  --exclude '.git' --exclude '.claude' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.venv*' --exclude 'reachy_env' --exclude 'models' \
  --exclude '.env' --exclude '.env.*' \
  --exclude 'memories' --exclude 'memory' --exclude 'transcripts' --exclude 'captures' \
  --exclude 'agent_runs' --exclude 'notes' --exclude '*.log' \
  --exclude '.telegram_state.json' --exclude '.telegram_contacts.json' \
  --exclude '.audio_prefs.json' --exclude '.voice_brain.json' --exclude '.mic_source.json' \
  --exclude '.speaker_source.json' --exclude '.cost.json' --exclude '.off' \
  --exclude '.robot_services' --exclude 'alarms.json' \
  ./ "$ROBOT:vibey/app/"
echo "code synced"

# --- 3. secrets: copied as a file, 600, never printed -------------------------
if [[ -f .env ]]; then
  rsync -a --chmod=F600 -e "${SSH[*]}" .env "$ROBOT:vibey/app/.env"
  echo ".env synced (600)"
fi
# First-time seeds of runtime state. Never overwrites what the robot already has.
for f in alarms.json .telegram_contacts.json .voice_brain.json .audio_prefs.json; do
  [[ -f "$f" ]] && rsync -a --ignore-existing --chmod=F600 -e "${SSH[*]}" "$f" "$ROBOT:vibey/app/$f"
done

# --- 4. install -----------------------------------------------------------------
if (( INSTALL )) || ! rsh 'test -x ~/vibey/.venv/bin/python'; then
  rsh 'bash ~/vibey/app/robot/install.sh'
fi

# --- 5. restart what already runs there, on the new code ----------------------
rsh "systemctl --user try-restart 'vibey-*.service'" || true

check() {  # $1 label
  (( CHECK )) || return 0
  echo "--- load check: $1"
  bash robot/load_check.sh "$HOST" 30
}

# --- 6. move a phase onto the robot -------------------------------------------
if [[ -n "$PHASE" ]]; then
  svcs=$(phase_services "$PHASE")
  [[ -z "$svcs" ]] && { echo "phase $PHASE adds no service (see ROBOT_NATIVE.md)"; exit 0; }
  check "before phase $PHASE" || echo "(already under the floor BEFORE this phase)"
  for s in $svcs; do
    scr=$(script_of "$s")
    if [[ -n "$scr" ]]; then
      grep -qx "$s" .robot_services 2>/dev/null || echo "$s" >> .robot_services
      pkill -f "$scr" 2>/dev/null && echo "stopped the Mac's $scr" || true
    fi
    if [[ "$s" == telegram && -f .telegram_state.json ]]; then
      # The Mac poller is stopped now, so its offset is final: hand it over, or
      # the robot re-answers the last day of messages.
      rsync -a --chmod=F600 -e "${SSH[*]}" .telegram_state.json "$ROBOT:vibey/app/.telegram_state.json"
    fi
    rsh "systemctl --user enable --now $(unit_of "$s")"
    echo "started $(unit_of "$s") on the robot"
  done
  # The robot watchdog picks its service list at start.
  rsh "systemctl --user try-restart vibey-watchdog.service" || true
  sleep 20
  rsh "systemctl --user --no-pager status 'vibey-*' | grep -E '^(●|  *Active:)'" || true
  if ! check "after phase $PHASE"; then
    echo
    echo "Control loop is under ${VIBEY_LOOP_MIN_HZ:-40}Hz. Try lower CPUQuota in robot/systemd,"
    echo "or roll back:  robot/deploy.sh --rollback $PHASE"
    exit 1
  fi
  echo "If the Mac is staying on, run 'vibey' once so its remaining services"
  echo "point at the robot (start_wonder.sh reads .robot_services)."
fi

# --- 7. hand a phase back to the Mac -------------------------------------------
if [[ -n "$ROLLBACK" ]]; then
  for s in $(phase_services "$ROLLBACK"); do
    rsh "systemctl --user disable --now $(unit_of "$s")" || true
    if [[ -f .robot_services ]]; then
      grep -vx "$s" .robot_services > .robot_services.tmp || true
      mv .robot_services.tmp .robot_services
    fi
    echo "stopped $(unit_of "$s") on the robot"
  done
  if [[ "$(phase_services "$ROLLBACK")" == *telegram* ]]; then
    rsync -a -e "${SSH[*]}" "$ROBOT:vibey/app/.telegram_state.json" .telegram_state.json || true
  fi
  echo "Mac copies are not auto-started. Run: vibey   (start_wonder.sh restarts the Mac side)"
fi
echo "done."
