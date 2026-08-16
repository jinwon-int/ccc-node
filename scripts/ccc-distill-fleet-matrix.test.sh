#!/usr/bin/env bash
# Tests for ccc-distill-fleet-matrix.sh — the #82 fleet closure matrix builder.
# Read-only: fixtures are text heredocs, the SUT does no network and no writes.
#
# Until now this script had ZERO coverage — the only two occurrences of its name
# anywhere in the repo were inside its own header comment. Both defects that
# #877 fixed (commit 81a7c20) were therefore unprotected, and both are the kind
# that fail *silently*: one misreports every converged node as `no_evidence`,
# the other pins `summary.verified` at 0 no matter how healthy the fleet is.
# A matrix that under-reports is worse than one that errors, because the
# finalizer reads it as "not converged yet" and keeps waiting.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUT="$ROOT/scripts/ccc-distill-fleet-matrix.sh"
pass=0; fail=0
BASE_TMP="${TMPDIR:-/tmp}"
mkdir -p "$BASE_TMP"
TMP="$(mktemp -d "$BASE_TMP/ccc-distill-fleet-matrix-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

TARGET=036c230947e2d2f92af2188d0707e5b0b0c5b268
TARGET_SHORT=036c230

# --- fixtures ---------------------------------------------------------------
# One probe file covering every branch the classifier has:
#   converged  — reporting checker, commit == target        -> verified
#   behindnode — reporting checker, commit != target        -> blocked/behind
#   nochecker  — NO_CHECKER_FOUND                           -> blocked
#   downnode   — ssh failure marker                         -> blocked/unreachable
#   ghostnode  — listed but absent from the probe entirely  -> blocked/no_evidence
#   hostless   — no HOST= line (the empty-field elision case \x1f exists for)
cat > "$TMP/probe.txt" <<EOF
===== converged =====
HOST=converged.example
CANDIDATE=/root/ccc-node
https://github.com/jinwon-int/ccc-node.git
$TARGET_SHORT

===== behindnode =====
HOST=behind.example
CANDIDATE=/root/ccc-node
https://github.com/jinwon-int/ccc-node.git
deadbee

===== nochecker =====
HOST=nochecker.example
CANDIDATE=/root/ccc-node
NO_CHECKER_FOUND

===== downnode =====
HOST=down.example
ssh: connect to host down.example port 22: Connection timed out

===== hostless =====
CANDIDATE=/root/ccc-node
https://github.com/jinwon-int/ccc-node.git
$TARGET_SHORT
EOF

NODES=converged,behindnode,nochecker,downnode,ghostnode,hostless
out="$(bash "$SUT" --path-probe "$TMP/probe.txt" --target-commit "$TARGET" --node-list "$NODES")"; rc=$?

ok "emits a single JSON object for issue 82" \
  '[ "$rc" = 0 ] && jq -e ".issue == 82 and (.nodes | length) == 6" <<<"$out" >/dev/null'
ok "echoes the target commit and its source files" \
  'jq -e --arg t "$TARGET" ".target_commit == \$t and .sources.path_probe_file != null and .sources.status_file == null" <<<"$out" >/dev/null'

# --- defect 1 (#877): \x1f fields were not split ----------------------------
# `for field in $extra` relied on word splitting, but the record separator is
# \x1f, which is not in the default IFS. $extra stayed ONE token, so
# `CANDIDATE=*` matched the whole blob and swallowed the URL, the commit and
# the status with it — every other field arrived empty and the node was
# misreported as no_evidence. These assert the four fields land separately.
ok "candidate, url and commit are parsed as distinct fields" \
  'jq -e --arg t "$TARGET_SHORT" ".nodes[] | select(.name == \"converged\")
     | .candidate == \"/root/ccc-node\"
       and .git_url == \"https://github.com/jinwon-int/ccc-node.git\"
       and .probe_commit == \$t" <<<"$out" >/dev/null'
ok "candidate does not swallow the following fields" \
  '! jq -e ".nodes[] | select(.name == \"converged\") | .candidate | test(\"https|036c230\")" <<<"$out" >/dev/null'
ok "a node present in the evidence is not flattened to no_evidence" \
  'jq -e ".nodes[] | select(.name == \"converged\") | .status == \"REPORTED\" and .checker_available == true" <<<"$out" >/dev/null'
ok "a node with a parsed status is not misreported as no_evidence" \
  'jq -e ".nodes[] | select(.name == \"nochecker\") | .status == \"NO_CHECKER_FOUND\" and .candidate == \"/root/ccc-node\"" <<<"$out" >/dev/null'
# The whole reason the separator is \x1f rather than whitespace: a block with
# no HOST= line must not shift every later field left by one.
ok "a block with no HOST= line still parses its remaining fields" \
  'jq -e --arg t "$TARGET_SHORT" ".nodes[] | select(.name == \"hostless\")
     | .host == null and .candidate == \"/root/ccc-node\" and .probe_commit == \$t" <<<"$out" >/dev/null'

# --- defect 2 (#877): `verified` was structurally unreachable ---------------
# state_for() only ever yields blocked or pending; `verified` is assigned
# solely by the resolve() layer. Without it summary.verified was pinned at 0
# even for a fully converged fleet, so the matrix could never report success.
ok "a reporting node at the target commit is verified" \
  'jq -e ".nodes[] | select(.name == \"converged\")
     | .verification == \"verified\" and .mode == \"reported\"
       and .blocker_reason == null and .checker_available == true
       and .behind_target == false and .commit_compare == \"equal\"" <<<"$out" >/dev/null'
# The assertion that would have caught the structural pin at 0.
ok "summary.verified counts converged nodes rather than staying at 0" \
  'jq -e ".summary.verified == 2 and .summary.total == 6" <<<"$out" >/dev/null'
ok "a reporting node behind the target is blocked, not verified" \
  'jq -e ".nodes[] | select(.name == \"behindnode\")
     | .verification == \"blocked\" and .mode == \"reported\"
       and .blocker_reason == \"probe_commit_behind_target\"
       and .behind_target == true and .commit_compare == \"behind\"" <<<"$out" >/dev/null'

# resolve() must not over-reach: a node whose checker never reported keeps its
# own blocker_reason. Three distinct reasons, so a blanket resolve is caught.
ok "missing checker keeps its own blocker reason" \
  'jq -e ".nodes[] | select(.name == \"nochecker\")
     | .verification == \"blocked\" and .checker_available == false
       and .mode == \"missing\"
       and .blocker_reason == \"checker_not_found_at_candidate_path\"" <<<"$out" >/dev/null'
ok "an unreachable node keeps its own blocker reason" \
  'jq -e ".nodes[] | select(.name == \"downnode\")
     | .status == \"UNREACHABLE\" and .mode == \"unreachable\"
       and .blocker_reason == \"node_unreachable_over_ssh\"" <<<"$out" >/dev/null'
ok "a node absent from the probe is reported as a gap, not silently dropped" \
  'jq -e ".nodes[] | select(.name == \"ghostnode\")
     | .status == \"no_evidence\" and .mode == \"unknown\"
       and .blocker_reason == \"no_evidence_in_probe\"
       and .host == null and .probe_commit == null" <<<"$out" >/dev/null'

ok "summary tallies agree with the per-node records" \
  'jq -e ".summary.blocked == 4 and .summary.checker_available == 3
          and .summary.behind_target == 1 and .summary.unreachable == 1
          and .summary.no_checker_found == 1
          and (.summary.verified + .summary.blocked + .summary.pending) == .summary.total" <<<"$out" >/dev/null'

# A reporting node with no parseable commit cannot be decided either way. It
# must stay blocked (and therefore actionable) rather than land in `pending`,
# which generates no recommended subissue.
cat > "$TMP/probe-nocommit.txt" <<'EOF'
===== quiet =====
HOST=quiet.example
CANDIDATE=/root/ccc-node
EOF
nc_out="$(bash "$SUT" --path-probe "$TMP/probe-nocommit.txt" --target-commit "$TARGET" --node-list quiet)"
ok "a reporting node without a commit is blocked, not silently pending" \
  'jq -e ".nodes[0].checker_available == true and .nodes[0].verification == \"blocked\"
          and .nodes[0].blocker_reason == \"probe_commit_missing\"
          and .summary.pending == 0" <<<"$nc_out" >/dev/null'
ok "every blocked node gets a recommended subissue" \
  'jq -e "(.recommended_subissues | length) == (.summary.blocked)" <<<"$nc_out" >/dev/null'
ok "node order follows --node-list" \
  '[ "$(jq -r "[.nodes[].name] | join(\",\")" <<<"$out")" = "'"$NODES"'" ]'

# --- full-length SHA in the probe -------------------------------------------
# detect_short_c accepts a 40-char sha and compares its first 7. A node that
# probed with the long form must compare equal, not fall through to null.
cat > "$TMP/probe-long.txt" <<EOF
===== converged =====
HOST=converged.example
CANDIDATE=/root/ccc-node
$TARGET
EOF
long_out="$(bash "$SUT" --path-probe "$TMP/probe-long.txt" --target-commit "$TARGET" --node-list converged)"
ok "a 40-char probe commit compares equal to the target" \
  'jq -e ".nodes[0].verification == \"verified\" and .nodes[0].commit_compare == \"equal\"" <<<"$long_out" >/dev/null'

# --- status file merges with the probe --------------------------------------
# ingest() runs twice and the two files share one set of maps; a status-only
# marker must reach a node whose other fields came from the probe.
cat > "$TMP/status.txt" <<'EOF'
===== converged =====
HOST=converged.example
NO_CHECKER_FOUND
EOF
merged="$(bash "$SUT" --status "$TMP/status.txt" --path-probe "$TMP/probe.txt" --target-commit "$TARGET" --node-list converged)"
ok "a status-file marker overrides an otherwise converged probe" \
  'jq -e ".nodes[0].status == \"NO_CHECKER_FOUND\" and .nodes[0].verification == \"blocked\"" <<<"$merged" >/dev/null'
ok "both source files are recorded when given" \
  'jq -e ".sources.status_file != null and .sources.path_probe_file != null" <<<"$merged" >/dev/null'

# --- degenerate inputs ------------------------------------------------------
: > "$TMP/empty.txt"
empty_out="$(bash "$SUT" --path-probe "$TMP/empty.txt" --target-commit "$TARGET" --node-list converged)"; erc=$?
ok "an empty evidence file yields a no_evidence node, not a crash" \
  '[ "$erc" = 0 ] && jq -e ".summary.total == 1 and .summary.verified == 0
      and .nodes[0].blocker_reason == \"no_evidence_in_probe\"" <<<"$empty_out" >/dev/null'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = "0" ]
