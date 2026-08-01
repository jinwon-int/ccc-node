#!/usr/bin/env bash
# Hermetic installer tests: provider routing, cron idempotence and legacy-hook cleanup.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

home="$TMP/home"; claude="$home/.claude"; state="$claude/state"; hooks="$claude/hooks/nunchi"
fakebin="$TMP/bin"; cron_state="$TMP/crontab"
mkdir -p "$hooks" "$state" "$home/.codex/sessions" "$home/.claude/projects" \
  "$home/.local/bin" "$fakebin"
cp "$ROOT"/claude/hooks/nunchi/{nunchi.py,codex-feed.sh,ingest-cron.sh,bench.sh,bench-qset.tsv,sessionstart.sh} "$hooks/"
chmod +x "$hooks"/*.sh "$home/.local/bin"
write_exec_stub "$home/.local/bin/mempalace" <<'SH'
exit 0
SH
cat > "$claude/settings.local.json" <<JSON
{"hooks":{"SessionStart":[
  {"hooks":[{"type":"command","command":"bash /root/nunchi/sessionstart.sh"}]},
  {"hooks":[{"type":"command","command":"bash $hooks/sessionstart.sh"}]},
  {"hooks":[{"type":"command","command":"bash $claude/hooks/load-memory.sh SessionStart"}]}
]}}
JSON
write_exec_stub "$fakebin/crontab" <<'SH'
case "${1:-}" in
  -l) [ -f "${CCC_TEST_CRONTAB:?}" ] && cat "$CCC_TEST_CRONTAB"; exit $? ;;
  ''|-) cat > "${CCC_TEST_CRONTAB:?}" ;;
  *) [ -f "$1" ] || exit 2; cp "$1" "${CCC_TEST_CRONTAB:?}" ;;
esac
SH

run_install() {
  HOME="$home" CCC_CLAUDE_DIR="$claude" CCC_STATE_DIR="$state" \
    CCC_TEST_CRONTAB="$cron_state" PATH="$fakebin:/usr/bin:/bin" \
    bash "$ROOT/scripts/install-nunchi.sh" "$@"
}

out="$(CCC_NUNCHI_PROVIDER=codex run_install --apply 2>&1)"; rc=$?
ok "Codex apply succeeds" '[ "$rc" = 0 ] && grep -q "provider=codex" <<<"$out"'
ok "Codex apply writes one feed, sweep and bench cron" \
  '[ "$(grep -c "nunchi:#816" "$cron_state")" = 3 ] && grep -q "codex-feed.sh" "$cron_state" && grep -q "$home/.codex/sessions" "$cron_state"'
ok "apply enables mode and initializes DB" '[ "$(cat "$state/nunchi.mode")" = on ] && [ -s "$home/.nunchi/facts.db" ]'
ok "apply removes canonical and retired standalone hooks only" \
  '! grep -q "nunchi/sessionstart.sh" "$claude/settings.local.json" && grep -q "load-memory.sh" "$claude/settings.local.json"'

out="$(CCC_NUNCHI_PROVIDER=codex run_install --apply 2>&1)"; rc=$?
ok "reapply is cron-idempotent" '[ "$rc" = 0 ] && [ "$(grep -c "nunchi:#816" "$cron_state")" = 3 ]'

out="$(CCC_NUNCHI_PROVIDER=claude run_install --apply 2>&1)"; rc=$?
ok "provider change rewires feed and sweep atomically" \
  '[ "$rc" = 0 ] && grep -q "ingest-cron.sh" "$cron_state" && grep -q "$home/.claude/projects" "$cron_state" && ! grep -q "codex-feed.sh" "$cron_state"'

out="$(run_install --remove 2>&1)"; rc=$?
ok "remove keeps DB but disables mode and cron" \
  '[ "$rc" = 0 ] && [ "$(cat "$state/nunchi.mode")" = off ] && [ -s "$home/.nunchi/facts.db" ] && [ ! -s "$cron_state" ]'
ok "target-user mode has an explicit fail-closed interface" \
  'grep -q -- "--target-user requires root" "$ROOT/scripts/install-nunchi.sh" && grep -q "safe target home not found" "$ROOT/scripts/install-nunchi.sh"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
