#!/usr/bin/env bash
# quick-check — the pre-push signal in seconds (#2611).
#
# Why: 7 of the 11 failed harness-ci runs of 2026-10-26..31 failed on
# deterministic static checks (repo-wide node-name ratchet x5, skill registry
# freshness x2) that take <0.1 s locally but surface ~3 min after a push. The
# documented local checks (validate-harness.sh: 10+ min; its static phase:
# ~2.5 min, mostly whole-tree shellcheck) are too heavy to run on every push.
#
# What it runs (all from the repo root):
#   1. CCC_HARNESS_PHASE=quick bash scripts/validate-harness.sh — the SAME
#      static-phase code path CI runs, with shellcheck and the interpreter
#      scan limited to files changed since the base ref and only the
#      measured-slow suites on QUICK_SKIP_SUITES skipped; every other check,
#      tree guards included, still runs.
#   2. python3 scripts/changelog_fragments.py check   (python-lint step in CI)
#   3. ruff check on the changed *.py files, when ruff is installed
# It never replaces CI: the hook-test suites, bridge tests, mypy and the
# whole-tree shellcheck sweep stay CI-only.
#
# Usage:
#   bash scripts/quick-check.sh [--base REF] [--verbose]
#   bash scripts/quick-check.sh --install-pre-push     # opt-in git hook
#   bash scripts/quick-check.sh --uninstall-pre-push
# Base ref: --base, else $CCC_HARNESS_QUICK_BASE, else merge-base of HEAD
# with origin/main (then main). With none, shellcheck lints the full scope.
#
# The pre-push hook is opt-in and for DEVELOPMENT checkouts only. A node's
# managed checkout (the one setup.sh guards with managed-checkout-guard)
# must stay on its branch with a clean tree; installation refuses there, and
# also refuses to overwrite a pre-push hook it did not write or to guess
# around a custom core.hooksPath. Bypass once with `git push --no-verify`.
set -uo pipefail

# git exports GIT_DIR (and friends) to hooks — from a linked worktree it is
# <common>/.git/worktrees/<name>. Left set, every `git init/commit` a check
# runs in a scratch repo would act on the USER's repo instead (it flipped
# core.bare and committed onto the pushed branch in review). Drop them all;
# repository discovery from the checkout directory is what every check wants.
# shellcheck disable=SC2046  # word-splitting the variable names is intended
unset $(git rev-parse --local-env-vars 2>/dev/null)

HOOK_MARKER="# marker: ccc-node:quick-check-pre-push v1"
GUARD_MARKER="ccc-node:managed-checkout-guard"

usage() { sed -n '/^# Usage:/,/^# Base ref:/p' "$0" | sed 's/^# \{0,1\}//; /^Base ref:/d'; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)" || exit 2
cd "$ROOT" || exit 2

mode=run; base="${CCC_HARNESS_QUICK_BASE:-}"; verbose=0; from_hook=0
base_explicit=0; [ -n "$base" ] && base_explicit=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base) [ "$#" -ge 2 ] || { echo "quick-check: --base needs a ref" >&2; exit 2; }; base="$2"; base_explicit=1; shift 2 ;;
    --verbose|-v) verbose=1; shift ;;
    --install-pre-push) mode=install; shift ;;
    --uninstall-pre-push) mode=uninstall; shift ;;
    --from-pre-push) from_hook=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "quick-check: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

hooks_dir() {
  if [ -n "$(git config --get core.hooksPath 2>/dev/null)" ]; then
    echo "quick-check: core.hooksPath is set ($(git config --get core.hooksPath)); wire scripts/quick-check.sh into that hook setup yourself" >&2
    return 1
  fi
  git rev-parse --git-path hooks 2>/dev/null
}

if [ "$mode" = install ] || [ "$mode" = uninstall ]; then
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "quick-check: not a git checkout" >&2; exit 2; }
  hdir="$(hooks_dir)" || exit 1
  hook="$hdir/pre-push"
  if [ "$mode" = uninstall ]; then
    if [ ! -e "$hook" ]; then echo "quick-check: no pre-push hook installed"; exit 0; fi
    if ! grep -qxF "$HOOK_MARKER" "$hook" 2>/dev/null; then
      echo "quick-check: $hook was not installed by quick-check — left untouched" >&2; exit 1
    fi
    rm -f "$hook" && echo "quick-check: removed $hook"; exit $?
  fi
  for g in "$hdir/pre-commit" "$hdir/post-checkout"; do
    if [ -f "$g" ] && grep -qF "$GUARD_MARKER" "$g" 2>/dev/null; then
      echo "quick-check: this is a node's MANAGED checkout ($GUARD_MARKER in $g)." >&2
      echo "  Develop in a separate clone/worktree; the managed checkout stays hook-free (#1328)." >&2
      exit 1
    fi
  done
  if [ -e "$hook" ] && ! grep -qxF "$HOOK_MARKER" "$hook" 2>/dev/null; then
    echo "quick-check: $hook already exists and is not ours — left untouched." >&2
    echo "  Call 'bash scripts/quick-check.sh' from it instead." >&2
    exit 1
  fi
  mkdir -p "$hdir" || exit 1
  tmp="$hook.tmp.$$"
  cat >"$tmp" <<HOOK || { rm -f "$tmp"; exit 1; }
#!/usr/bin/env bash
$HOOK_MARKER
# Installed by: bash scripts/quick-check.sh --install-pre-push (opt-in, #2611)
# Remove with:  bash scripts/quick-check.sh --uninstall-pre-push
# Bypass once:  git push --no-verify
# Drop the GIT_DIR & co. git exports to hooks (linked worktrees: see quick-check.sh).
# shellcheck disable=SC2046
unset \$(git rev-parse --local-env-vars 2>/dev/null)
root="\$(git rev-parse --show-toplevel)" || exit 1
if [ ! -f "\$root/scripts/quick-check.sh" ]; then
  echo "quick-check: scripts/quick-check.sh not in this checkout — pre-push check skipped" >&2
  exit 0
fi
exec bash "\$root/scripts/quick-check.sh" --from-pre-push <&0
HOOK
  chmod 0755 "$tmp" && mv -f "$tmp" "$hook" || { rm -f "$tmp"; exit 1; }
  echo "quick-check: installed $hook"
  exit 0
fi

# --- run --------------------------------------------------------------------
start=$(date +%s)
if [ -n "$base" ]; then
  if ! git rev-parse --verify --quiet "$base^{commit}" >/dev/null 2>&1; then
    echo "quick-check: base ref '$base' does not resolve to a commit" >&2
    exit 2
  fi
elif [ "$base_explicit" = 0 ]; then
  base="$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || true)"
fi
failed=0

if [ "$from_hook" = 1 ]; then
  # The checks read the working tree of the current checkout. Say so when
  # that is not exactly what is being pushed (stdin: <lref> <lsha> <rref> <rsha>).
  head_sha="$(git rev-parse HEAD 2>/dev/null || true)"
  # Bounded read: git closes the pipe after the last ref; a caller that hands
  # over an open terminal or a never-closed pipe must not hang the push.
  while read -r -t 5 lref lsha _rref _rsha; do
    case "$lsha" in ''|*[!0]*) ;; *) continue ;; esac  # all-zero sha = delete
    [ "$lsha" = "$head_sha" ] || echo "quick-check: warning: pushing $lref ($lsha), but the checks run on the checked-out HEAD ($head_sha)"
  done
  git diff --quiet HEAD -- 2>/dev/null \
    || echo "quick-check: warning: the working tree differs from HEAD; the checks see uncommitted changes that this push does not carry"
fi

say_step() { printf '== quick-check: %s ==\n' "$*"; }

say_step "validate-harness (quick phase${base:+, base ${base:0:12}})"
vh_out="$(mktemp "${TMPDIR:-/tmp}/quick-check.XXXXXX")" || exit 2
trap 'rm -f "$vh_out"' EXIT
CCC_HARNESS_PHASE=quick CCC_HARNESS_QUICK_BASE="$base" bash scripts/validate-harness.sh >"$vh_out" 2>&1
vh_rc=$?
if [ "$verbose" = 1 ]; then
  cat "$vh_out"
else
  # Hide the hundreds of per-file "ok" lines and section headers; keep every
  # FAIL, fix hint, captured tool output and every "(quick: ...)" note.
  awk '/^== .* ==$/ || /^=+$/ { next } /^  ok / && !/\(quick: / { next } { print }' "$vh_out"
fi
[ "$vh_rc" = 0 ] || failed=1

say_step "changelog fragments"
if python3 scripts/changelog_fragments.py check; then :; else failed=1; fi

say_step "ruff (changed *.py)"
if command -v ruff >/dev/null 2>&1; then
  if [ -n "$base" ]; then
    mapfile -d '' -t changed_py < <(git diff -z --name-only --diff-filter=ACMRT "$base" -- '*.py' 2>/dev/null)
  else
    echo "no base ref — linting the whole tree"
    changed_py=(.)
  fi
  if [ "${#changed_py[@]}" -eq 0 ]; then
    echo "no changed *.py"
  elif ruff check "${changed_py[@]}"; then :; else failed=1; fi
else
  echo "ruff not installed — skipped (CI python-lint runs it)"
fi

elapsed=$(( $(date +%s) - start ))
if [ "$failed" = 0 ]; then
  echo "QUICK CHECK: PASS (${elapsed}s) — CI still runs the hook-test shards, bridge tests and full shellcheck"
  exit 0
fi
echo "QUICK CHECK: FAIL (${elapsed}s)"
if [ "$from_hook" = 1 ]; then
  echo "push blocked by the opt-in pre-push hook — fix the above, or bypass once with: git push --no-verify"
fi
exit 1
