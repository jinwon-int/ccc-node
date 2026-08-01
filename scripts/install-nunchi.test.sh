#!/usr/bin/env bash
# Hermetic installer/rollback coverage for the Codex nunchi loader (#786).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

home="$TMP/home"
claude_dir="$home/.claude"
hooks="$claude_dir/hooks"
state="$claude_dir/state"
codex_home="$home/.codex"
nunchi_home="$home/.nunchi"
fake_bin="$TMP/bin"
mkdir -p "$hooks/nunchi" "$state" "$codex_home" "$nunchi_home" "$fake_bin"
cp "$ROOT/claude/hooks/nunchi/codex-loader.py" "$hooks/nunchi/codex-loader.py"
cp "$ROOT/claude/hooks/nunchi/nunchi.py" "$hooks/nunchi/nunchi.py"
cp "$ROOT/claude/hooks/scan-injection.sh" "$hooks/scan-injection.sh"
chmod 700 "$hooks/nunchi/codex-loader.py" "$hooks/nunchi/nunchi.py"
chmod 700 "$hooks/scan-injection.sh"

cat > "$hooks/load-memory.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"INSTALLER_BASE_SENTINEL"}}'
EOF
chmod 700 "$hooks/load-memory.sh"

cat > "$fake_bin/crontab" <<'EOF'
#!/usr/bin/env bash
store="${CCC_TEST_CRONTAB_STORE:?}"
if [ "${1:-}" = "-l" ]; then
  [ -f "$store" ] && cat "$store"
  exit 0
fi
if [ "${1:-}" = "-" ]; then
  cat > "$store.tmp"
  mv "$store.tmp" "$store"
else
  cp "$1" "$store"
fi
EOF
chmod 700 "$fake_bin/crontab"

common_env=(
  HOME="$home"
  CCC_CLAUDE_DIR="$claude_dir"
  CCC_STATE_DIR="$state"
  CODEX_HOME="$codex_home"
  NUNCHI_HOME="$nunchi_home"
  NUNCHI_DB="$nunchi_home/facts.db"
  NUNCHI_SNAPSHOT="$nunchi_home/snapshot.md"
  CCC_TEST_CRONTAB_STORE="$TMP/crontab"
  CCC_CRONTAB_CMD="$fake_bin/crontab"
)

out="$(env "${common_env[@]}" bash "$ROOT/scripts/install-nunchi.sh" --apply --codex 2>&1)"; rc=$?
ok "--apply --codex enables the owner-local mode marker" \
  '[ "$rc" = 0 ] && [ "$(cat "$state/nunchi.mode")" = on ] && [ "$(stat -c %a "$state/nunchi.mode")" = 600 ]'
ok "--apply --codex installs the Codex feed without bridge env wiring" \
  'grep -q "codex-feed.sh" "$TMP/crontab" && ! grep -q "CCC_CODEX_MEMORY_LOADER" <<<"$out"'

printf '%s' 'INSTALLER_NUNCHI_SENTINEL' > "$nunchi_home/snapshot.md"
chmod 600 "$nunchi_home/snapshot.md"
env "${common_env[@]}" python3 "$ROOT/scripts/ccc_codex_memory.py" materialize --json > "$TMP/materialize-on.json" 2> "$TMP/materialize-on.err"; rc=$?
ok "Codex materializer auto-selects installed nunchi loader after opt-in" \
  '[ "$rc" = 0 ] && grep -q "INSTALLER_BASE_SENTINEL" "$codex_home/AGENTS.md" && grep -q "INSTALLER_NUNCHI_SENTINEL" "$codex_home/AGENTS.md" && ! grep -q "INSTALLER_NUNCHI_SENTINEL" "$TMP/materialize-on.json" "$TMP/materialize-on.err"'

out="$(env "${common_env[@]}" bash "$ROOT/scripts/install-nunchi.sh" --remove 2>&1)"; rc=$?
env "${common_env[@]}" python3 "$ROOT/scripts/ccc_codex_memory.py" materialize --json > "$TMP/materialize-off.json" 2> "$TMP/materialize-off.err"; materialize_rc=$?
ok "--remove immediately rolls Codex selection back to canonical memory" \
  '[ "$rc" = 0 ] && [ "$materialize_rc" = 0 ] && [ "$(cat "$state/nunchi.mode")" = off ] && grep -q "INSTALLER_BASE_SENTINEL" "$codex_home/AGENTS.md" && ! grep -q "INSTALLER_NUNCHI_SENTINEL" "$codex_home/AGENTS.md" && ! grep -q "CCC_CODEX_MEMORY_LOADER" <<<"$out"'
ok "--remove strips only managed nunchi cron entries" \
  '! grep -q "nunchi" "$TMP/crontab"'

rm -f "$hooks/nunchi/codex-loader.py"
out="$(env "${common_env[@]}" bash "$ROOT/scripts/install-nunchi.sh" --apply --codex 2>&1)"; rc=$?
ok "Codex opt-in refuses a missing managed loader before enabling mode" \
  '[ "$rc" = 2 ] && [ "$(cat "$state/nunchi.mode")" = off ] && grep -q "loader missing or unsafe" <<<"$out"'

printf '%s\n' 'KEEP_EXISTING_CRON' > "$TMP/crontab"
cp "$ROOT/claude/hooks/nunchi/codex-loader.py" "$hooks/nunchi/codex-loader.py"
chmod 722 "$hooks/nunchi/codex-loader.py"
out="$(env "${common_env[@]}" bash "$ROOT/scripts/install-nunchi.sh" --apply --codex 2>&1)"; rc=$?
ok "Codex opt-in rejects a writable loader before state or cron mutation" \
  '[ "$rc" = 2 ] && [ "$(cat "$state/nunchi.mode")" = off ] && [ "$(cat "$TMP/crontab")" = KEEP_EXISTING_CRON ]'

rm -f "$hooks/nunchi/codex-loader.py"
cp "$ROOT/claude/hooks/nunchi/codex-loader.py" "$hooks/nunchi/loader-source.py"
ln "$hooks/nunchi/loader-source.py" "$hooks/nunchi/codex-loader.py"
out="$(env "${common_env[@]}" bash "$ROOT/scripts/install-nunchi.sh" --apply --codex 2>&1)"; rc=$?
ok "Codex opt-in rejects a hardlinked loader before state or cron mutation" \
  '[ "$rc" = 2 ] && [ "$(cat "$state/nunchi.mode")" = off ] && [ "$(cat "$TMP/crontab")" = KEEP_EXISTING_CRON ]'

rm -f "$hooks/nunchi/codex-loader.py" "$hooks/nunchi/loader-source.py"
: > "$hooks/nunchi/codex-loader.py"
chmod 700 "$hooks/nunchi/codex-loader.py"
out="$(env "${common_env[@]}" bash "$ROOT/scripts/install-nunchi.sh" --apply --codex 2>&1)"; rc=$?
ok "Codex opt-in rejects an empty loader before state or cron mutation" \
  '[ "$rc" = 2 ] && [ "$(cat "$state/nunchi.mode")" = off ] && [ "$(cat "$TMP/crontab")" = KEEP_EXISTING_CRON ]'

python3 - "$hooks/nunchi/codex-loader.py" <<'PY'
from pathlib import Path
import sys
Path(sys.argv[1]).write_bytes(b"x" * (1024 * 1024 + 1))
PY
chmod 700 "$hooks/nunchi/codex-loader.py"
out="$(env "${common_env[@]}" bash "$ROOT/scripts/install-nunchi.sh" --apply --codex 2>&1)"; rc=$?
ok "Codex opt-in rejects an oversized loader before state or cron mutation" \
  '[ "$rc" = 2 ] && [ "$(cat "$state/nunchi.mode")" = off ] && [ "$(cat "$TMP/crontab")" = KEEP_EXISTING_CRON ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
