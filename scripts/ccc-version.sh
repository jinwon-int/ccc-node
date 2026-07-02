#!/usr/bin/env bash
# ccc-version.sh — print the harness version anchor for this checkout.
set -euo pipefail
REPO="${CCC_VERSION_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO"
if git describe --tags --dirty --always >/dev/null 2>&1; then
  git describe --tags --dirty --always
elif git rev-parse --short HEAD >/dev/null 2>&1; then
  git rev-parse --short HEAD
else
  printf 'unknown\n'
fi
