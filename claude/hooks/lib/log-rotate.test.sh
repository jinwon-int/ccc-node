#!/usr/bin/env bash
# Tests for lib/log-rotate.sh — size-based generation rotation (#1882) and its
# two distill.log entry points (distill.sh, distill/pending-drain.sh).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOKS="$(cd "$HERE/.." && pwd)"
# shellcheck source=claude/hooks/lib/log-rotate.sh
. "$HERE/log-rotate.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/test-stub.sh"
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

fill() { # fill <path> <bytes> <marker>
  { printf '%s\n' "$3"; head -c "$2" /dev/zero | tr '\0' 'x'; } > "$1"
}
size_of() { wc -c < "$1" | tr -d '[:space:]'; }

# ---- under the cap: untouched ----------------------------------------------
D="$TMP/under"; mkdir -p "$D"
fill "$D/a.log" 100 gen0
ccc_rotate_log_if_large "$D/a.log" 1000 2
ok "file under the cap is left in place" '[ -f "$D/a.log" ] && [ ! -e "$D/a.log.1" ] && [ ! -e "$D/a.log.1.gz" ]'

# ---- over the cap: shifts into generations, keeps <keep> -------------------
export CCC_LOG_ROTATE_GZIP=0
D="$TMP/gens"; mkdir -p "$D"
fill "$D/a.log" 2000 gen1; ccc_rotate_log_if_large "$D/a.log" 1000 2
ok "over-cap live file moves to .1" '[ ! -e "$D/a.log" ] && head -n1 "$D/a.log.1" | grep -qx gen1'
fill "$D/a.log" 2000 gen2; ccc_rotate_log_if_large "$D/a.log" 1000 2
fill "$D/a.log" 2000 gen3; ccc_rotate_log_if_large "$D/a.log" 1000 2
ok "newest rotated generation is .1" 'head -n1 "$D/a.log.1" | grep -qx gen3'
ok "previous generation shifts to .2" 'head -n1 "$D/a.log.2" | grep -qx gen2'
ok "generations beyond keep are dropped" '[ ! -e "$D/a.log.3" ] && ! grep -rqx gen1 "$D"'
ok "no staging leftovers" '[ -z "$(find "$D" -name "*.rotating.*")" ]'
ok "rotated generation is owner-only" '[ "$(stat -c %a "$D/a.log.1" 2>/dev/null || stat -f %Lp "$D/a.log.1")" = 600 ]'
printf 'next\n' >> "$D/a.log"
ok "appends after rotation recreate the live log" '[ "$(cat "$D/a.log")" = next ]'

# ---- gzip generations (synchronous for determinism) ------------------------
if command -v gzip >/dev/null 2>&1; then
  export CCC_LOG_ROTATE_GZIP=1 CCC_LOG_ROTATE_GZIP_SYNC=1
  D="$TMP/gz"; mkdir -p "$D"
  fill "$D/a.log" 5000 gz1; ccc_rotate_log_if_large "$D/a.log" 1000 2
  ok "rotated generation is gzip-compressed" '[ -f "$D/a.log.1.gz" ] && [ ! -e "$D/a.log.1" ] && gzip -dc "$D/a.log.1.gz" | head -n1 | grep -qx gz1'
  fill "$D/a.log" 5000 gz2; ccc_rotate_log_if_large "$D/a.log" 1000 2
  fill "$D/a.log" 5000 gz3; ccc_rotate_log_if_large "$D/a.log" 1000 2
  ok "compressed generations shift and cap at keep" \
    'gzip -dc "$D/a.log.1.gz" | head -n1 | grep -qx gz3 && gzip -dc "$D/a.log.2.gz" | head -n1 | grep -qx gz2 && [ ! -e "$D/a.log.3.gz" ]'
  # A mixed leftover (.1 uncompressed from a gzip-less run) still shifts.
  D="$TMP/mixed"; mkdir -p "$D"
  fill "$D/a.log.1" 10 old-plain
  fill "$D/a.log" 5000 new; ccc_rotate_log_if_large "$D/a.log" 1000 2
  ok "uncompressed leftover generation shifts to .2" 'head -n1 "$D/a.log.2" | grep -qx old-plain && [ -f "$D/a.log.1.gz" ]'
  unset CCC_LOG_ROTATE_GZIP_SYNC
  export CCC_LOG_ROTATE_GZIP=0
fi

# ---- guards -------------------------------------------------------------------
D="$TMP/guard"; mkdir -p "$D"
fill "$D/a.log" 5000 g
ok "non-numeric max is a no-op (rc 0)" 'ccc_rotate_log_if_large "$D/a.log" abc 2 && [ -f "$D/a.log" ]'
ok "zero keep is a no-op (rc 0)" 'ccc_rotate_log_if_large "$D/a.log" 1000 0 && [ -f "$D/a.log" ]'
ok "missing file is a no-op (rc 0)" 'ccc_rotate_log_if_large "$D/missing.log" 1000 2 && [ ! -e "$D/missing.log.1" ]'
ln -s "$D/a.log" "$D/link.log"
ok "symlinked log is never rotated" 'ccc_rotate_log_if_large "$D/link.log" 1000 2 && [ -L "$D/link.log" ] && [ ! -e "$D/link.log.1" ]'

# ---- entry points: distill.sh and pending-drain.sh rotate distill.log ------
S="$TMP/state-distill"; mkdir -p "$S"
fill "$S/distill.log" 3000 legacy
: > "$S/distill.disabled"   # exit right after rotation + one skip line
CCC_STATE_DIR="$S" CCC_DISTILL_LOG_MAX_BYTES=1000 CCC_DISTILL_LOG_KEEP=2 \
  bash "$HOOKS/distill.sh" sessionend </dev/null >/dev/null 2>&1
ok "distill.sh rotates an oversized distill.log before appending" \
  'head -n1 "$S/distill.log.1" | grep -qx legacy && [ "$(size_of "$S/distill.log")" -lt 1000 ] && grep -q "skipped reason=disabled" "$S/distill.log"'

S="$TMP/state-drain"; mkdir -p "$S"
fill "$S/distill.log" 3000 legacy-drain
CCC_STATE_DIR="$S" CCC_DISTILL_LOG_MAX_BYTES=1000 \
  bash "$HOOKS/distill/pending-drain.sh" </dev/null >/dev/null 2>&1
ok "pending-drain.sh rotates an oversized distill.log" \
  'head -n1 "$S/distill.log.1" | grep -qx legacy-drain && [ ! -e "$S/distill.log" ]'

S="$TMP/state-small"; mkdir -p "$S"
fill "$S/distill.log" 10 small
CCC_STATE_DIR="$S" bash "$HOOKS/distill/pending-drain.sh" </dev/null >/dev/null 2>&1
ok "default cap leaves a small distill.log alone" '[ -f "$S/distill.log" ] && [ ! -e "$S/distill.log.1" ] && [ ! -e "$S/distill.log.1.gz" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
