#!/usr/bin/env bash
# Tests for claude/hooks/post-edit-lint.sh — PostToolUse post-edit Python
# diagnostics (#2261). Hermetic: private HOME/CCC_STATE_DIR, a stub ruff on a
# private PATH, a throwaway git repo for the scope gate. The fail-open contract
# (always exit 0, silent when clean) is asserted directly.
# shellcheck disable=SC2034  # rc/out/ctx are read via eval inside ok()
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/post-edit-lint.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/lib/test-stub.sh"
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

export HOME="$TMP/home"
export CCC_STATE_DIR="$TMP/state"
mkdir -p "$HOME" "$TMP/bin"
unset CCC_POST_EDIT_LINT CCC_POST_EDIT_LINT_SCOPE CCC_POST_EDIT_LINT_RUFF CCC_POST_EDIT_LINT_MAX_BYTES

REPO="$TMP/repo"
mkdir -p "$REPO/pkg"
git -C "$REPO" init -q 2>/dev/null || { echo "git init unavailable"; exit 1; }
printf 'import os\n\nx = 1\n' > "$REPO/pkg/unused.py"
printf 'def f(:\n    pass\n' > "$REPO/pkg/broken.py"
printf 'x = 1\n' > "$REPO/pkg/clean.py"
mkdir -p "$TMP/outside"
printf 'def g(:\n' > "$TMP/outside/loose.py"

# Stub ruff: emits one concise finding for unused.py, nothing for others.
write_exec_stub "$TMP/bin/ruff" <<'SH'
file="${@: -1}"
case "$file" in
  *unused.py) printf '%s:1:8: F401 `os` imported but unused\nFound 1 error.\n' "$file"; exit 1 ;;
  *) exit 0 ;;
esac
SH

run_hook() { # run_hook <json> [env...]
  printf '%s' "$1" | bash "$HOOK"
}
payload() { # payload <tool> <path>
  jq -nc --arg t "$1" --arg p "$2" '{tool_name:$t, session_id:"s1", tool_input:{file_path:$p}}'
}

# --- ruff path -------------------------------------------------------------
rc=0; out="$(CCC_POST_EDIT_LINT_RUFF="$TMP/bin/ruff" run_hook "$(payload Edit "$REPO/pkg/unused.py")")" || rc=$?
ok "ruff finding is returned as PostToolUse additionalContext, exit 0" \
  '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.hookEventName == \"PostToolUse\" and (.hookSpecificOutput.additionalContext | contains(\"F401\"))" <<<"$out" >/dev/null'
ok "context names the mode, the count and the repo-relative path" \
  'jq -r ".hookSpecificOutput.additionalContext" <<<"$out" | head -1 | grep -q "post-edit-lint (ruff): 1 finding(s) in pkg/unused.py"'
ok "the ruff summary line (Found N errors.) is not forwarded" \
  '! jq -r ".hookSpecificOutput.additionalContext" <<<"$out" | grep -q "Found 1 error"'

rc=0; out="$(CCC_POST_EDIT_LINT_RUFF="$TMP/bin/ruff" run_hook "$(payload Write "$REPO/pkg/clean.py")")" || rc=$?
ok "a clean file produces no output and exit 0" '[ "$rc" = 0 ] && [ -z "$out" ]'

ok "measurement log is body-free: counts only, no path, no finding text" \
  '[ "$(wc -l < "$CCC_STATE_DIR/post-edit-lint.log")" = 2 ] && grep -q "tool=Edit mode=ruff findings=1" "$CCC_STATE_DIR/post-edit-lint.log" && grep -q "tool=Write mode=ruff findings=0" "$CCC_STATE_DIR/post-edit-lint.log" && ! grep -q "unused.py\|F401" "$CCC_STATE_DIR/post-edit-lint.log"'

# --- py_compile fallback -----------------------------------------------------
rc=0; out="$(PATH="$TMP/nobin:/usr/bin:/bin" CCC_POST_EDIT_LINT_RUFF="" run_hook "$(payload Edit "$REPO/pkg/broken.py")")" || rc=$?
ok "without ruff a syntax error is reported via py_compile" \
  '[ "$rc" = 0 ] && jq -r ".hookSpecificOutput.additionalContext" <<<"$out" | grep -q "post-edit-lint (py_compile): "'
rc=0; out="$(PATH="$TMP/nobin:/usr/bin:/bin" CCC_POST_EDIT_LINT_RUFF="" run_hook "$(payload Edit "$REPO/pkg/clean.py")")" || rc=$?
ok "without ruff a syntactically valid file stays silent" '[ "$rc" = 0 ] && [ -z "$out" ]'

# --- scope, filters, switches ---------------------------------------------------
rc=0; out="$(CCC_POST_EDIT_LINT_RUFF="$TMP/bin/ruff" run_hook "$(payload Edit "$TMP/outside/loose.py")")" || rc=$?
ok "default scope=repo ignores a .py outside any git work tree" '[ "$rc" = 0 ] && [ -z "$out" ]'
rc=0; out="$(CCC_POST_EDIT_LINT_SCOPE=all PATH="$TMP/nobin:/usr/bin:/bin" CCC_POST_EDIT_LINT_RUFF="" run_hook "$(payload Edit "$TMP/outside/loose.py")")" || rc=$?
ok "scope=all lints a .py outside a repo" '[ "$rc" = 0 ] && [ -n "$out" ]'

rc=0; out="$(CCC_POST_EDIT_LINT_RUFF="$TMP/bin/ruff" run_hook "$(payload Edit "$REPO/pkg/notes.md")")" || rc=$?
ok "non-.py files are ignored" '[ "$rc" = 0 ] && [ -z "$out" ]'
rc=0; out="$(CCC_POST_EDIT_LINT_RUFF="$TMP/bin/ruff" run_hook "$(payload Bash "$REPO/pkg/unused.py")")" || rc=$?
ok "tools outside Edit|Write|MultiEdit are ignored" '[ "$rc" = 0 ] && [ -z "$out" ]'
rc=0; out="$(CCC_POST_EDIT_LINT_RUFF="$TMP/bin/ruff" run_hook "$(payload Edit "$REPO/pkg/missing.py")")" || rc=$?
ok "a file that does not exist yet is ignored" '[ "$rc" = 0 ] && [ -z "$out" ]'
rc=0; out="$(CCC_POST_EDIT_LINT=0 CCC_POST_EDIT_LINT_RUFF="$TMP/bin/ruff" run_hook "$(payload Edit "$REPO/pkg/unused.py")")" || rc=$?
ok "CCC_POST_EDIT_LINT=0 disables the hook" '[ "$rc" = 0 ] && [ -z "$out" ]'
rc=0; out="$(printf 'not json' | bash "$HOOK")" || rc=$?
ok "malformed stdin is fail-open" '[ "$rc" = 0 ] && [ -z "$out" ]'

# --- output bound ------------------------------------------------------------------
write_exec_stub "$TMP/bin/ruff-many" <<'SH'
file="${@: -1}"
for i in $(seq 1 200); do printf '%s:%s:1: F841 local variable `v%s` is assigned to but never used\n' "$file" "$i" "$i"; done
printf 'Found 200 errors.\n'; exit 1
SH
rc=0; out="$(CCC_POST_EDIT_LINT_MAX_BYTES=600 CCC_POST_EDIT_LINT_RUFF="$TMP/bin/ruff-many" run_hook "$(payload Edit "$REPO/pkg/clean.py")")" || rc=$?
ctx="$(jq -r ".hookSpecificOutput.additionalContext" <<<"$out")"
ok "oversized findings are cut at the byte cap but the total count is kept" \
  '[ "$rc" = 0 ] && [ "${#ctx}" -lt 1000 ] && grep -q "200 finding(s)" <<<"$ctx" && grep -q "truncated; 200 findings total" <<<"$ctx"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
