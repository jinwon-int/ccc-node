#!/usr/bin/env bash
# Daily broker-policy drift watch (a2a-nexus#2064).
#
# Why this exists: from 2026-07-22 to 2026-09-06 the live broker policy said
# `enforce` while the operator-committed document in the repo said `warn`, and
# nothing noticed for two months. a2a-nexus#2067 added the detector; this
# wrapper is what actually runs it, because a gate nobody executes is not a
# gate. Contract §4.1 asks for exactly this.
#
# Read-only by design: it reports drift and exits non-zero. It never repairs.
# `contracts/a2a/broker-policy.md` §2.4 reserves policy changes for operator
# commits, so a self-correcting watcher would be an agent editing policy.
#
# Canonical side is read from `origin/main`, NOT the working tree: this node's
# checkout is a developer workspace that may sit on a feature branch or a
# worktree mid-review, and comparing against that would produce false drift.
#
# Exit: 0 in sync · 1 drift (operator action needed) · 2 cannot determine.
set -uo pipefail

REPO="${A2A_NEXUS_REPO:-/root/work/a2a-nexus}"
LIVE="${BROKER_POLICY_LIVE:-/var/lib/a2a-broker/broker-policy.json}"
CANON_REF="${BROKER_POLICY_CANONICAL_REF:-origin/main}"
CANON_PATH="docs/ops/broker-policy.json"

fail() { echo "broker-policy-drift-watch: $*" >&2; exit 2; }

[ -d "$REPO/.git" ] || fail "repo checkout not found: $REPO"
[ -f "$LIVE" ] || fail "live policy document not found: $LIVE (is A2A_BROKER_POLICY_FILE wired on this node?)"

cd "$REPO" || fail "cannot enter $REPO"

# Refresh the canonical ref only. Never touches the working tree, so this is
# safe to run while someone is mid-review on a branch or in a worktree.
git fetch --quiet origin 2>/dev/null || fail "git fetch origin failed"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
git show "${CANON_REF}:${CANON_PATH}" > "$tmp" 2>/dev/null \
  || fail "cannot read ${CANON_REF}:${CANON_PATH}"

node scripts/check-broker-policy.mjs drift --live "$LIVE" --canonical "$tmp"
rc=$?

if [ "$rc" -eq 1 ]; then
  echo ""
  echo "Canonical side was ${CANON_REF}:${CANON_PATH} (not the working tree)."
  echo "Decide which side is correct — this watch does not."
  echo "  repo is right  -> node scripts/check-broker-policy.mjs sync --apply, then restart the broker"
  echo "  live is right  -> commit the live value to ${CANON_PATH} (operator commit, §2.4)"
  echo ""
  echo "After any restart, confirm the container actually restarted:"
  echo "  docker ps --format '{{.Names}} {{.Status}}'   # expect 'Up N seconds', not hours"
  echo "  'docker compose up -d' does NOT restart on a policy-file-only change —"
  echo "  the file lives in a mounted volume and is invisible to compose's config diff."
fi

exit "$rc"
