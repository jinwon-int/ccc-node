#!/usr/bin/env bash
# Install the daily Hermes-style skill-autosave sweep as a crontab entry.
#
# ccc-skill-autosave.sh closes the gap that SessionEnd hooks never fire for
# Telegram-bridge / SDK sessions: it refreshes the skill-candidate report,
# drafts skills from recent transcripts through skill-review.sh, and queues an
# owner-only Telegram notification when drafts await approval.
#
# Consistent with install-memory-refresh-cron.sh: SAFE BY DEFAULT (dry-run
# unless --apply), idempotent (a single marker-tagged entry), never prints
# secrets, and the harness setup.sh never installs this itself. The managed
# entry carries a `gen=h_<sha256:12>` stamp over this script plus the shared
# cron-installer lib (#1081, inputs owned by ccc_installer_gen_inputs) so
# ccc-doctor can tell when the installed entry was rendered by older code.
# The stamp rides on the entry line only — the BEGIN/END block markers stay
# exact-matchable by the block parser.
#
# The managed unit is a BEGIN/END block (unified removal strategy, #1077 —
# this installer's block carries an extra CRON_TZ pin line, which is why the
# fleet strategy converged on blocks): scripts/lib/installer-cron-common.sh
# owns block parsing and the whole install/remove flow; legacy bare marker
# lines are migrated into a block on the next apply.
#
# The cron entry runs through `bash -lc` so the login profile PATH is loaded;
# the sweep shells out to jq/find and skill-review.sh shells out to `claude`,
# which a bare cron PATH (especially on Termux) would not resolve.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SELF="$ROOT/scripts/install-skill-autosave-cron.sh"
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
# #1655: bake the provider lane into the cron line. piri (and codex) are
# explicit-only/auto-detected providers, so a scheduled sweep that should draft
# or install on a non-Claude lane needs the provider recorded in the entry —
# hand-edited crontabs are overwritten by the next reinstall.
OPT_PROVIDER=""
OPT_PIRI_DRAFTING=0
OPT_CODEX_DRAFTING=0
# #1653 follow-up: bake the promotion staging provider set so the scheduled
# sweep stages piri (or any non-default) candidates without hand-edited
# crontabs. Validated against the promoter's own _PROVIDERS vocabulary.
OPT_PROMOTION_PROVIDERS=""
# #1657 owner-decision-B follow-up: danso is a full provider, but its install
# target resolves ONLY from DANSO_SKILLS_DIR / CCC_DANSO_STATE_DIR (fail-closed,
# #1659), so a scheduled sweep needs the state dir — and the drafting opt-in —
# baked into the entry itself, exactly like the #1655 provider lane.
OPT_DANSO_DRAFTING=0
OPT_DANSO_STATE_DIR=""

# Shared installer libs (#1081, #1077): gen stamps + records, and the common
# crontab install/remove driver.
GEN_STAMP_LIB="$ROOT/scripts/lib/installer-gen-stamp.sh"
CRON_COMMON_LIB="$ROOT/scripts/lib/installer-cron-common.sh"
for lib in "$GEN_STAMP_LIB" "$CRON_COMMON_LIB"; do
  if [ ! -r "$lib" ]; then
    echo "shared installer library is missing: $lib" >&2
    exit 4
  fi
  # shellcheck source=/dev/null
  . "$lib"
done
GEN="$(ccc_installer_gen_stamp_auto "$SELF")"

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
crontab. Idempotent: re-running replaces the managed
"$BLOCK_BEGIN" .. "$BLOCK_END" block
(and migrates any legacy bare "$MARKER" line into it).

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
  --provider NAME  Bake CCC_SKILL_PROVIDER=NAME (claude|codex|piri|danso) into
                   the entry so the scheduled sweep resolves the provider lane
                   explicitly (#1655). Defaults to \$CCC_SKILL_PROVIDER when set;
                   otherwise the entry carries no provider and the sweep
                   auto-detects as before. piri is explicit-only (#643), so piri
                   nodes must pass --provider piri (or export
                   CCC_SKILL_PROVIDER=piri) for the piri target to engage.
                   danso nodes pass --provider danso together with
                   --danso-state-dir: the danso install target fails closed
                   when neither DANSO_SKILLS_DIR nor CCC_DANSO_STATE_DIR is
                   set (#1659).
  --piri-drafting  Bake CCC_SKILL_PIRI_DRAFTING=1 into the entry (opt-in piri
                   sweep branch; mirrors the codex flag below).
  --codex-drafting Bake CCC_SKILL_CODEX_DRAFTING=1 into the entry.
  --danso-drafting Bake CCC_SKILL_DANSO_DRAFTING=1 into the entry (opt-in
                   danso journal drafting sweep branch, #1660).
  --danso-state-dir PATH
                   Bake CCC_DANSO_STATE_DIR=PATH into the entry so the
                   scheduled sweep resolves the bridge-fixed danso HOME
                   (<PATH>/home, #1659). Defaults to \$CCC_DANSO_STATE_DIR when
                   set; otherwise omitted and the danso lane fails closed in
                   the sweep. PATH must be absolute and must not contain a
                   double quote, dollar, backtick, or backslash (it is baked
                   inside a double-quoted segment of the cron line).
  --promotion-providers LIST
                   Bake CCC_SKILL_PROMOTION_PROVIDERS=LIST (comma-separated,
                   each of claude|codex|piri) into the entry so scheduled
                   promotion staging scans those provider roots. Defaults to
                   \$CCC_SKILL_PROMOTION_PROVIDERS when set; otherwise omitted
                   (the promoter default claude,codex applies). piri nodes
                   wanting daily piri staging pass claude,piri.

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_SKILL_AUTOSAVE_CMD,
CCC_SKILL_AUTOSAVE_CRON, CCC_SKILL_AUTOSAVE_CRON_LOG, CCC_CRONTAB_CMD,
CCC_SKILL_PROVIDER (inherited as the baked provider when --provider is unset),
CCC_SKILL_PROMOTION_PROVIDERS (inherited when --promotion-providers is unset),
CCC_DANSO_STATE_DIR (inherited when --danso-state-dir is unset).
CCC_SKILL_AUTOSAVE_LOCAL_TIMEZONE and CCC_SKILL_AUTOSAVE_LOCAL_UTC_OFFSET
(+HHMM/-HHMM) are advanced deterministic overrides for image builds and
tests; normal installs auto-detect both.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --remove) REMOVE=1 ;;
    --schedule) ccc_cron_need_val "$1" "${2:-}"; SCHEDULE="$2"; shift ;;
    --node) ccc_cron_need_val "$1" "${2:-}"; OPT_NODE="$2"; shift ;;
    --provider)
      ccc_cron_need_val "$1" "${2:-}"
      case "$2" in
        claude|codex|piri|danso) OPT_PROVIDER="$2" ;;
        *) echo "invalid --provider '$2' (want claude|codex|piri|danso)" >&2; exit 2 ;;
      esac
      shift ;;
    --piri-drafting) OPT_PIRI_DRAFTING=1 ;;
    --codex-drafting) OPT_CODEX_DRAFTING=1 ;;
    --danso-drafting) OPT_DANSO_DRAFTING=1 ;;
    --danso-state-dir)
      ccc_cron_need_val "$1" "${2:-}"
      OPT_DANSO_STATE_DIR="$2"
      shift ;;
    --promotion-providers)
      ccc_cron_need_val "$1" "${2:-}"
      _pp_ok=1
      IFS=',' read -ra _pp_parts <<<"$2"
      for _pp in "${_pp_parts[@]}"; do
        case "$_pp" in
          claude|codex|piri|danso) ;;
          *) _pp_ok=0; break ;;
        esac
      done
      [ "$_pp_ok" = 1 ] || { echo "invalid --promotion-providers '$2' (comma list of claude|codex|piri)" >&2; exit 2; }
      unset _pp_ok _pp_parts _pp
      OPT_PROMOTION_PROVIDERS="$2"
      shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ ! -f "$AUTOSAVE" ] && [ -f "$ROOT/scripts/ccc-skill-autosave.sh" ]; then
  AUTOSAVE="$ROOT/scripts/ccc-skill-autosave.sh"
fi

FLEET_NODE="$(resolve_fleet_node)"
# #1655: --provider > inherited \$CCC_SKILL_PROVIDER > none (auto-detect).
CRON_PROVIDER="$OPT_PROVIDER"
if [ -z "$CRON_PROVIDER" ] && [ -n "${CCC_SKILL_PROVIDER:-}" ]; then
  case "$CCC_SKILL_PROVIDER" in
    claude|codex|piri) CRON_PROVIDER="$CCC_SKILL_PROVIDER" ;;
  esac
fi
CRON_ENV=""
if [ -n "$FLEET_NODE" ]; then
  CRON_ENV="CCC_NODE=\"$FLEET_NODE\" CCC_CLAUDE_DIR=\"$CLAUDE_DIR\""
else
  CRON_ENV="CCC_CLAUDE_DIR=\"$CLAUDE_DIR\""
  # Install anyway: the entry also refreshes candidates, drafts skills and
  # queues owner notifications, and those work without a fleet identity. Only
  # skill-promotion staging needs it, and that is opt-in and already fail-closed
  # (#1068) — so warn where the operator can see it instead of blocking cron.
  echo "WARNING: no fleet identity resolved (--node, \$CCC_NODE, $STATE_DIR/node.txt)." >&2
  echo "         Installing without CCC_NODE; scheduled skill-promotion staging will" >&2
  echo "         refuse with node_identity_unresolved until one is provided (#1067)." >&2
fi
[ -n "$CRON_PROVIDER" ] && CRON_ENV="$CRON_ENV CCC_SKILL_PROVIDER=\"$CRON_PROVIDER\""
[ "$OPT_PIRI_DRAFTING" = 1 ] && CRON_ENV="$CRON_ENV CCC_SKILL_PIRI_DRAFTING=1"
[ "$OPT_CODEX_DRAFTING" = 1 ] && CRON_ENV="$CRON_ENV CCC_SKILL_CODEX_DRAFTING=1"
# --danso-state-dir > inherited \$CCC_DANSO_STATE_DIR > omitted. Without it the
# scheduled danso lane fails closed (#1659), so say so at install time where
# the operator can see it instead of letting the entry no-op the lane silently.
danso_state_dir_ok() { # <path> — absolute and safe to bake inside double quotes
  local path="$1"
  [ -n "$path" ] || return 1
  case "$path" in /*) ;; *) return 1 ;; esac
  case "$path" in *'"'*|*'$'*|*'`'*|*'\'*) return 1 ;; esac
  return 0
}
CRON_DANSO_STATE_DIR="$OPT_DANSO_STATE_DIR"
if [ -n "$CRON_DANSO_STATE_DIR" ]; then
  danso_state_dir_ok "$CRON_DANSO_STATE_DIR" || {
    echo "invalid --danso-state-dir '$CRON_DANSO_STATE_DIR' (absolute path; no double quote, dollar, backtick, or backslash)" >&2
    exit 2
  }
elif [ -n "${CCC_DANSO_STATE_DIR:-}" ]; then
  if danso_state_dir_ok "$CCC_DANSO_STATE_DIR"; then
    CRON_DANSO_STATE_DIR="$CCC_DANSO_STATE_DIR"
  else
    echo "WARNING: ignoring invalid inherited CCC_DANSO_STATE_DIR (absolute path; no double quote, dollar, backtick, or backslash)." >&2
  fi
fi
if [ "$CRON_PROVIDER" = "danso" ] && [ -z "$CRON_DANSO_STATE_DIR" ]; then
  echo "WARNING: danso lane without a state dir: the scheduled sweep will fail closed" >&2
  echo "         (DANSO_SKILLS_DIR/CCC_DANSO_STATE_DIR unset, #1659). Pass --danso-state-dir." >&2
fi
[ -n "$CRON_DANSO_STATE_DIR" ] && CRON_ENV="$CRON_ENV CCC_DANSO_STATE_DIR=\"$CRON_DANSO_STATE_DIR\""
[ "$OPT_DANSO_DRAFTING" = 1 ] && CRON_ENV="$CRON_ENV CCC_SKILL_DANSO_DRAFTING=1"
# --promotion-providers > inherited \$CCC_SKILL_PROMOTION_PROVIDERS > omitted
CRON_PROMOTION_PROVIDERS="$OPT_PROMOTION_PROVIDERS"
if [ -z "$CRON_PROMOTION_PROVIDERS" ] && [ -n "${CCC_SKILL_PROMOTION_PROVIDERS:-}" ]; then
  _pp_inherit_ok=1
  IFS=',' read -ra _pp_parts <<<"$CCC_SKILL_PROMOTION_PROVIDERS"
  for _pp in "${_pp_parts[@]}"; do
    case "$_pp" in
      claude|codex|piri|danso) ;;
      *) _pp_inherit_ok=0; break ;;
    esac
  done
  [ "$_pp_inherit_ok" = 1 ] && CRON_PROMOTION_PROVIDERS="$CCC_SKILL_PROMOTION_PROVIDERS"
  unset _pp_inherit_ok _pp_parts _pp
fi
[ -n "$CRON_PROMOTION_PROVIDERS" ] && CRON_ENV="$CRON_ENV CCC_SKILL_PROMOTION_PROVIDERS=\"$CRON_PROMOTION_PROVIDERS\""
CRON_LINE="$SCHEDULE bash -lc '$CRON_ENV \"$AUTOSAVE\" run' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

# Install record (#1081 phase 2): replay must reproduce THIS entry, so the
# resolved schedule and fleet identity are materialized into argv rather than
# re-derived from the operator's environment.
record_argv=(--apply --schedule "$SCHEDULE")
[ -n "$FLEET_NODE" ] && record_argv+=(--node "$FLEET_NODE")
[ -n "$CRON_PROVIDER" ] && record_argv+=(--provider "$CRON_PROVIDER")
[ "$OPT_PIRI_DRAFTING" = 1 ] && record_argv+=(--piri-drafting)
[ "$OPT_CODEX_DRAFTING" = 1 ] && record_argv+=(--codex-drafting)
[ "$OPT_DANSO_DRAFTING" = 1 ] && record_argv+=(--danso-drafting)
[ -n "$CRON_DANSO_STATE_DIR" ] && record_argv+=(--danso-state-dir "$CRON_DANSO_STATE_DIR")
[ -n "$CRON_PROMOTION_PROVIDERS" ] && record_argv+=(--promotion-providers "$CRON_PROMOTION_PROVIDERS")

# The block body carries the CRON_TZ pin ahead of the entry line (cron has no
# per-job inline timezone syntax; the pin keeps an unrelated earlier CRON_TZ
# assignment from rescheduling this job).
ccc_cron_installer_finish \
  --label "skill-autosave" \
  --marker "$MARKER" --begin "$BLOCK_BEGIN" --end "$BLOCK_END" \
  --crontab "$CRONTAB" --state-dir "$STATE_DIR" --self "$SELF" --gen "$GEN" \
  --apply "$APPLY" --remove "$REMOVE" --schedule-desc "$SCHEDULE" \
  --body "$(printf 'CRON_TZ=%s\n%s' "$LOCAL_TIMEZONE" "$CRON_LINE")" -- \
  "${record_argv[@]}"
