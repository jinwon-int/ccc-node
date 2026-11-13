#!/usr/bin/env bash
# Hook-test wrapper (discovered by scripts/validate-harness.sh) for the
# hermetic code_graph_test.py unittest suite (ccc-node#2707): temp git repos,
# private cache root, no network.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$DIR/code_graph_test.py"
