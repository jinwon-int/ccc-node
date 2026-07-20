#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
POLICY="$ROOT/scripts/ccc_codex_github_policy.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0
fail=0

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

plugin_disabled() {
  python3 - "$1" <<'PY'
import pathlib
import sys
import tomllib

with pathlib.Path(sys.argv[1]).open("rb") as handle:
    config = tomllib.load(handle)
entry = config.get("plugins", {}).get("github@openai-curated-remote", {})
raise SystemExit(0 if entry.get("enabled") is False else 1)
PY
}

status_missing="$TMP/status-missing"
out="$(python3 "$POLICY" status --codex-home "$status_missing" --json)"; rc=$?
ok "missing status is read-only" \
  '[ "$rc" = 3 ] && [ ! -e "$status_missing" ] && jq -e '\''.status == "missing"'\'' <<<"$out" >/dev/null'

fresh="$TMP/fresh"
out="$(python3 "$POLICY" apply --codex-home "$fresh" --json)"; rc=$?
ok "fresh apply succeeds and reports a change" \
  '[ "$rc" = 0 ] && jq -e '\''(.status == "disabled") and (.changed == true)'\'' <<<"$out" >/dev/null'
ok "fresh apply creates the canonical disabled plugin entry" \
  'plugin_disabled "$fresh/config.toml"'
ok "fresh config is private" \
  '[ "$(stat -c %a "$fresh/config.toml")" = 600 ]'

fresh_before="$(sha256sum "$fresh/config.toml")"
out="$(python3 "$POLICY" apply --codex-home "$fresh" --json)"; rc=$?
fresh_after="$(sha256sum "$fresh/config.toml")"
ok "second apply is byte-idempotent" \
  '[ "$rc" = 0 ] && [ "$fresh_before" = "$fresh_after" ] && jq -e '\''.changed == false'\'' <<<"$out" >/dev/null'
out="$(python3 "$POLICY" status --codex-home "$fresh" --json)"; rc=$?
ok "status reports disabled without config contents" \
  '[ "$rc" = 0 ] && jq -e '\''.status == "disabled"'\'' <<<"$out" >/dev/null && ! grep -q "github@" <<<"$out"'

existing="$TMP/existing"
mkdir -p "$existing"
printf '%s\n' \
  '# keep-comment' \
  'sentinel = "KEEP-BYTE-FOR-BYTE"' \
  '' \
  '[plugins."github@openai-curated-remote"] # keep-plugin-comment' \
  'enabled = true # old state' \
  'mcp_servers = {}' \
  '' \
  '[projects."/srv/repo"]' \
  'trust_level = "trusted"' > "$existing/config.toml"
python3 "$POLICY" apply --codex-home "$existing" --json >/dev/null; rc=$?
ok "canonical existing config is updated" \
  '[ "$rc" = 0 ] && plugin_disabled "$existing/config.toml"'
ok "unrelated values and comments are preserved" \
  'grep -Fq '\''sentinel = "KEEP-BYTE-FOR-BYTE"'\'' "$existing/config.toml" && grep -Fq '\''# keep-plugin-comment'\'' "$existing/config.toml" && grep -Fq '\''enabled = false # old state'\'' "$existing/config.toml" && grep -Fq '\''[projects."/srv/repo"]'\'' "$existing/config.toml"'

inline="$TMP/inline"
mkdir -p "$inline"
printf '%s\n' '[plugins]' '"github@openai-curated-remote" = { enabled = true }' > "$inline/config.toml"
inline_before="$(sha256sum "$inline/config.toml")"
out="$(python3 "$POLICY" apply --codex-home "$inline" --json)"; rc=$?
inline_after="$(sha256sum "$inline/config.toml")"
ok "noncanonical inline plugin config fails closed" \
  '[ "$rc" = 2 ] && [ "$inline_before" = "$inline_after" ] && jq -e '\''.code == "plugin_config_noncanonical"'\'' <<<"$out" >/dev/null'

invalid="$TMP/invalid"
mkdir -p "$invalid"
printf '%s\n' 'not valid = [' > "$invalid/config.toml"
invalid_before="$(sha256sum "$invalid/config.toml")"
out="$(python3 "$POLICY" apply --codex-home "$invalid" --json)"; rc=$?
invalid_after="$(sha256sum "$invalid/config.toml")"
ok "invalid TOML fails without rewriting the file" \
  '[ "$rc" = 2 ] && [ "$invalid_before" = "$invalid_after" ] && jq -e '\''.code == "config_invalid_toml"'\'' <<<"$out" >/dev/null'

linked="$TMP/linked"
mkdir -p "$linked"
printf '%s\n' 'sentinel = "outside"' > "$TMP/outside.toml"
ln -s "$TMP/outside.toml" "$linked/config.toml"
outside_before="$(sha256sum "$TMP/outside.toml")"
out="$(python3 "$POLICY" apply --codex-home "$linked" --json)"; rc=$?
outside_after="$(sha256sum "$TMP/outside.toml")"
ok "config symlinks are rejected without touching the target" \
  '[ "$rc" = 2 ] && [ "$outside_before" = "$outside_after" ] && jq -e '\''.code == "config_unsafe"'\'' <<<"$out" >/dev/null'

home_target="$TMP/home-target"
home_link="$TMP/home-link"
mkdir -p "$home_target"
ln -s "$home_target" "$home_link"
out="$(python3 "$POLICY" apply --codex-home "$home_link" --json)"; rc=$?
ok "Codex home symlinks are rejected before config creation" \
  '[ "$rc" = 2 ] && [ ! -e "$home_target/config.toml" ] && jq -e '\''.code == "codex_home_unsafe"'\'' <<<"$out" >/dev/null'

printf 'PASS=%d FAIL=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
