#!/usr/bin/env bash
# robot/load_check.sh: is the robot keeping up? Samples the motor control loop
# rate, CPU and RAM, and what each vibey service costs.
#
#   robot/load_check.sh                 from the Mac: robot from .env / reachy-mini.local
#   robot/load_check.sh 10.0.0.196 60   from the Mac: that robot, 60 seconds
#   bash load_check.sh --local 30       on the robot itself
#
# Exit status 0 = control loop averaged >= VIBEY_LOOP_MIN_HZ (default 40), 1 = below,
# 2 = could not read it. deploy.sh uses this before and after each phase.
# Reference points (2026-09-22): nominal 50Hz; ~46Hz idle; ~28Hz with the
# daemon's own face tracking on. Under ~30Hz the robot feels glitchy.
set -uo pipefail

MIN_HZ="${VIBEY_LOOP_MIN_HZ:-40}"

if [[ "${1:-}" != "--local" ]]; then
  HOST="${1:-}"
  SECS="${2:-30}"
  if [[ -z "$HOST" ]]; then
    ENVF="$(cd "$(dirname "$0")/.." && pwd)/.env"
    HOST=$(grep -E '^REACHY_URL=' "$ENVF" 2>/dev/null | tail -1 | sed -E 's|.*https?://([^:/]+).*|\1|')
    HOST="${HOST:-reachy-mini.local}"
  fi
  exec ssh -o BatchMode=yes -o ConnectTimeout=5 "pollen@$HOST" \
    "VIBEY_LOOP_MIN_HZ=$MIN_HZ bash -s -- --local $SECS" < "$0"
fi

SECS="${2:-30}"
STEP=2
N=$(( SECS / STEP )); (( N < 1 )) && N=1

cpu_snap() { awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat; }
loop_hz() {
  curl -s -m 3 http://localhost:8000/api/daemon/status | python3 -c '
import json, sys
def find(d, k):
    if isinstance(d, dict):
        if k in d: return d[k]
        for v in d.values():
            r = find(v, k)
            if r is not None: return r
try:
    s = json.load(sys.stdin)
except Exception:
    sys.exit(0)
hz, gap = find(s, "mean_control_loop_frequency"), find(s, "max_control_loop_interval")
if isinstance(hz, (int, float)):
    print(f"{hz:.1f} {(gap or 0) * 1000:.0f}")
'
}
unit_cpu_ns() {
  for u in $(systemctl --user list-units 'vibey-*.service' --state=active --no-legend 2>/dev/null | awk '{print $1}'); do
    echo "$u $(systemctl --user show -p CPUUsageNSec --value "$u")"
  done
}

echo "load check on $(hostname), ${SECS}s, want control loop >= ${MIN_HZ}Hz"
read -r t0 i0 < <(cpu_snap)
declare -A u0; while read -r u ns; do [[ -n "$u" ]] && u0[$u]=$ns; done < <(unit_cpu_ns)

hzs=(); gaps=()
for ((i = 0; i < N; i++)); do
  if read -r hz gap < <(loop_hz) && [[ -n "${hz:-}" ]]; then
    hzs+=("$hz"); gaps+=("$gap")
    printf '  %3ds  %5s Hz  worst gap %4s ms\n' $(( i * STEP )) "$hz" "$gap"
  else
    printf '  %3ds  (no control_loop_stats: daemon down or motors off?)\n' $(( i * STEP ))
  fi
  sleep "$STEP"
done

read -r t1 i1 < <(cpu_snap)
CPU=$(awk -v a="$t0" -v b="$i0" -v c="$t1" -v d="$i1" 'BEGIN{dt=c-a; if (dt<=0) dt=1; printf "%.0f", 100*(1-(d-b)/dt)}')
echo
echo "CPU (all 4 cores): ${CPU}%   load avg: $(cut -d' ' -f1-3 /proc/loadavg)"
awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{printf "RAM: %.2f GB used of %.2f GB (%.2f GB available)\n", (t-a)/1048576, t/1048576, a/1048576}' /proc/meminfo
if [[ -r /sys/class/thermal/thermal_zone0/temp ]]; then
  echo "SoC temp: $(( $(cat /sys/class/thermal/thermal_zone0/temp) / 1000 ))C   throttled: $(vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || echo '?')"
fi

echo
echo "vibey services (share of ONE core over the window, nice, RSS):"
while read -r u ns; do
  [[ -z "$u" ]] && continue
  pct=$(awk -v a="${u0[$u]:-$ns}" -v b="$ns" -v s="$SECS" 'BEGIN{printf "%.0f", (b-a)/1e7/s}')
  pid=$(systemctl --user show -p MainPID --value "$u")
  rss=$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.0fMB", $1/1024}')
  ni=$(ps -o ni= -p "$pid" 2>/dev/null | tr -d ' ')
  printf '  %-26s %4s%%  nice %-3s %s\n' "$u" "$pct" "${ni:-?}" "${rss:-?}"
done < <(unit_cpu_ns)
[[ ${#u0[@]} -eq 0 ]] && echo "  (none running)"

echo
echo "top processes:"
ps -eo pid,ni,pcpu,rss,comm --sort=-pcpu | head -8 | awk 'NR==1{print "  "$0; next}{printf "  %s %s %s %.0fMB %s\n", $1, $2, $3, $4/1024, $5}'

echo
if (( ${#hzs[@]} == 0 )); then
  echo "RESULT: unknown (no control-loop readings)"; exit 2
fi
printf '%s\n' "${hzs[@]}" | awk -v min="$MIN_HZ" -v worst="$(printf '%s\n' "${gaps[@]}" | sort -n | tail -1)" '
  {s+=$1; if (NR==1 || $1<lo) lo=$1}
  END{avg=s/NR; printf "RESULT: control loop avg %.1fHz, min %.1fHz, worst gap %sms -> %s\n", avg, lo, worst, (avg>=min ? "OK" : "TOO LOW"); exit (avg>=min ? 0 : 1)}'
