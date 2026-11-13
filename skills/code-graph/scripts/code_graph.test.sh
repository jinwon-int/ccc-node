#!/usr/bin/env bash
# Hook-test wrapper (discovered by scripts/validate-harness.sh) for the
# hermetic code_graph_test.py unittest suite (ccc-node#2707): temp git repos,
# private cache root, no network. validate-harness requires a final
# `PASS=<n> FAIL=<n>` line, so the unittest tally is translated into one.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)" || exit 1
trap 'rm -rf "$TMP"' EXIT

python3 "$DIR/code_graph_test.py" >"$TMP/unit.out" 2>&1
rc=$?
ran="$(sed -nE 's/^Ran ([0-9]+) tests? in .*/\1/p' "$TMP/unit.out" | tail -1)"
ran="${ran:-0}"
bad="$(sed -nE 's/^FAILED \((.*)\)$/\1/p' "$TMP/unit.out" | tail -1 \
  | grep -oE '(failures|errors)=[0-9]+' | awk -F= '{n+=$2} END{print n+0}')"
bad="${bad:-0}"
if [ "$rc" -ne 0 ] || [ "$ran" -eq 0 ]; then
  [ "$bad" -eq 0 ] && bad=1
  echo "FAIL: code_graph unit suite (rc=$rc)"; tail -40 "$TMP/unit.out"
fi
echo "PASS=$((ran - bad < 0 ? 0 : ran - bad)) FAIL=$bad"
[ "$bad" -eq 0 ]
