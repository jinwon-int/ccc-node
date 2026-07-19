#!/bin/bash

# ccc-node Telegram bridge — Linux systemd startup-service subcommand.
#
# Extracted verbatim from bridge/start.sh systemd_paths/do_install_systemd/
# do_uninstall_systemd (#584 P3-2) so the service-install machinery is
# testable in isolation. start.sh --install-systemd/--uninstall-systemd
# dispatches here after its own pre-flight guards (check_env); this script
# only generates/removes the unit and drives systemctl. The generated unit is
# byte-identical to what start.sh produced before the extraction.
#
# Runs the bridge in the FOREGROUND under systemd supervision (Type=simple):
# start.sh's own prepare_runtime + exec_bot_once handle venv/deps/token-lock,
# and systemd handles restart-on-crash and reboot persistence — so we
# deliberately do NOT pass -d/--daemon in ExecStart. Service unit name is
# overridable via BRIDGE_SERVICE_NAME (default ccc-telegram-bridge), letting
# one host run multiple bridges (e.g. ccc-telegram-bridge-<slug>). Installs a
# system unit when run as root, otherwise a `systemctl --user` unit under
# ~/.config/systemd/user.
#
# Usage:
#   service-systemd.sh install   --project-root <dir> [--proxy-url <url>] [--caller <name>]
#   service-systemd.sh uninstall [--caller <name>]
#
# Inputs (resolved by start.sh when dispatched from there):
#   --project-root  absolute project path (was $PROJECT_ROOT); install only
#   --proxy-url     PROXY_URL resolved via read_env_with_fallback; empty = none
#   --caller        script name to print in user-facing hints (default:
#                   <this dir>/start.sh)
#   BRIDGE_SERVICE_NAME  env: unit base name (default: ccc-telegram-bridge)
#
# Test seams (same pattern as scripts/install-agent-cron-systemd.sh; defaults
# preserve production behavior):
#   CCC_SYSTEMD_DIR  unit directory override (default: scope-derived)
#   CCC_SYSTEMCTL    systemctl command override (default: systemctl)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYSTEMCTL_BIN="${CCC_SYSTEMCTL:-systemctl}"

usage() {
    cat <<EOF
Usage: $0 <install|uninstall> [options]

Subcommands:
  install     Generate the systemd unit and enable --now it
  uninstall   Disable the service and remove the unit

Options:
  --project-root <dir>  Project root directory (required for install)
  --proxy-url <url>     Proxy URL to embed in the unit environment
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
CALLER="${CALLER:-$SCRIPT_DIR/start.sh}"

systemd_paths() {
    # Sets SYSTEMD_UNIT_FILE, SYSTEMCTL (array), SYSTEMD_SCOPE based on euid.
    SYSTEMD_SERVICE="${BRIDGE_SERVICE_NAME:-ccc-telegram-bridge}.service"
    if [ "$(id -u)" = "0" ]; then
        SYSTEMD_SCOPE="system"
        SYSTEMD_UNIT_DIR="/etc/systemd/system"
        SYSTEMCTL=("$SYSTEMCTL_BIN")
    else
        SYSTEMD_SCOPE="user"
        SYSTEMD_UNIT_DIR="$HOME/.config/systemd/user"
        SYSTEMCTL=("$SYSTEMCTL_BIN" --user)
    fi
    # Test seam: never changes production paths unless explicitly exported.
    if [ -n "${CCC_SYSTEMD_DIR:-}" ]; then
        SYSTEMD_UNIT_DIR="$CCC_SYSTEMD_DIR"
    fi
    SYSTEMD_UNIT_FILE="$SYSTEMD_UNIT_DIR/$SYSTEMD_SERVICE"
}

do_install_systemd() {
    if ! command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1; then
        echo "❌ systemctl not found — this host does not use systemd. On macOS use --install instead."
        exit 1
    fi
    if [ -z "$PROJECT_ROOT_ARG" ]; then
        echo "❌ Error: --project-root is required"
        exit 1
    fi
    local PROJECT_ROOT PROJECT_SLUG
    PROJECT_ROOT="$(cd "$PROJECT_ROOT_ARG" 2>/dev/null && pwd)" || {
        echo "❌ Error: Project path does not exist: $PROJECT_ROOT_ARG"
        exit 1
    }
    PROJECT_SLUG="$(basename "$PROJECT_ROOT" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/-*$//')"
    systemd_paths

    # Build PATH so the claude CLI (often in ~/.local/bin) is reachable from the unit.
    local svc_path="$PATH"
    if [ -d "$HOME/.local/bin" ] && ! echo "$svc_path" | grep -q "$HOME/.local/bin"; then
        svc_path="$HOME/.local/bin:$svc_path"
    fi
    # Optional proxy, mirrored from the launchd installer (resolved by the
    # caller from PROXY_URL in the project/bot .env files).
    local proxy_url proxy_env=""
    proxy_url="$PROXY_URL_ARG"
    if [ -n "$proxy_url" ]; then
        proxy_env="Environment=http_proxy=${proxy_url}
Environment=https_proxy=${proxy_url}
Environment=all_proxy=${proxy_url}
Environment=no_proxy=localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"
    fi

    local wanted_by="default.target"
    [ "$SYSTEMD_SCOPE" = "system" ] && wanted_by="multi-user.target"

    echo "📝 Generating systemd unit: $SYSTEMD_UNIT_FILE"
    mkdir -p "$SYSTEMD_UNIT_DIR"
    cat > "$SYSTEMD_UNIT_FILE" <<UNIT
[Unit]
Description=ccc-node Telegram bridge (${PROJECT_SLUG})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_ROOT}
Environment=HOME=${HOME}
Environment=PATH=${svc_path}
${proxy_env}
ExecStart=/bin/bash ${SCRIPT_DIR}/start.sh --path ${PROJECT_ROOT}
# Recover when the bridge handles a direct SIGTERM as a clean exit. An explicit
# systemctl stop still suppresses restart, preserving operator stop semantics.
Restart=always
RestartSec=3
TimeoutStopSec=20

[Install]
WantedBy=${wanted_by}
UNIT
    # Collapse the blank line left when there is no proxy block.
    sed -i '/^$/d' "$SYSTEMD_UNIT_FILE" 2>/dev/null || true

    "${SYSTEMCTL[@]}" daemon-reload
    if "${SYSTEMCTL[@]}" enable --now "$SYSTEMD_SERVICE"; then
        echo "✅ Installed and started as $SYSTEMD_SCOPE service: $SYSTEMD_SERVICE"
    else
        echo "⚠️  enable --now failed; unit written to $SYSTEMD_UNIT_FILE — inspect with: ${SYSTEMCTL[*]} status $SYSTEMD_SERVICE"
        exit 1
    fi
    local journal_scope=""
    [ "$SYSTEMD_SCOPE" = "user" ] && journal_scope="--user "
    echo "💡 Status: ${SYSTEMCTL[*]} status $SYSTEMD_SERVICE"
    echo "💡 Logs:   journalctl ${journal_scope}-u $SYSTEMD_SERVICE -f"
    echo "💡 Remove: $CALLER --path \"$PROJECT_ROOT\" --uninstall-systemd"
    exit 0
}

do_uninstall_systemd() {
    if ! command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1; then
        echo "❌ systemctl not found — nothing to uninstall."
        exit 1
    fi
    systemd_paths
    if [ -f "$SYSTEMD_UNIT_FILE" ]; then
        echo "🗑️  Removing systemd unit: $SYSTEMD_UNIT_FILE"
        "${SYSTEMCTL[@]}" disable --now "$SYSTEMD_SERVICE" 2>/dev/null || true
        rm -f "$SYSTEMD_UNIT_FILE"
        "${SYSTEMCTL[@]}" daemon-reload
        echo "✅ systemd service uninstalled"
    else
        echo "⚪ systemd service not installed ($SYSTEMD_UNIT_FILE not found)"
    fi
    exit 0
}

case "$SUBCOMMAND" in
    install)   do_install_systemd ;;
    uninstall) do_uninstall_systemd ;;
esac
