#!/usr/bin/env bash
# Tests for resource-pressure-guard.sh — specifically that the stale-provider
# reap only fires under actual memory/thermal pressure, not on wall-clock age
# alone (gongyung 2026-08-07: healthy xhigh-effort turns >15min with 2GB+
# free were being SIGTERM'd, surfacing to users as
# "Claude turn failed ... aborted_streaming"). The severe/emergency path
# (kills everything regardless of age) must stay intact as the real safety
# net for genuine low-memory/thermal situations.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
G="$HERE/resource-pressure-guard.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = 0 ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $2 (rc=$1)"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---- portability -------------------------------------------------------------
ok "shebang is env-based (runs on VPS + Termux)" '[ "$(head -1 "$G")" = "#!/usr/bin/env bash" ]'
ok "no hardcoded Termux interpreter path remains" '! grep -q "com.termux/files/usr/bin/bash" "$G"'

run_guard() { # <mem_kb> <thermal_mc> <age_sec> -> prints the JSON summary line
  local n=$1
  CCC_RESOURCE_GUARD_DRY_RUN=1 \
  CCC_RESOURCE_TEST_PIDS="99999" \
  CCC_RESOURCE_TEST_PROVIDER_AGE_SEC="$3" \
  CCC_RESOURCE_SAMPLE_MEM_KB="$1" \
  CCC_RESOURCE_SAMPLE_THERMAL_MC="$2" \
  CCC_RESOURCE_GUARD_ROOT="$TMP/root-$n-$RANDOM" \
  CCC_RESOURCE_GUARD_LOCK_DIR="$TMP/lock-$n-$RANDOM" \
  bash "$G" test
}

# ---- no pressure, provider well past the old 900s / new 1800s threshold -----
# This is the exact false-positive this patch fixes: plenty of free memory,
# normal thermal, but a long-running (healthy) provider used to get reaped.
# shellcheck disable=SC2034  # consumed inside the quoted ok() assertions below
out="$(run_guard 2200000 50000 2000)"
ok "no pressure + stale age: action=none" 'grep -q "\"action\":\"none\"" <<<"$out"'
ok "no pressure + stale age: terminated=0" 'grep -q "\"terminated\":0" <<<"$out"'

# ---- real pressure (low available memory) + same stale age -> still reaped --
# shellcheck disable=SC2034  # consumed inside the quoted ok() assertions below
out="$(run_guard 1200000 50000 2000)"
ok "pressure + stale age: action=terminate-stale-provider" 'grep -q "\"action\":\"terminate-stale-provider\"" <<<"$out"'
ok "pressure + stale age: terminated=1" 'grep -q "\"terminated\":1" <<<"$out"'

# ---- no pressure, provider younger than the (bumped) threshold --------------
# shellcheck disable=SC2034  # consumed inside the quoted ok() assertions below
out="$(run_guard 2200000 50000 1700)"
ok "no pressure + young provider: action=none" 'grep -q "\"action\":\"none\"" <<<"$out"'

# ---- emergency (very low memory) -> the severe path still kills on sight ----
# Independent of provider age: this is the real safety net and must be
# unaffected by the pressure-gate added around the stale-provider branch.
# shellcheck disable=SC2034  # consumed inside the quoted ok() assertions below
out="$(run_guard 600000 50000 60)"
ok "emergency: action=terminate-provider" 'grep -q "\"action\":\"terminate-provider\"" <<<"$out"'
ok "emergency: terminated=1" 'grep -q "\"terminated\":1" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
