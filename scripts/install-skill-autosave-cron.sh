#!/usr/bin/env bash
# Install the daily Hermes-style skill-autosave sweep as a crontab entry.
#
# ccc-skill-autosave.sh closes the gap that SessionEnd hooks never fire for
# Telegram-bridge / SDK sessions: it refreshes the skill-candidate report,
# drafts skills from recent transcripts through skill-review.sh, and queues an
# owner-only Telegram notification when drafts await approval.
#
# Consistent with install-memory-refresh-cron.sh: SAFE BY DEFAULT (dry-run
# unless --apply), idempotent (a single marker-tagged line), never prints
# secrets, and the harness setup.sh never installs this itself.
#
# The cron entry runs through `bash -lc` so the login profile PATH is loaded;
# the sweep shells out to jq/find and skill-review.sh shells out to `claude`,
# which a bare cron PATH (especially on Termux) would not resolve.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
AUTOSAVE="${CCC_SKILL_AUTOSAVE_CMD:-$CLAUDE_DIR/hooks/ccc-skill-autosave.sh}"
LOG="${CCC_SKILL_AUTOSAVE_CRON_LOG:-$STATE_DIR/skill-autosave.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:skill-autosave"
BLOCK_BEGIN="# ccc-node:autosave-schedule:begin"
BLOCK_END="# ccc-node:autosave-schedule:end"
APPLY=0
REMOVE=0
OPT_NODE=""

# Fleet identity for the scheduled run (#1067). `bash -lc` — what this cron line
# uses — exports neither CCC_NODE nor HOSTNAME, so ccc-skill-promotion.py saw no
# identity and (before #1068) stamped every envelope with a placeholder that the
# collecting publisher rejected as remote_node_mismatch; the node reported
# ok/staged while nothing was ever published. #1068 made that fail closed, so the
# scheduled path now refuses to stage at all. Carrying the identity in the cron
# line is the other half: resolve it here, where the operator's environment is
# still rich, and bake it into the entry.
#
# Deliberately NO hostname fallback. The node name is a fleet identity the
# publisher matches against the SSH alias it dialled, not a machine name — on
# yukson `hostname -s` is vps5 while the fleet identity is yukson, so guessing
# reproduces exactly the mismatch #1068 fails closed on. Resolve it or leave it
# out and say so.
sanitize_node() { # <raw> — mirrors _safe_node() in scripts/ccc-skill-promotion.py
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9-]+/-/g; s/-+/-/g; s/^-+//; s/-+$//' \
    | cut -c1-32 | sed -E 's/-+$//'
}
resolve_fleet_node() { # --node > CCC_NODE > $STATE_DIR/node.txt (the file
                       # load-memory.sh / refresh-memory.sh / statusline.sh read)
  local raw=""
  if [ -n "$OPT_NODE" ]; then raw="$OPT_NODE"
  elif [ -n "${CCC_NODE:-}" ]; then raw="${CCC_NODE:-}"
  elif [ -r "$STATE_DIR/node.txt" ]; then raw="$(head -1 "$STATE_DIR/node.txt" 2>/dev/null || true)"
  fi
  sanitize_node "$raw"
}

# User crontabs are evaluated in the cron daemon's local timezone. Resolve the
# default 20:45 UTC target into a host-local expression at install time instead
# of assuming every node runs cron in UTC. Asia/Seoul becomes 05:45 local;
# UTC remains 20:45. Pin CRON_TZ to that detected system timezone as well:
# Cronie honors it, while Debian cron safely treats it as a job environment
# variable and continues using the same system-local schedule. This also
# prevents an unrelated earlier CRON_TZ assignment from changing our job.
detect_local_timezone() {
  local timezone="${CCC_SKILL_AUTOSAVE_LOCAL_TIMEZONE:-}"
  if [ -z "$timezone" ] && [ -r /etc/timezone ]; then
    timezone="$(head -1 /etc/timezone 2>/dev/null | tr -d '[:space:]')"
  fi
  if [ -z "$timezone" ] && command -v timedatectl >/dev/null 2>&1; then
    timezone="$(timedatectl show -p Timezone --value 2>/dev/null | tr -d '[:space:]')"
  fi
  if [ -z "$timezone" ] && command -v getprop >/dev/null 2>&1; then
    timezone="$(getprop persist.sys.timezone 2>/dev/null | tr -d '[:space:]')"
  fi
  [ -n "$timezone" ] || timezone="$(date +%Z)"
  case "$timezone" in
    ''|*[!A-Za-z0-9_+:/.-]*)
      echo "invalid local timezone '$timezone'" >&2
      return 2
      ;;
  esac
  printf '%s' "$timezone"
}

LOCAL_TIMEZONE="$(detect_local_timezone)"

default_local_schedule() {
  local offset="${CCC_SKILL_AUTOSAVE_LOCAL_UTC_OFFSET:-}"
  local sign hours minutes offset_minutes local_minutes
  [ -n "$offset" ] || offset="$(TZ="$LOCAL_TIMEZONE" date +%z)"
  case "$offset" in
    [+-][0-9][0-9][0-9][0-9]) ;;
    *) echo "invalid local UTC offset '$offset' (expected +HHMM or -HHMM)" >&2; return 2 ;;
  esac
  hours="${offset:1:2}"
  minutes="${offset:3:2}"
  if [ "$((10#$hours))" -gt 23 ] || [ "$((10#$minutes))" -gt 59 ]; then
    echo "invalid local UTC offset '$offset' (expected +HHMM or -HHMM)" >&2
    return 2
  fi
  [ "${offset:0:1}" = "+" ] && sign=1 || sign=-1
  offset_minutes=$((sign * (10#$hours * 60 + 10#$minutes)))
  local_minutes=$(((20 * 60 + 45 + offset_minutes + 1440) % 1440))
  printf '%d %d * * *' "$((local_minutes % 60))" "$((local_minutes / 60))"
}

if [ -n "${CCC_SKILL_AUTOSAVE_CRON:-}" ]; then
  SCHEDULE="$CCC_SKILL_AUTOSAVE_CRON"
else
  SCHEDULE="$(default_local_schedule)"
fi

usage() {
  cat <<EOF
Usage: install-skill-autosave-cron.sh [--dry-run|--apply] [--remove] [--schedule SPEC]

Installs (or removes) a crontab entry that runs ccc-skill-autosave.sh daily:
refresh skill candidates, draft skills from recent transcripts (Telegram
bridge sessions included), and queue an owner Telegram notification when
drafts await approval. Defaults to dry-run; --apply is required to change the
crontab. Idempotent: re-running replaces the single "$MARKER" line.

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change.
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Host-local cron schedule (5 fields). Default resolves the
                   20:45 UTC target for this host: "$SCHEDULE".
  --node NAME      Fleet identity to bake into the entry as CCC_NODE, so the
                   scheduled skill-promotion staging can resolve it. Defaults to
                   \$CCC_NODE, then the first line of \$STATE_DIR/node.txt. Never
                   guessed from the hostname: the publisher matches this against
                   the SSH alias it dialled, not the machine name. When it cannot
                   be resolved the entry installs without it and promotion stays
                   fail-closed (#1067).

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_SKILL_AUTOSAVE_CMD,
CCC_SKILL_AUTOSAVE_CRON, CCC_SKILL_AUTOSAVE_CRON_LOG, CCC_CRONTAB_CMD.
CCC_SKILL_AUTOSAVE_LOCAL_TIMEZONE and CCC_SKILL_AUTOSAVE_LOCAL_UTC_OFFSET
(+HHMM/-HHMM) are advanced deterministic overrides for image builds and
tests; normal installs auto-detect both.
EOF
}

need_val() { [ -n "${2:-}" ] || { echo "$1 requires a value" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --remove) REMOVE=1 ;;
    --schedule) need_val "$1" "${2:-}"; SCHEDULE="$2"; shift ;;
    --node) need_val "$1" "${2:-}"; OPT_NODE="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ! command -v "${CRONTAB%% *}" >/dev/null 2>&1; then
  echo "crontab command not found ('$CRONTAB'); cannot manage cron on this node" >&2
  exit 3
fi

if [ ! -f "$AUTOSAVE" ] && [ -f "$ROOT/scripts/ccc-skill-autosave.sh" ]; then
  AUTOSAVE="$ROOT/scripts/ccc-skill-autosave.sh"
fi

FLEET_NODE="$(resolve_fleet_node)"
if [ -n "$FLEET_NODE" ]; then
  CRON_LINE="$SCHEDULE bash -lc 'CCC_NODE=\"$FLEET_NODE\" CCC_CLAUDE_DIR=\"$CLAUDE_DIR\" \"$AUTOSAVE\" run' >> \"$LOG\" 2>&1  $MARKER"
else
  # Install anyway: the entry also refreshes candidates, drafts skills and
  # queues owner notifications, and those work without a fleet identity. Only
  # skill-promotion staging needs it, and that is opt-in and already fail-closed
  # (#1068) — so warn where the operator can see it instead of blocking cron.
  CRON_LINE="$SCHEDULE bash -lc 'CCC_CLAUDE_DIR=\"$CLAUDE_DIR\" \"$AUTOSAVE\" run' >> \"$LOG\" 2>&1  $MARKER"
  echo "WARNING: no fleet identity resolved (--node, \$CCC_NODE, $STATE_DIR/node.txt)." >&2
  echo "         Installing without CCC_NODE; scheduled skill-promotion staging will" >&2
  echo "         refuse with node_identity_unresolved until one is provided (#1067)." >&2
fi

current="$("$CRONTAB" -l 2>/dev/null || true)"
if ! without_marker="$(printf '%s\n' "$current" | awk \
  -v begin="$BLOCK_BEGIN" -v end="$BLOCK_END" -v marker="$MARKER" '
    $0 == begin { if (skip) bad=1; skip=1; next }
    $0 == end { if (!skip) bad=1; skip=0; next }
    !skip && index($0, marker) == 0 { print }
    END { if (skip || bad) exit 42 }
  ')"; then
  echo "skill-autosave cron: corrupt managed schedule block; refusing to edit" >&2
  exit 4
fi

if [ "$REMOVE" = 1 ]; then
  desired="$without_marker"
  action="remove"
else
  desired="$(printf '%s\n%s\nCRON_TZ=%s\n%s\n%s' \
    "$without_marker" "$BLOCK_BEGIN" "$LOCAL_TIMEZONE" "$CRON_LINE" "$BLOCK_END" | sed '/^$/d')"
  action="install"
fi

if [ "$APPLY" = 1 ]; then
  printf '%s\n' "$desired" | "$CRONTAB" -
  echo "skill-autosave cron: ${action} done (schedule: ${SCHEDULE})"
else
  echo "[dry-run] would ${action} skill-autosave cron (schedule: ${SCHEDULE}); pass --apply to write"
  echo "[dry-run] resulting crontab:"
  printf '%s\n' "$desired" | sed 's/^/  /'
fi
