#!/usr/bin/env bash
# Tests for ccc memory cache/index/eval helpers.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# The largest instance of this leak: inherited CCC_MEMORY_* redirects the tool
# at real node stores and 74 assertions miss on a live node (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# Hermetic by default: load-memory fires a DETACHED refresh-memory that rebuilds
# the index and consolidates facts out-of-band. During tests that races against
# fixtures we just built, mutating shared state mid-assertion. Suppress it suite-
# wide; the one guard test below unsets it to prove the default still fires.
export CCC_MEMORY_NO_REFRESH=1
# Never let a memory-check invocation without an explicit fixture inspect a
# checkout-local or operator journal while this broad suite is running.
export CCC_DISTILL_JOURNAL_DIR="$TMP/default-distill-journal"

state="$TMP/state"
cache="$TMP/cache"
mem="$TMP/memories"
mkdir -p "$state" "$cache" "$mem"
chmod 700 "$state"
printf 'test-node\n' > "$state/node.txt"
printf 'allowed operation policy\n' > "$mem/MEMORY.md"
printf 'user likes concise Korean reports\n' > "$mem/USER.md"
printf 'wiki cache contains practical evidence reports\n' > "$cache/wiki.txt"

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check default reports wiki ok without the retired honcho source" '[ "$rc" = 0 ] && jq -e ".wiki.status == \"ok\" and (.honcho | not)" >/dev/null <<<"$out"'
ok "memory check reports inactive new stack without reading bodies" 'jq -e ".nunchi.status == \"off\" and .mempalace.status == \"off\"" >/dev/null <<<"$out"'

# Body-free nunchi + MemPalace readiness projection (#827).
probe_home="$TMP/probe-home"
probe_claude="$probe_home/.claude"
probe_state="$probe_claude/state"
probe_nunchi="$probe_home/.nunchi"
probe_palace="$probe_home/.mempalace/palace"
mkdir -p "$probe_state" "$probe_claude/hooks/nunchi" "$probe_nunchi" \
  "$probe_palace" "$probe_home/.local/bin"
printf 'on' > "$probe_state/nunchi.mode"
printf '#!/usr/bin/env python3\n' > "$probe_claude/hooks/nunchi/nunchi.py"
printf '## nunchi working memory (primary)\nPROBE_SECRET_BODY\n' > "$probe_nunchi/snapshot.md"
printf '#!/usr/bin/env bash\n' > "$probe_home/.local/bin/mempalace"
chmod +x "$probe_home/.local/bin/mempalace"
python3 - "$probe_nunchi/facts.db" "$probe_palace/chroma.sqlite3" <<'PY'
import sqlite3, sys
facts = sqlite3.connect(sys.argv[1])
facts.execute("CREATE TABLE peer_facts(id INTEGER PRIMARY KEY, fact TEXT)")
facts.execute("INSERT INTO peer_facts(fact) VALUES ('PROBE_SECRET_FACT')")
facts.commit(); facts.close()
palace = sqlite3.connect(sys.argv[2])
palace.execute("CREATE TABLE embeddings(id INTEGER PRIMARY KEY)")
palace.execute("INSERT INTO embeddings DEFAULT VALUES")
palace.commit(); palace.close()
PY
probe_cron=$'*/10 * * * * bash /tmp/codex-feed.sh # nunchi:#816\n17 * * * * bash /tmp/mempalace-refresh.sh codex /tmp/sessions # nunchi:#816\n7 8 * * 1 bash /tmp/bench.sh # nunchi:#816'
repair_ok=$'  [drawers]\n    sqlite count:   1\n    hnsw count:     1\n    divergence:     0\n    status:         OK\n\n  [closets]'
repair_diverged=$'  [drawers]\n    sqlite count:   100\n    hnsw count:     10\n    divergence:     90\n    status:         DIVERGED\n\n  [closets]'
printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":180,"finished_at":190,"ignored":"PROBE_SECRET_REFRESH"}' > "$probe_nunchi/mempalace-refresh.status.json"
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check reports healthy nunchi and MemPalace scalars" '[ "$rc" = 0 ] && jq -e '\''
  .nunchi.status == "ok" and .nunchi.db.integrity == "ok" and .nunchi.db.facts == 1
  and .nunchi.snapshot.primary_header == true and .nunchi.cron.feed == "codex"
  and .mempalace.status == "ok" and .mempalace.integrity == "ok" and .mempalace.embeddings == 1
  and .mempalace.index == {status:"ok", sqlite_count:1, hnsw_count:1, divergence:0}
  and .mempalace.refresh.status == "ok" and .mempalace.refresh.age_seconds == 10
'\'' >/dev/null <<<"$out"'
ok "memory readiness JSON never exposes snapshot, fact or refresh bodies" '! grep -q "PROBE_SECRET_BODY\|PROBE_SECRET_FACT\|PROBE_SECRET_REFRESH" <<<"$out"'

# Regression: mempalace-refresh.sh writes `state=degraded, exit_code=0` on the
# documented #865 MemPalace-absent path; the unscoped probe used to reject the
# record as refresh-invalid (file corrupt) instead of reporting the state.
printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"degraded","exit_code":0,"started_at":180,"finished_at":190}' > "$probe_nunchi/mempalace-refresh.status.json"
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"
ok "degraded-by-design refresh state is reported, not called invalid" \
  'jq -e ".mempalace.refresh.status == \"degraded\"" >/dev/null <<<"$out"'
printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":180,"finished_at":190,"ignored":"PROBE_SECRET_REFRESH"}' > "$probe_nunchi/mempalace-refresh.status.json"

# #1174: since #1081/#1140 the installers render managed entries with a
# ` gen=h_<hex12>` stamp suffix; the probe must still recognize and count
# them (its marker regex used to be end-anchored at `# nunchi:#816`, which
# read every stamped entry as unmanaged and falsely reported feed/refresh/
# bench-count degraded on every bootstrapped node).
stamped_probe_cron=$'*/10 * * * * bash /tmp/codex-feed.sh # nunchi:#816 gen=h_0123456789ab\n17 * * * * bash /tmp/mempalace-refresh.sh codex /tmp/sessions # nunchi:#816 gen=h_0123456789ab\n7 8 * * 1 bash /tmp/bench.sh # nunchi:#816 gen=h_0123456789ab'
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$stamped_probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check counts gen-stamped managed cron entries (#1174)" '[ "$rc" = 0 ] && jq -e '\''
  .nunchi.status == "ok" and .nunchi.cron.feed == "codex"
  and .nunchi.cron.feed_count == 1 and .nunchi.cron.managed_refresh_count == 1
  and .nunchi.cron.bench_count == 1 and .nunchi.cron.legacy_sweep_count == 0
'\'' >/dev/null <<<"$out"'

audience_probe_root="$TMP/audience-probe"
audience_private="$audience_probe_root/private-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
audience_shared="$audience_probe_root/shared"
audience_invalid="$audience_probe_root/private-raw-12345"
mkdir -p "$audience_private/piri/sessions" "$audience_private/nunchi" \
  "$audience_private/mempalace-home/.mempalace/palace" \
  "$audience_shared/piri/sessions" "$audience_shared/nunchi" "$audience_invalid"
chmod 700 "$audience_probe_root" "$audience_private" "$audience_shared" "$audience_invalid"
chmod 700 "$audience_private/piri/sessions" "$audience_shared/piri/sessions"
printf 'PRIVATE_BODY_MUST_NOT_RENDER\n' > "$audience_private/nunchi/snapshot.md"
printf 'x' > "$audience_private/nunchi/facts.db"
printf 'x' > "$audience_private/mempalace-home/.mempalace/palace/chroma.sqlite3"
printf '{}' > "$audience_shared/nunchi/mempalace-refresh.status.json"
chmod 600 "$audience_private/nunchi/snapshot.md" "$audience_private/nunchi/facts.db" \
  "$audience_private/mempalace-home/.mempalace/palace/chroma.sqlite3" \
  "$audience_shared/nunchi/mempalace-refresh.status.json"
scoped_probe_cron="*/10 * * * * CCC_NUNCHI_AUDIENCE_SCOPED=1 CCC_NUNCHI_AUDIENCE_ROOT=$audience_probe_root bash /tmp/codex-feed.sh # nunchi:#816"
scoped_probe_cron+=$'\n'"17 * * * * CCC_NUNCHI_AUDIENCE_SCOPED=1 CCC_NUNCHI_AUDIENCE_ROOT=$audience_probe_root bash /tmp/mempalace-refresh.sh codex /tmp/sessions # nunchi:#816"
scoped_probe_cron+=$'\n7 8 * * 1 bash /tmp/bench.sh # nunchi:#816'
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$scoped_probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check reports body-free scoped partition counts and invalid entries" '[ "$rc" = 0 ] && jq -e '\''
  .nunchi.audience_scoped.enabled == true
  and .nunchi.audience_scoped.root_status == "ok"
  and .nunchi.audience_scoped.scope_count == 2
  and .nunchi.audience_scoped.private_count == 1
  and .nunchi.audience_scoped.shared_count == 1
  and .nunchi.audience_scoped.session_roots == 2
  and .nunchi.audience_scoped.nunchi_db_partitions == 1
  and .nunchi.audience_scoped.snapshot_partitions == 1
  and .nunchi.audience_scoped.mempalace_index_partitions == 1
  and .nunchi.audience_scoped.mempalace_status_partitions == 1
  and .nunchi.audience_scoped.invalid_entries == 1
'\'' >/dev/null <<<"$out" && ! grep -q "PRIVATE_BODY_MUST_NOT_RENDER\|private-aaaaaaaa" <<<"$out"'

custom_nunchi_store="$TMP/custom-nunchi-store"
mkdir -p "$custom_nunchi_store"
cp "$probe_nunchi/facts.db" "$custom_nunchi_store/facts.db"
cp "$probe_nunchi/snapshot.md" "$custom_nunchi_store/snapshot.md"
custom_path_cron="*/10 * * * * NUNCHI_DB=$custom_nunchi_store/facts.db NUNCHI_SNAPSHOT=$custom_nunchi_store/snapshot.md bash /tmp/codex-feed.sh # nunchi:#816"
custom_path_cron+=$'\n17 * * * * bash /tmp/mempalace-refresh.sh codex /tmp/sessions # nunchi:#816'
custom_path_cron+=$'\n'"7 8 * * 1 NUNCHI_DB=$custom_nunchi_store/facts.db NUNCHI_SNAPSHOT=$custom_nunchi_store/snapshot.md bash /tmp/bench.sh # nunchi:#816"
mv "$probe_nunchi/facts.db" "$probe_nunchi/facts.db.default"
mv "$probe_nunchi/snapshot.md" "$probe_nunchi/snapshot.md.default"
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$custom_path_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check recovers custom DB and snapshot paths from managed cron" '[ "$rc" = 0 ] && jq -e '\''
  .nunchi.status == "ok" and .nunchi.db.integrity == "ok" and .nunchi.db.facts == 1
  and .nunchi.snapshot.primary_header == true
'\'' >/dev/null <<<"$out"'
mv "$probe_nunchi/facts.db.default" "$probe_nunchi/facts.db"
mv "$probe_nunchi/snapshot.md.default" "$probe_nunchi/snapshot.md"

stamped_custom_path_cron="*/10 * * * * NUNCHI_DB=$custom_nunchi_store/facts.db NUNCHI_SNAPSHOT=$custom_nunchi_store/snapshot.md bash /tmp/codex-feed.sh # nunchi:#816 gen=h_0123456789ab"
stamped_custom_path_cron+=$'\n17 * * * * bash /tmp/mempalace-refresh.sh codex /tmp/sessions # nunchi:#816 gen=h_0123456789ab'
stamped_custom_path_cron+=$'\n'"7 8 * * 1 NUNCHI_DB=$custom_nunchi_store/facts.db NUNCHI_SNAPSHOT=$custom_nunchi_store/snapshot.md bash /tmp/bench.sh # nunchi:#816 gen=h_0123456789ab"
mv "$probe_nunchi/facts.db" "$probe_nunchi/facts.db.default"
mv "$probe_nunchi/snapshot.md" "$probe_nunchi/snapshot.md.default"
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$stamped_custom_path_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check recovers custom paths from gen-stamped managed cron (#1174)" '[ "$rc" = 0 ] && jq -e '\''
  .nunchi.status == "ok" and .nunchi.db.integrity == "ok" and .nunchi.db.facts == 1
  and .nunchi.snapshot.primary_header == true
'\'' >/dev/null <<<"$out"'
mv "$probe_nunchi/facts.db.default" "$probe_nunchi/facts.db"
mv "$probe_nunchi/snapshot.md.default" "$probe_nunchi/snapshot.md"

conflicting_path_cron="*/10 * * * * NUNCHI_DB=$custom_nunchi_store/facts.db bash /tmp/codex-feed.sh # nunchi:#816"
conflicting_path_cron+=$'\n17 * * * * bash /tmp/mempalace-refresh.sh codex /tmp/sessions # nunchi:#816'
conflicting_path_cron+=$'\n'"7 8 * * 1 NUNCHI_DB=$probe_nunchi/facts.db bash /tmp/bench.sh # nunchi:#816"
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$conflicting_path_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "conflicting managed cron paths fail readiness closed" \
  '[ "$rc" = 0 ] && jq -e '\'' .nunchi.status == "degraded" and (.nunchi.reasons | index("cron-env-conflict")) != null '\'' >/dev/null <<<"$out"'

printf 'not-a-sqlite-database\n' > "$custom_nunchi_store/broken.db"
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  NUNCHI_DB="$custom_nunchi_store/broken.db" NUNCHI_SNAPSHOT="$custom_nunchi_store/snapshot.md" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "custom DB failure cannot be masked by a healthy default database" \
  '[ "$rc" = 0 ] && jq -e '\'' .nunchi.status == "degraded" and .nunchi.db.integrity == "error" and (.nunchi.reasons | index("db-error")) != null '\'' >/dev/null <<<"$out"'

optional_cron=$'*/10 * * * * bash /tmp/codex-feed.sh # nunchi:#816\n7 8 * * 1 bash /tmp/bench.sh # nunchi:#816'
out="$(HOME="$probe_home" PREFIX="/data/data/com.termux/files/usr" \
  CCC_NUNCHI_MEMPALACE_CLI="$TMP/missing-mempalace" \
  CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_CRONTAB_TEXT="$optional_cron" \
  bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "Termux without optional MemPalace accepts feed and bench only" \
  '[ "$rc" = 0 ] && jq -e '\'' .nunchi.status == "ok" and .nunchi.cron.managed_refresh_count == 0 and .mempalace.status == "optional" and .mempalace.required == false '\'' >/dev/null <<<"$out"'

# Managed Termux PRoot topology: ccc-memory must inspect sqlite_exact instead
# of falsely requiring the native ~/.mempalace Chroma path (#867).
termux_prefix="$TMP/data/data/com.termux/files/usr"
termux_base="$termux_prefix/var/lib/proot-distro/containers/ccc-mempalace/rootfs/opt/ccc-mempalace"
termux_meta="$probe_nunchi/termux-mempalace/status.json"
termux_db="$termux_base/palace/sqlite_exact.sqlite3"
mkdir -p "$termux_base/palace" "$(dirname "$termux_meta")"
chmod 700 "$termux_base" "$(dirname "$termux_meta")"
printf '%s\n' 'ccc-node #867 managed container' > "$termux_base/.ccc-node-managed"
printf '%s\n' '{"schema":"ccc.termux-mempalace.install.v1","enabled":true,"provider":"codex","source":"/redacted","state":"ready","container":"ccc-mempalace","version":"3.6.0","updated_at":190}' > "$termux_meta"
chmod 600 "$termux_base/.ccc-node-managed" "$termux_meta"
python3 - "$termux_db" <<'PY'
import sqlite3, sys
db=sqlite3.connect(sys.argv[1])
db.execute("CREATE TABLE documents(id TEXT PRIMARY KEY, document TEXT NOT NULL)")
db.executemany("INSERT INTO documents VALUES (?, ?)", [("one", "PROBE_TERMUX_SECRET"), ("two", "hidden")])
db.commit(); db.close()
PY
out="$(HOME="$probe_home" PREFIX="$termux_prefix" \
  CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" \
  bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check recognizes the managed Termux sqlite_exact palace" \
  '[ "$rc" = 0 ] && jq -e '\'' .nunchi.status == "ok" and .mempalace.status == "ok" and .mempalace.backend == "sqlite_exact" and .mempalace.integrity == "ok" and .mempalace.embeddings == 2 and .mempalace.index == {status:"ok",sqlite_count:2,hnsw_count:2,divergence:0} '\'' >/dev/null <<<"$out" && ! grep -q "PROBE_TERMUX_SECRET" <<<"$out"'
printf '%s\n' 'not the managed marker' > "$termux_base/.ccc-node-managed"
out="$(HOME="$probe_home" PREFIX="$termux_prefix" \
  CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" \
  bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "an unsafe Termux palace marker fails readiness closed" \
  '[ "$rc" = 0 ] && jq -e '\'' .mempalace.status == "degraded" and (.mempalace.reasons | index("palace-error")) != null and .mempalace.backend == "sqlite_exact" '\'' >/dev/null <<<"$out"'
rm -rf "$probe_nunchi/termux-mempalace"

optional_refresh_status="$TMP/optional-refresh.status.json"
for refresh_case in missing error stale provider; do
  rm -f "$optional_refresh_status"
  case "$refresh_case" in
    missing) expected_reason="refresh-missing"; case_ttl=21600 ;;
    error)
      expected_reason="refresh-error"; case_ttl=21600
      printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"error","exit_code":17,"started_at":180,"finished_at":190}' > "$optional_refresh_status"
      ;;
    stale)
      expected_reason="refresh-stale"; case_ttl=10
      printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":1,"finished_at":2}' > "$optional_refresh_status"
      ;;
    provider)
      expected_reason="refresh-provider"; case_ttl=21600
      printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"claude","state":"ok","exit_code":0,"started_at":180,"finished_at":190}' > "$optional_refresh_status"
      ;;
  esac
  out="$(HOME="$probe_home" PREFIX="/data/data/com.termux/files/usr" \
    CCC_NUNCHI_MEMPALACE_CLI="$TMP/missing-mempalace" \
    CCC_NUNCHI_MEMPALACE_STATUS="$optional_refresh_status" \
    CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
    CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
    CCC_MEMORY_CACHE_TTL_SEC="$case_ttl" CCC_MEMORY_CHECK_NOW_EPOCH=200 \
    CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
  ok "Termux configured refresh fails closed for $refresh_case state without its CLI" \
    '[ "$rc" = 0 ] && jq -e --arg reason "$expected_reason" '\'' .mempalace.status == "degraded" and (.mempalace.reasons | index($reason)) != null '\'' >/dev/null <<<"$out"'
done

out="$(HOME="$probe_home" CCC_NUNCHI_MEMPALACE_CLI="$TMP/missing-mempalace" \
  CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_CRONTAB_TEXT="$optional_cron" \
  bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "Linux still requires the managed MemPalace refresh contract" \
  '[ "$rc" = 0 ] && jq -e '\'' .nunchi.status == "degraded" and (.nunchi.reasons | index("refresh-count")) != null and .mempalace.status == "degraded" and .mempalace.required == true '\'' >/dev/null <<<"$out"'

printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"error","exit_code":17,"started_at":180,"finished_at":190,"detail":"PROBE_SECRET_REFRESH_ERROR"}' > "$probe_nunchi/mempalace-refresh.status.json"
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_CHECK_NOW_EPOCH=200 \
  CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "failed refresh diagnostics stay bounded and body-free" '[ "$rc" = 0 ] && jq -e '\''
  .mempalace.status == "degraded" and (.mempalace.reasons | index("refresh-error")) != null
  and .mempalace.refresh == {status:"error", provider:"codex", exit_code:17, age_seconds:10}
'\'' >/dev/null <<<"$out" && ! grep -q "PROBE_SECRET_REFRESH_ERROR" <<<"$out"'
printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":180,"finished_at":190}' > "$probe_nunchi/mempalace-refresh.status.json"

out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_CHECK_NOW_EPOCH=200 \
  CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_diverged" \
  CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check detects a readable SQLite palace with a diverged HNSW index" '[ "$rc" = 0 ] && jq -e '\''
  .mempalace.status == "degraded" and (.mempalace.reasons | index("index-diverged")) != null
  and .mempalace.index == {status:"diverged", sqlite_count:100, hnsw_count:10, divergence:90}
'\'' >/dev/null <<<"$out"'

PROBE_PY="$ROOT/scripts/ccc_memory_probe.py" python3 - <<'PY'
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

spec = importlib.util.spec_from_file_location("ccc_memory_probe", os.environ["PROBE_PY"])
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

def repair(text, expected):
    assert probe.parse_repair_status(text)["status"] == expected, (text, expected)

repair("", "unknown")
repair("Palace is initialized but empty", "empty")
repair("[drawers]\nsqlite count: 1\nstatus: OK", "partial")
repair("[drawers]\nsqlite count: 1\nhnsw count: 2\ndivergence: 1\nstatus: OK", "malformed")
repair("[drawers]\nsqlite count: -1\nhnsw count: 1\ndivergence: 0\nstatus: OK", "malformed")
repair("[drawers]\nsqlite count: 1\nsqlite count: 1\nhnsw count: 1\ndivergence: 0\nstatus: OK", "malformed")
repair("[drawers]\nsqlite count: 1,000\nhnsw count: 1,000\ndivergence: 0\nstatus: OK\n[closets]", "ok")
with mock.patch.object(probe.subprocess, "run", side_effect=subprocess.TimeoutExpired("mp", 5)):
    assert probe.mempalace_index_probe(Path("/mp"), Path("/"))["status"] == "timeout"
with mock.patch.object(probe.subprocess, "run", return_value=subprocess.CompletedProcess([], 9, "secret", "secret")):
    assert probe.mempalace_index_probe(Path("/mp"), Path("/"))["status"] == "error"

valid = {
    "schema": "ccc.nunchi.mempalace-refresh.v1", "provider": "codex",
    "state": "ok", "exit_code": 0, "started_at": 90, "finished_at": 100,
}
invalid = [
    {**valid, "state": "bogus"},
    {**valid, "provider": "bogus"},
    {**valid, "exit_code": True},
    {**valid, "started_at": True},
    {**valid, "finished_at": True},
    {**valid, "state": "running", "started_at": 0, "finished_at": 0, "exit_code": -1},
    {**valid, "state": "running", "finished_at": 100, "exit_code": -1},
    {**valid, "state": "running", "finished_at": 0, "exit_code": 0},
    {**valid, "started_at": 0},
    {**valid, "started_at": 101, "finished_at": 100},
    {**valid, "exit_code": 1},
    {**valid, "state": "error", "started_at": 0, "exit_code": 1},
    {**valid, "state": "error", "started_at": 101, "exit_code": 1},
    {**valid, "state": "error", "exit_code": 0},
    {**valid, "state": "error", "exit_code": 256},
    {**valid, "started_at": 501, "finished_at": 501},
]
with tempfile.TemporaryDirectory() as root:
    path = Path(root) / "status.json"
    for record in invalid:
        path.write_text(json.dumps(record), encoding="utf-8")
        assert probe.mempalace_refresh_probe(path, 200)["status"] == "invalid", record
    for record in (
        valid,
        {**valid, "state": "running", "finished_at": 0, "exit_code": -1},
        {**valid, "provider": "piri"},
        {**valid, "state": "error", "exit_code": 124},
        {**valid, "started_at": 500, "finished_at": 500},
    ):
        path.write_text(json.dumps(record), encoding="utf-8")
        assert probe.mempalace_refresh_probe(path, 200)["status"] == record["state"]
PY
rc=$?
ok "repair and refresh parsers reject every malformed state invariant" '[ "$rc" = 0 ]'

printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":180,"finished_at":190}' > "$probe_nunchi/mempalace-refresh.status.json"
for index_case in unknown empty partial malformed; do
  case "$index_case" in
    unknown) repair_case="" ;;
    empty) repair_case="Palace is initialized but empty" ;;
    partial) repair_case=$'[drawers]\nsqlite count: 1\nstatus: OK' ;;
    malformed) repair_case=$'[drawers]\nsqlite count: 1\nhnsw count: 2\ndivergence: 1\nstatus: OK' ;;
  esac
  out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
    CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_CHECK_NOW_EPOCH=200 \
    CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_case" \
    CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
  ok "memory check fails closed for $index_case drawer readiness" \
    '[ "$rc" = 0 ] && jq -e --arg reason "index-'"$index_case"'" '\'' .mempalace.status == "degraded" and (.mempalace.reasons | index($reason)) != null '\'' >/dev/null <<<"$out"'
done

legacy_cron=$'*/10 * * * * bash /tmp/codex-feed.sh # nunchi:#816\n17 * * * * mempalace sweep /tmp/sessions # nunchi:#816\n7 8 * * 1 bash /tmp/bench.sh # nunchi:#816'
wrong_provider_cron=$'*/10 * * * * bash /tmp/codex-feed.sh # nunchi:#816\n17 * * * * bash /tmp/mempalace-refresh.sh claude /tmp/sessions # nunchi:#816\n7 8 * * 1 bash /tmp/bench.sh # nunchi:#816'
missing_arg_cron=$'*/10 * * * * bash /tmp/codex-feed.sh # nunchi:#816\n17 * * * * bash /tmp/mempalace-refresh.sh # nunchi:#816\n7 8 * * 1 bash /tmp/bench.sh # nunchi:#816'
decoy_cron=$'*/10 * * * * bash /tmp/codex-feed.sh # nunchi:#816\n17 * * * * bash /tmp/mempalace-refresh.sh codex /tmp/sessions # nunchi:#816-decoy\n7 8 * * 1 bash /tmp/bench.sh # nunchi:#816'
for cron_case in legacy wrong missing decoy; do
  case "$cron_case" in
    legacy) bad_cron="$legacy_cron"; reason="legacy-sweep" ;;
    wrong) bad_cron="$wrong_provider_cron"; reason="refresh-provider-arg" ;;
    missing) bad_cron="$missing_arg_cron"; reason="refresh-provider-arg" ;;
    decoy) bad_cron="$decoy_cron"; reason="refresh-count" ;;
  esac
  out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
    CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_CHECK_NOW_EPOCH=200 \
    CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
    CCC_NUNCHI_CRONTAB_TEXT="$bad_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
  ok "memory check rejects $cron_case managed refresh cron" \
    '[ "$rc" = 0 ] && jq -e --arg reason "'"$reason"'" '\'' .nunchi.status == "degraded" and (.nunchi.reasons | index($reason)) != null '\'' >/dev/null <<<"$out"'
done

operator_cron="$probe_cron"$'\n43 4 * * * /opt/operator/mempalace sweep /srv/operator-archive'
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_CHECK_NOW_EPOCH=200 \
  CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$operator_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check ignores unrelated operator MemPalace sweep jobs" \
  '[ "$rc" = 0 ] && jq -e '\'' .nunchi.status == "ok" and .mempalace.status == "ok" '\'' >/dev/null <<<"$out"'

rm -f "$probe_nunchi/mempalace-refresh.status.json"
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_CHECK_NOW_EPOCH=200 \
  CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "a recently touched palace cannot mask a missing managed Codex refresh" \
  '[ "$rc" = 0 ] && jq -e '\'' .mempalace.status == "degraded" and (.mempalace.reasons | index("refresh-missing")) != null '\'' >/dev/null <<<"$out"'

custom_refresh_status="$TMP/custom-refresh.status.json"
printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":180,"finished_at":190}' > "$custom_refresh_status"
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_CHECK_NOW_EPOCH=200 \
  CCC_NUNCHI_MEMPALACE_STATUS="$custom_refresh_status" \
  CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory probe honors the custom refresh status path" \
  '[ "$rc" = 0 ] && jq -e '\'' .mempalace.status == "ok" and .mempalace.refresh.age_seconds == 10 '\'' >/dev/null <<<"$out"'

cron_refresh_status="$TMP/cron-config-refresh.status.json"
printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"codex","state":"ok","exit_code":0,"started_at":180,"finished_at":190}' > "$cron_refresh_status"
cron_configured_refresh=$'*/10 * * * * bash /tmp/codex-feed.sh # nunchi:#816\n17 * * * * CCC_NUNCHI_MEMPALACE_STATUS='"$cron_refresh_status"' CCC_NUNCHI_MEMPALACE_CLI='"$probe_home/.local/bin/mempalace"' bash /tmp/mempalace-refresh.sh codex /tmp/sessions # nunchi:#816\n7 8 * * 1 bash /tmp/bench.sh # nunchi:#816'
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_CHECK_NOW_EPOCH=200 \
  CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$cron_configured_refresh" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory probe reuses CLI and status paths persisted in the managed cron" \
  '[ "$rc" = 0 ] && jq -e '\'' .mempalace.status == "ok" and .mempalace.refresh.age_seconds == 10 '\'' >/dev/null <<<"$out"'

claude_probe_cron=$'*/10 * * * * bash /tmp/ingest-cron.sh # nunchi:#816\n17 * * * * bash /tmp/mempalace-refresh.sh claude /tmp/projects # nunchi:#816\n7 8 * * 1 bash /tmp/bench.sh # nunchi:#816'
printf '%s\n' '{"schema":"ccc.nunchi.mempalace-refresh.v1","provider":"claude","state":"ok","exit_code":0,"started_at":180,"finished_at":190}' > "$probe_nunchi/mempalace-refresh.status.json"
printf '%s\n' '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"bash /tmp/hooks/nunchi/sessionstart.sh"}]}]}}' > "$probe_claude/settings.local.json"
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$claude_probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check accepts exactly one standalone hook for a Claude feed" '[ "$rc" = 0 ] && jq -e '\''
  .nunchi.status == "ok" and .nunchi.cron.feed == "claude"
  and .nunchi.standalone_sessionstart_hooks == 1
'\'' >/dev/null <<<"$out"'
python3 - "$probe_palace/chroma.sqlite3" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1]); db.execute("DELETE FROM embeddings"); db.commit(); db.close()
PY
printf '%s\n' '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"bash /root/nunchi/sessionstart.sh"}]}]}}' > "$probe_claude/settings.local.json"
out="$(HOME="$probe_home" CCC_CLAUDE_DIR="$probe_claude" CCC_STATE_DIR="$probe_state" \
  CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
  CCC_MEMORY_CHECK_NOW_EPOCH=200 CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT="$repair_ok" \
  CCC_NUNCHI_CRONTAB_TEXT="$probe_cron" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check flags legacy injection and an empty palace" '[ "$rc" = 0 ] && jq -e '\''
  .nunchi.status == "degraded" and (.nunchi.reasons | index("standalone-sessionstart")) != null
  and .mempalace.status == "degraded" and (.mempalace.reasons | index("embeddings-empty")) != null
'\'' >/dev/null <<<"$out"'

missing_journal="$TMP/missing-distill-journal"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_DISTILL_JOURNAL_DIR="$missing_journal" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check reports a missing write-back queue without creating it" '[ "$rc" = 0 ] && jq -e '\''
  .writeback_queue == {
    status:"missing", jobs:0, pending_jobs:0, invalid_records:0,
    record_bytes:0, snapshot_bytes:0,
    oldest_age_seconds:-1, oldest_pending_age_seconds:-1,
    retries:{snapshot:0, extraction:0, local:0, wiki:0, total:0},
    accounting:{accounted_attempts:0, turn_bytes:0, duration_ms:0, estimated_max_tokens:0, model_counts:{}},
    status_counts:{}, local_status_counts:{}, wiki_status_counts:{}
  }'\'' >/dev/null <<<"$out" && [ ! -e "$missing_journal" ]'

empty_journal="$TMP/empty-distill-journal"
mkdir -m 700 "$empty_journal"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_DISTILL_JOURNAL_DIR="$empty_journal" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check reports an empty write-back queue" '[ "$rc" = 0 ] && jq -e '\''.writeback_queue.status == "empty" and .writeback_queue.jobs == 0 and .writeback_queue.invalid_records == 0'\'' >/dev/null <<<"$out"'

journal="$TMP/distill-journal"
mkdir -m 700 "$journal"
queued_id="$(printf 'a%.0s' {1..64})"
done_id="$(printf 'b%.0s' {1..64})"
retry_id="$(printf 'c%.0s' {1..64})"
secret_thread="RAW_THREAD_ID_MUST_NOT_LEAK"
secret_message="RAW_TRANSCRIPT_BODY_MUST_NOT_LEAK"
secret_output="RAW_EXTRACTION_OUTPUT_MUST_NOT_LEAK"
cat > "$journal/$queued_id.json" <<JSON
{"job_id":"$queued_id","provider":"codex","thread_id":"$secret_thread","thread_hash":"$(printf '1%.0s' {1..64})","trigger":"checkpoint","status":"queued","created_at":"1970-01-01T00:01:20.123456Z","updated_at":"1970-01-01T00:01:30Z","attempts":1,"extraction_attempts":0,"local_sink_attempts":0,"snapshot":{"byte_count":120,"messages":[{"role":"user","text":"$secret_message"}]}}
JSON
cat > "$journal/$done_id.json" <<JSON
{"job_id":"$done_id","provider":"codex","thread_id":"$secret_thread-done","thread_hash":"$(printf '2%.0s' {1..64})","trigger":"explicit","status":"extraction_done","created_at":"1970-01-01T00:01:40Z","updated_at":"1970-01-01T00:01:50Z","attempts":1,"extraction_attempts":1,"extraction_accounting":[{"model":"gpt-5-mini","snapshot_bytes":200,"duration_ms":1250,"estimated_max_tokens":75000}],"local_sink_status":"done","local_sink_attempts":1,"snapshot":{"byte_count":200,"messages":[{"role":"assistant","text":"$secret_message"}]},"extraction_output":"$secret_output","memory_scope":"private-deadbeefdeadbeefdeadbeefdeadbeef"}
JSON
cat > "$journal/$retry_id.json" <<JSON
{"job_id":"$retry_id","provider":"codex","thread_id":"$secret_thread-retry","thread_hash":"$(printf '3%.0s' {1..64})","trigger":"shutdown","status":"extraction_done","created_at":"1970-01-01T00:02:00Z","updated_at":"1970-01-01T00:02:10Z","attempts":2,"extraction_attempts":3,"extraction_accounting":[{"model":"gpt-5-mini","snapshot_bytes":300,"duration_ms":1000,"estimated_max_tokens":75000},{"model":"gpt-5-mini","snapshot_bytes":300,"duration_ms":1500,"estimated_max_tokens":75000}],"local_sink_status":"retryable_failed","local_sink_attempts":4,"snapshot":{"byte_count":300,"messages":[{"role":"user","text":"$secret_message"}]},"extraction_output":"$secret_output","memory_scope":"private-feedfacefeedfacefeedfacefeedface"}
JSON
printf '{malformed %s\n' "$secret_message" > "$journal/$(printf 'd%.0s' {1..64}).json"
printf '{"thread_id":"%s"}\n' "$secret_thread-symlink" > "$TMP/symlink-target.json"
ln -s "$TMP/symlink-target.json" "$journal/$(printf 'e%.0s' {1..64}).json"
oversized_path="$journal/$(printf 'f%.0s' {1..64}).json"
python3 - "$oversized_path" <<'PY'
from pathlib import Path
import sys
Path(sys.argv[1]).write_bytes(b"x" * (1024 * 1024 + 1))
PY
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_DISTILL_JOURNAL_DIR="$journal" CCC_MEMORY_CHECK_NOW_EPOCH=200 bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check aggregates active and degraded write-back state" '[ "$rc" = 0 ] && jq -e '\''
  .writeback_queue.status == "degraded"
  and .writeback_queue.jobs == 3
  and .writeback_queue.pending_jobs == 2
  and .writeback_queue.invalid_records == 3
  and .writeback_queue.record_bytes > 1048576
  and .writeback_queue.snapshot_bytes == 620
  and .writeback_queue.oldest_age_seconds == 120
  and .writeback_queue.oldest_pending_age_seconds == 120
  and .writeback_queue.retries == {snapshot:4, extraction:4, local:5, wiki:0, total:13}
  and .writeback_queue.accounting == {accounted_attempts:3, turn_bytes:800, duration_ms:3750, estimated_max_tokens:225000, model_counts:{"gpt-5-mini":3}}
  and .writeback_queue.status_counts == {queued:1, extraction_done:2}
  and .writeback_queue.local_status_counts == {done:1, retryable_failed:1}
  and .writeback_queue.wiki_status_counts == {}
'\'' >/dev/null <<<"$out"'
ok "memory check write-back JSON never exposes journal bodies or identities" '! grep -q "$secret_thread\|$secret_message\|$secret_output\|private-deadbeef\|private-feedface" <<<"$out"'
text_out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_DISTILL_JOURNAL_DIR="$journal" CCC_MEMORY_CHECK_NOW_EPOCH=200 bash "$ROOT/scripts/ccc-memory-check.sh" text 2>&1)"; rc=$?
ok "memory check text reports one body-free write-back aggregate" '[ "$rc" = 0 ] && [ "$(grep -c "^- writeback:" <<<"$text_out")" = 1 ] && grep -q "status=degraded jobs=3 pending=2 invalid=3.*oldest=120s.*retries=13" <<<"$text_out" && ! grep -q "$secret_thread\|$secret_message\|$secret_output\|private-deadbeef\|private-feedface" <<<"$text_out"'

active_journal="$TMP/active-distill-journal"
mkdir -m 700 "$active_journal"
cp "$journal/$queued_id.json" "$active_journal/$queued_id.json"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_DISTILL_JOURNAL_DIR="$active_journal" CCC_MEMORY_CHECK_NOW_EPOCH=200 bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check reports a healthy pending write-back queue as active" '[ "$rc" = 0 ] && jq -e '\''.writeback_queue.status == "active" and .writeback_queue.jobs == 1 and .writeback_queue.pending_jobs == 1'\'' >/dev/null <<<"$out"'

settled_journal="$TMP/settled-distill-journal"
mkdir -m 700 "$settled_journal"
cp "$journal/$done_id.json" "$settled_journal/$done_id.json"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_DISTILL_JOURNAL_DIR="$settled_journal" CCC_MEMORY_CHECK_NOW_EPOCH=200 bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check reports a completed write-back queue as settled" '[ "$rc" = 0 ] && jq -e '\''.writeback_queue.status == "settled" and .writeback_queue.jobs == 1 and .writeback_queue.pending_jobs == 0'\'' >/dev/null <<<"$out"'

# All-valid multi-file journal: the single batched jq pass (no malformed file
# forcing the per-file fallback) must aggregate the same per-file projections.
valid_journal="$TMP/valid-batch-distill-journal"
mkdir -m 700 "$valid_journal"
cp "$journal/$queued_id.json" "$valid_journal/$queued_id.json"
cp "$journal/$done_id.json" "$valid_journal/$done_id.json"
cp "$journal/$retry_id.json" "$valid_journal/$retry_id.json"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_DISTILL_JOURNAL_DIR="$valid_journal" CCC_MEMORY_CHECK_NOW_EPOCH=200 bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check batched pass aggregates an all-valid multi-file queue" '[ "$rc" = 0 ] && jq -e '\''
  .writeback_queue.status == "degraded"
  and .writeback_queue.jobs == 3
  and .writeback_queue.pending_jobs == 2
  and .writeback_queue.invalid_records == 0
  and .writeback_queue.snapshot_bytes == 620
  and .writeback_queue.oldest_age_seconds == 120
  and .writeback_queue.retries == {snapshot:4, extraction:4, local:5, wiki:0, total:13}
  and .writeback_queue.accounting.accounted_attempts == 3
  and .writeback_queue.status_counts == {queued:1, extraction_done:2}
  and .writeback_queue.local_status_counts == {done:1, retryable_failed:1}
'\'' >/dev/null <<<"$out"'

codex_home="$TMP/codex-home"
CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CODEX_HOME="$codex_home" python3 "$ROOT/scripts/ccc_codex_memory.py" materialize --json >/dev/null 2>&1
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CODEX_HOME="$codex_home" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check exposes body-free ready Codex snapshot diagnostics" '[ "$rc" = 0 ] && jq -e '\''.codex.status == "ready" and .codex.metadata_status == "ok" and (.codex.snapshot_sha256 | length) == 64 and .codex.active_kind == "base"'\'' >/dev/null <<<"$out" && ! grep -q "allowed operation policy\|user likes concise" <<<"$out"'
rm -rf "$codex_home"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CODEX_HOME="$codex_home" bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check reports a missing Codex snapshot without creating CODEX_HOME" '[ "$rc" = 0 ] && jq -e '\''.codex.status == "missing"'\'' >/dev/null <<<"$out" && [ ! -e "$codex_home" ]'

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "external isolation overrides an explicit Wiki enable in diagnostics" '[ "$rc" = 0 ] && jq -e ".wiki.status == \"disabled\"" >/dev/null <<<"$out"'

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_WIKI_MEMORY_ENABLED=FALSE bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check reports Wiki disabled despite a stale cache file" '[ "$rc" = 0 ] && jq -e ".wiki.status == \"disabled\"" >/dev/null <<<"$out"'

secret_a="VALUE_SHOULD_NOT_INDEX_A"
secret_b="VALUE_SHOULD_NOT_INDEX_B"
secret_c="VALUE_SHOULD_NOT_INDEX_C"
printf 'Authorization: Bearer %s\n' "$secret_a" >> "$mem/MEMORY.md"
printf 'api_key: %s\n' "$secret_b" >> "$mem/MEMORY.md"
printf 'https://x.test/?access_token=%s\n' "$secret_c" >> "$mem/MEMORY.md"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild 2>&1)"; rc=$?
ok "memory index rebuild succeeds" '[ "$rc" = 0 ] && jq -e ".ok == true and .documents >= 2 and .distill_indexed == false" >/dev/null <<<"$out"'
mode="$(python3 - <<PY
import os, stat
p='$state/memory-index.sqlite'
print(oct(stat.S_IMODE(os.stat(p).st_mode)) if os.path.exists(p) else 'missing')
PY
)"
ok "memory index db is chmod 600" '[ "$mode" = "0o600" ]'
db_dump="$(python3 - <<PY
import sqlite3
con=sqlite3.connect('$state/memory-index.sqlite')
try:
    print('\n'.join(row[0] for row in con.execute('select content from memory_docs')))
finally:
    con.close()
PY
)"
ok "memory index redacts bearer/key/url secrets" '! grep -q "VALUE_SHOULD_NOT_INDEX_A\|VALUE_SHOULD_NOT_INDEX_B\|VALUE_SHOULD_NOT_INDEX_C" <<<"$db_dump"'

# A pre-disable opt-in index can contain valid/malformed distill artifacts. The
# external boundary must hide them immediately, before the next cleanup, and a
# disabled update must fail closed rather than indexing malformed raw JSON.
mkdir -p "$state/distill-history"
printf '%s\n' '{"honcho":[{"text":"STALE_VALID_HONCHO_KEEP"}],"wiki_candidates":[{"summary":"STALE_VALID_WIKI_DROP"}]}' > "$state/distill-history/stale-valid.json"
CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_INDEX_DISTILL=1 bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
out="$(CCC_STATE_DIR="$state" CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 bash "$ROOT/scripts/ccc-memory-search.sh" 'STALE_VALID_WIKI_DROP' 2>&1)"; rc=$?
ok "external search immediately hides stale valid distill-history rows" '[ "$rc" = 0 ] && jq -e "(.results | length) == 0" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_QUERY='STALE_VALID_WIKI_DROP' CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "external SessionStart immediately hides stale valid distill-history rows" '[ "$rc" = 0 ] && ! grep -q "STALE_VALID_WIKI_DROP\|stale-valid.json" <<<"$out"'
printf '%s\n' '{"wiki_candidates":[{"summary":"MALFORMED_HISTORY_WIKI_DROP"}]' > "$state/distill-history/malformed.json"
CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_INDEX_DISTILL=1 CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 bash "$ROOT/scripts/ccc-memory-index.sh" update >/dev/null 2>&1
malformed_dump="$(python3 - <<PY
import sqlite3
con=sqlite3.connect('$state/memory-index.sqlite')
try:
    print('\n'.join((row[0] or '')+' '+(row[1] or '') for row in con.execute('select path,content from memory_docs')))
finally:
    con.close()
PY
)"
ok "external update drops malformed distill-history JSON instead of indexing raw text" '! grep -q "MALFORMED_HISTORY_WIKI_DROP\|malformed.json" <<<"$malformed_dump"'
out="$(CCC_STATE_DIR="$state" CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 bash "$ROOT/scripts/ccc-memory-search.sh" 'MALFORMED_HISTORY_WIKI_DROP' 2>&1)"; rc=$?
ok "external search cannot surface malformed distill-history after update" '[ "$rc" = 0 ] && jq -e "(.results | length) == 0" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$state" CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 bash "$ROOT/scripts/ccc-memory-search.sh" 'STALE_VALID_HONCHO_KEEP' 2>&1)"; rc=$?
ok "external search preserves sanitized local Honcho distill after update" '[ "$rc" = 0 ] && jq -e "[.results[] | select(.source == \"distill-local\" and (.path | endswith(\"/stale-valid.json\")))] | length >= 1" >/dev/null <<<"$out"'
rm -rf "$state/distill-history"
CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1

out="$(CCC_STATE_DIR="$state" CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 bash "$ROOT/scripts/ccc-memory-search.sh" 'hybrid memory profile' 2>&1)"; rc=$?
ok "external search filters a stale Wiki row before index cleanup" '[ "$rc" = 0 ] && jq -e "[.results[] | select(.path | endswith(\"/wiki.txt\"))] | length == 0" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 bash "$ROOT/scripts/ccc-memory-explain.sh" --json --query 'hybrid memory profile' 2>&1)"; rc=$?
ok "external memory explain reports zero Wiki budget and no stale Wiki result" '[ "$rc" = 0 ] && jq -e ".budgets.wiki == 0 and ([.search.results[] | select(.path | endswith(\"/wiki.txt\"))] | length == 0)" >/dev/null <<<"$out"'

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 bash "$ROOT/scripts/ccc-memory-index.sh" update 2>&1)"; rc=$?
indexed_paths="$(python3 - <<PY
import sqlite3
con=sqlite3.connect('$state/memory-index.sqlite')
try:
    print('\n'.join(row[0] for row in con.execute('select path from memory_docs order by path')))
finally:
    con.close()
PY
)"
ok "wiki-disabled index update reports effective policy" '[ "$rc" = 0 ] && jq -e ".wiki_enabled == false" >/dev/null <<<"$out"'
ok "wiki-disabled index removes stale Wiki rows" '! grep -q "/wiki.txt$" <<<"$indexed_paths" && grep -q "/MEMORY.md$" <<<"$indexed_paths"'
printf '%s\n' '{"honcho":[{"text":"HONCHO_DISTILL_KEEP"}],"wiki_candidates":[{"summary":"WIKI_DISTILL_DROP"}]}' > "$state/distill-last.json"
printf 'WIKI_QUEUE_DROP\n' > "$state/wiki-candidates.md"
mkdir -p "$state/distill-history"
printf '%s\n' '{"honcho":[{"text":"HONCHO_HISTORY_KEEP"}],"wiki_candidates":[{"summary":"WIKI_HISTORY_DROP"}]}' > "$state/distill-history/one.json"
CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_INDEX_DISTILL=1 CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
distill_dump="$(python3 - <<PY
import sqlite3
con=sqlite3.connect('$state/memory-index.sqlite')
try:
    print('\n'.join(row[0] for row in con.execute('select content from memory_docs')))
finally:
    con.close()
PY
)"
ok "wiki-disabled opt-in distill index strips Wiki queue/history fields" 'grep -q "HONCHO_DISTILL_KEEP\|HONCHO_HISTORY_KEEP" <<<"$distill_dump" && ! grep -q "WIKI_DISTILL_DROP\|WIKI_QUEUE_DROP\|WIKI_HISTORY_DROP" <<<"$distill_dump"'
rm -rf "$state/distill-last.json" "$state/wiki-candidates.md" "$state/distill-history"
CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" bash "$ROOT/scripts/ccc-memory-index.sh" update >/dev/null 2>&1

old_state="$TMP/old-state"
old_cache="$TMP/old-cache"
old_mem="$TMP/old-memories"
old_marker="LEAK_MARKER_OLD_DB_BYTES_SHOULD_DISAPPEAR"
mkdir -p "$old_state" "$old_cache" "$old_mem"
printf 'clean replacement memory\n' > "$old_mem/MEMORY.md"
printf 'clean replacement user\n' > "$old_mem/USER.md"
python3 - <<PY
import sqlite3
marker = '$old_marker'
con = sqlite3.connect('$old_state/memory-index.sqlite')
con.execute('CREATE TABLE memory_docs (source TEXT NOT NULL, path TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
con.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(path UNINDEXED, source UNINDEXED, content)')
content = marker * 400
con.execute('INSERT INTO memory_docs(source,path,content) VALUES(?,?,?)', ('old', '/tmp/old', content))
con.execute('INSERT INTO memory_fts(path,source,content) VALUES(?,?,?)', ('/tmp/old', 'old', content))
con.commit(); con.close()
PY
out="$(CCC_STATE_DIR="$old_state" CCC_MEMORY_CACHE_DIR="$old_cache" CCC_MEMORY_DIR="$old_mem" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild 2>&1)"; rc=$?
old_marker_present="$(python3 - <<PY
from pathlib import Path
print('yes' if b'$old_marker' in Path('$old_state/memory-index.sqlite').read_bytes() else 'no')
PY
)"
ok "memory index rebuild scrubs old raw db bytes" '[ "$rc" = 0 ] && [ "$old_marker_present" = "no" ]'

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_DISABLE_FTS5=1 bash "$ROOT/scripts/ccc-memory-index.sh" rebuild 2>&1)"; rc=$?
ok "memory index degrades to docs-only when FTS5 is disabled" '[ "$rc" = 0 ] && jq -e ".ok == true and .fts5_enabled == false" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" 'concise Korean reports' 2>&1)"; rc=$?
ok "memory search LIKE fallback works when FTS5 is unavailable" '[ "$rc" = 0 ] && jq -e ".results | length > 0" >/dev/null <<<"$out"'

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" 'practical evidence reports' 2>&1)"; rc=$?
ok "memory search finds cache docs" '[ "$rc" = 0 ] && jq -e ".results | length > 0" >/dev/null <<<"$out"'

out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_PROFILE=hybrid CCC_LOCAL_MEMORY_ENABLED=1 CCC_MEMORY_QUERY=Honcho bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "load-memory emits hook json with bounded context" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext | contains(\"Local hot memory\")" >/dev/null <<<"$out"'

# Directly exercise the hook via a tiny budget and Korean memory; JSON must still parse.
printf '가나다라마바사아자차카타파하\n' > "$mem/USER.md"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_MAX_BYTES=90 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "load-memory byte budget remains valid JSON for UTF-8 text" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext" >/dev/null <<<"$out"'

resume_json="$TMP/resume.json"
cat > "$resume_json" <<'JSON'
{"resume":{"last_activity":"하트비트/ETA 배포 승인 대기","pending_action":"5대 노드 업그레이드 진행","awaiting_user":true,"open_question":"배포 진행할까요?","next_step":"승인 후 PR merge SHA 배포","evidence":["#233","be4a60c"]}}
JSON
out="$(CCC_STATE_DIR="$state" bash "$ROOT/claude/hooks/distill/resume-write.sh" < "$resume_json" 2>&1)"; rc=$?
ok "resume-write creates fixed-schema resume pointer" '[ "$rc" = 0 ] && [ -f "$state/resume.md" ] && grep -q "다음 액션: 5대 노드 업그레이드 진행" "$state/resume.md"'
resume_before="$(cat "$state/resume.md")"
cat > "$TMP/empty-resume.json" <<'JSON'
{"resume":{"last_activity":"","pending_action":"","awaiting_user":false,"open_question":"","next_step":"","evidence":[]}}
JSON
CCC_STATE_DIR="$state" bash "$ROOT/claude/hooks/distill/resume-write.sh" < "$TMP/empty-resume.json" >/dev/null 2>&1
ok "resume-write preserves previous resume on empty resume object" '[ "$(cat "$state/resume.md")" = "$resume_before" ]'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_NO_REFRESH=1 bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "load-memory injects resume pointer at the top of session context" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext | startswith(\"# test-node session memory\") and contains(\"▶ 직전 세션에서 이어서:\") and contains(\"배포 진행할까요?\")" >/dev/null <<<"$out"'

caller_state="$TMP/caller-state"
mkdir -p "$caller_state"
printf 'keep\n' > "$caller_state/marker.txt"

query_state="$TMP/query-state"
query_cwd="$TMP/query-repo"
mkdir -p "$query_state" "$query_cwd"
printf 'query-node\n' > "$query_state/node.txt"
printf '%s\n' "$query_cwd" > "$query_state/cwd.txt"
printf 'Implement issue 186 memory roadmap with Honcho cache TTL\n' > "$query_state/current-task.txt"
( cd "$query_cwd" && git init -q && git config user.email test@example.invalid && git config user.name test && printf 'x\n' > changed-memory-file.txt && git add changed-memory-file.txt && git commit -q -m init && printf 'changed\n' >> changed-memory-file.txt )
query_git_cache="$TMP/git-status-cache"
out="$(CCC_STATE_DIR="$query_state" CCC_GIT_STATUS_CACHE_DIR="$query_git_cache" CCC_MEMORY_QUERY_EXTRA='Authorization: Bearer QUERY_SECRET_SHOULD_NOT_LEAK' bash "$ROOT/scripts/ccc-memory-query.sh" --mode remote 2>&1)"; rc=$?
ok "memory query helper builds redacted task-aware query" '[ "$rc" = 0 ] && grep -q "issue 186" <<<"$out" && grep -q "changed-memory-file.txt" <<<"$out" && ! grep -q "QUERY_SECRET_SHOULD_NOT_LEAK" <<<"$out"'

# --- #1484: git state reuses statusline.sh's 5s TSV cache ---------------------
# The query runs on the SessionStart critical path and again from the detached
# refresh; each paid two git forks. Count them with a PATH shim.
query_git_bin="$TMP/query-git-bin"; mkdir -p "$query_git_bin"
cat > "$query_git_bin/git" <<EOF
#!/usr/bin/env bash
printf 'git\n' >> "\${QUERY_GIT_COUNT:?}"
exec "$(command -v git)" "\$@"
EOF
chmod +x "$query_git_bin/git"
run_query_counted() { # [env...] — prints the query; git forks land in $QUERY_GIT_COUNT
  PATH="$query_git_bin:$PATH" QUERY_GIT_COUNT="$TMP/query-git-count" CCC_STATE_DIR="$query_state" \
    CCC_WORKTREE="$query_cwd" CCC_GIT_STATUS_CACHE_DIR="$query_git_cache" env "$@" \
    bash "$ROOT/scripts/ccc-memory-query.sh" --mode local 2>/dev/null
}
query_key="${query_cwd//[!a-zA-Z0-9]/_}"
rm -rf "$query_git_cache"; : > "$TMP/query-git-count"
q_cold="$(run_query_counted CCC_GIT_STATUS_CACHE_TTL=5)"
n_cold="$(wc -l < "$TMP/query-git-count")"
ok "query: cold cache runs git and writes its own TSV row (ts, branch, paths)" \
  '[ "$n_cold" = 2 ] && grep -q "git_changed_paths: changed-memory-file.txt" <<<"$q_cold" && [ "$(cut -f2 "$query_git_cache/$query_key.query.tsv")" = "$(git -C "$query_cwd" branch --show-current)" ] && grep -q "changed-memory-file.txt" "$query_git_cache/$query_key.query.tsv"'
: > "$TMP/query-git-count"
q_warm="$(run_query_counted CCC_GIT_STATUS_CACHE_TTL=5)"
ok "query: a fresh own row answers with zero git forks and identical output" \
  '[ "$(wc -l < "$TMP/query-git-count")" = 0 ] && [ "$q_warm" = "$q_cold" ]'
# statusline.sh row: ts<TAB>branch<TAB>dirty. dirty=0 proves no changed paths.
rm -rf "$query_git_cache"; mkdir -p "$query_git_cache"
printf '%s\t%s\t%s\n' "$(date +%s)" "statusline-branch" "0" > "$query_git_cache/$query_key.tsv"
: > "$TMP/query-git-count"
q_sl="$(run_query_counted CCC_GIT_STATUS_CACHE_TTL=5)"
ok "query: a fresh statusline row with dirty=0 is reused (branch, no paths, no git)" \
  '[ "$(wc -l < "$TMP/query-git-count")" = 0 ] && grep -q "git_branch: statusline-branch" <<<"$q_sl" && ! grep -q "git_changed_paths" <<<"$q_sl"'
printf '%s\t%s\t%s\n' "$(date +%s)" "statusline-branch" "1" > "$query_git_cache/$query_key.tsv"
: > "$TMP/query-git-count"
q_dirty="$(run_query_counted CCC_GIT_STATUS_CACHE_TTL=5)"
ok "query: a dirty=1 statusline row cannot answer (may be untracked only) — git runs" \
  '[ "$(wc -l < "$TMP/query-git-count")" = 2 ] && grep -q "git_changed_paths: changed-memory-file.txt" <<<"$q_dirty" && ! grep -q "statusline-branch" <<<"$q_dirty"'
printf '%s\t%s\t%s\n' "$(( $(date +%s) - 60 ))" "stale-branch" "old-path" > "$query_git_cache/$query_key.query.tsv"
: > "$TMP/query-git-count"
q_stale="$(run_query_counted CCC_GIT_STATUS_CACHE_TTL=5)"
ok "query: an expired own row is recomputed, not trusted" \
  '[ "$(wc -l < "$TMP/query-git-count")" = 2 ] && ! grep -q "stale-branch\|old-path" <<<"$q_stale"'
ok "query: cache write is atomic (no tmp leftovers)" \
  '[ "$(find "$query_git_cache" -name "*.tsv.*" | wc -l)" = 0 ]'
# Bash-builtin file reads replaced four sed forks; the rendered fields must not move.
printf 'line1\nline2\n' > "$query_state/current-prompt.txt"
q_files="$(CCC_STATE_DIR="$query_state" CCC_WORKTREE="$query_cwd" CCC_GIT_STATUS_CACHE_DIR="$query_git_cache" bash "$ROOT/scripts/ccc-memory-query.sh" --mode local 2>/dev/null)"
ok "query: node/task/prompt fields still come from the first line / first 40 lines" \
  'grep -q "^task: Implement issue 186" <<<"$q_files" && grep -q "prompt: line1 line2;" <<<"$q_files" && grep -q "node: query-node;" <<<"$q_files"'
rm -f "$query_state/current-prompt.txt"

printf 'Implement issue 186 memory roadmap with Honcho cache TTL and changed-memory-file context\n' > "$mem/MEMORY.md"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild 2>&1)"; rc=$?
helper_query="$(CCC_STATE_DIR="$query_state" CCC_WORKTREE="$query_cwd" CCC_GIT_STATUS_CACHE_DIR="$query_git_cache" bash "$ROOT/scripts/ccc-memory-query.sh" --mode local)"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" "$helper_query" 2>&1)"; rc=$?
ok "memory search tolerates task-aware helper query punctuation" '[ "$rc" = 0 ] && jq -e "(.results | length) > 0 and (.tokens | index(\"honcho\") != null)" >/dev/null <<<"$out"'

facts="$state/memory-facts.jsonl"
printf '%s\n' \
  '{"id":"fact-current","kind":"preference","text":"Current ccc-node editor fixture is Helix.","entities":["ccc-node","Helix"],"tags":["temporal"],"durability":"durable","privacy":"private","review":"auto-local"}' \
  '{"id":"fact-volatile","kind":"task-progress","text":"Volatile task progress mentions Helix PR pending and should be demoted.","durability":"volatile","privacy":"private","review":"auto-local"}' \
  '{"id":"fact-secret","kind":"risk","text":"api_key: VALUE_SHOULD_NOT_INDEX_FACT","durability":"durable","privacy":"sensitive-redacted","review":"auto-local"}' \
  > "$facts"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_FACTS_FILE="$facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild 2>&1)"; rc=$?
ok "memory index includes structured facts" '[ "$rc" = 0 ] && jq -e ".documents >= 3" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" CCC_MEMORY_RETRIEVAL=hybrid-local bash "$ROOT/scripts/ccc-memory-search.sh" "current editor Helix" 2>&1)"; rc=$?
ok "hybrid-local search explains scoring signals" '[ "$rc" = 0 ] && jq -e ".retrievalMode == \"hybrid-local\" and (.results[0].signals.token_hits >= 1)" >/dev/null <<<"$out"'

# Recent distill lookup intentionally matches the structured metadata labels
# (`distilled text`).  The rendered snippet must still carry the actual fact,
# even when long metadata / fact prefixes put its unique value outside FTS5's
# small match-centred token window.
recent_state="$TMP/recent-structured-state"
recent_facts="$recent_state/memory-facts.jsonl"
mkdir -p "$recent_state"
printf '%s\n' \
  '{"id":"distill-long-prefix","kind":"preference","text":"This harmless durable preference includes a deliberately long explanatory prefix with enough ordinary words to exceed the metadata centred search snippet window before UNIQUE_RECENT_FACT_MARKER appears.","entities":["user","session","provider","memory","roundtrip"],"tags":["distilled","explicit"],"durability":"durable","privacy":"private","review":"auto-local","source":{"type":"distill","provider":"codex","thread_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","trigger":"explicit","schema_version":1}}' \
  > "$recent_facts"
CCC_STATE_DIR="$recent_state" CCC_MEMORY_FACTS_FILE="$recent_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
out="$(CCC_STATE_DIR="$recent_state" CCC_MEMORY_INDEX_DB="$recent_state/memory-index.sqlite" CCC_MEMORY_FUSION=0 bash "$ROOT/scripts/ccc-memory-search.sh" "distilled text" 2>&1)"; rc=$?
ok "recent structured lookup renders the durable fact body" '[ "$rc" = 0 ] && jq -e ".results | any(.snippet | contains(\"UNIQUE_RECENT_FACT_MARKER\"))" >/dev/null <<<"$out"'
CCC_STATE_DIR="$recent_state" CCC_MEMORY_FACTS_FILE="$recent_facts" CCC_MEMORY_DISABLE_FTS5=1 bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
out="$(CCC_STATE_DIR="$recent_state" CCC_MEMORY_INDEX_DB="$recent_state/memory-index.sqlite" CCC_MEMORY_FUSION=0 bash "$ROOT/scripts/ccc-memory-search.sh" "distilled text" 2>&1)"; rc=$?
ok "recent structured LIKE fallback renders the durable fact body" '[ "$rc" = 0 ] && jq -e ".results | any(.snippet | contains(\"UNIQUE_RECENT_FACT_MARKER\"))" >/dev/null <<<"$out"'

# Default retrieval must apply the durability/source boosts too (not raw bm25),
# so a keyword-dense volatile fact with EQUAL coverage can't outrank a durable
# one. Distinct fixture so the only differentiator is the boost.
rank_facts="$state/rank-facts.jsonl"
printf '%s\n' \
  '{"id":"durable-policy","kind":"decision","text":"durable operating policy memory ranking default mode evidence.","durability":"durable","privacy":"private","review":"auto-local"}' \
  '{"id":"volatile-dense","kind":"task-progress","text":"durable operating policy memory ranking default mode durable operating policy memory ranking default mode volatile draft pending.","durability":"volatile","privacy":"private","review":"auto-local"}' \
  > "$rank_facts"
CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_FACTS_FILE="$rank_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" "durable operating policy memory ranking" 2>&1)"; rc=$?
ok "default retrieval reranks with boosts (rerank/fusion mode)" '[ "$rc" = 0 ] && jq -e "(.retrievalMode == \"fts-rerank\") or (.retrievalMode == \"fusion-rrf\")" >/dev/null <<<"$out"'
ok "default retrieval demotes keyword-dense volatile below durable" '[ "$rc" = 0 ] && jq -e "(.results[0].path | contains(\"durable-policy\")) and (.results[0].signals.durability_penalty == 0) and ((.results | map(select(.path | contains(\"volatile-dense\")))[0].signals.durability_penalty) == -3.0)" >/dev/null <<<"$out"'
# Fusion lane: a char-ngram fuzzy lane recalls a doc when EVERY query token is
# typo'd/transposed so both FTS and the LIKE substring fallback miss it. Set
# CCC_MEMORY_FUSION=0 to fall back to the lexical lane only. Use an isolated
# state dir: earlier load-memory tests intentionally fire a detached background
# refresh against the main state, which can race this tiny one-doc fixture.
fuzz_state="$TMP/fuzz-state"
fuzz_cache="$fuzz_state/cache"
fuzz_mem="$fuzz_state/memories"
mkdir -p "$fuzz_cache" "$fuzz_mem"
printf 'fuzz fixture memory\n' > "$fuzz_mem/MEMORY.md"
printf 'fuzz fixture user\n' > "$fuzz_mem/USER.md"
fuzz_facts="$fuzz_state/fuzz-facts.jsonl"
printf '%s\n' \
  '{"id":"fuzzdoc","kind":"decision","text":"memory ranking default behaviour configuration.","durability":"durable","privacy":"private","review":"auto-local"}' \
  > "$fuzz_facts"
CCC_STATE_DIR="$fuzz_state" CCC_MEMORY_CACHE_DIR="$fuzz_cache" CCC_MEMORY_DIR="$fuzz_mem" CCC_MEMORY_FACTS_FILE="$fuzz_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
fuzz_q="memmory rankng behaviuor configuratoin"
out="$(CCC_STATE_DIR="$fuzz_state" CCC_MEMORY_INDEX_DB="$fuzz_state/memory-index.sqlite" CCC_MEMORY_FUSION=0 bash "$ROOT/scripts/ccc-memory-search.sh" "$fuzz_q" 2>&1)"; rc=$?
ok "lexical-only misses all-typo query" '[ "$rc" = 0 ] && jq -e "(.results | length) == 0" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$fuzz_state" CCC_MEMORY_INDEX_DB="$fuzz_state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" "$fuzz_q" 2>&1)"; rc=$?
ok "fusion fuzzy lane recalls all-typo query" '[ "$rc" = 0 ] && jq -e ".retrievalMode == \"fusion-rrf\" and (.results[0].path | contains(\"fuzzdoc\"))" >/dev/null <<<"$out"'

# Decay/forgetting: volatile facts past CCC_MEMORY_VOLATILE_TTL_DAYS are dropped
# at index time so stale working state stops surfacing; durable + undated facts
# never decay (fail-safe); TTL=0 disables decay entirely.
decay_state="$TMP/decay-state"
decay_cache="$decay_state/cache"
decay_mem="$decay_state/memories"
mkdir -p "$decay_cache" "$decay_mem"
printf 'decay fixture memory\n' > "$decay_mem/MEMORY.md"
printf 'decay fixture user\n' > "$decay_mem/USER.md"
decay_facts="$decay_state/decay-facts.jsonl"
OLD_TS="$(python3 -c 'from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
NEW_TS="$(python3 -c 'from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
printf '%s\n' \
  "{\"id\":\"decay-stale\",\"kind\":\"task-progress\",\"text\":\"stale ephemeral progress zalpha marker\",\"durability\":\"volatile\",\"observed_at\":\"$OLD_TS\",\"review\":\"auto-local\"}" \
  "{\"id\":\"decay-fresh\",\"kind\":\"task-progress\",\"text\":\"recent ephemeral progress zbeta marker\",\"durability\":\"volatile\",\"observed_at\":\"$NEW_TS\",\"review\":\"auto-local\"}" \
  "{\"id\":\"decay-durable\",\"kind\":\"decision\",\"text\":\"durable decision zgamma marker\",\"durability\":\"durable\",\"observed_at\":\"$OLD_TS\",\"review\":\"auto-local\"}" \
  "{\"id\":\"decay-undated\",\"kind\":\"task-progress\",\"text\":\"undated ephemeral progress zdelta marker\",\"durability\":\"volatile\",\"review\":\"auto-local\"}" \
  > "$decay_facts"
CCC_STATE_DIR="$decay_state" CCC_MEMORY_CACHE_DIR="$decay_cache" CCC_MEMORY_DIR="$decay_mem" CCC_MEMORY_FACTS_FILE="$decay_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
dq_has() {
  local out
  out="$(CCC_STATE_DIR="$decay_state" CCC_MEMORY_INDEX_DB="$decay_state/memory-index.sqlite" \
    bash "$ROOT/scripts/ccc-memory-search.sh" "$1" 2>/dev/null)" || return 1
  jq -e --arg marker "$1" '.results | any(.[]; (((.path // "") + " " + (.snippet // "")) | contains($marker)))' >/dev/null <<<"$out"
}
ok "decay drops stale volatile fact from index" '! dq_has "zalpha"'
ok "decay keeps recent volatile fact" 'dq_has "zbeta"'
ok "decay never forgets durable fact" 'dq_has "zgamma"'
ok "decay keeps undated volatile fact (fail-safe)" 'dq_has "zdelta"'
CCC_STATE_DIR="$decay_state" CCC_MEMORY_CACHE_DIR="$decay_cache" CCC_MEMORY_DIR="$decay_mem" CCC_MEMORY_FACTS_FILE="$decay_facts" CCC_MEMORY_VOLATILE_TTL_DAYS=0 bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
ok "TTL=0 disables decay (stale volatile returns)" 'dq_has "zalpha"'

# #871 slice 2 — typed retention: durability-class TTLs come from an
# inspectable policy table; guard kinds (constraint/procedure/decision) are
# NEVER age-expired even when mislabeled volatile (kept + counted); unknown
# kinds are kept conservatively + counted; a malformed policy file fails OPEN
# to defaults that reproduce legacy behavior. Diagnostics are body-free counts.
ret_state="$TMP/retention-state"
ret_cache="$ret_state/cache"
ret_mem="$ret_state/memories"
mkdir -p "$ret_cache" "$ret_mem"
printf 'retention fixture memory\n' > "$ret_mem/MEMORY.md"
printf 'retention fixture user\n' > "$ret_mem/USER.md"
ret_facts="$ret_state/retention-facts.jsonl"
printf '%s\n' \
  "{\"id\":\"ret-guard\",\"kind\":\"constraint\",\"text\":\"guarded safety rule qalpha marker\",\"durability\":\"volatile\",\"observed_at\":\"$OLD_TS\",\"review\":\"auto-local\"}" \
  "{\"id\":\"ret-session\",\"kind\":\"task-progress\",\"text\":\"session scratch qbeta marker\",\"durability\":\"session-only\",\"observed_at\":\"$OLD_TS\",\"review\":\"auto-local\"}" \
  "{\"id\":\"ret-week\",\"kind\":\"observation\",\"text\":\"week scale note qgamma marker\",\"durability\":\"week-scale\",\"observed_at\":\"$OLD_TS\",\"review\":\"auto-local\"}" \
  "{\"id\":\"ret-unknown\",\"kind\":\"mystery-kind\",\"text\":\"unknown kind note qdelta marker\",\"durability\":\"volatile\",\"observed_at\":\"$OLD_TS\",\"review\":\"auto-local\"}" \
  > "$ret_facts"
ret_out="$(CCC_STATE_DIR="$ret_state" CCC_MEMORY_CACHE_DIR="$ret_cache" CCC_MEMORY_DIR="$ret_mem" CCC_MEMORY_FACTS_FILE="$ret_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild 2>/dev/null)"
rq_has() {
  local out
  out="$(CCC_STATE_DIR="$ret_state" CCC_MEMORY_INDEX_DB="$ret_state/memory-index.sqlite" \
    bash "$ROOT/scripts/ccc-memory-search.sh" "$1" 2>/dev/null)" || return 1
  jq -e --arg marker "$1" '.results | any(.[]; (((.path // "") + " " + (.snippet // "")) | contains($marker)))' >/dev/null <<<"$out"
}
ok "guard kind (constraint) mislabeled volatile is never age-expired" 'rq_has "qalpha"'
ok "session-only durability expires past its table TTL" '! rq_has "qbeta"'
ok "week-scale durability survives past the volatile TTL" 'rq_has "qgamma"'
ok "unknown kind is kept conservatively (old + volatile)" 'rq_has "qdelta"'
ok "index summary reports body-free retention diagnostics" 'jq -e ".retention.guarded >= 1 and .retention.dropped >= 1 and .retention.unknown_kinds >= 1" >/dev/null <<<"$ret_out"'
CCC_STATE_DIR="$ret_state" CCC_MEMORY_CACHE_DIR="$ret_cache" CCC_MEMORY_DIR="$ret_mem" CCC_MEMORY_FACTS_FILE="$ret_facts" \
  CCC_MEMORY_RETENTION_POLICY="$TMP/does-not-exist.json" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
ok "missing policy file fails open (guard kind still kept via defaults)" 'rq_has "qalpha"'

# #871 valid-time semantics: observed_at and valid_from/valid_until are
# different axes. Boundary rule: valid_from inclusive, valid_until exclusive.
# current mode partitions expired facts below still-valid ones and excludes
# future-valid facts (demote/exclude, never delete); explicit as_of retrieves
# the point-in-time fact; malformed windows degrade to conservative keep.
temp_state="$TMP/temporal-state"
temp_cache="$temp_state/cache"
temp_mem="$temp_state/memories"
mkdir -p "$temp_cache" "$temp_mem"
printf 'temporal fixture memory\n' > "$temp_mem/MEMORY.md"
printf 'temporal fixture user\n' > "$temp_mem/USER.md"
temp_facts="$temp_state/temporal-facts.jsonl"
printf '%s\n' \
  '{"id":"t-old","kind":"decision","text":"release train departs at 10:00 zeta marker","durability":"durable","observed_at":"2025-06-01T00:00:00Z","valid_from":"2025-06-01T00:00:00Z","valid_until":"2026-06-01T00:00:00Z","review":"auto-local"}' \
  '{"id":"t-new","kind":"decision","text":"release train departs at 14:00 zeta marker","durability":"durable","observed_at":"2026-06-01T00:00:00Z","valid_from":"2026-06-01T00:00:00Z","review":"auto-local"}' \
  '{"id":"t-future","kind":"constraint","text":"embargo window zeta-future marker","durability":"durable","observed_at":"2026-06-02T00:00:00Z","valid_from":"2999-01-01T00:00:00Z","review":"auto-local"}' \
  '{"id":"t-broken","kind":"preference","text":"broken window zeta-broken marker","durability":"durable","observed_at":"2026-06-02T00:00:00Z","valid_from":"not-a-timestamp","review":"auto-local"}' \
  > "$temp_facts"
CCC_STATE_DIR="$temp_state" CCC_MEMORY_CACHE_DIR="$temp_cache" CCC_MEMORY_DIR="$temp_mem" CCC_MEMORY_FACTS_FILE="$temp_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
tsearch() {
  env CCC_STATE_DIR="$temp_state" CCC_MEMORY_INDEX_DB="$temp_state/memory-index.sqlite" "$@" bash "$ROOT/scripts/ccc-memory-search.sh" "release train departs zeta" 2>/dev/null
}
out="$(tsearch)"; rc=$?
ok "current mode partitions expired below still-valid" '[ "$rc" = 0 ] && jq -e '\''(.temporal.mode == "current") and (([.results[].path | contains(":t-new")] | index(true)) < ([.results[].path | contains(":t-old")] | index(true)))'\'' >/dev/null <<<"$out"'
ok "current mode excludes future valid_from" '[ "$rc" = 0 ] && jq -e '\''(.results | all(.path | contains(":t-future") | not)) and (.temporal.excluded >= 1)'\'' >/dev/null <<<"$out"'
ok "current mode keeps expired facts retrievable (demoted, not deleted)" '[ "$rc" = 0 ] && jq -e '\''.results | any(.path | contains(":t-old"))'\'' >/dev/null <<<"$out"'
ok "malformed window degrades to conservative keep with reason" '[ "$rc" = 0 ] && jq -e '\''.results | any((.path | contains(":t-broken")) and .temporal.status == "undated" and .temporal.reason == "malformed-window-kept")'\'' >/dev/null <<<"$out"'
out="$(tsearch CCC_MEMORY_AS_OF=2026-01-15T00:00:00Z)"; rc=$?
ok "as_of retrieves the point-in-time fact and excludes later ones" '[ "$rc" = 0 ] && jq -e '\''(.temporal.mode == "as_of") and (.results[0].path | contains(":t-old")) and (.results | all(.path | contains(":t-new") | not))'\'' >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$temp_state" CCC_MEMORY_INDEX_DB="$temp_state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" "release train departs zeta" --as-of 2026-01-15T00:00:00Z 2>/dev/null)"; rc=$?
ok "as_of works as a CLI flag too" '[ "$rc" = 0 ] && jq -e '\''(.temporal.mode == "as_of") and (.results[0].path | contains(":t-old"))'\'' >/dev/null <<<"$out"'
out="$(tsearch CCC_MEMORY_AS_OF=garbage)"; rc=$?
ok "unparseable as_of degrades to current mode with body-free signal" '[ "$rc" = 0 ] && jq -e '\''(.temporal.mode == "current") and (.temporal.reason == "as-of-parse-failed-using-current")'\'' >/dev/null <<<"$out"'

# #871 remaining slice: NL time-reference auto-expansion. The same fixture
# drives it — an absolute date in the query itself must land on the same
# instant as the explicit --as-of above, explicit flags always win, the
# kill-switch restores plain current mode, and ambiguous dates degrade with a
# body-free signal instead of guessing.
tsearchq() { # <query> [extra env...]
  local q="$1"; shift
  env CCC_STATE_DIR="$temp_state" CCC_MEMORY_INDEX_DB="$temp_state/memory-index.sqlite" "$@" bash "$ROOT/scripts/ccc-memory-search.sh" "$q" 2>/dev/null
}
out="$(tsearchq "2026-01-15에 release train departs zeta")"; rc=$?
ok "NL absolute date auto-expands to as_of and retrieves the point-in-time fact" '[ "$rc" = 0 ] && jq -e '\''(.temporal.mode == "as_of") and (.temporal.nl_as_of.rule == "abs:iso-date") and (.results[0].path | contains(":t-old")) and (.results | all(.path | contains(":t-new") | not))'\'' >/dev/null <<<"$out"'
out="$(tsearchq "2026-01-15에 release train departs zeta" CCC_MEMORY_AS_OF=2026-01-15T00:00:00Z)"; rc=$?
ok "explicit as_of wins over NL expansion (no nl_as_of marker)" '[ "$rc" = 0 ] && jq -e '\''(.temporal.mode == "as_of") and (.temporal | has("nl_as_of") | not)'\'' >/dev/null <<<"$out"'
out="$(tsearchq "2026-01-15에 release train departs zeta" CCC_MEMORY_NL_AS_OF=0)"; rc=$?
ok "CCC_MEMORY_NL_AS_OF=0 disables the expansion" '[ "$rc" = 0 ] && jq -e '\''(.temporal.mode == "current") and (.temporal | has("nl_as_of") | not)'\'' >/dev/null <<<"$out"'
out="$(tsearchq "2026-01-15와 2026-03-01에 release train departs zeta")"; rc=$?
ok "ambiguous absolute dates degrade to current with body-free signal" '[ "$rc" = 0 ] && jq -e '\''(.temporal.mode == "current") and (.temporal.nl_as_of.degraded == "ambiguous-absolute-dates")'\'' >/dev/null <<<"$out"'
out="$(tsearchq "release train departs zeta")"; rc=$?
ok "time-less query carries no nl_as_of marker" '[ "$rc" = 0 ] && jq -e '\''.temporal | has("nl_as_of") | not'\'' >/dev/null <<<"$out"'

# restore the structured-fact index for the secret-redaction test below
CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_FACTS_FILE="$facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
ok "structured fact indexing redacts secrets" '! python3 - <<PY | grep -q VALUE_SHOULD_NOT_INDEX_FACT
import sqlite3
con=sqlite3.connect("$state/memory-index.sqlite")
print("\n".join(r[0] for r in con.execute("select content from memory_docs")))
PY
'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_FACTS_FILE="$facts" CCC_MEMORY_RETRIEVAL=hybrid-local bash "$ROOT/scripts/ccc-memory-explain.sh" --json --query "current editor Helix" 2>&1)"; rc=$?
ok "memory explain emits read-only diagnostics" '[ "$rc" = 0 ] && jq -e ".ok == true and .safety.no_network == true and .search.retrievalMode == \"hybrid-local\"" >/dev/null <<<"$out"'
out="$(bash "$ROOT/scripts/ccc-memory-benchmark-export.sh" --json 2>&1)"; rc=$?
ok "benchmark export defaults to synthetic fixtures only" '[ "$rc" = 0 ] && jq -e ".ok == true and .real_memory_read == false and (.items | length) >= 3" >/dev/null <<<"$out"'

# Default profile (honcho) now queries the local hot-memory index too (was
# hybrid/max-perf-only). The structured-fact index built above is still present.
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_QUERY="current editor Helix" bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "default profile queries the local hot-memory index" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext | (contains(\"- (\") and (contains(\"local hot memory disabled\") | not))" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_LOCAL_MEMORY_ENABLED=0 CCC_MEMORY_QUERY="current editor Helix" bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1)"; rc=$?
ok "CCC_LOCAL_MEMORY_ENABLED=0 opts out of local hot memory" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext | (contains(\"local hot memory disabled\") and (contains(\"\\\"results\\\"\") | not))" >/dev/null <<<"$out"'

# Injection rendering: the local hot block is injected as compact readable lines
# ("- (source) snippet"), not the raw search JSON — the debug score/signals/full
# paths are noise to the model and waste the budget. CCC_MEMORY_INJECT_RENDER=0
# falls back to raw JSON (for diagnostics / back-compat).
hot_run() { # extra env assignments as args
  env CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" \
    CCC_MEMORY_INDEX_DB="$state/memory-index.sqlite" CCC_HOOK_DIR="$ROOT/claude/hooks" \
    CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_QUERY="current editor Helix" "$@" \
    bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>&1
}
out="$(hot_run)"; rc=$?
ok "rendered local hot block uses readable bullet lines" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext | contains(\"- (fact)\")" >/dev/null <<<"$out"'
ok "rendered local hot block drops debug signals/score/results noise" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext | ((contains(\"signals\") or contains(\"\\\"score\\\"\") or contains(\"\\\"results\\\"\")) | not)" >/dev/null <<<"$out"'
out="$(hot_run CCC_MEMORY_INJECT_RENDER=0)"; rc=$?
ok "CCC_MEMORY_INJECT_RENDER=0 injects raw search JSON" '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.additionalContext | (contains(\"\\\"results\\\"\") and contains(\"signals\"))" >/dev/null <<<"$out"'

# Relevance-aware budget: when small/empty canonical blocks (no wiki/honcho cache)
# leave budget unused, the local hot block reclaims it — fetching MORE than the
# default 5 results to fill the freed budget — while the whole injection stays
# within CCC_MEMORY_MAX_BYTES. Disable with CCC_MEMORY_DYNAMIC_BUDGET=0; an
# explicit CCC_MEMORY_SEARCH_LIMIT always wins.
bud_state="$TMP/budget-state"; bud_cache="$TMP/budget-cache"; bud_mem="$TMP/budget-mem"
rm -rf "$bud_state" "$bud_cache" "$bud_mem"; mkdir -p "$bud_state" "$bud_cache" "$bud_mem"
printf 'Tiny node identity memory.\n' > "$bud_mem/MEMORY.md"; printf 'concise\n' > "$bud_mem/USER.md"
bud_facts="$bud_state/memory-facts.jsonl"; : > "$bud_facts"
for i in $(seq 1 40); do
  printf '{"id":"bf%s","kind":"preference","text":"Operator preference %s about editor Helix workflow tooling configuration detail %s","review":"auto-local"}\n' "$i" "$i" "$i" >> "$bud_facts"
done
CCC_STATE_DIR="$bud_state" CCC_MEMORY_CACHE_DIR="$bud_cache" CCC_MEMORY_DIR="$bud_mem" CCC_MEMORY_FACTS_FILE="$bud_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
bud_bullets() { # extra env assignments; prints count of rendered local bullets
  # The fixture is intentionally many near-identical facts (to test result-count
  # scaling). The suite-wide CCC_MEMORY_NO_REFRESH=1 keeps the detached refresh
  # from consolidating them away mid-test, so no per-test pin is needed.
  env CCC_STATE_DIR="$bud_state" CCC_MEMORY_CACHE_DIR="$bud_cache" CCC_MEMORY_DIR="$bud_mem" \
    CCC_MEMORY_INDEX_DB="$bud_state/memory-index.sqlite" CCC_HOOK_DIR="$ROOT/claude/hooks" \
    CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_QUERY="editor Helix" "$@" \
    bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>/dev/null \
    | jq -r '.hookSpecificOutput.additionalContext' \
    | sed -n '/## Local hot memory/,/## Family Wiki/p' | grep -c '^- ('
}
ok "dynamic budget reclaims slack -> local surfaces more than the default 5" '[ "$(bud_bullets)" -gt 5 ]'
ok "dynamic budget OFF -> local stays at the default 5" '[ "$(bud_bullets CCC_MEMORY_DYNAMIC_BUDGET=0)" = 5 ]'
ok "explicit CCC_MEMORY_SEARCH_LIMIT wins over dynamic" '[ "$(bud_bullets CCC_MEMORY_SEARCH_LIMIT=3)" = 3 ]'
bud_total="$(env CCC_STATE_DIR="$bud_state" CCC_MEMORY_CACHE_DIR="$bud_cache" CCC_MEMORY_DIR="$bud_mem" CCC_MEMORY_INDEX_DB="$bud_state/memory-index.sqlite" CCC_HOOK_DIR="$ROOT/claude/hooks" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_QUERY="editor Helix" bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>/dev/null | jq -r '.hookSpecificOutput.additionalContext' | wc -c)"
ok "dynamic budget keeps the whole injection within CCC_MEMORY_MAX_BYTES" '[ "$bud_total" -le 12000 ]'

# Usage feedback loop: docs repeatedly RETRIEVED for real injections earn a small
# recency-decayed boost (tie-break only, capped below one token of coverage).
# Recording happens only when the caller sets CCC_MEMORY_RECORD_USAGE=1, so
# diagnostics stay read-only; CCC_MEMORY_USAGE_FEEDBACK=0 disables the whole loop.
us_state="$TMP/usage-state"; us_cache="$TMP/usage-cache"; us_mem="$TMP/usage-mem"
rm -rf "$us_state" "$us_cache" "$us_mem"; mkdir -p "$us_state" "$us_cache" "$us_mem"
printf 'x\n' > "$us_mem/MEMORY.md"; printf 'x\n' > "$us_mem/USER.md"
us_facts="$us_state/memory-facts.jsonl"
printf '%s\n' \
  '{"id":"ua","kind":"preference","text":"alpha topic about deployment runbook procedure","review":"auto-local"}' \
  '{"id":"ub","kind":"preference","text":"beta topic about deployment runbook procedure","review":"auto-local"}' \
  > "$us_facts"
CCC_STATE_DIR="$us_state" CCC_MEMORY_CACHE_DIR="$us_cache" CCC_MEMORY_DIR="$us_mem" CCC_MEMORY_FACTS_FILE="$us_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
us_search() { env CCC_STATE_DIR="$us_state" CCC_MEMORY_INDEX_DB="$us_state/memory-index.sqlite" "$@" bash "$ROOT/scripts/ccc-memory-search.sh" "deployment runbook" 2>/dev/null; }
us_file="$us_state/memory-usage.json"

out="$(us_search)"; rc=$?
ok "usage_boost is 0 with no stats (no behavior change on fresh node)" '[ "$rc" = 0 ] && jq -e "all(.results[].signals.usage_boost; . == 0)" >/dev/null <<<"$out"'
ok "search does not record usage without CCC_MEMORY_RECORD_USAGE" '[ ! -f "$us_file" ]'

# Record retrievals of the "beta" doc; it should then carry a boost and outrank
# its equal-coverage "alpha" peer (pure tie-break).
for i in 1 2 3 4; do CCC_STATE_DIR="$us_state" CCC_MEMORY_INDEX_DB="$us_state/memory-index.sqlite" CCC_MEMORY_RECORD_USAGE=1 CCC_MEMORY_SEARCH_LIMIT=1 bash "$ROOT/scripts/ccc-memory-search.sh" "beta deployment runbook" >/dev/null 2>&1; done
ok "RECORD_USAGE writes a bounded chmod-600 usage file" '[ -f "$us_file" ] && [ "$(stat -c %a "$us_file")" = 600 ] && jq -e "to_entries | length == 1 and .[0].value.n == 4" >/dev/null <<<"$(cat "$us_file")"'
out="$(us_search)"; rc=$?
ok "recorded doc earns a positive usage_boost and ranks first" '[ "$rc" = 0 ] && jq -e "(.results[0].path | contains(\"ub\")) and (.results[0].signals.usage_boost > 0)" >/dev/null <<<"$out"'
ok "usage_boost is capped below one token of coverage (<= 3.0)" 'jq -e "all(.results[].signals.usage_boost; . <= 3.0)" >/dev/null <<<"$out"'

# Off-switch disables both read (boost) and write (record).
out="$(us_search CCC_MEMORY_USAGE_FEEDBACK=0)"; rc=$?
ok "CCC_MEMORY_USAGE_FEEDBACK=0 zeroes the boost" '[ "$rc" = 0 ] && jq -e "all(.results[].signals.usage_boost; . == 0)" >/dev/null <<<"$out"'
cp "$us_file" "$us_file.bak"
CCC_STATE_DIR="$us_state" CCC_MEMORY_INDEX_DB="$us_state/memory-index.sqlite" CCC_MEMORY_USAGE_FEEDBACK=0 CCC_MEMORY_RECORD_USAGE=1 bash "$ROOT/scripts/ccc-memory-search.sh" "beta deployment" >/dev/null 2>&1
ok "CCC_MEMORY_USAGE_FEEDBACK=0 also suppresses recording" 'diff -q "$us_file" "$us_file.bak" >/dev/null'

# Fact consolidation: near-duplicate distilled facts (char-4-gram Jaccard >= thr,
# same kind) collapse to the most recent; older MACHINE-generated copies are
# marked review:superseded (audit trail) and the index skips them. Human-reviewed
# facts are never auto-superseded. Distinct facts are untouched; idempotent.
co_state="$TMP/consolidate-state"; co_cache="$TMP/consolidate-cache"; co_mem="$TMP/consolidate-mem"
rm -rf "$co_state" "$co_cache" "$co_mem"; mkdir -p "$co_state" "$co_cache" "$co_mem"
printf 'x\n' > "$co_mem/MEMORY.md"; printf 'x\n' > "$co_mem/USER.md"
co_facts="$co_state/memory-facts.jsonl"
printf '%s\n' \
  '{"id":"c-old","kind":"preference","text":"Operator switched current editor to Helix from Neovim","observed_at":"2026-06-01T00:00:00Z","review":"auto-local"}' \
  '{"id":"c-new","kind":"preference","text":"Operator switched current editor to Helix from Neovim now","observed_at":"2026-06-20T00:00:00Z","review":"auto-local"}' \
  '{"id":"c-distinct","kind":"decision","text":"Honcho auth is enforced via OAuth subprocess for distill","observed_at":"2026-06-10T00:00:00Z","review":"auto-local"}' \
  '{"id":"c-human","kind":"preference","text":"Operator switched current editor to Helix from Neovim","observed_at":"2026-05-01T00:00:00Z","review":"approved"}' \
  > "$co_facts"
out="$(CCC_STATE_DIR="$co_state" CCC_MEMORY_FACTS_FILE="$co_facts" bash "$ROOT/scripts/ccc-memory-consolidate.sh" 2>&1)"; rc=$?
ok "consolidate supersedes the older near-duplicate" '[ "$rc" = 0 ] && jq -e ".ok == true and .superseded == 1 and .changed == true" >/dev/null <<<"$out"'
ok "older auto-fact is marked superseded, newer kept" 'jq -e "select(.id==\"c-old\").review == \"superseded\"" >/dev/null <<<"$(grep c-old "$co_facts")" && jq -e "select(.id==\"c-new\").review == \"auto-local\"" >/dev/null <<<"$(grep c-new "$co_facts")"'
ok "distinct fact (different kind/topic) is untouched" 'jq -e "select(.id==\"c-distinct\").review == \"auto-local\"" >/dev/null <<<"$(grep c-distinct "$co_facts")"'
ok "human-approved near-duplicate is never auto-superseded" 'jq -e "select(.id==\"c-human\").review == \"approved\"" >/dev/null <<<"$(grep c-human "$co_facts")"'
out="$(CCC_STATE_DIR="$co_state" CCC_MEMORY_FACTS_FILE="$co_facts" bash "$ROOT/scripts/ccc-memory-consolidate.sh" 2>&1)"; rc=$?
ok "consolidate is idempotent (second run changes nothing)" '[ "$rc" = 0 ] && jq -e ".superseded == 0 and .changed == false" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$co_state" CCC_MEMORY_FACTS_FILE="$co_facts" CCC_MEMORY_CONSOLIDATE=0 bash "$ROOT/scripts/ccc-memory-consolidate.sh" 2>&1)"; rc=$?
ok "CCC_MEMORY_CONSOLIDATE=0 skips" '[ "$rc" = 0 ] && jq -e ".skipped == \"disabled\"" >/dev/null <<<"$out"'

# ---- #871 §4 supersede/conflict semantics, aligned to nunchi G1/G2/G3 -------
# The peer-facts lane already settled these questions; both lanes must mean the
# same thing. Fixtures carry no lexical tell — the decision comes from the
# logical key and source_rank, not from wording.

# The superseded loser must carry valid_until, not just review. Slice 1's
# temporal search (#1197) reads valid_from/valid_until and never review, so
# without this the supersede decision was invisible to current/as_of.
ok "a superseded fact records valid_until and the winner's id" \
  'jq -e ".review == \"superseded\" and .valid_until == \"2026-06-20T00:00:00Z\" and .superseded_by == \"c-new\"" >/dev/null <<<"$(grep c-old "$co_facts")"'

# G2: the winner is the highest source_rank, recency only breaks the tie.
g2_state="$TMP/consolidate-g2"; rm -rf "$g2_state"; mkdir -p "$g2_state"
g2_facts="$g2_state/memory-facts.jsonl"
printf '%s\n' \
  '{"id":"g2-old","kind":"preference","text":"Operator prefers concise Korean reports always","observed_at":"2026-06-01T00:00:00Z","review":"auto-local","source_rank":1,"entities":["operator"]}' \
  '{"id":"g2-new","kind":"preference","text":"Operator prefers concise Korean reports always now","observed_at":"2026-07-01T00:00:00Z","review":"auto-local","source_rank":3,"entities":["operator"]}' \
  > "$g2_facts"
out="$(CCC_STATE_DIR="$g2_state" CCC_MEMORY_FACTS_FILE="$g2_facts" bash "$ROOT/scripts/ccc-memory-consolidate.sh" 2>&1)"; rc=$?
ok "G2 higher-rank newcomer supersedes the weaker older fact" \
  '[ "$rc" = 0 ] && jq -e ".superseded == 1 and .conflicts == 0" >/dev/null <<<"$out" \
   && jq -e ".review == \"superseded\" and .superseded_by == \"g2-new\"" >/dev/null <<<"$(grep g2-old "$g2_facts")"'

# G3: newer-but-weaker against older-but-stronger is a real contradiction.
# Neither direction is safe, so nothing is superseded — an agent inference must
# not bury a user statement, and the newer fact must not be silently dropped.
g3_state="$TMP/consolidate-g3"; rm -rf "$g3_state"; mkdir -p "$g3_state"
g3_facts="$g3_state/memory-facts.jsonl"
printf '%s\n' \
  '{"id":"g3-user","kind":"preference","text":"Operator prefers concise Korean reports always","observed_at":"2026-06-01T00:00:00Z","review":"auto-local","source_rank":3,"entities":["operator"]}' \
  '{"id":"g3-agent","kind":"preference","text":"Operator prefers concise Korean reports always now","observed_at":"2026-07-01T00:00:00Z","review":"auto-local","source_rank":1,"entities":["operator"]}' \
  > "$g3_facts"
out="$(CCC_STATE_DIR="$g3_state" CCC_MEMORY_FACTS_FILE="$g3_facts" bash "$ROOT/scripts/ccc-memory-consolidate.sh" 2>&1)"; rc=$?
ok "G3 ambiguous rank conflict supersedes nothing and flags the newcomer" \
  '[ "$rc" = 0 ] && jq -e ".superseded == 0 and .conflicts == 1" >/dev/null <<<"$out"'
ok "G3 leaves the user-stated fact open — an inference never buries it" \
  'jq -e ".review == \"auto-local\" and (has(\"valid_until\") | not)" >/dev/null <<<"$(grep g3-user "$g3_facts")"'
ok "G3 flags the newcomer needs-human with an auditable conflict reason" \
  'jq -e ".review == \"needs-human\" and .conflict == \"source-rank\"" >/dev/null <<<"$(grep g3-agent "$g3_facts")"'
out="$(CCC_STATE_DIR="$g3_state" CCC_MEMORY_FACTS_FILE="$g3_facts" bash "$ROOT/scripts/ccc-memory-consolidate.sh" 2>&1)"; rc=$?
ok "G3 flagging is idempotent — a flagged fact is not re-flagged" \
  '[ "$rc" = 0 ] && jq -e ".conflicts == 0 and .changed == false" >/dev/null <<<"$out"'

# Logical key: identical wording about DIFFERENT subjects is not one fact.
# Lexical similarity alone would have collapsed these.
lkey_state="$TMP/consolidate-lkey"; rm -rf "$lkey_state"; mkdir -p "$lkey_state"
lkey_facts="$lkey_state/memory-facts.jsonl"
printf '%s\n' \
  '{"id":"lk-a","kind":"preference","text":"Node prefers concise Korean reports always","observed_at":"2026-06-01T00:00:00Z","review":"auto-local","entities":["nosuk"]}' \
  '{"id":"lk-b","kind":"preference","text":"Node prefers concise Korean reports always","observed_at":"2026-07-01T00:00:00Z","review":"auto-local","entities":["daegyo"]}' \
  > "$lkey_facts"
out="$(CCC_STATE_DIR="$lkey_state" CCC_MEMORY_FACTS_FILE="$lkey_facts" bash "$ROOT/scripts/ccc-memory-consolidate.sh" 2>&1)"; rc=$?
ok "same wording about different subjects never collapses (logical key)" \
  '[ "$rc" = 0 ] && jq -e ".clusters == 0 and .superseded == 0" >/dev/null <<<"$out" \
   && jq -e ".review == \"auto-local\"" >/dev/null <<<"$(grep lk-a "$lkey_facts")"'

# An unparseable source_rank is demoted to 1 rather than trusted (G2), so a
# malformed value can never let a fact outrank a real user statement.
bad_state="$TMP/consolidate-badrank"; rm -rf "$bad_state"; mkdir -p "$bad_state"
bad_facts="$bad_state/memory-facts.jsonl"
printf '%s\n' \
  '{"id":"br-user","kind":"preference","text":"Operator prefers concise Korean reports always","observed_at":"2026-06-01T00:00:00Z","review":"auto-local","source_rank":3,"entities":["operator"]}' \
  '{"id":"br-bad","kind":"preference","text":"Operator prefers concise Korean reports always now","observed_at":"2026-07-01T00:00:00Z","review":"auto-local","source_rank":"99","entities":["operator"]}' \
  > "$bad_facts"
out="$(CCC_STATE_DIR="$bad_state" CCC_MEMORY_FACTS_FILE="$bad_facts" bash "$ROOT/scripts/ccc-memory-consolidate.sh" 2>&1)"; rc=$?
ok "a malformed source_rank is demoted, not trusted" \
  '[ "$rc" = 0 ] && jq -e ".superseded == 0 and .conflicts == 1" >/dev/null <<<"$out"'

# Regression (#869 sweep / #1076): this pass rewrites the WHOLE facts file, so
# it must hold the same lock the appenders take (.local-memory-sink.lock) or a
# concurrent append lands inside the read->os.replace window and is lost.
lk_state="$TMP/consolidate-lock"; rm -rf "$lk_state"; mkdir -p "$lk_state"
lk_facts="$lk_state/memory-facts.jsonl"
printf '%s\n' \
  '{"id":"l-old","kind":"preference","text":"Operator switched current editor to Helix from Neovim","observed_at":"2026-06-01T00:00:00Z","review":"auto-local"}' \
  '{"id":"l-new","kind":"preference","text":"Operator switched current editor to Helix from Neovim now","observed_at":"2026-06-20T00:00:00Z","review":"auto-local"}' \
  > "$lk_facts"
cp "$lk_facts" "$lk_facts.before"
# Hold the sink lock exactly as an appender would, then run the pass.
python3 - "$lk_state/.local-memory-sink.lock" <<'PYLOCK' &
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
os.fchmod(fd, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
time.sleep(5)
PYLOCK
lk_pid=$!
sleep 1
out="$(CCC_STATE_DIR="$lk_state" CCC_MEMORY_FACTS_FILE="$lk_facts" bash "$ROOT/scripts/ccc-memory-consolidate.sh" 2>&1)"; rc=$?
ok "consolidate skips while an appender holds the sink lock" '[ "$rc" = 0 ] && jq -e ".skipped == \"locked\"" >/dev/null <<<"$out"'
ok "consolidate does not rewrite the facts file it could not lock" 'diff -q "$lk_facts" "$lk_facts.before" >/dev/null'
kill "$lk_pid" 2>/dev/null; wait "$lk_pid" 2>/dev/null
# With the lock free the same input consolidates normally.
out="$(CCC_STATE_DIR="$lk_state" CCC_MEMORY_FACTS_FILE="$lk_facts" bash "$ROOT/scripts/ccc-memory-consolidate.sh" 2>&1)"; rc=$?
ok "consolidate proceeds once the lock is released" '[ "$rc" = 0 ] && jq -e ".superseded == 1 and .changed == true" >/dev/null <<<"$out"'

CCC_STATE_DIR="$co_state" CCC_MEMORY_CACHE_DIR="$co_cache" CCC_MEMORY_DIR="$co_mem" CCC_MEMORY_FACTS_FILE="$co_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
out="$(CCC_STATE_DIR="$co_state" CCC_MEMORY_INDEX_DB="$co_state/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" "editor Helix Neovim" 2>/dev/null)"
ok "index skips superseded facts (c-old not surfaced)" 'jq -e "all(.results[].path; (contains(\"c-old\")|not))" >/dev/null <<<"$out"'

# Cross-source injection dedup: the local hot block must not echo hits that are
# ALSO injected verbatim as the MEMORY/wiki/honcho blocks (double-spending the
# budget). A memory-source hit fully present in the injected MEMORY block is
# dropped; a distilled fact (no other injection path) is kept; content truncated
# out of the canonical block is kept (lossless); CCC_MEMORY_INJECT_DEDUP=0 off.
dd_state="$TMP/dedup-state"; dd_cache="$TMP/dedup-cache"; dd_mem="$TMP/dedup-mem"
rm -rf "$dd_state" "$dd_cache" "$dd_mem"; mkdir -p "$dd_state" "$dd_cache" "$dd_mem"
printf 'Operator prefers Helix editor and the honcho memory profile by default.\n' > "$dd_mem/MEMORY.md"
printf 'user likes concise Korean reports\n' > "$dd_mem/USER.md"
printf 'wiki mentions unrelated deployment runbook details\n' > "$dd_cache/wiki.txt"
# Facts live at the DEFAULT path so the detached background refresh that
# load-memory.sh fires rebuilds the index WITH them (otherwise a concurrent
# rebuild from the empty default path would drop the fact mid-suite).
dd_facts="$dd_state/memory-facts.jsonl"
printf '%s\n' '{"id":"dedup-fact","kind":"preference","text":"Operator switched current editor to Helix from Neovim last sprint.","durability":"durable","review":"auto-local"}' > "$dd_facts"
CCC_STATE_DIR="$dd_state" CCC_MEMORY_CACHE_DIR="$dd_cache" CCC_MEMORY_DIR="$dd_mem" CCC_MEMORY_FACTS_FILE="$dd_facts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
dd_sources() { # remaining args become extra env assignments for the hook
  # `env` (not a bare prefix) so post-expansion NAME=VALUE words from "$@" are
  # honoured as assignments rather than treated as the command name.
  # Disable rendering so the local block stays raw JSON we can parse for sources.
  env CCC_STATE_DIR="$dd_state" CCC_MEMORY_CACHE_DIR="$dd_cache" CCC_MEMORY_DIR="$dd_mem" \
    CCC_MEMORY_INDEX_DB="$dd_state/memory-index.sqlite" CCC_HOOK_DIR="$ROOT/claude/hooks" \
    CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_QUERY="Helix editor" \
    CCC_MEMORY_INJECT_RENDER=0 "$@" \
    bash "$ROOT/claude/hooks/load-memory.sh" SessionStart 2>/dev/null \
    | jq -r '.hookSpecificOutput.additionalContext' \
    | sed -n '/## Local hot memory/,/## Family Wiki/p' \
    | python3 -c 'import sys,re,json
t=sys.stdin.read(); m=re.search(r"\{.*\}",t,re.S)
print(" ".join(sorted({r.get("source","") for r in (json.loads(m.group(0)).get("results",[]) if m else [])})))'
}
ok "injection dedup drops memory hit already in MEMORY block, keeps distilled fact" '[ "$(dd_sources)" = "structured" ]'
ok "injection dedup OFF keeps the redundant memory hit" '[ "$(dd_sources CCC_MEMORY_INJECT_DEDUP=0)" = "memory structured" ]'
ok "injection dedup is lossless when canonical block is truncated away" '[ "$(dd_sources CCC_BUILTIN_MEMORY_MAX_BYTES=20)" = "memory structured" ]'

# Embedding (semantic) lane — opt-in via CCC_MEMORY_EMBED_CMD. Uses a local,
# no-network fake embedder with a tiny concept map so a synonym query recalls a
# doc that the surface-form lexical + fuzzy lanes both miss.
estate="$TMP/embed-state"; rm -rf "$estate"; mkdir -p "$estate/cache" "$estate/memories"
printf 'x\n' > "$estate/memories/MEMORY.md"; printf 'x\n' > "$estate/memories/USER.md"
printf '%s\n' '{"id":"autodoc","kind":"decision","text":"The automobile parking guideline for the node.","durability":"durable","privacy":"private","review":"auto-local"}' > "$estate/facts.jsonl"
cat > "$estate/fake-embed.py" <<'PYEMB'
import sys, json, re
text = sys.stdin.read().lower()
concepts = [["car","automobile","vehicle"],["policy","rule","guideline","rules","parking"]]
toks = set(re.findall(r"[a-z]+", text))
vec = [0.0]*(len(concepts)+1)
for i, ws in enumerate(concepts):
    for w in ws:
        if w in toks:
            vec[i] += 1.0
vec[-1] = 0.01  # baseline on a dedicated axis so unrelated docs don't false-match
print(json.dumps(vec))
PYEMB
embcmd="python3 $estate/fake-embed.py"
CCC_STATE_DIR="$estate" CCC_MEMORY_CACHE_DIR="$estate/cache" CCC_MEMORY_DIR="$estate/memories" CCC_MEMORY_FACTS_FILE="$estate/facts.jsonl" CCC_MEMORY_EMBED_CMD="$embcmd" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
ok "index precomputes embedding vectors when CCC_MEMORY_EMBED_CMD is set" 'python3 - "$estate/memory-index.sqlite" <<PY >/dev/null 2>&1
import sqlite3,sys
c=sqlite3.connect(sys.argv[1])
n=c.execute("select count(*) from memory_vectors").fetchone()[0]
sys.exit(0 if n>=1 else 1)
PY'
CCC_STATE_DIR="$estate" CCC_MEMORY_CACHE_DIR="$estate/cache" CCC_MEMORY_DIR="$estate/memories" CCC_MEMORY_FACTS_FILE="$estate/facts.jsonl" CCC_MEMORY_EMBED_CMD="$embcmd" CCC_MEMORY_EMBED_MODEL="model-b" bash "$ROOT/scripts/ccc-memory-index.sh" update >/dev/null 2>&1
ok "embedding vectors refresh when the model label changes" 'python3 - "$estate/memory-index.sqlite" <<PY >/dev/null 2>&1
import sqlite3,sys
c=sqlite3.connect(sys.argv[1])
models={r[0] for r in c.execute("select model from memory_vectors")}
sys.exit(0 if models == {"model-b"} else 1)
PY'
out="$(CCC_STATE_DIR="$estate" CCC_MEMORY_INDEX_DB="$estate/memory-index.sqlite" bash "$ROOT/scripts/ccc-memory-search.sh" "car rules" 2>&1)"; rc=$?
ok "surface-form lanes miss the synonym query" '[ "$rc" = 0 ] && jq -e "(.results | length) == 0" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$estate" CCC_MEMORY_INDEX_DB="$estate/memory-index.sqlite" CCC_MEMORY_EMBED_CMD="$embcmd" bash "$ROOT/scripts/ccc-memory-search.sh" "car rules" 2>&1)"; rc=$?
ok "embedding lane recalls the synonym query" '[ "$rc" = 0 ] && jq -e "(.lanes | index(\"embedding\") != null) and (.results[0].path | contains(\"autodoc\"))" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$estate" CCC_MEMORY_INDEX_DB="$estate/memory-index.sqlite" CCC_MEMORY_EMBED_CMD=/bin/false bash "$ROOT/scripts/ccc-memory-search.sh" "automobile guideline" 2>&1)"; rc=$?
ok "embedding lane fails open when the provider errors" '[ "$rc" = 0 ] && jq -e "(.lanes | index(\"embedding\")) == null and (.results | length) >= 1" >/dev/null <<<"$out"'

out="$(CCC_STATE_DIR="$TMP/golden-state" bash "$ROOT/scripts/ccc-memory-eval.sh" --golden 2>&1)"; rc=$?
ok "memory eval golden-set reports precision recall mrr" '[ "$rc" = 0 ] && jq -e ".ok == true and .mode == \"golden\" and .metrics.precision_at_1 >= 0.5 and .metrics.recall_at_5 >= 0.5 and .metrics.mrr > 0 and .metrics.latency_p95_ms >= .metrics.latency_p50_ms" >/dev/null <<<"$out"'
out="$(CCC_STATE_DIR="$TMP/scenario-state" bash "$ROOT/scripts/ccc-memory-eval.sh" --scenario 2>&1)"; rc=$?
ok "memory eval scenario covers temporal conflict and volatile demotion" '[ "$rc" = 0 ] && jq -e ".ok == true and .mode == \"scenario\" and .metrics.temporal_current_accuracy == 1 and .metrics.volatile_exclusion_accuracy == 1" >/dev/null <<<"$out"'


printf '%s\n' '{"source":"wiki","status":"ok","refreshed_at":"2000-01-01T00:00:00Z","duration_ms":1,"bytes":10,"error":"","query_hash":"abc","stale":false,"max_age_sec":1}' > "$cache/wiki.meta.json"
out="$(CCC_STATE_DIR="$state" CCC_MEMORY_CACHE_DIR="$cache" CCC_MEMORY_DIR="$mem" CCC_WIKI_CACHE_MAX_AGE_SEC=1 bash "$ROOT/scripts/ccc-memory-check.sh" --json 2>&1)"; rc=$?
ok "memory check exposes cache metadata and recomputes stale flag" '[ "$rc" = 0 ] && jq -e ".wiki.meta.stale == true and .wiki.meta.query_hash == \"abc\"" >/dev/null <<<"$out"'

printf '## CAND-001\nProposed wiki fact\n\n## CAND-002\nSecond fact\n' > "$state/wiki-candidates.md"
out="$(CCC_STATE_DIR="$state" bash "$ROOT/scripts/ccc-wiki-triage.sh" list 2>&1)"; rc=$?
ok "wiki triage lists local candidates without writing Wiki" '[ "$rc" = 0 ] && jq -e "(.candidates | length) == 2 and .candidates[0].id == \"CAND-001\"" >/dev/null <<<"$out"'

out="$(CCC_STATE_DIR="$caller_state" CCC_MEMORY_EVAL_KEEP_TMP=0 bash "$ROOT/scripts/ccc-memory-eval.sh" Honcho 2>&1)"; rc=$?
ok "memory eval harness succeeds with caller state" '[ "$rc" = 0 ] && jq -e ".ok == true" >/dev/null <<<"$out"'
ok "memory eval does not delete caller-provided state" '[ -f "$caller_state/marker.txt" ]'
ok "memory eval cleans only its internal temp dir" '! compgen -G "$caller_state/ccc-memory-eval.*" >/dev/null'

real_mem="$TMP/real-memories"
real_cache="$TMP/real-cache"
mkdir -p "$real_mem" "$real_cache"
printf 'DO_NOT_OVERWRITE_REAL_MEMORY\n' > "$real_mem/MEMORY.md"
printf 'DO_NOT_OVERWRITE_REAL_USER\n' > "$real_mem/USER.md"
printf 'DO_NOT_OVERWRITE_REAL_WIKI\n' > "$real_cache/wiki.txt"
printf 'DO_NOT_OVERWRITE_REAL_HONCHO\n' > "$real_cache/honcho.txt"
out="$(CCC_STATE_DIR="$TMP/eval-external-state" CCC_MEMORY_DIR="$real_mem" CCC_MEMORY_CACHE_DIR="$real_cache" CCC_MEMORY_EVAL_KEEP_TMP=0 bash "$ROOT/scripts/ccc-memory-eval.sh" Honcho 2>&1)"; rc=$?
ok "memory eval succeeds while external memory/cache env vars are set" '[ "$rc" = 0 ] && jq -e ".ok == true" >/dev/null <<<"$out"'
ok "memory eval does not overwrite external memory/cache dirs by default" 'grep -q DO_NOT_OVERWRITE_REAL_MEMORY "$real_mem/MEMORY.md" && grep -q DO_NOT_OVERWRITE_REAL_USER "$real_mem/USER.md" && grep -q DO_NOT_OVERWRITE_REAL_WIKI "$real_cache/wiki.txt" && grep -q DO_NOT_OVERWRITE_REAL_HONCHO "$real_cache/honcho.txt"'

install_home="$TMP/install-home"
install_claude="$TMP/install-claude"
install_hermes="$TMP/install-hermes"
# Route the full-setup systemd reconcile away from the live tree (#885): the
# renderer refuses a scratch tmp HOME unless the CCC_SYSTEMD_DIR seam is set.
out="$(HOME="$install_home" CCC_CLAUDE_DIR="$install_claude" CCC_HERMES_DIR="$install_hermes" CCC_SYSTEMD_DIR="$TMP/install-systemd-seam" CCC_SYSTEMCTL=/bin/true bash "$ROOT/setup.sh" --no-backup >/dev/null 2>&1; echo rc=$?)"
ok "setup installs memory helper tools beside hooks" 'grep -q "rc=0" <<<"$out" && [ -x "$install_claude/hooks/ccc-memory-index.sh" ] && [ -x "$install_claude/hooks/ccc-memory-search.sh" ] && [ -x "$install_claude/hooks/ccc-memory-query.sh" ] && [ -x "$install_claude/hooks/ccc-memory-explain.sh" ] && [ -x "$install_claude/hooks/ccc-wiki-triage.sh" ] && [ -x "$install_claude/hooks/ccc-memory-benchmark-export.sh" ]'
ok "setup installs the shared detached-spawn helper" '[ -x "$install_claude/hooks/lib/spawn-detached.sh" ]'
out="$(CCC_STATE_DIR="$TMP/install-eval-state" bash "$install_claude/hooks/ccc-memory-eval.sh" Honcho 2>&1)"; rc=$?
ok "installed memory eval finds helper tools beside hooks" '[ "$rc" = 0 ] && jq -e ".ok == true" >/dev/null <<<"$out"'

# Refresh guard: load-memory fires the detached refresh by default, and
# CCC_MEMORY_NO_REFRESH=1 suppresses it (the knob that makes this suite hermetic).
# A fake refresh-memory.sh signals a FIFO, so we observe firing deterministically
# without sleeping; the guarded case just times out the read.
gr_state="$TMP/guard-state"; gr_hook="$TMP/guard-hookdir"
mkdir -p "$gr_state/cache" "$gr_state/mem" "$gr_hook"
gr_fifo="$TMP/guard.fifo"
gr_setsid_bin="$TMP/guard-setsid-bin"
mkdir -p "$gr_setsid_bin"
write_exec_stub "$gr_setsid_bin/setsid" <<'SH'
exec "$@"
SH
cat > "$gr_hook/refresh-memory.sh" <<'SH'
#!/usr/bin/env bash
printf 'fired\n' > "$CCC_GUARD_FIFO" 2>/dev/null || true
SH
chmod +x "$gr_hook/refresh-memory.sh"
gr_run() { # env-prefix args become extra assignments; -u clears the suite-wide guard
  env -u CCC_MEMORY_NO_REFRESH \
    CCC_STATE_DIR="$gr_state" CCC_MEMORY_CACHE_DIR="$gr_state/cache" CCC_MEMORY_DIR="$gr_state/mem" \
    CCC_HOOK_DIR="$gr_hook" CCC_MEMORY_TOOLS_DIR="$ROOT/scripts" CCC_MEMORY_QUERY="x" \
    CCC_GUARD_FIFO="$gr_fifo" "$@" \
    bash "$ROOT/claude/hooks/load-memory.sh" SessionStart >/dev/null 2>&1
}
rm -f "$gr_fifo"; mkfifo "$gr_fifo"
gr_run PATH="$gr_setsid_bin:$PATH"
if read -t 5 _l <>"$gr_fifo"; then gr_default=fired; else gr_default=silent; fi
ok "load-memory fires the background refresh by default" '[ "$gr_default" = fired ]'

# Simulate a minimal Termux-like PATH that has the commands needed by
# load-memory but deliberately has no setsid. The shared helper must fall back
# to a disowned subshell instead of silently losing the refresh.
no_setsid_bin="$TMP/no-setsid-bin"
mkdir -p "$no_setsid_bin"
for cmd in bash cat python3 jq date wc hostname dirname; do
  ln -s "$(command -v "$cmd")" "$no_setsid_bin/$cmd"
done
rm -f "$gr_fifo"; mkfifo "$gr_fifo"
gr_run PATH="$no_setsid_bin" CCC_LOCAL_MEMORY_ENABLED=0
if read -t 5 _l <>"$gr_fifo"; then gr_no_setsid=fired; else gr_no_setsid=silent; fi
ok "load-memory refresh falls back when setsid is unavailable" '[ "$gr_no_setsid" = fired ]'

rm -f "$gr_fifo"; mkfifo "$gr_fifo"
gr_run CCC_MEMORY_NO_REFRESH=1
if read -t 2 _l <>"$gr_fifo"; then gr_guarded=fired; else gr_guarded=silent; fi
ok "CCC_MEMORY_NO_REFRESH=1 suppresses the background refresh" '[ "$gr_guarded" = silent ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
