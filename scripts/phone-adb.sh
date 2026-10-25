#!/usr/bin/env bash
# phone-adb — run adb against an Android (Termux) node through an SSH tunnel to
# that phone's OWN adb server (#2517).
#
#   phone-adb <ssh-host> [--serial S] <adb args...>
#   phone-adb <ssh-host> --status
#   phone-adb <ssh-host> --close
#
# Why: a direct `adb connect <phone>:5555` needs every node's ADB key authorized
# on every phone, and Android wireless debugging revokes unused authorizations,
# so those links fall back to `unauthorized` and leave an "Allow USB debugging?"
# dialog on the phone. The phone's own adb client (self-loop 127.0.0.1:5555) is
# already authorized; forwarding a local port to the phone's adb server
# (127.0.0.1:5037) lets any node with SSH access use that one key — no new
# authorization, no private-key distribution. Access == SSH access.
#
# Modes:
#   tunnel   local `adb` present: ssh ControlMaster -L <port>:127.0.0.1:5037,
#            then `adb -H 127.0.0.1 -P <port> -s <serial> <args>`. Everything
#            adb does works (shell, push, pull, install, logcat, forward…).
#   ssh      no local `adb` (or PHONE_ADB_MODE=ssh): `ssh <host> adb -s <serial>
#            <args>`. Commands that read/write LOCAL files are refused.
#
# Safety:
#   - kill-server / start-server / bare `disconnect` / bare `reconnect` are
#     refused: they would act on the phone's shared server (and every other
#     node's session on it).
#   - tunnel mode refuses when local and phone adb versions differ: a client
#     that sees a different server version asks that server to exit.
#   - <ssh-host> must match [A-Za-z0-9][A-Za-z0-9._@-]* (no ssh option
#     injection); it is resolved by the caller's ~/.ssh/config.
#
# The tunnel stays up until `--close` or the SSH link drops (ServerAlive). One
# tunnel per host, shared by every invocation on this node.
#
# Exit codes: adb's own rc on success paths; 2 usage / refused subcommand;
#   3 tunnel or SSH failure; 4 unsupported in ssh mode; 5 adb version mismatch.
#
# Env (also test seams): PHONE_ADB_SSH (ssh), PHONE_ADB_ADB (adb),
#   PHONE_ADB_STATE_DIR (~/.cache/phone-adb), PHONE_ADB_MODE (auto|tunnel|ssh),
#   PHONE_ADB_SERIAL (127.0.0.1:5555), PHONE_ADB_PORT_BASE (15037),
#   PHONE_ADB_PORT_SPAN (200), PHONE_ADB_CONNECT_TIMEOUT (10).
set -uo pipefail

PROG="phone-adb"
SSH_BIN="${PHONE_ADB_SSH:-ssh}"
ADB_BIN="${PHONE_ADB_ADB:-adb}"
STATE_DIR="${PHONE_ADB_STATE_DIR:-${HOME:-/root}/.cache/phone-adb}"
MODE="${PHONE_ADB_MODE:-auto}"
SERIAL="${PHONE_ADB_SERIAL:-127.0.0.1:5555}"
PORT_BASE="${PHONE_ADB_PORT_BASE:-15037}"
PORT_SPAN="${PHONE_ADB_PORT_SPAN:-200}"
CONNECT_TIMEOUT="${PHONE_ADB_CONNECT_TIMEOUT:-10}"
REMOTE_SERVER_PORT=5037

die() { local rc="$1"; shift; printf '%s: %s\n' "$PROG" "$*" >&2; exit "$rc"; }

usage() {
  cat <<'EOF'
usage: phone-adb <ssh-host> [--serial S] <adb args...>
       phone-adb <ssh-host> --status | --close
Runs adb on an Android/Termux node through an SSH tunnel to the phone's own
adb server (no per-node ADB key authorization). Default serial 127.0.0.1:5555.
EOF
}

case "${1:-}" in
  "") usage >&2; exit 2 ;;
  -h|--help) usage; exit 0 ;;
esac
HOST="$1"; shift
[[ "$HOST" =~ ^[A-Za-z0-9][A-Za-z0-9._@-]*$ ]] || die 2 "invalid ssh host: $HOST"

ACTION=run
while [ $# -gt 0 ]; do
  case "$1" in
    --serial) [ $# -ge 2 ] || die 2 "--serial needs a value"; SERIAL="$2"; shift 2 ;;
    --serial=*) SERIAL="${1#--serial=}"; shift ;;
    --status) ACTION=status; shift ;;
    --close) ACTION=close; shift ;;
    --) shift; break ;;
    *) break ;;
  esac
done
[[ "$SERIAL" =~ ^[A-Za-z0-9._:-]+$ ]] || die 2 "invalid serial: $SERIAL"
if [ "$ACTION" = run ] && [ $# -eq 0 ]; then usage >&2; exit 2; fi

case "$MODE" in
  auto) if command -v "$ADB_BIN" >/dev/null 2>&1; then MODE=tunnel; else MODE=ssh; fi ;;
  tunnel|ssh) ;;
  *) die 2 "PHONE_ADB_MODE must be auto|tunnel|ssh (got $MODE)" ;;
esac

# ── subcommand guard ─────────────────────────────────────────────────────────
# adb global options (-s/-d/-e/-t/-H/-P/-L…) would fight the managed host,
# port and serial, so the first word must be the subcommand.
subcmd="${1:-}"
case "$subcmd" in
  -*) die 2 "adb global option '$subcmd' is managed by $PROG; use --serial S" ;;
  kill-server|start-server)
    die 2 "refusing '$subcmd': it would act on $HOST's shared adb server (use: ssh $HOST adb $subcmd)" ;;
  disconnect|reconnect)
    [ $# -gt 1 ] || die 2 "refusing bare '$subcmd': it would drop every transport on $HOST's server; name a target" ;;
esac

# ── ssh mode ─────────────────────────────────────────────────────────────────
ssh_base=("$SSH_BIN" -o BatchMode=yes -o ConnectTimeout="$CONNECT_TIMEOUT")

run_ssh_mode() {
  case "$subcmd" in
    push|pull|install|install-multiple|install-multi-package|sideload|restore|backup|sync)
      die 4 "'$subcmd' reads/writes local files and needs a local adb (tunnel mode); install adb (android-tools-adb / android-tools) or run it on $HOST" ;;
  esac
  local q="" tty=()
  # Interactive `adb shell` needs a remote tty.
  if [ "$subcmd" = shell ] && [ $# -eq 1 ] && [ -t 0 ] && [ -t 1 ]; then tty=(-t); fi
  # %q quoting assumes a bash-compatible login shell on the phone (Termux).
  printf -v q '%q ' adb -s "$SERIAL" "$@"
  "${ssh_base[@]}" "${tty[@]}" "$HOST" "$q"
}

# ── tunnel mode ──────────────────────────────────────────────────────────────
CTL="$STATE_DIR/$HOST.ctl"
PORTF="$STATE_DIR/$HOST.port"

tunnel_alive() { "$SSH_BIN" -S "$CTL" -O check "$HOST" >/dev/null 2>&1; }

port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

adb_version_of() { sed -n 's/^Android Debug Bridge version \([0-9.]*\).*/\1/p' | head -1; }

open_tunnel() {
  { mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"; } || die 3 "cannot create $STATE_DIR"
  local lv rv
  lv="$("$ADB_BIN" version 2>/dev/null | adb_version_of)"
  rv="$("${ssh_base[@]}" "$HOST" 'adb version' 2>/dev/null | adb_version_of)"
  [ -n "$rv" ] || die 3 "no adb on $HOST (or ssh failed)"
  [ "$lv" = "$rv" ] || die 5 "adb version mismatch: local ${lv:-?} vs $HOST $rv — refusing (a mismatched client asks the server to exit)"
  # Make sure the phone's server is up and its self-loop transport exists.
  "${ssh_base[@]}" "$HOST" "adb start-server >/dev/null 2>&1; adb connect $(printf '%q' "$SERIAL") >/dev/null 2>&1; true" \
    || die 3 "ssh $HOST failed while preparing the adb server"
  # A free-looking port can still lose a bind race, so retry a few ports; but
  # stop after 3 ssh failures — those are usually the link, not the port.
  local p=$PORT_BASE end=$((PORT_BASE + PORT_SPAN)) failures=0
  while [ "$p" -lt "$end" ] && [ "$failures" -lt 3 ]; do
    if ! port_in_use "$p"; then
      if "${ssh_base[@]}" -M -S "$CTL" -f -N \
          -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
          -L "127.0.0.1:$p:127.0.0.1:$REMOTE_SERVER_PORT" "$HOST" 2>/dev/null; then
        printf '%s\n' "$p" > "$PORTF"
        return 0
      fi
      failures=$((failures + 1))
    fi
    p=$((p + 1))
  done
  die 3 "could not open a tunnel to $HOST (tried from port $PORT_BASE, $failures ssh failures)"
}

ensure_tunnel() {
  if tunnel_alive && [ -s "$PORTF" ]; then PORT="$(cat "$PORTF")"; return 0; fi
  rm -f "$CTL" "$PORTF"
  open_tunnel
  PORT="$(cat "$PORTF")"
}

with_lock() {
  mkdir -p "$STATE_DIR" 2>/dev/null
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$STATE_DIR/$HOST.lock" || die 3 "cannot open $STATE_DIR/$HOST.lock"
    flock -w 30 9 || die 3 "lock busy: $STATE_DIR/$HOST.lock"
  fi
  "$@"
  if command -v flock >/dev/null 2>&1; then flock -u 9; fi
}

case "$ACTION" in
  close)
    if tunnel_alive; then "$SSH_BIN" -S "$CTL" -O exit "$HOST" >/dev/null 2>&1; fi
    rm -f "$CTL" "$PORTF"
    echo "$PROG: $HOST tunnel closed"
    exit 0 ;;
  status)
    echo "host=$HOST mode=$MODE serial=$SERIAL"
    if [ "$MODE" = tunnel ]; then
      if tunnel_alive && [ -s "$PORTF" ]; then
        PORT="$(cat "$PORTF")"; echo "tunnel=up port=$PORT"
        "$ADB_BIN" -H 127.0.0.1 -P "$PORT" devices
      else
        echo "tunnel=down"
      fi
    else
      "${ssh_base[@]}" "$HOST" 'adb devices'
    fi
    exit $? ;;
esac

if [ "$MODE" = ssh ]; then run_ssh_mode "$@"; exit $?; fi
PORT=""
with_lock ensure_tunnel
[ -n "$PORT" ] || die 3 "tunnel to $HOST not available"
exec "$ADB_BIN" -H 127.0.0.1 -P "$PORT" -s "$SERIAL" "$@"
