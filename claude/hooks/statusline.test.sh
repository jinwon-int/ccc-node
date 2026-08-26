#!/usr/bin/env bash
# shellcheck disable=SC2034  # out/rc/elapsed are read inside eval'd assertions
# Tests for statusline.sh — render fields, git TTL cache, detached collector.
# Usage: bash statusline.test.sh   (exit 0 = all pass)
#
# Hermetic: HOME points at a temp dir so runs never touch the node's real
# ~/.claude. git is shimmed through PATH to count invocations, proving the
# cache-hit path (including detached HEAD / non-repo cwds) stays git-free.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/statusline.sh"
pass=0; fail=0

TDIR="$(mktemp -d 2>/dev/null || mktemp -d -t ccc-statusline-test)"
trap 'rm -rf "$TDIR" 2>/dev/null || true' EXIT
export HOME="$TDIR/home"
mkdir -p "$HOME"

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# git shim: count invocations, then delegate to the real git.
REAL_GIT="$(command -v git)"
GIT_LOG="$TDIR/git-calls"
mkdir -p "$TDIR/bin"
printf '#!/bin/sh\necho x >> "%s"\nexec "%s" "$@"\n' "$GIT_LOG" "$REAL_GIT" > "$TDIR/bin/git"
chmod +x "$TDIR/bin/git"
git_calls() { wc -l < "$GIT_LOG" 2>/dev/null | tr -d '[:space:]' || printf 0; }

payload() { # <cwd> -> session JSON on stdout
  jq -nc --arg d "$1" '{model:{display_name:"TModel"},
    context_window:{used_percentage:37}, cost:{total_cost_usd:2.5},
    workspace:{current_dir:$d}}'
}
render() { # <cwd> -> $out
  out="$(payload "$1" | PATH="$TDIR/bin:$PATH" bash "$HOOK" 2>/dev/null)"
}

# Repo fixture with one commit so branches/detach behave normally.
REPO="$TDIR/repo"
mkdir -p "$REPO"
( cd "$REPO" && "$REAL_GIT" init -q && "$REAL_GIT" -c user.email=t@t -c user.name=t \
    commit -q --allow-empty -m init && "$REAL_GIT" checkout -qb feat/x ) 2>/dev/null

# 1) Cold render: fields present, branch shown, cache file written
: > "$GIT_LOG"
render "$REPO"
ok "render carries model, ctx percent, cost" \
  'grep -q "TModel" <<<"$out" && grep -q "37% ctx" <<<"$out" && grep -q "2.50" <<<"$out"'
ok "render shows the git branch" 'grep -q "feat/x" <<<"$out"'
ok "cold render consulted git" '[ "$(git_calls)" -gt 0 ]'
ok "cache file exists" 'ls "$HOME"/.claude/cache/git-status/*.tsv >/dev/null 2>&1'

# 2) Warm render inside the TTL: no git forks, same branch from cache
: > "$GIT_LOG"
render "$REPO"
ok "warm render still shows the branch" 'grep -q "feat/x" <<<"$out"'
ok "warm render runs zero git commands" '[ "$(git_calls)" = "0" ]'

# 3) Dirty state is cached too
( cd "$REPO" && touch dirty-file )
find "$HOME/.claude/cache/git-status" -type f -delete 2>/dev/null
render "$REPO"
ok "dirty marker rendered" 'grep -q "feat/x\*" <<<"$out"'
: > "$GIT_LOG"
render "$REPO"
ok "dirty marker served from cache without git" \
  'grep -q "feat/x\*" <<<"$out" && [ "$(git_calls)" = "0" ]'

# 4) Detached HEAD: warm render must also hit the cache (empty branch is a
#    legitimate cached value, not a miss).
( cd "$REPO" && "$REAL_GIT" checkout -q --detach && rm -f dirty-file )
find "$HOME/.claude/cache/git-status" -type f -delete 2>/dev/null
render "$REPO"
: > "$GIT_LOG"
render "$REPO"
ok "detached HEAD warm render runs zero git commands" '[ "$(git_calls)" = "0" ]'

# 5) Non-repo cwd: cached negative result, no git on the warm render
NONREPO="$TDIR/plain"; mkdir -p "$NONREPO"
render "$NONREPO"
: > "$GIT_LOG"
render "$NONREPO"
ok "non-repo warm render runs zero git commands" '[ "$(git_calls)" = "0" ]'

# 6) Stale cache recomputes: age the timestamp beyond the TTL
cache_file="$(ls "$HOME"/.claude/cache/git-status/*.tsv | head -1)"
IFS=$'\t' read -r ts rest < "$cache_file"
printf '%s\t%s\n' "$((ts - 3600))" "$rest" > "$cache_file"
: > "$GIT_LOG"
render "$NONREPO"
ok "stale cache falls back to git" '[ "$(git_calls)" -gt 0 ]'

# 7) The usage collector must never block rendering: a 3s-sleep collector
#    still lets the render finish quickly.
SLOW="$TDIR/slow-collector.py"
printf '#!/usr/bin/env python3\nimport time\ntime.sleep(3)\n' > "$SLOW"
chmod +x "$SLOW"
start="${EPOCHSECONDS:-$(date +%s)}"
out="$(payload "$NONREPO" | CCC_STATUSLINE_USAGE_COLLECTOR="$SLOW" bash "$HOOK" 2>/dev/null)"
elapsed=$(( ${EPOCHSECONDS:-$(date +%s)} - start ))
ok "slow collector does not delay the render" '[ -n "$out" ] && [ "$elapsed" -le 2 ]'

# 8) Robustness: empty and non-JSON stdin still render a line with defaults
out="$(printf '' | bash "$HOOK" 2>/dev/null)"; rc=$?
ok "empty stdin renders defaults" '[ "$rc" = 0 ] && grep -q "0% ctx" <<<"$out"'
out="$(printf 'not json' | bash "$HOOK" 2>/dev/null)"; rc=$?
ok "non-JSON stdin renders defaults" '[ "$rc" = 0 ] && grep -q "0% ctx" <<<"$out"'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = "0" ]
