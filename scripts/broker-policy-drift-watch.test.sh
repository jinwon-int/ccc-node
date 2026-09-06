#!/usr/bin/env bash
# Tests for the broker-policy drift watch.
#
# The watch exists because a live broker ran `enforce` for two months while the
# operator-committed document said `warn` and nothing detected it
# (a2a-nexus#2064). These tests pin the three properties that make it useful:
#
#   1. it compares against `origin/main`, NOT the working tree — a developer
#      checkout mid-review must not produce false drift;
#   2. it never repairs — §2.4 reserves policy changes for operator commits,
#      so a self-correcting watcher would be an agent editing policy;
#   3. it fails closed (exit 2) when it cannot determine the answer, rather
#      than reporting "in sync";
#   4. it takes the detector code from the canonical ref too, not just the
#      canonical document — on T1 the checkout's detector predated the `drift`
#      subcommand, so a working-tree detector meant the watch could not run
#      (a2a-nexus#2072);
#   5. it refuses to guess A2A_NEXUS_REPO, because the old default pointed at a
#      stale decoy clone on T1 while the broker ran from a different tree.
#
# A real git repo is built under $TMP with a committed canonical document and a
# separate live file, plus a stub `node` so the suite does not depend on the
# a2a-nexus checkout being present or on any particular node state.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCH="$ROOT/scripts/broker-policy-drift-watch.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1

ok()   { pass=$((pass+1)); echo "  ok $1"; }
bad()  { fail=$((fail+1)); echo "  FAIL $1"; }
check_rc() { # name expected actual
  if [ "$2" = "$3" ]; then ok "$1 (exit $3)"; else bad "$1 (expected exit $2, got $3)"; fi
}

policy_doc() { # mode
  printf '{\n  "schemaVersion": "a2a.broker.policy.v1",\n  "mode": "%s",\n  "defaultAction": "allow",\n  "rules": []\n}\n' "$1"
}

# ---- fake a2a-nexus repo: canonical lives in a commit on origin/main --------
REPO="$TMP/nexus"
ORIGIN="$TMP/nexus-origin.git"
mkdir -p "$REPO/docs/ops" "$REPO/scripts"
git init --quiet --initial-branch=main "$REPO"
git -C "$REPO" config user.email t@example.invalid
git -C "$REPO" config user.name test
policy_doc enforce > "$REPO/docs/ops/broker-policy.json"

# Stub `node`: the watch shells out to check-broker-policy.mjs, which is the
# unit under test elsewhere (a2a-nexus). Here we only need its contract:
# compare two JSON docs, exit 0 same / 1 different.
#
# The file itself still has to exist in the commit and mention `drift`: the
# watch extracts it from the canonical ref and preflights the subcommand, so a
# content-free placeholder would (correctly) be rejected as unable-to-determine.
mkdir -p "$REPO/scripts/lib"
cat > "$REPO/scripts/check-broker-policy.mjs" <<'EOF'
// stand-in for the real detector; the stub `node` on PATH is what executes.
// SUBCOMMANDS = validate, drift, sync
import './lib/broker-policy-deployment.mjs';
EOF
cat > "$REPO/scripts/lib/broker-policy-deployment.mjs" <<'EOF'
// relative import target — present so the extraction test is meaningful.
EOF
mkdir -p "$TMP/bin"
cat > "$TMP/bin/node" <<'EOF'
#!/usr/bin/env bash
# Minimal stand-in for `node scripts/check-broker-policy.mjs drift --live L --canonical C`
#
# The stub must actually inspect the script path it was handed, or every test
# below passes no matter which detector the watch chose and the #2072 fix is
# untested. Exit 3 (an "impossible" status) when the handed detector is absent
# or cannot do drift, so choosing the wrong one is loudly distinguishable.
script="$1"; shift
[ -r "$script" ] || { echo "stub: no such detector: $script" >&2; exit 3; }
grep -q drift "$script" || { echo "stub: detector cannot drift: $script" >&2; exit 3; }
live=""; canon=""
while [ $# -gt 0 ]; do
  case "$1" in
    --live) live="$2"; shift 2 ;;
    --canonical) canon="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -r "$live" ] || { echo "stub: cannot read live" >&2; exit 2; }
[ -r "$canon" ] || { echo "stub: cannot read canonical" >&2; exit 2; }
if diff -q "$live" "$canon" >/dev/null 2>&1; then echo "stub: in sync"; exit 0; fi
echo "stub: drift"; exit 1
EOF
chmod +x "$TMP/bin/node"
PATH="$TMP/bin:$PATH"; export PATH

git -C "$REPO" add -A
git -C "$REPO" commit --quiet -m "canonical enforce"
git clone --quiet --bare "$REPO" "$ORIGIN"
git -C "$REPO" remote add origin "$ORIGIN"
git -C "$REPO" fetch --quiet origin

LIVE="$TMP/live-policy.json"

run_watch() { A2A_NEXUS_REPO="$REPO" BROKER_POLICY_LIVE="$LIVE" bash "$WATCH" >"$TMP/out" 2>&1; }

echo "== broker-policy drift watch =="

# 1. in sync -> 0
policy_doc enforce > "$LIVE"
run_watch; check_rc "in-sync live document passes" 0 $?

# 2. drift -> 1
policy_doc warn > "$LIVE"
run_watch; check_rc "drifted live document fails" 1 $?

# 3. the failure must hand the operator both directions, and must NOT repair
if grep -q "repo is right" "$TMP/out" && grep -q "live is right" "$TMP/out"; then
  ok "drift output offers both resolutions"
else
  bad "drift output offers both resolutions"
fi
if [ "$(cat "$LIVE")" = "$(policy_doc warn)" ]; then
  ok "watch never modifies the live document"
else
  bad "watch never modifies the live document"
fi

# 4. the restart trap this watch was written after: compose does not restart on
#    a policy-file-only change, so the guidance must say to verify it did.
if grep -q "Up N seconds" "$TMP/out"; then
  ok "drift output warns that a restart must be verified"
else
  bad "drift output warns that a restart must be verified"
fi

# 5. missing live file -> 2 (fail closed, never "in sync")
rm -f "$LIVE"
run_watch; check_rc "missing live document fails closed" 2 $?
policy_doc enforce > "$LIVE"

# 6. missing repo -> 2
A2A_NEXUS_REPO="$TMP/nope" BROKER_POLICY_LIVE="$LIVE" bash "$WATCH" >/dev/null 2>&1
check_rc "missing repo fails closed" 2 $?

# 7. THE point of the design: a dirty/branched working tree must not matter.
#    Put a conflicting document in the working tree and leave origin/main alone.
policy_doc warn > "$REPO/docs/ops/broker-policy.json"
git -C "$REPO" checkout --quiet -b some-feature-branch
policy_doc enforce > "$LIVE"
run_watch
check_rc "working-tree edits do not cause false drift" 0 $?
git -C "$REPO" checkout --quiet main
git -C "$REPO" checkout --quiet -- docs/ops/broker-policy.json

# ---- a2a-nexus#2072 regressions -------------------------------------------

# 8. A2A_NEXUS_REPO must not be guessed. The old default silently watched a
#    stale decoy clone on T1 while the broker ran from another tree.
policy_doc enforce > "$LIVE"
( unset A2A_NEXUS_REPO; BROKER_POLICY_LIVE="$LIVE" bash "$WATCH" >"$TMP/out" 2>&1 )
check_rc "unset A2A_NEXUS_REPO fails closed" 2 $?
if grep -q "com.docker.compose.project.working_dir" "$TMP/out"; then
  ok "unset-repo error tells the operator how to find the real tree"
else
  bad "unset-repo error tells the operator how to find the real tree"
fi

# 9. THE #2072 fix: the detector is taken from the canonical ref, not the tree.
#    Delete it from the working tree entirely — the watch must still work.
rm -f "$REPO/scripts/check-broker-policy.mjs"
policy_doc enforce > "$LIVE"
run_watch; check_rc "detector missing from working tree does not matter" 0 $?
git -C "$REPO" checkout --quiet -- scripts/check-broker-policy.mjs

# 10. ...and a working-tree detector that predates `drift` (the literal T1
#     state) must not affect the verdict either.
cat > "$REPO/scripts/check-broker-policy.mjs" <<'EOF'
// old detector: only knows validate. No drift subcommand here.
EOF
run_watch; check_rc "stale working-tree detector does not matter" 0 $?
git -C "$REPO" checkout --quiet -- scripts/check-broker-policy.mjs

# 11. If the CANONICAL detector cannot do drift, that is cannot-determine (2),
#     never a drift verdict (1) — a broken deployment must not cry wolf.
git -C "$REPO" checkout --quiet -b no-drift-detector
cat > "$REPO/scripts/check-broker-policy.mjs" <<'EOF'
// canonical-but-ancient detector: validate only.
EOF
git -C "$REPO" commit --quiet -am "detector without the subcommand"
BROKER_POLICY_CANONICAL_REF=no-drift-detector A2A_NEXUS_REPO="$REPO" \
  BROKER_POLICY_LIVE="$LIVE" bash "$WATCH" >"$TMP/out" 2>&1
check_rc "canonical detector lacking 'drift' is cannot-determine" 2 $?
# The exit code alone would come out as 2 anyway: the real detector falls
# through to `validate`, treats the word "drift" as a filename, and fails to
# read it. That accidental 2 is right for the wrong reason and reports
# "cannot read policy document 'drift'", which sends the operator hunting for
# a missing file. The preflight exists to name the actual problem, so assert
# the message, not just the status.
if grep -q "does not support the 'drift' subcommand" "$TMP/out"; then
  ok "cannot-drift detector is diagnosed by name, not as a missing file"
else
  bad "cannot-drift detector is diagnosed by name, not as a missing file"
fi
git -C "$REPO" checkout --quiet main

# 12. A detector exit status outside 0/1 is normalized to 2, so a broken
#     deployment can never be read as drift.
cat > "$TMP/bin/node" <<'EOF'
#!/usr/bin/env bash
echo "stub: exploded" >&2; exit 7
EOF
chmod +x "$TMP/bin/node"
run_watch; check_rc "unexpected detector exit is normalized to cannot-determine" 2 $?

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
