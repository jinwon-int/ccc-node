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
CDT_PKG="${PHONE_BROWSER_CHROME_DEVTOOLS_PKG:-chrome-devtools-mcp@1.12.0}"
PW_PKG="${PHONE_BROWSER_PLAYWRIGHT_PKG:-@playwright/mcp@0.0.83}"
SOCKET="chrome_devtools_remote"
CHROME_ACTIVITY="com.android.chrome/com.google.android.apps.chrome.Main"

log() { printf '%s: %s\n' "$PROG" "$*" >&2; }
die() { local rc="$1"; shift; log "$*"; exit "$rc"; }
usage() {
  cat >&2 <<'EOF'
usage: phone-browser-mcp <ssh-host> [--mcp chrome-devtools|playwright] [-- <extra MCP args>]
stdio MCP server driving the phone's Android Chrome over CDP (adb forward + ssh tunnel).
EOF
}

case "${1:-}" in
  "") usage; exit 2 ;;
  -h|--help) usage; exit 0 ;;
esac
HOST="$1"; shift
[[ "$HOST" =~ ^[A-Za-z0-9][A-Za-z0-9._@-]*$ ]] || die 2 "invalid ssh host: $HOST"
[[ "$SERIAL" =~ ^[A-Za-z0-9._:-]+$ ]] || die 2 "invalid serial: $SERIAL"
[[ "$KEEP_AWAKE" =~ ^[0-9]+$ ]] || die 2 "PHONE_BROWSER_KEEP_AWAKE must be seconds"
[[ "$SOCKET_WAIT" =~ ^[0-9]+$ ]] || die 2 "PHONE_BROWSER_SOCKET_WAIT must be seconds"
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

ssh_q=("$SSH_BIN" -n -o BatchMode=yes -o ConnectTimeout=10)
radb() { # <adb args...> → run the phone's own adb on the self-loop serial
  local q=""
  printf -v q '%q ' adb -s "$SERIAL" "$@"
  "${ssh_q[@]}" "$HOST" "$q"
}
socket_count() { radb shell "grep -c $SOCKET /proc/net/unix" 2>/dev/null | tr -dc '0-9'; }

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

CTL=""; CHILD=""; AWAKE_PID=""
# shellcheck disable=SC2317  # invoked via trap
cleanup() {
  trap - EXIT INT TERM HUP
  [ -n "$AWAKE_PID" ] && kill "$AWAKE_PID" 2>/dev/null
  [ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null
  [ -n "$CTL" ] && "$SSH_BIN" -S "$CTL" -O exit "$HOST" >/dev/null 2>&1
  [ -n "$CTL" ] && rm -f "$CTL"
  radb forward --remove "tcp:$PP" >/dev/null 2>&1
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

# ── 3. per-session local tunnel ──────────────────────────────────────────────
{ mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"; } || die 3 "cannot create $STATE_DIR"
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
LP=""; p=$PORT_BASE; end=$((PORT_BASE + PORT_SPAN)); failures=0
while [ "$p" -lt "$end" ] && [ "$failures" -lt 3 ]; do
  if ! port_in_use "$p"; then
    c="$STATE_DIR/$HOST.$$.$p.ctl"
    if "${ssh_q[@]}" -M -S "$c" -f -N -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -L "127.0.0.1:$p:127.0.0.1:$PP" "$HOST" 2>/dev/null; then
      CTL="$c"; LP="$p"; break
    fi
    failures=$((failures + 1))
  fi
  p=$((p + 1))
done
[ -n "$LP" ] || die 3 "could not open a tunnel to $HOST ($failures ssh failures)"
log "$HOST Chrome DevTools → http://127.0.0.1:$LP (phone port $PP), mcp=$MCP"

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
