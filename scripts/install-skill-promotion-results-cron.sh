#!/usr/bin/env bash
# Install the publisher's 15-minute skill-promotion results collector as a
# crontab entry (#2239, follow-up to #2232).
#
# `ccc-skill-promotion.py collect --results-only` consumes review/revision
# results and runs the promotion drain without reading outboxes or admitting
# new intake (docs/skill-autosave.md, "Frequent review-result collection").
# Until now the only installation of it was an ad-hoc crontab line pointing at
# a repair worktree (`skill-pipeline-repair-20261004/run-results.py`) that no
# installer rendered, no gen stamp tracked and no `--remove` could undo. This
# installer replaces that line with a managed block, the same way the other
# cron lanes are managed.
#
# Consistent with install-skill-autosave-cron.sh: SAFE BY DEFAULT (dry-run
# unless --apply), idempotent (a single marker-tagged entry in a BEGIN/END
# block, #1077), never prints secrets, and setup.sh never installs this
# itself. The entry carries a `gen=h_<sha256:12>` stamp over this script plus
# the shared cron lib (#1081, inputs owned by ccc_installer_gen_inputs) so
# ccc-doctor can tell when it was rendered by older code.
#
# PUBLISHER ONLY. `--apply` refuses unless <state>/skill-promotion.publisher
# reads `true` (owner-only, the same file the promoter reads): a non-publisher
# running this collector every 15 minutes would only churn the promotion lock.
#
# PLACEMENT. The entry must follow a CRON_TZ line. When the autosave block
# (`# ccc-node:autosave-schedule:begin/end`, which carries the node's CRON_TZ
# pin) exists, this block is rendered directly below it and inherits that pin;
# otherwise the block opens with its own `CRON_TZ=Asia/Seoul` line. The driver
# in installer-cron-common.sh always appends at the end of the crontab, so the
# placement is rendered here from the lib's lower-level pieces (block parser,
# crontab guard, root-scope warning, install records).
#
# LOCK. The promoter's `--lock-wait 0` is passed explicitly: this job yields to
# the nightly sweep's bounded wait (#2232) instead of competing with it. A
# locked run is the benign `ok: true, status: "locked"` skip and the next tick
# retries.
#
# ENV. The rendered line sources, inside `bash -lc`, first the publisher
# config file (<CCC_SKILL_PROMOTION_ENV_FILE>, default
# ~/.config/ccc-node/skill-promotion.env) when it is a regular owner-only file
# we own, then the publisher edge env the way ccc-skill-autosave.sh's
# collect_with_edge_env does — so edge env and the baked CCC_NODE/CCC_CLAUDE_DIR
# win over the file, exactly like the sweep. The promoter re-reads the config
# file itself with its CCC_SKILL_PROMOTION_* whitelist; `status` shows what it
# applied.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SELF="$ROOT/scripts/install-skill-promotion-results-cron.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
PROMOTER="${CCC_SKILL_PROMOTION_CMD:-$CLAUDE_DIR/hooks/ccc-skill-promotion.py}"
LOG="${CCC_SKILL_PROMOTION_RESULTS_CRON_LOG:-$STATE_DIR/skill-promotion.results.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:skill-promotion-results"
BLOCK_BEGIN="# ccc-node:skill-promotion-results:begin"
BLOCK_END="# ccc-node:skill-promotion-results:end"
# The autosave block's END line is the anchor this block is rendered below.
AUTOSAVE_BLOCK_END="# ccc-node:autosave-schedule:end"
# Legacy ad-hoc collector line (2026-10-04 repair worktree). Detected and
# reported always; removed only with --apply --migrate-legacy.
LEGACY_NEEDLE="skill-pipeline-repair-20261004/run-results.py"
PUBLISHER_FLAG="$STATE_DIR/skill-promotion.publisher"
# Same defaults as ccc-skill-autosave.sh / ccc-skill-promotion.py (#1766, #2239).
PROMOTION_ENV="${CCC_SKILL_PROMOTION_ENV_FILE:-$HOME/.config/ccc-node/skill-promotion.env}"
EDGE_ENV="${CCC_A2A_EDGE_ENV:-$HOME/.a2a-broker-edge.env}"
CRON_TZ_PIN="${CCC_SKILL_PROMOTION_RESULTS_CRON_TZ:-Asia/Seoul}"
SCHEDULE="${CCC_SKILL_PROMOTION_RESULTS_CRON:-*/15 * * * *}"
APPLY=0
REMOVE=0
STATUS=0
MIGRATE_LEGACY=0
OPT_NODE=""

# Shared installer libs (#1081, #1077): gen stamps + records, block parser,
# crontab guard, root-scope warning.
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

# Fleet identity (#1067): same resolution and the same no-hostname-guess rule
# as install-skill-autosave-cron.sh. The results collector needs no identity
# for its own work, but the promoter stamps its ledger rows with it, so bake it
# when it resolves and say so when it does not.
sanitize_node() { # <raw> — mirrors _safe_node() in scripts/ccc-skill-promotion.py
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9-]+/-/g; s/-+/-/g; s/^-+//; s/-+$//' \
    | cut -c1-32 | sed -E 's/-+$//'
}
resolve_fleet_node() { # --node > CCC_NODE > $STATE_DIR/node.txt
  local raw=""
  if [ -n "$OPT_NODE" ]; then raw="$OPT_NODE"
  elif [ -n "${CCC_NODE:-}" ]; then raw="${CCC_NODE:-}"
  elif [ -r "$STATE_DIR/node.txt" ]; then raw="$(head -1 "$STATE_DIR/node.txt" 2>/dev/null || true)"
  fi
  sanitize_node "$raw"
}

usage() {
  cat <<EOF
Usage: install-skill-promotion-results-cron.sh [--dry-run|--apply] [--remove] [--status]
                                               [--migrate-legacy] [--schedule SPEC] [--node NAME]

Installs (or removes) the publisher's crontab entry that runs
  ccc-skill-promotion.py collect --results-only --lock-wait 0
every 15 minutes with the publisher config file and edge env loaded.
Defaults to dry-run; --apply is required to change the crontab. Idempotent:
re-running replaces the managed "$BLOCK_BEGIN" .. "$BLOCK_END" block.

Options:
  --dry-run          Show the resulting crontab without changing it (default).
  --apply            Write the crontab change. Refused (exit 5) unless
                     $PUBLISHER_FLAG
                     reads true (owner-only), and refused (exit 6) while the
                     legacy ad-hoc collector line is present without
                     --migrate-legacy.
  --remove           Remove the managed block (with --apply) instead of adding it.
  --status           Print publisher flag, config/edge env presence, managed
                     block + gen, legacy line and install record; exit 0.
  --migrate-legacy   With --apply: also drop crontab line(s) containing
                     "$LEGACY_NEEDLE".
  --schedule SPEC    Cron schedule (5 fields). Default: "$SCHEDULE".
  --node NAME        Fleet identity baked as CCC_NODE (default \$CCC_NODE, then
                     the first line of \$STATE_DIR/node.txt; never the hostname).

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_SKILL_PROMOTION_CMD,
CCC_SKILL_PROMOTION_RESULTS_CRON, CCC_SKILL_PROMOTION_RESULTS_CRON_LOG,
CCC_SKILL_PROMOTION_RESULTS_CRON_TZ (default Asia/Seoul; only used when no
autosave block exists to sit below), CCC_SKILL_PROMOTION_ENV_FILE,
CCC_A2A_EDGE_ENV, CCC_CRONTAB_CMD.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --remove) REMOVE=1 ;;
    --status) STATUS=1 ;;
    --migrate-legacy) MIGRATE_LEGACY=1 ;;
    --schedule) ccc_cron_need_val "$1" "${2:-}"; SCHEDULE="$2"; shift ;;
    --node) ccc_cron_need_val "$1" "${2:-}"; OPT_NODE="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ ! -f "$PROMOTER" ] && [ -f "$ROOT/scripts/ccc-skill-promotion.py" ]; then
  PROMOTER="$ROOT/scripts/ccc-skill-promotion.py"
fi

# Every path baked into the entry lands inside a double-quoted segment of a
# single-quoted `bash -lc` body: both quote flavors and every expansion/escape
# metachar are rejected outright (same rule as --danso-state-dir, #1707).
cron_path_ok() { # <path>
  local path="$1"
  [ -n "$path" ] || return 1
  case "$path" in /*) ;; *) return 1 ;; esac
  case "$path" in *'"'*|*"'"*|*'$'*|*'`'*|*'\'*) return 1 ;; esac
  return 0
}
for _p in "$CLAUDE_DIR" "$PROMOTER" "$LOG" "$PROMOTION_ENV" "$EDGE_ENV"; do
  cron_path_ok "$_p" || {
    echo "refusing to bake unsafe path '$_p' (absolute; no quote, dollar, backtick, or backslash)" >&2
    exit 2
  }
done
unset _p
case "$SCHEDULE" in
  *"'"*|*'"'*|*'$'*|*'`'*|*'\'*|*'#'*|'') echo "invalid --schedule '$SCHEDULE'" >&2; exit 2 ;;
esac

# Publisher opt-in, read like the promoter's _read_enabled_file: regular file we
# own, mode exactly 0600, at most 32 bytes, content `true`.
publisher_flag_true() {
  local f="$PUBLISHER_FLAG" size
  [ -f "$f" ] && [ ! -L "$f" ] && [ -O "$f" ] || return 1
  [ "$(stat -c %a "$f" 2>/dev/null)" = 600 ] || return 1
  size="$(stat -c %s "$f" 2>/dev/null)" || return 1
  [ "$size" -le 32 ] || return 1
  [ "$(tr -d '[:space:]' < "$f" | tr '[:upper:]' '[:lower:]')" = true ]
}
owner_only_regular() { # <path> — regular file we own, no group/other bits
  [ -f "$1" ] && [ ! -L "$1" ] && [ -O "$1" ] || return 1
  case "$(stat -c %a "$1" 2>/dev/null)" in [0-7]00) return 0 ;; esac
  return 1
}
file_state() { # <path> → present | unsafe | absent
  if owner_only_regular "$1"; then echo present
  elif [ -e "$1" ] || [ -L "$1" ]; then echo unsafe
  else echo absent; fi
}

FLEET_NODE="$(resolve_fleet_node)"
CRON_ENV="CCC_CLAUDE_DIR=\"$CLAUDE_DIR\""
[ -n "$FLEET_NODE" ] && CRON_ENV="CCC_NODE=\"$FLEET_NODE\" $CRON_ENV"

# The body of the `bash -lc` call. P = publisher config file (sourced only when
# it is a regular owner-only file we own), E = edge env (sourced when present,
# as collect_with_edge_env does). Both use `set -a` so `KEY=value` and
# `export KEY=value` files work; `|| :` keeps a broken file from aborting the
# tick; the files' own output never reaches the log.
CRON_BODY="P=\"$PROMOTION_ENV\"; if [ -f \"\$P\" ] && [ ! -L \"\$P\" ] && [ -O \"\$P\" ] && [ -z \"\$(find \"\$P\" -maxdepth 0 -perm /077 2>/dev/null)\" ]; then set -a; . \"\$P\" >/dev/null 2>&1 || :; set +a; fi; E=\"$EDGE_ENV\"; if [ -f \"\$E\" ]; then set -a; . \"\$E\" >/dev/null 2>&1 || :; set +a; fi; $CRON_ENV python3 \"$PROMOTER\" collect --results-only --lock-wait 0"
CRON_LINE="$SCHEDULE bash -lc '$CRON_BODY' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

record_argv=(--apply --schedule "$SCHEDULE")
[ -n "$FLEET_NODE" ] && record_argv+=(--node "$FLEET_NODE")

ccc_cron_check_crontab "$CRONTAB"
current="$("$CRONTAB" -l 2>/dev/null || true)"
legacy_count="$(printf '%s\n' "$current" | grep -cF -- "$LEGACY_NEEDLE" || true)"
legacy_state=absent
[ "$legacy_count" -gt 0 ] && legacy_state=present
if publisher_flag_true; then publisher_state=true; else publisher_state=false; fi
installed_gen="$(printf '%s\n' "$current" | grep -F -- "$MARKER" | grep -oE 'gen=h_[0-9a-f]{12}' | head -1 | sed 's/^gen=//' || true)"

if [ "$STATUS" = 1 ]; then
  echo "skill-promotion-results cron status"
  echo "  publisher flag: $publisher_state ($PUBLISHER_FLAG)"
  echo "  promotion env file: $(file_state "$PROMOTION_ENV") ($PROMOTION_ENV)"
  echo "  a2a edge env: $([ -f "$EDGE_ENV" ] && echo present || echo absent) ($EDGE_ENV)"
  if printf '%s\n' "$current" | grep -qxF -- "$BLOCK_BEGIN"; then
    if [ -n "$installed_gen" ] && [ "$installed_gen" = "$GEN" ]; then
      echo "  managed block: installed gen=$installed_gen (current)"
    else
      echo "  managed block: installed gen=${installed_gen:-unstamped} != current gen=$GEN — re-run --apply"
    fi
  else
    echo "  managed block: absent (current gen=$GEN)"
  fi
  echo "  legacy_results_cron: $legacy_state"
  rec="$(ccc_installer_record_path "$STATE_DIR" "$SELF")"
  echo "  install record: $([ -f "$rec" ] && echo present || echo absent) ($rec)"
  exit 0
fi

ccc_cron_root_scope_warning "skill-promotion-results"

if ! without="$(printf '%s\n' "$current" | ccc_cron_strip_managed "$BLOCK_BEGIN" "$BLOCK_END" "$MARKER")"; then
  echo "skill-promotion-results cron: corrupt managed schedule block; refusing to edit" >&2
  exit 4
fi
legacy_action=kept
if [ "$MIGRATE_LEGACY" = 1 ] && [ "$legacy_state" = present ]; then
  without="$(printf '%s\n' "$without" | grep -vF -- "$LEGACY_NEEDLE" || true)"
  legacy_action=removed
fi

placement="none"
if [ "$REMOVE" = 1 ]; then
  desired="$without"
  action="remove"
elif printf '%s\n' "$without" | grep -qxF -- "$AUTOSAVE_BLOCK_END"; then
  # Directly below the autosave block: its CRON_TZ pin covers this entry.
  placement="below autosave block (inherits its CRON_TZ)"
  desired="$(printf '%s\n' "$without" | ANCHOR="$AUTOSAVE_BLOCK_END" BEGIN="$BLOCK_BEGIN" BODY="$CRON_LINE" END="$BLOCK_END" \
    awk '{ print } !done && $0 == ENVIRON["ANCHOR"] { print ENVIRON["BEGIN"]; print ENVIRON["BODY"]; print ENVIRON["END"]; done = 1 }')"
  action="install"
else
  placement="appended with its own CRON_TZ=$CRON_TZ_PIN pin"
  desired="$(printf '%s\n%s\nCRON_TZ=%s\n%s\n%s' "$without" "$BLOCK_BEGIN" "$CRON_TZ_PIN" "$CRON_LINE" "$BLOCK_END")"
  action="install"
fi
desired="$(printf '%s\n' "$desired" | sed '/^$/d')"

refusals=()
if [ "$REMOVE" != 1 ]; then
  [ "$publisher_state" = true ] || refusals+=("publisher flag is not true ($PUBLISHER_FLAG; publisher-only installer) [exit 5]")
  if [ "$legacy_state" = present ] && [ "$MIGRATE_LEGACY" != 1 ]; then
    refusals+=("legacy ad-hoc collector line present ($legacy_count; pass --apply --migrate-legacy to replace it) [exit 6]")
  fi
fi

if [ "$APPLY" = 1 ]; then
  if [ "${#refusals[@]}" -gt 0 ]; then
    for r in "${refusals[@]}"; do echo "REFUSED: $r" >&2; done
    case "${refusals[0]}" in *"[exit 5]") exit 5 ;; *) exit 6 ;; esac
  fi
  printf '%s\n' "$desired" | "$CRONTAB" -
  if [ "$REMOVE" = 1 ]; then
    ccc_installer_record_remove "$STATE_DIR" "$SELF" || true
  else
    ccc_installer_record_write "$STATE_DIR" "$SELF" "$MARKER" "$GEN" -- "${record_argv[@]}" \
      || echo "WARNING: install record write failed — self-update re-apply will not track this entry" >&2
  fi
  echo "skill-promotion-results cron: ${action} done (schedule: ${SCHEDULE}; placement: ${placement}; legacy_results_cron: ${legacy_state}${legacy_state:+ }${legacy_action})"
else
  echo "[dry-run] would ${action} skill-promotion-results cron (schedule: ${SCHEDULE}; placement: ${placement}); pass --apply to write"
  echo "[dry-run] publisher: $publisher_state; legacy_results_cron: $legacy_state ($legacy_action on apply)"
  for r in "${refusals[@]:-}"; do [ -n "$r" ] && echo "[dry-run] --apply would be REFUSED: $r"; done
  echo "[dry-run] resulting crontab:"
  printf '%s\n' "$desired" | sed 's/^/  /'
fi
