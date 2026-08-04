#!/usr/bin/env bash
# ccc-crush-headless — ephemeral, non-interactive Crush runner for agent-cron.
#
# Crush (charmbracelet) is the third ccc-node harness, designated for the
# Kimi-K3 and GLM-5.2 nodes (issue #923). This runner mirrors the
# codex/headless.sh contract: fail-closed env validation, a wall-clock cap,
# and bounded output.
#
# Permission model: Crush takes tool permissions from crushrc, not from CLI
# flags, so this runner pins the global config to a fleet-managed file
# (default: crush/crushrc.readonly in this repo) via CRUSH_GLOBAL_CONFIG.
# Operators opt into broader access by pointing CCC_CRUSH_CONFIG at a
# reviewed config. There is deliberately no yolo path here.
set -uo pipefail

PROMPT="${1:-}"
if [ -z "$PROMPT" ]; then
  echo "usage: ccc-crush-headless <prompt>" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${CCC_CRUSH_BIN:-crush}"
MODEL="${CCC_CRUSH_MODEL:-${CCC_MODEL:-}}"
WORKDIR="${CCC_CRUSH_WORKDIR:-$PWD}"
CONFIG="${CCC_CRUSH_CONFIG:-$HERE/crushrc.readonly}"
# Wall-clock cap in seconds (0 disables). Guards against a run that never returns.
TMO="${CCC_HEADLESS_TIMEOUT:-1500}"

command -v "$BIN" >/dev/null 2>&1 || {
  echo "ccc-crush-headless: '$BIN' not found in PATH" >&2
  exit 127
}
[ -d "$WORKDIR" ] || {
  echo "ccc-crush-headless: workdir does not exist: $WORKDIR" >&2
  exit 2
}
if [ -z "$MODEL" ]; then
  echo "ccc-crush-headless: no model set (CCC_CRUSH_MODEL, e.g. kimi/k3 or zai/glm-5.2)" >&2
  exit 2
fi
case "$MODEL" in
  */*) : ;;
  *) echo "ccc-crush-headless: model must be provider/model form, got: $MODEL" >&2; exit 2 ;;
esac
[ -f "$CONFIG" ] || {
  echo "ccc-crush-headless: config missing: $CONFIG" >&2
  exit 2
}
case "$TMO" in
  ''|*[!0-9]*) echo "ccc-crush-headless: invalid CCC_HEADLESS_TIMEOUT: $TMO" >&2; exit 2 ;;
esac

ERR="$(mktemp "${TMPDIR:-/tmp}/ccc-crush-headless.XXXXXX.err")"
trap 'rm -f "$ERR"' EXIT

# Crush reads CRUSH_GLOBAL_CONFIG for the global config path and honours
# CRUSH_DISABLE_METRICS for the anonymous-usage opt-out. Both are pinned
# here so a caller environment cannot silently widen either.
export CRUSH_GLOBAL_CONFIG="$CONFIG"
export CRUSH_DISABLE_METRICS="${CCC_CRUSH_METRICS_OPTOUT:-1}"

runner=("$BIN")
if [ "$TMO" -gt 0 ]; then
  command -v timeout >/dev/null 2>&1 && runner=(timeout -k 30 "$TMO" "$BIN")
fi

"${runner[@]}" run -q --cwd "$WORKDIR" -m "$MODEL" "$PROMPT" 2>"$ERR"
rc=$?
if [ "$rc" -eq 124 ]; then
  echo "ccc-crush-headless: crush exceeded CCC_HEADLESS_TIMEOUT=${TMO}s and was killed" >&2
  cat "$ERR" >&2
  exit 124
fi
if [ "$rc" -ne 0 ]; then
  echo "ccc-crush-headless: crush exited $rc" >&2
  cat "$ERR" >&2
  exit "$rc"
fi
exit 0
