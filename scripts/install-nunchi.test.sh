#!/usr/bin/env bash
# Hermetic installer coverage: provider routing, managed Codex loader safety,
# cron idempotence, Claude hook ownership, rollback and target-user isolation.
set -uo pipefail
# Managed loader fixtures are owner-only by contract; do not inherit a 0002
# operator umask and accidentally create executable fixtures as 0775.
umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Inherited installer env aborts this suite outright on a live node -- it never
# reaches its summary, so all 60 assertions are lost (#1023).
ccc_test_reset_hook_env

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
piri_sessions="$home/.piri/agent/sessions"
mkdir -p "$hooks/nunchi" "$state" "$codex_home/sessions" "$piri_sessions" \
  "$home/.claude/projects" "$home/.local/bin" "$nunchi_home" "$fake_bin"
cp "$ROOT"/claude/hooks/nunchi/{codex-loader.py,nunchi.py,judge-batch.py,codex-feed.sh,piri-feed.sh,ingest-cron.sh,bench.sh,bench-qset.tsv,sessionstart.sh,mempalace-refresh.sh} "$hooks/nunchi/"
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

# --- generation stamp (#1081): every managed line carries the content hash of
# install-nunchi.sh, so ccc-doctor can detect entries frozen at an older
# installer. Appended after the marker; strip_cron matches by substring.
# shellcheck source=/dev/null
. "$ROOT/scripts/lib/installer-gen-stamp.sh"
want_gen="$(ccc_installer_gen_stamp "$ROOT/scripts/install-nunchi.sh")"
ok "all three managed cron lines carry the gen stamp" \
  '[ "$(grep -cE "nunchi:#816 gen=h_[0-9a-f]{12}$" "$cron_store")" = 3 ]'
ok "gen stamp matches installer content" \
  '[ "$(grep -cF "gen=$want_gen" "$cron_store")" = 3 ]'

# --- install record (#1081 phase 2): self-update replay material. The record
# must materialize the RESOLVED provider, not the ambient env.
nrec="$state/install-nunchi.json"
ok "apply writes an install record with resolved provider argv" \
  'jq -e ".schema==\"ccc.install-record.v1\" and .marker==\"# nunchi:#816\" and .gen==\"$want_gen\" and .argv==[\"--apply\",\"--codex\"]" "$nrec" >/dev/null'
ok "install record is owner-only" '[ "$(stat -c %a "$nrec")" = 600 ]'
refresh_capture="$TMP/codex-refresh.args"
CCC_TEST_MEMPALACE_CAPTURE="$refresh_capture" HOME="$home" \
  PATH="/usr/bin:/bin" CCC_STATE_DIR="$state" NUNCHI_HOME="$nunchi_home" \
  CCC_NUNCHI_MEMPALACE_CLI="$home/.local/bin/mempalace" \
  bash "$hooks/nunchi/mempalace-refresh.sh" codex "$codex_home/sessions" >/dev/null 2>&1; rc=$?
ok "Codex refresh uses the native incremental conversation miner with --wing codex" \
  '[ "$rc" = 0 ] && grep -qx "mine $codex_home/sessions --mode convos --wing codex" "$refresh_capture" && jq -e '\'' .provider == "codex" and .state == "ok" and .exit_code == 0 '\'' "$nunchi_home/mempalace-refresh.status.json" >/dev/null'

# --- Judge batch cron (#1204): opt-in via --judge, a 4th managed line that is
# dry-run by design (no NUNCHI_JUDGE_APPLY — flipping to apply is a fresh,
# per-node approval, never an installer default).
out="$(run_install --apply --codex --judge 2>&1)"; rc=$?
ok "--judge adds a managed daily judge-batch cron alongside feed/refresh/bench" \
  '[ "$rc" = 0 ] && [ "$(grep -c "nunchi:#816" "$cron_store")" = 4 ] && grep -q "judge-batch.py" "$cron_store"'
ok "judge cron is dry-run only (no APPLY env) and keeps the gen stamp" \
  '! grep "judge-batch.py" "$cron_store" | grep -q "NUNCHI_JUDGE_APPLY" && grep "judge-batch.py" "$cron_store" | grep -qE "gen=h_[0-9a-f]{12}$"'
# The install record is what self-update replays. If --judge is not
# materialized there, the replay runs a plain --apply, strip_cron drops every
# managed line, and only the recorded flags re-add them — so the judge cron
# disappears with no error. Measured 2026-08-25 (#1264): live on 1 of 11 fleet
# nodes although every node had the script and a non-empty review queue.
ok "install record materializes --judge so a self-update replay keeps the cron" \
  'jq -e ".argv==[\"--apply\",\"--codex\",\"--judge\"]" "$nrec" >/dev/null'
ok "replaying the recorded argv preserves the judge cron line" \
  'run_install --apply --codex --judge >/dev/null 2>&1 && [ "$(grep -c "judge-batch.py" "$cron_store")" = 1 ]'
# A re-apply that genuinely drops --judge is opt-OUT, not replay: the line is
# removed on purpose. This asserts the removal path, which the previous
# revision of this case claimed to be survival while asserting a count of 0.
ok "a deliberate re-apply without --judge removes the judge cron" \
  'run_install --apply --codex >/dev/null 2>&1 && [ "$(grep -c "judge-batch.py" "$cron_store")" = 0 ]'
ok "opting out also drops --judge from the install record" \
  'jq -e ".argv==[\"--apply\",\"--codex\"]" "$nrec" >/dev/null'

# --- APPLY mode (#1264): dry-run is the default and apply is opt-in per node.
# The flag exists so an approved apply pilot SURVIVES a re-apply — before it,
# the only way to enable apply was hand-editing the managed cron line, which
# strip_cron rewrites, silently switching the pilot back off.
out="$(run_install --apply --codex --judge-apply 2>&1)"; rc=$?
ok "--judge-apply puts NUNCHI_JUDGE_APPLY=1 on the judge cron line" \
  '[ "$rc" = 0 ] && grep "judge-batch.py" "$cron_store" | grep -q "NUNCHI_JUDGE_APPLY=1"'
ok "--judge-apply implies --judge (one judge line, not zero or two)" \
  '[ "$(grep -c "judge-batch.py" "$cron_store")" = 1 ]'
ok "--judge-apply announces that it mutates the store" 'grep -q "APPLY — mutates the fact store" <<<"$out"'
ok "install record materializes --judge-apply, not the weaker --judge" \
  'jq -e ".argv==[\"--apply\",\"--codex\",\"--judge-apply\"]" "$nrec" >/dev/null'
ok "replaying the recorded argv keeps APPLY (no silent downgrade to dry-run)" \
  'run_install --apply --codex --judge-apply >/dev/null 2>&1 && grep "judge-batch.py" "$cron_store" | grep -q "NUNCHI_JUDGE_APPLY=1"'
# Stepping back down to plain --judge must actually disarm the cron.
out="$(run_install --apply --codex --judge 2>&1)"; rc=$?
ok "re-applying with plain --judge disarms APPLY on the cron line" \
  '[ "$rc" = 0 ] && grep -q "judge-batch.py" "$cron_store" && ! grep "judge-batch.py" "$cron_store" | grep -q "NUNCHI_JUDGE_APPLY"'
ok "stepping down also rewrites the install record to --judge" \
  'jq -e ".argv==[\"--apply\",\"--codex\",\"--judge\"]" "$nrec" >/dev/null'
run_install --apply --codex >/dev/null 2>&1

# --- Piri lane: Piri has no distill feed, so its lane runs a per-session
# extractor (piri-feed.sh) and mines transcripts with the conversation miner
# attributed to the piri wing (--wing piri), mirroring the Codex lane.
out="$(run_install --apply --piri 2>&1)"; rc=$?
ok "--apply --piri enables an owner-only mode marker" \
  '[ "$rc" = 0 ] && [ "$(cat "$state/nunchi.mode")" = on ] && [ "$(stat -c %a "$state/nunchi.mode")" = 600 ]'
ok "Piri apply writes feed, refresh and bench cron and atomically drops the codex lane" \
  '[ "$(grep -c "nunchi:#816" "$cron_store")" = 3 ] && grep -q "piri-feed.sh" "$cron_store" && grep -q "mempalace-refresh.sh piri $piri_sessions" "$cron_store" && ! grep -q "codex-feed.sh" "$cron_store"'
ok "Piri apply keeps the standalone nunchi hook removed (no Claude SessionStart path)" \
  '! grep -q "nunchi/sessionstart.sh" "$claude_dir/settings.local.json"'

# --- Piri feed extractor CLI wiring: cron's bare PATH has no piri entry, so
# an unpinned feed cron was a silent no-op on real nodes. The installer must
# resolve a runnable CLI and pin CCC_PIRI_CLI_PATH into the cron line.
write_exec_stub "$fake_bin/piri-real.sh" <<'SH'
exit 0
SH
out="$(CCC_PIRI_REAL_CLI_PATH="$fake_bin/piri-real.sh" run_install --apply --piri 2>&1)"; rc=$?
ok "Piri apply pins the resolved extractor CLI into the feed cron" \
  '[ "$rc" = 0 ] && grep -q "CCC_PIRI_CLI_PATH=$fake_bin/piri-real.sh" "$cron_store"'
ok "Piri apply with a resolvable CLI does not warn" \
  '! grep -q "no runnable Piri CLI" <<<"$out"'
out="$(CCC_PIRI_REAL_CLI_PATH= CCC_PIRI_CLI_PATH= CCC_PIRI_DEFAULT_CLI_PATH="$fake_bin/absent.sh" PATH="$fake_bin:/usr/bin:/bin" run_install --apply --piri 2>&1)"; rc=$?
ok "Piri apply without any runnable CLI warns loudly instead of installing a dead cron" \
  '[ "$rc" = 0 ] && grep -q "no runnable Piri CLI" <<<"$out"'
ok "Piri apply without a CLI leaves the feed cron unpinned" \
  '! grep -q "CCC_PIRI_CLI_PATH" "$cron_store"'
out="$(env -i HOME="$home" PATH="/usr/bin:/bin" CCC_STATE_DIR="$state" NUNCHI_HOME="$nunchi_home" NUNCHI_DB="$nunchi_home/facts.db" NUNCHI_SNAPSHOT="$nunchi_home/snapshot.md" bash "$hooks/nunchi/piri-feed.sh" 2>&1)"; rc=$?
ok "piri-feed without a runnable CLI still exits 0 but now says so" \
  '[ "$rc" = 0 ] && grep -q "Piri CLI not runnable" <<<"$out"'
mkdir -p "$TMP/piri-dir-cwd/piri"
out="$(cd "$TMP/piri-dir-cwd" && env -i HOME="$home" PATH="/usr/bin:/bin" CCC_STATE_DIR="$state" NUNCHI_HOME="$nunchi_home" NUNCHI_DB="$nunchi_home/facts.db" NUNCHI_SNAPSHOT="$nunchi_home/snapshot.md" bash "$hooks/nunchi/piri-feed.sh" 2>&1)"; rc=$?
ok "piri-feed guard rejects an executable ./piri directory (checkout-root false positive)" \
  '[ "$rc" = 0 ] && grep -q "Piri CLI not runnable" <<<"$out"'
piri_refresh_capture="$TMP/piri-refresh.args"
CCC_TEST_MEMPALACE_CAPTURE="$piri_refresh_capture" HOME="$home" \
  PATH="/usr/bin:/bin" CCC_STATE_DIR="$state" NUNCHI_HOME="$nunchi_home" \
  CCC_NUNCHI_MEMPALACE_CLI="$home/.local/bin/mempalace" \
  bash "$hooks/nunchi/mempalace-refresh.sh" piri "$piri_sessions" >/dev/null 2>&1; rc=$?
ok "Piri refresh uses the conversation miner attributed to the piri wing" \
  '[ "$rc" = 0 ] && grep -qx "mine $piri_sessions --mode convos --wing piri" "$piri_refresh_capture" && jq -e '\''.provider == "piri" and .state == "ok" and .exit_code == 0 '\'' "$nunchi_home/mempalace-refresh.status.json" >/dev/null'
out="$(run_install 2>&1)"; rc=$?
ok "non-scoped status keeps reading the node-global collection status file" \
  '[ "$rc" = 0 ] && grep -q "^collection: state=ok exit_code=0 finished_at=" <<<"$out"'

# Audience-scoped Piri collection: canonical opaque direct children only,
# with one Nunchi store and one MemPalace HOME per audience.
audience_root="$TMP/audiences"
first_scope="$audience_root/private-11111111111111111111111111111111"
second_scope="$audience_root/private-22222222222222222222222222222222"
shared_scope="$audience_root/shared"
invalid_scope="$audience_root/private-raw-934719283"
mkdir -p "$first_scope/piri/sessions" "$second_scope/piri/sessions" \
  "$shared_scope/piri/sessions" "$invalid_scope/piri/sessions"
chmod 700 "$audience_root" "$first_scope" "$second_scope" "$shared_scope" "$invalid_scope"
chmod 700 "$first_scope/piri/sessions" "$second_scope/piri/sessions" \
  "$shared_scope/piri/sessions" "$invalid_scope/piri/sessions"
for scope_dir in "$first_scope" "$second_scope" "$shared_scope" "$invalid_scope"; do
  printf '%s\n' '{"type":"message","message":{"role":"user","content":"short"}}' \
    > "$scope_dir/piri/sessions/1_test.jsonl"
  chmod 600 "$scope_dir/piri/sessions/1_test.jsonl"
done
long_text="$(printf 'x%.0s' {1..260})"
printf '%s\n' "{\"type\":\"message\",\"message\":{\"role\":\"user\",\"content\":\"$long_text\"}}" \
  > "$first_scope/piri/sessions/1_test.jsonl"
chmod 600 "$first_scope/piri/sessions/1_test.jsonl"
out="$(env "${common_env[@]}" bash "$ROOT/scripts/install-nunchi.sh" \
  --apply --piri --audience-scoped "$audience_root" 2>&1)"; rc=$?
# All three managed lines must carry the scope, not just feed and refresh.
# This assertion previously expected 2 and so encoded the #827 defect: the
# bench cron ran unscoped, which made bench.sh grade $HOME/.nunchi — a store
# that stops receiving facts the moment ingest becomes scoped.
ok "scoped Piri install persists the exact audience dispatcher root in managed cron" \
  '[ "$rc" = 0 ] && [ "$(grep -c "CCC_NUNCHI_AUDIENCE_SCOPED=1" "$cron_store")" = 3 ] && [ "$(grep -c "CCC_NUNCHI_AUDIENCE_ROOT=$audience_root" "$cron_store")" = 3 ] && grep -q "audience_scoped: enabled=1 root=$audience_root" <<<"$out"'
ok "the weekly bench cron is scoped alongside the feed and refresh lines" \
  'grep "bench.sh" "$cron_store" | grep -q "CCC_NUNCHI_AUDIENCE_SCOPED=1 CCC_NUNCHI_AUDIENCE_ROOT=$audience_root"'
ok "scoped apply materializes provider and audience flags into the install record" \
  'jq -e --arg root "$audience_root" ".argv==[\"--apply\",\"--piri\",\"--audience-scoped\",\$root]" "$nrec" >/dev/null'
out="$(run_install 2>&1)"; rc=$?
ok "later installer status recovers scoped mode and root from managed cron" \
  '[ "$rc" = 0 ] && grep -q "audience_scoped: enabled=1 root=$audience_root" <<<"$out"'
write_exec_stub "$fake_bin/piri" <<'SH'
printf '%s\n' '{"honcho":[{"kind":"fact","text":"SCOPED_PROVIDER_PROVENANCE","subject":"session"}]}'
SH
HOME="$home" PATH="$fake_bin:/usr/bin:/bin" CCC_STATE_DIR="$state" \
  CCC_PIRI_CLI_PATH="$fake_bin/piri" CCC_NUNCHI_AUDIENCE_SCOPED=1 \
  CCC_NUNCHI_AUDIENCE_ROOT="$audience_root" \
  bash "$hooks/nunchi/piri-feed.sh" >/dev/null 2>&1; rc=$?
ok "scoped Piri feed creates independent seen/snapshot state for canonical audiences" \
  '[ "$rc" = 0 ] && [ -f "$first_scope/nunchi/piri-seen" ] && [ -f "$second_scope/nunchi/piri-seen" ] && [ -f "$shared_scope/nunchi/piri-seen" ]'
ok "scoped Piri feed ignores non-opaque audience names" \
  '[ ! -e "$invalid_scope/nunchi" ]'
ok "scoped Piri facts retain Piri provider provenance in their session evidence" \
  'python3 - "$first_scope/nunchi/facts.db" <<'"'"'PY'"'"'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
assert db.execute("SELECT COUNT(*) FROM peer_facts WHERE fact=? AND evidence LIKE ?", ("SCOPED_PROVIDER_PROVENANCE", "distill:piri:%")).fetchone()[0] == 1
PY'

scoped_mp_capture="$TMP/scoped-mempalace.args"
write_exec_stub "$fake_bin/scoped-mempalace" <<'SH'
printf '%s|%s\n' "$HOME" "$*" >> "${CCC_TEST_MEMPALACE_CAPTURE:?}"
exit 0
SH
HOME="$home" PATH="$fake_bin:/usr/bin:/bin" CCC_STATE_DIR="$state" \
  CCC_NUNCHI_AUDIENCE_SCOPED=1 CCC_NUNCHI_AUDIENCE_ROOT="$audience_root" \
  CCC_NUNCHI_MEMPALACE_CLI="$fake_bin/scoped-mempalace" \
  CCC_TEST_MEMPALACE_CAPTURE="$scoped_mp_capture" \
  bash "$hooks/nunchi/mempalace-refresh.sh" piri "$audience_root" >/dev/null 2>&1; rc=$?
ok "scoped MemPalace refresh uses a distinct HOME and target for each audience" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$scoped_mp_capture")" = 3 ] && grep -q "^$first_scope/mempalace-home|mine $first_scope/piri/sessions --mode convos --wing piri$" "$scoped_mp_capture" && grep -q "^$second_scope/mempalace-home|mine $second_scope/piri/sessions --mode convos --wing piri$" "$scoped_mp_capture" && grep -q "^$shared_scope/mempalace-home|mine $shared_scope/piri/sessions --mode convos --wing piri$" "$scoped_mp_capture"'
ok "scoped MemPalace refresh never creates state for an invalid audience" \
  '[ ! -e "$invalid_scope/mempalace-home" ] && [ -f "$first_scope/nunchi/mempalace-refresh.status.json" ] && [ -f "$shared_scope/nunchi/mempalace-refresh.status.json" ]'

# #985: scoped status must aggregate per-scope refresh results — the global
# status file goes stale once the audience dispatcher owns collection.
out="$(run_install 2>&1)"; rc=$?
ok "scoped status collection row aggregates per-scope results instead of the stale global file" \
  '[ "$rc" = 0 ] && grep -q "^collection: .*shared(state=ok exit_code=0 finished_at=" <<<"$out" && grep -q "private-1111…(state=ok" <<<"$out" && grep -q "private-2222…(state=ok" <<<"$out" && ! grep -q "^collection: state=" <<<"$out"'
ok "scoped collection row never lists non-canonical audience names" \
  '! grep -q "private-raw" <<<"$out"'
printf '%s\n' '{"provider":"piri","schema":1,"state":"error","exit_code":124,"started_at":1786000000,"finished_at":1786000001}' \
  > "$second_scope/nunchi/mempalace-refresh.status.json"
chmod 600 "$second_scope/nunchi/mempalace-refresh.status.json"
out="$(run_install 2>&1)"; rc=$?
ok "scoped collection row sorts the worst state first" \
  '[ "$rc" = 0 ] && grep -q "^collection: private-2222…(state=error exit_code=124 finished_at=1786000001) " <<<"$out"'

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
ok "missing CLI after mode-on degrades to peer-facts-only silently" \
  '[ "$rc" = 0 ] && jq -e '\'' .state == "degraded" and .exit_code == 0 '\'' "$edge_status" >/dev/null'
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
ok "reapply keeps the same gen stamp on all managed lines" \
  '[ "$(grep -cF "gen=$want_gen" "$cron_store")" = 3 ]'

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
runtime_cmd="${runtime_cmd% \# nunchi:#816 gen=h_*}"
runtime_cmd="${runtime_cmd% \# nunchi:#816}"
# crond removes the escape that protects each literal percent before /bin/sh.
runtime_cmd="${runtime_cmd//\\%/%}"
env -i HOME="$home" PATH="/usr/bin:/bin" CCC_TEST_MEMPALACE_CAPTURE="$weird_capture" \
  /bin/sh -c "$runtime_cmd" >/dev/null 2>&1; cron_rc=$?
ok "generated refresh cron preserves restricted-PATH custom and Termux-style paths" \
  '[ "$rc" = 0 ] && [ "$cron_rc" = 0 ] && grep -q "CCC_NUNCHI_MEMPALACE_CLI=" <<<"$refresh_line" && grep -qx "mine $weird_sweep --mode convos --wing codex" "$weird_capture" && jq -e '\'' .provider == "codex" and .state == "ok" '\'' "$weird_status" >/dev/null && [ "$(stat -c %a "$weird_status")" = 600 ]'
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
ok "--remove drops the install record (no resurrection via re-apply)" '[ ! -f "$nrec" ]'

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

# ---- #865: provider-aware collection hardening (env paths, umask, status, drift) ----
# Restore a valid managed loader: the safety cases above leave it removed/oversized.
cp "$ROOT/claude/hooks/nunchi/codex-loader.py" "$hooks/nunchi/codex-loader.py"
chmod 700 "$hooks/nunchi/codex-loader.py"
custom_codex="$TMP/custom-codex-home"; mkdir -p "$custom_codex/sessions"
out="$(env "${common_env[@]}" CODEX_HOME="$custom_codex" bash "$ROOT/scripts/install-nunchi.sh" --apply --codex 2>&1)"; rc=$?
ok "apply routes codex collection source through a custom CODEX_HOME" \
  '[ "$rc" = 0 ] && grep -q "mempalace-refresh.sh codex $custom_codex/sessions" "$cron_store" && grep -q "source: kind=mine path=$custom_codex/sessions" <<<"$out"'
ok "apply makes the nunchi home owner-only (0700)" \
  '[ "$(stat -c %a "$nunchi_home")" = 700 ]'

out="$(env "${common_env[@]}" CCC_AGENT_PROVIDER=codex bash "$ROOT/scripts/install-nunchi.sh" 2>&1)"; rc=$?
ok "status reports provider match=ok, source, mempalace and collection (body-free)" \
  '[ "$rc" = 0 ] && grep -q "provider: configured=codex runtime=codex match=ok" <<<"$out" && grep -q "source: kind=mine path=$custom_codex/sessions" <<<"$out" && grep -q "mempalace: binary=" <<<"$out" && grep -q "^collection: " <<<"$out"'

out="$(env "${common_env[@]}" CCC_AGENT_PROVIDER=claude bash "$ROOT/scripts/install-nunchi.sh" 2>&1)"; rc=$?
ok "status flags provider drift when runtime CCC_AGENT_PROVIDER differs" \
  '[ "$rc" = 0 ] && grep -q "provider: configured=codex runtime=claude match=DRIFT" <<<"$out"'

n865_status="$TMP/n865.status.json"
HOME="$home" PATH="/usr/bin:/bin" CCC_STATE_DIR="$state" NUNCHI_HOME="$nunchi_home" \
  CCC_NUNCHI_MEMPALACE_STATUS="$n865_status" CCC_NUNCHI_MEMPALACE_CLI="$home/.local/bin/mempalace" \
  bash "$hooks/nunchi/mempalace-refresh.sh" codex "$custom_codex/sessions" >/dev/null 2>&1; rc=$?
ok "refresh wrapper umask 077 keeps lock and status owner-only" \
  '[ "$rc" = 0 ] && [ "$(stat -c %a "$nunchi_home/mempalace-refresh.lock")" = 600 ] && [ "$(stat -c %a "$n865_status")" = 600 ]'

# --- ghost marker detection (#1079): a managed line whose paths no longer
# exist fails on every tick forever (gongmyoung root ran 3 for weeks).
# --apply strips our own ghosts (strip_cron) but must WARN first; status
# reports the count; a clean crontab stays quiet.
: > "$cron_store"
printf '%s\n' \
  '*/10 * * * * bash /nonexistent-ghost-home/.claude/hooks/nunchi/ingest-cron.sh >> /nonexistent-ghost-home/.nunchi/cron.log 2>&1 # nunchi:#816' \
  > "$cron_store"
out="$(run_install --apply --codex 2>&1)"; rc=$?
ok "apply warns about ghost cron lines whose paths are missing (#1079)" \
  '[ "$rc" = 0 ] && grep -q "WARNING (apply): managed nunchi cron line(s) point at missing paths" <<<"$out" && grep -q "nonexistent-ghost-home" <<<"$out"'
ok "apply still strips the ghost line from our own crontab" \
  '! grep -q "nonexistent-ghost-home" "$cron_store" && [ "$(grep -c "nunchi:#816" "$cron_store")" = 3 ]'

out="$(run_install 2>&1)"; rc=$?
ok "status reports ghost_cron=0 after a clean apply" \
  '[ "$rc" = 0 ] && grep -q "^ghost_cron: 0 line(s)" <<<"$out"'

printf '%s\n' \
  '7 8 * * 1 bash /gone/.claude/hooks/nunchi/bench.sh >> /gone/.nunchi/bench.cron.log 2>&1 # nunchi:#816' \
  >> "$cron_store"
out="$(run_install 2>&1)"; rc=$?
ok "status counts ghost lines pointing at missing paths" \
  '[ "$rc" = 0 ] && grep -q "^ghost_cron: 1 line(s)" <<<"$out"'

out="$(run_install --remove 2>&1)"; rc=$?
ok "remove cleans our ghosts silently (no cross-account residue in tests)" \
  '[ "$rc" = 0 ] && ! grep -q "ghost entries" <<<"$out" && [ "$(grep -c "nunchi:#816" "$cron_store" || true)" = 0 ]'

# ---- #1263: --apply must diagnose "no authenticated LLM backend" instead of
# silently succeeding with a dialectic/bench synthesis that can never answer.
# `run_install` inherits the caller's real PATH (no override in common_env),
# so a real claude/codex on the host running this suite would otherwise leak
# into the check — auth_bin is prepended ahead of it so the stub always wins,
# keeping this hermetic regardless of the host's actual login state.
auth_bin="$TMP/bin-auth"; mkdir -p "$auth_bin"
cat > "$auth_bin/claude" <<'EOF'
#!/usr/bin/env bash
[ "$1" = auth ] && [ "$2" = status ] && echo '{"loggedIn": false}'
EOF
cat > "$auth_bin/codex" <<'EOF'
#!/usr/bin/env bash
# Real `codex login status` writes its verdict to STDERR, not stdout (measured
# 2026-08-25). The stub must do the same or the probe is tested against
# behaviour the CLI does not have — which is exactly how the 2>/dev/null bug
# survived: an authenticated codex read as unauthenticated fleet-wide.
[ "$1" = login ] && [ "$2" = status ] && echo "Not logged in" >&2
EOF
chmod +x "$auth_bin/claude" "$auth_bin/codex"
out="$(PATH="$auth_bin:$PATH" run_install --apply --codex 2>&1)"; rc=$?
ok "apply warns when neither claude nor codex is authenticated" \
  '[ "$rc" = 0 ] && grep -q "no authenticated LLM backend" <<<"$out"'
out="$(PATH="$auth_bin:$PATH" run_install 2>&1)"; rc=$?
ok "status reports backend_auth=NONE when neither backend is authenticated" \
  '[ "$rc" = 0 ] && grep -q "^backend_auth: NONE" <<<"$out"'

# codex alone authenticated (claude still logged out): the verdict arrives on
# stderr, so this is the case the old `2>/dev/null` probe silently failed.
cat > "$auth_bin/codex" <<'EOF'
#!/usr/bin/env bash
[ "$1" = login ] && [ "$2" = status ] && echo "Logged in using ChatGPT" >&2
EOF
chmod +x "$auth_bin/codex"
out="$(PATH="$auth_bin:$PATH" run_install 2>&1)"; rc=$?
ok "backend_auth=ok when only codex is authenticated (verdict on stderr)" \
  '[ "$rc" = 0 ] && grep -q "^backend_auth: ok" <<<"$out"'
# The anchor must still reject a logged-out codex whose message merely
# contains "logged in".
cat > "$auth_bin/codex" <<'EOF'
#!/usr/bin/env bash
[ "$1" = login ] && [ "$2" = status ] && echo "Not logged in" >&2
EOF
chmod +x "$auth_bin/codex"
out="$(PATH="$auth_bin:$PATH" run_install 2>&1)"; rc=$?
ok "a logged-out codex is not rescued by merging stderr" \
  '[ "$rc" = 0 ] && grep -q "^backend_auth: NONE" <<<"$out"'

cat > "$auth_bin/claude" <<'EOF'
#!/usr/bin/env bash
[ "$1" = auth ] && [ "$2" = status ] && echo '{"loggedIn": true}'
EOF
chmod +x "$auth_bin/claude"
out="$(PATH="$auth_bin:$PATH" run_install --apply --codex 2>&1)"; rc=$?
ok "apply stays quiet once claude is authenticated" \
  '[ "$rc" = 0 ] && ! grep -q "no authenticated LLM backend" <<<"$out"'
out="$(PATH="$auth_bin:$PATH" run_install 2>&1)"; rc=$?
ok "status reports backend_auth=ok once a backend is authenticated" \
  '[ "$rc" = 0 ] && grep -q "^backend_auth: ok" <<<"$out"'

run_install --remove >/dev/null 2>&1

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
