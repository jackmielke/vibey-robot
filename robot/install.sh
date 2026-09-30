#!/usr/bin/env bash
# robot/install.sh: prepare the robot (Reachy Mini CM4) to run Vibey's services.
#
# Runs ON THE ROBOT, as the pollen user, normally via robot/deploy.sh:
#     ssh pollen@<robot> 'bash ~/vibey/app/robot/install.sh'
# Idempotent: safe to run again after every deploy. It prepares, it does NOT
# start or enable any vibey service (deploy.sh --phase N does that, one phase
# at a time, with a control-loop check before and after).
#
# Layout it expects / creates:
#   ~/vibey/app          code, rsynced by deploy.sh (never git-cloned here)
#   ~/vibey/app/.env     secrets, copied by deploy.sh, chmod 600, never printed
#   ~/vibey/.venv        lean venv: numpy + websockets (requirements-robot.txt)
#   ~/vibey/bin/sdk-python  wrapper around the Pollen daemon's own Python, which
#                        already has reachy_mini + GStreamer for the LOCAL media
#                        backend; nothing is installed into it
#   ~/vibey/memories     memories (already there, reachy_memories.py)
#   ~/.config/systemd/user/vibey-*.service, vibey.target
#
# It never uses an interactive sudo. Two things need root and are only tried
# with `sudo -n`; if that is not allowed it prints the one command to run:
#   * cgroup cpu delegation to user services (else CPUQuota is ignored; Nice
#     still applies)
#   * lingering, so user services start at boot without anyone logging in
set -euo pipefail

VIBEY="$HOME/vibey"
APP="$VIBEY/app"
UNIT_DIR="$HOME/.config/systemd/user"
say() { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }

echo "vibey install on $(hostname) ($(uname -m))"

# --- sanity -----------------------------------------------------------------
[[ "$(uname -m)" == "aarch64" ]] || warn "expected aarch64, got $(uname -m)"
[[ -f "$APP/reachy_chat.py" ]] || { warn "no code at $APP; run robot/deploy.sh from the Mac first"; exit 1; }
PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 11)))')
[[ "$PY_OK" == 1 ]] || { warn "python3 >= 3.11 required, have $(python3 -V)"; exit 1; }
say "python $(python3 -V 2>&1 | cut -d' ' -f2)"
DISK_FREE_MB=$(df -Pm "$HOME" | awk 'NR==2 {print $4}')
say "disk free: ${DISK_FREE_MB} MB"
(( DISK_FREE_MB > 1024 )) || warn "under 1 GB free; journald and transcripts will fill it"

mkdir -p "$VIBEY/bin" "$VIBEY/memories" "$UNIT_DIR"

# --- secrets: permissions only, contents never read or echoed --------------
if [[ -f "$APP/.env" ]]; then
  chmod 600 "$APP/.env"
  say ".env present (600)"
else
  warn ".env missing at $APP/.env; deploy.sh copies it"
fi
for f in .telegram_state.json .telegram_contacts.json; do
  [[ -f "$APP/$f" ]] && chmod 600 "$APP/$f"
done

# --- lean venv ---------------------------------------------------------------
REQ="$APP/robot/requirements-robot.txt"
if [[ ! -x "$VIBEY/.venv/bin/python" ]]; then
  say "creating $VIBEY/.venv"
  python3 -m venv "$VIBEY/.venv"
fi
REQ_HASH=$(sha256sum "$REQ" | cut -c1-16)
if [[ "$(cat "$VIBEY/.venv/.req-hash" 2>/dev/null)" != "$REQ_HASH" ]]; then
  say "installing requirements-robot.txt"
  "$VIBEY/.venv/bin/python" -m pip install -q --upgrade pip
  "$VIBEY/.venv/bin/python" -m pip install -q --only-binary=:all: -r "$REQ"
  echo "$REQ_HASH" > "$VIBEY/.venv/.req-hash"
else
  say "venv up to date"
fi
# Heavy packages must never land on the robot.
for pkg in faster-whisper ctranslate2 torch mediapipe dlib face-recognition; do
  if "$VIBEY/.venv/bin/python" -m pip show -q "$pkg" >/dev/null 2>&1; then
    warn "$pkg is installed in ~/vibey/.venv and should not be (see ROBOT_NATIVE.md)"
  fi
done

# --- the daemon's Python, for the camera/mic bridges ------------------------
# Pollen's daemon runs from its own venv with reachy_mini + PyGObject/GStreamer.
# Reusing that interpreter gives an exact SDK/daemon version match and no
# GStreamer build on the CM4. VIBEY_SDK_PYTHON overrides the detection.
sdk_ok() { [[ -x "$1" ]] && "$1" -c 'import reachy_mini, gi' >/dev/null 2>&1; }
SDK_PY="${VIBEY_SDK_PYTHON:-}"
if [[ -z "$SDK_PY" ]]; then
  for pid in $(pgrep -f 'reachy_mini[._-]daemon|reachy-mini-daemon' || true); do
    cand=$(ps -o args= -p "$pid" | awk '{print $1}')
    [[ "$cand" == */python* ]] || cand=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
    if sdk_ok "$cand"; then SDK_PY="$cand"; break; fi
  done
fi
if [[ -z "$SDK_PY" ]]; then
  for cand in /venvs/*/bin/python /opt/*/bin/python /home/*/.venv*/bin/python "$HOME"/*/bin/python; do
    if sdk_ok "$cand"; then SDK_PY="$cand"; break; fi
  done
fi
if [[ -n "$SDK_PY" ]] && sdk_ok "$SDK_PY"; then
  printf '#!/bin/sh\n# Pollen daemon interpreter (reachy_mini + GStreamer). Written by install.sh.\nexec %q "$@"\n' "$SDK_PY" > "$VIBEY/bin/sdk-python"
  chmod 755 "$VIBEY/bin/sdk-python"
  SDK_VER=$("$VIBEY/bin/sdk-python" -c 'from importlib.metadata import version; print(version("reachy_mini"))' 2>/dev/null || echo "?")
  say "sdk-python -> $SDK_PY (reachy_mini $SDK_VER)"
else
  warn "could not find the daemon's Python with reachy_mini + gi."
  warn "mic/camera units will not start. Find it with: systemctl cat 'reachy*' | grep ExecStart"
  warn "then rerun: VIBEY_SDK_PYTHON=/path/to/python bash $0"
fi

# --- cgroup cpu delegation (CPUQuota) ----------------------------------------
UID_=$(id -u)
CTRL="/sys/fs/cgroup/user.slice/user-$UID_.slice/user@$UID_.service/cgroup.controllers"
if [[ -r "$CTRL" ]] && grep -qw cpu "$CTRL"; then
  say "cgroup cpu controller delegated: CPUQuota enforced"
else
  DROPIN=/etc/systemd/system/user@.service.d/vibey-delegate.conf
  if sudo -n true 2>/dev/null; then
    sudo -n mkdir -p "$(dirname "$DROPIN")"
    printf '[Service]\nDelegate=cpu cpuset io memory pids\n' | sudo -n tee "$DROPIN" >/dev/null
    sudo -n systemctl daemon-reload
    say "cpu delegation drop-in written; takes effect after reboot (or re-login)"
  else
    warn "CPUQuota NOT enforced yet (no cpu controller for user units). Once, as root:"
    warn "  sudo mkdir -p /etc/systemd/system/user@.service.d && printf '[Service]\\nDelegate=cpu cpuset io memory pids\\n' | sudo tee $DROPIN && sudo reboot"
  fi
fi

# --- linger: user services at boot, without a login -------------------------
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" == "yes" ]]; then
  say "linger on"
elif loginctl enable-linger "$USER" 2>/dev/null || sudo -n loginctl enable-linger "$USER" 2>/dev/null; then
  say "linger enabled"
else
  warn "linger off: services stop when ssh logs out. Run once: sudo loginctl enable-linger $USER"
fi

# --- units -------------------------------------------------------------------
changed=0
for src in "$APP"/robot/systemd/*; do
  dst="$UNIT_DIR/$(basename "$src")"
  if ! cmp -s "$src" "$dst"; then cp "$src" "$dst"; changed=1; fi
done
if (( changed )); then
  systemctl --user daemon-reload
  say "units updated"
else
  say "units unchanged"
fi
systemctl --user enable vibey.target >/dev/null 2>&1 || true

enabled=$(systemctl --user list-unit-files 'vibey-*.service' --state=enabled --no-legend 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
say "enabled services: ${enabled:-none (deploy.sh --phase 1 starts the first ones)}"
echo "done."
