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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# The env-validation subcommands (check / run / print-command) delegate to
# this executable.  Overridable via A2A_PYTHON_HARNESS so unit tests can
# substitute a bash mock without needing a real Python worker.
PYTHON_HARNESS="${A2A_PYTHON_HARNESS:-$ROOT/scripts/a2a_termux_native_worker.py}"
# shellcheck source=scripts/lib/a2a-supervisor-identity.sh
. "$SCRIPT_DIR/lib/a2a-supervisor-identity.sh"

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

# Print SSH PIDs whose argv exactly matches this supervisor's forward, remote
# endpoint, and target.  Never use a same-port pgrep as a kill selector: another
# service may legitimately own a different forward on the same local port in a
# separate network namespace.
owned_tunnel_records() {
    local port="$1" remote="$2" target="$3"
    python3 - "$port" "$remote" "$target" <<'PY'
import glob
import os
import sys

forward = f"127.0.0.1:{sys.argv[1]}:{sys.argv[2]}"
target = sys.argv[3]
for path in glob.glob("/proc/[0-9]*/cmdline"):
    try:
        argv = [os.fsdecode(arg) for arg in open(path, "rb").read().split(b"\0") if arg]
    except OSError:
        continue
    if not argv or os.path.basename(argv[0]) != "ssh" or "-N" not in argv:
        continue
    owns_forward = any(
        (arg == "-L" and index + 1 < len(argv) and argv[index + 1] == forward)
        or arg == "-L" + forward
        for index, arg in enumerate(argv)
    )
    if owns_forward and argv[-1] == target:
        try:
            fields = open(path.replace("cmdline", "stat"), encoding="ascii").read().rsplit(") ", 1)[1].split()
            start_token = fields[19]
        except (OSError, IndexError):
            continue
        print(path.split("/")[2], start_token)
PY
}

tunnel_identity_matches() {
    local expected_pid="$1" expected_token="$2" port="$3" remote="$4" target="$5" pid token
    a2a_process_identity_matches "$expected_pid" "$expected_token" || return 1
    while read -r pid token; do
        [[ "$pid" == "$expected_pid" && "$token" == "$expected_token" ]] && return 0
    done < <(owned_tunnel_records "$port" "$remote" "$target")
    return 1
}

# Kill orphan SSH tunnels owned by this exact supervisor configuration
# (parent=1 == orphaned).
# Called at `supervise` start so a stuck ssh from a previous cycle can't
# hold the port and starve the fresh tunnel.
cleanup_orphans() {
    local port="$1" remote="$2" target="$3" pid token ppid
    while read -r pid token; do
        [[ -z "$pid" ]] && continue
        ppid=$(a2a_process_parent_pid "$pid" 2>/dev/null || true)
        if [[ "$ppid" == "1" ]] && \
                tunnel_identity_matches "$pid" "$token" "$port" "$remote" "$target"; then
            log "cleanup orphan ssh pid=$pid"
            kill -TERM "$pid" 2>/dev/null || true
            sleep 1
            tunnel_identity_matches "$pid" "$token" "$port" "$remote" "$target" && \
                kill -KILL "$pid" 2>/dev/null || true
        fi
    done < <(owned_tunnel_records "$port" "$remote" "$target")
}

# Walk pgrep -P recursively and kill each descendant, then the root.  The
# `pkill -f` approach used to fail here because a supervisor's SIGTERM only
# hit its immediate subshell, leaving the ssh grandchild orphaned.
kill_tree() {
    local root="$1" expected_parent="${2:-}" token current_parent
    [[ -z "$root" || "$root" -le 1 ]] && return 0
    token=$(a2a_process_start_token "$root" 2>/dev/null) || return 0
    if [[ -n "$expected_parent" ]]; then
        current_parent=$(a2a_process_parent_pid "$root" 2>/dev/null || true)
        [[ "$current_parent" == "$expected_parent" ]] || return 0
    fi
    local child
    for child in $(pgrep -P "$root" 2>/dev/null || true); do
        kill_tree "$child" "$root"
    done
    a2a_process_identity_matches "$root" "$token" || return 0
    kill -TERM "$root" 2>/dev/null || true
    local i
    for i in 1 2 3 4; do
        a2a_process_identity_matches "$root" "$token" || return 0
        sleep 1
    done
    a2a_process_identity_matches "$root" "$token" && kill -KILL "$root" 2>/dev/null || true
}

# Best-effort final sweep for any remaining ssh -N forwarding our exact owned
# endpoint.  Runs after kill_tree in the EXIT trap as belt-and-suspenders.
sweep_lingering_ssh() {
    local port="$1" remote="$2" target="$3" pid token
    while read -r pid token; do
        [[ -z "$pid" ]] && continue
        tunnel_identity_matches "$pid" "$token" "$port" "$remote" "$target" && \
            kill -KILL "$pid" 2>/dev/null || true
    done < <(owned_tunnel_records "$port" "$remote" "$target")
}

# Read the PID identity stashed by cmd_supervise.  Empty if no canonical
# supervisor is running or the PID/start-token record is stale.
current_supervisor_record() {
    a2a_current_supervisor_record "$PIDFILE" \
        "$SCRIPT_DIR/a2a-termux-native-worker.sh" "$LOCK"
}

current_supervisor_pid() {
    local record pid _token
    record=$(current_supervisor_record)
    [[ -n "$record" ]] || return 0
    read -r pid _token <<<"$record"
    printf '%s' "$pid"
}

# True on a Termux runtime.  Used to gate env sanitization so Linux CI and any
# non-Termux fleet path are untouched.
is_termux() { [[ -n "${TERMUX_VERSION:-}" || "${PREFIX:-}" == *com.termux* ]]; }

# Scrub a poisoned environment before spawning glibc children (the tunnel ssh
# and the worker node).  Some launch paths — a Claude/glibc session, a foreign
# trigger, a bare manual invocation — leak an LD_LIBRARY_PATH pointing at glibc
# libraries incompatible with Termux's bionic binaries.  Inheriting it crashes
# the tunnel ssh (rc=139) and the worker node (rc=103) in a tight retry loop,
# the recurring root cause behind Wiki ND-1236 (2026-07-04 / -07-17 / -07-30).
# Historically this was scrubbed only in the Termux:Boot launcher and the health
# cron, so every other launch path reintroduced the crash loop; scrub here so
# supervise/run are robust regardless of caller env.  The Termux exec preload
# MUST survive — dropping LD_PRELOAD entirely broke exec (rc=2) during the
# 2026-07-17 remediation — so we re-assert it rather than clear it.  No-op off
# Termux.
sanitize_termux_env() {
    is_termux || return 0
    if [[ -n "${LD_LIBRARY_PATH:-}" ]]; then
        log "sanitize env: clearing inherited LD_LIBRARY_PATH"
        unset LD_LIBRARY_PATH
    fi
    local preload="${PREFIX:-}/lib/libtermux-exec-ld-preload.so"
    if [[ -f "$preload" && ":${LD_PRELOAD:-}:" != *":$preload:"* ]]; then
        export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$preload"
    fi
}

# ---- subcommands ----

# The env-validation subcommands are pure delegations to the Python harness.
# We `exec` so the caller sees the harness's own rc + output; no set-flag
# surprises.
#
# harness_exec dispatches on whether A2A_PYTHON_HARNESS was overridden:
#   - unset (canonical production path): PYTHON_HARNESS is the checked-in
#     a2a_termux_native_worker.py.  We invoke it via an explicit `python3`
#     rather than relying on its shebang, because an absolute `env` shebang
#     path is not portable across OSes — Termux's real filesystem root has
#     no /usr, so `#!/usr/bin/env python3` fails there (exit 126, "bad
#     interpreter"), while a Termux-prefixed shebang fails the same way on
#     every non-Termux box (standard Linux/CI/macOS). `python3` resolved
#     via PATH works on both.
#   - set (unit tests substituting a bash mock via A2A_PYTHON_HARNESS): exec
#     it directly so the mock's own shebang picks its interpreter.
harness_exec() {
    if [[ -n "${A2A_PYTHON_HARNESS:-}" ]]; then
        exec "$PYTHON_HARNESS" "$@"
    else
        exec python3 "$PYTHON_HARNESS" "$@"
    fi
}
cmd_check()         { harness_exec check         --env-file "$1"; }
cmd_print_command() { harness_exec print-command --env-file "$1"; }
cmd_run()           { sanitize_termux_env; harness_exec run --env-file "$1"; }

# Internal helper for supervise: validate without exec-replacing our shell.
validate_env() {
    if [[ -n "${A2A_PYTHON_HARNESS:-}" ]]; then
        "$PYTHON_HARNESS" check --env-file "$1" >/dev/null
    else
        python3 "$PYTHON_HARNESS" check --env-file "$1" >/dev/null
    fi || {
        log "env validation failed for $1"
        return 2
    }
}

cmd_supervise() {
    local env_file="$1"
    # Scrub a poisoned LD_LIBRARY_PATH before the Python validation and before
    # spawning the tunnel ssh / worker node, both of which inherit this env
    # (Wiki ND-1236 crash-loop root cause).
    sanitize_termux_env
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
    # Stash PID + kernel start token in a sibling file so a recycled numeric
    # PID can never make stop/status target an unrelated process.
    if ! a2a_write_supervisor_record "$PIDFILE" "$$"; then
        log "supervise: failed to publish PID identity in $PIDFILE"
        return 2
    fi
    local supervisor_token
    supervisor_token=$(a2a_process_start_token "$$") || return 2

    log "supervise START env=$env_file ssh_target=$ssh_target"
    cleanup_orphans "$LOCAL_PORT" "$REMOTE_ENDPOINT" "$ssh_target"

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
        sweep_lingering_ssh "$LOCAL_PORT" "$REMOTE_ENDPOINT" "$ssh_target"
        # Releasing fd 200 releases the flock.  The lock file may linger on
        # disk (harmless: next flock -n on the same file succeeds because the
        # old holder is gone).  Remove the PID file so `status`/`stop` don't
        # chase a dead PID.
        local live_record
        live_record=$(current_supervisor_record)
        [[ "$live_record" == "$$ $supervisor_token" ]] && rm -f "$PIDFILE"
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
        ( exec 200>&-; harness_exec run --env-file "$env_file" ) &
        worker_pid=$!
        wait "$worker_pid"
        local rc=$?
        log "worker exited pid=$worker_pid rc=$rc; retry 8s"
        worker_pid=0
        sleep 8
    done
}

cmd_stop() {
    local record sup_pid sup_token env_file ssh_target
    record=$(current_supervisor_record)
    if [[ -z "$record" ]]; then
        echo "no supervisor holding $LOCK"
        return 0
    fi
    read -r sup_pid sup_token <<<"$record"
    env_file=$(a2a_supervisor_env_file "$sup_pid" 2>/dev/null || true)
    ssh_target=""
    [[ -n "$env_file" ]] && ssh_target=$(extract_env_value A2A_TUNNEL_SSH_TARGET "$env_file")

    # Re-check immediately before signaling to close the read/signal race.
    if ! a2a_validate_supervisor_identity "$sup_pid" "$sup_token" \
            "$SCRIPT_DIR/a2a-termux-native-worker.sh" "$LOCK" >/dev/null; then
        echo "no canonical supervisor holding $LOCK"
        return 0
    fi
    log "stop: sending SIGTERM to sup=$sup_pid"
    kill -TERM "$sup_pid" 2>/dev/null || true
    local i
    for ((i = 0; i < 20; i++)); do
        if ! a2a_validate_supervisor_identity "$sup_pid" "$sup_token" \
                "$SCRIPT_DIR/a2a-termux-native-worker.sh" "$LOCK" >/dev/null; then
            echo "stopped sup=$sup_pid"
            return 0
        fi
        sleep 1
    done
    # Signal only if this is still the same process instance and command.
    if a2a_validate_supervisor_identity "$sup_pid" "$sup_token" \
            "$SCRIPT_DIR/a2a-termux-native-worker.sh" "$LOCK" >/dev/null; then
        log "stop: SIGKILL fallback for sup=$sup_pid"
        kill -KILL "$sup_pid" 2>/dev/null || true
    fi
    [[ -n "$ssh_target" ]] && \
        sweep_lingering_ssh "$LOCAL_PORT" "$REMOTE_ENDPOINT" "$ssh_target"
    echo "killed sup=$sup_pid"
}

cmd_status() {
    local sup_pid
    sup_pid=$(current_supervisor_pid)
    printf 'supervisor: %s\n' "${sup_pid:-none}"
    printf 'lock: %s\n' "$LOCK"
    printf 'log: %s\n' "$LOG"
    printf 'tunnel: '
    if timeout 3 curl -fsS -o /dev/null "http://127.0.0.1:${LOCAL_PORT}/livez" 2>/dev/null; then
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
    # `read` returns 1 on EOF, so the loop above (and thus this function)
    # would otherwise report failure whenever there are zero orphan ssh
    # processes — the normal, healthy case. `status` is read-only reporting;
    # its exit code must reflect "ran successfully", not "found something".
    return 0
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
            --env-file)
                # `shift 2` with one arg left is a no-op under bash (status 1,
                # params untouched), and the unknown-arg branch without a
                # return re-dispatches the same token forever — both spun this
                # loop unbounded, flooding the supervisor log.
                [[ $# -ge 2 ]] || { echo "--env-file requires a value" >&2; usage; return 2; }
                env_file="$2"; shift 2 ;;
            -h|--help)  usage; return 2 ;;
            *)          echo "unknown arg: $1" >&2; usage; return 2 ;;
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
