#!/usr/bin/env bash
# Install ccc-node agent-cron as a systemd timer.
# Safe by default: dry-run unless --apply is provided. Never prints secrets.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APPLY=0
USER_MODE=0
SERVICE_NAME="${CCC_AGENT_CRON_SERVICE_NAME:-ccc-agent-cron}"
ON_CALENDAR="${CCC_AGENT_CRON_ON_CALENDAR:-*-*-* *:*:00}"
STORE="${CCC_AGENT_CRON_STORE:-$HOME/.claude/state/agent-cron/tasks.json}"
RUNNER="${CCC_AGENT_CRON_RUNNER:-claude}"
HEADLESS="${CCC_HEADLESS_CMD:-}"
CLAUDE_BIN="${CCC_CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"
CODEX_BIN="${CCC_CODEX_BIN:-$(command -v codex 2>/dev/null || true)}"
CODEX_SANDBOX="${CCC_CODEX_SANDBOX:-read-only}"
SPOOL="${CCC_AGENT_CRON_PUSH_SPOOL:-${CCC_PUSH_SPOOL:-$HOME/.claude/state/telegram-spool}}"
SYSTEMD_DIR="${CCC_SYSTEMD_DIR:-}"
SYSTEMCTL="${CCC_SYSTEMCTL:-systemctl}"
ENABLE=1
RESTART=1

usage() {
  cat <<EOF
Usage: install-agent-cron-systemd.sh [--dry-run|--apply] [--user] [--service-name NAME]
       [--on-calendar SPEC] [--store PATH] [--runner claude|codex]
       [--headless PATH] [--spool PATH] [--codex-sandbox MODE]

Installs ${SERVICE_NAME}.service and ${SERVICE_NAME}.timer for one-shot
agent-cron scheduler execution. Defaults to dry-run. --apply is required for
filesystem/systemd changes.
EOF
}

need_val() { [ -n "${2:-}" ] || { echo "$1 requires a value" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --user) USER_MODE=1 ;;
    --service-name) need_val "$1" "${2:-}"; SERVICE_NAME="$2"; shift ;;
    --on-calendar) need_val "$1" "${2:-}"; ON_CALENDAR="$2"; shift ;;
    --store) need_val "$1" "${2:-}"; STORE="$2"; shift ;;
    --runner) need_val "$1" "${2:-}"; RUNNER="$2"; shift ;;
    --headless) need_val "$1" "${2:-}"; HEADLESS="$2"; shift ;;
    --spool) need_val "$1" "${2:-}"; SPOOL="$2"; shift ;;
    --codex-sandbox) need_val "$1" "${2:-}"; CODEX_SANDBOX="$2"; shift ;;
    --no-enable) ENABLE=0 ;;
    --no-restart) RESTART=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

case "$SERVICE_NAME" in
  *[!A-Za-z0-9_.@-]*|"" ) echo "invalid service name: $SERVICE_NAME" >&2; exit 2 ;;
esac
case "$RUNNER" in
  claude|codex) ;;
  *) echo "invalid runner: $RUNNER" >&2; exit 2 ;;
esac
case "$CODEX_SANDBOX" in
  read-only|workspace-write|danger-full-access) ;;
  *) echo "invalid Codex sandbox: $CODEX_SANDBOX" >&2; exit 2 ;;
esac
if [ -z "$HEADLESS" ]; then
  if [ "$RUNNER" = codex ]; then
    HEADLESS="$ROOT/codex/headless.sh"
  else
    HEADLESS="$ROOT/claude/headless.sh"
  fi
fi

if [ -z "$SYSTEMD_DIR" ]; then
  if [ "$USER_MODE" = 1 ]; then
    SYSTEMD_DIR="$HOME/.config/systemd/user"
  else
    SYSTEMD_DIR="/etc/systemd/system"
  fi
fi

SERVICE_FILE="$SYSTEMD_DIR/$SERVICE_NAME.service"
TIMER_FILE="$SYSTEMD_DIR/$SERVICE_NAME.timer"
SYSTEMCTL_ARGS=()
[ "$USER_MODE" = 1 ] && SYSTEMCTL_ARGS+=(--user)

SERVICE_CONTENT="$(cat <<EOF
[Unit]
Description=ccc-node agent-cron scheduler tick
Documentation=https://github.com/jinwon-int/ccc-node/issues/55

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=HOME=$HOME
Environment=CCC_AGENT_CRON_STORE=$STORE
Environment=CCC_HEADLESS_CMD=$HEADLESS
Environment=CCC_CLAUDE_BIN=$CLAUDE_BIN
Environment=CCC_CODEX_BIN=$CODEX_BIN
Environment=CCC_CODEX_SANDBOX=$CODEX_SANDBOX
Environment=CCC_AGENT_CRON_PUSH_SPOOL=$SPOOL
ExecStart=$ROOT/scripts/agent-cron.sh scheduler --execute --json
Nice=5
EOF
)"

TIMER_CONTENT="$(cat <<EOF
[Unit]
Description=Run ccc-node agent-cron scheduler periodically
Documentation=https://github.com/jinwon-int/ccc-node/issues/55

[Timer]
OnCalendar=$ON_CALENDAR
Persistent=true
AccuracySec=30s
Unit=$SERVICE_NAME.service

[Install]
WantedBy=timers.target
EOF
)"

if [ "$APPLY" != 1 ]; then
  echo "# dry-run: would write $SERVICE_FILE"
  printf '%s\n' "$SERVICE_CONTENT"
  echo "# dry-run: would write $TIMER_FILE"
  printf '%s\n' "$TIMER_CONTENT"
  echo "# dry-run: would run ${SYSTEMCTL} ${SYSTEMCTL_ARGS[*]} daemon-reload"
  [ "$ENABLE" = 1 ] && echo "# dry-run: would run ${SYSTEMCTL} ${SYSTEMCTL_ARGS[*]} enable --now $SERVICE_NAME.timer"
  [ "$RESTART" = 1 ] && echo "# dry-run: would run ${SYSTEMCTL} ${SYSTEMCTL_ARGS[*]} restart $SERVICE_NAME.timer"
  exit 0
fi

mkdir -p "$SYSTEMD_DIR"
printf '%s\n' "$SERVICE_CONTENT" > "$SERVICE_FILE"
printf '%s\n' "$TIMER_CONTENT" > "$TIMER_FILE"
chmod 0644 "$SERVICE_FILE" "$TIMER_FILE"
"$SYSTEMCTL" "${SYSTEMCTL_ARGS[@]}" daemon-reload
if [ "$ENABLE" = 1 ]; then
  "$SYSTEMCTL" "${SYSTEMCTL_ARGS[@]}" enable --now "$SERVICE_NAME.timer"
fi
if [ "$RESTART" = 1 ]; then
  "$SYSTEMCTL" "${SYSTEMCTL_ARGS[@]}" restart "$SERVICE_NAME.timer"
fi
printf 'installed %s and %s\n' "$SERVICE_FILE" "$TIMER_FILE"
