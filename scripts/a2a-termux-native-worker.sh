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
#   restart-worker --if-idle       SIGTERM only an idle dist/worker.js; the
#                                  supervise loop relaunches it (#2791).
#   restart-supervisor --if-idle   Idle-gated stop + respawn of the whole
#                                  supervise loop (#2791, opt-in path).
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
# The health check spawns supervisors with stdout on its own log, so the
# worker's "registered with" line lands there (restart verification reads it).
HEALTH_LOG="${A2A_SUPERVISOR_HEALTH_LOG:-$LOG_DIR/a2a-native-worker-health.log}"
HEALTH_SCRIPT="$SCRIPT_DIR/a2a-termux-native-worker-health.sh"
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

# ---- tunnel readiness gate (#1923) ----

# Probe the broker health endpoint through the local tunnel exactly once.
# Shared by `status` (read-only reporting) and the supervise spawn gates, so
# every /livez check uses the same timeout and flags.
tunnel_ready() {
    timeout 3 curl -fsS -o /dev/null "http://127.0.0.1:${LOCAL_PORT}/livez" 2>/dev/null
}

# Bounded wait for the SSH tunnel to start forwarding before the worker dials
# the broker through 127.0.0.1:$LOCAL_PORT (#1923).  Spawning the worker
# against a not-yet-forwarding tunnel guarantees one failed boot and another
# retry-loop iteration; polling /livez instead of sleeping a fixed number of
# seconds means the worker spawns as soon as the tunnel is actually up.
#
# Fail-open by design: if /livez never answers within A2A_TUNNEL_GATE_TIMEOUT
# seconds we log the dedicated defer label and return nonzero, and the caller
# proceeds anyway.  Keeping the loop moving is the supervisor's job — turning
# a down tunnel into a wedged supervisor would just trade the old failure mode
# for a new one, and escalation for a persistently down tunnel is the health
# checker's contract, not the spawn gate's.
wait_for_tunnel() {
    local gate_timeout="${A2A_TUNNEL_GATE_TIMEOUT:-30}"
    local gate_interval="${A2A_TUNNEL_GATE_INTERVAL:-0.5}"
    local deadline=$(( SECONDS + gate_timeout ))
    while true; do
        if tunnel_ready; then
            return 0
        fi
        (( SECONDS >= deadline )) && break
        sleep "$gate_interval"
    done
    log "tunnel-gate: tunnel not ready; deferring worker start" \
        "(waited ${gate_timeout}s, fail-open)"
    return 1
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

    # Give the tunnel a bounded chance to establish before the worker
    # connects (#1923).  The gate polls /livez so the worker spawns as soon as
    # the tunnel actually forwards; on timeout it fails open and we still
    # spawn, because the retry loop below owns recovery.
    wait_for_tunnel || true

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
        # Same bounded gate before every respawn (#1923): when the broker or
        # tunnel is down, this keeps the 8s retry loop from becoming a tight
        # worker-boot crash loop — each iteration waits for /livez before
        # spending a boot.  Fail-open keeps the loop alive.
        wait_for_tunnel || true
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
    if tunnel_ready; then
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

# ---- idle-gated restart (#2791 stage 2b) ----
#
# The native worker is no systemd unit, so self-update's #2138 cgroup gate and
# restart never reach it. These two commands are the Termux equivalent:
#
#   restart-worker --if-idle      SIGTERM only the supervisor's dist/worker.js
#       child. The supervise loop relaunches it (after its 8 s retry delay)
#       from whatever is installed now; the tunnel stays up. This is the deploy
#       side's restart (scripts/a2a-termux-native-worker-deploy.sh).
#   restart-supervisor --if-idle  stop the supervisor (tunnel + worker) and
#       respawn it through the health check's --self-heal path. Needed when the
#       supervise script or its lib changed. The tunnel drops for a moment, so
#       the broker briefly sees the worker offline.
#
# Idle keeps #2138's meaning: the worker spawns each task's handler as a child
# process, so any descendant of the worker pid younger than BUSY_MAX means a
# task is running. A descendant older than BUSY_MAX counts as hung and does not
# block (it is reported, never killed). Unlike #2138 the gate fails CLOSED: when
# the worker's process tree cannot be read, nothing is signaled. There is no
# unconditional mode — a busy worker is never killed.
#
# After the signal, success needs all of: a new worker child under the expected
# supervisor, a fresh "registered with" line in the supervisor or health log,
# that worker still alive after the settle window, and the health check
# reporting sup_count=1 workers=1 tunnel=UP rc=0.
#
#   A2A_NATIVE_WORKER_BUSY_MAX_SECONDS   handler age cap; default
#                                        $CCC_SELF_UPDATE_BUSY_MAX_SECONDS or 1800
#   A2A_NATIVE_WORKER_RESTART_TIMEOUT    seconds to wait for registration (90)
#   A2A_NATIVE_WORKER_RESTART_SETTLE     seconds the new worker must survive (20)
#   A2A_NATIVE_WORKER_REGISTERED_PATTERN fixed string proving registration
#                                        (default "registered with")
#   A2A_NATIVE_WORKER_PROC_ROOT          proc tree the idle gate reads (/proc;
#                                        tests point it at a mock tree)
#
# Exit codes: 0 restarted + verified (--dry-run: would restart), 2 usage/env,
# 3 no canonical supervisor or not exactly one worker, 10 busy (nothing
# signaled), 11 idle state unknown (nothing signaled), 30 signaled but the
# restart was not verified.
RESTART_RC_NO_TARGET=3
RESTART_RC_BUSY=10
RESTART_RC_UNKNOWN=11
RESTART_RC_UNVERIFIED=30

restart_say() { # <cmd> <message...>: success line on stdout + supervisor log
    local cmd="$1"; shift
    printf '%s: %s\n' "$cmd" "$*"
    log "$cmd: $*"
}

restart_fail() { # <cmd> <message...>: failure line on stderr + supervisor log
    local cmd="$1"; shift
    printf '%s: %s\n' "$cmd" "$*" >&2
    log "$cmd: $*"
}

valid_restart_uint() { # <name> <value> <min> <max>
    if [[ "$2" =~ ^[0-9]{1,6}$ ]] && (( 10#$2 >= $3 && 10#$2 <= $4 )); then
        return 0
    fi
    printf '%s must be an integer in %s..%s (got %q)\n' "$1" "$3" "$4" "$2" >&2
    return 1
}

# Validate the restart tuning once per invocation.
restart_settings() {
    BUSY_MAX="${A2A_NATIVE_WORKER_BUSY_MAX_SECONDS:-${CCC_SELF_UPDATE_BUSY_MAX_SECONDS:-1800}}"
    RESTART_TIMEOUT="${A2A_NATIVE_WORKER_RESTART_TIMEOUT:-90}"
    RESTART_SETTLE="${A2A_NATIVE_WORKER_RESTART_SETTLE:-20}"
    valid_restart_uint A2A_NATIVE_WORKER_BUSY_MAX_SECONDS "$BUSY_MAX" 1 86400 &&
        valid_restart_uint A2A_NATIVE_WORKER_RESTART_TIMEOUT "$RESTART_TIMEOUT" 1 3600 &&
        valid_restart_uint A2A_NATIVE_WORKER_RESTART_SETTLE "$RESTART_SETTLE" 0 600
}

# The worker.js the supervisor's `run` executes (same rule as the Python
# harness: A2A_WORKER_SCRIPT, else $A2A_WORKER_ROOT/dist/worker.js).
native_worker_script() { # <env_file>
    local script root
    script=$(extract_env_value A2A_WORKER_SCRIPT "$1")
    if [[ -z "$script" ]]; then
        root=$(extract_env_value A2A_WORKER_ROOT "$1")
        [[ -n "$root" ]] || return 1
        script="$root/dist/worker.js"
    fi
    printf '%s' "${script/#\~/$HOME}"
}

# Print "PID START_TOKEN" for every direct child of <sup_pid> whose argv names
# <worker_script> (compared as real paths, never as a regex).
supervisor_worker_records() { # <sup_pid> <worker_script>
    python3 - "$1" "$2" <<'PY'
import glob
import os
import sys

sup, expected = sys.argv[1], os.path.realpath(sys.argv[2])
for proc in glob.glob("/proc/[0-9]*"):
    try:
        fields = open(proc + "/stat", encoding="ascii", errors="replace").read().rsplit(")", 1)[1].split()
        raw = open(proc + "/cmdline", "rb").read()
    except (OSError, IndexError):
        continue
    if len(fields) < 20 or fields[1] != sup:
        continue
    argv = [os.fsdecode(arg) for arg in raw.split(b"\0") if arg]
    if any(os.path.realpath(arg) == expected for arg in argv[1:]):
        print(proc.rsplit("/", 1)[1], fields[19])
PY
}

# Idle gate (#2138 semantics on a process tree instead of a cgroup). Prints a
# one-line state and returns 0 idle, 10 busy, anything else unknown. Process
# age uses CLOCK_BOOTTIME like the stale check: Android denies /proc/uptime.
worker_busy_probe() { # <worker_pid> <busy_max_seconds>
    python3 - "$1" "$2" "${A2A_NATIVE_WORKER_PROC_ROOT:-/proc}" <<'PY'
import glob
import os
import sys
import time

worker, busy_max, root = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]


def boot_seconds():
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return float(open("/proc/uptime", encoding="ascii").read().split()[0])


def read_stat(path):
    fields = open(path, encoding="ascii", errors="replace").read().rsplit(")", 1)[1].split()
    return int(fields[1]), int(fields[19])  # ppid, starttime (proc(5) field 22)


try:
    tick = os.sysconf("SC_CLK_TCK")
    now = boot_seconds()
    read_stat(os.path.join(root, str(worker), "stat"))
except Exception:
    print("state=unknown reason=worker-stat-unreadable")
    sys.exit(11)

children, starts = {}, {}
for path in glob.glob(os.path.join(root, "[0-9]*", "stat")):
    try:
        pid = int(os.path.basename(os.path.dirname(path)))
        ppid, start = read_stat(path)
    except (OSError, IndexError, ValueError):
        continue  # exited between listing and reading
    children.setdefault(ppid, []).append(pid)
    starts[pid] = start

seen, stack, ages = {worker}, list(children.get(worker, [])), []
while stack:
    pid = stack.pop()
    if pid in seen:
        continue
    seen.add(pid)
    ages.append(now - starts[pid] / tick)
    stack.extend(children.get(pid, []))

live = [age for age in ages if age < busy_max]
hung = [age for age in ages if age >= busy_max]
suffix = " hung=%d hung_oldest=%ds" % (len(hung), int(max(hung))) if hung else ""
if live:
    print("state=busy children=%d youngest=%ds%s" % (len(live), int(min(live)), suffix))
    sys.exit(10)
print("state=idle children=0%s" % suffix)
PY
}

log_size() { # <file>: byte size, 0 when absent
    local n
    n=$(wc -c < "$1" 2>/dev/null) || n=0
    n="${n//[!0-9]/}"
    printf '%s' "${n:-0}"
}

log_tail_since() { # <file> <offset>: bytes appended after offset (all if rotated)
    [[ -r "$1" ]] || return 0
    if (( $(log_size "$1") < $2 )); then
        cat "$1"
    else
        tail -c +"$(( $2 + 1 ))" "$1"
    fi
}

registered_since() { # <supervisor_log_offset> <health_log_offset>
    local pattern="${A2A_NATIVE_WORKER_REGISTERED_PATTERN:-registered with}"
    { log_tail_since "$LOG" "$1"; log_tail_since "$HEALTH_LOG" "$2"; } |
        grep -qF -- "$pattern"
}

# Health check verdict in the shape operators already read from its log line.
health_summary() { # <env_file>: prints "sup_count=N workers=N tunnel=X rc=R"
    local out
    out=$(bash "$HEALTH_SCRIPT" --env-file "$1" --no-self-heal --json --quiet 2>/dev/null)
    python3 - "$out" <<'PY'
import json
import sys

line = next((l for l in reversed(sys.argv[1].splitlines()) if l.startswith("{")), "")
try:
    d = json.loads(line)
except ValueError:
    print("health=unparsable")
    sys.exit(1)
got = (d.get("supervisor_count"), d.get("workers"), d.get("tunnel"), d.get("rc"))
print("sup_count=%s workers=%s tunnel=%s rc=%s" % got)
sys.exit(0 if got == (1, 1, "UP", 0) else 1)
PY
}

# Shared lookup: canonical supervisor, its env file, worker script and the
# worker children. Sets RT_SUP ("PID TOKEN"), RT_ENV, RT_SCRIPT, RT_WORKERS.
restart_prepare() { # <cmd> <env_file|''>
    local cmd="$1"
    RT_SUP=$(current_supervisor_record)
    if [[ -z "$RT_SUP" ]]; then
        restart_fail "$cmd" "result=no-supervisor lock=$LOCK — nothing restarted (the health check's --self-heal starts one)"
        return "$RESTART_RC_NO_TARGET"
    fi
    RT_ENV="$2"
    [[ -n "$RT_ENV" ]] || RT_ENV=$(a2a_supervisor_env_file "${RT_SUP%% *}" 2>/dev/null || true)
    if [[ -z "$RT_ENV" || ! -f "$RT_ENV" ]]; then
        restart_fail "$cmd" "result=no-env-file sup=${RT_SUP%% *} — pass --env-file"
        return 2
    fi
    if ! RT_SCRIPT=$(native_worker_script "$RT_ENV"); then
        restart_fail "$cmd" "result=no-worker-root env=$RT_ENV — set A2A_WORKER_ROOT"
        return 2
    fi
    mapfile -t RT_WORKERS < <(supervisor_worker_records "${RT_SUP%% *}" "$RT_SCRIPT")
    return 0
}

restart_gate() { # <cmd> <worker_pid>: 0 idle; sets RT_PROBE
    local rc
    RT_PROBE=$(worker_busy_probe "$2" "$BUSY_MAX"); rc=$?
    case "$rc" in
        0) return 0 ;;
        10)
            restart_fail "$1" "result=busy worker_pid=$2 $RT_PROBE busy_max=${BUSY_MAX}s — a task is running; not restarted, retry later"
            return "$RESTART_RC_BUSY" ;;
        *)
            restart_fail "$1" "result=unknown worker_pid=$2 ${RT_PROBE:-state=unknown} — idle state unreadable; not restarted (fail-closed)"
            return "$RESTART_RC_UNKNOWN" ;;
    esac
}

# Wait for the relaunched worker and prove it serves. mode=worker keeps the
# same supervisor; mode=supervisor expects a new one. Sets VERIFIED_WORKER and
# VERIFIED_HEALTH on success.
verify_restart() { # <cmd> <mode> <old_sup> <old_worker> <script> <env_file> <sup_off> <health_off>
    local cmd="$1" mode="$2" old_sup="$3" old_worker="$4" script="$5" env_file="$6"
    local sup_off="$7" h_off="$8" deadline sup="" worker="" summary
    local -a records
    deadline=$(( SECONDS + RESTART_TIMEOUT ))
    while (( SECONDS <= deadline )); do
        sup=$(current_supervisor_record)
        if [[ "$mode" == worker && "$sup" != "$old_sup" ]]; then
            restart_fail "$cmd" "result=unverified reason=supervisor-changed old_sup=${old_sup%% *} now=${sup:-none}"
            return "$RESTART_RC_UNVERIFIED"
        fi
        if [[ -n "$sup" && ( "$mode" == worker || "$sup" != "$old_sup" ) ]]; then
            mapfile -t records < <(supervisor_worker_records "${sup%% *}" "$script")
            if (( ${#records[@]} == 1 )) && [[ "${records[0]}" != "$old_worker" ]] &&
                    registered_since "$sup_off" "$h_off"; then
                worker="${records[0]}"
                break
            fi
        fi
        sleep 1
    done
    if [[ -z "$worker" ]]; then
        restart_fail "$cmd" "result=unverified reason=no-registered-worker timeout=${RESTART_TIMEOUT}s sup=${sup:-none}"
        return "$RESTART_RC_UNVERIFIED"
    fi
    sleep "$RESTART_SETTLE"
    if [[ "$(current_supervisor_record)" != "$sup" ]] ||
            ! a2a_process_identity_matches "${worker%% *}" "${worker##* }"; then
        restart_fail "$cmd" "result=unverified reason=worker-not-stable new_pid=${worker%% *} settle=${RESTART_SETTLE}s"
        return "$RESTART_RC_UNVERIFIED"
    fi
    if ! summary=$(health_summary "$env_file"); then
        restart_fail "$cmd" "result=unverified reason=health $summary"
        return "$RESTART_RC_UNVERIFIED"
    fi
    VERIFIED_WORKER="$worker"
    VERIFIED_HEALTH="$summary"
}

cmd_restart_worker() { # <env_file|''> <dry_run 0|1>
    local cmd=restart-worker dry_run="$2" wpid wtoken sup_off h_off
    restart_prepare "$cmd" "$1" || return $?
    if (( ${#RT_WORKERS[@]} != 1 )); then
        restart_fail "$cmd" "result=no-worker workers=${#RT_WORKERS[@]} sup=${RT_SUP%% *} script=$RT_SCRIPT — need exactly one worker child; not restarted"
        return "$RESTART_RC_NO_TARGET"
    fi
    wpid="${RT_WORKERS[0]%% *}"; wtoken="${RT_WORKERS[0]##* }"
    restart_gate "$cmd" "$wpid" || return $?
    if (( dry_run )); then
        restart_say "$cmd" "result=would-restart worker_pid=$wpid sup=${RT_SUP%% *} $RT_PROBE (dry-run: nothing signaled)"
        return 0
    fi
    sup_off=$(log_size "$LOG"); h_off=$(log_size "$HEALTH_LOG")
    # Re-check right before the signal: a task may have started since the gate.
    if ! a2a_process_identity_matches "$wpid" "$wtoken"; then
        restart_fail "$cmd" "result=no-worker reason=worker-exited worker_pid=$wpid — not restarted"
        return "$RESTART_RC_NO_TARGET"
    fi
    restart_gate "$cmd" "$wpid" || return $?
    log "$cmd: SIGTERM worker pid=$wpid sup=${RT_SUP%% *} $RT_PROBE"
    kill -TERM "$wpid" 2>/dev/null || {
        restart_fail "$cmd" "result=no-worker reason=signal-failed worker_pid=$wpid"
        return "$RESTART_RC_NO_TARGET"
    }
    verify_restart "$cmd" worker "$RT_SUP" "${RT_WORKERS[0]}" "$RT_SCRIPT" "$RT_ENV" \
        "$sup_off" "$h_off" || return $?
    restart_say "$cmd" "result=ok old_pid=$wpid new_pid=${VERIFIED_WORKER%% *} sup=${RT_SUP%% *} registered=yes $VERIFIED_HEALTH"
}

cmd_restart_supervisor() { # <env_file|''> <dry_run 0|1>
    local cmd=restart-supervisor dry_run="$2" old_worker="" wpid="" sup_off h_off hrc
    restart_prepare "$cmd" "$1" || return $?
    if (( ${#RT_WORKERS[@]} > 1 )); then
        restart_fail "$cmd" "result=no-worker workers=${#RT_WORKERS[@]} sup=${RT_SUP%% *} — more than one worker child; not restarted"
        return "$RESTART_RC_NO_TARGET"
    fi
    if (( ${#RT_WORKERS[@]} == 1 )); then
        old_worker="${RT_WORKERS[0]}"; wpid="${old_worker%% *}"
        restart_gate "$cmd" "$wpid" || return $?
    else
        RT_PROBE="state=idle worker=none"  # respawn gap: no task can be running
    fi
    if (( dry_run )); then
        restart_say "$cmd" "result=would-restart sup=${RT_SUP%% *} worker_pid=${wpid:-none} $RT_PROBE (dry-run: nothing signaled)"
        return 0
    fi
    sup_off=$(log_size "$LOG"); h_off=$(log_size "$HEALTH_LOG")
    if [[ -n "$wpid" ]] && a2a_process_identity_matches "$wpid" "${old_worker##* }"; then
        restart_gate "$cmd" "$wpid" || return $?
    fi
    log "$cmd: stopping sup=${RT_SUP%% *} worker_pid=${wpid:-none} $RT_PROBE"
    cmd_stop >/dev/null 2>&1
    if a2a_validate_supervisor_identity "${RT_SUP%% *}" "${RT_SUP##* }" \
            "$SCRIPT_DIR/a2a-termux-native-worker.sh" "$LOCK" >/dev/null 2>&1; then
        restart_fail "$cmd" "result=unverified reason=old-supervisor-still-running sup=${RT_SUP%% *}"
        return "$RESTART_RC_UNVERIFIED"
    fi
    # Respawn through the same path cron uses, so the new supervisor's stdout
    # (and the worker's "registered with") goes to the health log as usual.
    bash "$HEALTH_SCRIPT" --env-file "$RT_ENV" --self-heal --quiet >/dev/null 2>&1; hrc=$?
    (( hrc == 0 )) || log "$cmd: health --self-heal rc=$hrc; verifying anyway"
    verify_restart "$cmd" supervisor "$RT_SUP" "$old_worker" "$RT_SCRIPT" "$RT_ENV" \
        "$sup_off" "$h_off" || return $?
    restart_say "$cmd" "result=ok old_sup=${RT_SUP%% *} new_sup=$(current_supervisor_pid) worker_pid=${VERIFIED_WORKER%% *} registered=yes $VERIFIED_HEALTH"
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

Idle-gated restart (#2791; --if-idle is mandatory, a busy worker is never killed):
  restart-worker --if-idle [--env-file <path>] [--dry-run]
      SIGTERM only the supervisor's dist/worker.js child when no handler child
      younger than A2A_NATIVE_WORKER_BUSY_MAX_SECONDS runs; the supervise loop
      relaunches it.  Verifies "registered with" + health sup_count=1 workers=1.
  restart-supervisor --if-idle [--env-file <path>] [--dry-run]
      Same gate, then stop + respawn the supervisor via the health check's
      --self-heal path (the tunnel drops briefly).  Same verification.
  --env-file defaults to the running supervisor's own --env-file.  --dry-run
  runs the gate only.  rc: 0 ok, 3 no supervisor / not one worker, 10 busy,
  11 idle state unknown, 30 restarted but not verified.

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
  A2A_TUNNEL_GATE_TIMEOUT   Tunnel /livez gate bound in seconds before the
                            worker spawns anyway (fail-open); default 30.
  A2A_TUNNEL_GATE_INTERVAL  Gate poll interval in seconds; default 0.5.
  A2A_SUPERVISOR_HEALTH_LOG Health log (default \$LOG_DIR/a2a-native-worker-health.log)
  A2A_NATIVE_WORKER_BUSY_MAX_SECONDS  Restart gate handler-age cap; default
                            \$CCC_SELF_UPDATE_BUSY_MAX_SECONDS or 1800.
  A2A_NATIVE_WORKER_RESTART_TIMEOUT   Registration wait after restart; default 90.
  A2A_NATIVE_WORKER_RESTART_SETTLE    Seconds the new worker must survive; default 20.
EOF
    return 2
}

main() {
    local cmd="${1:-}"
    [[ -z "$cmd" ]] && { usage; return 2; }
    shift
    local env_file="" if_idle=0 dry_run=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --env-file)
                # `shift 2` with one arg left is a no-op under bash (status 1,
                # params untouched), and the unknown-arg branch without a
                # return re-dispatches the same token forever — both spun this
                # loop unbounded, flooding the supervisor log.
                [[ $# -ge 2 ]] || { echo "--env-file requires a value" >&2; usage; return 2; }
                env_file="$2"; shift 2 ;;
            --if-idle)  if_idle=1; shift ;;
            --dry-run)  dry_run=1; shift ;;
            -h|--help)  usage; return 2 ;;
            *)          echo "unknown arg: $1" >&2; usage; return 2 ;;
        esac
    done
    case "$cmd" in
        restart-worker|restart-supervisor) ;;
        *)
            if (( if_idle || dry_run )); then
                echo "--if-idle/--dry-run apply only to restart-worker/restart-supervisor" >&2
                usage; return 2
            fi ;;
    esac
    case "$cmd" in
        restart-worker|restart-supervisor)
            if (( ! if_idle )); then
                echo "$cmd requires --if-idle: there is no unconditional restart (a busy worker is never killed, #2791)" >&2
                return 2
            fi
            restart_settings || return 2
            local fn="${cmd//-/_}"
            "cmd_$fn" "$env_file" "$dry_run"
            ;;
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
