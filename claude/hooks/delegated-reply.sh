#!/usr/bin/env bash
# UserPromptSubmit hook — note the owner's non-approval of an open delegated-approval ask
# (ccc-node #2137, spec §10.2 criterion 5-d, slice 3).
#
# The shadow ledger knows when a person approved a PR and, since slice 1, when an agent asked.
# It could not see the owner answering "not this one" in the chat. When an ask is open on this
# node and the reply picks a different option of the same single-choice [답변 필요] block, or says
# hold for that PR, lib/delegated_reply.py appends one `decision` row — the class, the PR and the
# head, never any text.
#
# Contract: OFF unless the node opted in, non-blocking, fail-open (every path exits 0 with no
# output — stdout of this hook would be injected into the turn), and it never writes the prompt.
#   opt in : touch  ~/.claude/state/delegated-approval/reply-hook.on
#   opt out: remove that file (takes effect on the next prompt), or CCC_DELEGATED_SHADOW=0
# With no open ask (marker `open-asks`, kept by the recorder's `ask`) it exits before python.
set -uo pipefail
trap 'exit 0' EXIT
case "${CCC_DELEGATED_SHADOW:-1}" in 0|false|no|off) exit 0 ;; esac
STATE="${CCC_STATE_DIR:-${HOME:-/nonexistent}/.claude/state}/delegated-approval"
[ -e "$STATE/reply-hook.on" ] || exit 0
[ -e "$STATE/open-asks" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
command -v timeout >/dev/null 2>&1 || exit 0
HERE="${BASH_SOURCE[0]%/*}"; [ "$HERE" != "${BASH_SOURCE[0]}" ] || HERE=.
[ -r "$HERE/lib/delegated_reply.py" ] || exit 0
input="$(cat 2>/dev/null)"; [ -n "$input" ] || exit 0
[ ${#input} -le 262144 ] || exit 0
printf '%s' "$input" | timeout "${CCC_DELEGATED_REPLY_TIMEOUT:-3}" python3 "$HERE/lib/delegated_reply.py" >/dev/null 2>&1 || true
exit 0
