#!/usr/bin/env bash
# Hermetic curator lifecycle tests (#752): telemetry, deterministic
# stale/archive transitions, protection rules, backup/rollback, crash
# recovery and fail-closed boundaries. No provider calls, no network.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL="$HERE/curator.py"
OWN="$HERE/ownership.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0
fail=0

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

STATE="$TMP/state"
SKILLS="$TMP/skills"
mkdir -m 700 "$STATE" "$SKILLS"

tool() {
  python3 "$TOOL" --provider claude --skills-dir "$SKILLS" --state-dir "$STATE" "$@"
}

own() {
  python3 "$OWN" --provider claude --skills-dir "$SKILLS" --state-dir "$STATE" "$@"
}

at() { # <days-from-now> <cmd...> — pin the curator clock deterministically
  local days="$1"; shift
  CCC_SKILL_CURATOR_NOW="$(python3 -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(days=$days)).isoformat())")" tool "$@"
}

make_skill() {
  local name="$1"
  mkdir -m 700 "$SKILLS/$name"
  printf -- '---\nname: %s\ndescription: A sufficiently detailed recurring workflow for curator lifecycle tests.\n---\n\n# %s\n\n## Steps\n1. Read.\n2. Verify.\n3. Record.\n' "$name" "$name" > "$SKILLS/$name/SKILL.md"
  chmod 600 "$SKILLS/$name/SKILL.md"
}

make_managed() { # autosave-managed via the real ownership contract
  local name="$1"
  make_skill "$name"
  own mark-created "$name" >/dev/null
}

ledger_rows() {
  wc -l < "$STATE/skill-autosave-ownership.jsonl" 2>/dev/null | tr -d '[:space:]'
}

# --- 1. telemetry: untracked → seed → bump -----------------------------------
make_managed alpha
out="$(tool status alpha)"
ok "untracked skill has no telemetry" 'jq -e ".skills[0].telemetry == null" >/dev/null <<<"$out"'
out="$(tool run)"
ok "first sight seeds without changing" 'jq -e ".counts.seeded == 1 and (.changed | not)" >/dev/null <<<"$out"'
out="$(tool run)"
ok "seeded young skill is kept" 'jq -e ".counts.kept == 1 and (.changed | not)" >/dev/null <<<"$out"'
out="$(tool bump --event use --name alpha)"
ok "bump records" 'jq -e ".recorded == true" >/dev/null <<<"$out"'
tool bump --event use --name alpha >/dev/null
tool bump --event view --name alpha >/dev/null
out="$(tool status alpha)"
ok "bump counts accumulate body-free" 'jq -e ".skills[0].telemetry.use_count == 2 and .skills[0].telemetry.view_count == 1 and .skills[0].telemetry.state == \"active\"" >/dev/null <<<"$out"'
out="$(tool bump --event use --name no-such-skill)"; rc=$?
ok "bump on missing skill is fail-open" '[ "$rc" -eq 0 ] && jq -e ".recorded == false" >/dev/null <<<"$out"'
out="$(tool bump --event use --name "BAD NAME")"; rc=$?
ok "bump on invalid name is fail-open" '[ "$rc" -eq 0 ] && jq -e ".recorded == false" >/dev/null <<<"$out"'
ok "usage file is owner-only" '[ "$(stat -c %a "$STATE/skill-autosave-usage.json")" = "600" ]'
ok "usage store is body-free" '! grep -q -E "description|Steps|workflow" "$STATE/skill-autosave-usage.json"'

# --- 2. deterministic transitions --------------------------------------------
out="$(at 40 run)"
ok "40d idle never-used → stale (display only)" 'jq -e ".counts.marked_stale == 1" >/dev/null <<<"$out"'
ok "stale skill stays on disk" '[ -d "$SKILLS/alpha" ]'
out="$(at 40 status alpha)"
ok "state reports stale" 'jq -e ".skills[0].telemetry.state == \"stale\"" >/dev/null <<<"$out"'
out="$(at 45 run)"
ok "stale stays stale within archive window" 'jq -e ".counts.kept == 1" >/dev/null <<<"$out"'
out="$(at 100 run)"
ok "100d idle → archived with pre-run backup" 'jq -e ".counts.archived == 1 and (.backup.backup_id | type) == \"string\"" >/dev/null <<<"$out"'
ok "archived skill left the live dir" '[ ! -e "$SKILLS/alpha" ]'
ok "archive root holds exactly one entry" '[ "$(ls "$STATE/skill-autosave-archive" | wc -l)" = "1" ]'
ok "archive root is owner-only" '[ "$(stat -c %a "$STATE/skill-autosave-archive")" = "700" ]'
out="$(at 100 list-archived)"
ok "list-archived tracks the entry" 'jq -e ".archived | length == 1 and .[0].name == \"alpha\" and .[0].tracked" >/dev/null <<<"$out"'
out="$(at 100 run)"
ok "rerun after archive is a no-op" 'jq -e ".counts.archived == 0 and (.changed | not)" >/dev/null <<<"$out"'

# --- 3. restore ---------------------------------------------------------------
out="$(at 100 restore alpha)"
ok "restore moves the skill back" 'jq -e ".changed == true" >/dev/null <<<"$out" && [ -f "$SKILLS/alpha/SKILL.md" ]'
ok "restored skill keeps its autosave marker" '[ -f "$SKILLS/alpha/.autosave-meta.json" ]'
out="$(at 100 status alpha)"
ok "restored skill is active" 'jq -e ".skills[0].telemetry.state == \"active\"" >/dev/null <<<"$out"'
out="$(at 100 restore alpha)"; rc=$?
ok "double restore fails closed" '[ "$rc" -eq 2 ] && jq -e ".code == \"restore_denied_not_archived\"" >/dev/null <<<"$out"'

# --- 4. protection rules ------------------------------------------------------
make_managed beta
make_skill userone
mkdir -m 700 "$SKILLS/managedone" && printf -- '---\nname: managedone\ndescription: A sufficiently detailed recurring managed workflow for curator tests.\n---\n\n# managedone\n' > "$SKILLS/managedone/SKILL.md" && chmod 600 "$SKILLS/managedone/SKILL.md"
printf '{"schema_version":1,"manager":"ccc-node","name":"managedone","source":"test","source_hash":"abc","files":{}}' > "$SKILLS/managedone/.ccc-node-managed.json"
tool run >/dev/null  # seed beta
own pin beta >/dev/null
out="$(at 200 run)"
ok "pinned skill is protected at 200d" 'jq -e "[.decisions[] | select(.name == \"beta\" and .action == \"protect\" and .reason == \"pinned\")] | length == 1" >/dev/null <<<"$out"'
ok "pinned skill stays live" '[ -d "$SKILLS/beta" ]'
ok "user-owned skill is never auto-archived" '[ -d "$SKILLS/userone" ] && jq -e "[.decisions[] | select(.name == \"userone\" and .action == \"protect\")] | length == 1" >/dev/null <<<"$out"'
ok "managed/bundled skill is never auto-archived" '[ -d "$SKILLS/managedone" ] && jq -e "[.decisions[] | select(.name == \"managedone\" and .action == \"protect\")] | length == 1" >/dev/null <<<"$out"'

# --- 5. dry-run is mutation-free ----------------------------------------------
make_managed gamma
tool run >/dev/null
before_ledger="$(ledger_rows)"
before_backups="$(ls "$STATE/skill-autosave-curator-backups" 2>/dev/null | wc -l)"
out="$(at 300 run --dry-run)"
ok "dry-run reports the would-archive" 'jq -e ".counts.archived == 1 and .dry_run == true" >/dev/null <<<"$out"'
ok "dry-run leaves the skill live" '[ -d "$SKILLS/gamma" ]'
ok "dry-run appends no ledger rows" '[ "$(ledger_rows)" = "$before_ledger" ]'
ok "dry-run takes no backup" '[ "$(ls "$STATE/skill-autosave-curator-backups" 2>/dev/null | wc -l)" = "$before_backups" ]'
ok "dry-run does not advance run state" '! grep -q "run_count.: .[1-9]" "$STATE/skill-autosave-curator-state.json" 2>/dev/null'

# --- 6. manual archive / restore fail-closed ----------------------------------
out="$(tool archive gamma)"
ok "manual archive works" 'jq -e ".changed == true" >/dev/null <<<"$out" && [ ! -e "$SKILLS/gamma" ]'
mkdir -m 700 "$SKILLS/gamma"
out="$(tool restore gamma)"; rc=$?
ok "restore refuses to shadow a live dir" '[ "$rc" -eq 2 ] && jq -e ".code == \"restore_denied_live_exists\"" >/dev/null <<<"$out"'
rmdir "$SKILLS/gamma"
out="$(tool restore gamma)"
ok "restore succeeds once the path is clear" 'jq -e ".changed == true" >/dev/null <<<"$out"'
out="$(tool archive userone)"; rc=$?
ok "manual archive of user-owned is denied" '[ "$rc" -eq 2 ] && jq -e ".code | startswith(\"lifecycle_denied_\")" >/dev/null <<<"$out"'
out="$(tool archive beta)"; rc=$?
ok "manual archive of pinned is denied" '[ "$rc" -eq 2 ] && jq -e ".code == \"lifecycle_denied_pinned\"" >/dev/null <<<"$out"'

# --- 7. backup retention + rollback -------------------------------------------
for i in 1 2 3 4 5 6 7; do
  at "$((300 + i))" backup --reason "retention-test" >/dev/null
done
count="$(ls "$STATE/skill-autosave-curator-backups" | wc -l)"
ok "backup retention keeps only the newest 5" '[ "$count" -eq 5 ]'
out="$(tool list-backups)"
ok "list-backups is readable" 'jq -e "[.backups[] | select(.readable)] | length == 5" >/dev/null <<<"$out"'

# rollback: archive beta-class skill, then roll back to the pre-archive backup
make_managed delta
tool run >/dev/null
out="$(at 400 run)"
ok "delta archived at 400d" 'jq -e "[.decisions[] | select(.name == \"delta\" and .action == \"archive\")] | length == 1" >/dev/null <<<"$out" && [ ! -e "$SKILLS/delta" ]'
pre_archive_backup="$(jq -r ".backup.backup_id" <<<"$out")"
out="$(at 400 rollback --id "$pre_archive_backup" --dry-run)"
ok "rollback dry-run plans restore-archived" 'jq -e "[.planned[] | select(.name == \"delta\" and .action == \"restore-archived\")] | length == 1" >/dev/null <<<"$out"'
ok "rollback dry-run changes nothing" '[ ! -e "$SKILLS/delta" ]'
out="$(at 400 rollback --id "$pre_archive_backup")"
ok "rollback restores the archived skill" 'jq -e ".changed == true" >/dev/null <<<"$out" && [ -f "$SKILLS/delta/SKILL.md" ]'
ok "rollback takes a safety snapshot first" 'jq -e ".safety_backup_id != \"$pre_archive_backup\"" >/dev/null <<<"$out"'
out="$(at 400 status delta)"
ok "rolled-back skill is active again" 'jq -e ".skills[0].telemetry.state == \"active\"" >/dev/null <<<"$out"'
out="$(at 400 rollback --id "bad id")"; rc=$?
ok "rollback rejects a malformed id" '[ "$rc" -eq 2 ] && jq -e ".code == \"backup_id_invalid\"" >/dev/null <<<"$out"'
out="$(at 400 rollback --id "2000-01-01T00-00-00Z")"; rc=$?
ok "rollback fails closed on a missing backup" '[ "$rc" -eq 2 ] && jq -e ".code == \"backup_missing\"" >/dev/null <<<"$out"'

# --- 8. crash recovery ---------------------------------------------------------
make_managed epsilon
tool run >/dev/null
# Simulate a crash: durable prepared row, physical move, no terminal row.
mv "$SKILLS/epsilon" "$TMP/epsilon-parked"
txid="deadbeefdeadbeefdeadbeefdeadbeef"
arch_name="epsilon.20990101000000.deadbeef"
mkdir -m 700 "$STATE/skill-autosave-archive" 2>/dev/null || true
mv "$TMP/epsilon-parked" "$STATE/skill-autosave-archive/$arch_name"
printf '{"schema_version":1,"event":"curator-archive","transaction_id":"%s","ts":"2099-01-01T00:00:00Z","outcome":"prepared","provider":"claude","name":"epsilon","target_id":"%s","archive_name":"%s","archived_at":"2099-01-01T00:00:00Z","trigger":"automatic","skill_sha256":"00"}\n' \
  "$txid" "$(python3 -c "import hashlib,os; root=hashlib.sha256(os.fsencode(os.path.abspath('$SKILLS'))).hexdigest(); print(hashlib.sha256(f'claude\0{root}\0epsilon'.encode()).hexdigest())")" "$arch_name" \
  >> "$STATE/skill-autosave-ownership.jsonl"
out="$(tool run)"
ok "crash recovery finishes the dangling archive" 'jq -e "[.recoveries[] | select(.transaction_id == \"'$txid'\" and .outcome == \"archived\")] | length == 1" >/dev/null <<<"$out"'
out="$(tool status epsilon)"
ok "recovered skill tracks archived state" 'jq -e ".skills[0].telemetry.state == \"archived\"" >/dev/null <<<"$out"'
out="$(tool run)"
ok "recovery is idempotent on rerun" 'jq -e "(.recoveries // []) | length == 0" >/dev/null <<<"$out"'
out="$(at 500 restore epsilon)"
ok "recovered skill restores normally" 'jq -e ".changed == true" >/dev/null <<<"$out"'

# --- 9. auto gating ------------------------------------------------------------
out="$(at 600 run --auto)"
ok "auto run is disabled by default" 'jq -e ".skipped == \"curator-disabled\"" >/dev/null <<<"$out"'
export CCC_SKILL_CURATOR_ENABLED=true
NEWTMP="$(mktemp -d)"; NSTATE="$NEWTMP/state"; NSKILLS="$NEWTMP/skills"
mkdir -m 700 "$NSTATE" "$NSKILLS"
ntool() { python3 "$TOOL" --provider claude --skills-dir "$NSKILLS" --state-dir "$NSTATE" "$@"; }
out="$(ntool run --auto)"
ok "first auto run only seeds the interval timer" 'jq -e ".skipped == \"first-run-deferred\"" >/dev/null <<<"$out"'
out="$(ntool run --auto)"
ok "auto run respects the interval" 'jq -e ".skipped == \"interval-not-elapsed\"" >/dev/null <<<"$out"'
mkdir -m 700 "$NSKILLS/zeta"
printf -- '---\nname: zeta\ndescription: A sufficiently detailed recurring workflow for curator auto tests.\n---\n\n# zeta\n' > "$NSKILLS/zeta/SKILL.md"
chmod 600 "$NSKILLS/zeta/SKILL.md"
python3 "$OWN" --provider claude --skills-dir "$NSKILLS" --state-dir "$NSTATE" mark-created zeta >/dev/null
out="$(CCC_SKILL_CURATOR_NOW="$(python3 -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(days=2)).isoformat())")" ntool run --auto)"
ok "auto run proceeds after the interval" 'jq -e ".counts.seeded == 1" >/dev/null <<<"$out"'
# Record activity just 1h behind the pinned run clock → inside the min-idle gate.
CCC_SKILL_CURATOR_NOW="$(python3 -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(days=3)).isoformat())")" ntool bump --event use --name zeta >/dev/null
out="$(CCC_SKILL_CURATOR_NOW="$(python3 -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(days=3,hours=1)).isoformat())")" ntool run --auto)"
ok "auto run skips while the node is active within min-idle" 'jq -e ".skipped == \"node-active-within-min-idle\"" >/dev/null <<<"$out"'
unset CCC_SKILL_CURATOR_ENABLED
rm -rf "$NEWTMP"

# --- 10. configuration + fail-closed boundaries --------------------------------
make_managed eta
tool run >/dev/null
out="$(CCC_SKILL_CURATOR_STALE_AFTER_DAYS=5 at 6 run)"
ok "configurable stale threshold applies" 'jq -e "[.decisions[] | select(.name == \"eta\" and .action == \"mark-stale\")] | length == 1" >/dev/null <<<"$out"'
out="$(CCC_SKILL_CURATOR_STALE_AFTER_DAYS=0 at 7 run)"; rc=$?
ok "out-of-range threshold fails closed" '[ "$rc" -eq 2 ] && jq -e ".code == \"invalid_config_CCC_SKILL_CURATOR_STALE_AFTER_DAYS\"" >/dev/null <<<"$out"'
out="$(CCC_SKILL_CURATOR_CONSOLIDATE=true at 7 run)"; rc=$?
ok "consolidation flag fails closed with no provider call" '[ "$rc" -eq 2 ] && jq -e ".code == \"consolidation_not_implemented\"" >/dev/null <<<"$out"'
out="$(at 7 report)"
ok "report aggregates state and classification" 'jq -e ".totals.by_state.stale >= 1 and .totals.by_classification[\"autosave-managed\"] >= 1 and (.totals.backups | type) == \"number\"" >/dev/null <<<"$out"'
ok "report is body-free" '! grep -q -E "sufficiently detailed" <<<"$out"'
out="$(at 7 pin eta --dry-run)"
ok "curator exposes pin via the ownership contract" 'jq -e ".command == \"pin\" and .dry_run == true" >/dev/null <<<"$out"'

# --- 11. review-fix regressions ----------------------------------------------
# 11a. directory-symlink member → quarantined, never copied, run proceeds
make_managed theta
tool run >/dev/null
mkdir "$TMP/outside-data" && printf 'x' > "$TMP/outside-data/blob.bin"
ln -s "$TMP/outside-data" "$SKILLS/theta/linked-dir"
out="$(at 800 run)"
ok "dir-symlink skill is quarantined, not archived" 'jq -e "[.decisions[] | select(.name == \"theta\" and .action == \"quarantine\")] | length == 1" >/dev/null <<<"$out" && [ -d "$SKILLS/theta" ]'
ok "quarantine is reported at the top level" 'jq -e ".quarantined == [\"theta\"]" >/dev/null <<<"$out"'
backup_dir="$(jq -r '.backup.backup_id' <<<"$out")"
ok "quarantined skill is absent from the snapshot" '[ ! -e "$STATE/skill-autosave-curator-backups/$backup_dir/skills/theta" ]'
ok "outside data never enters the backup" '[ ! -e "$STATE/skill-autosave-curator-backups/$backup_dir/skills/theta/linked-dir/blob.bin" ]'
rm "$SKILLS/theta/linked-dir"
out="$(at 800 run)"
ok "after repair the skill transitions again" 'jq -e "[.decisions[] | select(.name == \"theta\" and .action == \"archive\")] | length == 1" >/dev/null <<<"$out"'

# 11b. rollback partial failure records conflict, not a clean abort
make_managed iota
make_managed kappa
tool run >/dev/null
out="$(tool backup --reason partial-test)"
partial_backup="$(jq -r '.backup_id' <<<"$out")"
printf -- '---\nname: iota\ndescription: Drifted content for the rollback honesty test case here.\n---\n\n# iota drifted\n' > "$SKILLS/iota/SKILL.md"
printf -- '---\nname: kappa\ndescription: Drifted content for the rollback honesty test case here.\n---\n\n# kappa drifted\n' > "$SKILLS/kappa/SKILL.md"
rm -rf "$STATE/skill-autosave-curator-backups/$partial_backup/skills/kappa"
out="$(tool rollback --id "$partial_backup")"; rc=$?
ok "rollback with a missing member fails closed" '[ "$rc" -eq 2 ] && jq -e ".code == \"backup_member_missing\"" >/dev/null <<<"$out"'
tail_row="$(grep '"curator-rollback"' "$STATE/skill-autosave-ownership.jsonl" | tail -1)"
ok "partial rollback records conflict with the applied count" 'jq -e ".outcome == \"conflict\" and .applied == 1" >/dev/null <<<"$tail_row"'
ok "the applied skill kept its restored content" 'grep -q "curator lifecycle tests" "$SKILLS/iota/SKILL.md"'

# 11c. dangling curator-rollback prepared row recovers as conflict
printf '{"schema_version":1,"event":"curator-rollback","transaction_id":"cafecafecafecafecafecafecafecafe","ts":"2099-01-01T00:00:00Z","outcome":"prepared","provider":"claude","backup_id":"2099-01-01T00-00-00Z","safety_backup_id":"2099-01-01T00-00-01Z","planned":1}\n' >> "$STATE/skill-autosave-ownership.jsonl"
out="$(tool run)"
ok "dangling rollback recovers as conflict" 'jq -e "[.recoveries[] | select(.transaction_id == \"cafecafecafecafecafecafecafecafe\" and .outcome == \"conflict\")] | length == 1" >/dev/null <<<"$out"'
out="$(tool run)"
ok "rollback recovery is idempotent" 'jq -e "[(.recoveries // [])[] | select(.transaction_id == \"cafecafecafecafecafecafecafecafe\")] | length == 0" >/dev/null <<<"$out"'

# 11d. dangling curator-restore prepared row recovers from FS state
out="$(tool list-archived)"
arch_name="$(jq -r '.archived[0].archive_name' <<<"$out")"
arch_skill="$(jq -r '.archived[0].name' <<<"$out")"
printf '{"schema_version":1,"event":"curator-restore","transaction_id":"beefbeefbeefbeefbeefbeefbeefbeef","ts":"2099-01-02T00:00:00Z","outcome":"prepared","provider":"claude","name":"%s","target_id":"00","archive_name":"%s","restored_at":"2099-01-02T00:00:00Z"}\n' "$arch_skill" "$arch_name" >> "$STATE/skill-autosave-ownership.jsonl"
out="$(tool run)"
ok "dangling restore with skill still archived recovers as aborted" 'jq -e "[.recoveries[] | select(.transaction_id == \"beefbeefbeefbeefbeefbeefbeefbeef\" and .outcome == \"aborted\")] | length == 1" >/dev/null <<<"$out"'
out="$(tool status "$arch_skill")"
ok "record stays archived after aborted restore recovery" 'jq -e ".skills[0].telemetry.state == \"archived\"" >/dev/null <<<"$out"'

# 11e. bump degrades instead of blocking behind a held mutation lock
python3 - "$STATE" <<'PY' &
import fcntl, os, sys, time
fd = os.open(os.path.join(sys.argv[1], ".skill-autosave-ownership.lock"), os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
time.sleep(8)
PY
lock_pid=$!
sleep 1
start="$(date +%s)"
out="$(tool bump --event use --name iota)"; rc=$?
elapsed=$(( $(date +%s) - start ))
ok "bump behind a held lock returns fast and degraded" '[ "$rc" -eq 0 ] && [ "$elapsed" -lt 5 ] && jq -e ".recorded == false and .degraded == true" >/dev/null <<<"$out"'
wait "$lock_pid" 2>/dev/null
out="$(tool bump --event use --name iota)"
ok "bump records again once the lock is free" 'jq -e ".recorded == true" >/dev/null <<<"$out"'

# 11f. group/world-readable usage store fails mutations closed
chmod 644 "$STATE/skill-autosave-usage.json"
out="$(tool run)"; rc=$?
ok "0644 usage store fails the run closed" '[ "$rc" -eq 2 ] && jq -e ".code == \"unsafe_metadata\"" >/dev/null <<<"$out"'
chmod 600 "$STATE/skill-autosave-usage.json"
out="$(tool run)"
ok "restored 0600 unblocks the run" 'jq -e ".ok == true" >/dev/null <<<"$out"'

# 11g. bump between runs survives (load-inside-lock ordering)
tool bump --event use --name iota >/dev/null
tool run >/dev/null
out="$(tool status iota)"
ok "inter-run bump is never overwritten by a run" 'jq -e ".skills[0].telemetry.use_count == 2" >/dev/null <<<"$out"'

# --- 12. read batching: one mutating run reads the ledger exactly once ---------
# Recovery, patch sync and every first-sight seed must share a single ledger
# read; batch-one's marker loses created_at so its seed can only come from the
# shared create/adopt index, not from a hidden per-skill re-read.
RB_TMP="$(mktemp -d)"; RB_STATE="$RB_TMP/state"; RB_SKILLS="$RB_TMP/skills"
mkdir -m 700 "$RB_STATE" "$RB_SKILLS"
for name in batch-one batch-two; do
  mkdir -m 700 "$RB_SKILLS/$name"
  printf -- '---\nname: %s\ndescription: A sufficiently detailed recurring workflow for read batching tests.\n---\n\n# %s\n' "$name" "$name" > "$RB_SKILLS/$name/SKILL.md"
  chmod 600 "$RB_SKILLS/$name/SKILL.md"
  python3 "$OWN" --provider claude --skills-dir "$RB_SKILLS" --state-dir "$RB_STATE" mark-created "$name" >/dev/null
done
jq -c 'del(.created_at)' "$RB_SKILLS/batch-one/.autosave-meta.json" > "$RB_TMP/marker"
mv "$RB_TMP/marker" "$RB_SKILLS/batch-one/.autosave-meta.json"
chmod 600 "$RB_SKILLS/batch-one/.autosave-meta.json"
TOOL_PATH="$TOOL" SKILLS_PATH="$RB_SKILLS" STATE_PATH="$RB_STATE" python3 - <<'PY'
import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("curator_read_batching_test", os.environ["TOOL_PATH"])
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
real_read_ledger = module.ownership._read_ledger
ledger_reads = 0


def counted_read_ledger(context: object) -> list[dict[str, object]]:
    global ledger_reads
    ledger_reads += 1
    return real_read_ledger(context)


module.ownership._read_ledger = counted_read_ledger
context = module.ownership.Context(
    provider="claude",
    skills_dir=Path(os.environ["SKILLS_PATH"]),
    state_dir=Path(os.environ["STATE_PATH"]),
    uid=os.geteuid(),
)
report = module._command_run(context, dry_run=False, auto=False)
assert report["counts"]["seeded"] == 2, report
assert ledger_reads == 1, ledger_reads
usage = module._load_usage(context, strict=False)
record = usage["records"]["claude:batch-one"]
created = [
    row["ts"]
    for row in real_read_ledger(context)
    if row.get("event") == "create"
    and row.get("outcome") == "changed"
    and row.get("name") == "batch-one"
]
assert record["created_at"] == module._ts(module._parse_ts(created[-1])), record
PY
rc=$?
ok "mutating run reads the ownership ledger exactly once" '[ "$rc" = 0 ]'
rm -rf "$RB_TMP"

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
