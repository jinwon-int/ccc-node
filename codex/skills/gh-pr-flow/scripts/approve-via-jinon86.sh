#!/usr/bin/env bash
# Convenience wrapper for seoseo-ai-authored PRs reviewed by jinon86.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$script_dir/approve-via-seoseo.sh" \
  --review-profile jinon86 "$@"
