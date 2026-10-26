#!/usr/bin/env bash
# phone-browser-mcp — stdio MCP launcher that drives a phone's real Android
# Chrome over CDP (#2522).
#
#   phone-browser-mcp <ssh-host> [--mcp chrome-devtools|playwright] [-- <extra MCP args>]
#
# Register it like any stdio MCP server, e.g.
#   claude mcp add -s user android-browser -- ~/.claude/hooks/phone-browser-mcp.sh <ssh-host>
#
# Path:  MCP (this node) --ssh -L--> phone 127.0.0.1:<pp> --adb forward-->
#        localabstract:chrome_devtools_remote (Android Chrome)
#
# Requirements on the phone: Termux with sshd + adb, a self-loop ADB transport
# (default 127.0.0.1:5555) and Android **USB debugging on** (adb_enabled=1):
# Chrome only opens its DevTools socket then (wireless debugging alone is not
# enough). This launcher never changes that setting — it reports and exits.
# No local adb is needed: the forward is created by the phone's own adb.
#
# Per session: `adb forward tcp:0 …` lets the phone's adb pick a free port, so
# concurrent sessions from different nodes never clobber each other; a private
# SSH ControlMaster forwards a free local port to it. On exit (EOF, TERM, INT,
# HUP) only this session's forward and tunnel are removed.
#
# Session state (#2529): every session owns the stem
#   $STATE_DIR/<host>.<wrapper-pid>.<local-port>
# with <stem>.ctl (the ControlMaster socket) and <stem>.state (host, adb serial
# and the phone forward port this session created). Leaks are reclaimed twice:
#  - startup sweep: before opening its own tunnel the launcher scans its host's
#    stems (only names in this scheme, only files owned by this uid, never
#    symlinks). A stem whose wrapper PID is gone — or alive but no longer a
#    phone-browser-mcp wrapper (PID reuse, checked via /proc/<pid>/cmdline or
#    `ps`) — is orphaned: its master gets `-O exit`, its socket is removed and
#    exactly the forward recorded in its .state is removed. A socket without a
#    .state file (pre-#2529 sessions) only gets its tunnel closed; phone
#    forwards are left alone because other nodes' sessions share the phone.
#    The sweep is best-effort: bounded per step and in total
#    (PHONE_BROWSER_SWEEP_BUDGET), errors go to stderr, it never fails launch.
#  - SIGKILL watcher: a small reaper (this script re-run as `--reap-after`,
#    under setsid when available, stdin/stdout on /dev/null, stderr appended
#    to $STATE_DIR/reaper.log) polls the wrapper PID every
#    PHONE_BROWSER_WATCH_INTERVAL seconds. If the wrapper dies without running
#    its traps, the reaper reclaims the stem the same way; normal cleanup
#    stops the reaper and removes the .state file, which also ends it.
# A .state file is claimed by atomic rename before its forward is removed, so
# concurrent sweeps/reapers never remove a forward twice. When the phone is
# unreachable the .state file is kept and the next launch retries it.
#
# The screen must be on for screenshots/evaluate (Chrome renders no frames
# while the display is off; those calls hang). The launcher wakes the screen
# once at start; PHONE_BROWSER_KEEP_AWAKE=<sec> re-sends a wake key every <sec>
# while the MCP runs (off by default — sessions are long and the screen would
# stay on for all of them).
#
# Security: CDP drives the phone Chrome profile including its cookies/logins.
# Access == SSH access to the phone. Use isolated contexts (chrome-devtools-mcp
# `new_page` isolatedContext) for anything that does not need the profile.
#
# stdout belongs to the MCP protocol: every ssh runs with -n and all of this
# script's messages go to stderr.
#
# Exit codes: the MCP's rc; 2 usage; 3 ssh/tunnel failure; 6 DevTools socket
#   unavailable (USB debugging off / Chrome not reachable).
#
# Env (also test seams): PHONE_BROWSER_SSH (ssh), PHONE_BROWSER_NPX (npx),
#   PHONE_BROWSER_STATE_DIR (~/.cache/phone-browser-mcp), PHONE_BROWSER_SERIAL
#   (127.0.0.1:5555), PHONE_BROWSER_PORT_BASE (19300), PHONE_BROWSER_PORT_SPAN
#   (200), PHONE_BROWSER_KEEP_AWAKE (0), PHONE_BROWSER_SOCKET_WAIT (10),
#   PHONE_BROWSER_SWEEP_BUDGET (20 s; 0 disables the startup sweep),
#   PHONE_BROWSER_WATCH_INTERVAL (3 s; 0 disables the SIGKILL watcher),
#   PHONE_BROWSER_CHROME_DEVTOOLS_PKG (chrome-devtools-mcp@1.12.0),
#   PHONE_BROWSER_PLAYWRIGHT_PKG (@playwright/mcp@0.0.83).
set -uo pipefail

PROG="phone-browser-mcp"
SSH_BIN="${PHONE_BROWSER_SSH:-ssh}"
NPX_BIN="${PHONE_BROWSER_NPX:-npx}"
STATE_DIR="${PHONE_BROWSER_STATE_DIR:-${HOME:-/root}/.cache/phone-browser-mcp}"
SERIAL="${PHONE_BROWSER_SERIAL:-127.0.0.1:5555}"
PORT_BASE="${PHONE_BROWSER_PORT_BASE:-19300}"
PORT_SPAN="${PHONE_BROWSER_PORT_SPAN:-200}"
KEEP_AWAKE="${PHONE_BROWSER_KEEP_AWAKE:-0}"
SOCKET_WAIT="${PHONE_BROWSER_SOCKET_WAIT:-10}"
SWEEP_BUDGET="${PHONE_BROWSER_SWEEP_BUDGET:-20}"
WATCH_INTERVAL="${PHONE_BROWSER_WATCH_INTERVAL:-3}"
CDT_PKG="${PHONE_BROWSER_CHROME_DEVTOOLS_PKG:-chrome-devtools-mcp@1.12.0}"
PW_PKG="${PHONE_BROWSER_PLAYWRIGHT_PKG:-@playwright/mcp@0.0.83}"
SOCKET="chrome_devtools_remote"
CHROME_ACTIVITY="com.android.chrome/com.google.android.apps.chrome.Main"
SELF_NAME="${BASH_SOURCE[0]##*/}"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)/$SELF_NAME"
HOST_RE='^[A-Za-z0-9][A-Za-z0-9._@-]*$'
SERIAL_RE='^[A-Za-z0-9._:-]+$'

log() { printf '%s: %s\n' "$PROG" "$*" >&2; }
die() { local rc="$1"; shift; log "$*"; exit "$rc"; }
usage() {
  cat >&2 <<'EOF'
usage: phone-browser-mcp <ssh-host> [--mcp chrome-devtools|playwright] [-- <extra MCP args>]
stdio MCP server driving the phone's Android Chrome over CDP (adb forward + ssh tunnel).
EOF
}

ssh_q=("$SSH_BIN" -n -o BatchMode=yes -o ConnectTimeout=10)
bounded() { # <seconds> <cmd...> → run an executable with a hard time limit when `timeout` exists
  local s="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout -k 2 "$s" "$@"; else "$@"; fi
}
radb_on() { # <host> <serial> <adb args...> → run the phone's own adb on that serial
  local host="$1" serial="$2" q=""; shift 2
  printf -v q '%q ' adb -s "$serial" "$@"
  bounded 25 "${ssh_q[@]}" "$host" "$q"
}
remove_forward() { # <host> <serial> <pp> → 0 removed (or already gone), 1 phone unreachable
  local rc
  radb_on "$1" "$2" forward --remove "tcp:$3" >/dev/null 2>&1; rc=$?
  case "$rc" in 124|137|255) return 1 ;; esac
  return 0
}

# ── session state / orphan reclaim (#2529) ───────────────────────────────────
pid_is_wrapper() { # <pid> → 0 when <pid> is a live phone-browser-mcp wrapper (not a reaper)
  local pid="$1" args=""
  kill -0 "$pid" 2>/dev/null || return 1
  if [ -r "/proc/$pid/cmdline" ]; then
    args="$(tr '\0' ' ' 2>/dev/null <"/proc/$pid/cmdline")"
  elif command -v ps >/dev/null 2>&1; then
    args="$(ps -o args= -p "$pid" 2>/dev/null)"
  else
    return 0 # cannot tell: assume live, never reclaim a possibly-live session
  fi
  case "$args" in
    *--reap-after*) return 1 ;;
    *"$SELF_NAME"*) return 0 ;;
  esac
  return 1
}
owned_file() { [ -f "$1" ] && [ ! -L "$1" ] && [ -O "$1" ]; }
owned_socket() { [ -S "$1" ] && [ ! -L "$1" ] && [ -O "$1" ]; }
write_state() { # <file> → record this session's phone forward
  ( umask 077; printf 'host=%s\nserial=%s\npp=%s\n' "$HOST" "$SERIAL" "$PP" >"$1" )
}
RS_PP=""; RS_SERIAL=""
read_state() { # <file> → sets RS_PP / RS_SERIAL; 0 when both are valid
  local k v
  RS_PP=""; RS_SERIAL=""
  while IFS='=' read -r k v; do
    case "$k" in pp) RS_PP="$v" ;; serial) RS_SERIAL="$v" ;; esac
  done <"$1"
  [[ "$RS_PP" =~ ^[0-9]{1,5}$ ]] && [ "$RS_PP" -gt 0 ] && [[ "$RS_SERIAL" =~ $SERIAL_RE ]]
}
PHONE_UNREACHABLE=0
reclaim_stem() { # <host> <pid> <local-port> → close a dead session's tunnel + its recorded forward
  local host="$1" stem="$STATE_DIR/$1.$2.$3" claim
  local ctl="$stem.ctl" state="$stem.state" closed=0
  if owned_socket "$ctl"; then
    bounded 5 "$SSH_BIN" -S "$ctl" -O exit "$host" >/dev/null 2>&1 \
      || log "reclaim ${ctl##*/}: master already gone"
    rm -f "$ctl"; closed=1
  elif [ -e "$ctl" ] || [ -L "$ctl" ]; then
    log "reclaim ${ctl##*/}: skipped (not a socket owned by this user)"
  fi
  if owned_file "$state"; then
    claim="$stem.reap.$$"
    mv "$state" "$claim" 2>/dev/null || return 0 # another sweep/reaper claimed it
    if ! read_state "$claim"; then
      log "reclaim ${state##*/}: unreadable state, dropped (phone forwards left alone)"
      rm -f "$claim"
    elif [ "$PHONE_UNREACHABLE" = 1 ] || ! remove_forward "$host" "$RS_SERIAL" "$RS_PP"; then
      PHONE_UNREACHABLE=1
      mv "$claim" "$state" 2>/dev/null || rm -f "$claim"
      log "reclaim ${state##*/}: $host unreachable, phone forward tcp:$RS_PP kept for the next launch"
    else
      rm -f "$claim"
      log "reclaim ${stem##*/}: tunnel closed, phone forward tcp:$RS_PP removed"
    fi
  elif [ -e "$state" ] || [ -L "$state" ]; then
    log "reclaim ${state##*/}: skipped (not a regular file owned by this user)"
  elif [ "$closed" = 1 ]; then
    log "reclaim ${stem##*/}: tunnel closed; no state file, phone forwards left alone"
  fi
  return 0
}
sweep_orphans() { # reclaim this host's sessions whose wrapper is gone (best-effort, bounded)
  [ "$SWEEP_BUDGET" -gt 0 ] && [ -d "$STATE_DIR" ] || return 0
  local start=$SECONDS f name rest pid lport seen=" "
  for f in "$STATE_DIR/$HOST".*.ctl "$STATE_DIR/$HOST".*.state; do
    [ -e "$f" ] || [ -L "$f" ] || continue
    name="${f##*/}"; rest="${name#"$HOST".}"; rest="${rest%.ctl}"; rest="${rest%.state}"
    [[ "$rest" =~ ^([0-9]+)\.([0-9]+)$ ]] || continue # another host's name or not ours
    pid="${BASH_REMATCH[1]}"; lport="${BASH_REMATCH[2]}"
    case "$seen" in *" $pid.$lport "*) continue ;; esac
    seen="$seen$pid.$lport "
    if [ "$pid" != "$$" ] && pid_is_wrapper "$pid"; then continue; fi
    if [ $((SECONDS - start)) -ge "$SWEEP_BUDGET" ]; then
      log "orphan sweep: ${SWEEP_BUDGET}s budget used, the rest is left for the next launch"
      break
    fi
    log "orphan sweep: wrapper $pid of $HOST.$pid.$lport is gone; reclaiming"
    reclaim_stem "$HOST" "$pid" "$lport"
  done
  return 0
}

# ── reaper mode: `--reap-after <host> <wrapper-pid> <local-port>` (internal) ──
if [ "${1:-}" = --reap-after ]; then
  if [ $# -ne 4 ] || ! [[ "$2" =~ $HOST_RE ]] || ! [[ "$3" =~ ^[0-9]+$ ]] || ! [[ "$4" =~ ^[0-9]+$ ]]; then
    die 2 "usage: --reap-after <host> <wrapper-pid> <local-port>"
  fi
  [[ "$WATCH_INTERVAL" =~ ^[0-9]+(\.[0-9]+)?$ ]] || WATCH_INTERVAL=3
  trap '' HUP INT
  PROG="${PROG}[reaper $$]"
  stem="$STATE_DIR/$2.$3.$4"
  while [ -e "$stem.state" ] && pid_is_wrapper "$3"; do sleep "$WATCH_INTERVAL"; done
  [ -e "$stem.state" ] || exit 0 # the wrapper's own cleanup ran
  log "$(date '+%F %T' 2>/dev/null) wrapper $3 died without cleanup; reclaiming $2.$3.$4"
  reclaim_stem "$2" "$3" "$4"
  exit 0
fi

case "${1:-}" in
  "") usage; exit 2 ;;
  -h|--help) usage; exit 0 ;;
esac
HOST="$1"; shift
[[ "$HOST" =~ $HOST_RE ]] || die 2 "invalid ssh host: $HOST"
[[ "$SERIAL" =~ $SERIAL_RE ]] || die 2 "invalid serial: $SERIAL"
[[ "$KEEP_AWAKE" =~ ^[0-9]+$ ]] || die 2 "PHONE_BROWSER_KEEP_AWAKE must be seconds"
[[ "$SOCKET_WAIT" =~ ^[0-9]+$ ]] || die 2 "PHONE_BROWSER_SOCKET_WAIT must be seconds"
[[ "$SWEEP_BUDGET" =~ ^[0-9]+$ ]] || die 2 "PHONE_BROWSER_SWEEP_BUDGET must be seconds"
[[ "$WATCH_INTERVAL" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die 2 "PHONE_BROWSER_WATCH_INTERVAL must be seconds"
MCP=chrome-devtools
while [ $# -gt 0 ]; do
  case "$1" in
    --mcp) [ $# -ge 2 ] || die 2 "--mcp needs a value"; MCP="$2"; shift 2 ;;
    --mcp=*) MCP="${1#--mcp=}"; shift ;;
    --) shift; break ;;
    *) die 2 "unknown argument: $1 (extra MCP args go after --)" ;;
  esac
done
case "$MCP" in chrome-devtools|playwright) ;; *) die 2 "--mcp must be chrome-devtools or playwright" ;; esac

radb() { radb_on "$HOST" "$SERIAL" "$@"; }
socket_count() { radb shell "grep -c $SOCKET /proc/net/unix" 2>/dev/null | tr -dc '0-9'; }

# ── 0. state dir + orphan sweep (#2529) ──────────────────────────────────────
{ mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"; } || die 3 "cannot create $STATE_DIR"
sweep_orphans

# ── 1. DevTools socket ───────────────────────────────────────────────────────
n="$(socket_count)"
if [ "${n:-0}" -eq 0 ]; then
  usb="$(radb shell settings get global adb_enabled 2>/dev/null | tr -dc '0-9')"
  [ -n "$usb" ] || die 3 "cannot reach adb on $HOST (ssh or self-loop $SERIAL down)"
  [ "$usb" = 1 ] || die 6 "USB debugging is off on $HOST (adb_enabled=$usb): Chrome exposes no DevTools socket. Turn on Developer options → USB debugging."
  log "DevTools socket missing on $HOST; waking screen and foregrounding Chrome"
  radb shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
  radb shell am start -n "$CHROME_ACTIVITY" >/dev/null 2>&1
  waited=0
  while [ "$waited" -lt "$SOCKET_WAIT" ]; do
    sleep 1; waited=$((waited + 1))
    n="$(socket_count)"; [ "${n:-0}" -gt 0 ] && break
  done
  [ "${n:-0}" -gt 0 ] || die 6 "Chrome DevTools socket did not appear on $HOST within ${SOCKET_WAIT}s"
else
  radb shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
fi

# ── 2. per-session phone-side forward ────────────────────────────────────────
PP="$(radb forward tcp:0 "localabstract:$SOCKET" 2>/dev/null | tr -dc '0-9')"
if [ -z "$PP" ] || [ "$PP" -eq 0 ]; then die 3 "adb forward on $HOST failed"; fi

CTL=""; CHILD=""; AWAKE_PID=""; WATCH_PID=""
# Record the forward at once (local port 0 until the tunnel picks one) so a
# SIGKILL from here on still leaves the sweep what it needs.
STATE="$STATE_DIR/$HOST.$$.0.state"
write_state "$STATE" || { log "cannot write $STATE (orphan reclaim disabled for this session)"; STATE=""; }
# shellcheck disable=SC2317  # invoked via trap
cleanup() {
  trap - EXIT INT TERM HUP
  [ -n "$WATCH_PID" ] && kill "$WATCH_PID" 2>/dev/null
  [ -n "$AWAKE_PID" ] && kill "$AWAKE_PID" 2>/dev/null
  [ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null
  [ -n "$CTL" ] && bounded 5 "$SSH_BIN" -S "$CTL" -O exit "$HOST" >/dev/null 2>&1
  [ -n "$CTL" ] && rm -f "$CTL"
  if remove_forward "$HOST" "$SERIAL" "$PP"; then
    [ -n "$STATE" ] && rm -f "$STATE"
  else
    log "could not remove phone forward tcp:$PP ($HOST unreachable); the next launch retries it"
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

# ── 3. per-session local tunnel ──────────────────────────────────────────────
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
LP=""; p=$PORT_BASE; end=$((PORT_BASE + PORT_SPAN)); failures=0
while [ "$p" -lt "$end" ] && [ "$failures" -lt 3 ]; do
  if ! port_in_use "$p"; then
    stem="$STATE_DIR/$HOST.$$.$p"
    # The .state file follows the candidate stem before the master starts.
    if [ -n "$STATE" ] && mv "$STATE" "$stem.state" 2>/dev/null; then STATE="$stem.state"; fi
    if "${ssh_q[@]}" -M -S "$stem.ctl" -f -N -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -L "127.0.0.1:$p:127.0.0.1:$PP" "$HOST" 2>/dev/null; then
      CTL="$stem.ctl"; LP="$p"; break
    fi
    failures=$((failures + 1))
  fi
  p=$((p + 1))
done
[ -n "$LP" ] || die 3 "could not open a tunnel to $HOST ($failures ssh failures)"
log "$HOST Chrome DevTools → http://127.0.0.1:$LP (phone port $PP), mcp=$MCP"

# ── 3b. SIGKILL watcher (#2529) ──────────────────────────────────────────────
# Re-runs this script as a reaper in its own session (setsid when available,
# so a process-group kill of the wrapper does not take it along). It never
# holds the MCP's stdin/stdout.
if [ "$WATCH_INTERVAL" != 0 ] && [ -n "$STATE" ] && [ -r "$SELF" ]; then
  wlog="$STATE_DIR/reaper.log"
  if [ -f "$wlog" ] && [ "$(wc -c <"$wlog" 2>/dev/null || echo 0)" -gt 262144 ]; then : >"$wlog"; fi
  launcher=()
  command -v setsid >/dev/null 2>&1 && launcher=(setsid)
  ${launcher[@]+"${launcher[@]}"} "${BASH:-bash}" "$SELF" --reap-after "$HOST" "$$" "$LP" \
    </dev/null >/dev/null 2>>"$wlog" &
  WATCH_PID=$!
fi

# ── 4. optional keep-awake ───────────────────────────────────────────────────
if [ "$KEEP_AWAKE" -gt 0 ]; then
  ( while sleep "$KEEP_AWAKE"; do radb shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1; done ) &
  AWAKE_PID=$!
fi

# ── 5. MCP (stdin/stdout pass through) ───────────────────────────────────────
# `<&0` is required: without job control bash gives `&` jobs /dev/null as stdin.
if [ "$MCP" = chrome-devtools ]; then
  "$NPX_BIN" -y "$CDT_PKG" --browserUrl "http://127.0.0.1:$LP" \
    --no-usage-statistics --no-performance-crux "$@" <&0 &
else
  "$NPX_BIN" -y "$PW_PKG" --cdp-endpoint "http://127.0.0.1:$LP" "$@" <&0 &
fi
CHILD=$!
wait "$CHILD"; rc=$?
CHILD=""
exit "$rc"
