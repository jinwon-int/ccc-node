#!/usr/bin/env bash
# skill-review/gates-lib.sh — near-duplicate gate pieces shared by the staging
# hook (skill-review.sh, draft time) and the unattended installer
# (autoinstall.sh, install time). Sourced, never executed; functions and
# constants only, no side effects (#2239).
#
# Until #2239 these lived in autoinstall.sh alone, so a near-duplicate draft was
# only caught AFTER it had entered pending-skills/ — in review mode never by a
# machine at all, and 85%+ of fleet intake verdicts came back "revise", mostly
# trigger wording and duplication. skill-review.sh now runs the same comparison
# before a draft is staged, against the installed skills root of the drafting
# provider and against the drafts already waiting in the queue.
#
# Thresholds (env override → default; malformed values fall back):
#   CCC_SKILL_DEDUP_JACCARD_PCT          DUP_JACCARD_PCT          60
#       description-token Jaccard (percent) at which a candidate counts as a
#       duplicate of an INSTALLED skill. Shared by autoinstall gate_dedup.
#   CCC_SKILL_DEDUP_MIN_UNION            DUP_MIN_UNION            6
#       minimum token-union size before the Jaccard test applies (very short
#       descriptions would otherwise collide on two or three words).
#   CCC_SKILL_DEDUP_PENDING_JACCARD_PCT  DUP_PENDING_JACCARD_PCT  75
#       stricter Jaccard for candidate-vs-PENDING-draft comparison. Pending
#       drafts are unreviewed model output, often several variations of one
#       procedure from one session, so an installed-level threshold would
#       reject too many genuinely different follow-ups.
#
# Requires ccc_fm_field (lib/skill-frontmatter.sh); sourced here when absent.

if ! declare -f ccc_fm_field >/dev/null 2>&1; then
  _ccc_gates_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || _ccc_gates_lib_dir=""
  if [ -n "$_ccc_gates_lib_dir" ] && [ -r "$_ccc_gates_lib_dir/../lib/skill-frontmatter.sh" ]; then
    # shellcheck source=claude/hooks/lib/skill-frontmatter.sh
    . "$_ccc_gates_lib_dir/../lib/skill-frontmatter.sh"
  fi
  unset _ccc_gates_lib_dir
fi

# _ccc_pct_or <value> <default> — integer 0..100 or the default.
_ccc_pct_or() {
  case "${1:-}" in
    ''|*[!0-9]*) printf '%s' "$2" ;;
    *) if [ "$1" -le 100 ]; then printf '%s' "$1"; else printf '%s' "$2"; fi ;;
  esac
}
_ccc_uint_or() {
  case "${1:-}" in
    ''|*[!0-9]*) printf '%s' "$2" ;;
    *) printf '%s' "$1" ;;
  esac
}

DUP_JACCARD_PCT="$(_ccc_pct_or "${CCC_SKILL_DEDUP_JACCARD_PCT:-}" 60)"
DUP_MIN_UNION="$(_ccc_uint_or "${CCC_SKILL_DEDUP_MIN_UNION:-}" 6)"
DUP_PENDING_JACCARD_PCT="$(_ccc_pct_or "${CCC_SKILL_DEDUP_PENDING_JACCARD_PCT:-}" 75)"

norm_name() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -dc 'a-z0-9'; }

# Trigger-first descriptions all open with the same "Use when ..." clause, so
# those words would count as shared tokens in every pair and push unrelated
# skills toward the similarity threshold. Drop them, as the fleet-skills
# near-duplicate advisory does (its SIMILARITY_STOPWORDS covers these).
DESC_TRIGGER_STOPWORDS=' use using when whenever while before after this '
desc_tokens() { # <text> — one lowercase token (len>=3) per line, unique+sorted
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '\n' \
    | awk -v stop="$DESC_TRIGGER_STOPWORDS" 'length($0) >= 3 && index(stop, " " $0 " ") == 0' | sort -u
}

# ccc_skill_dedup_scan <name> <description> <workdir> <jaccard-pct>
#   SKILL.md paths, one per line, on stdin. Writes <workdir>/draft.tok and
#   <workdir>/exist.tok. On the first hit prints "name-similar <existing>" or
#   "description-similar <existing>" and returns 1; returns 0 when nothing in
#   the list resembles the candidate. Output never quotes draft content —
#   names only — so logs and markers stay redaction-safe.
ccc_skill_dedup_scan() {
  local name="$1" desc="$2" work="$3" pct="$4" f ex_name ex_desc inter union
  desc_tokens "$desc" > "$work/draft.tok"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    ex_name="$(ccc_fm_field "$f" name)"
    [ -n "$ex_name" ] || continue
    if [ "$(norm_name "$ex_name")" = "$(norm_name "$name")" ]; then
      printf 'name-similar %s' "$ex_name"; return 1
    fi
    ex_desc="$(ccc_fm_field "$f" description)"
    [ -n "$ex_desc" ] || continue
    desc_tokens "$ex_desc" > "$work/exist.tok"
    inter="$(comm -12 "$work/draft.tok" "$work/exist.tok" 2>/dev/null | wc -l | tr -d '[:space:]')"
    union="$(sort -u "$work/draft.tok" "$work/exist.tok" 2>/dev/null | wc -l | tr -d '[:space:]')"
    if [ "${union:-0}" -ge "$DUP_MIN_UNION" ] \
       && [ $((inter * 100)) -ge $((union * pct)) ]; then
      printf 'description-similar %s' "$ex_name"; return 1
    fi
  done
  return 0
}

# ccc_skill_dedup_installed <name> <description> <workdir> <skills-root>
#   The installed-skills comparison: an existing <skills-root>/<name> directory
#   is a hit on its own ("already-exists <name>"), then every
#   <skills-root>/*/SKILL.md is scanned at DUP_JACCARD_PCT. An empty or missing
#   root passes (nothing to collide with).
ccc_skill_dedup_installed() {
  local name="$1" desc="$2" work="$3" root="$4"
  [ -n "$root" ] || return 0
  if [ -e "$root/$name" ]; then printf 'already-exists %s' "$name"; return 1; fi
  ccc_skill_dedup_scan "$name" "$desc" "$work" "$DUP_JACCARD_PCT" \
    < <(find "$root" -maxdepth 2 -name SKILL.md 2>/dev/null | sort)
}

# ccc_skill_dedup_pending <name> <description> <workdir> <pending-dir>
#   The queue comparison: undecided drafts only (no .approved-/.rejected-/
#   .installed- suffix, no .stage- scratch dirs), at the stricter
#   DUP_PENDING_JACCARD_PCT.
ccc_skill_dedup_pending() {
  local name="$1" desc="$2" work="$3" pending="$4"
  [ -n "$pending" ] && [ -d "$pending" ] || return 0
  ccc_skill_dedup_scan "$name" "$desc" "$work" "$DUP_PENDING_JACCARD_PCT" \
    < <(find "$pending" -mindepth 2 -maxdepth 2 -name SKILL.md 2>/dev/null \
          | grep -Ev '/\.stage-|\.(approved|rejected|installed)-[0-9]+/SKILL\.md$' | sort)
}

# gate_dedup <name> <description> <workdir> — autoinstall.sh's install-time
# gate, unchanged contract: reads the SKILLS_DIR global (routed per draft by
# route_draft_provider), prints "dedup already-exists|name-similar|
# description-similar <existing>" and returns 1 on a hit.
gate_dedup() {
  local verdict
  if verdict="$(ccc_skill_dedup_installed "$1" "$2" "$3" "${SKILLS_DIR:-}")"; then
    return 0
  fi
  printf 'dedup %s' "$verdict"
  return 1
}
