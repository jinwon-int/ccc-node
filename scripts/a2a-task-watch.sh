#!/usr/bin/env bash
# A2A task pass-through pin watcher for the Termux-native worker (cron-safe).
#
# Read-only: never creates tasks, never calls a provider, never signals the
# worker. Every run compares the live worker's runtime pin env
# (/proc/<pid>/environ) against the same keys in the worker's canonical env
# file, key by exact value. A key absent from the env file is not pinned and
# is skipped. When worker artifacts changed in the last 5 minutes it also
# records a TASK ACTIVITY block with the captured pin and supervisor tail.
#
# Replaces the untracked pilot copy (~/.a2a/a2a-task-watch.sh) that hardcoded
# the pilot's expected values (thinking=medium) and so logged PIN DRIFT every
# 5 minutes after the fleet moved to high (ccc-node#2837).
#
# Usage:
#   a2a-task-watch.sh <node> --env-file <path>
#
# Log: $HOME/.a2a/task-evidence-<node>.log. A drift is logged when its
# signature changes, then at most once per A2A_TASK_WATCH_REPEAT_SECS
# (default 3600). The log rotates to <log>.1 past A2A_TASK_WATCH_MAX_BYTES
# (default 1 MiB).
#
# Exit codes: 0 ok / activity / drift recorded, 2 usage or missing env file.
set -uo pipefail

PIN_KEYS=(
    A2A_WORKER_BRIDGE_ADAPTER
    A2A_ANALYSIS_BRIDGE_ADAPTER
    A2A_WORKER_RUNTIME_FLAVOR
    A2A_CLAUDE_MODEL
    A2A_CLAUDE_EFFORT
    CLAUDE_CODE_EFFORT_LEVEL
    A2A_WORKER_THINKING
)

usage() { echo "usage: $0 <node> --env-file <path>" >&2; exit 2; }

NODE="${1:-}"
[[ -n "$NODE" && "$NODE" != -* ]] || usage
shift
ENV_FILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file) ENV_FILE="${2:-}"; shift 2 || usage ;;
        *) usage ;;
    esac
done
[[ -n "$ENV_FILE" ]] || usage
if [[ ! -r "$ENV_FILE" ]]; then
    echo "a2a-task-watch: env file not readable: $ENV_FILE" >&2
    exit 2
fi

OUT="${A2A_TASK_WATCH_OUT:-$HOME/.a2a/task-evidence-${NODE}.log}"
STATE="${OUT}.drift-state"
SUPLOG="${A2A_TASK_WATCH_SUPLOG:-$HOME/.hermes/logs/a2a-native-worker-supervisor.log}"
MAX_BYTES="${A2A_TASK_WATCH_MAX_BYTES:-1048576}"
REPEAT_SECS="${A2A_TASK_WATCH_REPEAT_SECS:-3600}"
mkdir -p "$(dirname "$OUT")" 2>/dev/null || true

stamp() { date "+%F %T%z"; }

rotate_log() {
    local size
    [[ -f "$OUT" ]] || return 0
    size=$(wc -c < "$OUT" 2>/dev/null || echo 0)
    if (( size > MAX_BYTES )); then
        mv -f "$OUT" "$OUT.1" 2>/dev/null || true
    fi
}

# Parse the env file with the same quoting rules as the worker harness
# (a2a-termux-native-worker.sh extract_env_value) and compare against the
# process environ. Prints "SNAP <k=v ...>" then one "DRIFT <k> expected=<v>
# actual=<v>" line per mismatch, or "NOPIN" when the env file pins none of
# the keys. Only the allowlisted pin keys are ever read or printed.
compare_pins() {
    python3 - "$ENV_FILE" "$1" "${PIN_KEYS[@]}" <<'PY'
import shlex, sys
env_path, environ_path, keys = sys.argv[1], sys.argv[2], sys.argv[3:]

def parse_env(path):
    out = {}
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k not in keys:
            continue
        if v.startswith(('"', "'")):
            try:
                parts = shlex.split(v, posix=True)
                if len(parts) == 1:
                    v = parts[0]
            except ValueError:
                pass
        out[k] = v
    return out

def parse_environ(path):
    out = {}
    try:
        data = open(path, "rb").read()
    except OSError:
        return out
    for item in data.split(b"\0"):
        k, sep, v = item.partition(b"=")
        key = k.decode("utf-8", "replace")
        if sep and key in keys:
            out[key] = v.decode("utf-8", "replace")
    return out

expected = parse_env(env_path)
actual = parse_environ(environ_path)
print("SNAP " + " ".join(f"{k}={actual[k]}" for k in sorted(actual)))
if not expected:
    print("NOPIN")
for k in keys:
    if k in expected and actual.get(k) != expected[k]:
        print(f"DRIFT {k} expected={expected[k]} actual={actual.get(k, '<unset>')}")
PY
}

WROOT="${A2A_TASK_WATCH_WROOT:-}"
if [[ -z "$WROOT" ]]; then
    WROOT=$(python3 - "$ENV_FILE" <<'PY'
import shlex, sys
for raw in open(sys.argv[1], encoding="utf-8"):
    line = raw.strip()
    if line.startswith("export "):
        line = line[7:].strip()
    if line.startswith("A2A_WORKER_ROOT="):
        v = line.split("=", 1)[1].strip()
        try:
            v = shlex.split(v)[0] if v else v
        except ValueError:
            pass
        print(v)
        break
PY
)
fi
[[ -n "$WROOT" ]] || WROOT="$HOME/a2a-broker-worker"

rotate_log

P="${A2A_TASK_WATCH_PID:-$(pgrep -f "[${WROOT:0:1}]${WROOT:1}/dist/worker.js" | head -1)}"
if [[ -z "$P" ]]; then
    echo "$(stamp) [watch] WARN worker not running on $NODE" >> "$OUT"
    exit 0
fi
ENVIRON="${A2A_TASK_WATCH_ENVIRON:-/proc/$P/environ}"

RESULT="$(compare_pins "$ENVIRON")"
ENVSNAP="$(printf '%s\n' "$RESULT" | sed -n 's/^SNAP //p')"
DRIFTS="$(printf '%s\n' "$RESULT" | grep -E '^(DRIFT|NOPIN)' || true)"
PIN="ok"
[[ -n "$DRIFTS" ]] && PIN="DRIFT"

RECENT="$(find "$WROOT" -maxdepth 2 -newermt '-5 minutes' -type f 2>/dev/null | head -5)"

if [[ -n "$RECENT" ]]; then
    {
        echo "$(stamp) [watch] TASK ACTIVITY node=$NODE pid=$P pin=$PIN"
        echo "  runtime-pin: $ENVSNAP"
        [[ -n "$DRIFTS" ]] && printf '%s\n' "$DRIFTS" | sed 's|^|  |'
        echo "  artifacts:"
        printf '%s\n' "$RECENT" | sed 's|^|    |'
        echo "  supervisor-tail:"
        tail -3 "$SUPLOG" 2>/dev/null | sed 's|^|    |'
    } >> "$OUT"
elif [[ -n "$DRIFTS" ]]; then
    sig="$(printf '%s' "$DRIFTS" | cksum | cut -d' ' -f1)"
    now=$(date +%s)
    prev_sig="" prev_at=0
    [[ -r "$STATE" ]] && read -r prev_sig prev_at < "$STATE"
    if [[ "$sig" != "$prev_sig" ]] || (( now - ${prev_at:-0} >= REPEAT_SECS )); then
        {
            echo "$(stamp) [watch] PIN DRIFT node=$NODE pid=$P"
            printf '%s\n' "$DRIFTS" | sed 's|^|  |'
        } >> "$OUT"
        printf '%s %s\n' "$sig" "$now" > "$STATE"
    fi
else
    rm -f "$STATE" 2>/dev/null || true
fi
exit 0
