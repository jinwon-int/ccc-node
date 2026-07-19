#!/bin/bash

# ccc-node Telegram bridge — macOS launchd startup-service subcommand.
#
# Extracted verbatim from bridge/start.sh do_install/do_uninstall (#584 P3-2)
# so the service-install machinery is testable in isolation. start.sh
# --install/--uninstall dispatches here after its own pre-flight guards
# (check_env, already-running and token-lock checks); this script only
# generates/removes the plist and drives launchctl. The generated plist is
# byte-identical to what start.sh produced before the extraction.
#
# Usage:
#   service-launchd.sh install   --project-root <dir> [--proxy-url <url>] [--caller <name>]
#   service-launchd.sh uninstall --project-root <dir> [--caller <name>]
#
# Inputs (resolved by start.sh when dispatched from there):
#   --project-root  absolute project path (was $PROJECT_ROOT)
#   --proxy-url     PROXY_URL resolved via read_env_with_fallback; empty = none
#   --caller        script name to print in user-facing hints (default:
#                   <this dir>/start.sh)
#   CCC_BRIDGE_TOKEN_LOCK_FILE  env: token-lock file path from start.sh's
#                   init_token_lock; empty = skip token-lock cleanup
#
# Test seam (default preserves production behavior):
#   CCC_LAUNCHCTL   launchctl command override (default: launchctl)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHCTL="${CCC_LAUNCHCTL:-launchctl}"

usage() {
    cat <<EOF
Usage: $0 <install|uninstall> --project-root <dir> [options]

Subcommands:
  install     Generate the launchd plist and bootstrap it
  uninstall   Stop the service and remove the plist

Options:
  --project-root <dir>  Project root directory (required)
  --proxy-url <url>     Proxy URL to embed in the launchd environment
  --caller <name>       Script name shown in usage hints
  -h, --help            Show this help message and exit
EOF
}

SUBCOMMAND=""
PROJECT_ROOT_ARG=""
PROXY_URL_ARG=""
CALLER=""

while [ $# -gt 0 ]; do
    case "$1" in
        install|uninstall)
            SUBCOMMAND="$1"
            shift
            ;;
        --project-root)
            PROJECT_ROOT_ARG="$2"
            shift 2
            ;;
        --proxy-url)
            PROXY_URL_ARG="$2"
            shift 2
            ;;
        --caller)
            CALLER="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "❌ Error: Unknown option: $1"
            usage >&2
            exit 1
            ;;
    esac
done

if [ -z "$SUBCOMMAND" ]; then
    usage >&2
    exit 1
fi
if [ -z "$PROJECT_ROOT_ARG" ]; then
    echo "❌ Error: --project-root is required"
    exit 1
fi
PROJECT_ROOT="$(cd "$PROJECT_ROOT_ARG" 2>/dev/null && pwd)" || {
    echo "❌ Error: Project path does not exist: $PROJECT_ROOT_ARG"
    exit 1
}

# Same derivations as start.sh — identical inputs give identical plist bytes.
BOT_DATA_DIR="$PROJECT_ROOT/.telegram_bot"
LOGS_DIR="$BOT_DATA_DIR/logs"
PID_FILE="$BOT_DATA_DIR/bot.pid"
SUPERVISOR_PID_FILE="$BOT_DATA_DIR/supervisor.pid"
PROJECT_SLUG="$(basename "$PROJECT_ROOT" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/-*$//')"
PLIST_LABEL="com.telegram-skill-bot.${PROJECT_SLUG}"
PLIST_FILE="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
CALLER="${CALLER:-$SCRIPT_DIR/start.sh}"
TOKEN_LOCK_FILE="${CCC_BRIDGE_TOKEN_LOCK_FILE:-}"

# ── PID utility functions (same semantics as start.sh) ──

read_pid() {
    [ -f "$PID_FILE" ] && cat "$PID_FILE"
}

read_supervisor_pid() {
    [ -f "$SUPERVISOR_PID_FILE" ] && cat "$SUPERVISOR_PID_FILE"
}

cleanup_pid() {
    rm -f "$PID_FILE" 2>/dev/null || true
}

cleanup_supervisor_pid() {
    rm -f "$SUPERVISOR_PID_FILE" 2>/dev/null || true
}

cleanup_token_lock() {
    [ -n "$TOKEN_LOCK_FILE" ] && rm -f "$TOKEN_LOCK_FILE"
}

# Same policy as start.sh cleanup_token_lock_if_safe, except the lock path is
# handed in via CCC_BRIDGE_TOKEN_LOCK_FILE (start.sh derives it with
# init_token_lock before dispatching). Without a lock path we skip cleanup.
cleanup_token_lock_if_safe() {
    local expected_pid_1="$1"
    local expected_pid_2="$2"
    local lock_pid

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

do_install() {
    echo "📝 Generating launchd plist: $PLIST_FILE"
    mkdir -p "$(dirname "$PLIST_FILE")"
    mkdir -p "$LOGS_DIR"
    # Ensure .local/bin is in PATH for claude CLI
    LAUNCHD_PATH="${PATH}"
    if [ -d "$HOME/.local/bin" ] && ! echo "$LAUNCHD_PATH" | grep -q "$HOME/.local/bin"; then
        LAUNCHD_PATH="$HOME/.local/bin:$LAUNCHD_PATH"
    fi

    # Proxy config for launchd environment (resolved by the caller from
    # PROXY_URL in the project/bot .env files)
    local proxy_url="$PROXY_URL_ARG"

    # Build environment variables section
    local env_vars
    env_vars="    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${LAUNCHD_PATH}</string>
        <key>HOME</key>
        <string>${HOME}</string>"
    if [ -n "$proxy_url" ]; then
        env_vars="$env_vars
        <key>http_proxy</key>
        <string>${proxy_url}</string>
        <key>https_proxy</key>
        <string>${proxy_url}</string>
        <key>all_proxy</key>
        <string>${proxy_url}</string>
        <key>no_proxy</key>
        <string>localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12</string>"
    fi
    env_vars="$env_vars
    </dict>"

    cat > "$PLIST_FILE" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${SCRIPT_DIR}/start.sh</string>
        <string>--path</string>
        <string>${PROJECT_ROOT}</string>
        <string>--_launchd_child</string>
    </array>
${env_vars}
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOGS_DIR}/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOGS_DIR}/launchd_stderr.log</string>
    <key>WorkingDirectory</key>
    <string>${REPO_ROOT}</string>
</dict>
</plist>
PLIST
    # Ensure old service is unloaded first
    "$LAUNCHCTL" bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null
    # Load using new API
    if "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$PLIST_FILE"; then
        echo "✅ Installed and loaded as startup service"
        echo "🚀 Bot started via launchd"
    else
        echo "⚠️  launchctl bootstrap failed, trying legacy API..."
        "$LAUNCHCTL" load -w "$PLIST_FILE"
    fi
    # Wait for process to start (up to 5 seconds)
    echo "⏳ Waiting for bot to initialize..."
    for i in $(seq 1 10); do
        sleep 0.5
        if [ -f "$PID_FILE" ]; then
            pid="$(cat "$PID_FILE")"
            if kill -0 "$pid" 2>/dev/null; then
                echo "✅ Bot process started (PID: $pid)"
                break
            fi
        fi
    done
    echo "💡 Use $CALLER --path \"$PROJECT_ROOT\" --status to check status"
    echo "💡 Use $CALLER --path \"$PROJECT_ROOT\" --uninstall to remove startup service"
    exit 0
}

do_uninstall() {
    if [ -f "$PLIST_FILE" ]; then
        echo "🗑️  Uninstalling launchd plist..."
        # Stop the service first
        "$LAUNCHCTL" bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || "$LAUNCHCTL" unload "$PLIST_FILE" 2>/dev/null || true
        sleep 1
        # Stop any remaining processes
        local pid supervisor_pid
        supervisor_pid="$(read_supervisor_pid)"
        pid="$(read_pid)"
        if [ -n "$supervisor_pid" ] && kill -0 "$supervisor_pid" 2>/dev/null; then
            echo "🛑 Stopping daemon supervisor (PID: $supervisor_pid)..."
            kill "$supervisor_pid" 2>/dev/null || true
        fi
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "🛑 Stopping bot process (PID: $pid)..."
            kill "$pid" 2>/dev/null || true
        fi
        cleanup_pid
        cleanup_supervisor_pid
        cleanup_token_lock_if_safe "$supervisor_pid" "$pid"
        # Remove the plist file
        rm -f "$PLIST_FILE"
        echo "✅ Startup service uninstalled"
    else
        echo "⚪ Startup service not installed (plist not found)"
    fi
    exit 0
}

case "$SUBCOMMAND" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
esac
