#!/bin/bash

# Telegram Skill Bot startup script

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
LOCK_FILE="$SCRIPT_DIR/requirements.lock.txt"
PYPROJECT_FILE="$SCRIPT_DIR/pyproject.toml"
ENV_FILE="$SCRIPT_DIR/.env"
REQ_HASH_FILE="$VENV_DIR/.req_hash"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

deps_install_mode() {
    # Locked (default): install third-party dependencies exclusively from the
    # hash-locked requirements.lock.txt so every node at the same checkout
    # resolves identical versions and pip rejects any unhashed transitive
    # dependency. CCC_DEPS_UNLOCKED=1 is the documented escape hatch for hosts
    # where a locked artifact cannot build; it restores the legacy
    # lower-bound requirements.txt flow and therefore loses reproducibility.
    #
    # Resolution order matches the rest of the bridge config: an explicitly
    # set process environment value wins, then the project .env
    # ($PROJECT_ROOT/.telegram_bot/.env), then the bot source dir .env —
    # merge_env_files() never exports project .env keys into the shell, so
    # this must go through read_env_with_fallback. Only the literal "1"
    # selects the unlocked flow; anything else stays locked (fail closed to
    # the reproducible default).
    local configured="${CCC_DEPS_UNLOCKED:-}"
    if [ -z "$configured" ]; then
        configured="$(read_env_with_fallback "CCC_DEPS_UNLOCKED")"
    fi
    if [ "$configured" = "1" ]; then
        echo "unlocked"
    else
        echo "locked"
    fi
}

get_requirements_hash() {
    "$VENV_DIR/bin/python" - "$REQ_FILE" "$LOCK_FILE" "$PYPROJECT_FILE" "$(deps_install_mode)" <<'PY'
import hashlib, pathlib, sys
h = hashlib.sha256()
for name in sys.argv[1:-1]:
    path = pathlib.Path(name)
    h.update(path.read_bytes() if path.exists() else b"<absent>")
    h.update(b"\0")
h.update(sys.argv[-1].encode())
print(h.hexdigest())
PY
}

ensure_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        echo "📦 Virtual environment not found, creating..."
        if ! python3 -m venv "$VENV_DIR"; then
            echo "❌ Failed to create virtual environment: $VENV_DIR"
            exit 1
        fi
    fi
    cleanup_package_link
}

cleanup_package_link() {
    # Older installs exposed bridge/ as telegram_bot through a site-packages
    # symlink. Remove that stale link before editable installation so import
    # resolution is owned by packaging metadata instead of filesystem state.
    local sp
    sp="$("$VENV_DIR/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)"
    if [ -n "$sp" ] && [ -L "$sp/telegram_bot" ]; then
        rm -f "$sp/telegram_bot"
    fi
}

ensure_android_api_level() {
    # Some Rust-backed Python dependencies (for example pyromark via maturin/pyo3)
    # need ANDROID_API_LEVEL when building on Termux. Auto-detect it so mobile
    # installs do not fail before the bridge can start.
    if [ -n "${ANDROID_API_LEVEL:-}" ]; then
        return 0
    fi
    if { [ -n "${TERMUX_VERSION:-}" ] || printf '%s' "${PREFIX:-}" | grep -q '/com.termux/'; } \
        && command -v getprop >/dev/null 2>&1; then
        local sdk
        sdk="$(getprop ro.build.version.sdk 2>/dev/null | tr -dc '0-9')"
        if [ -n "$sdk" ]; then
            export ANDROID_API_LEVEL="$sdk"
            echo -e "\033[90m✓ Android API level auto-detected: $ANDROID_API_LEVEL\033[0m"
        fi
    fi
}

sync_dependencies() {
    local force_install="$1"
    local current_hash saved_hash

    current_hash="$(get_requirements_hash)"
    [ -f "$REQ_HASH_FILE" ] && saved_hash="$(cat "$REQ_HASH_FILE")"

    if [ "$force_install" = "1" ] || [ -z "$saved_hash" ] || [ "$saved_hash" != "$current_hash" ]; then
        echo "📦 Installing Python dependencies..."
        ensure_android_api_level
        if [ "$(deps_install_mode)" = "locked" ]; then
            if [ ! -f "$LOCK_FILE" ]; then
                echo "❌ Hash lock not found: $LOCK_FILE"
                echo "   Regenerate it with scripts/ccc-deps-lock.sh, or set"
                echo "   CCC_DEPS_UNLOCKED=1 to use the legacy unlocked install."
                exit 1
            fi
            # Every third-party package (including transitives) must match a
            # recorded hash; the venv's bundled pip (>=22.3 on Python 3.11+)
            # already supports --require-hashes, so no unpinned pip
            # self-upgrade is performed on this path.
            if ! "$VENV_DIR/bin/pip" install -q --require-hashes -r "$LOCK_FILE"; then
                echo "❌ Hash-locked dependency installation failed"
                echo "   If this host cannot install a locked artifact, retry with"
                echo "   CCC_DEPS_UNLOCKED=1 and report the platform gap."
                exit 1
            fi
            # --no-deps keeps the editable first-party install from pulling
            # any unhashed transitive dependency outside the lock.
            if ! "$VENV_DIR/bin/pip" install -q --no-deps -e "$SCRIPT_DIR"; then
                echo "❌ Editable bridge package installation failed"
                exit 1
            fi
        else
            echo "⚠️  CCC_DEPS_UNLOCKED=1 — legacy unlocked install (no hash verification)"
            if ! "$VENV_DIR/bin/pip" install -q --upgrade pip; then
                echo "❌ Failed to upgrade pip"
                exit 1
            fi
            if ! "$VENV_DIR/bin/pip" install -q -r "$REQ_FILE"; then
                echo "❌ Dependency installation failed"
                exit 1
            fi
            if ! "$VENV_DIR/bin/pip" install -q -e "$SCRIPT_DIR"; then
                echo "❌ Editable bridge package installation failed"
                exit 1
            fi
        fi
        echo "$current_hash" > "$REQ_HASH_FILE"
        echo "✅ Dependencies are up to date"
    else
        echo -e "\033[90m✓ Dependencies unchanged (requirements hash match)\033[0m"
    fi
}

get_checkout_version() {
    local version_cmd="$REPO_ROOT/scripts/ccc-version.sh"
    if [ ! -x "$version_cmd" ]; then
        return 1
    fi
    CCC_VERSION_REPO_DIR="$REPO_ROOT" "$version_cmd"
}

validate_canonical_origin() {
    local origin
    origin="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null)" || return 1
    case "$origin" in
        https://github.com/jinwon-int/ccc-node|\
        https://github.com/jinwon-int/ccc-node.git|\
        git@github.com:jinwon-int/ccc-node|\
        git@github.com:jinwon-int/ccc-node.git|\
        ssh://git@github.com/jinwon-int/ccc-node|\
        ssh://git@github.com/jinwon-int/ccc-node.git)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

check_update() {
    local current
    current="$(get_checkout_version 2>/dev/null || echo unknown)"
    echo -e "\033[90m✓ ccc-node checkout: ${current}\033[0m"
    echo -e "\033[90m  Updates are managed by: scripts/ccc-self-update.sh\033[0m"
}

# Basic repository sanity check
if [ ! -f "$REQ_FILE" ]; then
    echo ""
    echo -e "${RED}❌ requirements.txt not found: $REQ_FILE${NC}"
    echo "Please run this script from the project repository."
    echo ""
    exit 1
fi

ACTION="run"
DAEMON_MODE=0  # Default to foreground mode
PROCESS_MODE="foreground"
RUN_AS_DAEMON_SUPERVISOR=0
INTERNAL_RUN=0
WATCHDOG_INTERVAL=60

# Show help when no arguments are given
if [ $# -eq 0 ]; then
    set -- "--help"
fi

# Parse arguments
while [ $# -gt 0 ]; do
    case "$1" in
        --path)
            export PROJECT_ROOT="$2"
            shift 2
            ;;
        --debug)
            export BOT_DEBUG=1
            export LOG_LEVEL=DEBUG
            shift
            ;;
        -d|--daemon)
            DAEMON_MODE=1
            shift
            ;;
        --install)
            ACTION="install"
            shift
            ;;
        --uninstall)
            ACTION="uninstall"
            shift
            ;;
        --install-systemd)
            ACTION="install-systemd"
            shift
            ;;
        --uninstall-systemd)
            ACTION="uninstall-systemd"
            shift
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        --stop)
            ACTION="stop"
            shift
            ;;
        --restart)
            ACTION="restart"
            shift
            ;;
        --upgrade)
            ACTION="upgrade"
            shift
            ;;
        --version)
            ACTION="version"
            shift
            ;;
        --_daemon_supervisor)
            PROCESS_MODE="daemon"
            RUN_AS_DAEMON_SUPERVISOR=1
            INTERNAL_RUN=1
            DAEMON_MODE=0
            shift
            ;;
        --_launchd_child)
            PROCESS_MODE="launchd"
            INTERNAL_RUN=1
            shift
            ;;
        --_reap-competing-pollers)
            # Internal/testing seam: terminate competing project-bot pollers
            # for --path and exit. Exercises the same code path the daemon
            # supervisor runs before an auto-restart (409 self-heal).
            ACTION="reap-competing-pollers"
            INTERNAL_RUN=1
            shift
            ;;
        -h|--help)
            cat <<EOF
Usage: $0 <project_path> [options]
       $0 --path <project_path> [options]

Options:
  -h, --help          Show this help message and exit
  --path <dir>        Set project root directory (required for all actions)
  -d, --daemon        Run bot in background (default: foreground)
  --debug             Enable debug/verbose logging
  --status            Show whether the bot is running
  --stop              Stop the running bot
  --restart           Atomic stop → start → verify-available (add -d to restart
                      into daemon mode). Exits 0 only once --status reports
                      "available"; nonzero with a reason otherwise. Refuses
                      when systemd/launchd manages the bridge (exit 3), or
                      when invoked from inside the target bridge tree (exit 5).
  --upgrade           Update through canonical ccc-self-update and reinstall if changed
  --version           Print the installed ccc-node checkout identity
  --install           Install as macOS launchd startup service
  --uninstall         Remove macOS launchd startup service
  --install-systemd   Install as a Linux systemd startup service (reboot-persistent)
  --uninstall-systemd Remove the Linux systemd startup service
EOF
            exit 0
            ;;
        *)
            # First non-option argument as project path
            # Reject unknown flags loudly. Silently swallowing them turns a
            # typo (or a plausible-but-nonexistent flag like `--start`) into a
            # foreground run whose lifecycle is tied to the invoking shell —
            # observed on daegyo where `--start` was assumed to mean a managed
            # start.
            case "$1" in
                -*)
                    echo "❌ Error: Unknown option: $1"
                    echo "Use --help to list supported options."
                    exit 1
                    ;;
            esac
            if [ -z "$PROJECT_ROOT" ]; then
                export PROJECT_ROOT="$1"
            fi
            shift
            ;;
    esac
done

echo "🤖 Claude Telegram Bot Bridge"
echo "================================"

if [ -n "$BOT_DEBUG" ]; then
    echo "🐛 Debug mode enabled"
fi

if [ -z "$PROJECT_ROOT" ]; then
    echo "❌ Error: Please specify project path"
    echo "Usage: $0 <project_path>  or  $0 --path <project_path>"
    exit 1
fi

# Validate project path
PROJECT_ROOT="$(cd "$PROJECT_ROOT" 2>/dev/null && pwd)" || {
    echo "❌ Error: Project path does not exist: $PROJECT_ROOT"
    exit 1
}
export PROJECT_ROOT
echo "📂 Project path: $PROJECT_ROOT"
BOT_DATA_DIR="$PROJECT_ROOT/.telegram_bot"
LOGS_DIR="$BOT_DATA_DIR/logs"
PID_FILE="$BOT_DATA_DIR/bot.pid"
SUPERVISOR_PID_FILE="$BOT_DATA_DIR/supervisor.pid"
HEALTH_FILE="$BOT_DATA_DIR/health.json"
HEALTH_STALE_SECONDS=$((WATCHDOG_INTERVAL * 2 + 30))
ENV_FILE="$BOT_DATA_DIR/.env"
ENV_EXAMPLE_FILE="$SCRIPT_DIR/.env.example"
PROJECT_SLUG="$(basename "$PROJECT_ROOT" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/-*$//')"
PLIST_LABEL="com.telegram-skill-bot.${PROJECT_SLUG}"
PLIST_FILE="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
mkdir -p "$LOGS_DIR"

# ── PID utility functions ──

read_pid() {
    [ -f "$PID_FILE" ] && cat "$PID_FILE"
}

read_supervisor_pid() {
    [ -f "$SUPERVISOR_PID_FILE" ] && cat "$SUPERVISOR_PID_FILE"
}

is_running() {
    local pid
    pid="$(read_pid)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

is_supervisor_running() {
    local pid
    pid="$(read_supervisor_pid)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

cleanup_pid() {
    rm -f "$PID_FILE" 2>/dev/null || true
}

# True iff /proc/<pid>/cmdline is exactly a telegram_bot bot for THIS project
# root: a `telegram_bot` argv token plus a `--path` token whose immediately
# following argument equals PROJECT_ROOT *literally*. NUL-delimited exact
# comparison — no regex — so paths with ERE metacharacters (. + ( ) space …)
# can neither false-match nor be missed.
_cmdline_is_project_bot() {
    local file="$1" arg has_module=0 path_match=0
    local -a argv=()
    while IFS= read -r -d '' arg; do argv+=("$arg"); done < "$file" 2>/dev/null
    local i
    for ((i = 0; i < ${#argv[@]}; i++)); do
        [ "${argv[i]}" = "telegram_bot" ] && has_module=1
        if [ "${argv[i]}" = "--path" ] \
            && [ "$((i + 1))" -lt "${#argv[@]}" ] \
            && [ "${argv[$((i + 1))]}" = "$PROJECT_ROOT" ]; then
            path_match=1
        fi
    done
    [ "$has_module" = 1 ] && [ "$path_match" = 1 ]
}

# /proc-less fallback (e.g. macOS): literal substring match on the ps argv
# string. `case` globs treat PROJECT_ROOT as a literal, so no regex injection;
# the trailing space/end anchor stops `/root` matching `/rootX`.
_ps_argv_is_project_bot() {
    local pid="$1" args
    args="$(ps -o args= -p "$pid" 2>/dev/null)" || return 1
    case "$args" in
        *"-m telegram_bot "*) : ;;
        *) return 1 ;;
    esac
    case "$args" in
        *"--path $PROJECT_ROOT") return 0 ;;
        *"--path $PROJECT_ROOT "*) return 0 ;;
        *) return 1 ;;
    esac
}

# PIDs of `python -m telegram_bot --path $PROJECT_ROOT` processes for THIS
# project root, regardless of pid-file state. Covers unmanaged instances whose
# pid file was lost (pid-file race between concurrent instances) or never
# written — the same fallback the fleet watchdogs already use, so --status /
# --stop and the watchdogs agree on what "running" means.
#
# pgrep -f gathers candidates by a metacharacter-free literal prefix; the exact
# owner is then confirmed against /proc/<pid>/cmdline (NUL-delimited), because
# inserting PROJECT_ROOT raw into the pgrep ERE mis-judged metacharacter paths
# and could self-inflict a getUpdates Conflict by launching a second instance.
find_project_bot_pids() {
    local pid
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        if [ -r "/proc/$pid/cmdline" ]; then
            _cmdline_is_project_bot "/proc/$pid/cmdline" && printf '%s\n' "$pid"
        else
            _ps_argv_is_project_bot "$pid" && printf '%s\n' "$pid"
        fi
    done < <(pgrep -f -- "-m telegram_bot --path" 2>/dev/null || true)
}

# Print the direct parent pid of "$1". Linux /proc is preferred because it
# avoids locale/formatting differences; ps keeps the guard available on macOS
# and other /proc-less hosts. Failure is conservative: callers simply fall
# through to the existing lifecycle guards.
_parent_pid_of() {
    local pid="$1" ppid=""
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    if [ -r "/proc/$pid/status" ]; then
        ppid="$(awk '$1 == "PPid:" { print $2; exit }' "/proc/$pid/status" 2>/dev/null)"
    else
        ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | awk 'NR == 1 { print $1 }')"
    fi
    case "$ppid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    printf '%s\n' "$ppid"
}

# True when process "$1" is "$2" or descends from it. Bound the walk so a
# corrupt/test process table cannot loop forever.
_pid_descends_from() {
    local current="$1" ancestor="$2" parent hops=0
    case "$current" in ''|*[!0-9]*) return 1 ;; esac
    case "$ancestor" in ''|*[!0-9]*) return 1 ;; esac
    while [ "$current" -gt 0 ] && [ "$hops" -lt 128 ]; do
        [ "$current" = "$ancestor" ] && return 0
        parent="$(_parent_pid_of "$current")" || return 1
        [ "$parent" = "$current" ] && return 1
        current="$parent"
        hops=$((hops + 1))
    done
    return 1
}

# Print the target bridge ancestor when this start.sh is itself running below
# the live bot/supervisor. An in-turn Claude/Codex Bash call has exactly this
# shape: bridge -> provider -> shell -> start.sh. Letting --restart reach
# do_stop would terminate the restart driver before its start/readiness half,
# leaving Telegram offline (#706).
restart_caller_bridge_ancestor() {
    local caller="${BASHPID:-$$}" pid
    for pid in "$(read_pid 2>/dev/null || true)" \
               "$(read_supervisor_pid 2>/dev/null || true)" \
               $(find_project_bot_pids); do
        case "$pid" in
            ''|*[!0-9]*) continue ;;
        esac
        kill -0 "$pid" 2>/dev/null || continue
        if _pid_descends_from "$caller" "$pid"; then
            printf '%s\n' "$pid"
            return 0
        fi
    done
    return 1
}

# Terminate competing project-bot pollers for this PROJECT_ROOT — other
# `python -m telegram_bot --path $PROJECT_ROOT` processes that are NOT the pid
# passed in "$1" (the still-live current child, if any) and NOT the supervisor
# itself. The daemon supervisor calls this before an auto-restart: a crash
# caused by a Telegram getUpdates 409 Conflict means a stray/second poller
# still holds the token, so without clearing it the relaunch 409s again and the
# supervisor burns the rapid-crash budget and gives up, leaving the bot down.
# No-op when no competitor exists (the common case for an ordinary crash).
reap_competing_pollers() {
    local keep="${1:-}" pid self_sup
    self_sup="$(read_supervisor_pid 2>/dev/null || true)"
    for pid in $(find_project_bot_pids); do
        [ -n "$keep" ] && [ "$pid" = "$keep" ] && continue
        [ -n "$self_sup" ] && [ "$pid" = "$self_sup" ] && continue
        echo "🧹 Clearing competing bot poller (PID: $pid) to resolve token conflict"
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 5); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    done
    return 0
}

cleanup_supervisor_pid() {
    rm -f "$SUPERVISOR_PID_FILE" 2>/dev/null || true
}

print_component_status() {
    local component="$1"
    local state="$2"
    local detail="$3"
    printf '   %s: %s' "$component" "$state"
    if [ -n "$detail" ]; then
        printf ' (%s)' "$detail"
    fi
    printf '\n'
}

configured_agent_provider() {
    local provider="${CCC_AGENT_PROVIDER:-}"
    if [ -z "$provider" ]; then
        provider="$(read_env_with_fallback "CCC_AGENT_PROVIDER")"
    fi
    if [ "$(printf '%s' "$provider" | tr '[:upper:]' '[:lower:]')" = "codex" ]; then
        echo "codex"
    else
        echo "claude"
    fi
}

configured_agent_label() {
    if [ "$(configured_agent_provider)" = "codex" ]; then
        echo "Codex"
    else
        echo "Claude"
    fi
}

render_status_from_health() {
    # The staleness threshold, component state → icon mapping, and elapsed-time
    # formatting are single-sourced in bridge/utils/health_render.py (#455). It
    # is run by path with the system python3 (no venv/import required) so the
    # --status fallback behavior is unchanged.
    python3 "$SCRIPT_DIR/utils/health_render.py" "$1" "$2" "$3" "$4"
}

# ── Action handlers ──

# MainPID of the active systemd service that manages THIS bridge, if any (empty
# when no systemd unit owns it, systemctl is unavailable, or the unit is
# inactive). Delegates the scope/unit-name/ownership rules to service-systemd.sh
# so --status agrees with --restart on what "managed" means.
service_managed_main_pid() {
    local main_pid
    main_pid="$("$SCRIPT_DIR/service-systemd.sh" main-pid 2>/dev/null)" || return 1
    [ -n "$main_pid" ] && [ "$main_pid" != "0" ] || return 1
    printf '%s\n' "$main_pid"
}

# Shared fallback for do_status when the pid file is missing or stale: a bot
# process for this project may still be alive (unmanaged — e.g. its pid file
# was deleted by a dying concurrent instance, or it was started foreground in
# an ssh session). Report it instead of declaring the bot dead.
report_unmanaged_or_dead() {
    local reason="$1" live_pids agent_label managed_pid
    agent_label="$(configured_agent_label)"
    live_pids="$(find_project_bot_pids | tr '\n' ' ' | sed 's/ $//')"
    if [ -n "$live_pids" ]; then
        # Reconcile with the service manager first: if the live project bot is
        # the active systemd MainPID, the bot is genuinely service-managed and
        # healthy — its pid file was merely lost to the concurrent-instance race
        # (see find_project_bot_pids). Report the true health snapshot as
        # "available" instead of "degraded" so fleet watchdogs and operators are
        # not misled by a bookkeeping gap the service manager already covers.
        managed_pid="$(service_managed_main_pid 2>/dev/null || true)"
        if [ -n "$managed_pid" ] && kill -0 "$managed_pid" 2>/dev/null \
            && printf ' %s ' "$live_pids" | grep -q " $managed_pid "; then
            render_status_from_health \
                "$HEALTH_FILE" "$managed_pid" "$HEALTH_STALE_SECONDS" \
                "$(configured_agent_provider)"
            return 0
        fi
        echo "🟡 Bot status: degraded"
        print_component_status "Process" "alive" "unmanaged PID(s): $live_pids ($reason)"
        print_component_status "Service" "degraded" "running without pid file; not recoverable by --status/--stop bookkeeping"
        echo "💡 Recover: $0 --path \"$PROJECT_ROOT\" --stop && $0 --path \"$PROJECT_ROOT\" --daemon"
        return 0
    fi
    echo "🔴 Bot status: unavailable"
    print_component_status "Process" "dead" "$reason"
    print_component_status "Service" "unavailable" "process not running"
    print_component_status "Telegram" "unavailable" "process not running"
    print_component_status "$agent_label" "unavailable" "process not running"
}

do_status() {
    local pid
    pid="$(read_pid)"
    if [ -z "$pid" ]; then
        report_unmanaged_or_dead "no PID file"
        exit 0
    fi
    if kill -0 "$pid" 2>/dev/null; then
        render_status_from_health \
            "$HEALTH_FILE" "$pid" "$HEALTH_STALE_SECONDS" "$(configured_agent_provider)"
    else
        cleanup_pid
        report_unmanaged_or_dead "stale PID: $pid"
    fi
    exit 0
}

do_stop() {
    local pid supervisor_pid upid
    local stopped_service=0
    local unmanaged_stopped=0
    supervisor_pid="$(read_supervisor_pid)"
    pid="$(read_pid)"

    if [ -f "$PLIST_FILE" ]; then
        echo "🛑 Stopping launchd service: $PLIST_LABEL..."
        launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || launchctl unload "$PLIST_FILE" 2>/dev/null || true
        stopped_service=1
        sleep 1
    fi

    if [ -n "$supervisor_pid" ] && kill -0 "$supervisor_pid" 2>/dev/null; then
        echo "🛑 Stopping daemon supervisor (PID: $supervisor_pid)..."
        kill "$supervisor_pid"
        for i in $(seq 1 10); do
            kill -0 "$supervisor_pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$supervisor_pid" 2>/dev/null; then
            echo "⚠️  Supervisor not responding to SIGTERM, sending SIGKILL..."
            kill -9 "$supervisor_pid" 2>/dev/null
            sleep 0.5
        fi
    fi

    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "🛑 Stopping bot process (PID: $pid)..."
        kill "$pid"
        for i in $(seq 1 10); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "⚠️  Bot process not responding to SIGTERM, sending SIGKILL..."
            kill -9 "$pid" 2>/dev/null
            sleep 0.5
        fi
    fi

    # Also stop unmanaged instances for this project root (pid file lost or
    # never written) — otherwise --stop reports "not running" while a live
    # bot keeps holding the Telegram token.
    for upid in $(find_project_bot_pids); do
        [ -n "$pid" ] && [ "$upid" = "$pid" ] && continue
        [ -n "$supervisor_pid" ] && [ "$upid" = "$supervisor_pid" ] && continue
        echo "🛑 Stopping unmanaged bot process (PID: $upid)..."
        kill "$upid" 2>/dev/null || true
        for i in $(seq 1 10); do
            kill -0 "$upid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$upid" 2>/dev/null; then
            echo "⚠️  Unmanaged bot process not responding to SIGTERM, sending SIGKILL..."
            kill -9 "$upid" 2>/dev/null
            sleep 0.5
        fi
        unmanaged_stopped=1
    done

    cleanup_pid
    cleanup_supervisor_pid
    cleanup_token_lock_if_safe "$supervisor_pid" "$pid"
    if [ "$stopped_service" -eq 1 ] || [ -n "$supervisor_pid" ] || [ -n "$pid" ] || [ "$unmanaged_stopped" -eq 1 ]; then
        echo "✅ Bot stopped"
    else
        echo "⚪ Bot is not running"
    fi
    exit 0
}

read_env_value() {
    local key="$1"
    local file="${2:-$ENV_FILE}"
    [ -f "$file" ] || return 0
    local line
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$file" | tail -n1)"
    [ -n "$line" ] || return 0
    local value="${line#*=}"
    value="$(printf '%s' "$value" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    # Strip trailing inline comment (common pattern: KEY=value # comment)
    value="${value%% \#*}"
    value="$(printf '%s' "$value" | sed -E 's/[[:space:]]+$//')"
    echo "$value"
}

upsert_env_value() {
    local key="$1"
    local value="$2"
    local file="$3"
    local tmp_file

    tmp_file="$(mktemp "${file}.tmp.XXXXXX")" || return 1

    if awk -v key="$key" -v value="$value" '
        BEGIN { updated = 0 }
        $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
            if (!updated) {
                print key "=" value
                updated = 1
            }
            next
        }
        { print }
        END {
            if (!updated) {
                print key "=" value
            }
        }
    ' "$file" > "$tmp_file"; then
        mv "$tmp_file" "$file"
        return 0
    fi

    rm -f "$tmp_file"
    return 1
}

_is_valid_token() {
    [ -n "$1" ] && [ "$1" != "your_bot_token_here" ]
}

# Read a config value from project .env, falling back to bot source dir .env
# NOTE: For comprehensive env merging at startup, merge_env_files() is preferred
read_env_with_fallback() {
    local key="$1"
    local value
    value="$(read_env_value "$key")"
    if [ -z "$value" ]; then
        value="$(read_env_value "$key" "$SCRIPT_DIR/.env")"
    fi
    echo "$value"
}

# Merge project .env with global fallback .env
# Project .env values take precedence over global .env
merge_env_files() {
    local project_env="$ENV_FILE"
    local global_env="$SCRIPT_DIR/.env"

    if [ ! -f "$global_env" ]; then
        return
    fi

    # Read all keys from global .env
    local keys key value project_value
    keys=$(grep -E "^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=" "$global_env" 2>/dev/null | \
           sed -E 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*).*/\2/' | sort -u)

    for key in $keys; do
        # Check if key exists in project .env
        project_value="$(read_env_value "$key" "$project_env")"
        if [ -z "$project_value" ]; then
            # Not in project .env, get from global and export
            value="$(read_env_value "$key" "$global_env")"
            if [ -n "$value" ]; then
                export "$key=$value"
            fi
        fi
    done
}

# ── Ensure project config exists and validate required settings ──
check_env() {
    if [ ! -f "$ENV_FILE" ]; then
        if [ -f "$ENV_EXAMPLE_FILE" ]; then
            echo "📝 Creating project config file..."
            cp "$ENV_EXAMPLE_FILE" "$ENV_FILE"
            echo "✅ Created: $ENV_FILE"
        else
            echo "❌ Error: config template not found: $ENV_EXAMPLE_FILE"
            exit 1
        fi
    fi

    local bot_token
    bot_token="$(read_env_value "TELEGRAM_BOT_TOKEN")"
    if _is_valid_token "$bot_token"; then
        return
    fi

    # Fallback: check bot source dir .env
    bot_token="$(read_env_value "TELEGRAM_BOT_TOKEN" "$SCRIPT_DIR/.env")"
    if _is_valid_token "$bot_token"; then
        echo "ℹ️  Using TELEGRAM_BOT_TOKEN from $SCRIPT_DIR/.env (fallback)"
        return
    fi

    # No valid token anywhere — guide user
    echo ""
    echo "⚠️  TELEGRAM_BOT_TOKEN is not configured"
    echo "Open Telegram, search @BotFather, send /newbot to create a bot and get the token."
    echo ""
    if [ -t 0 ]; then
        printf "Enter Bot Token: "
        read -r INPUT_TOKEN
        if [ -z "$INPUT_TOKEN" ]; then
            echo "❌ Token cannot be empty. Please re-run and enter a valid token."
            exit 1
        fi
        if ! upsert_env_value "TELEGRAM_BOT_TOKEN" "$INPUT_TOKEN" "$ENV_FILE"; then
            echo "❌ Failed to save token to $ENV_FILE"
            exit 1
        fi
        echo "✅ Token saved to $ENV_FILE"
        echo ""
        echo "💡 To configure optional settings (ALLOWED_USER_IDS, PROXY_URL, etc.), edit:"
        echo "   $ENV_FILE"
        echo ""
    else
        echo "Please edit the config file and set TELEGRAM_BOT_TOKEN:"
        echo "   $ENV_FILE"
        echo ""
        echo "💡 See comments in the file for optional settings. Re-run after configuration."
        exit 1
    fi
}

# ── Startup-service install/uninstall (extracted subcommands, #584 P3-2) ──
# The plist/unit generation and loader machinery lives in service-launchd.sh
# and service-systemd.sh so it is testable in isolation (see
# service-install.test.sh). Pre-flight guards that depend on start.sh state
# (check_env, running-instance and token-lock checks) stay here; the
# subcommands receive everything else via explicit flags/env:
#   --project-root  validated $PROJECT_ROOT
#   --proxy-url     PROXY_URL resolved through read_env_with_fallback (project
#                   .env first, then bot source dir .env — same order as the
#                   previous inline implementation)
#   --caller        $0, so user-facing hints keep the invoked script name
#   CCC_BRIDGE_TOKEN_LOCK_FILE  env: token-lock path from init_token_lock, so
#                   uninstall can clear a stale lock safely

do_install() {
    check_env
    init_token_lock
    # Refuse if an instance is already running (any startup mode)
    if is_running || is_supervisor_running; then
        echo "⚠️  Bot is already running. Use --stop first."
        exit 1
    fi
    if is_token_locked; then
        echo "⚠️  Another instance is already using the same Bot Token (PID: $(cat "$TOKEN_LOCK_FILE")). Stop it first."
        exit 1
    fi
    exec "$SCRIPT_DIR/service-launchd.sh" install \
        --project-root "$PROJECT_ROOT" \
        --proxy-url "$(read_env_with_fallback "PROXY_URL")" \
        --caller "$0"
}

do_uninstall() {
    init_token_lock
    export CCC_BRIDGE_TOKEN_LOCK_FILE="$TOKEN_LOCK_FILE"
    exec "$SCRIPT_DIR/service-launchd.sh" uninstall \
        --project-root "$PROJECT_ROOT" \
        --caller "$0"
}

# ── Linux systemd startup service (reboot-persistent) ──
# Mirrors do_install for Linux nodes, where `start.sh --install` (launchd)
# does not apply. Unit generation and systemctl handling live in
# service-systemd.sh (see its header for the unit semantics and the
# BRIDGE_SERVICE_NAME / CCC_SYSTEMD_DIR / CCC_SYSTEMCTL contracts).

do_install_systemd() {
    # Keep the systemd-availability hint BEFORE check_env so macOS users get
    # "use --install instead" without being walked through token setup first.
    if ! command -v "${CCC_SYSTEMCTL:-systemctl}" >/dev/null 2>&1; then
        echo "❌ systemctl not found — this host does not use systemd. On macOS use --install instead."
        exit 1
    fi
    check_env
    exec "$SCRIPT_DIR/service-systemd.sh" install \
        --project-root "$PROJECT_ROOT" \
        --proxy-url "$(read_env_with_fallback "PROXY_URL")" \
        --caller "$0"
}

do_uninstall_systemd() {
    exec "$SCRIPT_DIR/service-systemd.sh" uninstall --caller "$0"
}

do_version() {
    local current
    if ! current="$(get_checkout_version)"; then
        echo "❌ Unable to derive ccc-node checkout identity" >&2
        exit 1
    fi
    echo "ccc-node checkout: $current"
    exit 0
}

do_upgrade() {
    local updater="$REPO_ROOT/scripts/ccc-self-update.sh"
    local current rc

    if [ ! -x "$updater" ]; then
        echo "❌ Canonical updater is missing or not executable: $updater" >&2
        exit 1
    fi
    if ! validate_canonical_origin; then
        echo "❌ Refusing update: checkout origin is not canonical jinwon-int/ccc-node" >&2
        exit 4
    fi

    echo "🔄 Running canonical ccc-node updater..."
    if CCC_SELF_UPDATE_REPO="$REPO_ROOT" \
        CCC_SELF_UPDATE_BRANCH="main" \
        "$updater" run; then
        if ! current="$(get_checkout_version)"; then
            echo "❌ Update completed but installed checkout identity is unavailable" >&2
            exit 1
        fi
        echo "✅ Upgrade complete — installed checkout: $current"
        exit 0
    else
        rc=$?
        echo "❌ Canonical updater did not complete (exit $rc)" >&2
        exit "$rc"
    fi
}

# ── Token-based global lock (prevents duplicate instances across different project dirs) ──
TOKEN_LOCK_FILE=""

init_token_lock() {
    if [ -n "$TOKEN_LOCK_FILE" ]; then
        return 0
    fi
    local raw_token token_hash
    raw_token="$(read_env_with_fallback "TELEGRAM_BOT_TOKEN")"
    token_hash="$(printf '%s' "$raw_token" | md5 -q 2>/dev/null || printf '%s' "$raw_token" | md5sum | cut -d' ' -f1)"
    TOKEN_LOCK_DIR="$HOME/.telegram-bot-locks"
    TOKEN_LOCK_FILE="$TOKEN_LOCK_DIR/${token_hash}.pid"
    mkdir -p "$TOKEN_LOCK_DIR"
}

is_token_locked() {
    init_token_lock
    [ -f "$TOKEN_LOCK_FILE" ] || return 1
    local lock_pid
    lock_pid="$(cat "$TOKEN_LOCK_FILE")"
    [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null
}

write_token_lock() {
    init_token_lock
    printf '%s\n' "$1" > "$TOKEN_LOCK_FILE"
}

cleanup_token_lock() {
    if [ -z "$TOKEN_LOCK_FILE" ]; then
        init_token_lock
    fi
    [ -n "$TOKEN_LOCK_FILE" ] && rm -f "$TOKEN_LOCK_FILE"
}

cleanup_token_lock_if_safe() {
    local expected_pid_1="$1"
    local expected_pid_2="$2"
    local lock_pid

    if [ -z "$TOKEN_LOCK_FILE" ]; then
        init_token_lock
    fi
    [ -n "$TOKEN_LOCK_FILE" ] || return 0
    [ -f "$TOKEN_LOCK_FILE" ] || return 0

    lock_pid="$(cat "$TOKEN_LOCK_FILE" 2>/dev/null)"
    if [ -z "$lock_pid" ]; then
        cleanup_token_lock
        return 0
    fi

    if [ -n "$expected_pid_1" ] && [ "$lock_pid" = "$expected_pid_1" ]; then
        cleanup_token_lock
        return 0
    fi
    if [ -n "$expected_pid_2" ] && [ "$lock_pid" = "$expected_pid_2" ]; then
        cleanup_token_lock
        return 0
    fi
    if ! kill -0 "$lock_pid" 2>/dev/null; then
        cleanup_token_lock
    fi
}

# ── Atomic restart (--restart) ──
# First-class stop→start→verify so rollouts never depend on ad-hoc
# `--stop && start &` compositions (2026-07-19 fleet rollout: a detached
# restart silently never executed; the wrong checkout's start.sh was invoked;
# pre-flight guards were bypassed so token resolution failed after the stop).
# Reuses the existing code paths only: pre-flight via check_env, stop via
# do_stop, start via THIS checkout's start.sh (which runs the normal start
# guards itself), readiness via the do_status internals.
#
# Exit codes:
#   0  restart verified — --status reports "available"
#   1  stop-failed        (old process refuses to exit within the bound)
#   2  start-failed       (start invocation failed / process died before ready)
#   3  supervisor-managed (systemd/launchd owns the bridge; restart it there)
#   4  not-available-within-timeout
#   5  self-invoked       (caller is inside the target bridge process tree)
#
# Test seams (defaults preserve production behavior):
#   CCC_BRIDGE_RESTART_STOP_TIMEOUT   seconds to wait for old-process exit (15)
#   CCC_BRIDGE_RESTART_READY_TIMEOUT  seconds to wait for "available" (45)
#   CCC_BRIDGE_RESTART_SPAWN          start command override (this start.sh)

RESTART_OLD_PID=""
RESTART_OLD_SUPERVISOR_PID=""

restart_status_snapshot() {
    # do_status exits, so run it in a subshell to reuse it as a probe.
    ( do_status ) 2>/dev/null
}

restart_live_old_pids() {
    local pid
    for pid in "${RESTART_OLD_PID:-}" "${RESTART_OLD_SUPERVISOR_PID:-}"; do
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && printf '%s\n' "$pid"
    done
    find_project_bot_pids
}

do_restart() {
    local stop_timeout="${CCC_BRIDGE_RESTART_STOP_TIMEOUT:-15}"
    local ready_timeout="${CCC_BRIDGE_RESTART_READY_TIMEOUT:-45}"
    local spawn_cmd="${CCC_BRIDGE_RESTART_SPAWN:-$SCRIPT_DIR/start.sh}"
    local waited live ready new_pid spawn_pid="" restart_log="" unit scope_flag
    local caller_ancestor="" daemon_hint=""

    # Refuse before ANY destructive lifecycle action when this restart driver
    # is a descendant of the bridge it would stop. This is intentionally before
    # the service-manager probe: an installed-but-inactive systemd unit can
    # coexist with a detached serving bridge, which is the exact nosuk incident
    # topology from #706.
    caller_ancestor="$(restart_caller_bridge_ancestor 2>/dev/null || true)"
    if [ -n "$caller_ancestor" ]; then
        unit="${BRIDGE_SERVICE_NAME:-ccc-telegram-bridge}.service"
        scope_flag=""
        [ "$(id -u)" = "0" ] || scope_flag=" --user"
        [ "$DAEMON_MODE" -eq 1 ] && daemon_hint=" -d"
        echo "⚠️  Restart refused: this command is running inside the target bridge process tree."
        echo "   owner=target-bridge caller=descendant action=refused-before-stop"
        echo "   Stopping it here would terminate the restart driver before start/readiness verification."
        echo "💡 Re-run from a shell outside the bridge tree:"
        echo "   systemctl${scope_flag} restart $unit    # systemd installation"
        echo "   $0 --path \"$PROJECT_ROOT\" --restart${daemon_hint}    # unmanaged installation"
        exit 5
    fi

    # Supervisor-managed bridges: restarting underneath launchd KeepAlive or
    # systemd Restart=always causes supervisor fights. Conservative check —
    # refuse only when the manager clearly owns it.
    if [ -f "$PLIST_FILE" ]; then
        echo "⚠️  Bridge is managed by launchd ($PLIST_LABEL) — not restarting it here."
        echo "💡 Restart via the service manager: launchctl kickstart -k gui/$(id -u)/$PLIST_LABEL"
        exit 3
    fi
    if "$SCRIPT_DIR/service-systemd.sh" is-managed >/dev/null 2>&1; then
        unit="${BRIDGE_SERVICE_NAME:-ccc-telegram-bridge}.service"
        scope_flag=""
        [ "$(id -u)" = "0" ] || scope_flag=" --user"
        echo "⚠️  Bridge is managed by systemd (active unit: $unit) — not restarting it here."
        echo "💡 Restart via the service manager: systemctl${scope_flag} restart $unit"
        exit 3
    fi

    # Pre-flight BEFORE touching the running bot: fail while the old instance
    # is still up rather than after the stop (the 2026-07-19 mode where the
    # bot stayed down because the start half could not resolve its token).
    merge_env_files
    check_env

    RESTART_OLD_PID="$(read_pid)"
    RESTART_OLD_SUPERVISOR_PID="$(read_supervisor_pid)"
    if [ -z "$RESTART_OLD_PID" ]; then
        RESTART_OLD_PID="$(find_project_bot_pids | head -n1)"
    fi
    echo "🔁 Restarting bridge (old PID: ${RESTART_OLD_PID:-none})"

    # Stop: the exact --stop code path (launchd bootout, supervisor, managed
    # and unmanaged processes, token-lock cleanup). Subshell because do_stop
    # exits.
    if ! ( do_stop ); then
        echo "❌ Restart failed: stop-failed (--stop path exited nonzero)"
        exit 1
    fi

    # Bounded wait for the old process(es) to actually exit.
    waited=0
    live="$(restart_live_old_pids | tr '\n' ' ' | sed 's/ $//')"
    while [ -n "$live" ] && [ "$waited" -lt "$stop_timeout" ]; do
        sleep 1
        waited=$((waited + 1))
        live="$(restart_live_old_pids | tr '\n' ' ' | sed 's/ $//')"
    done
    if [ -n "$live" ]; then
        echo "❌ Restart failed: stop-failed — old process refuses to exit after ${stop_timeout}s (PID(s): $live)"
        echo "   Not starting a new instance on top of it."
        exit 1
    fi

    # Start via the same code paths the plain flags use, pinned to THIS
    # checkout's start.sh (a wrong-checkout start.sh was a 2026-07-19 mode).
    local spawn_args=("--path" "$PROJECT_ROOT")
    [ -n "$BOT_DEBUG" ] && spawn_args+=("--debug")
    if [ "$DAEMON_MODE" -eq 1 ]; then
        # Same path as `start.sh --path <p> --daemon`.
        if ! "$spawn_cmd" "${spawn_args[@]}" --daemon; then
            echo "❌ Restart failed: start-failed (daemon start exited nonzero)"
            exit 2
        fi
    else
        # Same path as a plain foreground `start.sh --path <p>` run (its own
        # pre-flight guards, prepare_runtime, exec into the bot). Detached
        # from this terminal so the verified bridge survives the restart
        # command exiting; output goes to the restart log.
        restart_log="$LOGS_DIR/restart.log"
        nohup "$spawn_cmd" "${spawn_args[@]}" >> "$restart_log" 2>&1 &
        spawn_pid=$!
        echo "🚀 Starting bridge (spawn PID: $spawn_pid, log: $restart_log)"
    fi

    # Bounded readiness verification: poll the --status internals until the
    # health snapshot renders "available".
    waited=0
    ready=0
    while [ "$waited" -lt "$ready_timeout" ]; do
        if restart_status_snapshot | grep -q "Bot status: available"; then
            ready=1
            break
        fi
        if [ -n "$spawn_pid" ] && ! kill -0 "$spawn_pid" 2>/dev/null && ! is_running; then
            echo "❌ Restart failed: start-failed (spawned process exited before becoming available; see $restart_log)"
            exit 2
        fi
        sleep 1
        waited=$((waited + 1))
    done

    if [ "$ready" -ne 1 ]; then
        echo "❌ Restart failed: not-available-within-timeout (${ready_timeout}s)"
        echo "── last status ──"
        restart_status_snapshot
        echo "💡 The new process (if any) was left running — inspect with: $0 --path \"$PROJECT_ROOT\" --status"
        exit 4
    fi

    new_pid="$(read_pid)"
    echo "✅ Restart verified: bot available (old PID: ${RESTART_OLD_PID:-none} → new PID: ${new_pid:-unknown})"
    echo "── health ──"
    restart_status_snapshot
    exit 0
}

# ── Dispatch action ──

case "$ACTION" in
    status)    do_status ;;
    stop)      do_stop ;;
    restart)   do_restart ;;
    install)           do_install ;;
    uninstall)         do_uninstall ;;
    install-systemd)   do_install_systemd ;;
    uninstall-systemd) do_uninstall_systemd ;;
    upgrade)           do_upgrade ;;
    version)           do_version ;;
    reap-competing-pollers) reap_competing_pollers ""; exit 0 ;;
    run)       ;; # Continue to startup flow below
esac

# Check for updates (skip if running upgrade action)
[ "$ACTION" = "run" ] && [ "$INTERNAL_RUN" -eq 0 ] && check_update

load_optional_env() {
    local env_cli
    env_cli="$(read_env_with_fallback "CLAUDE_CLI_PATH")"
    if [ -n "$env_cli" ] && [ -z "$CLAUDE_CLI_PATH" ]; then
        export CLAUDE_CLI_PATH="$env_cli"
    fi

    local proxy_url
    proxy_url="$(read_env_with_fallback "PROXY_URL")"
    if [ -n "$proxy_url" ]; then
        export http_proxy="$proxy_url"
        export https_proxy="$proxy_url"
        export all_proxy="$proxy_url"
        export no_proxy="localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"
        echo "🌐 Proxy configured: $proxy_url"
    fi
}

maybe_setup_agent_cli() {
    local provider codex_cli
    provider="$(read_env_with_fallback "CCC_AGENT_PROVIDER")"
    provider="${provider:-claude}"

    case "${provider,,}" in
        codex)
            codex_cli="$(read_env_with_fallback "CCC_CODEX_CLI_PATH")"
            codex_cli="${codex_cli:-codex}"
            if [[ "$codex_cli" == */* ]]; then
                if [ ! -f "$codex_cli" ] || [ ! -x "$codex_cli" ]; then
                    echo "❌ Error: configured Codex CLI is not executable"
                    exit 1
                fi
            elif ! command -v "$codex_cli" >/dev/null 2>&1; then
                echo "❌ Error: Codex CLI not found. Install Codex CLI or set CCC_CODEX_CLI_PATH in .env"
                exit 1
            fi
            echo "✅ Codex provider CLI is available"
            return
            ;;
        claude)
            ;;
        *)
            echo "❌ Error: unsupported CCC_AGENT_PROVIDER (expected claude or codex)"
            exit 1
            ;;
    esac

    if [ -n "$CLAUDE_CLI_PATH" ]; then
        echo "🛠️ Using user-specified CLAUDE_CLI_PATH: $CLAUDE_CLI_PATH"
        return
    fi

    if command -v claude >/dev/null 2>&1; then
        echo "✅ Using system Claude CLI: $(command -v claude)"
    else
        echo "❌ Error: claude command not found. Please install Claude CLI or set CLAUDE_CLI_PATH in .env"
        exit 1
    fi
}

prepare_runtime() {
    load_optional_env
    maybe_setup_agent_cli

    if ! command -v python3 >/dev/null 2>&1; then
        echo "❌ Error: Python 3.11+ is required"
        exit 1
    fi

    ensure_venv
    sync_dependencies 0

    echo "✅ Activating virtual environment"
    . "$VENV_DIR/bin/activate"

    CLEANUP_MARKER="$BOT_DATA_DIR/.last_cleanup"
    if [ -d "$LOGS_DIR" ]; then
        if [ ! -f "$CLEANUP_MARKER" ] || [ -n "$(find "$CLEANUP_MARKER" -mtime +1 2>/dev/null)" ]; then
            echo -e "\033[90m🧹 Cleaning up logs older than 14 days...\033[0m"
            find "$LOGS_DIR" -name "*.log" -mtime +14 -delete 2>/dev/null
            touch "$CLEANUP_MARKER"
        fi
    fi

    cd "$REPO_ROOT"
}

exec_bot_once() {
    export BOT_PROCESS_MODE="$PROCESS_MODE"
    export BOT_TOKEN_LOCK_FILE="$TOKEN_LOCK_FILE"
    export BOT_OWNS_TOKEN_LOCK="1"
    write_token_lock "$$"
    # Write PID file so `--status` works in foreground/systemd mode.
    # `exec` replaces the current shell with the Python process, keeping the
    # same PID ($$), so this file stays valid after exec.  do_status() already
    # handles stale PIDs via kill-0 + cleanup_pid, so no extra cleanup is needed.
    echo $$ > "$PID_FILE"

    _acquire_platform_wakelock

    echo ""
    echo "🚀 Starting Telegram Bot..."
    echo "================================"

    if [ -n "$BOT_DEBUG" ]; then
        exec "$VENV_DIR/bin/python" -m telegram_bot --path "$PROJECT_ROOT" --debug
    fi
    exec "$VENV_DIR/bin/python" -m telegram_bot --path "$PROJECT_ROOT"
}

# On Termux (Android), hold a wake lock so Doze / battery optimisation does not
# suspend the process (and its network) when the screen is off. Without it the
# long-lived Claude SDK stream and Telegram polling drop during idle periods and
# sessions end early. Idempotent; a no-op on non-Termux hosts. We only acquire
# (never auto-release) so a lock shared with other Termux services (e.g. sshd via
# ~/.termux/boot) is never dropped when the bridge stops.
_acquire_platform_wakelock() {
    if command -v termux-wake-lock >/dev/null 2>&1; then
        termux-wake-lock >/dev/null 2>&1 || true
        echo "🔒 Termux wake lock held (prevents Doze from suspending the bot)"
    fi
}

run_daemon_supervisor() {
    # Crash/rapid-restart policy — single source shared with the in-process
    # guard (core/bot_lifecycle.py via core/crash_policy.py). Sourcing
    # crash-policy.env keeps this process-supervisor layer and the in-process
    # layer from silently diverging (#445). Inline values below are a documented
    # fallback that mirrors the file for when it is unreadable.
    CCC_MAX_RAPID_CRASHES=5
    CCC_PROCESS_CRASH_WINDOW_SECONDS=60
    CCC_INPROCESS_MIN_UPTIME_SECONDS=30
    CCC_RESTART_DELAY_BASE_SECONDS=3
    CCC_RESTART_DELAY_MAX_SECONDS=30
    if [ -r "$SCRIPT_DIR/crash-policy.env" ]; then
        # shellcheck disable=SC1091
        . "$SCRIPT_DIR/crash-policy.env"
    fi
    # Export so the python child inherits the exact same numbers (env has highest
    # precedence in core.crash_policy).
    export CCC_MAX_RAPID_CRASHES CCC_PROCESS_CRASH_WINDOW_SECONDS \
        CCC_INPROCESS_MIN_UPTIME_SECONDS

    MAX_RAPID_CRASHES="$CCC_MAX_RAPID_CRASHES"
    RAPID_CRASH_WINDOW="$CCC_PROCESS_CRASH_WINDOW_SECONDS"
    RESTART_DELAY_BASE="$CCC_RESTART_DELAY_BASE_SECONDS"
    rapid_crash_count=0
    child_pid=""

    daemon_cleanup() {
        if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
            kill "$child_pid" 2>/dev/null || true
            wait "$child_pid" 2>/dev/null || true
        fi
        cleanup_supervisor_pid
        cleanup_token_lock
    }

    trap daemon_cleanup EXIT
    trap 'exit 143' TERM INT

    echo $$ > "$SUPERVISOR_PID_FILE"
    write_token_lock "$$"

    # Debug: log environment variables for daemon supervisor
    echo "DEBUG: VENV_DIR=$VENV_DIR" >> "$LOGS_DIR/supervisor.log"
    echo "DEBUG: PROJECT_ROOT=$PROJECT_ROOT" >> "$LOGS_DIR/supervisor.log"

    _acquire_platform_wakelock

    while true; do
        echo ""
        echo "🚀 Starting Telegram Bot..."
        echo "================================"

        start_time=$(date +%s)
        proxy_env_args=()
        [ -n "$http_proxy" ]  && proxy_env_args+=("http_proxy=$http_proxy")
        [ -n "$https_proxy" ] && proxy_env_args+=("https_proxy=$https_proxy")
        [ -n "$all_proxy" ]   && proxy_env_args+=("all_proxy=$all_proxy")
        [ -n "$no_proxy" ]    && proxy_env_args+=("no_proxy=$no_proxy")
        if [ -n "$BOT_DEBUG" ]; then
            BOT_PROCESS_MODE=daemon BOT_TOKEN_LOCK_FILE="$TOKEN_LOCK_FILE" BOT_OWNS_TOKEN_LOCK=0 \
                env "${proxy_env_args[@]}" \
                "$VENV_DIR/bin/python" -m telegram_bot --path "$PROJECT_ROOT" --debug &
        else
            BOT_PROCESS_MODE=daemon BOT_TOKEN_LOCK_FILE="$TOKEN_LOCK_FILE" BOT_OWNS_TOKEN_LOCK=0 \
                env "${proxy_env_args[@]}" \
                "$VENV_DIR/bin/python" -m telegram_bot --path "$PROJECT_ROOT" &
        fi
        child_pid=$!
        wait "$child_pid"
        exit_code=$?
        child_pid=""
        end_time=$(date +%s)

        if [ "$exit_code" -eq 0 ]; then
            echo "✅ Bot exited normally"
            break
        fi

        crash_log="$LOGS_DIR/crash_$(date +%Y%m%d_%H%M%S).log"
        {
            echo "=== Bot crashed ==="
            echo "Time: $(date '+%Y-%m-%d %H:%M:%S')"
            echo "Exit code: $exit_code"
            echo "Uptime: $((end_time - start_time)) seconds"
        } > "$crash_log"
        echo "❌ Bot crashed (exit code: $exit_code), log written to: $crash_log"

        if [ $((end_time - start_time)) -lt $RAPID_CRASH_WINDOW ]; then
            rapid_crash_count=$((rapid_crash_count + 1))
            echo "⚠️  Rapid crash ($rapid_crash_count/$MAX_RAPID_CRASHES)"
            if [ "$rapid_crash_count" -ge "$MAX_RAPID_CRASHES" ]; then
                echo "🛑 Rapid crash limit reached ($MAX_RAPID_CRASHES times), stopping restart"
                exit 1
            fi
        else
            rapid_crash_count=0
        fi

        restart_delay=$((RESTART_DELAY_BASE * (rapid_crash_count + 1)))
        if [ "$restart_delay" -gt "$CCC_RESTART_DELAY_MAX_SECONDS" ]; then
            restart_delay="$CCC_RESTART_DELAY_MAX_SECONDS"
        fi
        # The current child has already exited and been reaped (child_pid=""),
        # so any surviving project-bot poller is a stray/second instance holding
        # the Telegram token. Clear it before relaunching so a getUpdates 409
        # Conflict self-heals instead of crash-looping to the rapid-crash limit.
        reap_competing_pollers ""

        echo "🔄 Auto-restarting in ${restart_delay} seconds..."
        sleep "$restart_delay"
    done
}

# Merge env files before check_env so all config is available
merge_env_files

check_env
init_token_lock

if [ "$DAEMON_MODE" -eq 1 ] && [ "$RUN_AS_DAEMON_SUPERVISOR" -eq 0 ]; then
    if is_supervisor_running || is_running; then
        echo "⚠️  Bot is already running. Use --stop first to restart."
        exit 1
    fi
    if is_token_locked; then
        echo "⚠️  Another instance is already using the same Bot Token (PID: $(cat "$TOKEN_LOCK_FILE")). Stop it first."
        exit 1
    fi

    echo "🌙 Starting in daemon mode..."
    DAEMON_LOG="$LOGS_DIR/supervisor.log"
    SUPERVISOR_ARGS=("--path" "$PROJECT_ROOT" "--_daemon_supervisor")
    [ -n "$BOT_DEBUG" ] && SUPERVISOR_ARGS+=("--debug")
    nohup "$SCRIPT_DIR/start.sh" "${SUPERVISOR_ARGS[@]}" >> "$DAEMON_LOG" 2>&1 &
    SUPERVISOR_PID=$!
    echo "✅ Bot started in background (PID: $SUPERVISOR_PID)"
    echo "📄 Log: $DAEMON_LOG"
    echo "💡 Use $0 --path \"$PROJECT_ROOT\" --status to check status"
    echo "💡 Use $0 --path \"$PROJECT_ROOT\" --stop to stop"
    exit 0
fi

if [ "$RUN_AS_DAEMON_SUPERVISOR" -eq 1 ]; then
    prepare_runtime
    run_daemon_supervisor
    exit $?
fi

# Double-start guard for the plain foreground path (the daemon path above has
# its own). Managed instances are caught via pid files; unmanaged ones (pid
# file lost/never written) via the project-scoped process match — starting a
# second instance anyway ends in a Telegram getUpdates conflict where the
# loser's cleanup can delete the survivor's pid file.
if [ "$INTERNAL_RUN" -eq 0 ]; then
    if is_supervisor_running || is_running || [ -n "$(find_project_bot_pids)" ]; then
        echo "⚠️  Bot is already running. Use --stop first."
        exit 1
    fi
fi

if is_token_locked; then
    echo "⚠️  Another instance is already using the same Bot Token (PID: $(cat "$TOKEN_LOCK_FILE")). Stop it first."
    exit 1
fi

prepare_runtime
exec_bot_once
