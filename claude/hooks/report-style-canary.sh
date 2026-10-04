#!/usr/bin/env bash
# SessionStart: owner-facing report-style canary (#2109 A — Karpathy 2026-10-02,
# "ASD-STE100 at 80%"). Injects the controlled-language rule block as
# additionalContext ONLY on a node whose operator armed the canary flag; every
# other node gets nothing, so the fleet-wide ccc-report style is unchanged until
# the owner's verdict folds the rule in. Body-free, fail-open (always exit 0).
#
# Arm:    touch ~/.claude/state/report-style-canary.flag   (optionally write the
#         KST end time inside; it is echoed back so the model knows the window)
# Disarm: rm ~/.claude/state/report-style-canary.flag
set -uo pipefail

# Distill subprocess guard (see ~/.claude/hooks/distill.sh).
[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

EVENT="${1:-SessionStart}"
case "$EVENT" in SessionStart|PostCompact) ;; *) exit 0 ;; esac

CLAUDE_DIR="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}"
HOOK_DIR="${CCC_HOOK_DIR:-$CLAUDE_DIR/hooks}"
# setup installs the shared reader before the hook tree. All three providers
# use identical bounded reads and Unicode-safe note clipping.
python3 "$HOOK_DIR/ccc_report_style.py" "$CLAUDE_DIR" "$EVENT" 2>/dev/null
exit 0
