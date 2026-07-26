#!/usr/bin/env bash
# Hermetic incremental skill-proposal mutation contracts (#751).
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
  python3 "$TOOL" --provider codex --skills-dir "$SKILLS" --state-dir "$STATE" "$@"
}

make_skill() {
  local name="$1"
  mkdir -m 700 "$SKILLS/$name"
  printf -- '---\nname: %s\ndescription: A sufficiently detailed recurring workflow for incremental ownership tests.\n---\n\n# %s\n\n## Procedure\n1. Read.\n2. Verify.\n3. Record.\n' \
    "$name" "$name" > "$SKILLS/$name/SKILL.md"
  chmod 600 "$SKILLS/$name/SKILL.md"
  tool adopt "$name" >/dev/null
}

make_created_skill() {
  local name="$1"
  mkdir -m 700 "$SKILLS/$name"
  printf -- '---\nname: %s\ndescription: A sufficiently detailed recurring workflow for incremental ownership tests.\n---\n\n# %s\n\n## Procedure\n1. Read.\n2. Verify.\n3. Record.\n' \
    "$name" "$name" > "$SKILLS/$name/SKILL.md"
  chmod 600 "$SKILLS/$name/SKILL.md"
  tool mark-created "$name" >/dev/null
}

make_patch() { # name relative old new id output
  local name="$1" relative="$2" old="$3" new="$4" id="$5" output="$6"
  local sha
  sha="$(sha256sum "$SKILLS/$name/$relative" | awk '{print $1}')"
  jq -nc \
    --arg id "$id" --arg name "$name" --arg relative "$relative" \
    --arg sha "$sha" --arg old "$old" --arg new "$new" \
    '{
      schema_version:2,
      proposal_id:$id,
      provenance:{
        provider:"codex",
        source_thread_hash:("a"*64),
        trigger:"checkpoint",
        distilled_at:"2026-07-27T00:00:00Z"
      },
      proposal:{
        action:"patch",
        target_skill:$name,
        relative_target:$relative,
        expected_sha256:$sha,
        old_text:$old,
        new_text:$new,
        improvement_reason:"Preserve a repeatable verified improvement.",
        reason:"Improve the existing overlapping skill.",
        evidence_excerpt:"repeatable improvement"
      }
    }' > "$output"
  chmod 600 "$output"
}

make_write() { # name relative content id output
  local name="$1" relative="$2" content="$3" id="$4" output="$5"
  local status revision provenance
  status="$(tool status "$name")"
  revision="$(jq -r '.skills[0].provenance_revision' <<<"$status")"
  provenance="$(jq -r '.skills[0].provenance_sha256' <<<"$status")"
  jq -nc \
    --arg id "$id" --arg name "$name" --arg relative "$relative" \
    --arg content "$content" --argjson revision "$revision" \
    --arg provenance "$provenance" \
    '{
      schema_version:2,
      proposal_id:$id,
      provenance:{
        provider:"codex",
        source_thread_hash:("b"*64),
        trigger:"checkpoint",
        distilled_at:"2026-07-27T00:00:00Z"
      },
      proposal:{
        action:"write_file",
        target_skill:$name,
        relative_target:$relative,
        expected_absent:true,
        expected_provenance_revision:$revision,
        expected_provenance_sha256:$provenance,
        content:$content,
        improvement_reason:"Add one bounded support file.",
        reason:"Improve the existing overlapping skill.",
        evidence_excerpt:"bounded support file"
      }
    }' > "$output"
  chmod 600 "$output"
}

make_skill patch-one
make_patch patch-one SKILL.md "1. Read." "1. Read twice." "$(printf '1%.0s' {1..64})" "$TMP/patch.json"
before="$(sha256sum "$SKILLS/patch-one/SKILL.md")"
ledger_before="$(grep -c '"event":"skill-proposal-apply"' "$STATE/skill-autosave-ownership.jsonl" || true)"
out="$(tool apply-proposal --proposal "$TMP/patch.json" --dry-run)"
ledger_after="$(grep -c '"event":"skill-proposal-apply"' "$STATE/skill-autosave-ownership.jsonl" || true)"
ok "patch dry-run reports hashes without mutation" \
  'jq -e ".dry_run == true and .changed == false" >/dev/null <<<"$out" && [ "$before" = "$(sha256sum "$SKILLS/patch-one/SKILL.md")" ] && [ "$ledger_before" = "$ledger_after" ]'

out="$(tool apply-proposal --proposal "$TMP/patch.json")"
ok "exact patch applies and advances provenance" \
  'jq -e ".changed == true and .code == \"applied\"" >/dev/null <<<"$out" && grep -q "1. Read twice." "$SKILLS/patch-one/SKILL.md" && jq -e ".provenance_revision == 2" "$SKILLS/patch-one/.autosave-meta.json" >/dev/null'
out="$(tool apply-proposal --proposal "$TMP/patch.json")"
ok "patch replay is idempotent and counted once" \
  'jq -e ".changed == false and .idempotent == true and .code == \"already_applied\"" >/dev/null <<<"$out" && [ "$(jq -s "[.[] | select(.event == \"skill-proposal-apply\" and .outcome == \"applied\" and .proposal_id == (\"1\"*64))] | length" "$STATE/skill-autosave-ownership.jsonl")" = 1 ]'

make_patch patch-one SKILL.md "1. Read twice." "1. Read three times." "$(printf '2%.0s' {1..64})" "$TMP/stale.json"
sed -i 's/1\. Read twice\./1. Read changed externally./' "$SKILLS/patch-one/SKILL.md"
out="$(tool apply-proposal --proposal "$TMP/stale.json")"; rc=$?
ok "stale expected hash fails without overwriting" \
  '[ "$rc" = 2 ] && jq -e ".code == \"target_drift\" or .code == \"autonomous_write_denied_unknown_unreadable\"" >/dev/null <<<"$out" && grep -q "changed externally" "$SKILLS/patch-one/SKILL.md"'

make_skill write-one
mkdir -m 700 "$SKILLS/write-one/references"
make_write write-one references/checklist.md $'# Checklist\n\n- Verify twice.\n' "$(printf '3%.0s' {1..64})" "$TMP/write.json"
out="$(tool apply-proposal --proposal "$TMP/write.json")"
ok "absence-bound write_file publishes without replacement" \
  'jq -e ".changed == true and .action == \"write_file\"" >/dev/null <<<"$out" && grep -q "Verify twice" "$SKILLS/write-one/references/checklist.md" && [ "$(stat -c %a "$SKILLS/write-one/references/checklist.md")" = 600 ]'
make_write write-one references/checklist.md "replacement" "$(printf '4%.0s' {1..64})" "$TMP/replace.json"
out="$(tool apply-proposal --proposal "$TMP/replace.json")"; rc=$?
ok "write_file never replaces an existing target" \
  '[ "$rc" = 2 ] && jq -e ".code == \"target_already_exists\"" >/dev/null <<<"$out" && grep -q "Verify twice" "$SKILLS/write-one/references/checklist.md"'

jq '.proposal.relative_target="../escape.md" | .proposal_id=("5"*64)' \
  "$TMP/write.json" > "$TMP/traversal.json"
chmod 600 "$TMP/traversal.json"
out="$(tool apply-proposal --proposal "$TMP/traversal.json")"; rc=$?
ok "proposal traversal is rejected before mutation" \
  '[ "$rc" = 2 ] && jq -e ".code == \"target_outside_skill\"" >/dev/null <<<"$out" && [ ! -e "$SKILLS/escape.md" ]'

make_skill hardlink-skill
mkdir -m 700 "$SKILLS/hardlink-skill/references"
printf 'repeat once\n' > "$SKILLS/hardlink-skill/references/note.md"
chmod 600 "$SKILLS/hardlink-skill/references/note.md"
make_patch hardlink-skill references/note.md "repeat once" "repeat twice" "$(printf '6%.0s' {1..64})" "$TMP/hardlink.json"
ln "$SKILLS/hardlink-skill/references/note.md" "$TMP/hardlink-copy"
out="$(tool apply-proposal --proposal "$TMP/hardlink.json")"; rc=$?
ok "hardlinked patch target is rejected" \
  '[ "$rc" = 2 ] && jq -e ".code == \"unsafe_target_file\"" >/dev/null <<<"$out" && grep -q "repeat once" "$TMP/hardlink-copy"'

# A non-cooperating same-owner writer can create a new leaf after the old
# target has been claimed but before the proposal publishes. The no-replace
# commit must preserve that raced leaf and record conflict instead of
# overwriting it.
make_skill patch-race
make_patch patch-race SKILL.md "1. Read." "1. Read proposed." "$(printf 'c%.0s' {1..64})" "$TMP/patch-race.json"
TOOL_PATH="$TOOL" SKILLS_PATH="$SKILLS" STATE_PATH="$STATE" PROPOSAL_PATH="$TMP/patch-race.json" python3 - <<'PY'
import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("ownership_incremental_patch_race", os.environ["TOOL_PATH"])
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
context = module.Context(
    provider="codex",
    skills_dir=Path(os.environ["SKILLS_PATH"]),
    state_dir=Path(os.environ["STATE_PATH"]),
    uid=os.geteuid(),
)
real_rename = module._rename_noreplace
injected = False
external = (
    b"---\nname: patch-race\n"
    b"description: External writer content must survive the proposal race.\n"
    b"---\n\n# External\n\n## Procedure\n1. Preserve external update.\n"
)


def race_before_publish(parent_fd, source, destination):
    global injected
    if (
        not injected
        and source.startswith(".ccc-skill-proposal.")
        and destination == "SKILL.md"
    ):
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(descriptor, external)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        injected = True
    return real_rename(parent_fd, source, destination)


module._rename_noreplace = race_before_publish
try:
    module._command_apply_proposal(
        context,
        Path(os.environ["PROPOSAL_PATH"]),
        dry_run=False,
        automatic=False,
        daily_cap=None,
    )
except module.ContractError as error:
    assert error.code == "incremental_rollback_conflict"
else:
    raise SystemExit(1)
assert (context.skills_dir / "patch-race" / "SKILL.md").read_bytes() == external
PY
rc=$?
ok "raced patch leaf is preserved without overwrite" \
  '[ "$rc" = 0 ] && grep -q "External writer content" "$SKILLS/patch-race/SKILL.md" && jq -s -e "[.[] | select(.event == \"skill-proposal-apply\" and .proposal_id == (\"c\"*64) and .outcome == \"conflict\")] | length == 1" "$STATE/skill-autosave-ownership.jsonl" >/dev/null'

# If the canonical parent drifts after a no-replace support-file link and a
# concurrent writer replaces the published leaf, rollback must not unlink the
# writer's entry.
make_skill write-race
mkdir -m 700 "$SKILLS/write-race/references"
TOOL_PATH="$TOOL" SKILLS_PATH="$SKILLS" STATE_PATH="$STATE" python3 - <<'PY'
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import sys

spec = importlib.util.spec_from_file_location("ownership_incremental_write_race", os.environ["TOOL_PATH"])
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
context = module.Context(
    provider="codex",
    skills_dir=Path(os.environ["SKILLS_PATH"]),
    state_dir=Path(os.environ["STATE_PATH"]),
    uid=os.geteuid(),
)
real_parent = module._target_parent_fd
calls = 0
external = b"external support writer wins\n"


@contextmanager
def drift_after_link(context_arg, name, relative):
    global calls
    with real_parent(context_arg, name, relative) as opened:
        calls += 1
        parent_fd, leaf, parent = opened
        if calls == 3:
            replacement = ".external-support-replacement"
            descriptor = os.open(
                replacement,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                os.write(descriptor, external)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                replacement,
                leaf,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            parent = SimpleNamespace(
                st_dev=parent.st_dev,
                st_ino=parent.st_ino + 1,
            )
        yield parent_fd, leaf, parent


module._target_parent_fd = drift_after_link
try:
    module._create_target_noreplace(
        context,
        "write-race",
        "references/new.md",
        b"proposal support content\n",
    )
except module.ContractError as error:
    assert error.code == "target_parent_drift"
else:
    raise SystemExit(1)
assert (
    context.skills_dir / "write-race" / "references" / "new.md"
).read_bytes() == external
PY
rc=$?
ok "parent-drift rollback never unlinks a raced support leaf" \
  '[ "$rc" = 0 ] && grep -q "external support writer wins" "$SKILLS/write-race/references/new.md" && ! find "$SKILLS/write-race/references" -name ".ccc-skill-proposal.*" | grep -q .'

# Adopted user skills are incrementally writable only with explicit owner
# approval; unattended mutation is refused because whole-skill rollback is
# intentionally unavailable for adopted content.
make_skill adopted-auto
make_patch adopted-auto SKILL.md "1. Read." "1. Read unattended." "$(printf 'd%.0s' {1..64})" "$TMP/adopted-auto.json"
out="$(tool apply-proposal --proposal "$TMP/adopted-auto.json" --automatic --daily-cap 100)"; rc=$?
ok "automatic mutation requires rollback-eligible autosave provenance" \
  '[ "$rc" = 2 ] && jq -e ".code == \"incremental_auto_rollback_unavailable\"" >/dev/null <<<"$out" && grep -q "1. Read." "$SKILLS/adopted-auto/SKILL.md"'

# One authoritative automatic slot is competed for under the ownership lock.
make_created_skill cap-a
make_created_skill cap-b
make_patch cap-a SKILL.md "1. Read." "1. Read cap A." "$(printf '7%.0s' {1..64})" "$TMP/cap-a.json"
make_patch cap-b SKILL.md "1. Read." "1. Read cap B." "$(printf '8%.0s' {1..64})" "$TMP/cap-b.json"
tool apply-proposal --proposal "$TMP/cap-a.json" --automatic --daily-cap 1 >"$TMP/cap-a.out" &
p1=$!
tool apply-proposal --proposal "$TMP/cap-b.json" --automatic --daily-cap 1 >"$TMP/cap-b.out" &
p2=$!
wait "$p1"; r1=$?
wait "$p2"; r2=$?
ok "concurrent auto apply cannot double-spend the last cap slot" \
  '[ "$(( (r1 == 0) + (r2 == 0) ))" = 1 ] && [ "$(cat "$TMP/cap-a.out" "$TMP/cap-b.out" | jq -s "[.[] | select(.code == \"incremental_daily_cap_exhausted\")] | length")" = 1 ]'

# Inject a crash window after durable file+marker mutation but before terminal
# ledger append; replay must recover and write exactly one applied terminal.
make_created_skill recover-one
make_patch recover-one SKILL.md "1. Read." "1. Read recovered." "$(printf '9%.0s' {1..64})" "$TMP/recover.json"
TOOL_PATH="$TOOL" SKILLS_PATH="$SKILLS" STATE_PATH="$STATE" PROPOSAL_PATH="$TMP/recover.json" python3 - <<'PY'
import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("ownership_incremental_recovery", os.environ["TOOL_PATH"])
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
context = module.Context(
    provider="codex",
    skills_dir=Path(os.environ["SKILLS_PATH"]),
    state_dir=Path(os.environ["STATE_PATH"]),
    uid=os.geteuid(),
)
real_append = module._append_ledger
failed = False


def crash_before_terminal(context_arg, record):
    global failed
    if (
        not failed
        and record.get("event") == "skill-proposal-apply"
        and record.get("outcome") == "applied"
    ):
        failed = True
        raise module.ContractError("simulated_terminal_crash")
    return real_append(context_arg, record)


module._append_ledger = crash_before_terminal
try:
    module._command_apply_proposal(
        context,
        Path(os.environ["PROPOSAL_PATH"]),
        dry_run=False,
        automatic=True,
        daily_cap=100,
    )
except module.ContractError as error:
    assert error.code == "simulated_terminal_crash"
else:
    raise SystemExit(1)
module._append_ledger = real_append
result = module._command_apply_proposal(
    context,
    Path(os.environ["PROPOSAL_PATH"]),
    dry_run=False,
    automatic=True,
    daily_cap=100,
)
assert result["code"] == "recovered_applied"
assert result["recovered"] is True
assert result["counted"] is True
assert "1. Read recovered." in (
    Path(os.environ["SKILLS_PATH"]) / "recover-one" / "SKILL.md"
).read_text()
PY
rc=$?
ok "prepared replay recovers durable mutation exactly once" \
  '[ "$rc" = 0 ] && [ "$(jq -s "[.[] | select(.event == \"skill-proposal-apply\" and .proposal_id == (\"9\"*64) and .outcome == \"applied\")] | length" "$STATE/skill-autosave-ownership.jsonl")" = 1 ]'

# A crash after marker mutation but before target mutation is an active
# rollback, not a no-op abort. Recovery restores the marker, records one
# rolled_back terminal, and releases the automatic cap reservation.
make_created_skill recover-marker-only
make_patch recover-marker-only SKILL.md "1. Read." "1. Read marker recovery." "$(printf 'f%.0s' {1..64})" "$TMP/recover-marker.json"
TOOL_PATH="$TOOL" SKILLS_PATH="$SKILLS" STATE_PATH="$STATE" PROPOSAL_PATH="$TMP/recover-marker.json" python3 - <<'PY'
import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("ownership_incremental_marker_recovery", os.environ["TOOL_PATH"])
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
context = module.Context(
    provider="codex",
    skills_dir=Path(os.environ["SKILLS_PATH"]),
    state_dir=Path(os.environ["STATE_PATH"]),
    uid=os.geteuid(),
)
with module._MutationLock(context):
    envelope = module._incremental_proposal(
        Path(os.environ["PROPOSAL_PATH"]),
        context,
    )
    plan = module._build_incremental_plan(
        context,
        envelope,
        automatic=True,
    )
    backup = module._incremental_backup(context, plan)
    fields = module._incremental_transaction_fields(
        context,
        plan,
        backup,
        automatic=True,
        cap_day="2098-01-01",
        cap_slot=1,
    )
    prepared = module._transaction_record(
        "skill-proposal-apply",
        "marker-only-recovery",
        outcome="prepared",
        fields=fields,
    )
    module._append_ledger(context, prepared)
    module._write_existing_json_in_skill(
        context,
        plan.name,
        module._AUTOSAVE_MARKER,
        plan.marker_after,
    )
    outcome = module._recover_incremental_transaction(
        context,
        prepared,
        envelope,
    )
    assert outcome == "rolled_back"
    assert module._read_target(
        context,
        plan.name,
        plan.relative,
    ).sha256 == module._sha256(plan.old_payload)
    marker = module._safe_json_file(
        context.skills_dir / plan.name / module._AUTOSAVE_MARKER,
        owner=context.uid,
        exact_mode=0o600,
    )
    assert module._sha256(module._canonical_json(marker)) == plan.marker_before_sha256
    rows = module._read_ledger(context)
    assert module._automatic_cap_used(rows, "2098-01-01") == 0
    terminals = [
        row
        for row in rows
        if row.get("transaction_id") == "marker-only-recovery"
        and row.get("outcome") == "rolled_back"
    ]
    assert len(terminals) == 1
PY
rc=$?
ok "marker-only recovery records rolled_back and releases cap" \
  '[ "$rc" = 0 ] && jq -s -e "[.[] | select(.transaction_id == \"marker-only-recovery\" and .outcome == \"rolled_back\")] | length == 1" "$STATE/skill-autosave-ownership.jsonl" >/dev/null'

# Cap accounting rejects impossible or corrupted transaction state instead of
# letting an unknown last outcome release a previously consumed slot.
jq -nc '{
  schema_version:1,
  event:"skill-proposal-apply",
  transaction_id:"invalid-cap-transition",
  ts:"2099-01-01T00:00:00Z",
  outcome:"unknown-terminal",
  proposal_id:("e"*64),
  automatic:true,
  cap_day:"2099-01-01"
}' >> "$STATE/skill-autosave-ownership.jsonl"
out="$(tool automatic-usage --day 2099-01-01)"; rc=$?
ok "invalid cap-ledger transition fails closed" \
  '[ "$rc" = 2 ] && jq -e ".code == \"incremental_ledger_state_invalid\"" >/dev/null <<<"$out"'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
