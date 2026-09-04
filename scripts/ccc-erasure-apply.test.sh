#!/usr/bin/env bash
# Tests for ccc-erasure-apply.py — the #873 step-4 approval-gated apply
# boundary. Hermetic fixture tree with a PURPOSE-BUILT inventory so every
# fail-closed condition is exercisable deterministically:
#   digest binding (file digest, live re-plan drift), blockers,
#   owner-only verification (bits, symlink, ancestors), rollback-first
#   backup, per-path multi-file targets, and the plan-only default.
# The armed path deletes ONLY fixture files; no real store is ever touched.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APPLY="$ROOT/scripts/ccc-erasure-apply.py"
PLANNER="$ROOT/scripts/ccc-erasure-planner.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
umask 077

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

FIX="$TMP/fix"
HOME_DIR="$FIX/home"
mkdir -p "$HOME_DIR" "$FIX/state"   # $FIX/backup must NOT pre-exist: plan-only
                                       # must never create it (asserted below)
export T_HOME="$HOME_DIR"
export HOME="$HOME_DIR"
export CCC_ERASURE_BACKUP_DIR="$FIX/backup"
unset ERASURE_APPLY

INV="$FIX/inv.json"

write_inv() { # purpose-built inventory: every fixture file classified
  cat > "$INV" <<'EOF'
{
  "schema": "ccc.memory-artifact-inventory.v1",
  "note": "apply test fixture inventory",
  "artifacts": [
    {"id": "t.deletable", "path_class": "node-local derived",
     "contains": ["log"], "role": "derived", "owner": "node",
     "resolve": {"candidates": [{"env": "T_HOME", "join": true, "path": "delete-me.log"}]},
     "retention": "regenerable", "requests": {"node-decommission": "delete"},
     "derived_copies": [], "verification": "presence"},
    {"id": "t.append_only", "path_class": "node-local audit append-only",
     "contains": ["events"], "role": "audit", "owner": "node",
     "resolve": {"candidates": [{"env": "T_HOME", "join": true, "path": "append-only.jsonl"}]},
     "retention": "append-only", "requests": {"node-decommission": "pseudonymize"},
     "derived_copies": [], "verification": "line count"},
    {"id": "t.retained", "path_class": "node-local audit",
     "contains": ["evidence"], "role": "audit", "owner": "node",
     "resolve": {"candidates": [{"env": "T_HOME", "join": true, "path": "retained.md"}]},
     "retention": "retain", "requests": {"node-decommission": "retain"},
     "derived_copies": [], "verification": "presence"},
    {"id": "t.handoff", "path_class": "node-local outbox external-adjacent",
     "contains": ["candidates"], "role": "outbox", "owner": "node",
     "resolve": {"candidates": [{"env": "T_HOME", "join": true, "path": "handoff.txt"}]},
     "retention": "until review",
     "requests": {"node-decommission": "external-handoff"},
     "handoff_note": "owner workflow", "derived_copies": [],
     "verification": "queue depth"},
    {"id": "t.dir_target", "path_class": "node-local derived",
     "contains": ["cache"], "role": "derived", "owner": "node",
     "resolve": {"candidates": [{"env": "T_HOME", "join": true, "path": "cache-dir", "kind": "dir"}]},
     "retention": "regenerable", "requests": {"node-decommission": "delete"},
     "derived_copies": [], "verification": "presence"},
    {"id": "t.logs", "path_class": "node-local derived",
     "contains": ["log"], "role": "derived", "owner": "node",
     "resolve": {"candidates": [{"env": "T_HOME", "join": true, "path": "a.log"}]},
     "extra_paths": [{"env": "T_HOME", "join": true, "path": "b.log"}],
     "retention": "regenerable", "requests": {"node-decommission": "delete"},
     "derived_copies": [], "verification": "presence"}
  ]
}
EOF
}

seed_fixture() { # the full classified file set — plan is blocker-free
  rm -rf "$HOME_DIR"          # sections must not inherit each other's state
  mkdir -p "$HOME_DIR/cache-dir"
  printf 'payload\n' > "$HOME_DIR/delete-me.log"
  printf 'event\n'   > "$HOME_DIR/append-only.jsonl"
  printf 'evidence\n' > "$HOME_DIR/retained.md"
  printf 'candidate\n' > "$HOME_DIR/handoff.txt"
  printf 'log-a\n' > "$HOME_DIR/a.log"
  printf 'log-b\n' > "$HOME_DIR/b.log"
  printf 'cache\n' > "$HOME_DIR/cache-dir/item.bin"
}

digest_of() { # canonical digest exactly as the apply script computes it
  python3 - "$1" <<'PY'
import hashlib
import json
import sys
with open(sys.argv[1]) as fh:
    plan = json.load(fh)
canonical = json.dumps(plan, sort_keys=True, ensure_ascii=True, indent=1)
print(hashlib.sha256(canonical.encode()).hexdigest())
PY
}

make_plan() { # regenerate plan.json + echo the digest
  python3 "$PLANNER" --inventory "$INV" node-decommission --json > "$FIX/plan.json"
  digest_of "$FIX/plan.json"
}

tree_sum() { find "$FIX/home" -type f -exec md5sum {} + | sort | md5sum; }

# --- 1) plan-only default: full report, zero mutation, zero writes -----------
seed_fixture
write_inv
DIGEST="$(make_plan)"
BEFORE="$(tree_sum)"
out="$(ERASURE_APPLY= python3 "$APPLY" node-decommission --inventory "$INV" \
        --plan "$FIX/plan.json" --plan-digest "$DIGEST" --json)"; rc=$?
ok "plan-only exits 0" '[ "$rc" = 0 ]'
ok "plan-only names the mode and executable count" \
  'grep -q "plan-only" <<<"$out" && grep -qF "\"executable\"" <<<"$out"'
ok "plan-only writes nothing anywhere" '[ "$BEFORE" = "$(tree_sum)" ] && [ ! -d "$FIX/backup" ]'
ok "plan-only reports skipped non-delete actions" \
  'grep -qF "action-pseudonymize-not-executable" <<<"$out" && grep -qF "action-retain-not-executable" <<<"$out"'

# --- 2) usage ----------------------------------------------------------------
python3 "$APPLY" node-decommission --inventory "$INV" --plan "$FIX/plan.json" \
  >/dev/null 2>&1; rc=$?
ok "missing --plan-digest exits 2 (argparse)" '[ "$rc" = 2 ]'
python3 "$APPLY" bogus-request --inventory "$INV" --plan "$FIX/plan.json" \
  --plan-digest "$DIGEST" >/dev/null 2>&1; rc=$?
ok "unknown request exits 2" '[ "$rc" = 2 ]'
python3 "$APPLY" node-decommission --inventory "$INV" --plan "$FIX/plan.json" \
  --plan-digest deadbeef >/dev/null 2>&1; rc=$?
ok "non-hex digest exits 2" '[ "$rc" = 2 ]'

# --- 3) digest binding: wrong digest, live drift, blockers --------------------
out="$(ERASURE_APPLY=1 python3 "$APPLY" node-decommission --inventory "$INV" \
        --plan "$FIX/plan.json" \
        --plan-digest 0000000000000000000000000000000000000000000000000000000000000000)"; rc=$?
ok "wrong digest is blocked" '[ "$rc" = 4 ] && grep -q "does not match --plan-digest" <<<"$out"'
ok "blocked run mutates nothing" '[ "$BEFORE" = "$(tree_sum)" ]'

printf 'stray\n' > "$HOME_DIR/stray-orphan.bin"   # world drifts after planning
SUM_WITH_STRAY="$(tree_sum)"                       # drift-blocked runs change NOTHING else
out="$(ERASURE_APPLY=1 python3 "$APPLY" node-decommission --inventory "$INV" \
        --plan "$FIX/plan.json" --plan-digest "$DIGEST")"; rc=$?
ok "live drift (new stray file) kills the stale plan" \
  '[ "$rc" = 4 ] && grep -q "live re-plan differs" <<<"$out"'
ok "drift-blocked run mutates nothing" '[ "$SUM_WITH_STRAY" = "$(tree_sum)" ]'
rm "$HOME_DIR/stray-orphan.bin"
DIGEST="$(make_plan)"   # re-plan over the cleaned tree for later cases

# --- 4) armed happy path: backup ALL, delete only delete-files, verify --------
out="$(ERASURE_APPLY=1 python3 "$APPLY" node-decommission --inventory "$INV" \
        --plan "$FIX/plan.json" --plan-digest "$DIGEST" --json)"; rc=$?
ok "armed run exits 0" '[ "$rc" = 0 ]'
ok "delete targets are gone" \
  '[ ! -f "$HOME_DIR/delete-me.log" ] && [ ! -f "$HOME_DIR/a.log" ] && [ ! -f "$HOME_DIR/b.log" ]'
ok "non-delete actions are untouched" \
  '[ -f "$HOME_DIR/append-only.jsonl" ] && [ -f "$HOME_DIR/retained.md" ] && [ -f "$HOME_DIR/handoff.txt" ]'
ok "dir target deferred to a later slice" \
  '[ -d "$HOME_DIR/cache-dir" ] && grep -qF "dir-target-needs-later-slice" <<<"$out"'
BACKUP="$(grep -oE '"backup_dir": "[^"]+"' <<<"$out" | head -1 | cut -d'"' -f4)"
ok "manifest recorded in owner-only backup dir" \
  '[ -n "$BACKUP" ] && [ -f "$BACKUP/manifest.json" ] && [ "$(stat -c %a "$BACKUP")" = 700 ]'
ok "pre-image backup verifies against recorded sha256" \
  'grep -qF "\"verified\": true" <<<"$out"'
ok "deleted file restorable from backup (non-empty pre-image)" \
  '[ -n "$(find "$BACKUP" -name "t_deletable-*" -size +1c 2>/dev/null)" ]'

# --- 5) owner-only verification: permission bits and symlink ------------------
seed_fixture
rm -rf "$FIX/backup"
DIGEST="$(make_plan)"
chmod 640 "$HOME_DIR/delete-me.log"
out="$(ERASURE_APPLY=1 python3 "$APPLY" node-decommission --inventory "$INV" \
        --plan "$FIX/plan.json" --plan-digest "$DIGEST")"; rc=$?
ok "group/other bits on target abort before any backup" \
  '[ "$rc" = 4 ] && grep -q "group-other-bits-on-target" <<<"$out" && [ ! -d "$FIX/backup" ]'
seed_fixture
rm -rf "$FIX/backup"
rm -f "$HOME_DIR/a.log"
ln -s "$HOME_DIR/retained.md" "$HOME_DIR/a.log"
DIGEST="$(make_plan)"
out="$(ERASURE_APPLY=1 python3 "$APPLY" node-decommission --inventory "$INV" \
        --plan "$FIX/plan.json" --plan-digest "$DIGEST")"; rc=$?
ok "symlink target is a hard reject" \
  '[ "$rc" = 4 ] && grep -q "symlink-target" <<<"$out" && [ -f "$HOME_DIR/retained.md" ]'

# --- 6) backup failure aborts with nothing deleted ----------------------------
seed_fixture
rm -rf "$FIX/backup"
DIGEST="$(make_plan)"
: > "$FIX/not-a-dir"
out="$(CCC_ERASURE_BACKUP_DIR="$FIX/not-a-dir/sub" ERASURE_APPLY=1 \
        python3 "$APPLY" node-decommission --inventory "$INV" \
        --plan "$FIX/plan.json" --plan-digest "$DIGEST")"; rc=$?
ok "unusable backup dir blocks the run" \
  '[ "$rc" = 4 ] && grep -q "backup-dir-create-failed" <<<"$out"'
ok "blocked-by-backup run deletes nothing" '[ -f "$HOME_DIR/delete-me.log" ]'

# --- 7) audience-erasure requires its key -------------------------------------
python3 "$APPLY" audience-erasure --inventory "$INV" --plan "$FIX/plan.json" \
  --plan-digest "$DIGEST" >/dev/null 2>&1; rc=$?
ok "audience-erasure without --audience exits 2" '[ "$rc" = 2 ]'

echo "----------------------------------------"
echo "PASS=$pass FAIL=$fail"
python3 "$ROOT/scripts/ccc_erasure_regression_test.py" || fail=$((fail+1))
[ "$fail" = 0 ]
