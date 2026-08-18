#!/usr/bin/env bash
# Hermetic tests for install-pr-status-poll-cron.sh.
# Uses a stub crontab (CCC_CRONTAB_CMD) backed by a temp file, so no real
# crontab is touched and the suite is platform-independent (Linux + Termux).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$ROOT/scripts/install-pr-status-poll-cron.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccc-cron-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

export FAKE_CRON="$TMP/crontab.txt"
: > "$FAKE_CRON"

# Stub crontab: `-l` prints the file, `-` overwrites it from stdin.
# Shebang must resolve to a real path: this sandbox has no /usr/bin/env (same
# constraint bridge/start.sh works around for its own restart-spawn target).
STUB="$TMP/crontab-stub.sh"
BASH_BIN="$(command -v bash)"
cat > "$STUB" <<STUBEOF
#!$BASH_BIN
f="\${FAKE_CRON:?}"
case "\${1:-}" in
  -l) [ -s "\$f" ] && cat "\$f" || exit 1 ;;
  -)  cat > "\$f" ;;
  *)  exit 2 ;;
esac
STUBEOF
chmod +x "$STUB"
export CCC_CRONTAB_CMD="$STUB"
export CCC_CLAUDE_DIR="$TMP/claude"
export CCC_STATE_DIR="$CCC_CLAUDE_DIR/state"
mkdir -p "$CCC_CLAUDE_DIR/hooks" "$CCC_CLAUDE_DIR/state"
: > "$CCC_CLAUDE_DIR/hooks/ccc-pr-status-poll.sh"

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
marker_count() { grep -cF "# ccc-node:pr-status-poll" "$FAKE_CRON" 2>/dev/null | head -1; }

# dry-run does not mutate the crontab
out="$(bash "$INSTALLER" --dry-run 2>&1)"; rc=$?
ok "dry-run exits 0" '[ "$rc" = 0 ]'
ok "dry-run announces install" 'printf "%s" "$out" | grep -q "would install"'
ok "dry-run does not write crontab" '[ "$(marker_count)" = 0 ]'

# apply installs exactly one marker line
out="$(bash "$INSTALLER" --apply 2>&1)"; rc=$?
ok "apply exits 0" '[ "$rc" = 0 ]'
ok "apply installs one marker line" '[ "$(marker_count)" = 1 ]'
ok "installed line carries default schedule" 'grep -qF "*/17 * * * *" "$FAKE_CRON"'
ok "installed line loads login PATH via bash -lc" 'grep -qF "bash -lc" "$FAKE_CRON"'
ok "installed line invokes run mode" 'grep -qF "ccc-pr-status-poll.sh\" run" "$FAKE_CRON"'

# generation stamp (#1081): content hash of the installer, pinned at end of line
# shellcheck source=/dev/null
. "$ROOT/scripts/lib/installer-gen-stamp.sh"
want_gen="$(ccc_installer_gen_stamp "$INSTALLER")"
ok "installed line carries gen stamp" 'grep -qE "# ccc-node:pr-status-poll gen=h_[0-9a-f]{12}$" "$FAKE_CRON"'
ok "gen stamp matches installer content" 'grep -qF "gen=$want_gen" "$FAKE_CRON"'

# idempotent: re-apply keeps a single line
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "re-apply stays idempotent (one line)" '[ "$(marker_count)" = 1 ]'
ok "re-apply keeps the same gen stamp" 'grep -qF "gen=$want_gen" "$FAKE_CRON"'

# custom schedule replaces, still single line
bash "$INSTALLER" --apply --schedule "3 * * * *" >/dev/null 2>&1
ok "custom schedule still single line" '[ "$(marker_count)" = 1 ]'
ok "custom schedule applied" 'grep -qF "3 * * * *" "$FAKE_CRON"'
ok "old schedule removed" '! grep -qF "*/17 * * * *" "$FAKE_CRON"'

# install record (#1081 phase 2): replay material for self-update
REC="$CCC_CLAUDE_DIR/state/install-pr-status-poll-cron.json"
ok "apply writes an install record" '[ -f "$REC" ]'
ok "record carries schema/marker/gen" 'jq -e ".schema==\"ccc.install-record.v1\" and .marker==\"# ccc-node:pr-status-poll\" and .gen==\"$want_gen\"" "$REC" >/dev/null'
ok "record argv materializes the resolved schedule" 'jq -e ".argv == [\"--apply\",\"--schedule\",\"3 * * * *\"]" "$REC" >/dev/null'
ok "record is owner-only" '[ "$(stat -c %a "$REC")" = 600 ]'

# a pre-existing unrelated cron line is preserved
printf '0 4 * * * echo keepme\n' >> "$FAKE_CRON"
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "unrelated cron line preserved" 'grep -qF "echo keepme" "$FAKE_CRON"'
ok "still one marker line after preserve" '[ "$(marker_count)" = 1 ]'

# remove takes the marker line out, keeps the unrelated one
bash "$INSTALLER" --apply --remove >/dev/null 2>&1
ok "remove deletes marker line" '[ "$(marker_count)" = 0 ]'
ok "remove keeps unrelated line" 'grep -qF "echo keepme" "$FAKE_CRON"'
ok "remove drops the install record (no resurrection via re-apply)" '[ ! -f "$REC" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
