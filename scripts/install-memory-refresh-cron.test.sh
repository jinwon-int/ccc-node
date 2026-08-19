#!/usr/bin/env bash
# Hermetic tests for install-memory-refresh-cron.sh.
# Uses a stub crontab (CCC_CRONTAB_CMD) backed by a temp file, so no real
# crontab is touched and the suite is platform-independent (Linux + Termux).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$ROOT/scripts/install-memory-refresh-cron.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccc-cron-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

export FAKE_CRON="$TMP/crontab.txt"
: > "$FAKE_CRON"

# Stub crontab: `-l` prints the file, `-` overwrites it from stdin.
STUB="$TMP/crontab-stub.sh"
cat > "$STUB" <<'STUBEOF'
#!/usr/bin/env bash
f="${FAKE_CRON:?}"
case "${1:-}" in
  -l) [ -s "$f" ] && cat "$f" || exit 1 ;;
  -)  cat > "$f" ;;
  *)  exit 2 ;;
esac
STUBEOF
chmod +x "$STUB"
export CCC_CRONTAB_CMD="$STUB"
export CCC_CLAUDE_DIR="$TMP/claude"
export CCC_STATE_DIR="$CCC_CLAUDE_DIR/state"
mkdir -p "$CCC_CLAUDE_DIR/hooks" "$CCC_CLAUDE_DIR/state"
: > "$CCC_CLAUDE_DIR/hooks/refresh-memory.sh"

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
# Entry lines are the only stamped lines; the #1077 BEGIN/END block markers
# carry the lane marker as a substring and must not be counted as entries.
marker_count() { grep -E "# ccc-node:memory-refresh gen=h_" "$FAKE_CRON" 2>/dev/null | wc -l | tr -d ' '; }
block_count() { grep -cF "# ccc-node:memory-refresh:begin" "$FAKE_CRON" 2>/dev/null | head -1; }

# dry-run does not mutate the crontab
out="$(bash "$INSTALLER" --dry-run 2>&1)"; rc=$?
ok "dry-run exits 0" '[ "$rc" = 0 ]'
ok "dry-run announces install" 'printf "%s" "$out" | grep -q "would install"'
ok "dry-run does not write crontab" '[ "$(marker_count)" = 0 ]'

# apply installs exactly one entry line inside one managed block (#1077)
out="$(bash "$INSTALLER" --apply 2>&1)"; rc=$?
ok "apply exits 0" '[ "$rc" = 0 ]'
ok "apply installs one marker line" '[ "$(marker_count)" = 1 ]'
ok "apply wraps the entry in a managed block" '[ "$(block_count)" = 1 ] && grep -qF "# ccc-node:memory-refresh:end" "$FAKE_CRON"'
ok "installed line carries default schedule" 'grep -qF "*/30 * * * *" "$FAKE_CRON"'
ok "installed line loads login PATH via bash -lc" 'grep -qF "bash -lc" "$FAKE_CRON"'

# generation stamp (#1081): content hash of installer + shared rendering libs,
# pinned at end of the entry line; inputs owned by ccc_installer_gen_inputs
# shellcheck source=/dev/null
. "$ROOT/scripts/lib/installer-gen-stamp.sh"
want_gen="$(ccc_installer_gen_stamp_auto "$INSTALLER")"
ok "installed line carries gen stamp" 'grep -qE "# ccc-node:memory-refresh gen=h_[0-9a-f]{12}$" "$FAKE_CRON"'
ok "gen stamp matches installer content" 'grep -qF "gen=$want_gen" "$FAKE_CRON"'
ok "BEGIN/END block markers stay unstamped (exact-match parsed)" '! grep -qE "memory-refresh:(begin|end) gen=" "$FAKE_CRON"'

# idempotent: re-apply keeps a single line
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "re-apply stays idempotent (one line)" '[ "$(marker_count)" = 1 ]'
ok "re-apply keeps one block" '[ "$(block_count)" = 1 ]'
ok "re-apply keeps the same gen stamp" 'grep -qF "gen=$want_gen" "$FAKE_CRON"'

# custom schedule replaces, still single line
bash "$INSTALLER" --apply --schedule "17 * * * *" >/dev/null 2>&1
ok "custom schedule still single line" '[ "$(marker_count)" = 1 ]'
ok "custom schedule applied" 'grep -qF "17 * * * *" "$FAKE_CRON"'
ok "old schedule removed" '! grep -qF "*/30 * * * *" "$FAKE_CRON"'

# install record (#1081 phase 2): replay material for self-update
REC="$CCC_CLAUDE_DIR/state/install-memory-refresh-cron.json"
ok "apply writes an install record" '[ -f "$REC" ]'
ok "record carries schema/marker/gen" 'jq -e ".schema==\"ccc.install-record.v1\" and .marker==\"# ccc-node:memory-refresh\" and .gen==\"$want_gen\"" "$REC" >/dev/null'
ok "record argv materializes the resolved schedule" 'jq -e ".argv == [\"--apply\",\"--schedule\",\"17 * * * *\"]" "$REC" >/dev/null'
ok "record is owner-only" '[ "$(stat -c %a "$REC")" = 600 ]'

# a pre-existing unrelated cron line is preserved
printf '0 4 * * * echo keepme\n' >> "$FAKE_CRON"
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "unrelated cron line preserved" 'grep -qF "echo keepme" "$FAKE_CRON"'
ok "still one marker line after preserve" '[ "$(marker_count)" = 1 ]'

# remove takes the marker line out, keeps the unrelated one
bash "$INSTALLER" --apply --remove >/dev/null 2>&1
ok "remove deletes marker line" '[ "$(marker_count)" = 0 ]'
ok "remove deletes the block markers too" '! grep -qF "# ccc-node:memory-refresh:begin" "$FAKE_CRON"'
ok "remove keeps unrelated line" 'grep -qF "echo keepme" "$FAKE_CRON"'
ok "remove drops the install record (no resurrection via re-apply)" '[ ! -f "$REC" ]'

# legacy migration (#1077): a bare stamped pre-#1077 line is folded into a block
printf '%s\n' '*/30 * * * * bash -lc '"'"'old'"'"'  # ccc-node:memory-refresh gen=h_000000000000' '0 4 * * * echo keepme2' > "$FAKE_CRON"
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "legacy bare marker line is migrated into a block" '[ "$(marker_count)" = 1 ] && [ "$(block_count)" = 1 ]'
ok "legacy line content replaced (old gen gone)" '! grep -qF "gen=h_000000000000" "$FAKE_CRON"'
ok "migration preserves unrelated lines" 'grep -qF "echo keepme2" "$FAKE_CRON"'

# corrupt managed block fails closed (#1077: guard now covers this lane too)
printf '%s\n' '# ccc-node:memory-refresh:begin' '*/30 * * * * dangling  # ccc-node:memory-refresh gen=h_000000000000' > "$FAKE_CRON"
out="$(bash "$INSTALLER" --apply 2>&1)"; rc=$?
ok "corrupt managed block exits 4" '[ "$rc" = 4 ]'
ok "corrupt managed block is reported" 'printf "%s" "$out" | grep -q "corrupt managed schedule block"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
