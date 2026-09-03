#!/usr/bin/env bash
# Backward-compatible seoseo-ai profile wrapper.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$script_dir/approve-via-relay.sh" \
  --review-profile seoseo-ai "$@"
