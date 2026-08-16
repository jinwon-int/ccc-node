#!/usr/bin/env bash
# Tests for skill-review/gate-sim.sh — hermetic, no network, no installs.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SIM="$HERE/gate-sim.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

STATE="$TMP/state"
SKILLS="$TMP/skills"
PENDING="$STATE/pending-skills"
mkdir -p "$STATE" "$SKILLS" "$PENDING"
chmod 700 "$STATE" "$SKILLS"

run_sim() {
  CCC_STATE_DIR="$STATE" CLAUDE_SKILLS_DIR="$SKILLS" \
  CCC_SKILL_REVIEW_PENDING_DIR="$PENDING" CCC_NODE=testnode \
  bash "$SIM" "$@"
}

# gate_lint requires >= BODY_MIN_LINES (5) non-empty body lines and a
# description >= DESC_MIN (20) chars, so fixtures must clear both or every
# case degenerates into "lint body-too-short".
mkdraft() { # <dirname> <distinguishing-body-line>
  local d="$PENDING/$1"
  mkdir -p "$d"
  {
    printf -- '---\nname: %s\n' "${1%%.*}"
    printf 'description: a deterministic fixture draft used by the gate-sim tests\n'
    printf -- '---\n\n## Steps\n\n'
    printf -- '%s\n' "$2"
    printf -- '1. Inspect the current state before changing anything.\n'
    printf -- '2. Record what was observed.\n'
    printf -- '3. Apply the smallest reversible change.\n'
    printf -- '4. Verify the result and report it.\n'
  } > "$d/SKILL.md"
  printf '{"name":"%s","status":"pending"}\n' "${1%%.*}" > "$d/meta.json"
}

# 1. A clean draft passes.
mkdraft "clean-draft" "Run \`git status\` and report the result."
out="$(run_sim 2>&1)"
ok "clean draft reports PASS" '[[ "$out" == *"clean-draft"*"PASS"* ]]'

# 2. A node-specific path is blocked.
mkdraft "rooty-draft" "Store output in /root/.claude/backups/ on this host."
out="$(run_sim 2>&1)"
ok "node-specific draft reports BLOCK" \
  '[[ "$(grep rooty-draft <<<"$out")" == *BLOCK* ]]'
ok "clean draft still PASS alongside a blocked one" \
  '[[ "$(grep clean-draft <<<"$out")" == *PASS* ]]'

# 3. Nothing is installed or mutated. This is the whole point of the tool:
#    it must be safe to run against a live queue in approve mode.
# shellcheck disable=SC2034  # used inside ok()'s eval
before="$(find "$SKILLS" "$PENDING" | sort | md5sum)"
run_sim >/dev/null 2>&1
# shellcheck disable=SC2034  # used inside ok()'s eval
after="$(find "$SKILLS" "$PENDING" | sort | md5sum)"
ok "no files created or removed" '[ "$before" = "$after" ]'
ok "skills root stays empty" '[ -z "$(ls -A "$SKILLS")" ]'

# 4. The human verdict comes from the directory suffix, NOT meta.json. Batch
#    reviews have moved directories without rewriting meta.json, so trusting
#    the status field misreports rejected drafts as pending.
mv "$PENDING/clean-draft" "$PENDING/clean-draft.rejected-20260101000000"
out="$(run_sim 2>&1)"
ok "archived draft reports the suffix verdict, not meta.json status" \
  '[[ "$(grep clean-draft <<<"$out")" == *rejected* ]]'
ok "meta.json status is not echoed for an archived draft" \
  '[[ "$(grep clean-draft <<<"$out")" != *pending* ]]'

# 5. JSON mode is valid JSON with the expected shape.
if command -v jq >/dev/null 2>&1; then
  out="$(run_sim --json 2>/dev/null)"
  ok "--json emits parseable JSON" 'jq -e . >/dev/null 2>&1 <<<"$out"'
  ok "--json carries a gate field per row" \
    'jq -e "all(.[]; .gate == \"PASS\" or .gate == \"BLOCK\")" >/dev/null 2>&1 <<<"$out"'
fi

# 6. Explicit path arguments are honoured and --json is not mistaken for one.
# shellcheck disable=SC2034  # used inside ok()'s eval
out="$(run_sim "$PENDING/rooty-draft/SKILL.md" 2>&1)"
ok "explicit path argument replays just that draft" \
  '[ "$(grep -c BLOCK <<<"$out")" -ge 1 ] && [[ "$out" != *clean-draft* ]]'

# 7. A lint failure is surfaced as a BLOCK like any other gate. Pinned because
#    a too-thin draft is the most common real rejection and it must not be
#    silently reported as PASS.
thin="$PENDING/thin-draft"
mkdir -p "$thin"
printf -- '---\nname: thin-draft\ndescription: a deterministic fixture draft used by the gate-sim tests\n---\n\nonly one line\n' \
  > "$thin/SKILL.md"
# shellcheck disable=SC2034  # used inside ok()'s eval
out="$(run_sim "$thin/SKILL.md" 2>&1)"
ok "a too-short body is reported as a lint BLOCK" \
  '[[ "$out" == *BLOCK* && "$out" == *body-too-short* ]]'

# 8. Sourcing autoinstall.sh must not dispatch this script's arguments to it.
#    If the verb were not pinned, `--json` would reach autoinstall's dispatcher.
ok "--json does not leak into autoinstall dispatch" \
  '[[ "$(run_sim --json 2>&1)" != *"incremental_action_invalid"* ]]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
