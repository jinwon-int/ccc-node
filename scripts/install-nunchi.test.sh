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
cp "$ROOT"/claude/hooks/nunchi/{codex-loader.py,nunchi.py,codex-feed.sh,ingest-cron.sh,bench.sh,bench-qset.tsv,sessionstart.sh,mempalace-refresh.sh} "$hooks/nunchi/"
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
[ -z "${CCC_TEST_MEMPALACE_CAPTURE:-}" ] || printf '%s\n' "$*" > "$CCC_TEST_MEMPALACE_CAPTURE"
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
ok "Codex apply writes one feed, managed refresh and bench cron" \
  '[ "$(grep -c "nunchi:#816" "$cron_store")" = 3 ] && grep -q "codex-feed.sh" "$cron_store" && grep -q "mempalace-refresh.sh codex $codex_home/sessions" "$cron_store"'
ok "Codex apply removes standalone nunchi hooks but preserves the canonical loader" \
  '! grep -q "nunchi/sessionstart.sh" "$claude_dir/settings.local.json" && grep -q "load-memory.sh" "$claude_dir/settings.local.json"'
refresh_capture="$TMP/codex-refresh.args"
CCC_TEST_MEMPALACE_CAPTURE="$refresh_capture" HOME="$home" \
  PATH="/usr/bin:/bin" CCC_STATE_DIR="$state" NUNCHI_HOME="$nunchi_home" \
  CCC_NUNCHI_MEMPALACE_CLI="$home/.local/bin/mempalace" \
  bash "$hooks/nunchi/mempalace-refresh.sh" codex "$codex_home/sessions" >/dev/null 2>&1; rc=$?
ok "Codex refresh uses the native incremental conversation miner" \
  '[ "$rc" = 0 ] && grep -qx "mine $codex_home/sessions --mode convos" "$refresh_capture" && jq -e '\'' .provider == "codex" and .state == "ok" and .exit_code == 0 '\'' "$nunchi_home/mempalace-refresh.status.json" >/dev/null'

edge_status="$TMP/edge-refresh.status.json"
timeout_capture="$TMP/timeout.args"
write_exec_stub "$fake_bin/timeout" <<'SH'
printf '%s\n' "$*" > "${CCC_TEST_TIMEOUT_CAPTURE:?}"
exit "${CCC_TEST_TIMEOUT_RC:-0}"
SH
run_edge_refresh() {
  env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" CCC_STATE_DIR="$state" \
    NUNCHI_HOME="$nunchi_home" CCC_NUNCHI_MEMPALACE_STATUS="$edge_status" \
    CCC_NUNCHI_MEMPALACE_CLI="$home/.local/bin/mempalace" \
    CCC_TEST_TIMEOUT_CAPTURE="$timeout_capture" "$@" \
    bash "$hooks/nunchi/mempalace-refresh.sh" codex "$codex_home/sessions"
}
run_edge_refresh CCC_NUNCHI_MEMPALACE_REFRESH_TIMEOUT_SEC=0 >/dev/null 2>&1; rc_zero=$?
zero_args="$(cat "$timeout_capture")"
run_edge_refresh CCC_NUNCHI_MEMPALACE_REFRESH_TIMEOUT_SEC=99999 >/dev/null 2>&1; rc_large=$?
large_args="$(cat "$timeout_capture")"
ok "zero and oversized refresh timeouts cannot disable the 3300-second bound" \
  '[ "$rc_zero" = 0 ] && [ "$rc_large" = 0 ] && [[ "$zero_args" == "-k 30s 3300 "* ]] && [[ "$large_args" == "-k 30s 3300 "* ]]'

run_edge_refresh CCC_TEST_TIMEOUT_RC=124 >/dev/null 2>&1; rc=$?
ok "timeout exit 124 is recorded atomically without a body" \
  '[ "$rc" = 124 ] && [ "$(stat -c %a "$edge_status")" = 600 ] && jq -e '\'' .state == "error" and .exit_code == 124 and keys == ["exit_code","finished_at","provider","schema","started_at","state"] '\'' "$edge_status" >/dev/null'
run_edge_refresh CCC_TEST_TIMEOUT_RC=137 >/dev/null 2>&1; rc=$?
ok "timeout kill exit 137 is recorded without sleeping" \
  '[ "$rc" = 137 ] && jq -e '\'' .state == "error" and .exit_code == 137 '\'' "$edge_status" >/dev/null'

write_exec_stub "$fake_bin/flock" <<'SH'
exit "${CCC_TEST_FLOCK_RC:-0}"
SH
printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"running","exit_code":-1,"started_at":10,"finished_at":0}' > "$edge_status"
run_edge_refresh CCC_TEST_FLOCK_RC=75 CCC_NUNCHI_MEMPALACE_CLI="$TMP/missing-mempalace" >/dev/null 2>&1; rc=$?
ok "real lock contention is a no-op before provider preflight" \
  '[ "$rc" = 0 ] && jq -e '\'' .state == "running" and .started_at == 10 '\'' "$edge_status" >/dev/null'
run_edge_refresh CCC_TEST_FLOCK_RC=64 >/dev/null 2>&1; rc=$?
ok "flock errors cannot overwrite the active lock owner's state" \
  '[ "$rc" = 2 ] && jq -e '\'' .state == "running" and .started_at == 10 '\'' "$edge_status" >/dev/null'
rm -f "$fake_bin/flock"

noflock_bin="$TMP/no-flock-bin"
mkdir -p "$noflock_bin"
for tool in cat date mkdir python3 timeout; do
  ln -s "$(command -v "$tool")" "$noflock_bin/$tool"
done
printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":1,"finished_at":2}' > "$edge_status"
HOME="$home" PATH="$noflock_bin" CCC_STATE_DIR="$state" NUNCHI_HOME="$nunchi_home" \
  CCC_NUNCHI_MEMPALACE_STATUS="$edge_status" \
  CCC_NUNCHI_MEMPALACE_CLI="$home/.local/bin/mempalace" \
  /bin/bash "$hooks/nunchi/mempalace-refresh.sh" codex "$codex_home/sessions" >/dev/null 2>&1; rc=$?
ok "missing flock fails closed without writing unlocked status" \
  '[ "$rc" = 2 ] && jq -e '\'' .state == "ok" and .started_at == 1 and .finished_at == 2 '\'' "$edge_status" >/dev/null'

printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":1,"finished_at":2}' > "$edge_status"
run_edge_refresh CCC_NUNCHI_MEMPALACE_CLI="$TMP/missing-mempalace" >/dev/null 2>&1; rc=$?
ok "missing CLI after mode-on replaces a stale success" \
  '[ "$rc" = 2 ] && jq -e '\'' .state == "error" and .exit_code == 2 '\'' "$edge_status" >/dev/null'
printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":1,"finished_at":2}' > "$edge_status"
env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" CCC_STATE_DIR="$state" \
  NUNCHI_HOME="$nunchi_home" CCC_NUNCHI_MEMPALACE_STATUS="$edge_status" \
  CCC_NUNCHI_MEMPALACE_CLI="$home/.local/bin/mempalace" \
  bash "$hooks/nunchi/mempalace-refresh.sh" codex "$TMP/missing-target" >/dev/null 2>&1; rc=$?
ok "missing target after mode-on replaces a stale success" \
  '[ "$rc" = 2 ] && jq -e '\'' .state == "error" and .exit_code == 2 '\'' "$edge_status" >/dev/null'

printf '%s' 'INSTALLER_NUNCHI_SENTINEL' > "$nunchi_home/snapshot.md"
chmod 600 "$nunchi_home/snapshot.md"
env "${common_env[@]}" python3 "$ROOT/scripts/ccc_codex_memory.py" materialize --json \
  > "$TMP/materialize-on.json" 2> "$TMP/materialize-on.err"; rc=$?
ok "Codex materializer auto-selects the installed managed nunchi loader" \
  '[ "$rc" = 0 ] && grep -q "INSTALLER_BASE_SENTINEL" "$codex_home/AGENTS.md" && grep -q "INSTALLER_NUNCHI_SENTINEL" "$codex_home/AGENTS.md" && ! grep -q "INSTALLER_NUNCHI_SENTINEL" "$TMP/materialize-on.json" "$TMP/materialize-on.err"'

unrelated_sweep='43 4 * * * /opt/operator/mempalace sweep /srv/operator-archive'
printf '%s\n' "$unrelated_sweep" >> "$cron_store"
out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "Codex reapply is cron-idempotent" \
  '[ "$rc" = 0 ] && [ "$(grep -c "nunchi:#816" "$cron_store")" = 3 ]'
ok "managed cron rewrites preserve unrelated operator MemPalace jobs" \
  'grep -qxF "$unrelated_sweep" "$cron_store"'

termux_root="$TMP/data/data/com.termux/files/home/space 'quote %;false"
weird_state="$termux_root/state dir"
weird_nunchi="$termux_root/nunchi ' %; dir"
weird_status="$termux_root/status ' %; file.json"
weird_sweep="$termux_root/codex sessions ' %;"
weird_mp_dir="$termux_root/bin ' %;"
weird_mp="$weird_mp_dir/mempalace"
mkdir -p "$weird_state" "$weird_nunchi" "$weird_sweep" "$weird_mp_dir"
cp "$home/.local/bin/mempalace" "$weird_mp"
chmod 755 "$weird_mp"
weird_capture="$TMP/weird-cron.args"
out="$(env "${common_env[@]}" PATH="$weird_mp_dir:/usr/bin:/bin" \
  CCC_STATE_DIR="$weird_state" NUNCHI_HOME="$weird_nunchi" \
  NUNCHI_DB="$weird_nunchi/facts.db" NUNCHI_SNAPSHOT="$weird_nunchi/snapshot.md" \
  CCC_NUNCHI_MEMPALACE_STATUS="$weird_status" NUNCHI_SWEEP_DIR="$weird_sweep" \
  bash "$ROOT/scripts/install-nunchi.sh" --apply --codex 2>&1)"; rc=$?
refresh_line="$(grep 'mempalace-refresh.sh' "$cron_store")"
feed_line="$(grep 'codex-feed.sh' "$cron_store")"
bench_line="$(grep 'bench.sh' "$cron_store")"
runtime_cmd="$(cut -d ' ' -f 6- <<<"$refresh_line")"
runtime_cmd="${runtime_cmd% \# nunchi:#816}"
# crond removes the escape that protects each literal percent before /bin/sh.
runtime_cmd="${runtime_cmd//\\%/%}"
env -i HOME="$home" PATH="/usr/bin:/bin" CCC_TEST_MEMPALACE_CAPTURE="$weird_capture" \
  /bin/sh -c "$runtime_cmd" >/dev/null 2>&1; cron_rc=$?
ok "generated refresh cron preserves restricted-PATH custom and Termux-style paths" \
  '[ "$rc" = 0 ] && [ "$cron_rc" = 0 ] && grep -q "CCC_NUNCHI_MEMPALACE_CLI=" <<<"$refresh_line" && grep -qx "mine $weird_sweep --mode convos" "$weird_capture" && jq -e '\'' .provider == "codex" and .state == "ok" '\'' "$weird_status" >/dev/null && [ "$(stat -c %a "$weird_status")" = 600 ]'
ok "generated cron protects quotes, percent and semicolon from splitting or injection" \
  'grep -q '\''\\%'\'' <<<"$refresh_line" && [ "$(grep -c "mempalace-refresh.sh" "$cron_store")" = 1 ]'
ok "generated feed and bench cron retain the installed state and nunchi paths" \
  'for line in "$feed_line" "$bench_line"; do grep -q "CCC_STATE_DIR=" <<<"$line" && grep -q "NUNCHI_HOME=" <<<"$line" && grep -q "NUNCHI_DB=" <<<"$line" && grep -q "NUNCHI_SNAPSHOT=" <<<"$line" || exit 1; done'

# Restore the ordinary fixture before provider-switch assertions.
out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "ordinary paths remain idempotent after custom-path installation" \
  '[ "$rc" = 0 ] && [ "$(grep -c "nunchi:#816" "$cron_store")" = 3 ]'

out="$(run_install --apply --claude 2>&1)"; rc=$?
ok "provider change atomically rewires feed and sweep to Claude" \
  '[ "$rc" = 0 ] && grep -q "ingest-cron.sh" "$cron_store" && grep -q "mempalace-refresh.sh claude $home/.claude/projects" "$cron_store" && ! grep -q "codex-feed.sh" "$cron_store"'
ok "Claude apply owns exactly one standalone nunchi hook" \
  '[ "$(grep -c "$hooks/nunchi/sessionstart.sh" "$claude_dir/settings.local.json")" = 1 ] && grep -q "load-memory.sh" "$claude_dir/settings.local.json"'
refresh_capture="$TMP/claude-refresh.args"
CCC_TEST_MEMPALACE_CAPTURE="$refresh_capture" HOME="$home" \
  PATH="/usr/bin:/bin" CCC_STATE_DIR="$state" NUNCHI_HOME="$nunchi_home" \
  CCC_NUNCHI_MEMPALACE_CLI="$home/.local/bin/mempalace" \
  bash "$hooks/nunchi/mempalace-refresh.sh" claude "$home/.claude/projects" >/dev/null 2>&1; rc=$?
ok "Claude refresh retains message-granular sweep" \
  '[ "$rc" = 0 ] && grep -qx "sweep $home/.claude/projects" "$refresh_capture" && jq -e '\'' .provider == "claude" and .state == "ok" and .exit_code == 0 '\'' "$nunchi_home/mempalace-refresh.status.json" >/dev/null'

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
  '[ "$(grep -c "nunchi:#816" "$cron_store" || true)" = 0 ] && grep -qxF "$unrelated_sweep" "$cron_store" && ! grep -q "nunchi/sessionstart.sh" "$claude_dir/settings.local.json" && [ -s "$nunchi_home/facts.db" ]'

cron_before_dependency_failure="$(cat "$cron_store")"
out="$(env "${common_env[@]}" CCC_NUNCHI_TIMEOUT_CLI="$TMP/missing-timeout" \
  bash "$ROOT/scripts/install-nunchi.sh" --apply --codex 2>&1)"; rc=$?
ok "refresh dependency failure leaves mode and existing cron untouched" \
  '[ "$rc" = 2 ] && [ "$(cat "$state/nunchi.mode")" = off ] && [ "$(cat "$cron_store")" = "$cron_before_dependency_failure" ]'

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
target_uid="$(stat -c %u "$target_home")"
write_exec_stub "$fake_bin/id" <<'SH'
case "${1:-}" in -un) echo root ;; -u) echo 0 ;; *) exit 2 ;; esac
SH
write_exec_stub "$fake_bin/getent" <<SH
[ "\${1:-}" = passwd ] && [ "\${2:-}" = worker ] || exit 2
echo 'worker:x:${target_uid}:0::${target_home}:/bin/bash'
SH
write_exec_stub "$fake_bin/runuser" <<'SH'
printf '%s\n' "$@" > "${CCC_TEST_RUNUSER_CAPTURE:?}"
SH
capture="$TMP/runuser.args"
target_mp="$TMP/target-tools/mempalace"
target_timeout="$TMP/target-tools/timeout"
target_flock="$TMP/target-tools/flock"
out="$(HOME="$home" PATH="$fake_bin:/usr/bin:/bin" CCC_TEST_RUNUSER_CAPTURE="$capture" \
  CCC_NUNCHI_MEMPALACE_CLI="$target_mp" CCC_NUNCHI_TIMEOUT_CLI="$target_timeout" \
  CCC_NUNCHI_FLOCK_CLI="$target_flock" GH_TOKEN=DO_NOT_FORWARD \
  bash "$ROOT/scripts/install-nunchi.sh" --apply --target-user worker --codex 2>&1)"; rc=$?
ok "target-user re-exec uses a minimal environment and never forwards ambient credentials" \
  '[ "$rc" = 0 ] && grep -qx -- "-i" "$capture" && grep -q "HOME=$target_home" "$capture" && ! grep -q "DO_NOT_FORWARD\|GH_TOKEN" "$capture"'
ok "target-user re-exec preserves explicit refresh tool paths" \
  'grep -qx "CCC_NUNCHI_MEMPALACE_CLI=$target_mp" "$capture" && grep -qx "CCC_NUNCHI_TIMEOUT_CLI=$target_timeout" "$capture" && grep -qx "CCC_NUNCHI_FLOCK_CLI=$target_flock" "$capture"'

out="$(HOME="$home" PATH="$fake_bin:/usr/bin:/bin" bash "$ROOT/scripts/install-nunchi.sh" --apply --target-user bad/user 2>&1)"; rc=$?
ok "target-user rejects unsafe account names before re-exec" \
  '[ "$rc" = 2 ] && grep -q "invalid target user" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
