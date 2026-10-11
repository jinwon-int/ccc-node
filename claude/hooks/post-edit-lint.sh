#!/usr/bin/env bash
# PostToolUse(Edit|Write|MultiEdit) post-edit diagnostics for Python files (#2261).
#
# Harness-design evidence (arXiv 2609.20804 §2.4) keeps a fast, read-only
# syntax/lint pass right after every edit so the model sees undefined names,
# syntax errors and unused imports BEFORE it spends a turn on the tests. The
# repo already carries the ruff rules in pyproject.toml, but until now the
# agent met them for the first time in CI.
#
# Contract:
#   - Reminder, not a gate: PostToolUse cannot block; this hook always exits 0
#     and only speaks when it has findings (hookSpecificOutput.additionalContext).
#   - Scope: `.py` files only. Default CCC_POST_EDIT_LINT_SCOPE=repo lints a file
#     only when it sits inside a git work tree; `all` lints any `.py` path.
#   - Tool: ruff (`CCC_POST_EDIT_LINT_RUFF`, PATH, repo venvs, ~/.local/bin) with
#     `--select E,F`; without ruff it falls back to `python3 -m py_compile`
#     (syntax only). ruff runs with the repo root as cwd so pyproject per-file
#     ignores apply.
#   - Bounded: one file, CCC_POST_EDIT_LINT_TIMEOUT (default 5 s), output capped
#     at CCC_POST_EDIT_LINT_MAX_BYTES (default 2048) with the finding count kept.
#   - Body-free log (ts, tool, mode, finding count) for before/after measurement
#     in $CCC_STATE_DIR/post-edit-lint.log; the path, code and findings are never
#     persisted. Off switch: CCC_POST_EDIT_LINT=0.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
# shellcheck source=claude/hooks/lib/hook-common.sh
[ -r "$HERE/lib/hook-common.sh" ] && . "$HERE/lib/hook-common.sh"
command -v is_disabled >/dev/null 2>&1 || is_disabled() { case "${1:-}" in 0|false|FALSE|off|OFF|no|NO) return 0;; *) return 1;; esac; }
umask 077

is_disabled "${CCC_POST_EDIT_LINT:-1}" && exit 0
command -v jq >/dev/null 2>&1 || exit 0

input="$(cat 2>/dev/null)"; [ -n "$input" ] || exit 0

meta="$(printf '%s' "$input" | jq -r '
  (.tool_name // "") as $tool
  | (.tool_input.file_path // "") as $fpath
  | if (["Edit","Write","MultiEdit"] | index($tool)) != null and ($fpath | test("\\.py$"))
    then [$tool, $fpath] | join("\u0001") else empty end
' 2>/dev/null)" || exit 0
[ -n "$meta" ] || exit 0
tool=""; fpath=""
IFS=$'\x01' read -r tool fpath <<<"$meta" || true
case "$fpath" in /*) ;; *) exit 0 ;; esac   # absolute paths only
[ -f "$fpath" ] || exit 0                    # the edit must have landed

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/post-edit-lint.log"
TIMEOUT="${CCC_POST_EDIT_LINT_TIMEOUT:-5}"; case "$TIMEOUT" in ''|*[!0-9]*) TIMEOUT=5 ;; esac
MAX_BYTES="${CCC_POST_EDIT_LINT_MAX_BYTES:-2048}"; case "$MAX_BYTES" in ''|*[!0-9]*) MAX_BYTES=2048 ;; esac

# Scope gate: repo (default) requires a git work tree around the file.
repo_root="$(git -C "$(dirname "$fpath")" rev-parse --show-toplevel 2>/dev/null || true)"
case "${CCC_POST_EDIT_LINT_SCOPE:-repo}" in
  all) ;;
  *) [ -n "$repo_root" ] || exit 0 ;;
esac
cwd="${repo_root:-$(dirname "$fpath")}"

# Locate ruff: explicit override, PATH, repo venvs, user bin.
ruff=""
for cand in "${CCC_POST_EDIT_LINT_RUFF:-}" "$(command -v ruff 2>/dev/null || true)" \
            "${repo_root:+$repo_root/bridge/venv/bin/ruff}" "${repo_root:+$repo_root/.venv/bin/ruff}" \
            "${HOME:-/root}/.local/bin/ruff"; do
  [ -n "$cand" ] && [ -x "$cand" ] && { ruff="$cand"; break; }
done

run_bounded() { # run_bounded <cmd...> — timeout when available, never fatal
  if command -v timeout >/dev/null 2>&1; then timeout "$TIMEOUT" "$@"; else "$@"; fi
}

mode=""; findings=""; count=0
if [ -n "$ruff" ]; then
  mode="ruff"
  findings="$(cd "$cwd" 2>/dev/null && run_bounded "$ruff" check --select E,F --no-fix --output-format concise -- "$fpath" 2>/dev/null)"
  rc=$?
  # ruff: 0 clean, 1 findings, 2+ tool/usage error (treat as nothing to say).
  if [ "$rc" -eq 0 ]; then findings=""; elif [ "$rc" -ne 1 ]; then findings=""; mode="ruff-error"; fi
  # Drop ruff's trailing "Found N errors." summary; one finding per line stays.
  findings="$(printf '%s\n' "$findings" | grep -E '^.+:[0-9]+:[0-9]+: ' || true)"
else
  mode="py_compile"
  findings="$(run_bounded python3 -m py_compile "$fpath" 2>&1 >/dev/null || true)"
  findings="$(printf '%s\n' "$findings" | grep -vE '^\s*$' || true)"
fi
[ -n "$findings" ] && count="$(printf '%s\n' "$findings" | grep -c .)"

# Body-free measurement log; mirrors audit.sh (counts only, no path or code).
if [ -d "$STATE_DIR" ] || mkdir -p "$STATE_DIR" 2>/dev/null; then
  printf '%s tool=%s mode=%s findings=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tool" "$mode" "$count" 2>/dev/null >> "$LOG" || :
fi

[ "$count" -gt 0 ] || exit 0

# Bound what reaches the model: keep the count, cut the body on a line boundary.
body="$findings"
if [ "${#body}" -gt "$MAX_BYTES" ]; then
  body="$(printf '%s\n' "$findings" | head -c "$MAX_BYTES" | sed '$d')"
  body="$body"$'\n'"… (truncated; $count findings total)"
fi
rel="${fpath#"${repo_root:+$repo_root/}"}"
header="post-edit-lint ($mode): $count finding(s) in $rel — fix before running tests, or justify if intentional."
ctx="$header"$'\n'"$body"

jq -n --arg ctx "$ctx" '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}' 2>/dev/null || :
exit 0
