#!/usr/bin/env bash
# Tests for the canonical skills-intake review dispatcher/handler/installer.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HANDLER="$ROOT/scripts/skills-intake-review-handler.sh"
DISPATCHER="$ROOT/scripts/a2a-intent-dispatcher.sh"
INSTALLER="$ROOT/scripts/install-a2a-review-handler.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0
fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

HEAD_OK="$(printf 'a%.0s' $(seq 1 64))"
HEAD_OTHER="$(printf 'b%.0s' $(seq 1 64))"
BIN="$TMP/bin"
mkdir -p "$BIN"

# Stub review agent: emits the verdict JSON named by REVIEW_STUB_MODE.
cat > "$BIN/stub-agent" <<STUB
#!/usr/bin/env bash
cat >/dev/null
case "\${REVIEW_STUB_MODE:-approve}" in
  approve)
    printf '{"verdict":"approve","findings":[],"head_sha":"%s","skillName":"stub-skill","rubric_version":"2026-08-28.2","model":"stub-model"}' "\$REVIEW_STUB_HEAD"
    ;;
  wronghead)
    printf '{"verdict":"approve","findings":[],"head_sha":"%s","rubric_version":"2026-08-28.2"}' "\$REVIEW_STUB_OTHER_HEAD"
    ;;
  blocker)
    printf '{"verdict":"approve","findings":[{"severity":"blocker","area":"safety","note":"credential pattern"}],"head_sha":"%s","rubric_version":"2026-08-28.2"}' "\$REVIEW_STUB_HEAD"
    ;;
  prose)
    printf 'This candidate looks generally fine to me.'
    ;;
  crash) exit 1 ;;
  empty) exit 0 ;;
esac
STUB
chmod +x "$BIN/stub-agent"

make_task() {
  jq -n --arg head "$1" \
    '{id:"task-1", intent:"skills_intake_review",
      payload:{skillName:"stub-skill", rubricVersion:"2026-08-28.2",
        provenance:{author_node:"authornode", head_sha:$head, source_tree_sha256:"c3ab8ff13720e8ad9047dd39466b3c8974e592c2fa383d4a3960714caef0c4f2"},
        workerProcedure:"## Worker procedure\nApply the rubric.",
        verdictSchema:{verdict:"approve | revise | reject"},
        skillFiles:[{path:"SKILL.md", content:"# stub skill"}],
        inventorySnapshot:[]}}' > "$TMP/task.json"
}
run_handler() { # $1 task file; uses the shared stub agent
  REVIEW_AGENT_BIN="$BIN/stub-agent" REVIEW_AGENT_ARGS="" REVIEW_TIMEOUT_SEC=30 \
    WORKER_ID=testnode REVIEW_STUB_HEAD="$HEAD_OK" REVIEW_STUB_OTHER_HEAD="$HEAD_OTHER" \
    REVIEW_STUB_MODE="${REVIEW_STUB_MODE:-approve}" \
    bash "$HANDLER" < "$1"
}

make_task "$HEAD_OK"
REVIEW_STUB_MODE=approve run_handler "$TMP/task.json" > "$TMP/out-approve.json" 2>/dev/null; rc=$?
ok "approve verdict composes a bound result" \
  '[ "$rc" = 0 ] && jq -e ".output.verdict == \"approve\" and .output.head_sha == \"$HEAD_OK\"
    and .output.headSha == \"$HEAD_OK\" and .output.rubric_version == \"2026-08-28.2\"
    and .output.reviewer_node == \"testnode\" and .output.model == \"stub-model\"" >/dev/null "$TMP/out-approve.json"'
ok "approve validates as kind=review pass by the reviewer node" \
  'jq -e ".validations[0].kind == \"review\" and .validations[0].nodeId == \"testnode\" and .validations[0].verdict == \"pass\"" >/dev/null "$TMP/out-approve.json"'

REVIEW_STUB_MODE=approve run_handler "$TMP/task.json" 2>/dev/null | python3 -c '
import json, sys
r = json.load(sys.stdin)
assert r["output"]["head_sha"] and r["output"]["head_sha"] != ""
assert r["output"]["rubric_version"] == "2026-08-28.2"
' 2>/dev/null; rc=$?
ok "publisher binding keys survive (head_sha/rubric_version, snake_case)" '[ "$rc" = 0 ]'

make_task "$HEAD_OK"
REVIEW_STUB_MODE=wronghead run_handler "$TMP/task.json" > "$TMP/out-wrong.json" 2>/dev/null; rc=$?
ok "head_sha mismatch downgrades to revise with a major claims finding" \
  '[ "$rc" = 0 ] && jq -e ".output.verdict == \"revise\" and (.output.findings | length) == 1
    and .output.findings[0].severity == \"major\" and .output.findings[0].area == \"claims\"" >/dev/null "$TMP/out-wrong.json"'

make_task "$HEAD_OK"
REVIEW_STUB_MODE=blocker run_handler "$TMP/task.json" > "$TMP/out-blocker.json" 2>/dev/null; rc=$?
ok "blocker finding forces reject" \
  '[ "$rc" = 0 ] && jq -e ".output.verdict == \"reject\"" >/dev/null "$TMP/out-blocker.json"'

make_task "$HEAD_OK"
REVIEW_STUB_MODE=crash run_handler "$TMP/task.json" >/dev/null 2>&1; rc=$?
ok "agent crash is a retryable handler failure" '[ "$rc" != 0 ]'

make_task "$HEAD_OK"
REVIEW_STUB_MODE=empty run_handler "$TMP/task.json" >/dev/null 2>&1; rc=$?
ok "empty agent output is a handler failure" '[ "$rc" != 0 ]'

make_task "$HEAD_OK"
REVIEW_STUB_MODE=prose run_handler "$TMP/task.json" >/dev/null 2>&1; rc=$?
ok "prose-only output (no verdict JSON) is a handler failure" '[ "$rc" != 0 ]'

# ─── dispatcher routing ──────────────────────────────────────────────────────
printf '#!/usr/bin/env bash\necho REVIEW-HANDLER-CALLED\n' > "$BIN/review-stub"
printf '#!/usr/bin/env bash\necho DEFAULT-CALLED\n' > "$BIN/default-stub"
chmod +x "$BIN/review-stub" "$BIN/default-stub"
printf '{"intent":"skills_intake_review","payload":{}}\n' > "$TMP/i-review.json"
printf '{"intent":"something_else","payload":{}}\n' > "$TMP/i-other.json"

out="$(INTAKE_REVIEW_HANDLER="$BIN/review-stub" DEFAULT_TASK_HANDLER="$BIN/default-stub" bash "$DISPATCHER" < "$TMP/i-review.json" 2>/dev/null)"; rc=$?
ok "dispatcher routes review intents to the review handler" '[ "$rc" = 0 ] && [ "$out" = "REVIEW-HANDLER-CALLED" ]'
out="$(INTAKE_REVIEW_HANDLER="$BIN/review-stub" DEFAULT_TASK_HANDLER="$BIN/default-stub" bash "$DISPATCHER" < "$TMP/i-other.json" 2>/dev/null)"; rc=$?
ok "dispatcher routes other intents to the default task handler" '[ "$rc" = 0 ] && [ "$out" = "DEFAULT-CALLED" ]'
out="$(DEFAULT_TASK_HANDLER="$BIN/default-stub" bash "$DISPATCHER" < "$TMP/i-other.json" 2>/dev/null)"; rc=0; : "$out"
ok "dispatcher default handler path works from the repo checkout" '[ "$rc" = 0 ] && [ "$out" = "DEFAULT-CALLED" ]'

# ─── installer ────────────────────────────────────────────────────────────────
DEST="$TMP/install-dest"
bash "$INSTALLER" --dest "$DEST" >/dev/null 2>&1; rc=$?
ok "installer copies dispatcher and handler executable into dest" \
  '[ "$rc" = 0 ] && [ -x "$DEST/a2a-intent-dispatcher.sh" ] && [ -x "$DEST/skills-intake-review-handler.sh" ]'
bash "$INSTALLER" --dest "$DEST" >/dev/null 2>&1; rc=$?
ok "installer backs up a previous installation on reinstall" \
  '[ "$rc" = 0 ] && ls "$DEST"/a2a-intent-dispatcher.sh.bak-* >/dev/null 2>&1'
bash "$INSTALLER" --dest "$DEST/relative/path" >/dev/null 2>&1; rc=$?
ok "installer creates the destination directory" '[ "$rc" = 0 ] && [ -x "$DEST/relative/path/skills-intake-review-handler.sh" ]'
"$INSTALLER" --termux --dest "$TMP/termux-dest" >/dev/null 2>&1; rc=$?
ok "installer accepts the Termux profile flag"   '[ "$rc" = 0 ] && [ -x "$TMP/termux-dest/a2a-intent-dispatcher.sh" ] && [ -x "$TMP/termux-dest/skills-intake-review-handler.sh" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
