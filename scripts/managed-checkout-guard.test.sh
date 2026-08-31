#!/usr/bin/env bash
# Tests for scripts/git-hooks/managed-checkout-guard — hermetic fixture git repos.
# The hook is warn-only; every case must exit 0. What varies is whether it speaks.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/git-hooks/managed-checkout-guard"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

CLAUDE_FIX="$TMP/claude"
mkdir -p "$CLAUDE_FIX"

# Fixture: a normal checkout with one commit, mirroring a managed node checkout.
M="$TMP/managed"
git init -q -b main "$M"
git -C "$M" commit -q --allow-empty -m base

# OUT/RC are read inside the eval'd ok() assertion strings, which shellcheck
# cannot see — silence its write-only-variable heuristic here.
# shellcheck disable=SC2034
RC=0; OUT=""
run_hook() { # <post-checkout args...> — runs the hook the way git would
  # (cwd = worktree root) and captures its output/exit into OUT/RC.
  # shellcheck disable=SC2034
  # shellcheck disable=SC2034
  OUT="$(cd "$M" && CCC_CLAUDE_DIR="$CLAUDE_FIX" bash "$HOOK" "$@" 2>&1)"
  # shellcheck disable=SC2034
  RC=$?
}
run_hook_in() { # <dir> <env-assignments...> -- <post-checkout args...>
  local dir="$1"; shift
  local envs=()
  while [ "${1:-}" != "--" ]; do envs+=("$1"); shift; done
  shift
  # shellcheck disable=SC2034
  # shellcheck disable=SC2034
  OUT="$(cd "$dir" && env ${envs[@]+"${envs[@]}"} CCC_CLAUDE_DIR="$CLAUDE_FIX" bash "$HOOK" "$@" 2>&1)"
  # shellcheck disable=SC2034
  RC=$?
}

HEAD_SHA="$(git -C "$M" rev-parse HEAD)"

# --- 1) stays on the expected branch: silent ---------------------------------
run_hook "$HEAD_SHA" "$HEAD_SHA" 1
ok "post-checkout on main exits 0 silently" '[ "$RC" = 0 ] && [ -z "$OUT" ]'

# --- 2) branch checkout landing off main: loud warning, never a block ----------
git -C "$M" checkout -q -b feature/x
run_hook "$HEAD_SHA" "$HEAD_SHA" 1
ok "off-main checkout warns but exits 0 (warn-only)" '[ "$RC" = 0 ] && [ -n "$OUT" ]'
ok "warning names the expected branch" 'grep -q "main" <<<"$OUT"'
ok "warning teaches the worktree discipline" 'grep -q "git worktree add" <<<"$OUT"'
ok "warning is marked as the managed checkout" 'grep -q "MANAGED CHECKOUT" <<<"$OUT"'
git -C "$M" checkout -q main

# --- 3) detached HEAD in the managed checkout warns ---------------------------
git -C "$M" checkout -q --detach "$HEAD_SHA"
run_hook "$HEAD_SHA" "$HEAD_SHA" 1
ok "detached HEAD warns" '[ "$RC" = 0 ] && grep -q "detached HEAD" <<<"$OUT"'
git -C "$M" checkout -q main

# --- 4) file checkout (flag=0) never warns ------------------------------------
run_hook "$HEAD_SHA" "$HEAD_SHA" 0
ok "file checkout (flag=0) is silent" '[ "$RC" = 0 ] && [ -z "$OUT" ]'

# --- 5) linked worktrees are exempt (the sanctioned dev path) -----------------
git -C "$M" worktree add -q -b wtdev "$TMP/wt" 2>/dev/null
git -C "$TMP/wt" checkout -q -b wtdev2
WT_SHA="$(git -C "$TMP/wt" rev-parse HEAD)"
run_hook_in "$TMP/wt" -- "$HEAD_SHA" "$WT_SHA" 1
ok "linked worktree checkout is silent" '[ "$RC" = 0 ] && [ -z "$OUT" ]'
git -C "$M" worktree remove --force "$TMP/wt"

# --- 6) kill switch: CCC_MANAGED_CHECKOUT_GUARD=0 silences everything ---------
git -C "$M" checkout -q -b muted
run_hook_in "$M" CCC_MANAGED_CHECKOUT_GUARD=0 -- "$HEAD_SHA" "$HEAD_SHA" 1
ok "CCC_MANAGED_CHECKOUT_GUARD=0 silences the warning" '[ "$RC" = 0 ] && [ -z "$OUT" ]'
git -C "$M" checkout -q main && git -C "$M" branch -qD muted

# --- 7) self-update.repo naming a DIFFERENT repo: silent everywhere -----------
printf '%s\n' "$TMP/other-repo" > "$CLAUDE_FIX/self-update.repo"
git -C "$M" checkout -q -b elsewhere
run_hook "$HEAD_SHA" "$HEAD_SHA" 1
ok "non-managed repo (per self-update.repo) is silent" '[ "$RC" = 0 ] && [ -z "$OUT" ]'
git -C "$M" checkout -q main && git -C "$M" branch -qD elsewhere

# --- 8) self-update.repo naming THIS repo: still warns -------------------------
printf '%s\n' "$M" > "$CLAUDE_FIX/self-update.repo"
git -C "$M" checkout -q -b pointed-here
run_hook "$HEAD_SHA" "$HEAD_SHA" 1
ok "managed repo (per self-update.repo) still warns" '[ "$RC" = 0 ] && [ -n "$OUT" ]'
git -C "$M" checkout -q main && git -C "$M" branch -qD pointed-here
rm -f "$CLAUDE_FIX/self-update.repo"

# --- 9) custom expected branch via CCC_SELF_UPDATE_BRANCH ---------------------
run_hook_in "$M" CCC_SELF_UPDATE_BRANCH=main -- "$HEAD_SHA" "$HEAD_SHA" 1
ok "explicit expected=main stays silent on main" '[ "$RC" = 0 ] && [ -z "$OUT" ]'
git -C "$M" checkout -q -b trunk
git -C "$M" checkout -q main
run_hook_in "$M" CCC_SELF_UPDATE_BRANCH=trunk -- "$HEAD_SHA" "$HEAD_SHA" 1
ok "expected=trunk warns while sitting on main" '[ "$RC" = 0 ] && [ -n "$OUT" ]'

# --- 10) missing git context: fail-open, silent --------------------------------
run_hook_in "$TMP" -- "$HEAD_SHA" "$HEAD_SHA" 1
ok "hook outside a git repo exits 0 silently" '[ "$RC" = 0 ] && [ -z "$OUT" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
