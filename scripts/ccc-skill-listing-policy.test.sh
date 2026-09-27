#!/usr/bin/env bash
# harness: umask-rerun
# Hermetic tests for the skill-listing budget policy (#2011 A). Every case uses
# a private temp CCC_CLAUDE_DIR; the live ~/.claude is never touched.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT/scripts/ccc_skill_listing_policy_test.py"
