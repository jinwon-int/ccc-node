#!/usr/bin/env bash
# Tests for skill-review/description_trigger.py — the single ccc-node copy of
# the fleet-skills validate.py TRIGGER_RE (fleet-skills#315/#316). Hermetic;
# the optional parity check reads a local fleet-skills checkout only when
# CCC_FLEET_SKILLS_VALIDATE points at its scripts/validate.py.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL="$HERE/description_trigger.py"
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

check() { printf '%s' "$1" | python3 "$TOOL" check >/dev/null 2>&1; }

# Accepted: every alternative of the pattern.
ok "leading When" 'check "When a release checklist must be re-verified, walk it."'
ok "leading Before" 'check "Before tagging a release, re-verify the checklist."'
ok "leading After (case-insensitive)" 'check "after a failed deploy, collect the evidence bundle."'
ok "Use when" 'check "Use when a release checklist must be re-verified before tagging."'
ok "Use this skill for" 'check "Checklist walker. Use this skill for release verification."'
ok "Invoke it if" 'check "Evidence collector; invoke it if a deploy fails."'
ok "Triggers on" 'check "Release checklist walker. Triggers on tag creation requests."'
ok "Trigger:" 'check "Release checklist walker. Trigger: a tag is about to be created."'
ok "When to use" 'check "Release checklist walker (see When to use)."'
ok "Korean 할 때 사용" 'check "릴리스 체크리스트를 다시 검증할 때 사용. 단계별로 확인한다."'
ok "Korean 시 적용" 'check "릴리스 태그 생성 시 적용하는 체크리스트 검증 절차."'
ok "Korean 시에 호출" 'check "배포 실패 시에 호출하는 증거 수집 절차."'
ok "surrounding quotes are ignored" 'check "\"Use when a release checklist must be re-verified.\""'

# Rejected: what-only descriptions and the documented false friends.
ok "what-only description" '! check "Walk the recurring release checklist and record the output."'
ok "mid-sentence used when is not a trigger" '! check "Release checklist procedure used when tagging goes wrong."'
ok "Korean 때문 is not a trigger" '! check "설정이 바뀌었기 때문에 체크리스트를 다시 검증하는 절차."'
ok "Korean 때때로 is not a trigger" '! check "때때로 필요한 체크리스트 검증 절차를 정리한다."'
ok "empty description" '! check ""'

# CLI contract: 0 pass, 1 missing, 2 usage; non-UTF-8 input is "missing".
printf 'Use when x happens' | python3 "$TOOL" >/dev/null 2>&1; rc=$?
ok "missing verb is a usage error (rc 2)" '[ "$rc" = 2 ]'
printf 'Walk the checklist' | python3 "$TOOL" check >/dev/null 2>&1; rc=$?
ok "missing trigger exits 1" '[ "$rc" = 1 ]'
printf '\377\376 Use when' | python3 "$TOOL" check >/dev/null 2>&1
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "non-UTF-8 input fails as missing (rc 1)" '[ "$rc" = 1 ]'

# Optional drift check against fleet-skills (source of truth).
if [ -n "${CCC_FLEET_SKILLS_VALIDATE:-}" ] && [ -r "$CCC_FLEET_SKILLS_VALIDATE" ]; then
  ok "TRIGGER_RE matches fleet-skills validate.py verbatim" \
    'python3 - "$TOOL" "$CCC_FLEET_SKILLS_VALIDATE" <<"PY"
import importlib.util, re, sys
spec = importlib.util.spec_from_file_location("dt", sys.argv[1])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
src = open(sys.argv[2], encoding="utf-8").read()
start = src.index("TRIGGER_RE = re.compile(")
end = src.index(")\n", src.index("re.I", start)) + 2
ns = {"re": re}
exec(src[start:end], ns)
sys.exit(0 if (ns["TRIGGER_RE"].pattern, ns["TRIGGER_RE"].flags) == (mod.TRIGGER_RE.pattern, mod.TRIGGER_RE.flags) else 1)
PY'
fi

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
