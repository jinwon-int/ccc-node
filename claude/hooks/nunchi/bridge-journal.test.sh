#!/usr/bin/env bash
# Tests for the bridge distill-journal feed (#1018): the adapter's payload and
# exit contract, and ingest-cron.sh's use of it. No provider/network calls.
# shellcheck disable=SC2034 # assertion variables are consumed through ok/eval
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

ADAPTER="$ROOT/claude/hooks/nunchi/bridge-journal.py"

# ---------------------------------------------------------------- fixtures --
job() {
  # job <status> <honcho-json> [extra-top-level-json]
  python3 - "$1" "$2" "${3:-{\}}" <<'PY'
import json, sys
status, honcho, extra = sys.argv[1], sys.argv[2], sys.argv[3]
output = {
    "schema_version": 1,
    "provenance": {"provider": "claude", "source_thread_hash": "a" * 64,
                   "trigger": "shutdown", "distilled_at": "2026-08-07T05:46:53Z"},
    "honcho": json.loads(honcho),
    "wiki_candidates": [],
    "resume": {},
}
doc = {"status": status, "thread_id": "c47e5604-a299-4077-a9b8-e2ebee8e8b3e",
       "job_id": "j" * 8, "trigger": "shutdown",
       "updated_at": "2026-08-07T05:56:53.682782Z",
       "extraction_output": json.dumps(output)}
doc.update(json.loads(extra))
print(json.dumps(doc))
PY
}

FACT='[{"kind":"fact","subject":"node","text":"브릿지 저널에서 미러링된 사실"}]'

mkdir -p "$TMP/journal"
job extraction_done "$FACT" > "$TMP/journal/done.json"
job queued "$FACT" '{"extraction_output": null}' > "$TMP/journal/queued.json"
job extraction_done '[]' > "$TMP/journal/empty.json"
job extraction_terminal_failed "$FACT" '{"extraction_output": null}' > "$TMP/journal/failed.json"
printf 'not json' > "$TMP/journal/broken.json"

# ------------------------------------------------------------ adapter: ok --
out="$(python3 "$ADAPTER" "$TMP/journal/done.json")"; rc=$?
ok "finished job exits 0" "[ $rc -eq 0 ]"
ok "payload carries honcho items" \
  "printf '%s' \"\$out\" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if len(d[\"honcho\"])==1 else 1)'"
ok "session_id is the bridge thread id" \
  "printf '%s' \"\$out\" | grep -q 'c47e5604-a299-4077-a9b8-e2ebee8e8b3e'"
ok "distilled_at comes from provenance, not the journal mtime" \
  "printf '%s' \"\$out\" | grep -q '2026-08-07T05:46:53Z'"
ok "korean fact text survives without escaping to ascii" \
  "printf '%s' \"\$out\" | grep -q '미러링된'"

# ------------------------------------------------- adapter: exit contract --
python3 "$ADAPTER" "$TMP/journal/queued.json" >/dev/null 2>&1
ok "in-flight job exits 4 so the caller retries" "[ $? -eq 4 ]"
python3 "$ADAPTER" "$TMP/journal/empty.json" >/dev/null 2>&1
ok "finished job with no facts exits 3 so the caller stops re-reading" "[ $? -eq 3 ]"
python3 "$ADAPTER" "$TMP/journal/failed.json" >/dev/null 2>&1
ok "terminally failed job exits 3" "[ $? -eq 3 ]"
python3 "$ADAPTER" "$TMP/journal/broken.json" >/dev/null 2>&1
ok "unreadable job exits 1" "[ $? -eq 1 ]"
python3 "$ADAPTER" >/dev/null 2>&1
ok "missing argument exits 1" "[ $? -eq 1 ]"

# ------------------------------------------------- adapter: transcript path --
projects="$TMP/projects/-root"
mkdir -p "$projects"
printf '{}' > "$projects/c47e5604-a299-4077-a9b8-e2ebee8e8b3e.jsonl"
found="$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/claude/hooks/nunchi')
import importlib.util
spec = importlib.util.spec_from_file_location('bj', '$ADAPTER')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.transcript_for('c47e5604-a299-4077-a9b8-e2ebee8e8b3e', '$TMP/projects'))
print(m.transcript_for('../escape', '$TMP/projects'))
print(m.transcript_for('', '$TMP/projects'))
")"
ok "transcript is resolved from the thread id" \
  "printf '%s' \"\$found\" | head -1 | grep -q 'c47e5604.*\.jsonl'"
ok "path-traversal thread id resolves to nothing" \
  "[ -z \"\$(printf '%s' \"\$found\" | sed -n 2p)\" ]"
ok "empty thread id resolves to nothing" \
  "[ -z \"\$(printf '%s' \"\$found\" | sed -n 3p)\" ]"

# --------------------------------------------------------- ingest-cron.sh --
home="$TMP/home"
hooks="$home/.claude/hooks/nunchi"
state="$home/.claude/state"
mkdir -p "$hooks" "$state" "$home/.nunchi"
cp "$ROOT/claude/hooks/nunchi/ingest-cron.sh" "$hooks/ingest-cron.sh"
cp "$ADAPTER" "$hooks/bridge-journal.py"
chmod 700 "$hooks/ingest-cron.sh"
printf 'on' > "$state/nunchi.mode"

# Record every ingest call instead of touching a real DB.
cat > "$hooks/nunchi.py" <<PY
#!/usr/bin/env python3
import sys
if sys.argv[1:2] == ["ingest"]:
    body = sys.stdin.read() if sys.argv[2:3] == ["-"] else sys.argv[2]
    with open("$TMP/ingested.log", "a", encoding="utf-8") as fh:
        fh.write(body.replace("\\n", " ") + "\n")
PY

run_cron() {
  env -u CCC_BRIDGE_DISTILL_JOURNAL \
    HOME="$home" CCC_STATE_DIR="$state" NUNCHI_HOME="$home/.nunchi" \
    CCC_BRIDGE_DISTILL_JOURNAL="$TMP/journal" \
    bash "$hooks/ingest-cron.sh" 2>"$TMP/cron.err"
}

run_cron
ok "journal job is ingested" "grep -q '미러링된' '$TMP/ingested.log'"
ok "only the finished job is ingested" "[ \$(wc -l < '$TMP/ingested.log') -eq 1 ]"
ok "finished jobs are marked seen" "grep -qxF '$TMP/journal/done.json' '$home/.nunchi/ingested-files'"
ok "empty finished job is marked seen" "grep -qxF '$TMP/journal/empty.json' '$home/.nunchi/ingested-files'"
ok "in-flight job is NOT marked seen" \
  "! grep -qxF '$TMP/journal/queued.json' '$home/.nunchi/ingested-files'"

run_cron
ok "second tick does not re-ingest a seen job" "[ \$(wc -l < '$TMP/ingested.log') -eq 1 ]"

# The in-flight job finishing later must be picked up without operator action.
job extraction_done "$FACT" > "$TMP/journal/queued.json"
run_cron
ok "job that finishes later is ingested on a subsequent tick" \
  "[ \$(wc -l < '$TMP/ingested.log') -eq 2 ]"

# --------------------------------------------- silent-empty-pipeline guard --
empty_home="$TMP/empty-home"
mkdir -p "$empty_home/.claude/state" "$empty_home/.nunchi"
printf 'on' > "$empty_home/.claude/state/nunchi.mode"
cp "$hooks/nunchi.py" "$TMP/nunchi-stub.py"
HOME="$empty_home" CCC_STATE_DIR="$empty_home/.claude/state" \
  NUNCHI_HOME="$empty_home/.nunchi" \
  CCC_BRIDGE_DISTILL_JOURNAL="$TMP/nonexistent-journal" \
  bash "$hooks/ingest-cron.sh" 2>"$TMP/empty.err"
ok "warns when neither input source exists" "grep -q 'no input source' '$TMP/empty.err'"
ok "normal run stays quiet" "! grep -q 'no input source' '$TMP/cron.err'"

# ------------------------------------------------------- ingest receipt (#1018) --
# The receipt is what lets readiness tell "ran with no input" from "ran fine".
receipt="$home/.nunchi/ingest.status.json"
ok "a tick leaves a receipt" "[ -f '$receipt' ]"
ok "receipt carries the versioned schema" \
  "grep -q '\"schema\":\"ccc.nunchi.ingest.v1\"' '$receipt'"
ok "receipt counts the present input sources" \
  "python3 -c 'import json,sys; sys.exit(0 if json.load(open(\"$receipt\"))[\"sources\"]==1 else 1)'"
ok "receipt counts what was ingested" \
  "python3 -c 'import json,sys; sys.exit(0 if json.load(open(\"$receipt\"))[\"ingested\"]>=1 else 1)'"
ok "receipt timestamp is a positive integer" \
  "python3 -c 'import json,sys; d=json.load(open(\"$receipt\")); sys.exit(0 if type(d[\"finished_at\"]) is int and d[\"finished_at\"]>0 else 1)'"

empty_receipt="$empty_home/.nunchi/ingest.status.json"
ok "sourceless tick still leaves a receipt" "[ -f '$empty_receipt' ]"
ok "sourceless receipt records zero sources" \
  "python3 -c 'import json,sys; sys.exit(0 if json.load(open(\"$empty_receipt\"))[\"sources\"]==0 else 1)'"

# A tick that ingests something says so; a tick with nothing new stays quiet so
# a 10-minute cron does not write 144 no-op lines a day.
tick_stdout() {
  env HOME="$home" CCC_STATE_DIR="$state" NUNCHI_HOME="$home/.nunchi" \
    CCC_BRIDGE_DISTILL_JOURNAL="$TMP/journal" \
    bash "$hooks/ingest-cron.sh" 2>/dev/null
}
job extraction_done '[{"kind":"fact","subject":"node","text":"로그 검증용 신규 사실"}]' \
  > "$TMP/journal/logged.json"
tick_stdout > "$TMP/loud.out"
ok "a working tick logs its counts" "grep -q 'nunchi ingest: ingested=1' '$TMP/loud.out'"
tick_stdout > "$TMP/quiet.out"
ok "a tick with nothing new stays silent" "[ ! -s '$TMP/quiet.out' ]"

# nunchi disabled must stay a no-op regardless of journal contents
printf 'off' > "$state/nunchi.mode"
before="$(wc -l < "$TMP/ingested.log")"
run_cron
ok "disabled nunchi ingests nothing" "[ \$(wc -l < '$TMP/ingested.log') -eq $before ]"

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
