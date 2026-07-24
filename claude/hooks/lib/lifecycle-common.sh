#!/usr/bin/env bash
# Small, dependency-light helpers shared by lifecycle-aware shell hooks.
# Never print the raw value passed to ccc_lifecycle_ref.

ccc_lifecycle_ref() { # <raw-id> -> first 16 chars of sha256("ccc-lifecycle:" + id)
  local value="${1:-}" digest=""
  [ -n "$value" ] || return 1
  if command -v sha256sum >/dev/null 2>&1; then
    digest="$(printf 'ccc-lifecycle:%s' "$value" | sha256sum 2>/dev/null)"
    digest="${digest%% *}"
  elif command -v shasum >/dev/null 2>&1; then
    digest="$(printf 'ccc-lifecycle:%s' "$value" | shasum -a 256 2>/dev/null)"
    digest="${digest%% *}"
  elif command -v openssl >/dev/null 2>&1; then
    digest="$(printf 'ccc-lifecycle:%s' "$value" | openssl dgst -sha256 -r 2>/dev/null)"
    digest="${digest%% *}"
  fi
  case "$digest" in
    ''|*[!0-9a-fA-F]*) return 1 ;;
  esac
  printf '%.16s' "$digest"
}

ccc_lifecycle_state_dir() {
  if [ -n "${CCC_STATE_DIR:-}" ]; then
    printf '%s' "$CCC_STATE_DIR"
  elif [ -n "${HOME:-}" ]; then
    printf '%s' "$HOME/.claude/state"
  else
    return 1
  fi
}
