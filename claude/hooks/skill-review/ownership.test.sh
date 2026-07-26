#!/usr/bin/env bash
# Hermetic ownership/provenance/read-before-write contract tests (#750).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL="$HERE/ownership.py"
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

make_skill() {
  local name="$1"
  mkdir -m 700 "$SKILLS/$name"
  printf -- '---\nname: %s\ndescription: A sufficiently detailed recurring workflow for ownership contract tests.\n---\n\n# %s\n\n## Steps\n1. Read.\n2. Verify.\n3. Record.\n' "$name" "$name" > "$SKILLS/$name/SKILL.md"
  chmod 600 "$SKILLS/$name/SKILL.md"
}

proposal_from_read() {
  local read_json="$1" output="$2"
  jq '{
    schema_version: 1,
    attempt_id: .receipt.attempt_id,
    receipt_id: .receipt.receipt_id,
    operation: .receipt.operation,
    provider: .receipt.provider,
    name: .receipt.name,
    target_id: .receipt.target_id,
    relative_target: .receipt.relative_target,
    expected_sha256: .receipt.expected_sha256,
    expected_provenance_revision: .receipt.expected_provenance_revision,
    expected_provenance_sha256: .receipt.expected_provenance_sha256
  }' <<<"$read_json" > "$output"
  chmod 600 "$output"
}

# User-owned is visible but autonomous read-only.
make_skill user-one
out="$(tool status user-one)"
ok "unmarked skill is user-owned" 'jq -e ".skills[0].classification == \"user-owned\" and (.skills[0].autonomous_write_allowed | not)" >/dev/null <<<"$out"'
out="$(tool list-unmanaged)"
ok "list-unmanaged includes protected user skill" 'jq -e ".skills | map(.name) | index(\"user-one\") != null" >/dev/null <<<"$out"'

# Dry-run performs all checks but writes no metadata or ledger.
before="$(find "$STATE" "$SKILLS/user-one" -printf '%P:%s:%T@\\n' | sort)"
out="$(tool adopt user-one --dry-run)"
after="$(find "$STATE" "$SKILLS/user-one" -printf '%P:%s:%T@\\n' | sort)"
ok "adopt dry-run reports transition" 'jq -e ".dry_run == true and .changed == false and .reason == \"would-adopt\"" >/dev/null <<<"$out"'
ok "adopt dry-run has no filesystem effect" '[ "$before" = "$after" ]'

# Explicit adopt produces owner-only v2 provenance and body-free ledger.
out="$(tool adopt user-one)"
ok "adopt makes skill autonomous-managed" 'jq -e ".skill.classification == \"autosave-managed\" and .skill.autonomous_write_allowed" >/dev/null <<<"$out"'
ok "adopt marker is v2 and non-rollbackable" 'jq -e ".schema_version == 2 and .created_by == \"operator-adopt\" and .rollback_eligible == false" "$SKILLS/user-one/.autosave-meta.json" >/dev/null'
ok "adopt marker is owner-only" '[ "$(stat -c %a "$SKILLS/user-one/.autosave-meta.json")" = 600 ]'
ok "ownership ledger is owner-only and body-free" '[ "$(stat -c %a "$STATE/skill-autosave-ownership.jsonl")" = 600 ] && jq -e "select(.event == \"adopt\") | has(\"content\") | not" "$STATE/skill-autosave-ownership.jsonl" >/dev/null'
ok "adopt audit has durable prepared and terminal phases" 'jq -s -e "[.[] | select(.event == \"adopt\" and .name == \"user-one\")] | group_by(.transaction_id) | any(map(.outcome) == [\"prepared\", \"changed\"])" "$STATE/skill-autosave-ownership.jsonl" >/dev/null'
tool rollback-check user-one >/dev/null 2>&1; rc=$?
ok "adopted user skill is never rollback eligible" '[ "$rc" != 0 ]'

# Pin is an overlay: it blocks autonomous mutation without erasing ownership.
before="$(find "$STATE" -printf '%P:%s:%T@\\n' | sort)"
out="$(tool pin user-one --dry-run)"
after="$(find "$STATE" -printf '%P:%s:%T@\\n' | sort)"
ok "pin dry-run writes nothing" '[ "$before" = "$after" ] && jq -e ".reason == \"would-pin\"" >/dev/null <<<"$out"'
out="$(tool pin user-one)"
ok "pin preserves autosave base and denies autonomous write" 'jq -e ".skill.classification == \"pinned\" and .skill.base_classification == \"autosave-managed\" and (.skill.autonomous_write_allowed | not)" >/dev/null <<<"$out"'
ok "pin control metadata is owner-only" '[ "$(stat -c %a "$STATE/skill-autosave-control.json")" = 600 ]'
out="$(tool unpin user-one)"
ok "unpin restores original ownership" 'jq -e ".skill.classification == \"autosave-managed\" and .skill.autonomous_write_allowed" >/dev/null <<<"$out"'

# Exact-read receipt authorizes one exact proposal, once.
read_json="$(tool read-target user-one SKILL.md --attempt-id review-1 --operation patch)"
ok "read-target returns content and bounded receipt" 'jq -e ".content_encoding == \"utf-8\" and (.content | contains(\"# user-one\")) and .receipt.consumed == false" >/dev/null <<<"$read_json"'
proposal_from_read "$read_json" "$TMP/proposal.json"
out="$(tool guard-proposal --proposal "$TMP/proposal.json")"; rc=$?
ok "exact same-attempt proposal is authorized" '[ "$rc" = 0 ] && jq -e ".allowed == true and .code == \"authorized\"" >/dev/null <<<"$out"'
out="$(tool guard-proposal --proposal "$TMP/proposal.json")"; rc=$?
ok "receipt is single-use" '[ "$rc" != 0 ] && jq -e ".code == \"receipt_consumed\"" >/dev/null <<<"$out"'

# Drift after the exact read consumes and rejects the stale proposal.
read_json="$(tool read-target user-one SKILL.md --attempt-id review-2 --operation edit)"
proposal_from_read "$read_json" "$TMP/drift-proposal.json"
printf '\n# changed after read\n' >> "$SKILLS/user-one/SKILL.md"
out="$(tool guard-proposal --proposal "$TMP/drift-proposal.json")"; rc=$?
ok "content or identity drift is rejected" '[ "$rc" = 3 ] && jq -e ".allowed == false and .code == \"target_drift\"" >/dev/null <<<"$out"'

# Re-adopted fixture for cross-attempt / operation mismatch checks.
make_skill user-two
tool adopt user-two >/dev/null
read_json="$(tool read-target user-two SKILL.md --attempt-id review-3 --operation write_file)"
proposal_from_read "$read_json" "$TMP/mismatch-proposal.json"
jq '.attempt_id = "another-attempt"' "$TMP/mismatch-proposal.json" > "$TMP/mismatch-proposal.tmp"
mv "$TMP/mismatch-proposal.tmp" "$TMP/mismatch-proposal.json"
chmod 600 "$TMP/mismatch-proposal.json"
out="$(tool guard-proposal --proposal "$TMP/mismatch-proposal.json")"; rc=$?
ok "cross-attempt receipt replay is rejected" '[ "$rc" = 3 ] && jq -e ".code == \"proposal_receipt_mismatch\"" >/dev/null <<<"$out"'

# Pinning after an exact read invalidates the pending autonomous proposal.
read_json="$(tool read-target user-two SKILL.md --attempt-id review-pin --operation patch)"
proposal_from_read "$read_json" "$TMP/pinned-proposal.json"
tool pin user-two >/dev/null
out="$(tool guard-proposal --proposal "$TMP/pinned-proposal.json")"; rc=$?
ok "pin blocks a previously read autonomous proposal" '[ "$rc" = 3 ] && jq -e ".code == \"autonomous_write_denied_pinned\"" >/dev/null <<<"$out"'
tool unpin user-two >/dev/null

# Managed provenance wins over autosave and stays self-update-only.
make_skill bundled-one
sha="$(sha256sum "$SKILLS/bundled-one/SKILL.md" | awk '{print $1}')"
jq -nc --arg sha "$sha" '{
  schema_version: 1,
  manager: "ccc-node",
  name: "bundled-one",
  source: "codex/skills/bundled-one",
  source_hash: $sha,
  files: {"SKILL.md": $sha}
}' > "$SKILLS/bundled-one/.ccc-node-managed.json"
chmod 600 "$SKILLS/bundled-one/.ccc-node-managed.json"
out="$(tool status bundled-one)"
ok "managed skill is autonomous read-only" 'jq -e ".skills[0].classification == \"managed/bundled\" and (.skills[0].autonomous_write_allowed | not)" >/dev/null <<<"$out"'
out="$(tool adopt bundled-one)"; rc=$?
ok "managed skill cannot be adopted" '[ "$rc" != 0 ] && jq -e ".code | startswith(\"adopt_denied_managed\")" >/dev/null <<<"$out"'
cp "$SKILLS/user-two/.autosave-meta.json" "$SKILLS/bundled-one/.autosave-meta.json"
chmod 600 "$SKILLS/bundled-one/.autosave-meta.json"
out="$(tool status bundled-one)"
ok "managed marker keeps precedence during dual-marker conflict" 'jq -e ".skills[0].base_classification == \"managed/bundled\" and .skills[0].reason == \"managed-marker-conflicts-with-autosave\"" >/dev/null <<<"$out"'

# Missing marker is user-owned; corrupt/mismatched metadata is unknown fail-closed.
make_skill corrupt-one
printf '{not-json\n' > "$SKILLS/corrupt-one/.autosave-meta.json"
chmod 600 "$SKILLS/corrupt-one/.autosave-meta.json"
out="$(tool status corrupt-one)"
ok "corrupt autosave provenance is unknown" 'jq -e ".skills[0].classification == \"unknown/unreadable\"" >/dev/null <<<"$out"'

make_skill legacy-one
legacy_sha="$(sha256sum "$SKILLS/legacy-one/SKILL.md" | awk '{print $1}')"
jq -nc --arg path "$SKILLS/legacy-one/SKILL.md" --arg sha "$legacy_sha" '{
  event: "install", installed_by: "autosave", name: "legacy-one",
  path: $path, sha256: $sha
}' > "$SKILLS/legacy-one/.autosave-meta.json"
chmod 644 "$SKILLS/legacy-one/.autosave-meta.json"
out="$(tool status legacy-one)"
ok "matching legacy marker migrates read-only in memory" 'jq -e ".skills[0].classification == \"autosave-managed\" and .skills[0].provenance_revision == 0" >/dev/null <<<"$out"'
printf '\n# manual drift\n' >> "$SKILLS/legacy-one/SKILL.md"
out="$(tool status legacy-one)"
ok "legacy SHA mismatch fails closed" 'jq -e ".skills[0].classification == \"unknown/unreadable\"" >/dev/null <<<"$out"'

# Unknown schemas, bool revisions, and loose v2 permissions never downgrade to
# the legacy contract or acquire autonomous authority.
make_skill future-one
future_sha="$(sha256sum "$SKILLS/future-one/SKILL.md" | awk '{print $1}')"
jq -nc --arg path "$SKILLS/future-one/SKILL.md" --arg sha "$future_sha" '{
  schema_version: 3, installed_by: "autosave", name: "future-one",
  path: $path, sha256: $sha
}' > "$SKILLS/future-one/.autosave-meta.json"
chmod 600 "$SKILLS/future-one/.autosave-meta.json"
out="$(tool status future-one)"
ok "unknown autosave schema cannot downgrade to legacy" 'jq -e ".skills[0].classification == \"unknown/unreadable\"" >/dev/null <<<"$out"'

make_skill bool-revision
tool adopt bool-revision >/dev/null
jq '.provenance_revision = true' "$SKILLS/bool-revision/.autosave-meta.json" > "$TMP/bool-marker"
mv "$TMP/bool-marker" "$SKILLS/bool-revision/.autosave-meta.json"
chmod 600 "$SKILLS/bool-revision/.autosave-meta.json"
out="$(tool status bool-revision)"
ok "boolean provenance revision fails closed" 'jq -e ".skills[0].classification == \"unknown/unreadable\"" >/dev/null <<<"$out"'

make_skill loose-v2
tool adopt loose-v2 >/dev/null
chmod 644 "$SKILLS/loose-v2/.autosave-meta.json"
out="$(tool status loose-v2)"
ok "v2 provenance must remain owner-only" 'jq -e ".skills[0].classification == \"unknown/unreadable\"" >/dev/null <<<"$out"'

# External/repo and unsafe path forms never become autonomous.
make_skill repo-one
mkdir "$SKILLS/repo-one/.git"
out="$(tool status repo-one)"
ok "repo marker classifies external installation" 'jq -e ".skills[0].classification == \"external/repo-installed\"" >/dev/null <<<"$out"'

make_skill symlink-target
ln -s "$SKILLS/symlink-target" "$SKILLS/symlink-leaf"
out="$(tool status symlink-leaf)"
ok "symlink skill is external and read-only" 'jq -e ".skills[0].classification == \"external/repo-installed\" and (.skills[0].autonomous_write_allowed | not)" >/dev/null <<<"$out"'

make_skill hardlink-one
ln "$SKILLS/hardlink-one/SKILL.md" "$TMP/hardlink-copy"
out="$(tool read-target hardlink-one SKILL.md --attempt-id review-hard --operation patch)"; rc=$?
ok "hardlinked target is refused" '[ "$rc" != 0 ] && jq -e ".code == \"read_denied_unknown\"" >/dev/null <<<"$out"'

out="$(tool read-target user-two ../SKILL.md --attempt-id review-path --operation patch)"; rc=$?
ok "path traversal is refused" '[ "$rc" != 0 ] && jq -e ".code == \"target_outside_skill\"" >/dev/null <<<"$out"'
out="$(tool read-target user-two /etc/passwd --attempt-id review-path2 --operation patch)"; rc=$?
ok "absolute external target is refused" '[ "$rc" != 0 ] && jq -e ".code == \"target_outside_skill\"" >/dev/null <<<"$out"'

ln -s /etc/passwd "$SKILLS/user-two/external-note"
out="$(tool read-target user-two external-note --attempt-id review-path3 --operation patch)"; rc=$?
ok "symlinked support target is refused" '[ "$rc" != 0 ] && jq -e ".code == \"unsafe_target_path\"" >/dev/null <<<"$out"'

ln -s "$SKILLS" "$TMP/skills-link"
out="$(python3 "$TOOL" --provider claude --skills-dir "$TMP/skills-link" --state-dir "$STATE" status user-two)"
ok "symlinked provider root fails closed" 'jq -e ".skills[0].classification == \"unknown/unreadable\" and .skills[0].reason == \"symlink_component\"" >/dev/null <<<"$out"'

BAD_STATE="$TMP/bad-state"
mkdir -m 700 "$BAD_STATE"
printf '{broken\n' > "$BAD_STATE/skill-autosave-control.json"
chmod 600 "$BAD_STATE/skill-autosave-control.json"
out="$(python3 "$TOOL" --provider claude --skills-dir "$SKILLS" --state-dir "$BAD_STATE" status user-two)"
ok "corrupt pin provenance fails all autonomous writes closed" 'jq -e ".skills[0].classification == \"unknown/unreadable\" and (.skills[0].autonomous_write_allowed | not)" >/dev/null <<<"$out"'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
