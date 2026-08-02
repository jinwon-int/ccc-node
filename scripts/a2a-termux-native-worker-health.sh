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
# `setsid -f`, verifies a new canonical PID identity, and returns rc=0.  It also
# recovers the ND-1236 "stuck" state — a
# live supervisor whose tunnel has died and whose worker is therefore crash-
# looping — by restarting the supervisor, but only under strict guards (N
# consecutive down cycles, a reachable ssh target, and a restart cooldown; see
# the tuning block below).  Pass --no-self-heal to keep it strictly read-only
# (safe from any cron entry), or --no-tunnel-recovery to keep only the spawn
# behavior and leave a DOWN tunnel flagged for a human.
#
# Exit codes (fail-closed with distinct rc so cron logs are self-explanatory):
#   0   healthy, spawned a fresh supervisor, or recovered a dead tunnel
#   2   env validation failure, or missing --env-file
#   3   unhealthy: no supervisor in read-only mode, or supervisor up but the
#       tunnel is DOWN and this cycle could not recover it
#   4   supervisor-count-cap exceeded — MANUAL SWEEP REQUIRED (ND-1236 replay)
#   5   self-heal/recovery was requested but no canonical supervisor appeared
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$SCRIPT_DIR/a2a-termux-native-worker.sh"
# shellcheck source=scripts/lib/a2a-supervisor-identity.sh
. "$SCRIPT_DIR/lib/a2a-supervisor-identity.sh"

LOCAL_PORT=18790

LOCK_DIR="${A2A_SUPERVISOR_LOCK_DIR:-$HOME/.a2a}"
LOG_DIR="${A2A_SUPERVISOR_LOG_DIR:-$HOME/.hermes/logs}"
mkdir -p "$LOCK_DIR" "$LOG_DIR" 2>/dev/null || true

LOG="${A2A_SUPERVISOR_HEALTH_LOG:-$LOG_DIR/a2a-native-worker-health.log}"
LOCK="${A2A_SUPERVISOR_LOCK:-$LOCK_DIR/a2a-native-worker-supervisor.lock}"
PIDFILE="$LOCK.pid"

MAX_SUPERVISORS_DEFAULT=1

# Tunnel-down controlled-recovery tuning (#810).  A supervisor that holds the
# lock but has a dead tunnel is the ND-1236 "stuck" state: the worker then
# crash-loops (it can't reach the broker) and plain self-heal can't help because
# the lock is held.  We restart it — but only after TUNNEL_DOWN_RESTART_AFTER
# consecutive down cycles, only when the ssh tunnel target is reachable, and
# never more often than TUNNEL_RESTART_COOLDOWN seconds, so an unreachable broker
# or a flapping tunnel can never turn recovery into a restart storm.
TUNNEL_DOWN_RESTART_AFTER_DEFAULT=3
TUNNEL_RESTART_COOLDOWN_DEFAULT=1800
STREAK_FILE="${A2A_TUNNEL_DOWN_STREAK_FILE:-$LOCK.tunnel_down_streak}"
RESTART_TS_FILE="${A2A_TUNNEL_RESTART_TS_FILE:-$LOCK.tunnel_restart_ts}"

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

# Return the running canonical supervisor's verified identity, or empty.
current_supervisor_record() {
    a2a_current_supervisor_record "$PIDFILE" "$HARNESS" "$LOCK"
}

current_supervisor_pid() {
    local record pid _token
    record=$(current_supervisor_record)
    [[ -n "$record" ]] || return 0
    read -r pid _token <<<"$record"
    printf '%s' "$pid"
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
    local pid="$1" stat rest ppid pgrp session
    [[ -n "$pid" && -r "/proc/$pid/stat" ]] || return 1
    stat=$(cat "/proc/$pid/stat" 2>/dev/null) || return 1
    rest=${stat##*) }
    read -r _ ppid pgrp session _ <<<"$rest"
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
    # Compare NUL-delimited argv exactly.  Treating an operator-controlled root
    # as a pgrep regex lets '.', '[', or '\\' change which processes match.
    python3 - "$root/dist/worker.js" <<'PY'
import glob
import os
import sys

expected = os.path.realpath(sys.argv[1])
count = 0
for path in glob.glob("/proc/[0-9]*/cmdline"):
    if path == f"/proc/{os.getpid()}/cmdline":
        continue
    try:
        argv = [os.fsdecode(arg) for arg in open(path, "rb").read().split(b"\0") if arg]
    except OSError:
        continue
    if any(os.path.realpath(arg) == expected for arg in argv[1:]):
        count += 1
print(count)
PY
}

tunnel_status() {
    if timeout 3 curl -fsS -o /dev/null "http://127.0.0.1:${LOCAL_PORT}/livez" 2>/dev/null; then
        echo UP
    else
        echo DOWN
    fi
}

# ---- tunnel-down controlled-recovery helpers (#810) ----
# Read a small non-negative integer from a state file (0 if absent/garbage).
_read_int() {
    local f="$1" v
    v=$(head -n1 "$f" 2>/dev/null | tr -dc '0-9')
    printf '%s' "${v:-0}"
}
# Increment the consecutive-tunnel-down streak and echo the new value.
bump_tunnel_down_streak() {
    local n
    n=$(_read_int "$STREAK_FILE"); n=$((n + 1))
    printf '%s\n' "$n" > "$STREAK_FILE" 2>/dev/null || true
    printf '%s' "$n"
}
reset_tunnel_down_streak() { rm -f "$STREAK_FILE" 2>/dev/null || true; }
record_restart_ts()        { date +%s > "$RESTART_TS_FILE" 2>/dev/null || true; }
# True while we are still inside the post-restart cooldown window.
in_restart_cooldown() {
    local last now
    last=$(_read_int "$RESTART_TS_FILE")
    [[ "$last" -gt 0 ]] || return 1
    now=$(date +%s)
    (( now - last < TUNNEL_RESTART_COOLDOWN ))
}
# True when the ssh tunnel target actually accepts a connection.  Gates the
# restart so a genuinely unreachable broker stays a flagged (rc=3) condition for
# a human, rather than triggering pointless supervisor churn.
ssh_target_reachable() {
    local env_file="$1" target
    target=$(extract_env_value A2A_TUNNEL_SSH_TARGET "$env_file")
    [[ -n "$target" ]] || return 1
    timeout 10 ssh -o BatchMode=yes -o ConnectTimeout=6 \
        -o StrictHostKeyChecking=accept-new "$target" true >/dev/null 2>&1
}

# Print a single-line JSON summary.  Python is already a hard dependency of the
# native harness; json.dumps safely quotes paths and control characters without
# adding jq to minimal Termux profiles.
emit_json() {
    local sup_pid="$1" sup_count="$2" sup_pids_csv="$3" \
          workers="$4" tunnel="$5" action="$6" rc="$7"
    python3 - "$sup_pid" "$sup_count" "$sup_pids_csv" "$workers" \
        "$tunnel" "$action" "$rc" "$MAX_SUPERVISORS" "$LOCK" <<'PY'
import datetime
import json
import sys

sup_pid, sup_count, sup_pids, workers, tunnel, action, rc, maximum, lock = sys.argv[1:]
payload = {
    "schema": "a2a-native-worker-health.v1",
    "supervisor_pid": int(sup_pid) if sup_pid else None,
    "supervisor_count": int(sup_count),
    "supervisor_pids": [int(pid) for pid in sup_pids.split(",") if pid],
    "workers": int(workers),
    "tunnel": tunnel,
    "action": action,
    "rc": int(rc),
    "max_supervisors": int(maximum),
    "lock": lock,
    "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
PY
}

valid_bounded_uint() {
    local name="$1" value="$2" maximum="$3" numeric
    if [[ ! "$value" =~ ^[0-9]{1,6}$ ]]; then
        printf '%s must be an integer in 1..%s (got %q)\n' \
            "$name" "$maximum" "$value" >&2
        return 1
    fi
    numeric=$((10#$value))
    if (( numeric < 1 || numeric > maximum )); then
        printf '%s must be an integer in 1..%s (got %q)\n' \
            "$name" "$maximum" "$value" >&2
        return 1
    fi
}

# `setsid -f` returning zero proves only that its fork request was accepted.
# Require a new canonical PID/start-token record before claiming recovery.
spawn_supervisor_and_verify() {
    local env_file="$1" old_record="${2:-}" record attempt
    setsid -f bash "$HARNESS" supervise --env-file "$env_file" \
        </dev/null >>"$LOG" 2>&1 || return 1
    for ((attempt = 0; attempt < 50; attempt++)); do
        record=$(current_supervisor_record)
        if [[ -n "$record" && "$record" != "$old_record" ]]; then
            return 0
        fi
        sleep 0.1
    done
    return 1
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
  --tunnel-down-restart-after N
                          Consecutive tunnel-DOWN cycles (with a live
                          supervisor) before a controlled restart is attempted.
                          Default: $TUNNEL_DOWN_RESTART_AFTER_DEFAULT.  Requires --self-heal + reachable target.
  --tunnel-restart-cooldown SEC
                          Minimum seconds between controlled restarts.
                          Default: $TUNNEL_RESTART_COOLDOWN_DEFAULT.
  --no-tunnel-recovery    Disable the controlled restart; keep the legacy
                          flag-only rc=3 behavior for a DOWN tunnel.
  --json                  Emit a single-line JSON summary in addition to
                          human-readable output.
  --quiet                 Suppress human-readable output on rc=0 (still prints
                          on failure so cron mails see it).
  -h, --help              Show this help.

Exit codes:
  0  healthy or spawned a fresh supervisor
  2  env validation failure / missing --env-file
  3  no supervisor in read-only mode, or supervisor tunnel is DOWN
  4  supervisor-count-cap exceeded — MANUAL SWEEP REQUIRED (Wiki ND-1236)
  5  self-heal was requested but no canonical supervisor appeared

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
    local tunnel_recovery=1
    MAX_SUPERVISORS="$MAX_SUPERVISORS_DEFAULT"
    TUNNEL_DOWN_RESTART_AFTER="$TUNNEL_DOWN_RESTART_AFTER_DEFAULT"
    TUNNEL_RESTART_COOLDOWN="$TUNNEL_RESTART_COOLDOWN_DEFAULT"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --env-file)
                [[ $# -ge 2 ]] || { echo "--env-file requires a value" >&2; return 2; }
                env_file="$2"; shift 2 ;;
            --self-heal)        self_heal=1; shift ;;
            --no-self-heal)     self_heal=0; shift ;;
            --max-supervisors)
                [[ $# -ge 2 ]] || { echo "--max-supervisors requires a value" >&2; return 2; }
                MAX_SUPERVISORS="$2"; shift 2 ;;
            --no-tunnel-recovery)        tunnel_recovery=0; shift ;;
            --tunnel-down-restart-after)
                [[ $# -ge 2 ]] || { echo "--tunnel-down-restart-after requires a value" >&2; return 2; }
                TUNNEL_DOWN_RESTART_AFTER="$2"; shift 2 ;;
            --tunnel-restart-cooldown)
                [[ $# -ge 2 ]] || { echo "--tunnel-restart-cooldown requires a value" >&2; return 2; }
                TUNNEL_RESTART_COOLDOWN="$2"; shift 2 ;;
            --json)             emit_json_flag=1; shift ;;
            --quiet)            quiet=1; shift ;;
            -h|--help)          usage; return 2 ;;
            *)                  echo "unknown arg: $1" >&2; usage; return 2 ;;
        esac
    done

    [[ -n "$env_file" ]] || { echo "--env-file required" >&2; usage; return 2; }
    [[ -f "$env_file" ]] || { echo "env file not found: $env_file" >&2; return 2; }
    valid_bounded_uint --max-supervisors "$MAX_SUPERVISORS" 100 || return 2
    valid_bounded_uint --tunnel-down-restart-after "$TUNNEL_DOWN_RESTART_AFTER" 1000 || return 2
    valid_bounded_uint --tunnel-restart-cooldown "$TUNNEL_RESTART_COOLDOWN" 604800 || return 2

    # Validate the env via the canonical harness before doing anything else.
    if ! "$HARNESS" check --env-file "$env_file" >/dev/null 2>&1; then
        log "env validation failed for $env_file"
        echo "env validation failed for $env_file" >&2
        return 2
    fi

    local sup_record sup_pid _sup_token worker_root workers tunnel sup_count sup_pids_csv
    sup_record=$(current_supervisor_record)
    sup_pid=""
    [[ -n "$sup_record" ]] && read -r sup_pid _sup_token <<<"$sup_record"
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
            # ND-1236 "stuck" state: a supervisor holds the lock but has no
            # tunnel, so the worker crash-loops and plain self-heal can't help.
            # Attempt a *controlled* restart under strict guards (see the tuning
            # block at the top).  Never in read-only (--no-self-heal) mode or
            # when recovery is disabled.
            if (( self_heal && tunnel_recovery )); then
                local streak
                streak=$(bump_tunnel_down_streak)
                if (( streak >= TUNNEL_DOWN_RESTART_AFTER )) \
                   && ! in_restart_cooldown \
                   && ssh_target_reachable "$env_file"; then
                    log "tunnel-down recovery: sup=$sup_pid streak=$streak reachable — controlled restart"
                    record_restart_ts
                    reset_tunnel_down_streak
                    "$HARNESS" stop >/dev/null 2>&1 || true
                    if spawn_supervisor_and_verify "$env_file" "$sup_record"; then
                        local msg="tunnel-down recovery: restarted supervisor (was pid=$sup_pid after $streak down cycles)"
                        log "$msg"
                        (( quiet )) || echo "$msg"
                        (( emit_json_flag )) && emit_json "$sup_pid" "$sup_count" "$sup_pids_csv" \
                            "$workers" "$tunnel" tunnel-recovered 0
                        return 0
                    fi
                    log "tunnel-down recovery: no canonical supervisor appeared after setsid"
                    echo "tunnel-down recovery: no canonical supervisor appeared after setsid" >&2
                    (( emit_json_flag )) && emit_json "$sup_pid" "$sup_count" "$sup_pids_csv" \
                        "$workers" "$tunnel" self-heal-failed 5
                    return 5
                fi
                # Guards not met this cycle — flag and wait for the next one.
                local why="streak=$streak/$TUNNEL_DOWN_RESTART_AFTER"
                in_restart_cooldown && why="$why cooldown"
                local msg="supervisor pid=$sup_pid up but tunnel DOWN — $why; deferring restart (investigate ssh target if persistent)"
                log "$msg"
                echo "$msg" >&2
                (( emit_json_flag )) && emit_json "$sup_pid" "$sup_count" "$sup_pids_csv" \
                    "$workers" "$tunnel" tunnel-down 3
                return 3
            fi
            # Read-only / recovery-disabled: original flag-only behavior.
            local msg="supervisor pid=$sup_pid up but tunnel DOWN — self-heal cannot fix; investigate ssh target"
            log "$msg"
            echo "$msg" >&2
            (( emit_json_flag )) && emit_json "$sup_pid" "$sup_count" "$sup_pids_csv" \
                "$workers" "$tunnel" tunnel-down 3
            return 3
        fi
        # Tunnel UP — clear any pending down-streak so the next down episode
        # starts its debounce from zero.
        (( self_heal && tunnel_recovery )) && reset_tunnel_down_streak
        (( quiet )) || printf 'OK sup=%s workers=%s tunnel=%s (cap=%s/%s)\n' \
            "$sup_pid" "$workers" "$tunnel" "$sup_count" "$MAX_SUPERVISORS"
        (( emit_json_flag )) && emit_json "$sup_pid" "$sup_count" "$sup_pids_csv" \
            "$workers" "$tunnel" ok 0
        return 0
    fi

    # ---- no supervisor: optionally self-heal ----
    if (( self_heal == 0 )); then
        printf 'DOWN no supervisor holding %s (self-heal disabled)\n' "$LOCK" >&2
        (( emit_json_flag )) && emit_json "" "$sup_count" "$sup_pids_csv" \
            "$workers" "$tunnel" no-supervisor 3
        return 3
    fi

    log "self-heal: spawning supervisor via setsid"
    if spawn_supervisor_and_verify "$env_file"; then
        (( quiet )) || printf 'STARTED workers=%s tunnel=%s\n' "$workers" "$tunnel"
        (( emit_json_flag )) && emit_json "" "$sup_count" "$sup_pids_csv" \
            "$workers" "$tunnel" spawned 0
        return 0
    fi

    log "self-heal FAILED: no canonical supervisor appeared after setsid"
    echo "self-heal FAILED: no canonical supervisor appeared after setsid" >&2
    (( emit_json_flag )) && emit_json "" "$sup_count" "$sup_pids_csv" \
        "$workers" "$tunnel" self-heal-failed 5
    return 5
}

# Only run main() when executed, not when sourced (so unit tests can pull in
# helpers like list_supervisor_pids without triggering the checker).
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
