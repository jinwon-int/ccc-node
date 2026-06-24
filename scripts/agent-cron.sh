#!/usr/bin/env bash
# agent-cron — compatibility wrapper for the Python implementation.
#
# Existing docs, slash commands, and systemd units may still call this shell
# path. The implementation lives in scripts/agent_cron.py so future work can
# evolve the scheduler in Python while preserving the historical CLI contract.
set -euo pipefail
SCRIPT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$SCRIPT_ROOT/scripts/agent_cron.py" "$@"
