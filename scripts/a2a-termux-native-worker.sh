#!/usr/bin/env bash
# Canonical Termux-native A2A worker: env validation + native launcher + supervisor.
#
# Subcommands (bash dispatcher):
#   check         --env-file <p>   Validate env file (delegates to Python harness).
#   print-command --env-file <p>   Print the exact native `node worker.js` command.
#   run           --env-file <p>   Validate then exec native node worker.js.
#   supervise     --env-file <p>   Singleton SSH tunnel + worker respawn loop.
#   stop                           SIGTERM the supervisor holding the lock.
#   status                         Read-only supervisor/tunnel/worker snapshot.
#
# Historical context — Wiki ND-1236: gongyung and daegyo used to hand-roll the
# supervise/stop/status logic in ~/.hermes/scripts/native-worker-supervisor.sh.
# Two failure modes let a single Seoseo-broker restart snowball into a
# 7-supervisor pile-up on gongyung:
#
#   1. Nothing prevented multiple concurrent supervisors from spawning, so a
#      fleet-wide broker outage stacked retry loops each trying to bind
#      127.0.0.1:18790.
#   2. When a supervisor was killed, its background tunnel subshell was killed
#      but the ssh grandchild orphaned to parent=1 and kept holding the local
#      port, so the next supervisor's `ssh -N -o ExitOnForwardFailure` exited
#      rc=0 in a tight retry loop.
#
# The canonical version now living here fixes both:
#   * Singleton via `flock -n` on a lock file — a second `supervise` invocation
#     exits immediately with rc=3.
#   * `cleanup_orphans` scans for parent=1 SSH tunnels bound to our local port
#     at supervise start and kills them.
#   * `kill_tree` walks pgrep -P recursively so the tunnel subshell AND its ssh
#     grandchild are torn down together, then `sweep_lingering_ssh` KILLs any
#     remaining forward on our port as a safety net.
#
# Health check (with supervisor-count-cap detection) is intentionally in a
# separate script, `a2a-termux-native-worker-health.sh`, so cron can invoke it
# without loading supervise state, and so it can flag the exact >1-supervisor
# pile-up that motivated ND-1236 even when flock has been bypassed.

# NOTE: no `-e`.  The supervisor half needs explicit rc-based propagation
# (rc=2 validation, rc=3 lock contention).  The Python-delegated subcommands
# `exec` and never return, so their rc propagates naturally.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# The env-validation subcommands (check / run / print-command) delegate to
# this executable.  Overridable via A2A_PYTHON_HARNESS so unit tests can
# substitute a bash mock without needing a real Python worker.
PYTHON_HARNESS="${A2A_PYTHON_HARNESS:-$ROOT/scripts/a2a_termux_native_worker.py}"

# Fixed by BROKER_URL validation in a2a_termux_native_worker.py: the local
# tunnel port must be 18790.  Remote endpoint is overridable in case a future
# fleet reshuffle moves the broker off :8787 on the remote host.
LOCAL_PORT=18790
REMOTE_ENDPOINT="${A2A_TUNNEL_REMOTE:-127.0.0.1:8787}"

LOCK_DIR="${A2A_SUPERVISOR_LOCK_DIR:-$HOME/.a2a}"
LOG_DIR="${A2A_SUPERVISOR_LOG_DIR:-$HOME/.hermes/logs}"
mkdir -p "$LOCK_DIR" "$LOG_DIR" 2>/dev/null || true

LOG="${A2A_SUPERVISOR_LOG:-$LOG_DIR/a2a-native-worker-supervisor.log}"
LOCK="${A2A_SUPERVISOR_LOCK:-$LOCK_DIR/a2a-native-worker-supervisor.lock}"
# PID file is separate from the flock file so `stop`/`status` (and the health
# checker) can identify the current supervisor without racing bash's fd
# buffering on the lock fd.
PIDFILE="$LOCK.pid"

log() {
    printf '%s [worker:%d] %s\n' "$(date '+%F %T%z')" "$$" "$*" >> "$LOG"
}

# Extract one value from an env file using the same quoting rules as the
# Python validator, so the shell layer sees exactly what the worker will.
extract_env_value() {
    local key="$1" file="$2"
    python3 - "$key" "$file" <<'PY'
import shlex, sys
key, path = sys.argv[1], sys.argv[2]
try:
    for raw in open(path, encoding='utf-8'):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        if '=' not in line:
            continue
        k, v = line.split('=', 1)
        if k.strip() != key:
            continue
        v = v.strip()
        if v.startswith(('"', "'")):
            try:
                parts = shlex.split(v, posix=True)
                if len(parts) == 1:
                    v = parts[0]
            except ValueError:
                pass
        print(v)
        break
except FileNotFoundError:
    pass
PY
}

# Kill orphan SSH tunnels bound to our local port (parent=1 == orphaned).
# Called at `supervise` start so a stuck ssh from a previous cycle can't
# hold the port and starve the fresh tunnel.
cleanup_orphans() {
    local port="$1" pid ppid
    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        ppid=$(awk '/^PPid:/ {print $2}' "/proc/$pid/status" 2>/dev/null || echo "")
        if [[ "$ppid" == "1" ]]; then
            log "cleanup orphan ssh pid=$pid"
            kill -TERM "$pid" 2>/dev/null || true
            sleep 1
            kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
        fi
    done < <(pgrep -f "ssh -N.*-L 127\.0\.0\.1:${port}:" 2>/dev/null || true)
}

# Walk pgrep -P recursively and kill each descendant, then the root.  The
# `pkill -f` approach used to fail here because a supervisor's SIGTERM only
# hit its immediate subshell, leaving the ssh grandchild orphaned.
kill_tree() {
    local root="$1"
    [[ -z "$root" || "$root" -le 1 ]] && return 0
    local child
    for child in $(pgrep -P "$root" 2>/dev/null || true); do
        kill_tree "$child"
    done
    kill -TERM "$root" 2>/dev/null || true
    local i
    for i in 1 2 3 4; do
        kill -0 "$root" 2>/dev/null || return 0
        sleep 1
    done
    kill -KILL "$root" 2>/dev/null || true
}

# Best-effort final sweep for any remaining ssh -N forwarding our local port.
# Runs after kill_tree in the EXIT trap as belt-and-suspenders.
sweep_lingering_ssh() {
    local port="$1" pid
    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        kill -KILL "$pid" 2>/dev/null || true
    done < <(pgrep -f "ssh -N.*-L 127\.0\.0\.1:${port}:" 2>/dev/null || true)
}

# Read the PID stashed by cmd_supervise.  Empty if no supervisor is running
# or the PID file is stale.  Avoids `fuser`, whose output format is not
# portable across Termux/Linux and BSDs.
current_supervisor_pid() {
    [[ -f "$PIDFILE" ]] || return 0
    local pid
    pid=$(head -n1 "$PIDFILE" 2>/dev/null | tr -d '[:space:]')
    [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] || return 0
    kill -0 "$pid" 2>/dev/null && printf '%s' "$pid" || return 0
}

# ---- subcommands ----

# The env-validation subcommands are pure delegations to the Python harness.
# We `exec` so the caller sees the harness's own rc + output; no set-flag
# surprises.  We call PYTHON_HARNESS directly (relying on its shebang) so
# tests can substitute a bash mock via A2A_PYTHON_HARNESS.
cmd_check()         { exec "$PYTHON_HARNESS" check         --env-file "$1"; }
cmd_print_command() { exec "$PYTHON_HARNESS" print-command --env-file "$1"; }
cmd_run()           { exec "$PYTHON_HARNESS" run           --env-file "$1"; }

# Internal helper for supervise: validate without exec-replacing our shell.
validate_env() {
    "$PYTHON_HARNESS" check --env-file "$1" >/dev/null || {
        log "env validation failed for $1"
        return 2
    }
}

cmd_supervise() {
    local env_file="$1"
    validate_env "$env_file" || return 2
    local ssh_target
    ssh_target=$(extract_env_value A2A_TUNNEL_SSH_TARGET "$env_file")
    if [[ -z "$ssh_target" ]]; then
        log "supervise: A2A_TUNNEL_SSH_TARGET missing from $env_file"
        printf 'ERROR: A2A_TUNNEL_SSH_TARGET must be set in %s\n' "$env_file" >&2
        return 2
    fi

    exec 200>"$LOCK"
    if ! flock -n 200; then
        log "supervise: another instance holds $LOCK; exiting"
        return 3
    fi
    # Stash our PID in a sibling file (separate from the flock fd) so tests,
    # `stop`, `status`, and the health checker can identify us reliably.  A
    # normal shell redirect here flushes on close.
    printf '%d\n' "$$" > "$PIDFILE"

    log "supervise START env=$env_file ssh_target=$ssh_target"
    cleanup_orphans "$LOCAL_PORT"

    (
        # Close the inherited flock fd so a lingering ssh child can't hold
        # the singleton lock after the main supervisor exits (the exact
        # orphan-tunnel failure mode this script is meant to prevent).
        exec 200>&-
        while true; do
            ssh -N \
                -o BatchMode=yes \
                -o ExitOnForwardFailure=yes \
                -o ServerAliveInterval=20 \
                -o ServerAliveCountMax=3 \
                -o StrictHostKeyChecking=accept-new \
                -L "127.0.0.1:${LOCAL_PORT}:${REMOTE_ENDPOINT}" \
                "$ssh_target"
            log "tunnel exited rc=$?; retry 5s"
            sleep 5
        done
    ) &
    local tunnel_pid=$!
    log "tunnel loop pid=$tunnel_pid"

    local worker_pid=0
    _cleanup() {
        log "supervise EXIT — tearing down tunnel_pid=$tunnel_pid worker_pid=$worker_pid"
        [[ "$worker_pid" -ne 0 ]] && kill_tree "$worker_pid"
        kill_tree "$tunnel_pid"
        sweep_lingering_ssh "$LOCAL_PORT"
        # Releasing fd 200 releases the flock.  The lock file may linger on
        # disk (harmless: next flock -n on the same file succeeds because the
        # old holder is gone).  Remove the PID file so `status`/`stop` don't
        # chase a dead PID.
        rm -f "$PIDFILE"
        exec 200>&-
    }
    trap _cleanup EXIT
    trap 'log "signal received"; exit 0' TERM INT HUP

    # Give the tunnel a moment to establish before the worker connects.
    sleep 3

    while true; do
        # Close the inherited flock fd in the worker child so a hung
        # worker.js can't extend the singleton lock beyond the supervisor.
        # We call the Python harness directly here (not `cmd_run`) so we can
        # background it — `cmd_run`'s exec would replace this shell.
        ( exec 200>&-; exec "$PYTHON_HARNESS" run --env-file "$env_file" ) &
        worker_pid=$!
        wait "$worker_pid"
        local rc=$?
        log "worker exited pid=$worker_pid rc=$rc; retry 8s"
        worker_pid=0
        sleep 8
    done
}

cmd_stop() {
    local sup_pid
    sup_pid=$(current_supervisor_pid)
    if [[ -z "$sup_pid" ]]; then
        echo "no supervisor holding $LOCK"
        return 0
    fi
    log "stop: sending SIGTERM to sup=$sup_pid"
    kill -TERM "$sup_pid" 2>/dev/null || true
    local i
    for i in $(seq 1 20); do
        kill -0 "$sup_pid" 2>/dev/null || {
            echo "stopped sup=$sup_pid"
            return 0
        }
        sleep 1
    done
    log "stop: SIGKILL fallback for sup=$sup_pid"
    kill -KILL "$sup_pid" 2>/dev/null || true
    sweep_lingering_ssh "$LOCAL_PORT"
    echo "killed sup=$sup_pid"
}

cmd_status() {
    local sup_pid
    sup_pid=$(current_supervisor_pid)
    printf 'supervisor: %s\n' "${sup_pid:-none}"
    printf 'lock: %s\n' "$LOCK"
    printf 'log: %s\n' "$LOG"
    printf 'tunnel: '
    if timeout 3 curl -sS -o /dev/null "http://127.0.0.1:${LOCAL_PORT}/livez" 2>/dev/null; then
        printf 'UP (127.0.0.1:%s -> %s)\n' "$LOCAL_PORT" "$REMOTE_ENDPOINT"
    else
        printf 'DOWN\n'
    fi
    printf 'workers (dist/worker.js): %s\n' "$(pgrep -c -f 'dist/worker.js' 2>/dev/null || echo 0)"
    printf 'orphan ssh on port %s (parent=1):\n' "$LOCAL_PORT"
    local pid ppid
    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        ppid=$(awk '/^PPid:/ {print $2}' "/proc/$pid/status" 2>/dev/null || echo "")
        [[ "$ppid" == "1" ]] && printf '  pid=%s\n' "$pid"
    done < <(pgrep -f "ssh -N.*-L 127\.0\.0\.1:${LOCAL_PORT}:" 2>/dev/null || true)
}

usage() {
    cat <<EOF >&2
Usage: $(basename "$0") <command> [--env-file <path>]

Env-validation commands (delegate to a2a_termux_native_worker.py):
  check         --env-file <path>   Validate the env file and print a summary.
  print-command --env-file <path>   Print the exact native node worker.js command.
  run           --env-file <path>   Validate, then exec native node worker.js.

Supervisor commands (singleton SSH tunnel + worker respawn):
  supervise     --env-file <path>
      Run the singleton supervisor.  Fails with rc=3 if another supervisor
      already holds the lock.
  stop
      SIGTERM the supervisor holding the lock, verify shutdown, KILL fallback.
  status
      Print supervisor / tunnel / worker state (read-only, no side effects).

Health check (separate script — call directly, or from cron):
  scripts/a2a-termux-native-worker-health.sh --env-file <path> [--self-heal]

Env keys (in --env-file, on top of what \`check\`/\`run\` need):
  A2A_TUNNEL_SSH_TARGET   SSH host alias for the remote broker.  Required for
                          supervise.  Uses ~/.ssh/config as usual.
  A2A_TUNNEL_REMOTE       Optional remote endpoint; defaults to 127.0.0.1:8787.

Environment overrides (supervisor paths):
  A2A_SUPERVISOR_LOCK_DIR   Default \$HOME/.a2a
  A2A_SUPERVISOR_LOG_DIR    Default \$HOME/.hermes/logs
  A2A_SUPERVISOR_LOCK       Full lock file path (overrides LOCK_DIR)
  A2A_SUPERVISOR_LOG        Full log file path (overrides LOG_DIR)
EOF
    return 2
}

main() {
    local cmd="${1:-}"
    [[ -z "$cmd" ]] && { usage; return 2; }
    shift
    local env_file=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --env-file) env_file="${2:-}"; shift 2 ;;
            -h|--help)  usage; return 2 ;;
            *)          echo "unknown arg: $1" >&2; usage ;;
        esac
    done
    case "$cmd" in
        check|print-command|run)
            [[ -n "$env_file" ]] || { echo "--env-file required for $cmd" >&2; return 2; }
            local fn="${cmd//-/_}"
            "cmd_$fn" "$env_file"
            ;;
        supervise)
            [[ -n "$env_file" ]] || { echo "--env-file required for $cmd" >&2; return 2; }
            cmd_supervise "$env_file"
            ;;
        stop|status)
            "cmd_$cmd"
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "unknown command: $cmd" >&2
            usage
            ;;
    esac
}

# Only run main() when executed, not when sourced (so unit tests can pull in
# helpers like kill_tree without triggering the dispatcher).
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
