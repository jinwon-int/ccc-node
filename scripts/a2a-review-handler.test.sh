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
  stdouterr)
    printf 'quota exceeded: weekly limit'
    exit 1
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
REVIEW_STUB_MODE=stdouterr run_handler "$TMP/task.json" >/dev/null 2>"$TMP/err-stdouterr.txt"; rc=$?
ok "agent stdout failure message reaches handler logs" \
  '[ "$rc" != 0 ] && grep -q "quota exceeded" "$TMP/err-stdouterr.txt"'

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
bash "$INSTALLER" --termux --dest "$TMP/termux-dest" >/dev/null 2>&1; rc=$?
ok "installer accepts the Termux profile flag"   '[ "$rc" = 0 ] && [ -x "$TMP/termux-dest/a2a-intent-dispatcher.sh" ] && [ -x "$TMP/termux-dest/skills-intake-review-handler.sh" ]'

bash "$INSTALLER" --termux --dest "$TMP/termux-dest" >/dev/null 2>&1; rc=$?
ok "installer accepts the Termux profile flag"   '[ "$rc" = 0 ] && [ -x "$TMP/termux-dest/a2a-intent-dispatcher.sh" ] && [ -x "$TMP/termux-dest/skills-intake-review-handler.sh" ]'

# ---- #2027: review provenance fields (review_agent / review_model) ----
make_task "$HEAD_OK"
REVIEW_AGENT_BIN="$BIN/stub-agent" REVIEW_AGENT_ARGS="-p --model xai/grok-4.6" REVIEW_TIMEOUT_SEC=30 \
  WORKER_ID=testnode REVIEW_STUB_HEAD="$HEAD_OK" REVIEW_STUB_OTHER_HEAD="$HEAD_OTHER" REVIEW_STUB_MODE=approve \
  bash "$HANDLER" < "$TMP/task.json" > "$TMP/out-prov-model.json" 2>/dev/null; rc=$?
ok "explicit --model in REVIEW_AGENT_ARGS becomes review_model, bin becomes review_agent (#2027)" \
  '[ "$rc" = 0 ] && jq -e ".output.review_agent == \"stub-agent\" and .output.review_model == \"xai/grok-4.6\"
    and .output.reviewer_node == \"testnode\"" >/dev/null "$TMP/out-prov-model.json"'

make_task "$HEAD_OK"
REVIEW_AGENT_BIN="$BIN/stub-agent" REVIEW_AGENT_ARGS="--model=openai/gpt-5.6-sol -p" REVIEW_TIMEOUT_SEC=30 \
  WORKER_ID=testnode REVIEW_STUB_HEAD="$HEAD_OK" REVIEW_STUB_OTHER_HEAD="$HEAD_OTHER" REVIEW_STUB_MODE=approve \
  bash "$HANDLER" < "$TMP/task.json" > "$TMP/out-prov-eq.json" 2>/dev/null; rc=$?
ok "--model= form parses too (#2027)" \
  '[ "$rc" = 0 ] && jq -e ".output.review_model == \"openai/gpt-5.6-sol\"" >/dev/null "$TMP/out-prov-eq.json"'

# No --model configured: fall back to the agent self-report (stub emits model).
make_task "$HEAD_OK"
run_handler "$TMP/task.json" > "$TMP/out-prov-fallback.json" 2>/dev/null; rc=$?
ok "review_model falls back to the agent self-report when args carry no --model (#2027)" \
  '[ "$rc" = 0 ] && jq -e ".output.review_agent == \"stub-agent\" and .output.review_model == \"stub-model\"" >/dev/null "$TMP/out-prov-fallback.json"'

# ─── #1460: skills-intake revise handler ─────────────────────────────────
REVISE_HANDLER="$ROOT/scripts/skills-intake-revise-handler.sh"
TREE_OK="c3ab8ff13720e8ad9047dd39466b3c8974e592c2fa383d4a3960714caef0c4f2"

cat > "$BIN/revise-stub-agent" <<STUB
#!/usr/bin/env bash
cat >/dev/null
REVISE_STUB_HEAD="${TREE_OK}"
case "\${REVIEW_STUB_MODE:-revised}" in
  revised)
    printf '{"outcome":"revised","skillName":"stub-skill","sourceTreeSha256":"%s","changeSummary":"addressed findings 1-2 by rewriting the procedure","skillFiles":[{"path":"SKILL.md","content":"# revised stub skill"}],"model":"stub-model"}' "\$REVISE_STUB_HEAD"
    ;;
  revised-unbound)
    # omits the bindings — the handler fills them node-side
    printf '{"outcome":"revised","changeSummary":"rewrote the procedure","skillFiles":[{"path":"SKILL.md","content":"# revised stub skill"}]}'
    ;;
  wrongbinding)
    printf '{"outcome":"revised","skillName":"other-skill","sourceTreeSha256":"%s","changeSummary":"x","skillFiles":[{"path":"SKILL.md","content":"# x"}]}' "\$REVISE_STUB_HEAD"
    ;;
  unsafepath)
    printf '{"outcome":"revised","skillName":"stub-skill","sourceTreeSha256":"%s","changeSummary":"x","skillFiles":[{"path":"../escape.md","content":"# x"}]}' "\$REVISE_STUB_HEAD"
    ;;
  nofiles)
    printf '{"outcome":"revised","skillName":"stub-skill","sourceTreeSha256":"%s","changeSummary":"x"}' "\$REVISE_STUB_HEAD"
    ;;
  droprec)
    printf '{"outcome":"drop_recommendation","skillName":"stub-skill","sourceTreeSha256":"%s","dropRecommendation":{"reason":"superseded by the approved shared-id-registry skill"}}' "\$REVISE_STUB_HEAD"
    ;;
  prose)
    printf 'I revised the skill and it looks great now.'
    ;;
  crash) exit 1 ;;
esac
STUB
chmod +x "$BIN/revise-stub-agent"

make_revise_task() {
  local head64 tree12
  head64="$(printf 'd%.0s' $(seq 1 64))"
  tree12="${TREE_OK:0:12}"
  jq -n --arg tree "$TREE_OK" --arg head64 "$head64" --arg tree12 "$tree12" \
    '{id:"revise-task-1", intent:"skills_intake_revise",
      payload:{skillName:"stub-skill",
        provenance:{author_node:"authornode", provider:"claude", intake_pr:7,
          branch:("skill-intake/authornode/stub-skill-claude-" + $tree12),
          head_sha:$head64, source_tree_sha256:$tree, revise_round:1, revise_round_limit:2},
        findings:[{severity:"major", area:"procedure", note:"the procedure is one past incident narrowed"}],
        skillFiles:[{path:"SKILL.md", content:"# stub skill"}],
        reviseResultSchema:{outcome:"revised | drop_recommendation"},
        workerProcedure:"## Worker procedure\nAddress the findings holistically."}}' > "$TMP/revise-task.json"
}
run_revise_handler() {
  REVIEW_AGENT_BIN="$BIN/revise-stub-agent" REVIEW_AGENT_ARGS="" REVIEW_TIMEOUT_SEC=30 \
    WORKER_ID=testnode REVIEW_STUB_MODE="${REVIEW_STUB_MODE:-revised}" \
    bash "$REVISE_HANDLER" < "$TMP/revise-task.json"
}

make_revise_task
REVIEW_STUB_MODE=revised run_revise_handler > "$TMP/out-revised.json" 2>/dev/null; rc=$?
ok "revised outcome composes a bound TaskResult (#1460)" \
  '[ "$rc" = 0 ] && jq -e ".output.outcome == \"revised\" and .output.skillName == \"stub-skill\"
    and .output.sourceTreeSha256 == \"$TREE_OK\" and (.output.skillFiles | length) == 1
    and .output.skillFiles[0].path == \"SKILL.md\" and .output.reviser_node == \"testnode\"
    and .output.reviser_agent == \"revise-stub-agent\" and .output.model == \"stub-model\"" >/dev/null "$TMP/out-revised.json"'

make_revise_task
REVIEW_STUB_MODE=revised-unbound run_revise_handler > "$TMP/out-unbound.json" 2>/dev/null; rc=$?
ok "missing bindings are filled node-side from the packet (#1460)" \
  '[ "$rc" = 0 ] && jq -e ".output.outcome == \"revised\" and .output.skillName == \"stub-skill\"
    and .output.sourceTreeSha256 == \"$TREE_OK\"" >/dev/null "$TMP/out-unbound.json"'

make_revise_task
REVIEW_STUB_MODE=wrongbinding run_revise_handler >/dev/null 2>&1; rc=$?
ok "contradicting skillName binding is a handler failure, never a revision (#1460)" '[ "$rc" != 0 ]'

make_revise_task
REVIEW_STUB_MODE=unsafepath run_revise_handler >/dev/null 2>&1; rc=$?
ok "unsafe candidate path (../) is rejected (#1460)" '[ "$rc" != 0 ]'

make_revise_task
REVIEW_STUB_MODE=nofiles run_revise_handler >/dev/null 2>&1; rc=$?
ok "revised outcome without skillFiles is a handler failure (#1460)" '[ "$rc" != 0 ]'

make_revise_task
REVIEW_STUB_MODE=droprec run_revise_handler > "$TMP/out-droprec.json" 2>/dev/null; rc=$?
ok "drop_recommendation outcome composes a bounded result (#1460)" \
  '[ "$rc" = 0 ] && jq -e ".output.outcome == \"drop_recommendation\"
    and .output.dropRecommendation.reason != \"\" and .output.skillFiles == null" >/dev/null "$TMP/out-droprec.json"'

make_revise_task
REVIEW_STUB_MODE=prose run_revise_handler >/dev/null 2>&1; rc=$?
ok "prose-only reviser output is a handler failure (#1460)" '[ "$rc" != 0 ]'

make_revise_task
REVIEW_STUB_MODE=crash run_revise_handler >/dev/null 2>&1; rc=$?
ok "reviser agent crash is a retryable handler failure (#1460)" '[ "$rc" != 0 ]'

# non-revise intents stay rejected by the revise handler
printf '{"id":"x","intent":"skills_intake_review","payload":{}}\n' | \
  REVIEW_AGENT_BIN="$BIN/revise-stub-agent" bash "$REVISE_HANDLER" >/dev/null 2>&1; rc=$?
ok "revise handler rejects non-revise intents" '[ "$rc" != 0 ]'

# ─── dispatcher revise routing (#1460) ──────────────────────────────────
printf '#!/usr/bin/env bash\necho REVISE-HANDLER-CALLED\n' > "$BIN/revise-route-stub"
chmod +x "$BIN/revise-route-stub"
printf '{"intent":"skills_intake_revise","payload":{}}\n' > "$TMP/i-revise.json"
out="$(INTAKE_REVISE_HANDLER="$BIN/revise-route-stub" DEFAULT_TASK_HANDLER="$BIN/default-stub" bash "$DISPATCHER" < "$TMP/i-revise.json" 2>/dev/null)"; rc=$?
ok "dispatcher routes revise intents to the revise handler (#1460)" '[ "$rc" = 0 ] && [ "$out" = "REVISE-HANDLER-CALLED" ]'
# A node without the revise handler fails LOUDLY (never a generic ack).
out="$(INTAKE_REVISE_HANDLER="$TMP/definitely-missing.sh" DEFAULT_TASK_HANDLER="$BIN/default-stub" bash "$DISPATCHER" < "$TMP/i-revise.json" 2>&1 >/dev/null)"; rc=$?
ok "missing revise handler fails loudly instead of acking (#1460)" \
  '[ "$rc" != 0 ] && grep -q "revise-unsupported" <<<"$out"'
# ─── installer ships the revise handler (#1460) ─────────────────────────────
DEST="$TMP/install-dest-1460"
bash "$INSTALLER" --dest "$DEST" >/dev/null 2>&1; rc=$?
ok "installer copies dispatcher + review + revise handlers into dest (#1460)" \
  '[ "$rc" = 0 ] && [ -x "$DEST/skills-intake-revise-handler.sh" ] && [ -x "$DEST/skills-intake-review-handler.sh" ]'
bash "$INSTALLER" --termux --dest "$TMP/termux-dest-1460" >/dev/null 2>&1; rc=$?
ok "installer Termux profile ships the revise handler (#1460)" '[ "$rc" = 0 ] && [ -x "$TMP/termux-dest-1460/skills-intake-revise-handler.sh" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
