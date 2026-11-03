#!/usr/bin/env bash
# UserPromptSubmit hook — prompt-time skill routing (ccc-node #2645).
#
# Scores the submitted prompt against the installed skills' name+description
# (BM25 over a cached index, Korean→English synonyms) and injects up to three
# candidate skills as additionalContext. Push-side complement to the pull-only
# `family-skills` search: the model still decides, nothing is auto-invoked.
#
# Contract: non-blocking, non-mutating, fail-open (every error path exits 0
# with no output), body-free ledgers (the prompt text is never written).
# Kill switch: CCC_SKILL_ROUTER=0 or ~/.claude/state/skill-router.off.
set -uo pipefail
trap 'exit 0' EXIT
case "${CCC_SKILL_ROUTER:-1}" in 0|false|no|off) exit 0 ;; esac
# Builtins only until python3 is known to exist: this hook runs on every
# prompt and must stay silent on a node without the interpreter or coreutils.
command -v python3 >/dev/null 2>&1 || exit 0
command -v timeout >/dev/null 2>&1 || exit 0
HERE="${BASH_SOURCE[0]%/*}"; [ "$HERE" != "${BASH_SOURCE[0]}" ] || HERE=.
[ -r "$HERE/lib/skill_router.py" ] || exit 0
input="$(cat 2>/dev/null)"; [ -n "$input" ] || exit 0
[ ${#input} -le 65536 ] || exit 0
# A slow disk or a huge roots scan must never hold the turn: bounded wall clock.
printf '%s' "$input" | timeout "${CCC_SKILL_ROUTER_TIMEOUT:-4}" python3 "$HERE/lib/skill_router.py" suggest 2>/dev/null || true
exit 0
