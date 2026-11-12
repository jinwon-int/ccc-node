#!/usr/bin/env bash
# Tests for delegated-reply.sh / lib/delegated_reply.py (ccc-node #2137, spec §10.2 5-d slice 3) —
# the UserPromptSubmit hook that notes the owner's non-approval of an open delegated-approval ask.
# Hermetic: private HOME/state, a synthetic transcript, the recorder from this checkout, no network.
# The contract is asserted directly: off unless opted in, always exit 0, never any output, no text
# in the ledger, and "when in doubt it is not a refusal".
# shellcheck disable=SC2034  # out / rc are read via eval inside ok()
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
HOOK="$HERE/delegated-reply.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export HOME="$TMP/home"
export CCC_STATE_DIR="$HOME/.claude/state"
export CODEX_HOME="$TMP/no-codex"
export CCC_DELEGATED_RECORDER_DIR="$ROOT/codex/skills/gh-pr-flow/scripts"
unset CCC_DELEGATED_SHADOW
REC="$CCC_DELEGATED_RECORDER_DIR/delegated_approval_shadow.py"
DA="$CCC_STATE_DIR/delegated-approval"
LEDGER="$DA/shadow.jsonl"
TR="$TMP/transcript.jsonl"
PRJSON="$TMP/pr.json"
mkdir -p "$CCC_STATE_DIR"
printf '{"files":[{"path":"docs/a.md"}],"additions":1,"deletions":0,"title":"docs (#1)","author":{"login":"jinon86"},"baseRefName":"main"}' >"$PRJSON"

reset() { rm -rf "$DA"; mkdir -p "$DA"; : >"$TR"; }
on() { : >"$DA/reply-hook.on"; }
ask() { # <repo> <pr> <head-char>
  python3 "$REC" ask --repo "jinwon-int/$1" --pr "$2" --head "$(printf "$3%.0s" $(seq 40))" \
    --pr-json-file "$PRJSON" --state-dir "$CCC_STATE_DIR" --node t 2>/dev/null
}
say() { # the agent's last message; a sidechain message after it must be ignored
  python3 - "$TR" "$1" <<'PY'
import json, sys
path, text = sys.argv[1], sys.argv[2]
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "user", "message": {"content": "earlier"}}) + "\n")
    fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}, ensure_ascii=False) + "\n")
    fh.write(json.dumps({"type": "assistant", "isSidechain": True,
                         "message": {"content": [{"type": "text", "text": "**[답변 필요]** 하나만 선택\n1. 999 승인\n2. x"}]}}, ensure_ascii=False) + "\n")
    fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}) + "\n")
PY
}
send() { # <prompt> — runs the hook like Claude Code does (payload on stdin); sets out and rc
  python3 -c 'import json,sys; print(json.dumps({"prompt": sys.argv[1], "session_id": "s", "transcript_path": sys.argv[2]}, ensure_ascii=False))' "$1" "$TR" >"$TMP/payload.json"
  out="$(bash "$HOOK" <"$TMP/payload.json" 2>&1)"; rc=$?
}
decisions() { # "pr:decision:detail" per decision row, in order
  [ -f "$LEDGER" ] || return 0
  python3 - "$LEDGER" <<'PY'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if row.get("kind") == "decision":
        print(f"{row['pr']}:{row['decision']}:{row['detail']}:{row['source']}")
PY
}
ONE=$'결과입니다.\n\n**[답변 필요]** 하나만 선택\n1. `41 승인` — head `aaaa1111` 교차리뷰 후 머지 큐 (권장)\n2. 머지 보류 — 다음 빌드 일정에 맞춰 다시 결정'
MANY=$'결과입니다.\n\n**[답변 필요]** 여러 개 선택 가능\n1. `41 승인` — head `aaaa1111` 머지 (권장)\n2. 조각 2 착수 — PR과 CI까지\n3. 이슈 등록'

# --- off by default, and the fast exits ---
reset; ask ccc-node 41 a; say "$ONE"
send '2. 머지 보류 — 다음 빌드 일정에 맞춰 다시 결정'
ok "off unless the node opted in" '[ "$rc" = 0 ] && [ -z "$out" ] && [ -z "$(decisions)" ]'
on
ok "ask left the open-asks marker" '[ -e "$DA/open-asks" ]'
CCC_DELEGATED_SHADOW=0 send '2. 머지 보류 — 다음 빌드 일정에 맞춰 다시 결정'
ok "CCC_DELEGATED_SHADOW=0 turns it off" '[ "$rc" = 0 ] && [ -z "$out" ] && [ -z "$(decisions)" ]'
rm -f "$DA/open-asks"
send '2. 머지 보류 — 다음 빌드 일정에 맞춰 다시 결정'
ok "no open-asks marker: exits before the classifier" '[ -z "$(decisions)" ]'

# --- single-choice block: another option is a decline ---
reset; on; ask ccc-node 41 a; say "$ONE"
send '2. 머지 보류 — 다음 빌드 일정에 맞춰 다시 결정'
ok "another option sent back verbatim is a decline" '[ "$rc" = 0 ] && [ -z "$out" ] && [ "$(decisions)" = "41:decline:other-option:reply" ]'
ok "the prompt text is not in the ledger" '! grep -q "다음 빌드" "$LEDGER"'
send '2. 머지 보류 — 다음 빌드 일정에 맞춰 다시 결정'
ok "a closed ask is not decided twice" '[ "$(decisions | wc -l)" = 1 ]'
ok "the marker goes away once nothing is open" '[ ! -e "$DA/open-asks" ]'

reset; on; ask ccc-node 41 a; say "$ONE"
send '2'
ok "a bare number picking the other option is a decline" '[ "$(decisions)" = "41:decline:other-option:reply" ]'

# --- approvals are left to the approval tool ---
reset; on; ask ccc-node 41 a; say "$ONE"
send '1. 41 승인 — head aaaa1111 교차리뷰 후 머지 큐 (권장)'
ok "the approval option sent back records nothing" '[ -z "$(decisions)" ]'
send '1'
ok "a bare number picking the approval option records nothing" '[ -z "$(decisions)" ]'
send '41 승인'
ok "the approval phrase alone records nothing" '[ -z "$(decisions)" ]'

# --- hold needs the word and the PR number ---
reset; on; ask ccc-node 41 a; say "$ONE"
send '41은 일단 보류하자'
ok "hold word with the PR number is a hold" '[ "$(decisions)" = "41:hold:hold-word:reply" ]'
reset; on; ask ccc-node 41 a; say "작업을 계속합니다."
send '오늘은 여기서 중단하자'
ok "hold word without the PR number records nothing" '[ -z "$(decisions)" ]'
send '141 보류'
ok "another number that merely contains the digits records nothing" '[ -z "$(decisions)" ]'

# --- when in doubt it is not a refusal ---
reset; on; ask ccc-node 41 a; say "$MANY"
send $'2. 조각 2 착수 — PR과 CI까지\n3. 이슈 등록'
ok "multi-choice: not picking the approval is a note, not a decline" '[ "$(decisions)" = "41:reply-unclassified:multi-choice-not-picked:reply" ]'
reset; on; ask ccc-node 41 a; say "$ONE"
send '잠깐, 이 변경이 다른 노드에도 영향이 있어?'
ok "free text while the approval was offered is a note" '[ "$(decisions)" = "41:reply-unclassified:free-text:reply" ]'
ok "a note leaves the ask open" '[ -e "$DA/open-asks" ]'
send '[external_event: github_pr_checks terminal=success repo=jinwon-int/ccc-node pr=41 head=aaaa1111]'
ok "a harness wake-up is not the owner answering" '[ "$(decisions | wc -l)" = 1 ]'
reset; on; ask ccc-node 41 a; say $'**[답변 필요]** 하나만 선택\n1. 조각 3 착수\n2. 보류'
send '2. 보류'
ok "a block that never offered this PR approval is unrelated" '[ -z "$(decisions)" ]'
reset; on; ask ccc-node 41 a; say "블록이 없는 답입니다."
send '다음 작업 진행'
ok "no block and no PR number: nothing" '[ -z "$(decisions)" ]'

# --- same PR number in two repositories cannot be told apart ---
reset; on; ask ccc-node 41 a; ask a2a-nexus 41 b; say "$ONE"
send '2'
ok "ambiguous PR number is a note on both asks" '[ "$(decisions | sort -u)" = "41:reply-unclassified:ambiguous-pr:reply" ] && [ "$(decisions | wc -l)" = 2 ]'

# --- fail-open ---
reset; on; ask ccc-node 41 a
printf 'not json' >"$TMP/payload.json"; out="$(bash "$HOOK" <"$TMP/payload.json" 2>&1)"; rc=$?
ok "garbage on stdin: exit 0, silent" '[ "$rc" = 0 ] && [ -z "$out" ]'
out="$(bash "$HOOK" </dev/null 2>&1)"; rc=$?
ok "empty stdin: exit 0, silent" '[ "$rc" = 0 ] && [ -z "$out" ]'
TR="$TMP/missing.jsonl" send '41 보류'
ok "missing transcript still classifies what needs no block" '[ "$rc" = 0 ] && [ -z "$out" ] && [ "$(decisions)" = "41:hold:hold-word:reply" ]'
TR="$TMP/transcript.jsonl"
reset; on; ask ccc-node 41 a; say "$ONE"
# An installed copy outside any checkout, with no managed skill around: nothing to import.
mkdir -p "$TMP/installed/hooks/lib"
cp "$HOOK" "$TMP/installed/hooks/"; cp "$HERE/lib/delegated_reply.py" "$TMP/installed/hooks/lib/"
HOOK="$TMP/installed/hooks/delegated-reply.sh" CCC_DELEGATED_RECORDER_DIR="$TMP/nowhere" send '2'
ok "recorder not installed: exit 0, silent, nothing written" '[ "$rc" = 0 ] && [ -z "$out" ] && [ -z "$(decisions)" ]'
printf 'broken {{{\n' >>"$LEDGER"
send '2'
ok "a corrupt ledger line does not stop the hook" '[ "$rc" = 0 ] && [ -z "$out" ] && [ "$(decisions)" = "41:decline:other-option:reply" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
