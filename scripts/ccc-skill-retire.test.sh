#!/usr/bin/env bash
# shellcheck disable=SC2034  # fixture variables are read via eval inside ok()
# Hermetic tests for scripts/ccc-skill-retire.py (#2239 L1): a local bare
# repository stands in for jinwon-int/fleet-skills and a `gh` stub on PATH
# records the PR call. Nothing reaches the network.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL="$HERE/ccc-skill-retire.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0
fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

BIN="$TMP/bin"
GH_STATE="$TMP/gh-state"
mkdir -p "$BIN" "$GH_STATE"
chmod 700 "$BIN" "$GH_STATE"
write_exec_stub "$BIN/gh" <<'SH'
set -eu
printf '%s\n' "$*" >> "$GH_TEST_STATE/calls"
case "${1:-} ${2:-}" in
  "api user") printf '12345\ntestuser\n' ;;
  "pr create") printf 'https://github.com/test/fleet-skills/pull/77\n' ;;
  *) exit 9 ;;
esac
SH
export PATH="$BIN:$PATH" GH_TEST_STATE="$GH_STATE"

# --- canon repository fixture: approved/{shared,claude} with approval.json ----
SEED="$TMP/seed"
BARE="$TMP/fleet-skills.git"
mkdir -p "$SEED"
git -C "$SEED" init -q
git -C "$SEED" config user.name fixture
git -C "$SEED" config user.email fixture@example.invalid
add_skill() { # <audience> <name> <approved_at>
  local dir="$SEED/approved/$1/$2"
  mkdir -p "$dir"
  printf -- '---\nname: %s\ndescription: Use when testing %s.\n---\n# %s\n' "$2" "$2" "$2" > "$dir/SKILL.md"
  printf '{"schema_version":1,"source_candidate_id":"%s-abc","source_tree_sha256":"%s","approved_at":"%s"}\n' \
    "$2" "$(printf '%064d' 1)" "$3" > "$dir/approval.json"
}
add_skill shared alpha 2026-01-01T00:00:00Z
add_skill shared beta 2026-02-01T00:00:00Z
add_skill claude gamma 2026-03-01T00:00:00Z
add_skill shared recent "$(date -u -d '10 days ago' +%Y-%m-%dT%H:%M:%SZ)"
mkdir -p "$SEED/retired/shared/old-one"
printf '{"schema_version":1}\n' > "$SEED/retired/shared/old-one/retirement.json"
git -C "$SEED" add -A
git -C "$SEED" commit -qm "seed"
git -C "$SEED" branch -M main
git clone -q --bare "$SEED" "$BARE"
seed_head="$(git -C "$SEED" rev-parse HEAD)"

AGG="$TMP/aggregate.json"
cat > "$AGG" <<'JSON'
{"schema_version":1,"kind":"ccc-skill-usage-aggregate","generated_at":"2026-10-22T00:00:00Z",
 "nodes":{"a":"2026-10-22T00:00:00Z","b":"2026-10-22T00:00:00Z","c":"2026-10-22T00:00:00Z"},
 "thresholds":{"retire_min_nodes":3},
 "retire_candidates":[
   {"name":"gamma","nodes":["a","b","c"],"holder_count":3},
   {"name":"alpha","nodes":["a","b","c"],"holder_count":3},
   {"name":"delta","nodes":["a","b","c"],"holder_count":3},
   {"name":"beta","nodes":["a","b","c"],"holder_count":4},
   {"name":"recent","nodes":["a","b","c"],"holder_count":3},
   {"name":"old-one","nodes":["a","b","c"],"holder_count":3}
 ]}
JSON
agg_sha="$(sha256sum "$AGG" | awk '{print $1}')"

run_tool() { env CCC_SKILL_RETIRE_REPO=test/fleet-skills CCC_SKILL_RETIRE_REMOTE="$BARE" python3 -I "$TOOL" "$@"; }

# --- plan ---------------------------------------------------------------------
out="$(run_tool plan --aggregate "$AGG")"; rc=$?
ok "plan succeeds read-only" '[ "$rc" = 0 ] && jq -e ".ok and .mode == \"plan\" and .aggregate_sha256 == \"$agg_sha\"" >/dev/null <<<"$out"'
ok "plan selects approved, old-enough candidates sorted by name" \
  'jq -e "[.selected[].name] == [\"alpha\",\"beta\",\"gamma\"]" >/dev/null <<<"$out"'
ok "plan resolves audience and approved_at age from approval.json" \
  'jq -e ".selected[0].audience == \"shared\" and .selected[0].approved_at == \"2026-01-01T00:00:00Z\" and .selected[0].idle_days >= 290 and .selected[2].audience == \"claude\" and .selected[1].holders == 4" >/dev/null <<<"$out"'
ok "plan skips not_in_approved, approved_too_recent and already_retired" \
  'jq -e "([.skipped[] | select(.name == \"delta\") | .reason] == [\"not_in_approved\"]) and ([.skipped[] | select(.name == \"recent\") | .reason] == [\"approved_too_recent\"]) and ([.skipped[] | select(.name == \"old-one\") | .reason] == [\"already_retired\"])" >/dev/null <<<"$out"'
ok "plan output carries no SKILL.md body or description" '! grep -q "Use when testing" <<<"$out"'
out="$(run_tool plan --aggregate "$AGG" --max 2)"
ok "--max caps the selection and reports over_max" \
  'jq -e "[.selected[].name] == [\"alpha\",\"beta\"] and ([.skipped[] | select(.reason == \"over_max\") | .name] == [\"gamma\"])" >/dev/null <<<"$out"'
out="$(run_tool plan --aggregate "$AGG" --exclude beta)"
ok "--exclude leaves a name out" \
  'jq -e "[.selected[].name] == [\"alpha\",\"gamma\"] and ([.skipped[] | select(.name == \"beta\") | .reason] == [\"excluded\"])" >/dev/null <<<"$out"'
out="$(run_tool plan --aggregate "$AGG" --min-idle-days 5)"
ok "--min-idle-days admits the recent approval" 'jq -e "[.selected[].name] | index(\"recent\") != null" >/dev/null <<<"$out"'
CLONE="$TMP/clone"
git clone -q "$BARE" "$CLONE"
out="$(run_tool plan --aggregate "$AGG" --fleet-skills-dir "$CLONE")"; rc=$?
ok "--fleet-skills-dir plans from an existing clone" '[ "$rc" = 0 ] && jq -e "[.selected[].name] == [\"alpha\",\"beta\",\"gamma\"]" >/dev/null <<<"$out"'
ok "plan never touches gh" '[ ! -e "$GH_STATE/calls" ]'

# --- pr dry run ---------------------------------------------------------------
out="$(run_tool pr --aggregate "$AGG")"; rc=$?
ok "pr without --yes is a dry run" '[ "$rc" = 0 ] && jq -e ".ok and .mode == \"pr-dry-run\" and ([.selected[].name] == [\"alpha\",\"beta\",\"gamma\"])" >/dev/null <<<"$out"'
ok "dry run pushes nothing and calls no gh" \
  '[ "$(git -C "$BARE" branch --list "retire/*" | wc -l)" = 0 ] && [ ! -e "$GH_STATE/calls" ]'
out="$(run_tool pr --aggregate "$AGG" --yes --fleet-skills-dir "$CLONE")"; rc=$?
ok "pr --yes refuses to mutate an operator clone" '[ "$rc" = 2 ] && jq -e ".code == \"pr_requires_fresh_clone\"" >/dev/null <<<"$out"'

# --- pr --yes -----------------------------------------------------------------
out="$(run_tool pr --aggregate "$AGG" --yes --max 2)"; rc=$?
ok "pr --yes opens the PR" '[ "$rc" = 0 ] && jq -e ".ok and .mode == \"pr\" and .pr_url == \"https://github.com/test/fleet-skills/pull/77\"" >/dev/null <<<"$out"'
branch="$(jq -r .branch <<<"$out")"
head="$(jq -r .head <<<"$out")"
ok "branch name is retire/auto-<date>-<8hex of aggregate sha>" \
  '[ "$branch" = "retire/auto-$(date -u +%Y%m%d)-${agg_sha:0:8}" ]'
ok "branch was pushed to the remote at the reported head" \
  '[ "$(git -C "$BARE" rev-parse "refs/heads/$branch")" = "$head" ]'
ok "gh pr create was called once with the branch and base" \
  '[ "$(grep -c "^pr create" "$GH_STATE/calls")" = 1 ] && grep -q -- "--head $branch" "$GH_STATE/calls" && grep -q -- "--base main" "$GH_STATE/calls" && grep -q -- "--repo test/fleet-skills" "$GH_STATE/calls"'
ok "gh never approved, merged or enqueued" '! grep -qE "pr (merge|review)|graphql" "$GH_STATE/calls"'
tree="$(git -C "$BARE" ls-tree -r --name-only "$branch")"
ok "selected skills moved approved -> retired with history" \
  'grep -q "^retired/shared/alpha/SKILL.md$" <<<"$tree" && grep -q "^retired/shared/alpha/approval.json$" <<<"$tree" && grep -q "^retired/shared/beta/SKILL.md$" <<<"$tree" && ! grep -q "^approved/shared/alpha/" <<<"$tree" && ! grep -q "^approved/shared/beta/" <<<"$tree"'
ok "over_max skill stays in approved" 'grep -q "^approved/claude/gamma/SKILL.md$" <<<"$tree" && ! grep -q "^retired/claude/gamma/" <<<"$tree"'
rec="$(git -C "$BARE" show "$branch:retired/shared/alpha/retirement.json")"
ok "retirement.json matches the lane B schema" \
  'jq -e ".schema_version == 1 and .reason == \"unused\" and .source_commit == \"$seed_head\" and (.retired_at | test(\"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$\")) and .evidence.report_generated_at == \"2026-10-22T00:00:00Z\" and .evidence.nodes_measured == 3 and (.evidence.min_idle_days | type) == \"number\" and .evidence.min_idle_days >= 290 and .evidence.aggregate_sha256 == \"$agg_sha\"" >/dev/null <<<"$rec"'
ok "retirement.json carries nothing but the contract keys" \
  'jq -e "keys == [\"evidence\",\"reason\",\"retired_at\",\"schema_version\",\"source_commit\"] and (.evidence | keys) == [\"aggregate_sha256\",\"min_idle_days\",\"nodes_measured\",\"report_generated_at\"]" >/dev/null <<<"$rec"'
msg="$(git -C "$BARE" log -1 --format=%B "$branch")"
ok "commit is authored as the gh account with the trailer" \
  '[ "$(git -C "$BARE" log -1 --format=%an "$branch")" = testuser ] && grep -q "^Co-Authored-By: Claude Fable 5.1" <<<"$msg" && grep -q "^retire: 2 unused fleet skills" <<<"$msg"'
ok "delta is reported as not_in_approved in the pr result" \
  'jq -e "[.skipped[] | select(.name == \"delta\") | .reason] == [\"not_in_approved\"]" >/dev/null <<<"$out"'

# --- fail-closed inputs -------------------------------------------------------
printf '{"kind":"other"}\n' > "$TMP/bad.json"
out="$(run_tool plan --aggregate "$TMP/bad.json")"; rc=$?
ok "a foreign aggregate fails closed" '[ "$rc" = 2 ] && jq -e ".code == \"aggregate_invalid\"" >/dev/null <<<"$out"'
out="$(run_tool plan --aggregate "$TMP/missing.json")"; rc=$?
ok "a missing aggregate fails closed" '[ "$rc" = 2 ] && jq -e ".code == \"aggregate_unreadable\"" >/dev/null <<<"$out"'
: > "$TMP/empty-list.json"
jq '.retire_candidates = []' "$AGG" > "$TMP/empty-list.json"
out="$(run_tool pr --aggregate "$TMP/empty-list.json" --yes)"; rc=$?
ok "pr --yes with nothing selected opens no PR" '[ "$rc" = 0 ] && jq -e ".ok and .code == \"nothing_to_retire\" and .selected == []" >/dev/null <<<"$out" && [ "$(grep -c "^pr create" "$GH_STATE/calls")" = 1 ]'

echo "ccc-skill-retire: pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
