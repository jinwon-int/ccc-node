#!/usr/bin/env bash
# Hermetic installer coverage: provider routing, managed Codex loader safety,
# cron idempotence, Claude hook ownership, rollback and target-user isolation.
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
cron_store="$TMP/crontab"
mkdir -p "$hooks/nunchi" "$state" "$codex_home/sessions" \
  "$home/.claude/projects" "$home/.local/bin" "$nunchi_home" "$fake_bin"
cp "$ROOT"/claude/hooks/nunchi/{codex-loader.py,nunchi.py,codex-feed.sh,ingest-cron.sh,bench.sh,bench-qset.tsv,sessionstart.sh} "$hooks/nunchi/"
cp "$ROOT/claude/hooks/scan-injection.sh" "$hooks/scan-injection.sh"
chmod 700 "$hooks/nunchi/codex-loader.py" "$hooks/nunchi/nunchi.py" "$hooks/scan-injection.sh"
chmod 755 "$hooks/nunchi"/*.sh

cat > "$hooks/load-memory.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"INSTALLER_BASE_SENTINEL"}}'
EOF
chmod 700 "$hooks/load-memory.sh"

cat > "$claude_dir/settings.local.json" <<JSON
{"hooks":{"SessionStart":[
  {"hooks":[{"type":"command","command":"bash /root/nunchi/sessionstart.sh"}]},
  {"hooks":[{"type":"command","command":"bash $hooks/nunchi/sessionstart.sh"}]},
  {"hooks":[{"type":"command","command":"bash $hooks/load-memory.sh"}]}
]}}
JSON

write_exec_stub "$home/.local/bin/mempalace" <<'SH'
exit 0
SH
write_exec_stub "$fake_bin/crontab" <<'SH'
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
SH

common_env=(
  HOME="$home"
  CCC_CLAUDE_DIR="$claude_dir"
  CCC_STATE_DIR="$state"
  CODEX_HOME="$codex_home"
  NUNCHI_HOME="$nunchi_home"
  NUNCHI_DB="$nunchi_home/facts.db"
  NUNCHI_SNAPSHOT="$nunchi_home/snapshot.md"
  CCC_TEST_CRONTAB_STORE="$cron_store"
  CCC_CRONTAB_CMD="$fake_bin/crontab"
)

run_install() {
  env "${common_env[@]}" bash "$ROOT/scripts/install-nunchi.sh" "$@"
}

out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "--apply --codex enables an owner-only mode marker" \
  '[ "$rc" = 0 ] && [ "$(cat "$state/nunchi.mode")" = on ] && [ "$(stat -c %a "$state/nunchi.mode")" = 600 ]'
ok "Codex apply writes one feed, sweep and bench cron" \
  '[ "$(grep -c "nunchi:#816" "$cron_store")" = 3 ] && grep -q "codex-feed.sh" "$cron_store" && grep -q "$codex_home/sessions" "$cron_store"'
ok "Codex apply removes standalone nunchi hooks but preserves the canonical loader" \
  '! grep -q "nunchi/sessionstart.sh" "$claude_dir/settings.local.json" && grep -q "load-memory.sh" "$claude_dir/settings.local.json"'

printf '%s' 'INSTALLER_NUNCHI_SENTINEL' > "$nunchi_home/snapshot.md"
chmod 600 "$nunchi_home/snapshot.md"
env "${common_env[@]}" python3 "$ROOT/scripts/ccc_codex_memory.py" materialize --json \
  > "$TMP/materialize-on.json" 2> "$TMP/materialize-on.err"; rc=$?
ok "Codex materializer auto-selects the installed managed nunchi loader" \
  '[ "$rc" = 0 ] && grep -q "INSTALLER_BASE_SENTINEL" "$codex_home/AGENTS.md" && grep -q "INSTALLER_NUNCHI_SENTINEL" "$codex_home/AGENTS.md" && ! grep -q "INSTALLER_NUNCHI_SENTINEL" "$TMP/materialize-on.json" "$TMP/materialize-on.err"'

out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "Codex reapply is cron-idempotent" \
  '[ "$rc" = 0 ] && [ "$(grep -c "nunchi:#816" "$cron_store")" = 3 ]'

out="$(run_install --apply --claude 2>&1)"; rc=$?
ok "provider change atomically rewires feed and sweep to Claude" \
  '[ "$rc" = 0 ] && grep -q "ingest-cron.sh" "$cron_store" && grep -q "$home/.claude/projects" "$cron_store" && ! grep -q "codex-feed.sh" "$cron_store"'
ok "Claude apply owns exactly one standalone nunchi hook" \
  '[ "$(grep -c "$hooks/nunchi/sessionstart.sh" "$claude_dir/settings.local.json")" = 1 ] && grep -q "load-memory.sh" "$claude_dir/settings.local.json"'

rm -rf "$home/.claude/projects"
out="$(run_install --apply 2>&1)"; rc=$?
ok "auto provider fallback recognizes a Codex-only transcript tree" \
  '[ "$rc" = 0 ] && grep -q "provider=codex" <<<"$out" && grep -q "codex-feed.sh" "$cron_store" && ! grep -q "nunchi/sessionstart.sh" "$claude_dir/settings.local.json"'
mkdir -p "$home/.claude/projects"

out="$(run_install --remove 2>&1)"; rc=$?
env "${common_env[@]}" python3 "$ROOT/scripts/ccc_codex_memory.py" materialize --json \
  > "$TMP/materialize-off.json" 2> "$TMP/materialize-off.err"; materialize_rc=$?
ok "--remove immediately rolls Codex back to canonical memory" \
  '[ "$rc" = 0 ] && [ "$materialize_rc" = 0 ] && [ "$(cat "$state/nunchi.mode")" = off ] && grep -q "INSTALLER_BASE_SENTINEL" "$codex_home/AGENTS.md" && ! grep -q "INSTALLER_NUNCHI_SENTINEL" "$codex_home/AGENTS.md"'
ok "--remove strips managed cron and standalone hook state while retaining the DB" \
  '[ ! -s "$cron_store" ] && ! grep -q "nunchi/sessionstart.sh" "$claude_dir/settings.local.json" && [ -s "$nunchi_home/facts.db" ]'

rm -f "$hooks/nunchi/codex-loader.py"
out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "Codex opt-in refuses a missing managed loader before enabling mode" \
  '[ "$rc" = 2 ] && [ "$(cat "$state/nunchi.mode")" = off ] && grep -q "loader missing or unsafe" <<<"$out"'

printf '%s\n' 'KEEP_EXISTING_CRON' > "$cron_store"
cp "$ROOT/claude/hooks/nunchi/codex-loader.py" "$hooks/nunchi/codex-loader.py"
chmod 722 "$hooks/nunchi/codex-loader.py"
out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "Codex opt-in rejects a writable loader before state or cron mutation" \
  '[ "$rc" = 2 ] && [ "$(cat "$state/nunchi.mode")" = off ] && [ "$(cat "$cron_store")" = KEEP_EXISTING_CRON ]'

rm -f "$hooks/nunchi/codex-loader.py"
cp "$ROOT/claude/hooks/nunchi/codex-loader.py" "$hooks/nunchi/loader-source.py"
ln "$hooks/nunchi/loader-source.py" "$hooks/nunchi/codex-loader.py"
out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "Codex opt-in rejects a hardlinked loader before state or cron mutation" \
  '[ "$rc" = 2 ] && [ "$(cat "$state/nunchi.mode")" = off ] && [ "$(cat "$cron_store")" = KEEP_EXISTING_CRON ]'

rm -f "$hooks/nunchi/codex-loader.py" "$hooks/nunchi/loader-source.py"
: > "$hooks/nunchi/codex-loader.py"
chmod 700 "$hooks/nunchi/codex-loader.py"
out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "Codex opt-in rejects an empty loader before state or cron mutation" \
  '[ "$rc" = 2 ] && [ "$(cat "$state/nunchi.mode")" = off ] && [ "$(cat "$cron_store")" = KEEP_EXISTING_CRON ]'

python3 - "$hooks/nunchi/codex-loader.py" <<'PY'
from pathlib import Path
import sys
Path(sys.argv[1]).write_bytes(b"x" * (1024 * 1024 + 1))
PY
chmod 700 "$hooks/nunchi/codex-loader.py"
out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "Codex opt-in rejects an oversized loader before state or cron mutation" \
  '[ "$rc" = 2 ] && [ "$(cat "$state/nunchi.mode")" = off ] && [ "$(cat "$cron_store")" = KEEP_EXISTING_CRON ]'

target_home="$TMP/target-home"
mkdir -p "$target_home"
write_exec_stub "$fake_bin/id" <<'SH'
case "${1:-}" in -un) echo root ;; -u) echo 0 ;; *) exit 2 ;; esac
SH
write_exec_stub "$fake_bin/getent" <<SH
[ "\${1:-}" = passwd ] && [ "\${2:-}" = worker ] || exit 2
echo 'worker:x:0:0::${target_home}:/bin/bash'
SH
write_exec_stub "$fake_bin/runuser" <<'SH'
printf '%s\n' "$@" > "${CCC_TEST_RUNUSER_CAPTURE:?}"
SH
capture="$TMP/runuser.args"
out="$(HOME="$home" PATH="$fake_bin:/usr/bin:/bin" CCC_TEST_RUNUSER_CAPTURE="$capture" \
  GH_TOKEN=DO_NOT_FORWARD bash "$ROOT/scripts/install-nunchi.sh" --apply --target-user worker --codex 2>&1)"; rc=$?
ok "target-user re-exec uses a minimal environment and never forwards ambient credentials" \
  '[ "$rc" = 0 ] && grep -qx -- "-i" "$capture" && grep -q "HOME=$target_home" "$capture" && ! grep -q "DO_NOT_FORWARD\|GH_TOKEN" "$capture"'

out="$(HOME="$home" PATH="$fake_bin:/usr/bin:/bin" bash "$ROOT/scripts/install-nunchi.sh" --apply --target-user bad/user 2>&1)"; rc=$?
ok "target-user rejects unsafe account names before re-exec" \
  '[ "$rc" = 2 ] && grep -q "invalid target user" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
