#!/usr/bin/env bash
# Install the fleet work board as a systemd timer (#2914).
# Safe by default: dry-run unless --apply is given. Run it on the ONE hub node
# that holds a GitHub login able to read the organisation and a wiki-agent
# install; no other node needs anything.
#
#   install-work-board-timer.sh --org ORG [--dry-run|--apply] [--uninstall]
#       [--page pages/current/work-board.md] [--on-calendar SPEC]
#       [--service-name NAME] [--state-dir DIR]
#
# The unit runs scripts/ccc-work-board.py once a day (default 05:30
# Asia/Seoul — after the 04:45 node self-update). It reads open issues and PRs
# over the GitHub API, rewrites one Wiki page in the wiki-agent PR worktree and
# runs `wiki-agent pr`; nothing is submitted when only the timestamp changed.
# The unit needs the account's HOME (gh login, wiki-agent worktree), so it is
# not sandboxed with ProtectSystem=strict like the read-only span view.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APPLY=0
UNINSTALL=0
ORG="${CCC_WORK_BOARD_ORG:-}"
PAGE="${CCC_WORK_BOARD_PAGE:-pages/current/work-board.md}"
SERVICE_NAME="${CCC_WORK_BOARD_SERVICE_NAME:-ccc-work-board}"
ON_CALENDAR="${CCC_WORK_BOARD_ON_CALENDAR:-*-*-* 05:30:00 Asia/Seoul}"
STATE_DIR="${CCC_STATE_DIR:-$HOME/.claude/state}"
SYSTEMD_DIR="${CCC_SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMCTL="${CCC_SYSTEMCTL:-systemctl}"
# 52 repositories took about 70 s on 2026-11-26; wiki-agent pr can wait on CI.
TIMEOUT_START="${CCC_WORK_BOARD_TIMEOUT_START:-1500}"

usage() {
  cat <<'EOF'
Usage: install-work-board-timer.sh --org ORG [--dry-run|--apply] [--uninstall]
       [--page pages/current/work-board.md] [--on-calendar SPEC]
       [--service-name NAME] [--state-dir DIR]

Installs (or with --uninstall removes) the systemd service + timer that
rebuilds the fleet work board Wiki page once a day on the hub node. Defaults
to dry-run: prints the unit files and the commands it would run. --apply is
required to change anything.
EOF
}
need_val() { [ -n "${2:-}" ] || { echo "$1 requires a value" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --org) need_val "$1" "${2:-}"; ORG="$2"; shift ;;
    --page) need_val "$1" "${2:-}"; PAGE="$2"; shift ;;
    --on-calendar) need_val "$1" "${2:-}"; ON_CALENDAR="$2"; shift ;;
    --service-name) need_val "$1" "${2:-}"; SERVICE_NAME="$2"; shift ;;
    --state-dir) need_val "$1" "${2:-}"; STATE_DIR="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# Every value below is written into a unit file, so each one is held to a
# plain shape instead of being quoted for systemd.
plain_path() { [[ "$1" =~ ^/[A-Za-z0-9._/-]+$ ]] && [[ "$1" != *..* ]]; }
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$ ]] || { echo "bad --service-name" >&2; exit 2; }
[[ "$ON_CALENDAR" =~ ^[A-Za-z0-9*:,/\ ._~-]{1,80}$ ]] || { echo "bad --on-calendar" >&2; exit 2; }
[[ "$TIMEOUT_START" =~ ^[0-9]{1,5}$ ]] || { echo "bad CCC_WORK_BOARD_TIMEOUT_START" >&2; exit 2; }
plain_path "$ROOT" || { echo "repository path has characters a unit file cannot carry safely" >&2; exit 2; }
plain_path "$HOME" || { echo "HOME has characters a unit file cannot carry safely" >&2; exit 2; }
plain_path "$STATE_DIR" || { echo "bad --state-dir (want a plain absolute path)" >&2; exit 2; }

SERVICE_FILE="$SYSTEMD_DIR/$SERVICE_NAME.service"
TIMER_FILE="$SYSTEMD_DIR/$SERVICE_NAME.timer"

if [ "$UNINSTALL" = 1 ]; then
  if [ "$APPLY" != 1 ]; then
    echo "# dry-run: would run $SYSTEMCTL disable --now $SERVICE_NAME.timer"
    echo "# dry-run: would remove $SERVICE_FILE and $TIMER_FILE"
    echo "# dry-run: would run $SYSTEMCTL daemon-reload"
    echo "# the Wiki page and the state files are left in place"
    exit 0
  fi
  "$SYSTEMCTL" disable --now "$SERVICE_NAME.timer" 2>/dev/null || true
  rm -f "$SERVICE_FILE" "$TIMER_FILE"
  "$SYSTEMCTL" daemon-reload
  printf 'removed %s and %s (Wiki page and state left in place)\n' "$SERVICE_FILE" "$TIMER_FILE"
  exit 0
fi

[ -n "$ORG" ] || { echo "--org is required (or CCC_WORK_BOARD_ORG)" >&2; usage >&2; exit 2; }
[[ "$ORG" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}$ ]] || { echo "bad --org" >&2; exit 2; }
[[ "$PAGE" =~ ^pages/[A-Za-z0-9._/-]+\.md$ ]] && [[ "$PAGE" != *..* ]] || { echo "bad --page (want pages/<path>.md)" >&2; exit 2; }

RESULT="$STATE_DIR/$SERVICE_NAME.result"
NOTIFY="$HOME/.claude/hooks/lib/detached_jobs.py"
NOTIFY_LINE=""
# One line per run: success waits for the morning digest, a failure is sent at once.
[ ! -f "$NOTIFY" ] || NOTIFY_LINE="ExecStopPost=-/usr/bin/env python3 $NOTIFY notify --unit $SERVICE_NAME --exit \${EXIT_STATUS} --tier info --fail-tier notice --result-file $RESULT"

SERVICE_CONTENT="$(cat <<EOF
[Unit]
Description=ccc-node fleet work board (GitHub open issues -> one Wiki page)
Documentation=https://github.com/jinwon-int/ccc-node/issues/2914
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=HOME=$HOME
Environment=CCC_STATE_DIR=$STATE_DIR
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/env python3 $ROOT/scripts/ccc-work-board.py run --org $ORG --page $PAGE --state-dir $STATE_DIR --result-file $RESULT
${NOTIFY_LINE:+$NOTIFY_LINE
}TimeoutStartSec=$TIMEOUT_START
Nice=10
NoNewPrivileges=true
PrivateTmp=true
EOF
)"

TIMER_CONTENT="$(cat <<EOF
[Unit]
Description=Rebuild the ccc-node fleet work board once a day
Documentation=https://github.com/jinwon-int/ccc-node/issues/2914

[Timer]
OnCalendar=$ON_CALENDAR
Persistent=true
AccuracySec=1min
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
  echo "# dry-run: would run $SYSTEMCTL daemon-reload"
  echo "# dry-run: would run $SYSTEMCTL enable --now $SERVICE_NAME.timer"
  exit 0
fi

[ -d "$STATE_DIR" ] || { echo "--state-dir does not exist; nothing installed" >&2; exit 3; }
mkdir -p "$SYSTEMD_DIR"
printf '%s\n' "$SERVICE_CONTENT" > "$SERVICE_FILE"
printf '%s\n' "$TIMER_CONTENT" > "$TIMER_FILE"
chmod 0644 "$SERVICE_FILE" "$TIMER_FILE"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now "$SERVICE_NAME.timer"
printf 'installed %s and %s\n' "$SERVICE_FILE" "$TIMER_FILE"
