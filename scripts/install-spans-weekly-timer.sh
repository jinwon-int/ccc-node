#!/usr/bin/env bash
# Install the weekly fleet span summary as a systemd timer (#2899).
# Safe by default: dry-run unless --apply is given. Run it on the ONE hub node
# that already runs the daily span view (install-spans-view-timer.sh) and has
# a wiki-agent install; no other node needs anything.
#
#   install-spans-weekly-timer.sh --nodes "a b c" [--dry-run|--apply] [--uninstall]
#       [--page pages/services/span-rollup.md] [--on-calendar SPEC]
#       [--service-name NAME] [--state-dir DIR]
#
# The unit runs scripts/spans-weekly.py once a week (default Monday 05:00
# Asia/Seoul — after the 04:30 daily view collection, which is capped at
# 900 s, and before the 05:30 work board that uses the same wiki-agent
# worktree). It reads only the records the daily view already keeps in the
# state folder, adds the previous week's section to one Wiki page in the
# wiki-agent PR worktree and runs `wiki-agent pr`; a week already on the page
# is never written again. The unit needs the account's HOME (wiki-agent
# worktree), so it is not sandboxed with ProtectSystem=strict.
set -euo pipefail
# --nodes is split on blanks below; a `*` in it must stay a (refused) name.
set -f

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APPLY=0
UNINSTALL=0
NODES="${CCC_FLEET_NODES:-}"
PAGE="${CCC_SPANS_WEEKLY_PAGE:-pages/services/span-rollup.md}"
SERVICE_NAME="${CCC_SPANS_WEEKLY_SERVICE_NAME:-ccc-spans-weekly}"
ON_CALENDAR="${CCC_SPANS_WEEKLY_ON_CALENDAR:-Mon *-*-* 05:00:00 Asia/Seoul}"
STATE_DIR="${CCC_STATE_DIR:-$HOME/.claude/state}"
SYSTEMD_DIR="${CCC_SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMCTL="${CCC_SYSTEMCTL:-systemctl}"
# Reading 14 days of records takes well under a second; wiki-agent pr can wait
# on CI. 25 minutes keeps the run clear of the 05:30 work board.
TIMEOUT_START="${CCC_SPANS_WEEKLY_TIMEOUT_START:-1500}"

usage() {
  cat <<'EOF'
Usage: install-spans-weekly-timer.sh --nodes "a b c" [--dry-run|--apply] [--uninstall]
       [--page pages/services/span-rollup.md] [--on-calendar SPEC]
       [--service-name NAME] [--state-dir DIR]

Installs (or with --uninstall removes) the systemd service + timer that adds
the weekly fleet span summary to a Family Wiki page on the hub node. Defaults
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
    --nodes) need_val "$1" "${2:-}"; NODES="$2"; shift ;;
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
[[ "$TIMEOUT_START" =~ ^[0-9]{1,5}$ ]] || { echo "bad CCC_SPANS_WEEKLY_TIMEOUT_START" >&2; exit 2; }
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
    echo "# the Wiki page and the span records are left in place"
    exit 0
  fi
  "$SYSTEMCTL" disable --now "$SERVICE_NAME.timer" 2>/dev/null || true
  rm -f "$SERVICE_FILE" "$TIMER_FILE"
  "$SYSTEMCTL" daemon-reload
  printf 'removed %s and %s (Wiki page and records left in place)\n' "$SERVICE_FILE" "$TIMER_FILE"
  exit 0
fi

# No built-in node list (#1451): a node that never answered must still show up
# as 「미수집」, so the unit carries the hub's list.
[ -n "$NODES" ] || { echo "--nodes is required (space-separated node list, or CCC_FLEET_NODES)" >&2; exit 2; }
NODE_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
for node in $NODES; do
  [[ "$node" =~ $NODE_RE ]] || { echo "bad node name in --nodes" >&2; exit 2; }
done
[[ "$NODES" =~ ^[A-Za-z0-9._\ -]*$ ]] || { echo "bad --nodes (names separated by single spaces)" >&2; exit 2; }
[[ "$PAGE" =~ ^pages/[A-Za-z0-9._/-]+\.md$ ]] && [[ "$PAGE" != *..* ]] || { echo "bad --page (want pages/<path>.md)" >&2; exit 2; }

RESULT="$STATE_DIR/$SERVICE_NAME.result"
NOTIFY="$HOME/.claude/hooks/lib/detached_jobs.py"
NOTIFY_LINE=""
# One line per run: success waits for the morning digest, a failed PR is sent
# at once (the next week's run retries the missing week).
[ ! -f "$NOTIFY" ] || NOTIFY_LINE="ExecStopPost=-/usr/bin/env python3 $NOTIFY notify --unit $SERVICE_NAME --exit \${EXIT_STATUS} --tier info --fail-tier notice --result-file $RESULT"

SERVICE_CONTENT="$(cat <<EOF
[Unit]
Description=ccc-node weekly fleet span summary (hub records -> one Wiki page)
Documentation=https://github.com/jinwon-int/ccc-node/issues/2899
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=HOME=$HOME
Environment=CCC_STATE_DIR=$STATE_DIR
Environment="CCC_FLEET_NODES=$NODES"
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/env python3 $ROOT/scripts/spans-weekly.py --page $PAGE --state-dir $STATE_DIR --result-file $RESULT
${NOTIFY_LINE:+$NOTIFY_LINE
}TimeoutStartSec=$TIMEOUT_START
Nice=10
NoNewPrivileges=true
PrivateTmp=true
EOF
)"

TIMER_CONTENT="$(cat <<EOF
[Unit]
Description=Add the ccc-node weekly fleet span summary once a week
Documentation=https://github.com/jinwon-int/ccc-node/issues/2899

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
