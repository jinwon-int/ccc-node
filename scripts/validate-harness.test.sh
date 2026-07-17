#!/usr/bin/env bash
# Regression tests for validate-harness.sh scratch-dir hermeticity (#565).
# Usage: bash scripts/validate-harness.test.sh   (exit 0 = all pass)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VALIDATE="$ROOT/scripts/validate-harness.sh"
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

HOSTILE="$(mktemp -d)"
trap 'rm -rf "$HOSTILE" 2>/dev/null || true' EXIT
# A stale fixed-name artifact left by "another account" in the shared temp dir.
# Before #565, both the harness (rendered.json) and its child tests
# (checkpoint-guard.out) opened fixed names directly in ${TMPDIR:-/tmp} and
# false-FAILed on such hostile state.
touch "$HOSTILE/rendered.json" "$HOSTILE/checkpoint-guard.out"
chmod 000 "$HOSTILE/rendered.json" "$HOSTILE/checkpoint-guard.out" 2>/dev/null || true

# shellcheck disable=SC2034  # tmp/tmpdir are referenced inside eval'd ok() assertions
read -r tmp tmpdir <<<"$(TMPDIR="$HOSTILE" bash "$VALIDATE" --print-scratch)"
# NOTE: mktemp -d honours the caller TMPDIR, so the private dir may be NESTED
# under it — that is fine: hermeticity comes from the fresh unique 0700 dir,
# not its parent. What must never happen is using the caller dir ITSELF (where
# the stale fixed-name artifacts live).
ok "validate resolves a private scratch dir, never the caller TMPDIR itself" \
  '[ -n "$tmp" ] && [ "$tmp" != "$HOSTILE" ]'
ok "validate exports the private scratch as TMPDIR for child tests" \
  '[ "$tmpdir" = "$tmp" ]'
ok "scratch dir is cleaned up on exit" '[ ! -d "$tmp" ]'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = "0" ]
