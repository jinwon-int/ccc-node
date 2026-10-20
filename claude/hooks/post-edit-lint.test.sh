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

# --- repo ruff config and edited-line scope (#2265) -----------------------------
# Behaviour stub: F401 for every `import X` line, `invalid-syntax` at line 1 when
# the file holds `def f(:`, E501 for lines over 88 chars ONLY when `--select` is
# on the command line — modelling real ruff, where a CLI `--select E` re-enables
# a configured `ignore = ["E501"]`. argv is recorded so the flags can be checked.
export CCC_TEST_RUFF_ARGS="$TMP/ruff.args"
write_exec_stub "$TMP/bin/ruff-model" <<'SH'
printf '%s\n' "$*" >> "$CCC_TEST_RUFF_ARGS"
file="${@: -1}"; sel=0
for a in "$@"; do [ "$a" = "--select" ] && sel=1; done
awk -v f="$file" -v sel="$sel" '
  /^import [a-z]+$/ { printf "%s:%d:8: F401 [*] `%s` imported but unused\n", f, NR, $2; n++ }
  /def f\(:/ { syn = 1 }
  sel == 1 && length($0) > 88 { printf "%s:%d:89: E501 Line too long (%d > 88)\n", f, NR, length($0); n++ }
  END { if (syn) { printf "%s:1:7: invalid-syntax: Expected a parameter\n", f; n++ }
        if (n) { printf "Found %d errors.\n", n; exit 1 } }
' "$file"
SH
gitc() { git -C "$1" -c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false "${@:2}" >/dev/null 2>&1; }
LONG="x = \"$(printf '%0100d' 0 | tr 0 a)\""
lint() { # lint <path> [env...] — run the hook with the behaviour stub
  : > "$CCC_TEST_RUFF_ARGS"
  CCC_POST_EDIT_LINT_RUFF="$TMP/bin/ruff-model" run_hook "$(payload Edit "$1")"
}
ctx_of() { jq -r ".hookSpecificOutput.additionalContext" <<<"$1"; }

CFG="$TMP/cfg"; mkdir -p "$CFG/pkg"; git -C "$CFG" init -q
printf '[project]\nname = "x"\n\n[tool.ruff.lint]\nselect = ["E", "F"]\nignore = ["E501"]\n' > "$CFG/pyproject.toml"
printf '%s\n' "$LONG" > "$CFG/pkg/long.py"
rc=0; out="$(lint "$CFG/pkg/long.py")" || rc=$?
ok "pyproject [tool.ruff] repo: no --select is passed (repo config decides)" \
  '[ "$rc" = 0 ] && grep -q -- "--no-fix" "$CCC_TEST_RUFF_ARGS" && ! grep -q -- "--select" "$CCC_TEST_RUFF_ARGS"'
ok "pyproject [tool.ruff] ignore E501: a long line is not reported" '[ -z "$out" ]'
ok "log records the rule source" 'tail -1 "$CCC_STATE_DIR/post-edit-lint.log" | grep -q "mode=ruff findings=0 rules=repo preexisting=0"'

NOCFG="$TMP/nocfg"; mkdir -p "$NOCFG/pkg"; git -C "$NOCFG" init -q
printf '[project]\nname = "x"\n' > "$NOCFG/pyproject.toml"   # pyproject without [tool.ruff]
printf '%s\n' "$LONG" > "$NOCFG/pkg/long.py"
rc=0; out="$(lint "$NOCFG/pkg/long.py")" || rc=$?
ok "repo without ruff config (pyproject lacks [tool.ruff]): --select E,F is passed" \
  '[ "$rc" = 0 ] && grep -q -- "--select E,F" "$CCC_TEST_RUFF_ARGS"'
ok "repo without ruff config: E501 is reported" 'ctx_of "$out" | grep -q "E501 Line too long"'

for cfgname in ruff.toml .ruff.toml; do
  RT="$TMP/rt-$cfgname"; mkdir -p "$RT/pkg"; git -C "$RT" init -q
  printf '[lint]\nignore = ["E501"]\n' > "$RT/$cfgname"
  printf '%s\n' "$LONG" > "$RT/pkg/long.py"
  rc=0; out="$(lint "$RT/pkg/long.py")" || rc=$?
  ok "$cfgname at the repo root: no --select, long line not reported" \
    '[ "$rc" = 0 ] && [ -z "$out" ] && ! grep -q -- "--select" "$CCC_TEST_RUFF_ARGS"'
done
NEST="$TMP/nest"; mkdir -p "$NEST/sub/pkg"; git -C "$NEST" init -q
printf '[tool.ruff]\nline-length = 120\n' > "$NEST/sub/pyproject.toml"
printf '%s\n' "$LONG" > "$NEST/sub/pkg/long.py"
rc=0; out="$(lint "$NEST/sub/pkg/long.py")" || rc=$?
ok "a [tool.ruff] table in a sub-project between the file and the git root counts" \
  '[ "$rc" = 0 ] && ! grep -q -- "--select" "$CCC_TEST_RUFF_ARGS"'

# Edited-line filtering against HEAD.
LR="$TMP/lines"; mkdir -p "$LR/pkg"; git -C "$LR" init -q
printf 'import os\n\nx = 1\n' > "$LR/pkg/mod.py"
printf 'import os\npass\n' > "$LR/pkg/del.py"
printf 'x = 1\n\ny = 2\n' > "$LR/pkg/syn.py"
gitc "$LR" add -A; gitc "$LR" commit -qm base
rc=0; out="$(lint "$LR/pkg/mod.py")" || rc=$?
ok "tracked file with no change vs HEAD: a pre-existing F401 is not repeated" \
  '[ "$rc" = 0 ] && [ -z "$out" ] && tail -1 "$CCC_STATE_DIR/post-edit-lint.log" | grep -q "findings=0 rules=default preexisting=1"'
printf 'import sys\n' >> "$LR/pkg/mod.py"
rc=0; out="$(lint "$LR/pkg/mod.py")" || rc=$?
ok "a new F401 on an edited line is reported, the pre-existing one is not" \
  '[ "$rc" = 0 ] && ctx_of "$out" | grep -q "pkg/mod.py:4:8: F401" && ! ctx_of "$out" | grep -q "mod.py:1:8"'
ok "header counts edited-line findings only; pre-existing ones are summarised" \
  'ctx_of "$out" | head -1 | grep -q "post-edit-lint (ruff): 1 finding(s) in pkg/mod.py" && ctx_of "$out" | grep -q "^(1 pre-existing finding(s) outside the edited lines not shown)$"'
gitc "$LR" add pkg/mod.py
rc=0; out="$(lint "$LR/pkg/mod.py")" || rc=$?
ok "staged edits still count as changed (diff is against HEAD)" 'ctx_of "$out" | grep -q "pkg/mod.py:4:8: F401"'
printf 'import os\n' > "$LR/pkg/del.py"
rc=0; out="$(lint "$LR/pkg/del.py")" || rc=$?
ok "a pure deletion touches the adjacent lines" 'ctx_of "$out" | grep -q "pkg/del.py:1:8: F401"'
printf 'x = 1\n\ndef f(:\n' > "$LR/pkg/syn.py"
rc=0; out="$(lint "$LR/pkg/syn.py")" || rc=$?
ok "syntax errors are kept even off the edited lines" 'ctx_of "$out" | grep -q "pkg/syn.py:1:7: invalid-syntax"'
printf 'import os\nimport re\n' > "$LR/pkg/new.py"
rc=0; out="$(lint "$LR/pkg/new.py")" || rc=$?
ok "an untracked new file reports every line" \
  'ctx_of "$out" | head -1 | grep -q "2 finding(s) in pkg/new.py" && ! ctx_of "$out" | grep -q "pre-existing"'
NH="$TMP/nohead"; mkdir -p "$NH"; git -C "$NH" init -q
printf 'import os\n' > "$NH/a.py"; gitc "$NH" add a.py
rc=0; out="$(lint "$NH/a.py")" || rc=$?
ok "a repo without HEAD falls back to the whole file" 'ctx_of "$out" | grep -q "a.py:1:8: F401"'
mkdir -p "$TMP/outside2"; printf 'import os\n%s\n' "$LONG" > "$TMP/outside2/loose.py"
rc=0; out="$(CCC_POST_EDIT_LINT_SCOPE=all lint "$TMP/outside2/loose.py")" || rc=$?
ok "non-git path (scope=all): whole-file report with --select E,F" \
  '[ "$rc" = 0 ] && ctx_of "$out" | head -1 | grep -q "2 finding(s)" && grep -q -- "--select E,F" "$CCC_TEST_RUFF_ARGS"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
