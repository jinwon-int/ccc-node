#!/usr/bin/env bash
# termux-restart-frontends.sh — self-update restart-cmd for Termux nodes that
# run BOTH frontends from the prepared generation behind
# ~/.ccc-node/bridge-current: the Telegram bridge (start.sh, idle-gated by its
# own restart controller) and the runit Matrix frontend (ccc-matrix-bridge,
# launched by run-matrix through the same pointer).
#
#   1. Restart Telegram through bridge-current.sh --restart -d (verified by the
#      bridge's own controller; its exit status is this script's unless the
#      Matrix step below fails).
#   2. Make the Matrix frontend follow bridge-current: restart it only when the
#      generation it is RUNNING differs from the pointer, idle-gated on
#      ~/.ccc-matrix/health.json workload.active_requests. If it stays busy
#      until the restart deadline, a detached follower keeps waiting (max 60
#      min) instead of killing a turn.
#   3. After a Matrix restart, PROVE the new process survives (ccc-node#2175,
#      DOC-3768 ④): the 2026-10-09 switches crash-looped Matrix
#      (`ModuleNotFoundError: aiohttp`, 30 s backoff) while this step reported
#      `matrix restarted: running=<gen> want=<gen>` — it compared the
#      generation name in the cmdline and never asked whether the process was
#      alive. Now the step waits for the bridge's own health.json to show a
#      process started at/after the restart, with the wanted generation under
#      runtime_generation.python_prefix, then holds for
#      CCC_MATRIX_SURVIVE_SECONDS (60) and requires: same pid the whole
#      window, `sv status` uptime >= window for that pid (runit resets it on
#      every crash), and service.state == available. Anything else → exit 3,
#      so self-update records the restart as failed and notifies instead of
#      `activated`. The pointer is NOT rolled back here (Telegram is serving the
#      same generation; that call is the operator's — the previous target is in
#      ~/.ccc-node/bridge-current.prev-<ts>).
#
# Usage: termux-restart-frontends.sh            (default: both frontends)
#        termux-restart-frontends.sh --matrix-only [wait_seconds]
#        termux-restart-frontends.sh --verify [survive_seconds]   (read-only:
#            judge the CURRENT Matrix process with the same rule; no restart)
#        termux-restart-frontends.sh --dry-run                    (read-only)
# Env:   CCC_TERMUX_CCC_NODE_DIR (~/.ccc-node), CCC_TERMUX_MATRIX_DIR
#        (~/.ccc-matrix), CCC_TERMUX_MATRIX_SERVICE (ccc-matrix-bridge), SVDIR
#        ($PREFIX/var/service), CCC_TERMUX_RESTART_LOG
#        (~/.claude/state/restart-frontends.log), CCC_MATRIX_SURVIVE_SECONDS
#        (60), CCC_MATRIX_START_SECONDS (90: budget for health.json to report
#        the new process), CCC_MATRIX_POLL_SECONDS (5), CCC_MATRIX_BUSY_POLL_SECONDS
#        (15), CCC_MATRIX_MIN_WINDOW_SECONDS (20), CCC_TERMUX_FOLLOWER_WAIT_SECONDS
#        (3600), CCC_BRIDGE_RESTART_DEADLINE_EPOCH (set by self-update; the Matrix
#        step keeps 30 s of it in reserve and truncates the survival window to
#        what is left, never below 20 s).
# Exit:  0 ok (or Matrix busy → follower detached) · <telegram rc> when the
#        Telegram restart failed · 3 Matrix did not come up / did not survive
#        the window on the wanted generation · 2 usage.
set -u

H="${HOME:?}"
CN="${CCC_TERMUX_CCC_NODE_DIR:-$H/.ccc-node}"
POINTER="$CN/bridge-current"
MDIR="${CCC_TERMUX_MATRIX_DIR:-$H/.ccc-matrix}"
HEALTH="$MDIR/health.json"
SVC="${CCC_TERMUX_MATRIX_SERVICE:-ccc-matrix-bridge}"
SVDIR_="${SVDIR:-${PREFIX:-/usr}/var/service}"
LOG="${CCC_TERMUX_RESTART_LOG:-$H/.claude/state/restart-frontends.log}"
SURVIVE="${CCC_MATRIX_SURVIVE_SECONDS:-60}"
START_WAIT="${CCC_MATRIX_START_SECONDS:-90}"
POLL="${CCC_MATRIX_POLL_SECONDS:-5}"
BUSY_POLL="${CCC_MATRIX_BUSY_POLL_SECONDS:-15}"
MIN_WINDOW="${CCC_MATRIX_MIN_WINDOW_SECONDS:-20}"
FOLLOWER_WAIT="${CCC_TERMUX_FOLLOWER_WAIT_SECONDS:-3600}"

now() { date +%s; }
log() { mkdir -p "$(dirname "$LOG")" 2>/dev/null || :; printf '%s %s\n' "$(date -Is)" "$*" >> "$LOG" 2>/dev/null || :; }
want() { basename "$(readlink -f "$POINTER" 2>/dev/null)"; }

# health.json → "pid started_epoch state generation" ("-" for anything unknown).
# The bridge rewrites this file itself; a stale copy from a dead process is
# filtered by the pid liveness check in have()/survives().
health_fields() {
  python3 - "$HEALTH" <<'PY' 2>/dev/null || printf -- '- - - -\n'
import json, re, sys
from datetime import datetime
pid = started = state = gen = "-"
try:
    h = json.load(open(sys.argv[1], encoding="utf-8"))
    p = h.get("process") or {}
    if type(p.get("pid")) is int and p["pid"] > 1:
        pid = str(p["pid"])
    s = p.get("started_at")
    if isinstance(s, str):
        started = str(int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()))
    st = (h.get("service") or {}).get("state")
    if isinstance(st, str) and re.fullmatch(r"[a-z_-]{1,32}", st):
        state = st
    prefix = (h.get("runtime_generation") or {}).get("python_prefix")
    if isinstance(prefix, str):
        m = re.search(r"/preparations/([^/]+)/", prefix)
        if m:
            gen = m.group(1)
except Exception:
    pass
print(pid, started, state, gen)
PY
}
alive() { [ "${1:-}" != "-" ] && [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
have() { # generation the LIVE Matrix process runs, or empty
  local pid started state gen
  read -r pid started state gen < <(health_fields)
  alive "$pid" && [ "$gen" != "-" ] && printf '%s' "$gen"
}
busy() {
  python3 -c 'import json,sys
try: h=json.load(open(sys.argv[1])); print(int((h.get("workload") or {}).get("active_requests") or 0))
except Exception: print(0)' "$HEALTH" 2>/dev/null || echo 0
}
sv_run() { # "pid uptime_seconds" of the supervised run process, or empty
  local line
  line="$(SVDIR="$SVDIR_" sv status "$SVC" 2>/dev/null | head -n 1)"
  line="${line%%;*}"   # cut before "; run: log: ..." — the log pid is not the bridge
  printf '%s' "$line" | sed -nE "s/^run: ${SVC}: \(pid ([0-9]+)\) ([0-9]+)s.*/\1 \2/p"
}

# survives <restart_epoch> <deadline_epoch|0> — judge the Matrix process that
# started at/after <restart_epoch>. Returns 0 only when it ran the wanted
# generation for the whole survival window.
survives() {
  local since="$1" deadline="${2:-0}" w pid started state gen t0 window spid sup
  w="$(want)"
  t0=$(now)
  # phase 1: the bridge must report a NEW process on the wanted generation
  while :; do
    read -r pid started state gen < <(health_fields)
    if [ "$started" != "-" ] && [ "$started" -ge $((since - 2)) ] && alive "$pid" && [ "$gen" = "$w" ]; then break; fi
    if [ $(( $(now) - t0 )) -ge "$START_WAIT" ]; then
      log "matrix NOT up after ${START_WAIT}s: health pid=${pid} started=${started} gen=${gen} want=${w} (since=${since})"
      return 3
    fi
    sleep "$POLL"
  done
  # phase 2: hold the window — same pid, runit uptime growing, no crash
  window="$SURVIVE"
  if [ "$deadline" -gt 0 ]; then
    local left=$(( deadline - $(now) ))
    if [ "$left" -lt "$window" ]; then
      window=$(( left < MIN_WINDOW ? MIN_WINDOW : left ))
      log "matrix survival window truncated to ${window}s by restart deadline"
    fi
  fi
  local end=$(( $(now) + window ))
  while [ "$(now)" -lt "$end" ]; do
    if ! alive "$pid"; then log "matrix pid $pid died inside the ${window}s window (gen=$w)"; return 3; fi
    read -r spid sup < <(sv_run)
    if [ -n "${spid:-}" ] && [ "$spid" != "$pid" ]; then log "matrix supervised pid changed $pid -> $spid inside the window (crash/restart, gen=$w)"; return 3; fi
    sleep "$POLL"
  done
  read -r spid sup < <(sv_run)
  read -r pid2 _ state gen2 < <(health_fields)
  if ! alive "$pid" || [ "$pid2" != "$pid" ] || [ "$gen2" != "$w" ]; then
    log "matrix changed at the end of the window: pid=$pid now=$pid2 gen=$gen2 want=$w"; return 3
  fi
  if [ -z "${spid:-}" ] || [ "$spid" != "$pid" ] || [ "${sup:-0}" -lt $(( window - POLL )) ]; then
    log "matrix sv status disagrees: run pid=${spid:--} uptime=${sup:--}s want pid=$pid uptime>=$((window - POLL))s (gen=$w)"; return 3
  fi
  if [ "$state" != "available" ]; then
    log "matrix service.state=$state after ${window}s (want available, gen=$w)"; return 3
  fi
  log "matrix verified: gen=$w pid=$pid uptime=${sup}s state=$state window=${window}s"
  return 0
}

matrix_follow() { # <until_epoch> → 0 ok · 2 busy until deadline · 3 unhealthy
  local w h restart_epoch rc; w="$(want)"; h="$(have)"
  if [ "$h" = "$w" ]; then log "matrix up to date ($h)"; return 0; fi
  while [ "$(busy)" != 0 ]; do
    [ "$(now)" -ge "$1" ] && { log "matrix busy until deadline (running=${h:-none} want=$w)"; return 2; }
    sleep "$BUSY_POLL"
  done
  restart_epoch=$(now)
  SVDIR="$SVDIR_" sv restart "$SVC" >> "$LOG" 2>&1
  log "matrix restart issued: running=${h:-none} want=$w"
  survives "$restart_epoch" "$1"; rc=$?
  [ "$rc" = 0 ] || log "matrix restarted but NOT healthy (rc=$rc): pointer unchanged, operator decision — previous target in $CN/bridge-current.prev-*"
  return "$rc"
}

case "${1:-}" in
  --dry-run)
    w="$(want)"; h="$(have)"
    read -r spid sup < <(sv_run)
    echo "bridge-current=$w matrix_running=${h:-none} matrix_busy=$(busy) sv_pid=${spid:--} sv_uptime=${sup:--}s survive=${SURVIVE}s -> $([ "$h" = "$w" ] && echo no-op || echo would-restart-matrix-then-verify)"
    exit 0 ;;
  --verify)
    [ -z "${2:-}" ] || SURVIVE="$2"
    read -r pid started state gen < <(health_fields)
    [ "$started" != "-" ] || { echo "matrix: no health.json / started_at" >&2; exit 3; }
    survives "$started" 0; rc=$?
    tail -n 1 "$LOG"; exit "$rc" ;;
  --matrix-only) matrix_follow $(( $(now) + ${2:-3600} )); exit $? ;;
  "") ;;
  *) echo "termux-restart-frontends: unknown argument $1" >&2; exit 2 ;;
esac

bash "$CN/scripts/bridge-current.sh" --path "$H" --restart -d; rc=$?
log "telegram restart rc=$rc"
deadline_m=$(( ${CCC_BRIDGE_RESTART_DEADLINE_EPOCH:-$(( $(now) + 300 ))} - 30 ))
matrix_follow "$deadline_m"; m=$?
if [ "$m" = 2 ]; then
  ( nohup setsid bash "$0" --matrix-only "$FOLLOWER_WAIT" > /dev/null 2>&1 < /dev/null & )
  log "matrix follower detached (waits up to ${FOLLOWER_WAIT}s for idle)"
  m=0
fi
[ "$rc" != 0 ] && exit "$rc"
exit "$m"
