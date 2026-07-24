#!/usr/bin/env bash
# Feed one Claude hook payload to the canonical bridge lifecycle CLI.
#
# Default-off and fail-open: unless CCC_LIFECYCLE_AUDIT is true this does
# nothing, and a missing bridge venv/module never breaks the parent hook.
# CCC_LIFECYCLE_PYTHON is an explicit operator/test override.
set -uo pipefail

case "${CCC_LIFECYCLE_AUDIT:-}" in
  1|true|TRUE|yes|YES|on|ON) ;;
  *) exit 0 ;;
esac

EVENT="${1:-}"
[ -n "$EVENT" ] || exit 0
input="$(cat 2>/dev/null)"
[ -n "$input" ] || exit 0

HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
python_bin=""
if [ -n "${CCC_LIFECYCLE_PYTHON:-}" ] && [ -x "$CCC_LIFECYCLE_PYTHON" ]; then
  python_bin="$CCC_LIFECYCLE_PYTHON"
else
  # /opt is rewritten by setup.sh on non-canonical checkouts. The relative
  # candidate covers direct use from a source/plugin checkout.
  for candidate in \
    "/opt/ccc-node/bridge/venv/bin/python" \
    "$HERE/../../bridge/venv/bin/python"
  do
    if [ -x "$candidate" ]; then
      python_bin="$candidate"
      break
    fi
  done
fi

if [ -z "$python_bin" ] && command -v python3 >/dev/null 2>&1 \
   && python3 -c 'import telegram_bot.core.lifecycle_hook' >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
fi
[ -n "$python_bin" ] || exit 0

printf '%s' "$input" \
  | "$python_bin" -m telegram_bot.core.lifecycle_hook "$EVENT" >/dev/null 2>&1 \
  || true
exit 0
