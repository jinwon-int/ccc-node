#!/usr/bin/env bash
# Install the weekly fleet agent-trust audit cron (ccc-node#2538 — planted
# AGENTS.md/CLAUDE.md, endpoint overrides, MCP drift, known-bad npm). Opt-in,
# hub-node only: the payload ssh-es to every fleet node, so it belongs on a
# node with fleet ssh reach (seoseo, like tunnel-audit), not on all twelve.
#
# The cron line runs scripts/agent-trust-audit-fleet.sh from THIS checkout and
# appends its verdicts to the log. Exit 1 (DANGER / NEW / UNREACHABLE / ERROR)
# is the signal an operator reviews; pair it with an agent-cron on-failure
# notification where that lane exists.
#
# Same shape as install-tunnel-audit-cron.sh: dry-run by default, BEGIN/END
# block (#1077), gen stamp over script + shared cron lib (#1081), install
# record, --remove.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/install-agent-trust-audit-cron.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
FLEET="${CCC_AGENT_TRUST_AUDIT_FLEET_CMD:-$SELF_DIR/agent-trust-audit-fleet.sh}"
SCHEDULE="${CCC_AGENT_TRUST_AUDIT_CRON:-40 6 * * 1}"
LOG="${CCC_AGENT_TRUST_AUDIT_CRON_LOG:-$STATE_DIR/agent-trust-audit.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:agent-trust-audit"
BLOCK_BEGIN="# ccc-node:agent-trust-audit:begin"
BLOCK_END="# ccc-node:agent-trust-audit:end"
APPLY=0
REMOVE=0

# Shared installer libs (#1081, #1077): gen stamps + records, and the common
# crontab install/remove driver.
GEN_STAMP_LIB="$SELF_DIR/lib/installer-gen-stamp.sh"
CRON_COMMON_LIB="$SELF_DIR/lib/installer-cron-common.sh"
for lib in "$GEN_STAMP_LIB" "$CRON_COMMON_LIB"; do
  if [ ! -r "$lib" ]; then
    echo "shared installer library is missing: $lib" >&2
    exit 4
  fi
  # shellcheck source=/dev/null
  . "$lib"
done
GEN="$(ccc_installer_gen_stamp_auto "$SELF")"

usage() {
  cat <<EOF
Usage: install-agent-trust-audit-cron.sh [--dry-run|--apply] [--remove] [--schedule SPEC]

Installs (or removes) a weekly crontab entry that runs
scripts/agent-trust-audit-fleet.sh --quiet from this checkout (hub node only) and
appends the per-node verdicts + exit code to the log. Defaults to dry-run;
--apply is required to change the crontab. Idempotent: re-running
replaces the managed "$BLOCK_BEGIN" ..
"$BLOCK_END" block (and migrates any legacy bare "$MARKER" line into it).

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change.
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Cron schedule (5 fields). Default: "$SCHEDULE".

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_AGENT_TRUST_AUDIT_FLEET_CMD,
CCC_AGENT_TRUST_AUDIT_CRON, CCC_AGENT_TRUST_AUDIT_CRON_LOG, CCC_CRONTAB_CMD.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --remove) REMOVE=1 ;;
    --schedule) ccc_cron_need_val "$1" "${2:-}"; SCHEDULE="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

CRON_LINE="$SCHEDULE bash -lc 'CCC_STATE_DIR=\"$STATE_DIR\" bash \"$FLEET\" --quiet; echo \"agent-trust-audit-fleet rc=\$?\"' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

if [ "$APPLY" = 1 ] && [ "$REMOVE" != 1 ]; then
  # Same redirect-first failure mode as the sibling installers: the cron line
  # appends to "$LOG" (under STATE_DIR); create the directory now.
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi

ccc_cron_installer_finish \
  --label "agent-trust-audit" \
  --marker "$MARKER" --begin "$BLOCK_BEGIN" --end "$BLOCK_END" \
  --crontab "$CRONTAB" --state-dir "$STATE_DIR" --self "$SELF" --gen "$GEN" \
  --apply "$APPLY" --remove "$REMOVE" --schedule-desc "$SCHEDULE" \
  --body "$CRON_LINE" -- \
  --apply --schedule "$SCHEDULE"
