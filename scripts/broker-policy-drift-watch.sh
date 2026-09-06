#!/usr/bin/env bash
# Daily broker-policy drift watch (a2a-nexus#2064, hardened per a2a-nexus#2072).
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
# BOTH SIDES come from `${CANON_REF}`, never from the working tree — the
# canonical document AND the detector code that compares it. The first version
# of this script took only the document from the ref and still executed
# `scripts/check-broker-policy.mjs` out of the checkout. On T1 that checkout is
# a detached HEAD with 16 attached agent worktrees whose detector predates the
# `drift` subcommand entirely, so the watch could not have run at all
# (a2a-nexus#2072). Canonical data compared by stale code is not a canonical
# comparison.
#
# Exit: 0 in sync · 1 drift (operator action needed) · 2 cannot determine.
# Anything the detector returns outside 0/1 is normalized to 2: a broken
# deployment must never be indistinguishable from real drift, or the watch
# opens its life crying wolf about the very thing it was built to catch.
set -uo pipefail

LIVE="${BROKER_POLICY_LIVE:-/var/lib/a2a-broker/broker-policy.json}"
CANON_REF="${BROKER_POLICY_CANONICAL_REF:-origin/main}"
CANON_PATH="docs/ops/broker-policy.json"
DETECTOR="scripts/check-broker-policy.mjs"

fail() { echo "broker-policy-drift-watch: $*" >&2; exit 2; }

# A2A_NEXUS_REPO is required and deliberately has no default. The previous
# default (/root/work/a2a-nexus) was wrong on T1: a 430-commit-stale clone that
# nothing referenced sat at that path, while the broker actually ran from
# /root/work/a2a/a2a-nexus. A watch pointed at a decoy checkout reports "in
# sync" about a repository the broker has nothing to do with — worse than no
# watch, because it looks like coverage. Ask the running broker instead of
# guessing.
REPO="${A2A_NEXUS_REPO:-}"
[ -n "$REPO" ] || fail "A2A_NEXUS_REPO is required (no default: the broker's checkout path differs per node).
  Ask the running container which tree it was composed from:
    docker inspect a2a-broker -f '{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}'
  That prints <repo>/packages/broker; pass the <repo> part."

[ -d "$REPO/.git" ] || fail "repo checkout not found: $REPO"
[ -f "$LIVE" ] || fail "live policy document not found: $LIVE (is A2A_BROKER_POLICY_FILE wired on this node?)"

cd "$REPO" || fail "cannot enter $REPO"

# Refresh the canonical ref only. Never touches the working tree, so this is
# safe to run while someone is mid-review on a branch or in a worktree.
git fetch --quiet origin 2>/dev/null || fail "git fetch origin failed"

tmp="$(mktemp -d)" || fail "cannot create temp dir"
trap 'rm -rf "$tmp"' EXIT

git show "${CANON_REF}:${CANON_PATH}" > "$tmp/canonical.json" 2>/dev/null \
  || fail "cannot read ${CANON_REF}:${CANON_PATH}"

# Extract the whole `scripts/` subtree, not just the detector file: it imports
# ./lib/broker-policy-deployment.mjs relatively, so a lone file would fail to
# resolve. `git archive` writes only into $tmp and never touches the checkout.
git archive "$CANON_REF" scripts 2>/dev/null | tar -x -C "$tmp" 2>/dev/null \
  || fail "cannot extract scripts/ from ${CANON_REF}"

[ -f "$tmp/$DETECTOR" ] \
  || fail "${CANON_REF} has no ${DETECTOR} — is ${CANON_REF} really the a2a-nexus canonical ref?"

# Preflight the subcommand. If the canonical detector cannot do `drift`, say so
# as "cannot determine" rather than letting an unknown-subcommand exit status
# be read as a drift verdict.
grep -q "drift" "$tmp/$DETECTOR" \
  || fail "${CANON_REF}:${DETECTOR} does not support the 'drift' subcommand"

node "$tmp/$DETECTOR" drift --live "$LIVE" --canonical "$tmp/canonical.json"
rc=$?

if [ "$rc" -eq 1 ]; then
  echo ""
  echo "Canonical side was ${CANON_REF}:${CANON_PATH} (not the working tree),"
  echo "compared by ${CANON_REF}:${DETECTOR} (also not the working tree)."
  echo "Decide which side is correct — this watch does not."
  echo "  repo is right  -> node ${DETECTOR} sync --apply, then restart the broker"
  echo "  live is right  -> commit the live value to ${CANON_PATH} (operator commit, §2.4)"
  echo ""
  echo "After any restart, confirm the container actually restarted:"
  echo "  docker ps --format '{{.Names}} {{.Status}}'   # expect 'Up N seconds', not hours"
  echo "  'docker compose up -d' does NOT restart on a policy-file-only change —"
  echo "  the file lives in a mounted volume and is invisible to compose's config diff."
elif [ "$rc" -ne 0 ]; then
  echo "broker-policy-drift-watch: detector exited $rc (neither in-sync nor drift); reporting as cannot-determine" >&2
  rc=2
fi

exit "$rc"
