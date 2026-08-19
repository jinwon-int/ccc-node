#!/usr/bin/env bash
# installer-cron-common.sh — shared machinery for the three crontab
# installers (#1077): install-memory-refresh-cron.sh,
# install-pr-status-poll-cron.sh, install-skill-autosave-cron.sh.
#
# Before this lib, pr-status-poll was a ~67% character-identical clone of
# memory-refresh and the duplication was still GROWING when the issue was
# filed. The three installers now keep only what actually differs — defaults,
# usage text, their argument loop, the rendered entry line, and installer-
# specific preconditions — and funnel everything else through
# ccc_cron_installer_finish below.
#
# Removal strategy (unified here, was the issue's precondition): every
# installer manages a BEGIN/END block, not a bare marker line.
#   - autosave must co-manage a CRON_TZ pin with its entry (cron has no
#     per-job inline timezone syntax), and a block is the only atomic unit
#     that covers two lines.
#   - The block parser is a strict superset of the old `grep -vF "$MARKER"`:
#     it also drops LEGACY bare marker lines outside any block, so stamped
#     pre-#1077 entries migrate on the next apply with no special casing.
#   - Unbalanced/foreign-corrupted blocks fail closed (exit 42 from the
#     parser, mapped to exit 4 by the driver) instead of being edited around.
#   - The gen stamp (#1081) rides on the entry line only; BEGIN/END markers
#     stay exact-matchable and unstamped.
#
# This lib expects installer-gen-stamp.sh to be available (record helpers are
# used by the driver); it sources it itself when the caller has not.

_CRON_COMMON_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v ccc_installer_record_write >/dev/null 2>&1; then
  # shellcheck source=/dev/null
  . "$_CRON_COMMON_LIB_DIR/installer-gen-stamp.sh" || return 4 2>/dev/null || exit 4
fi

# ccc_cron_need_val <flag> <value> — shared "--flag requires a value" guard.
ccc_cron_need_val() { [ -n "${2:-}" ] || { echo "$1 requires a value" >&2; exit 2; }; }

# ccc_cron_check_crontab <crontab-cmd> — exit 3 when the command is absent.
# The command may carry arguments (e.g. a stub with flags), so probe the first
# word only.
ccc_cron_check_crontab() {
  if ! command -v "${1%% *}" >/dev/null 2>&1; then
    echo "crontab command not found ('$1'); cannot manage cron on this node" >&2
    exit 3
  fi
}

# ccc_cron_strip_managed <begin> <end> <marker> — stdin → stdout.
# Removes this installer's managed block (exact-matched begin/end) AND any
# legacy bare lines carrying the marker outside a block. Exit 42 when the
# block structure is corrupt (nested begin, end without begin, unterminated
# block) so the driver can refuse to edit a crontab it cannot parse safely.
ccc_cron_strip_managed() {
  awk -v begin="$1" -v end="$2" -v marker="$3" '
    $0 == begin { if (skip) bad=1; skip=1; next }
    $0 == end { if (!skip) bad=1; skip=0; next }
    !skip && index($0, marker) == 0 { print }
    END { if (skip || bad) exit 42 }
  '
}

# ccc_cron_root_scope_warning <label> [euid] [root-home] [home-parent] —
# warn when an installer runs as root on a node whose harness lives under a
# service account. #1079: install-nunchi.sh ran once as root on gongmyoung,
# and the resulting root-crontab entries failed on every tick for WEEKS —
# the real harness had moved to the gongmyoung account and nothing could see
# root's crontab. Every installer that resolves paths from $HOME writes a
# full second (dead) install on a root invocation. Legit root nodes (a real
# /root/.claude harness) stay silent.
# Warning-only, like the #1186 nunchi ghost warning — removal/scope choice
# stays an operator decision. The euid/home args are test seams.
ccc_cron_root_scope_warning() {
  local label="$1" euid="${2:-$(id -u)}" root_home="${3:-/root}" home_parent="${4:-/home}"
  [ "$euid" = 0 ] || return 0
  [ -d "$root_home/.claude" ] && return 0
  local d found=""
  for d in "$home_parent"/*/.claude; do
    [ -d "$d" ] || continue
    found="$found ${d%/.claude}"
  done
  [ -n "$found" ] || return 0
  echo "WARNING ($label): running as root but no $root_home/.claude — harness lives under:$found" >&2
  echo "  this would install for ROOT (HOME=/root), not the service account (the #1079 ghost class);" >&2
  echo "  re-run as the service account, e.g.: runuser -u <user> -- $0 $*" >&2
}

# ccc_cron_installer_finish — one driver for the shared install/remove flow.
#
#   --label TEXT         short lane name for messages ("memory-refresh")
#   --marker TEXT        entry marker ("# ccc-node:memory-refresh")
#   --begin TEXT         block begin marker (exact-match line)
#   --end TEXT           block end marker (exact-match line)
#   --crontab CMD        crontab command (CCC_CRONTAB_CMD-resolved)
#   --state-dir DIR      node state dir (install records live here)
#   --self PATH          installer absolute path (record identity)
#   --gen STAMP          h_<hex12> stamp computed by the caller
#   --apply 0|1          write when 1, dry-run print when 0
#   --remove 0|1         remove the managed block instead of installing
#   --schedule-desc TEXT schedule echoed in the done/would messages
#   --body LINES         newline-joined block body (CRON_TZ line + entry, or
#                        just the entry) placed between begin and end
#   --                   everything after a literal -- is the install-record
#                        argv (the RESOLVED invocation self-update replays)
#
# Exit codes: 2 bad driver args, 3 crontab missing, 4 corrupt managed block.
ccc_cron_installer_finish() {
  local label="" marker="" begin="" end="" crontab="" state_dir="" self="" gen=""
  local apply=0 remove=0 schedule_desc="" body=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --label) ccc_cron_need_val "$1" "${2:-}"; label="$2"; shift ;;
      --marker) ccc_cron_need_val "$1" "${2:-}"; marker="$2"; shift ;;
      --begin) ccc_cron_need_val "$1" "${2:-}"; begin="$2"; shift ;;
      --end) ccc_cron_need_val "$1" "${2:-}"; end="$2"; shift ;;
      --crontab) ccc_cron_need_val "$1" "${2:-}"; crontab="$2"; shift ;;
      --state-dir) ccc_cron_need_val "$1" "${2:-}"; state_dir="$2"; shift ;;
      --self) ccc_cron_need_val "$1" "${2:-}"; self="$2"; shift ;;
      --gen) ccc_cron_need_val "$1" "${2:-}"; gen="$2"; shift ;;
      --apply) ccc_cron_need_val "$1" "${2:-}"; apply="$2"; shift ;;
      --remove) ccc_cron_need_val "$1" "${2:-}"; remove="$2"; shift ;;
      --schedule-desc) ccc_cron_need_val "$1" "${2:-}"; schedule_desc="$2"; shift ;;
      --body) body="$2"; shift ;;  # may legitimately be empty only for tests
      --) shift; break ;;
      *) echo "installer-cron-common: unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
  done
  local missing=""
  [ -n "$label" ] || missing="$missing --label"
  [ -n "$marker" ] || missing="$missing --marker"
  [ -n "$begin" ] || missing="$missing --begin"
  [ -n "$end" ] || missing="$missing --end"
  [ -n "$crontab" ] || missing="$missing --crontab"
  [ -n "$state_dir" ] || missing="$missing --state-dir"
  [ -n "$self" ] || missing="$missing --self"
  [ -n "$gen" ] || missing="$missing --gen"
  [ -n "$schedule_desc" ] || missing="$missing --schedule-desc"
  if [ -n "$missing" ]; then
    echo "installer-cron-common: finish missing required:$missing" >&2
    exit 2
  fi

  ccc_cron_check_crontab "$crontab"
  ccc_cron_root_scope_warning "$label"

  local current without_marker desired action
  current="$("$crontab" -l 2>/dev/null || true)"
  if ! without_marker="$(printf '%s\n' "$current" | ccc_cron_strip_managed "$begin" "$end" "$marker")"; then
    echo "$label cron: corrupt managed schedule block; refusing to edit" >&2
    exit 4
  fi

  if [ "$remove" = 1 ]; then
    desired="$without_marker"
    action="remove"
  else
    desired="$(printf '%s\n%s\n%s\n%s' "$without_marker" "$begin" "$body" "$end" | sed '/^$/d')"
    action="install"
  fi

  if [ "$apply" = 1 ]; then
    printf '%s\n' "$desired" | "$crontab" -
    if [ "$remove" = 1 ]; then
      # A deliberate removal must drop the record too, or the next self-update
      # re-apply would resurrect the entry.
      ccc_installer_record_remove "$state_dir" "$self" || true
    else
      # Install record (#1081 phase 2): lets self-update replay this exact
      # resolved invocation when the gen stamp drifts. Best-effort — the cron
      # change above is already done and must not be reported as failed.
      ccc_installer_record_write "$state_dir" "$self" "$marker" "$gen" -- "$@" \
        || echo "WARNING: install record write failed — self-update re-apply will not track this entry" >&2
    fi
    echo "$label cron: ${action} done (schedule: ${schedule_desc})"
  else
    echo "[dry-run] would ${action} $label cron (schedule: ${schedule_desc}); pass --apply to write"
    echo "[dry-run] resulting crontab:"
    printf '%s\n' "$desired" | sed 's/^/  /'
  fi
}
