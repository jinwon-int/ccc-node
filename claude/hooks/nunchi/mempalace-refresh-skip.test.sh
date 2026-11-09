#!/usr/bin/env bash
# Tests for mempalace-refresh.sh skip-unchanged (#2722 B-2).
# Fully isolated: HOME/NUNCHI_HOME point into a temp dir and the MemPalace CLI
# is a stub that only counts its `sweep`/`mine` calls.
# shellcheck disable=SC2034  # $out/$rc are read inside ok()'s eval strings
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REFRESH="$HERE/mempalace-refresh.sh"
PROBE="$HERE/../../../scripts/ccc_memory_probe.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../lib/test-stub.sh"
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

mkdir -p "$TMP/bin"
cat > "$TMP/bin/mempalace" <<'SH'
#!/usr/bin/env bash
case "$1" in
  sweep|mine) echo "$1" >> "$MP_CALLS"; exit "${MP_RC:-0}" ;;
  repair-status) exit 1 ;;
esac
exit 0
SH
chmod +x "$TMP/bin/mempalace"

# One isolated node home per scenario.
setup() { # <name> [home-suffix]
  H="$TMP/$1${2:-}"; NH="$H/.nunchi"; SRC="$H/.claude/projects/p"
  mkdir -p "$NH" "$SRC" "$H/.claude/state"
  echo on > "$H/.claude/state/nunchi.mode"   # the refresh is a no-op unless nunchi is on
  printf '{"a":1}\n' > "$SRC/s1.jsonl"
  CALLS="$H/calls"; : > "$CALLS"
}
run() { # [env...] — runs the refresh for provider claude on $SRC's parent
  env HOME="$H" NUNCHI_HOME="$NH" CCC_STATE_DIR="$H/.claude/state" MP_CALLS="$CALLS" PATH="$TMP/bin:/usr/bin:/bin" \
    CCC_NUNCHI_MEMPALACE_CLI="$TMP/bin/mempalace" "$@" \
    bash "$REFRESH" claude "$H/.claude/projects" >/dev/null 2>&1
}
calls() { wc -l < "$CALLS" | tr -d ' '; }
st() { python3 -c "import json;d=json.load(open('$NH/mempalace-refresh.status.json'));print(d['state'], d.get('note','-'))"; }

# --- default off outside Termux ------------------------------------------------
setup plain
run; run
ok "default (non-Termux) sweeps every run" '[ "$(calls)" = 2 ] && [ ! -e "$NH/mempalace-refresh.fingerprint.json" ]'

# --- forced on ---------------------------------------------------------------------
setup on
run CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=1; rc=$?
ok "first run sweeps and saves an owner-only fingerprint" '[ "$rc" = 0 ] && [ "$(calls)" = 1 ] && [ "$(stat -c %a "$NH/mempalace-refresh.fingerprint.json" 2>/dev/null || stat -f %Lp "$NH/mempalace-refresh.fingerprint.json")" = 600 ]'
run CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=1; rc=$?
ok "unchanged tree skips the sweep, status ok + note" '[ "$rc" = 0 ] && [ "$(calls)" = 1 ] && [ "$(st)" = "ok sweep-skipped-unchanged" ]'
ok "skipped status keeps the index verdict shape" 'python3 -c "import json,sys;d=json.load(open(\"$NH/mempalace-refresh.status.json\"));sys.exit(0 if set(d[\"index_status\"])=={\"state\",\"sqlite\",\"hnsw\"} else 1)"'
if [ -f "$PROBE" ]; then
  ok "probe reads a skipped status as ok" 'python3 -I -c "import importlib.util,sys,time;from pathlib import Path;sys.path.insert(0,str(Path(\"$PROBE\").resolve().parent));s=importlib.util.spec_from_file_location(\"p\",\"$PROBE\");m=importlib.util.module_from_spec(s);s.loader.exec_module(m);r=m.mempalace_refresh_probe(Path(\"$NH/mempalace-refresh.status.json\"),int(time.time()));sys.exit(0 if r[\"status\"]==\"ok\" else 1)"'
fi
printf '{"b":2}\n' >> "$SRC/s1.jsonl"
run CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=1
ok "a grown transcript is swept" '[ "$(calls)" = 2 ] && [ "$(st)" = "ok -" ]'
printf '{"c":3}\n' > "$SRC/s2.jsonl"
run CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=1
ok "a new transcript is swept" '[ "$(calls)" = 3 ]'
run CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=1
ok "then unchanged again skips" '[ "$(calls)" = 3 ]'

# --- the periodic full sweep ------------------------------------------------------
python3 - "$NH/mempalace-refresh.fingerprint.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["swept_at"] -= 7 * 3600
json.dump(d, open(p, "w"))
PY
run CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=1
ok "an unchanged tree older than MAX_SKIP (6 h) is swept anyway" '[ "$(calls)" = 4 ]'

# --- failures never arm a skip ----------------------------------------------------
setup failing
run CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=1 MP_RC=3; rc=$?
ok "a failed sweep records error and saves no fingerprint" '[ "$rc" = 3 ] && [ "$(st)" = "error -" ] && [ ! -e "$NH/mempalace-refresh.fingerprint.json" ]'
run CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=1
ok "the next run sweeps again" '[ "$(calls)" = 2 ] && [ "$(st)" = "ok -" ]'
python3 - "$NH/mempalace-refresh.status.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d.update(state="error", exit_code=1)
json.dump(d, open(p, "w"))
PY
run CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=1
ok "a previous error status never skips even with a matching fingerprint" '[ "$(calls)" = 3 ]'

# --- scope -------------------------------------------------------------------------
setup codexlane
mkdir -p "$H/sessions"; printf '{}\n' > "$H/sessions/x.jsonl"
for _ in 1 2; do env HOME="$H" NUNCHI_HOME="$NH" CCC_STATE_DIR="$H/.claude/state" MP_CALLS="$CALLS" PATH="$TMP/bin:/usr/bin:/bin" \
  CCC_NUNCHI_MEMPALACE_CLI="$TMP/bin/mempalace" CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=1 \
  bash "$REFRESH" codex "$H/sessions" >/dev/null 2>&1; done
ok "codex (mine) is never skipped" '[ "$(calls)" = 2 ] && grep -qx mine "$CALLS"'

setup termux /data/data/com.termux/files/home
run; run
ok "auto-on when HOME is a Termux home (cron may lack PREFIX)" '[ "$(calls)" = 1 ] && [ "$(st)" = "ok sweep-skipped-unchanged" ]'
run CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=0
ok "SKIP_UNCHANGED=0 forces the sweep on Termux" '[ "$(calls)" = 2 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
