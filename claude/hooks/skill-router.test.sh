#!/usr/bin/env bash
# Tests for skill-router.sh / lib/skill_router.py (ccc-node #2645) — the
# UserPromptSubmit routing hook, the routability gate and the measurement
# report. Hermetic: a synthetic skills root under a private CCC_CLAUDE_DIR,
# private state, no network. The hook's fail-open contract (always exit 0,
# silent when unsure) is asserted directly.
# shellcheck disable=SC2034  # out / rc / before are read via eval inside ok()
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/skill-router.sh"
MOD="$HERE/lib/skill_router.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export HOME="$TMP/home"
export CCC_CLAUDE_DIR="$HOME/.claude"
export CODEX_HOME="$HOME/.codex"
export CCC_STATE_DIR="$CCC_CLAUDE_DIR/state"
export CCC_SKILL_LISTING_CORE="$TMP/core.txt"
unset CCC_SKILL_ROUTER
SKILLS="$CCC_CLAUDE_DIR/skills"; mkdir -p "$SKILLS" "$CODEX_HOME/skills" "$CCC_STATE_DIR"
chmod 700 "$CCC_CLAUDE_DIR"

skill() { # <root> <name> <description>
  mkdir -p "$1/$2"
  printf -- '---\nname: %s\ndescription: %s\n---\n\n# %s\n\nbody\n' "$2" "$3" "$2" > "$1/$2/SKILL.md"
}
skill "$SKILLS" bridge-restart-detached "Use when restarting the telegram bridge or gateway service from inside its own session: detach restart and verification into a systemd transient unit first."
skill "$SKILLS" wiki-operating-log "Use when a durable operating log entry, decision or runbook must be recorded in the family wiki through the PR-first write-path flow."
skill "$SKILLS" hwpx-report-build "Use when producing a Korean hwpx report document that must inherit an institutional form template without Hangul Office installed."
skill "$SKILLS" merge-queue-landing "Use when a reviewed pull request must be enqueued into the GitHub merge queue and its landing confirmed after CI checks pass."
skill "$SKILLS" firewall-port-audit "Use when auditing ufw firewall allow rules and listening ports on a node after a CVE advisory or before exposing a service."
skill "$SKILLS" core-pr-flow "Use when committing, pushing, opening or merging a pull request in fleet repositories: exact-head checks, cross-account review, squash merge."
skill "$CODEX_HOME/skills" codex-only-probe "Use when probing codex runtime skill roots for the router index test: unique zebra quokka tokens."
printf '# core set\ncore-pr-flow\n' > "$CCC_SKILL_LISTING_CORE"
# Not a skill: no frontmatter name match / status off / symlink / oversize.
mkdir -p "$SKILLS/mismatch"; printf -- '---\nname: other\ndescription: Use when x.\n---\n' > "$SKILLS/mismatch/SKILL.md"
mkdir -p "$SKILLS/retired"; printf -- '---\nname: retired\ndescription: Use when retired skill tokens appear in a prompt about retired things.\nstatus: deprecated\n---\n' > "$SKILLS/retired/SKILL.md"
ln -s "$SKILLS/wiki-operating-log" "$SKILLS/linked"

# shellcheck disable=SC2034  # out/rc are read via eval inside ok()
run_hook() { # run_hook <prompt> [session] -> $out $rc
  local payload
  payload="$(jq -nc --arg p "$1" --arg s "${2:-}" '{prompt:$p} + (if $s != "" then {session_id:$s} else {} end)')"
  out="$(printf '%s' "$payload" | bash "$HOOK" 2>/dev/null)"; rc=$?
}
ctx() { jq -r '.hookSpecificOutput.additionalContext' <<<"$out"; }
names() { ctx | grep -oE '^[0-9]+\. `[a-z0-9-]+`' | sed -E 's/.*`([a-z0-9-]+)`/\1/'; }

# ---- index ------------------------------------------------------------------
out="$(python3 "$MOD" index --rebuild --json)"
ok "index scans claude + codex roots and skips mismatched/deprecated/symlinked entries" \
  'jq -e ".skills == 7 and .by_runtime.claude == 6 and .by_runtime.codex == 1 and .core_excluded == 1" <<<"$out" >/dev/null'
ok "index cache is owner-only" '[ "$(stat -c %a "$CCC_STATE_DIR/skill-router/index.json")" = 600 ]'
# shellcheck disable=SC2034  # before is read via eval inside ok()
before="$(jq -r .built_at "$CCC_STATE_DIR/skill-router/index.json")"
python3 "$MOD" index --json >/dev/null
ok "unchanged roots reuse the cached index" '[ "$(jq -r .built_at "$CCC_STATE_DIR/skill-router/index.json")" = "$before" ]'
skill "$SKILLS" late-arrival "Use when a skill installed after the index was built must still be routable without a manual rebuild: pelican tokens."
sleep 1; touch "$SKILLS"
out="$(python3 "$MOD" index --json)"
ok "a changed root listing rebuilds the index" 'jq -e ".skills == 8" <<<"$out" >/dev/null'

# ---- hook: suggestions ----------------------------------------------------------
run_hook "브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘" sess-1
ok "a Korean ops prompt routes through the synonym map to the matching skill" \
  '[ "$rc" = 0 ] && [ "$(names | head -1)" = bridge-restart-detached ]'
ok "output is a UserPromptSubmit additionalContext record" \
  'jq -e ".hookSpecificOutput.hookEventName == \"UserPromptSubmit\"" <<<"$out" >/dev/null'
ok "the injected text never echoes the prompt" '! grep -q "돌려서" <<<"$out"'
ok "the injected text stays within budget" '[ "$(ctx | wc -c)" -le 760 ]'
ok "suggestion ledger row is body-free (names + scores + session only)" \
  'jq -e ".session == \"sess-1\" and (.candidates[0].name == \"bridge-restart-detached\") and (keys == [\"candidates\",\"session\",\"ts\"])" "$CCC_STATE_DIR/skill-router/suggestions.jsonl" >/dev/null'
ok "last-<session> file lists the candidate names" \
  'jq -e ".candidates | index(\"bridge-restart-detached\") != null" "$CCC_STATE_DIR/skill-router/last-sess-1.json" >/dev/null'
ok "ledger files are owner-only" '[ "$(stat -c %a "$CCC_STATE_DIR/skill-router/suggestions.jsonl")" = 600 ] && [ "$(stat -c %a "$CCC_STATE_DIR/skill-router/last-sess-1.json")" = 600 ]'

run_hook "hwpx 한글 보고서를 기관 양식 그대로 만들어줘" sess-2
ok "English identifiers inside a Korean prompt route too" '[ "$(names | head -1)" = hwpx-report-build ]'

run_hook "please commit and push, open the pull request and squash merge it after the cross-account review" sess-3
ok "a prompt best served by a core skill yields no suggestion (its description is already listed)" '[ -z "$out" ]'
run_hook "enqueue the reviewed pull request into the merge queue and confirm the landing after the checks pass" sess-4
ok "a non-core skill comparable to the core winner is still suggested" 'grep -q merge-queue-landing <<<"$(names)"'
ok "the core skill itself is never suggested" '! grep -q core-pr-flow <<<"$(names)"'

# ---- hook: silence ------------------------------------------------------------------
for p in "1" "2637 승인" "1. 승인" "1 2 3" "2637 2638 approve" "ok" "000000000000000000000000000000!" "/skillsuggest" "고마워 잘했다" "응 그렇게 해"; do
  run_hook "$p" sess-5
  ok "no suggestion for '$p' (short / approval / slash / no signal)" '[ "$rc" = 0 ] && [ -z "$out" ]'
done
# #2652: copied option replies — approvals with an explanation tail, and any
# reply that starts with an option marker — never get candidates (the words
# are the assistant's own option text, routing on them is an echo).
for p in "1. 2651 승인 (권장)" "2637 2638 승인 — 두 PR 모두 리뷰" "2651 승인 (recommended option)" \
         "1. 브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 — 권장안" "2) 브리지 재시작 전에 분리 유닛으로 돌려서 검증" "3 . 지금 상태로 브리지 재시작 분리 유닛 검증"; do
  run_hook "$p" sess-5
  ok "#2652: no suggestion for copied option reply '$p'" '[ "$rc" = 0 ] && [ -z "$out" ]'
done
# Recommended options are marked up front now ("1. (권장) …", several allowed).
# A pasted option in that form, with or without its number, never routes; a
# real request behind the note still does. (Guards the new convention: the
# numbered form is a choice echo, the bare form carries no skill words.)
for p in "1. (권장) 2651 승인" "(권장) 2651 승인 — 두 PR 모두 리뷰" "(권장) 2637 2638 승인" "2. (권장) 2652 승인 (recommended option)"; do
  run_hook "$p" sess-5
  ok "prefix (권장): no suggestion for copied option reply '$p'" '[ "$rc" = 0 ] && [ -z "$out" ]'
done
run_hook "(권장) 브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘" sess-11
ok "prefix (권장): a real request behind the note still routes" '[ "$(names | head -1)" = bridge-restart-detached ]'
run_hook "2026 브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘" sess-9
ok "#2652: a request that merely starts with a number still routes" '[ "$(names | head -1)" = bridge-restart-detached ]'
ok "silent prompts leave no last-session file" '[ ! -e "$CCC_STATE_DIR/skill-router/last-sess-5.json" ]'
# #2658: the hook only offers skills the calling runtime can invoke. It is
# registered in Claude Code's hooks, so the default runtime is claude.
run_hook "tell me about zebra quokka probing in the codex runtime roots" sess-6
ok "#2658: a codex-only skill is never offered to the claude runtime (default)" '[ "$rc" = 0 ] && [ -z "$out" ]'
CCC_SKILL_ROUTER_RUNTIME=codex run_hook "tell me about zebra quokka probing in the codex runtime roots" sess-6
ok "#2658: CCC_SKILL_ROUTER_RUNTIME=codex routes to codex-root skills" '[ "$(names | head -1)" = codex-only-probe ]'
CCC_SKILL_ROUTER_RUNTIME=codex run_hook "브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘" sess-6
ok "#2658: a claude-only skill is never offered to the codex runtime" '! grep -q bridge-restart-detached <<<"$(names)"'
CCC_SKILL_ROUTER_RUNTIME=piri run_hook "브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘" sess-6
ok "#2658: an unknown runtime is silent rather than wrong" '[ "$rc" = 0 ] && [ -z "$out" ]'
skill "$CODEX_HOME/skills" hwpx-report-build "Use when producing a Korean hwpx report document that must inherit an institutional form template without Hangul Office installed."
sleep 1; touch "$CODEX_HOME/skills"
out="$(python3 "$MOD" index --json)"
ok "#2658: a name in both roots stays one index document listing both runtimes" \
  'jq -e ".skills == 8 and .by_runtime.claude == 7 and .by_runtime.codex == 1" <<<"$out" >/dev/null && jq -e "[.docs[] | select(.name == \"hwpx-report-build\") | .runtimes] == [[\"claude\",\"codex\"]]" "$CCC_STATE_DIR/skill-router/index.json" >/dev/null'
run_hook "hwpx 한글 보고서를 기관 양식 그대로 만들어줘" sess-6
ok "#2658: a skill in both roots routes for claude" '[ "$(names | head -1)" = hwpx-report-build ]'
CCC_SKILL_ROUTER_RUNTIME=codex run_hook "hwpx 한글 보고서를 기관 양식 그대로 만들어줘" sess-6
ok "#2658: … and for codex" '[ "$(names | head -1)" = hwpx-report-build ]'
EXTRA="$TMP/extra-skills"; skill "$EXTRA" osprey-ledger-audit "Use when auditing the osprey ledger rollups for drift between nightly snapshots and the live heron counters."
CCC_SKILL_ROUTER_EXTRA_ROOTS="$EXTRA" run_hook "audit the osprey ledger rollups for drift against the heron counters" sess-6
ok "#2658: operator extra roots are offered to the claude runtime" '[ "$(names | head -1)" = osprey-ledger-audit ]'

CCC_SKILL_ROUTER=0 run_hook "브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘" sess-7
ok "CCC_SKILL_ROUTER=0 silences the hook" '[ "$rc" = 0 ] && [ -z "$out" ]'
: > "$CCC_STATE_DIR/skill-router.off"
run_hook "브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘" sess-8
ok "state/skill-router.off silences the hook" '[ -z "$out" ]'
rm -f "$CCC_STATE_DIR/skill-router.off"

out="$(printf 'not json' | bash "$HOOK" 2>/dev/null)"; rc=$?
ok "malformed hook input fails open" '[ "$rc" = 0 ] && [ -z "$out" ]'
out="$(printf '' | bash "$HOOK" 2>/dev/null)"; rc=$?
ok "empty input fails open" '[ "$rc" = 0 ] && [ -z "$out" ]'
# Here-string, not a pipe: the hook exits before reading stdin when python3 is
# missing, and a producer process would die of SIGPIPE (rc 141 under pipefail).
payload="$(jq -nc '{prompt:"브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘"}')"
out="$(PATH="$TMP/nopy" /bin/bash "$HOOK" <<<"$payload" 2>/dev/null)"; rc=$?
ok "missing python3 fails open" '[ "$rc" = 0 ] && [ -z "$out" ]'

# ---- check-description (autoinstall gate) ----------------------------------------------
out="$(python3 "$MOD" check-description --name copycat --description "Use when a durable operating log entry, decision or runbook must be recorded in the family wiki through the PR-first write-path flow." --json)"; rc=$?
ok "a description copied from an installed skill is shadowed (rc 1)" \
  '[ "$rc" = 1 ] && jq -e ".ok == false and .reason == \"shadowed-by-installed-skill\" and .best_other.name == \"wiki-operating-log\"" <<<"$out" >/dev/null'
out="$(python3 "$MOD" check-description --name matrix-room-mode-switch --description "Use when a Matrix room must move between alert and agent modes and the sender bots need a coordinated restart afterwards." --json)"; rc=$?
ok "a distinctive description passes (rc 0)" '[ "$rc" = 0 ] && jq -e ".ok == true and .distinct_tokens >= 6" <<<"$out" >/dev/null'
out="$(python3 "$MOD" check-description --name tiny --description "Use when deploying." 2>&1)"; rc=$?
ok "a description with too few content tokens fails" '[ "$rc" = 1 ] && grep -q "too-few-content-tokens" <<<"$out"'
out="$(python3 "$MOD" check-description --name x 2>&1)"; rc=$?
ok "check-description without a description is a usage error (rc 2)" '[ "$rc" = 2 ]'
# shellcheck disable=SC2034  # rc is read via eval inside ok()
out="$(python3 "$MOD" check-description --name wiki-operating-log --description "Use when a durable operating log entry, decision or runbook must be recorded in the family wiki through the PR-first write-path flow." --json)"; rc=$?
ok "re-checking an installed skill against itself is not a self-shadow" '[ "$rc" = 0 ]'

# ---- report -------------------------------------------------------------------------------
USAGE="$CCC_STATE_DIR/skill-usage"; mkdir -p "$USAGE"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"ts":"%s","skill":"bridge-restart-detached","tool":"Skill","runtime":"claude","suggested":true}\n{"ts":"%s","skill":"hwpx-report-build","tool":"Skill","runtime":"claude","suggested":false}\n{"ts":"%s","skill":"core-pr-flow","tool":"Skill","runtime":"claude"}\n{"ts":"%s","skill":"wiki-operating-log","tool":"Read","runtime":"claude","suggested":true}\n{"ts":"2020-01-01T00:00:00Z","skill":"ancient","tool":"Skill","runtime":"claude","suggested":true}\n' "$now" "$now" "$now" "$now" > "$USAGE/usage.jsonl"
out="$(python3 "$MOD" report --days 7 --json)"
ok "report counts prompts with suggestions in the window" 'jq -e ".prompts_with_suggestions >= 3" <<<"$out" >/dev/null'
ok "report hit rate uses tagged Skill invocations only (1 of 2 tagged → 0.5), Read rows and untagged rows excluded" \
  'jq -e ".skill_invocations == 3 and .invocations_tagged == 2 and .invocations_following_a_suggestion == 1 and .hit_rate == 0.5" <<<"$out" >/dev/null'
ok "report lists suggested-never-used and used-never-suggested" \
  'jq -e "(.suggested_never_used | map(.[0]) | index(\"merge-queue-landing\") != null) and (.used_never_suggested | map(.[0]) | index(\"core-pr-flow\") != null)" <<<"$out" >/dev/null'
out="$(python3 "$MOD" report --days 7)"
ok "text report renders the headline lines" 'grep -q "followed a suggestion" <<<"$out" && grep -q "prompts with suggestions" <<<"$out"'

# ---- #2748: match precision ------------------------------------------------------------
# Synthetic fixtures only. A four-member family (relay/sync/mirror are generic:
# in more than GENERIC_DF_FLOOR skills) and one skill that a single Korean
# word used to reach through four synonym targets.
skill "$SKILLS" relay-sync-kestrel-rotate "Use when the relay sync mirror for the kestrel shard needs a rotate pass before the nightly window."
skill "$SKILLS" relay-sync-puffin-prune "Use when the relay sync mirror for the puffin shard needs a prune pass before the nightly window."
skill "$SKILLS" relay-sync-gannet-replay "Use when the relay sync mirror for the gannet shard needs a replay pass before the nightly window."
skill "$SKILLS" relay-sync-curlew-export "Use when the relay sync mirror for the curlew shard needs an export pass before the nightly window."
skill "$SKILLS" counter-audit-check "Use when somebody must check and verify the tally counters, audit the totals and diagnose any gap."
sleep 1; touch "$SKILLS"
python3 "$MOD" index --json >/dev/null
filler() { # filler <n> -> n distinct letter-only words no skill mentions
  python3 -c 'import sys; print(" ".join("zq" + chr(97 + i // 26) + chr(97 + i % 26) + "x" for i in range(int(sys.argv[1]))))' "$1"
}

run_hook "그거 한번 점검 좀 해줘 부탁해" sess-p1
ok "#2748: one prompt word is one concept — a synonym line with four targets cannot match alone" '[ "$rc" = 0 ] && [ -z "$out" ]'
run_hook "relay sync mirror 쪽 좀 봐줘 지금" sess-p1
ok "#2748: family (generic) tokens alone are not evidence" '[ -z "$out" ]'
run_hook "kestrel 쪽 relay sync rotate 한번 돌려줘" sess-p2
ok "#2748: two distinctive name words pick the one family member, not its siblings" '[ "$(names | tr "\n" " ")" = "relay-sync-kestrel-rotate " ]'
run_hook "kestrel 쪽 relay sync 좀 봐줘 지금" sess-p1
ok "#2748: one distinctive name word plus family words is not enough" '[ -z "$out" ]'

run_hook "cve advisory 나온 뒤에 ufw allow rules 정리해줘" sess-p3
ok "#2748: a focused prompt in the description's own words still routes without name words" '[ "$(names | head -1)" = firewall-port-audit ]'
run_hook "cve advisory 나온 뒤에 ufw allow rules 정리해줘 $(filler 60)" sess-p1
ok "#2748: the same few description words inside a pasted block do not" '[ -z "$out" ]'
run_hook "텔레그램 브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘 $(filler 60)" sess-p4
ok "#2748: a name-anchored request is still found in a long prompt (coverage window)" '[ "$(names | head -1)" = bridge-restart-detached ]'
run_hook "텔레그램 브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘 $(filler 600)" sess-p1
ok "#2748: a prompt over MAX_PROMPT_CHARS is a pasted document — skipped" '[ "$rc" = 0 ] && [ -z "$out" ]'

run_hook "브리지 재시작 지금 바로 해줘" sess-p5
ok "#2748: a two-word request routes" '[ "$(names | head -1)" = bridge-restart-detached ]'
run_hook "브리지 재시작은 누가 하는 거야?" sess-p1
ok "#2748: the same two words in a pure question do not" '[ -z "$out" ]'
run_hook "브리지 재시작 지금 할 수 있을까?" sess-p6
ok "#2748: a request in question form (…할 수 있을까?) still routes" '[ "$(names | head -1)" = bridge-restart-detached ]'
run_hook "브리지 재시작은 분리 유닛으로 하는 거야?" sess-p7
ok "#2748: a pure question with one more shared concept routes" '[ "$(names | head -1)" = bridge-restart-detached ]'

for p in "Reply with the single word PONG." "Name two tools you can call." "Answer in one line: which bridge restart unit are you?" "respond with exactly: bridge restart detached"; do
  run_hook "$p" sess-p1
  ok "#2748: no suggestion for probe '$p'" '[ "$rc" = 0 ] && [ -z "$out" ]'
done
ok "#2748: silent precision cases leave no last-session file" '[ ! -e "$CCC_STATE_DIR/skill-router/last-sess-p1.json" ]'

# Unit level: surface matching, concepts, the pruned synonym lines, skip reasons.
out="$(python3 - "$MOD" <<'PY' 2>&1
import importlib.util, sys
spec = importlib.util.spec_from_file_location("skill_router", sys.argv[1])
R = importlib.util.module_from_spec(spec); spec.loader.exec_module(R)
syn = R.load_synonyms()
bad = []
def check(label, cond):
    if not cond:
        bad.append(label)
def toks(text):
    return set(R.prompt_concepts(text, syn)[0])
# ASCII surfaces are whole tokens.
check("pr inside provider", not R.surface_matches("the provider approval probe", "pr"))
check("ci inside decision", not R.surface_matches("an explicit decision", "ci"))
check("pr as a token", R.surface_matches("open a pr now", "pr") and R.surface_matches("pr-first flow", "pr"))
check("no pull/request from english prose", not {"pull", "request"} & toks("the provider procedure is explicit about approval"))
# Short Korean surfaces start a word; longer ones match anywhere.
check("머지 inside 나머지", not R.surface_matches("나머지는 내일 하자", "머지"))
check("워드 inside 키워드", not R.surface_matches("검색 키워드 목록", "워드"))
check("머지 with an ending", R.surface_matches("바로 머지해줘", "머지") and R.surface_matches("검토·머지", "머지"))
check("long surface inside a compound", R.surface_matches("팀파일보관함에 올려줘", "파일보관함"))
# One surface is one concept; a phrase still counts per word.
tokens, concept_of = R.prompt_concepts("테스트 다시 돌려줘", syn)
check("테스트 is one concept", {"test", "tests"} <= set(tokens) and concept_of["test"] == concept_of["tests"])
tokens, concept_of = R.prompt_concepts("머지 큐에 넣어줘", syn)
check("머지 큐 is two concepts", concept_of["merge"] != concept_of["queue"])
# Pruned lines: a common word no longer reaches a domain it does not name.
check("문제 is not a quiz item", not {"question", "nclex", "item"} & toks("빌드가 느려지는 문제 좀 봐줘"))
check("감사합니다 is not an audit", "audit" not in toks("감사합니다 덕분에 잘 끝났어요"))
check("체크박스 is not ci checks", not {"checks", "ci"} & toks("설정 화면에 체크박스 하나 추가해줘"))
check("키우다/키워드 is not a credential", not {"key", "credential"} & toks("아이콘을 키우고 검색 키워드 칸도 넣어줘"))
check("승인 is not review", "review" not in toks("이번 변경은 승인해도 돼"))
check("브리지 is not telegram", "telegram" not in toks("브리지 로그 좀 보여줘"))
check("한글 alone is not hwp", not {"hwp", "hwpx"} & toks("한글로 답해줘") and {"hwp", "hwpx"} <= toks("한글 파일로 만들어줘"))
# Numbers and ids are not content words; generic needs a real family.
ranker = R.Ranker(R.load_index().get("docs") or [])
check("numbers are noise", ranker.is_noise("2026") and ranker.is_noise("10.14") and ranker.is_noise("913e077e"))
check("words are not noise", not ranker.is_noise("ufw") and not ranker.is_noise("unknownword"))
check("family tokens are generic", ranker.is_generic("relay") and ranker.is_generic("sync"))
check("member tokens are not", not ranker.is_generic("kestrel") and not ranker.is_generic("bridge"))
# Questions and probes.
check("pure question (ko)", R.is_pure_question("백업은 매일 밤에 도는 거야? 아니면 주말에만 돌아?"))
check("pure question (en)", R.is_pure_question("Does the nightly backup run on every node?"))
check("question + request", not R.is_pure_question("로그가 제일 큰 노드가 어디야? 거기부터 정리하자"))
check("proposal question", not R.is_pure_question("다음부터 자동으로 알려줄 방법 없을까?"))
check("can-you question", not R.is_pure_question("Can you restart the bridge detached?"))
check("statement", not R.is_pure_question("run.py 를 v1.2 로 올려줘"))
check("probe", R._skip_reason("Reply with exactly: pong") == "probe" and R._skip_reason("List two tools you can call.") == "probe")
check("korean request is not a probe", R._skip_reason("브리지 재시작 지금 바로 해줘") is None)
check("long", R._skip_reason("브리지 재시작 " + "가" * R.MAX_PROMPT_CHARS) == "long")
print("bad: " + "; ".join(bad) if bad else "unit-ok")
PY
)"
ok "#2748: surface matching, concept grouping, pruned synonyms, noise/generic, question and probe rules ($out)" '[ "$out" = unit-ok ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
