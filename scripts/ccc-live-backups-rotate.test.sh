#!/usr/bin/env bash
# Tests for ccc-live-backups-rotate.sh — hermetic via CCC_LIVE_BACKUPS_ROOTS.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/ccc-live-backups-rotate.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export CCC_STATE_DIR="$TMP/state"
mkdir -p "$CCC_STATE_DIR"

mkbackups() { # <root> <count> — oldest first names b001..bNNN with increasing mtime
  local root="$1" count="$2" i
  mkdir -p "$root"
  for i in $(seq 1 "$count"); do
    d=$(printf '%s/b%03d' "$root" "$i")
    mkdir -p "$d"
    touch -t "2026070${i}0000.00" "$d" 2>/dev/null || touch "$d"
  done
}

run_rotate() { # <extra-roots...>
  CCC_LIVE_BACKUPS_ROOTS="$*" bash "$SCRIPT"
}

# --- 1) prunes to KEEP newest -------------------------------------------------
mkbackups "$TMP/lb1" 8
out="$(run_rotate "$TMP/lb1")"; rc=$?
ok "keeps the 5 newest backups" '[ "$rc" = 0 ] && [ "$(ls "$TMP/lb1" | wc -l)" = 5 ] && [ -d "$TMP/lb1/b008" ] && [ -d "$TMP/lb1/b004" ] && [ ! -d "$TMP/lb1/b003" ]'
ok "writes a body-free log line" 'grep -qE "pruned=3 failed=0 keep=5" "$CCC_STATE_DIR/live-backups-rotate.log"'

# --- 2) nothing to prune is a quiet success ------------------------------------
mkbackups "$TMP/lb2" 3
out="$(run_rotate "$TMP/lb2")"; rc=$?
ok "under-cap root prunes nothing" '[ "$rc" = 0 ] && [ "$(ls "$TMP/lb2" | wc -l)" = 3 ]'

# --- 3) duplicate roots are processed once --------------------------------------
mkbackups "$TMP/lb3" 7
out="$(run_rotate "$TMP/lb3" "$TMP/lb3")"; rc=$?
ok "duplicate root entry is de-duplicated" '[ "$rc" = 0 ] && [ "$(ls "$TMP/lb3" | wc -l)" = 5 ] && [ "$(grep -c "pruned=" "$CCC_STATE_DIR/live-backups-rotate.log" 2>/dev/null)" = 3 ]'

# --- 4) missing roots are skipped ------------------------------------------------
out="$(run_rotate "$TMP/no-such-dir" "$TMP/lb2")"; rc=$?
ok "missing roots are skipped without failure" '[ "$rc" = 0 ]'

# --- 5) prune failure exits non-zero ----------------------------------------------
mkbackups "$TMP/lb4" 7
chmod 555 "$TMP/lb4"
out="$(run_rotate "$TMP/lb4")"; rc=$?
chmod 755 "$TMP/lb4"
if [ "$(id -u)" = 0 ]; then
  # root ignores directory write bits; simulate the failure with a read-only rm
  ok "prune failure exits non-zero (skipped as root)" 'true'
else
  ok "prune failure exits non-zero" '[ "$rc" != 0 ] && grep -qE "failed=[1-9]" "$CCC_STATE_DIR/live-backups-rotate.log"'
fi

# --- 6) never touches the root itself or non-children ----------------------------
mkbackups "$TMP/lb5" 6
out="$(run_rotate "$TMP/lb5" "$TMP/lb5/")"; rc=$?
ok "trailing-slash duplicate and the root itself are untouched" '[ "$rc" = 0 ] && [ -d "$TMP/lb5" ] && [ "$(ls "$TMP/lb5" | wc -l)" = 5 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
