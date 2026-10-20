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
#   - Tool: ruff (`CCC_POST_EDIT_LINT_RUFF`, PATH, repo venvs, ~/.local/bin);
#     without ruff it falls back to `python3 -m py_compile` (syntax only).
#   - Rules (#2265): when the file's project carries its own ruff config
#     (`ruff.toml`, `.ruff.toml`, or a `pyproject.toml` with a `[tool.ruff` table,
#     searched from the file's directory up to the git root, or `/` outside a
#     repo) no `--select` is passed, so ruff's own config discovery from the file
#     path gives the same verdict as CI (a CLI `--select E` would re-enable a
#     configured `ignore = ["E501"]`). Without a config: `--select E,F`. ruff runs
#     with the repo root as cwd.
#   - Edited lines only (#2265): for a file tracked at HEAD, rule findings are
#     kept only on lines touched by `git diff -U0 HEAD` (a pure deletion touches
#     the lines on either side); pre-existing findings elsewhere are counted, not
#     repeated. Syntax errors (no rule code) are always kept. Untracked/new
#     files, repos without HEAD, non-git paths or a failing diff fall back to the
#     whole file. Limitation: a finding a change causes on an untouched line
#     (e.g. deleting the last use of an import -> F401 at the import) is hidden.
#   - Bounded: one file, CCC_POST_EDIT_LINT_TIMEOUT (default 5 s), output capped
#     at CCC_POST_EDIT_LINT_MAX_BYTES (default 2048) with the finding count kept.
#   - Body-free log (ts, tool, mode, finding count, rule source, hidden
#     pre-existing count) for before/after measurement
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

# has_ruff_config <dir> <stop-dir> — a ruff config in <dir> or an ancestor, up to
# <stop-dir> (the git root) or `/`. Mirrors ruff's own discovery file set.
has_ruff_config() {
  local d="$1" stop="$2"
  while [ -n "$d" ]; do
    { [ -f "$d/ruff.toml" ] || [ -f "$d/.ruff.toml" ]; } && return 0
    [ -f "$d/pyproject.toml" ] && grep -qE '^[[:space:]]*\[tool\.ruff[].]' "$d/pyproject.toml" 2>/dev/null && return 0
    { [ "$d" = "/" ] || { [ -n "$stop" ] && [ "$d" = "$stop" ]; }; } && return 1
    d="$(dirname "$d")"
  done
  return 1
}

# changed_ranges — "start end" new-side line ranges of `git diff -U0 HEAD` for
# the file, one per line (possibly none). Fails (non-zero) when the edited lines
# cannot be determined cheaply: no repo, no HEAD, untracked file, diff error.
changed_ranges() {
  [ -n "$repo_root" ] || return 1
  git -C "$repo_root" rev-parse -q --verify HEAD >/dev/null 2>&1 || return 1
  git -C "$repo_root" ls-files --error-unmatch -- "$fpath" >/dev/null 2>&1 || return 1
  local diff
  diff="$(run_bounded git -C "$repo_root" diff --no-color --no-ext-diff --no-textconv -U0 HEAD -- "$fpath" 2>/dev/null)" || return 1
  printf '%s\n' "$diff" | sed -nE 's/^@@ -[0-9]+(,[0-9]+)? \+([0-9]+)(,([0-9]+))? @@.*/\2 \4/p' |
    awk '{ n = ($2 == "") ? 1 : $2 + 0; if (n == 0) print $1, $1 + 1; else print $1, $1 + n - 1 }'
}

mode=""; findings=""; count=0; rules="-"; hidden=0
if [ -n "$ruff" ]; then
  mode="ruff"
  select=(--select "E,F"); rules="default"
  if has_ruff_config "$(dirname "$fpath")" "$repo_root"; then select=(); rules="repo"; fi
  findings="$(cd "$cwd" 2>/dev/null && run_bounded "$ruff" check ${select[@]+"${select[@]}"} --no-fix --output-format concise -- "$fpath" 2>/dev/null)"
  rc=$?
  # ruff: 0 clean, 1 findings, 2+ tool/usage error (treat as nothing to say).
  if [ "$rc" -eq 0 ]; then findings=""; elif [ "$rc" -ne 1 ]; then findings=""; mode="ruff-error"; fi
  # Drop ruff's trailing "Found N errors." summary; one finding per line stays.
  findings="$(printf '%s\n' "$findings" | grep -E '^.+:[0-9]+:[0-9]+: ' || true)"
  # Keep rule findings on edited lines only, when those lines are known.
  if [ -n "$findings" ] && ranges="$(changed_ranges)"; then
    all="$(printf '%s\n' "$findings" | grep -c .)"
    findings="$(printf '%s\n' "$findings" | awk -v ranges="$(printf '%s' "$ranges" | tr '\n' ';')" '
      BEGIN { n = split(ranges, r, ";"); m = 0
              for (i = 1; i <= n; i++) if (split(r[i], p, " ") == 2) { lo[++m] = p[1] + 0; hi[m] = p[2] + 0 } }
      NF == 0 { next }
      { if (!match($0, /:[0-9]+:[0-9]+: /)) { print; next }
        pos = substr($0, RSTART + 1, RLENGTH - 1); split(pos, q, ":"); ln = q[1] + 0
        rest = substr($0, RSTART + RLENGTH); split(rest, w, " "); code = w[1]
        if (code !~ /^[A-Z]+[0-9]+$/) { print; next }          # syntax errors: always kept
        for (i = 1; i <= m; i++) if (ln >= lo[i] && ln <= hi[i]) { print; next } }
    ')"
    kept=0; [ -n "$findings" ] && kept="$(printf '%s\n' "$findings" | grep -c .)"
    hidden=$((all - kept))
  fi
else
  mode="py_compile"
  findings="$(run_bounded python3 -m py_compile "$fpath" 2>&1 >/dev/null || true)"
  findings="$(printf '%s\n' "$findings" | grep -vE '^\s*$' || true)"
fi
[ -n "$findings" ] && count="$(printf '%s\n' "$findings" | grep -c .)"

# Body-free measurement log; mirrors audit.sh (counts only, no path or code).
if [ -d "$STATE_DIR" ] || mkdir -p "$STATE_DIR" 2>/dev/null; then
  printf '%s tool=%s mode=%s findings=%s rules=%s preexisting=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tool" "$mode" "$count" "$rules" "$hidden" 2>/dev/null >> "$LOG" || :
fi

[ "$count" -gt 0 ] || exit 0

# Bound what reaches the model: keep the count, cut the body on a line boundary.
body="$findings"
if [ "${#body}" -gt "$MAX_BYTES" ]; then
  body="$(printf '%s\n' "$findings" | head -c "$MAX_BYTES" | sed '$d')"
  body="$body"$'\n'"… (truncated; $count findings total)"
fi
[ "$hidden" -gt 0 ] && body="$body"$'\n'"($hidden pre-existing finding(s) outside the edited lines not shown)"
rel="${fpath#"${repo_root:+$repo_root/}"}"
header="post-edit-lint ($mode): $count finding(s) in $rel — fix before running tests, or justify if intentional."
ctx="$header"$'\n'"$body"

jq -n --arg ctx "$ctx" '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}' 2>/dev/null || :
exit 0
