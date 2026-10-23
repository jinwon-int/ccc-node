#!/usr/bin/env bash
# A2A Terminal Brief -> Matrix fleet alerts consumer (ccc-node#2498): hermetic unit tests.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$HERE/a2a_terminal_brief_fleet_notify_test.py" -q
echo "a2a-terminal-brief-fleet-notify: ok"
