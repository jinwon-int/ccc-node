#!/usr/bin/env bash
# self-update: detect drift between this node's ccc-node checkout and origin/main.
# READ-ONLY — fetches and reports; never pulls, installs, or restarts.
set -uo pipefail

REPO="${CCC_REPO_DIR:-$([ -d /opt/ccc-node/.git ] && echo /opt/ccc-node || echo "${HOME:-/root}/ccc-node")}"
if [ ! -d "$REPO/.git" ]; then
  echo "ccc-node repo not found at $REPO (set CCC_REPO_DIR)"; exit 1
fi
cd "$REPO" || exit 1

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
if ! git fetch origin --quiet 2>/dev/null; then
  echo "git fetch failed (network/credentials?) — cannot check drift"; exit 1
fi

behind="$(git rev-list --count HEAD..origin/main 2>/dev/null || echo '?')"
ahead="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo '?')"
dirty="$(git status --porcelain 2>/dev/null | head -1)"

echo "repo:   $REPO"
echo "branch: $branch   (ahead $ahead / behind $behind of origin/main)"
[ -n "$dirty" ] && echo "WARNING: working tree has uncommitted changes — resolve before updating."

if [ "$behind" = "0" ]; then
  echo "STATUS: up to date — no harness update needed."
  exit 0
fi

echo "STATUS: $behind commit(s) behind origin/main — update available."
echo
echo "--- new commits ---"
git --no-pager log --oneline HEAD..origin/main 2>/dev/null | head -20
echo
echo "--- changed harness files (claude/ scripts/) ---"
git --no-pager diff --stat HEAD..origin/main -- claude scripts 2>/dev/null | head -40
echo
echo "--- CHANGELOG additions ---"
git --no-pager diff HEAD..origin/main -- CHANGELOG.md 2>/dev/null \
  | grep -E '^\+' | grep -vE '^\+\+\+' | sed 's/^+//' | head -30
