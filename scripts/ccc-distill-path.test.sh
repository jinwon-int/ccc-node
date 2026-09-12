#!/usr/bin/env bash
# Provider-aware journal selection through both public diagnostics (#1703).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export HOME="$TMP/home" CCC_STATE_DIR="$TMP/state" BOT_DATA_DIR="$TMP/bot"
export CCC_MEMORY_PROBE_PATH="$TMP/absent" CCC_CODEX_MEMORY_MATERIALIZER_PATH="$TMP/absent"
unset CCC_DISTILL_JOURNAL_DIR CCC_AGENT_PROVIDER CCC_BRIDGE_ENV_FILE
mkdir -p "$HOME" "$CCC_STATE_DIR" "$BOT_DATA_DIR/distill-journal" "$BOT_DATA_DIR/danso-distill-journal"
printf '{"status":"snapshot_done"}\n' > "$BOT_DATA_DIR/danso-distill-journal/a.json"
pass=0
check() {
  local expected="$1" script out path
  for script in ccc-distill-check ccc-memory-check; do
    out="$(bash "$ROOT/scripts/$script.sh" --json)"
    if [ "$script" = ccc-distill-check ]; then
      path="$(jq -r '.provider_neutral.journal' <<<"$out")"
    else
      path="$(jq -r '.journal_selection.path' <<<"$out")"
    fi
    [ "$path" = "$expected" ] || { echo "FAIL $script: $path != $expected"; exit 1; }
    pass=$((pass+1))
  done
}
export CCC_AGENT_PROVIDER=danso
check "$BOT_DATA_DIR/danso-distill-journal"
out="$(bash "$ROOT/scripts/ccc-distill-check.sh" --json)"
jq -e '.provider_neutral.ready == 1' <<<"$out" >/dev/null
export CCC_AGENT_PROVIDER=codex
check "$BOT_DATA_DIR/distill-journal"
unset CCC_AGENT_PROVIDER
printf 'CCC_AGENT_PROVIDER=codex\nCCC_AGENT_PROVIDER="danso" # current\n' > "$BOT_DATA_DIR/.env"
check "$BOT_DATA_DIR/danso-distill-journal"
export CCC_AGENT_PROVIDER=claude
check "$BOT_DATA_DIR/distill-journal"
export CCC_DISTILL_JOURNAL_DIR="$TMP/explicit missing"
check "$CCC_DISTILL_JOURNAL_DIR"
out="$(bash "$ROOT/scripts/ccc-distill-check.sh" --json)"
jq -e '.provider_neutral.journal_status == "missing" and .provider_neutral.reason == "journal_missing"' <<<"$out" >/dev/null
[ ! -e "$CCC_DISTILL_JOURNAL_DIR" ]
unset CCC_DISTILL_JOURNAL_DIR CCC_AGENT_PROVIDER
export CCC_BRIDGE_ENV_FILE="$TMP/alternate.env"
printf "CCC_AGENT_PROVIDER='danso'\n" > "$CCC_BRIDGE_ENV_FILE"
check "$BOT_DATA_DIR/danso-distill-journal"
unset CCC_BRIDGE_ENV_FILE
export CCC_DISTILL_JOURNAL_DIR="$TMP/linked"
ln -s "$BOT_DATA_DIR/danso-distill-journal" "$CCC_DISTILL_JOURNAL_DIR"
for script in ccc-distill-check ccc-memory-check; do
  out="$(bash "$ROOT/scripts/$script.sh" --json)"
  jq -e '(.provider_neutral.reason // .journal_selection.reason) == "unsafe_journal_root"' <<<"$out" >/dev/null
  jq -e '(.provider_neutral.total // .writeback_queue.jobs) == 0' <<<"$out" >/dev/null
done
unset CCC_DISTILL_JOURNAL_DIR
# PROJECT_ROOT fallback agrees across diagnostics when BOT_DATA_DIR is unset.
export PROJECT_ROOT="$TMP/project" CCC_AGENT_PROVIDER=danso
saved_bot="$BOT_DATA_DIR"
unset BOT_DATA_DIR
check "$PROJECT_ROOT/.telegram_bot/danso-distill-journal"
export BOT_DATA_DIR="$saved_bot"
# Installed layout must resolve the same shared helper as checkout scripts.
mkdir -p "$HOME/.claude/hooks"
cp -R "$ROOT/claude/hooks/lib" "$ROOT/claude/hooks/nunchi" "$HOME/.claude/hooks/"
cp "$ROOT/scripts/ccc-memory-check.sh" "$HOME/.claude/hooks/"
out="$(bash "$HOME/.claude/hooks/ccc-memory-check.sh" --json)"
jq -e --arg path "$BOT_DATA_DIR/danso-distill-journal" '.journal_selection.path == $path' <<<"$out" >/dev/null
printf 'PASS=%s FAIL=0 (plus counters, absence, installed layout)\n' "$pass"
