#!/usr/bin/env bash
# Hermetic tests for the audience-scoped Claude nunchi lane (#1921):
# ingest-cron.sh with CCC_NUNCHI_AUDIENCE_SCOPED=1 hands off to
# claude-audience-feed.py, which routes every bridge distill job to exactly one
# <root>/<scope>/nunchi store through the bridge's per-turn
# session_id -> audience sidecar. No mapping, two mappings or a bad mapping
# must never be collected — anywhere — and must be counted in the tick.
# No provider/network calls: nunchi.py is a recording stub.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
umask 077

home="$TMP/home"
hooks="$home/.claude/hooks/nunchi"
state="$home/.claude/state"
journal="$TMP/journal"
aud="$TMP/audiences"
PRIV="private-0123456789abcdef0123456789abcdef"
mkdir -p "$hooks" "$state" "$home/.nunchi" "$journal" "$aud/$PRIV" "$aud/shared"
chmod 700 "$aud" "$aud/$PRIV" "$aud/shared"
for f in ingest-cron.sh feed-common.sh bridge-journal.py claude-audience-feed.py; do
  cp "$ROOT/claude/hooks/nunchi/$f" "$hooks/$f"
done
printf 'on' > "$state/nunchi.mode"

# Recording stub: one line per call — "<verb>\t<NUNCHI_DB>\t<payload>".
cat > "$hooks/nunchi.py" <<PY
#!/usr/bin/env python3
import os, sys
verb = sys.argv[1] if len(sys.argv) > 1 else ""
body = sys.stdin.read().replace("\\n", " ") if sys.argv[2:3] == ["-"] else ""
with open("$TMP/calls.log", "a", encoding="utf-8") as fh:
    fh.write(f"{verb}\t{os.environ.get('NUNCHI_DB', '')}\t{body}\n")
PY

SID_OWNER="aaaaaaaa-0000-4000-8000-000000000001"
SID_ROOM="bbbbbbbb-0000-4000-8000-000000000002"
SID_NONE="cccccccc-0000-4000-8000-000000000003"
SID_BOTH="dddddddd-0000-4000-8000-000000000004"
SID_BAD="eeeeeeee-0000-4000-8000-000000000005"
SID_LIE="ffffffff-0000-4000-8000-000000000006"

sidecar() {  # <scope> <sid> [kind-override] [scope-override]
  local scope="$1" sid="$2" kind="${3:-}" rscope="${4:-$1}" dir
  [ -n "$kind" ] || { kind=private; [ "$scope" = shared ] && kind=shared; }
  dir="$aud/$scope/claude/session-map"
  mkdir -p "$dir"; chmod 700 "$aud/$scope/claude" "$dir"
  printf '{"schema":"ccc.claude.session-audience.v1","provider":"claude","session_id":"%s","memory_audience":"%s","memory_scope":"%s","updated_at":"2026-09-29T00:00:00+00:00"}\n' \
    "$sid" "$kind" "$rscope" > "$dir/$sid.json"
  chmod 600 "$dir/$sid.json"
}

job() {  # <file> <sid> <fact-text> [status] [declared-audience declared-scope]
  python3 - "$@" <<'PY'
import json, sys
path, sid, text = sys.argv[1:4]
status = sys.argv[4] if len(sys.argv) > 4 else "extraction_done"
job = {"job_id": path, "thread_id": sid, "status": status,
       "updated_at": "2026-09-29T00:00:00+00:00", "trigger": "checkpoint"}
if text:
    job["extraction_output"] = json.dumps({"honcho": [{"kind": "fact", "subject": "user", "text": text}]})
if len(sys.argv) > 6:
    job["memory_audience"], job["memory_scope"] = sys.argv[5], sys.argv[6]
json.dump(job, open(path, "w"), ensure_ascii=False)
PY
}

run_cron() {  # scoped tick; stdout -> $TMP/out, stderr -> $TMP/err
  env -u CCC_BRIDGE_DISTILL_JOURNAL -u CCC_AGENT_PROVIDER \
    HOME="$home" CCC_STATE_DIR="$state" NUNCHI_HOME="$home/.nunchi" \
    NUNCHI_DB="$home/.nunchi/facts.db" NUNCHI_SNAPSHOT="$home/.nunchi/snapshot.md" \
    CCC_BRIDGE_ENV_FILE="$TMP/no-such.env" \
    CCC_BRIDGE_DISTILL_JOURNAL="$journal" \
    CCC_NUNCHI_AUDIENCE_SCOPED=1 CCC_NUNCHI_AUDIENCE_ROOT="$aud" \
    bash "$hooks/ingest-cron.sh" >"$TMP/out" 2>"$TMP/err"
}
status_field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]))' "$home/.nunchi/ingest.status.json" "$1"; }
ingests_into() { awk -F'\t' -v db="$1" '$1=="ingest" && $2==db' "$TMP/calls.log" 2>/dev/null; }

# --- 0. no sidecar at all: fail closed, loudly --------------------------------
job "$journal/owner.json" "$SID_OWNER" "owner-dm-fact" extraction_done private "$PRIV"
run_cron
ok "no sidecar: nothing is ingested anywhere" "[ ! -s '$TMP/calls.log' ] || ! grep -q '^ingest' '$TMP/calls.log'"
ok "no sidecar: the tick says why (skipped=no-audience-sidecar)" "[ \"\$(status_field skipped)\" = no-audience-sidecar ]"
ok "no sidecar: the item is counted as unmapped" "[ \"\$(status_field unmapped)\" = 1 ]"
ok "no sidecar: stderr names the fail-closed state" "grep -q 'no Claude session->audience sidecar' '$TMP/err'"
ok "no sidecar: the job is left unseen for a later tick" "! grep -qxF '$journal/owner.json' '$home/.nunchi/ingested-files'"

# --- 1. routed, unmapped, ambiguous and invalid in one tick -------------------
sidecar "$PRIV" "$SID_OWNER"
sidecar shared "$SID_ROOM"
sidecar "$PRIV" "$SID_BOTH"; sidecar shared "$SID_BOTH"
sidecar shared "$SID_BAD" private "$PRIV"           # record lies about its scope
sidecar shared "$SID_LIE"
job "$journal/room.json" "$SID_ROOM" "family-room-fact" extraction_done shared shared
job "$journal/none.json" "$SID_NONE" "unmapped-fact" extraction_done private "$PRIV"
job "$journal/both.json" "$SID_BOTH" "ambiguous-fact" extraction_done shared shared
job "$journal/bad.json" "$SID_BAD" "bad-sidecar-fact" extraction_done shared shared
job "$journal/lie.json" "$SID_LIE" "declared-route-mismatch-fact" extraction_done private "$PRIV"
SID_LEGACY="abababab-0000-4000-8000-000000000009"
sidecar shared "$SID_LEGACY"
job "$journal/legacy.json" "$SID_LEGACY" "routeless-legacy-job-fact"   # no route of its own
job "$journal/queued.json" "$SID_OWNER" "" running
job "$journal/empty.json" "$SID_OWNER" "" extraction_done
: > "$TMP/calls.log"
run_cron
rc=$?
ok "scoped tick exits 0" "[ $rc = 0 ]"
ok "owner DM job lands in the private scope DB only" \
  "ingests_into '$aud/$PRIV/nunchi/facts.db' | grep -q 'owner-dm-fact'"
ok "family room job lands in the shared scope DB only" \
  "ingests_into '$aud/shared/nunchi/facts.db' | grep -q 'family-room-fact'"
ok "routed payload carries the sidecar route" \
  "ingests_into '$aud/shared/nunchi/facts.db' | grep -q '\"memory_audience\": \"shared\", \"memory_scope\": \"shared\"'"
ok "nothing crosses scopes (room fact not in private, DM fact not in shared)" \
  "! ingests_into '$aud/$PRIV/nunchi/facts.db' | grep -q family-room-fact && ! ingests_into '$aud/shared/nunchi/facts.db' | grep -q owner-dm-fact"
ok "the node-global DB is never written in scoped mode" \
  "[ -z \"\$(ingests_into '$home/.nunchi/facts.db')\" ]"
for leaked in unmapped-fact ambiguous-fact bad-sidecar-fact declared-route-mismatch-fact routeless-legacy-job-fact; do
  ok "fail-closed: $leaked is collected nowhere" "! grep -q '$leaked' '$TMP/calls.log'"
done
ok "tick counts: ingested=2" "[ \"\$(status_field ingested)\" = 2 ]"
ok "tick counts: unmapped=1" "[ \"\$(status_field unmapped)\" = 1 ]"
ok "tick counts: ambiguous=1" "[ \"\$(status_field ambiguous)\" = 1 ]"
ok "tick counts: invalid=3 (lying record, route mismatch, routeless job)" "[ \"\$(status_field invalid)\" = 3 ]"
ok "tick counts: in-flight job deferred" "[ \"\$(status_field deferred)\" = 1 ]"
ok "tick counts: terminal empty job retired" "[ \"\$(status_field retired)\" = 1 ]"
ok "tick is the claude lane, audience-scoped, no skip reason" \
  "[ \"\$(status_field feed)\" = claude ] && [ \"\$(status_field audience_scoped)\" = True ] && [ \"\$(status_field skipped)\" = None ]"
ok "routed jobs are marked seen" \
  "grep -qxF '$journal/owner.json' '$home/.nunchi/ingested-files' && grep -qxF '$journal/room.json' '$home/.nunchi/ingested-files'"
ok "skipped jobs are NOT marked seen" \
  "! grep -qE '/(none|both|bad|lie|legacy|queued)\\.json\$' '$home/.nunchi/ingested-files'"
ok "each touched scope gets a snapshot" \
  "[ \"\$(awk -F'\t' '\$1==\"snapshot\"' '$TMP/calls.log' | wc -l | tr -d ' ')\" = 2 ]"
ok "scope nunchi homes are owner-only" \
  "[ \"\$(stat -c %a '$aud/$PRIV/nunchi')\" = 700 ] && [ \"\$(stat -c %a '$aud/shared/nunchi')\" = 700 ]"
ok "the log line is body-free (counts only)" \
  "grep -q 'unmapped=1 ambiguous=1 invalid=3' '$TMP/out' && ! grep -qE 'fact|$SID_NONE|$PRIV' '$TMP/out'"

# --- 2. a session that gains a sidecar later is routed on a later tick --------
: > "$TMP/calls.log"
run_cron
ok "second tick re-ingests nothing already seen" "! grep -q 'owner-dm-fact\\|family-room-fact' '$TMP/calls.log'"
sidecar "$PRIV" "$SID_NONE"
run_cron
ok "a late sidecar routes the previously unmapped job" \
  "ingests_into '$aud/$PRIV/nunchi/facts.db' | grep -q 'unmapped-fact'"

# --- 2b. node-wide distill-history is never an input (privacy review repro) --
# The owner runs `claude --resume <family-room sid>` in a terminal; that CLI
# distill lands in the node-wide ~/.claude/state/distill-history under the
# room's session id, which HAS a shared sidecar. Routing it by session id
# alone would push the owner's private CLI facts into the shared store.
hist_item() {  # <dir> <file> <sid> <fact-text>
  mkdir -p "$1"
  printf '{"session_id":"%s","honcho":[{"kind":"fact","subject":"user","text":"%s"}]}\n' "$3" "$4" > "$1/$2"
  chmod 600 "$1/$2"
}
hist_item "$state/distill-history" cli.json "$SID_ROOM" "owner-terminal-cli-fact"
: > "$TMP/calls.log"
run_cron
ok "node-wide CLI distill with a shared-mapped sid is ingested nowhere" \
  "! grep -q 'owner-terminal-cli-fact' '$TMP/calls.log'"
ok "and in particular not into the shared store" \
  "[ -z \"\$(ingests_into '$aud/shared/nunchi/facts.db' | grep owner-terminal-cli-fact)\" ]"

# --- 2c. a scope's own distill-history feeds only that scope ------------------
mkdir -p "$aud/shared/state" "$aud/$PRIV/state"; chmod 700 "$aud/shared/state" "$aud/$PRIV/state"
hist_item "$aud/shared/state/distill-history" h1.json "$SID_ROOM" "scoped-history-room-fact"
hist_item "$aud/$PRIV/state/distill-history" h2.json "$SID_ROOM" "misplaced-history-fact"
: > "$TMP/calls.log"
run_cron
ok "per-scope history whose sidecar agrees is ingested into its own scope" \
  "ingests_into '$aud/shared/nunchi/facts.db' | grep -q 'scoped-history-room-fact'"
ok "per-scope history whose sidecar maps elsewhere is invalid and collected nowhere" \
  "! grep -q 'misplaced-history-fact' '$TMP/calls.log' && [ \"\$(status_field invalid)\" -ge 1 ]"

# --- 2d. a steady rejection backlog does not re-log every tick ----------------
run_cron
: > "$TMP/calls.log"
run_cron
ok "unchanged counts: the tick writes no log line" "[ ! -s '$TMP/out' ]"
ok "unchanged counts: the status still reports the backlog" "[ \"\$(status_field invalid)\" -ge 1 ]"

# --- 3. unsafe sidecars fail closed -------------------------------------------
SID_PERM="12121212-0000-4000-8000-000000000007"
sidecar shared "$SID_PERM"; chmod 644 "$aud/shared/claude/session-map/$SID_PERM.json"
job "$journal/perm.json" "$SID_PERM" "world-readable-sidecar-fact" extraction_done shared shared
SID_LINK="34343434-0000-4000-8000-000000000008"
sidecar shared "$SID_LINK"
mv "$aud/shared/claude/session-map/$SID_LINK.json" "$TMP/real-sidecar.json"
ln -s "$TMP/real-sidecar.json" "$aud/shared/claude/session-map/$SID_LINK.json"
job "$journal/link.json" "$SID_LINK" "symlinked-sidecar-fact" extraction_done shared shared
: > "$TMP/calls.log"
run_cron
ok "a group/other-readable sidecar is not trusted" "! grep -q 'world-readable-sidecar-fact' '$TMP/calls.log'"
ok "a symlinked sidecar is not trusted" "! grep -q 'symlinked-sidecar-fact' '$TMP/calls.log'"

# --- 4. an unsafe audience root yields no routes ------------------------------
chmod 755 "$aud"
job "$journal/open.json" "$SID_ROOM" "open-root-fact" extraction_done shared shared
: > "$TMP/calls.log"
run_cron
ok "a group/other-accessible audience root routes nothing" "! grep -q 'open-root-fact' '$TMP/calls.log'"
ok "and reports zero sidecars" "[ \"\$(status_field sidecars)\" = 0 ]"
chmod 700 "$aud"

# --- 4b. stale sidecars are pruned unless their session still has input -----
SID_OLD="56565656-0000-4000-8000-00000000000a"
SID_OLD_PENDING="78787878-0000-4000-8000-00000000000b"
sidecar shared "$SID_OLD"; sidecar shared "$SID_OLD_PENDING"
job "$journal/old-pending.json" "$SID_OLD_PENDING" "" running
touch -d '100 days ago' "$aud/shared/claude/session-map/$SID_OLD.json" \
  "$aud/shared/claude/session-map/$SID_OLD_PENDING.json"
CCC_NUNCHI_CLAUDE_SIDECAR_MAX_AGE_DAYS=0 run_cron
ok "max age 0 disables pruning" "[ -f '$aud/shared/claude/session-map/$SID_OLD.json' ]"
run_cron
ok "a sidecar older than 90 days with no pending input is pruned" \
  "[ ! -e '$aud/shared/claude/session-map/$SID_OLD.json' ] && [ \"\$(status_field pruned)\" -ge 1 ]"
ok "an old sidecar whose session still has a pending job is kept" \
  "[ -f '$aud/shared/claude/session-map/$SID_OLD_PENDING.json' ]"
ok "fresh sidecars are kept" "[ -f '$aud/shared/claude/session-map/$SID_ROOM.json' ]"

# --- 5. the unscoped Claude lane is unchanged ---------------------------------
: > "$TMP/calls.log"
env -u CCC_BRIDGE_DISTILL_JOURNAL HOME="$home" CCC_STATE_DIR="$state" \
  NUNCHI_HOME="$TMP/global-nunchi" NUNCHI_DB="$TMP/global.db" \
  CCC_BRIDGE_ENV_FILE="$TMP/no-such.env" CCC_BRIDGE_DISTILL_JOURNAL="$journal" \
  bash "$hooks/ingest-cron.sh" >/dev/null 2>&1
ok "without the scoped flag the global lane still mirrors into NUNCHI_DB" \
  "[ -n \"\$(ingests_into '$TMP/global.db')\" ]"

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
