#!/usr/bin/env bash
# Termux-native A2A worker health check + optional self-heal (cron-safe).
#
# Read-only diagnostics against the singleton supervisor deployed by
# a2a-termux-native-worker.sh (LOCK/PIDFILE, tunnel, worker count), plus a
# supervisor-count-cap detector that flags the exact >1-supervisor pile-up
# that motivated Wiki ND-1236 — even when flock has been bypassed (e.g. on
# a pre-migration node still running ~/.hermes/scripts/native-worker-supervisor.sh
# in parallel with the canonical one).
#
# By default this script self-heals: if no supervisor is holding the lock and
# the supervisor-count-cap is not violated, it detaches a fresh supervisor via
# `setsid -f` and returns rc=0.  Pass --no-self-heal to keep it strictly
# read-only (safe from any cron entry).
#
# Exit codes (fail-closed with distinct rc so cron logs are self-explanatory):
#   0   healthy, or spawned a fresh supervisor
#   2   env validation failure, or missing --env-file
#   3   supervisor is running but tunnel is DOWN and self-heal cannot fix that
#   4   supervisor-count-cap exceeded — MANUAL SWEEP REQUIRED (ND-1236 replay)
#   5   self-heal was requested but spawning setsid failed
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HARNESS="$SCRIPT_DIR/a2a-termux-native-worker.sh"

LOCAL_PORT=18790

LOCK_DIR="${A2A_SUPERVISOR_LOCK_DIR:-$HOME/.a2a}"
LOG_DIR="${A2A_SUPERVISOR_LOG_DIR:-$HOME/.hermes/logs}"
mkdir -p "$LOCK_DIR" "$LOG_DIR" 2>/dev/null || true

LOG="${A2A_SUPERVISOR_HEALTH_LOG:-$LOG_DIR/a2a-native-worker-health.log}"
LOCK="${A2A_SUPERVISOR_LOCK:-$LOCK_DIR/a2a-native-worker-supervisor.lock}"
PIDFILE="$LOCK.pid"

MAX_SUPERVISORS_DEFAULT=1

# Regex fragments used by pgrep -f.  We match BOTH the canonical script (the
# one this file lives beside) AND the legacy hand-rolled script name that
# gongyung/daegyo used before migration.  A pre-migration node that still
# runs both is exactly the pile-up scenario the cap-detector must catch.
CANONICAL_SIG='a2a-termux-native-worker\.sh[[:space:]]+supervise'
LEGACY_SIG='native-worker-supervisor\.sh'

log() {
    printf '%s [health:%d] %s\n' "$(date '+%F %T%z')" "$$" "$*" >> "$LOG"
}

# Extract one value from an env file (same rules as the Python validator).
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

# Return the running supervisor's PID, or empty.  Same logic as the harness.
current_supervisor_pid() {
    [[ -f "$PIDFILE" ]] || return 0
    local pid
    pid=$(head -n1 "$PIDFILE" 2>/dev/null | tr -d '[:space:]')
    [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] || return 0
    kill -0 "$pid" 2>/dev/null && printf '%s' "$pid" || return 0
}

# Count distinct supervisor-looking processes.  Emits space-separated PIDs on
# stdout.  We look at *both* the canonical supervise loop AND the legacy
# hand-rolled script, so a pre-migration node with both running is caught
# even if flock is intact for each individually.
#
# We keep only detached supervisor roots.  On classic init systems a
# `setsid -f` detached process is often reparented to ppid=1, but under
# user/systemd managers or other subreapers it may instead be reparented to the
# manager while still being the process-group/session leader.  Accept either
# shape.  Tunnel-loop / worker-loop subshells inherit the supervisor's session
# and process group but are not leaders, so they are still filtered out.
is_detached_supervisor_root() {
    local pid="$1" stat rest state ppid pgrp session
    [[ -n "$pid" && -r "/proc/$pid/stat" ]] || return 1
    stat=$(cat "/proc/$pid/stat" 2>/dev/null) || return 1
    rest=${stat##*) }
    read -r state ppid pgrp session _ <<<"$rest"
    [[ -n "$ppid" && -n "$pgrp" && -n "$session" ]] || return 1
    [[ "$ppid" == "1" || "$pid" == "$pgrp" || "$pid" == "$session" ]]
}

list_supervisor_pids() {
    { pgrep -f "$CANONICAL_SIG" 2>/dev/null || true
      pgrep -f "$LEGACY_SIG"    2>/dev/null || true
    } | awk 'NF && !seen[$0]++' | while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        is_detached_supervisor_root "$pid" && printf '%s\n' "$pid"
    done
}

count_workers_under() {
    local root="$1"
    local n
    if [[ -z "$root" ]]; then
        n=$(pgrep -c -f 'dist/worker\.js' 2>/dev/null || true)
        printf '%s\n' "${n:-0}"
        return
    fi
    # Skip regex-escape entirely: pgrep's `.` in worker.js is technically
    # "any char", but for our purpose (counting live worker.js processes
    # under a specific root) that's a harmless over-match — no real command
    # line will match "$root/dist/worker[not-a-dot]js".  This avoids brittle
    # portability of `sed` character-class parsing (Termux's sed rejects some
    # bracket patterns that GNU sed accepts).
    n=$(pgrep -c -f "$root/dist/worker.js" 2>/dev/null || true)
    printf '%s\n' "${n:-0}"
}

tunnel_status() {
    if timeout 3 curl -sS -o /dev/null "http://127.0.0.1:${LOCAL_PORT}/livez" 2>/dev/null; then
        echo UP
    else
        echo DOWN
    fi
}

# Print a single-line JSON summary.  Deliberately hand-rolled (no jq) so the
# health check works on minimal Termux profiles and can be piped straight into
# a fleet log ingester.
emit_json() {
    local sup_pid="$1" sup_count="$2" sup_pids_csv="$3" \
          workers="$4" tunnel="$5" action="$6" rc="$7"
    printf '{"schema":"a2a-native-worker-health.v1","supervisor_pid":%s,"supervisor_count":%s,"supervisor_pids":[%s],"workers":%s,"tunnel":"%s","action":"%s","rc":%s,"max_supervisors":%s,"lock":"%s","ts":"%s"}\n' \
        "${sup_pid:-null}" "$sup_count" "$sup_pids_csv" \
        "$workers" "$tunnel" "$action" "$rc" "$MAX_SUPERVISORS" \
        "$LOCK" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
}

usage() {
    cat <<EOF >&2
Usage: $(basename "$0") --env-file <path> [options]

Options:
  --env-file <path>       Same env file the supervisor uses.  Required.
  --self-heal             (default) spawn a supervisor via \`setsid -f\` if
                          none is running and the cap check passes.
  --no-self-heal          Read-only mode — never spawn a supervisor.
  --max-supervisors N     Hard cap on distinct supervisor-looking processes
                          (canonical + legacy).  Default: $MAX_SUPERVISORS_DEFAULT.
                          rc=4 if exceeded; no self-heal in that case.
  --json                  Emit a single-line JSON summary in addition to
                          human-readable output.
  --quiet                 Suppress human-readable output on rc=0 (still prints
                          on failure so cron mails see it).
  -h, --help              Show this help.

Exit codes:
  0  healthy or spawned a fresh supervisor
  2  env validation failure / missing --env-file
  3  supervisor is running but tunnel is DOWN (self-heal cannot help)
  4  supervisor-count-cap exceeded — MANUAL SWEEP REQUIRED (Wiki ND-1236)
  5  self-heal was requested but spawning setsid failed

Environment overrides (shared with the harness):
  A2A_SUPERVISOR_LOCK_DIR       Default \$HOME/.a2a
  A2A_SUPERVISOR_LOG_DIR        Default \$HOME/.hermes/logs
  A2A_SUPERVISOR_LOCK           Full lock file path
  A2A_SUPERVISOR_HEALTH_LOG     Full health log path (default \$LOG_DIR/a2a-native-worker-health.log)
EOF
    return 2
}

main() {
    local env_file=""
    local self_heal=1
    local emit_json_flag=0
    local quiet=0
    MAX_SUPERVISORS="$MAX_SUPERVISORS_DEFAULT"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --env-file)         env_file="${2:-}"; shift 2 ;;
            --self-heal)        self_heal=1; shift ;;
            --no-self-heal)     self_heal=0; shift ;;
            --max-supervisors)  MAX_SUPERVISORS="${2:-1}"; shift 2 ;;
            --json)             emit_json_flag=1; shift ;;
            --quiet)            quiet=1; shift ;;
            -h|--help)          usage; return 2 ;;
            *)                  echo "unknown arg: $1" >&2; usage; return 2 ;;
        esac
    done

    [[ -n "$env_file" ]] || { echo "--env-file required" >&2; usage; return 2; }
    [[ -f "$env_file" ]] || { echo "env file not found: $env_file" >&2; return 2; }

    # Validate the env via the canonical harness before doing anything else.
    if ! "$HARNESS" check --env-file "$env_file" >/dev/null 2>&1; then
        log "env validation failed for $env_file"
        echo "env validation failed for $env_file" >&2
        return 2
    fi

    local sup_pid worker_root workers tunnel sup_pids sup_count sup_pids_csv
    sup_pid=$(current_supervisor_pid)
    worker_root=$(extract_env_value A2A_WORKER_ROOT "$env_file")
    workers=$(count_workers_under "$worker_root")
    tunnel=$(tunnel_status)

    # Collect every supervisor-looking PID so the cap detector catches BOTH
    # a duplicated canonical AND a leftover legacy script.
    mapfile -t _pids < <(list_supervisor_pids)
    sup_count=${#_pids[@]}
    # Comma-separated PID list for the JSON emitter.
    sup_pids_csv=$(printf '%s\n' "${_pids[@]}" | paste -sd, -)

    log "state sup_pid=${sup_pid:-none} sup_count=$sup_count workers=$workers tunnel=$tunnel"

    # ---- cap check first: refuse to self-heal on top of a pile-up ----
    if (( sup_count > MAX_SUPERVISORS )); then
        local msg="supervisor-count-cap EXCEEDED: count=$sup_count > max=$MAX_SUPERVISORS pids=[$sup_pids_csv] — MANUAL SWEEP REQUIRED (Wiki ND-1236)"
        log "$msg"
        echo "$msg" >&2
        (( emit_json_flag )) && emit_json "$sup_pid" "$sup_count" "$sup_pids_csv" \
            "$workers" "$tunnel" cap-exceeded 4
        return 4
    fi

    # ---- supervisor already up ----
    if [[ -n "$sup_pid" ]]; then
        if [[ "$tunnel" == DOWN ]]; then
            local msg="supervisor pid=$sup_pid up but tunnel DOWN — self-heal cannot fix; investigate ssh target"
            log "$msg"
            echo "$msg" >&2
            (( emit_json_flag )) && emit_json "$sup_pid" "$sup_count" "$sup_pids_csv" \
                "$workers" "$tunnel" tunnel-down 3
            return 3
        fi
        (( quiet )) || printf 'OK sup=%s workers=%s tunnel=%s (cap=%s/%s)\n' \
            "$sup_pid" "$workers" "$tunnel" "$sup_count" "$MAX_SUPERVISORS"
        (( emit_json_flag )) && emit_json "$sup_pid" "$sup_count" "$sup_pids_csv" \
            "$workers" "$tunnel" ok 0
        return 0
    fi

    # ---- no supervisor: optionally self-heal ----
    if (( self_heal == 0 )); then
        (( quiet )) || printf 'DOWN no supervisor holding %s (self-heal disabled)\n' "$LOCK"
        (( emit_json_flag )) && emit_json "" "$sup_count" "$sup_pids_csv" \
            "$workers" "$tunnel" no-supervisor 0
        return 0
    fi

    log "self-heal: spawning supervisor via setsid"
    if setsid -f bash "$HARNESS" supervise --env-file "$env_file" \
            </dev/null >>"$LOG" 2>&1; then
        (( quiet )) || printf 'STARTED workers=%s tunnel=%s\n' "$workers" "$tunnel"
        (( emit_json_flag )) && emit_json "" "$sup_count" "$sup_pids_csv" \
            "$workers" "$tunnel" spawned 0
        return 0
    fi

    log "self-heal FAILED: setsid returned nonzero"
    echo "self-heal FAILED: setsid returned nonzero" >&2
    (( emit_json_flag )) && emit_json "" "$sup_count" "$sup_pids_csv" \
        "$workers" "$tunnel" self-heal-failed 5
    return 5
}

# Only run main() when executed, not when sourced (so unit tests can pull in
# helpers like list_supervisor_pids without triggering the checker).
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
