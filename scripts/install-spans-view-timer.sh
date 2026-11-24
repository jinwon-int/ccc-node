#!/usr/bin/env bash
# Install the hub-side span view as a systemd timer (#2858, item 4).
# Safe by default: dry-run unless --apply is given. Run it on the ONE node that
# hosts the view; the other nodes need nothing installed.
#
#   install-spans-view-timer.sh --out-dir DIR --nodes "a b c" [--dry-run|--apply]
#       [--uninstall] [--self NODE] [--on-calendar SPEC] [--service-name NAME]
#       [--state-dir DIR]
#
# The unit runs scripts/spans-view-collect.sh once a day (default 04:30
# Asia/Seoul — after the 03:41 node rollups). That script reads one body-free
# summary file per node over a read-only ssh probe and writes static pages
# into --out-dir. The folder is created (0755) on the first run if its parent
# exists; the unit may write only there and in the state directory
# (ProtectSystem=strict + ReadWritePaths=).
set -euo pipefail
# --nodes is split on blanks below; a `*` in it must stay a (refused) name.
set -f

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APPLY=0
UNINSTALL=0
OUT_DIR=''
NODES="${CCC_FLEET_NODES:-}"
SELF_NODE="${CCC_FLEET_SELF:-}"
SERVICE_NAME="${CCC_SPANS_VIEW_SERVICE_NAME:-ccc-spans-view}"
ON_CALENDAR="${CCC_SPANS_VIEW_ON_CALENDAR:-*-*-* 04:30:00 Asia/Seoul}"
STATE_DIR="${CCC_STATE_DIR:-$HOME/.claude/state}"
SYSTEMD_DIR="${CCC_SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMCTL="${CCC_SYSTEMCTL:-systemctl}"
# 12 nodes x (2 x 20s + 2s) = 504s worst case; a oneshot without a cap can sit
# in `activating` forever and stop the timer from firing again (#55).
TIMEOUT_START="${CCC_SPANS_VIEW_TIMEOUT_START:-900}"

usage() {
  cat <<'EOF'
Usage: install-spans-view-timer.sh --out-dir DIR --nodes "a b c" [--dry-run|--apply]
       [--uninstall] [--self NODE] [--on-calendar SPEC] [--service-name NAME]
       [--state-dir DIR]

Installs (or with --uninstall removes) the systemd service + timer that builds
the static span-rollup pages on the hub node. Defaults to dry-run: prints the
unit files and the commands it would run. --apply is required to change
anything.
EOF
}
need_val() { [ -n "${2:-}" ] || { echo "$1 requires a value" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --out-dir) need_val "$1" "${2:-}"; OUT_DIR="$2"; shift ;;
    --nodes) need_val "$1" "${2:-}"; NODES="$2"; shift ;;
    --self) need_val "$1" "${2:-}"; SELF_NODE="$2"; shift ;;
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
[[ "$TIMEOUT_START" =~ ^[0-9]{1,5}$ ]] || { echo "bad CCC_SPANS_VIEW_TIMEOUT_START" >&2; exit 2; }
plain_path "$ROOT" || { echo "repository path has characters a unit file cannot carry safely" >&2; exit 2; }
plain_path "$HOME" || { echo "HOME has characters a unit file cannot carry safely" >&2; exit 2; }
plain_path "$STATE_DIR" || { echo "bad --state-dir (want a plain absolute path)" >&2; exit 2; }
NODE_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
for node in $NODES; do
  [[ "$node" =~ $NODE_RE ]] || { echo "bad node name in --nodes" >&2; exit 2; }
done
# The list is split on blanks; anything else in it (a tab, a newline) would
# reach the unit file as written.
[[ "$NODES" =~ ^[A-Za-z0-9._\ -]*$ ]] || { echo "bad --nodes (names separated by single spaces)" >&2; exit 2; }
# --self is one name, checked whole: split on blanks, "a b" would pass as two.
[ -z "$SELF_NODE" ] || [[ "$SELF_NODE" =~ $NODE_RE ]] || { echo "bad --self" >&2; exit 2; }

SERVICE_FILE="$SYSTEMD_DIR/$SERVICE_NAME.service"
TIMER_FILE="$SYSTEMD_DIR/$SERVICE_NAME.timer"

if [ "$UNINSTALL" = 1 ]; then
  if [ "$APPLY" != 1 ]; then
    echo "# dry-run: would run $SYSTEMCTL disable --now $SERVICE_NAME.timer"
    echo "# dry-run: would remove $SERVICE_FILE and $TIMER_FILE"
    echo "# dry-run: would run $SYSTEMCTL daemon-reload"
    echo "# pages and collected records are left in place"
    exit 0
  fi
  "$SYSTEMCTL" disable --now "$SERVICE_NAME.timer" 2>/dev/null || true
  rm -f "$SERVICE_FILE" "$TIMER_FILE"
  "$SYSTEMCTL" daemon-reload
  printf 'removed %s and %s (pages and records left in place)\n' "$SERVICE_FILE" "$TIMER_FILE"
  exit 0
fi

[ -n "$OUT_DIR" ] || { echo "--out-dir is required" >&2; usage >&2; exit 2; }
# The collector has no built-in node list (#1451), so the unit must carry one.
[ -n "$NODES" ] || { echo "--nodes is required (space-separated node list, or CCC_FLEET_NODES)" >&2; exit 2; }
plain_path "$OUT_DIR" || { echo "bad --out-dir (want a plain absolute path)" >&2; exit 2; }

ENV_LINES="Environment=HOME=$HOME
Environment=CCC_STATE_DIR=$STATE_DIR
Environment=\"CCC_FLEET_NODES=$NODES\""
[ -z "$SELF_NODE" ] || ENV_LINES="$ENV_LINES
Environment=CCC_FLEET_SELF=$SELF_NODE"

SERVICE_CONTENT="$(cat <<EOF
[Unit]
Description=ccc-node span rollup view (read-only node probes, static pages)
Documentation=https://github.com/jinwon-int/ccc-node/issues/2858
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
$ENV_LINES
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/env bash $ROOT/scripts/spans-view-collect.sh --out-dir $OUT_DIR
TimeoutStartSec=$TIMEOUT_START
Nice=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$STATE_DIR $(dirname "$OUT_DIR")
EOF
)"

TIMER_CONTENT="$(cat <<EOF
[Unit]
Description=Build the ccc-node span rollup view once a day
Documentation=https://github.com/jinwon-int/ccc-node/issues/2858

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

[ -d "$(dirname "$OUT_DIR")" ] || { echo "parent of --out-dir does not exist; nothing installed" >&2; exit 3; }
# ReadWritePaths= names both folders; a missing one stops the unit before it starts.
[ -d "$STATE_DIR" ] || { echo "--state-dir does not exist; nothing installed" >&2; exit 3; }
[ ! -L "$OUT_DIR" ] || { echo "--out-dir is a symlink; nothing installed" >&2; exit 3; }
mkdir -p "$SYSTEMD_DIR"
printf '%s\n' "$SERVICE_CONTENT" > "$SERVICE_FILE"
printf '%s\n' "$TIMER_CONTENT" > "$TIMER_FILE"
chmod 0644 "$SERVICE_FILE" "$TIMER_FILE"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now "$SERVICE_NAME.timer"
printf 'installed %s and %s\n' "$SERVICE_FILE" "$TIMER_FILE"
