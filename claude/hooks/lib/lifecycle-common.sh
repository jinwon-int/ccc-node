#!/usr/bin/env bash
# Small, dependency-light helpers shared by lifecycle-aware shell hooks.
# Never print the raw value passed to ccc_lifecycle_ref.

ccc_lifecycle_digest() { # <raw-value> -> sha256("ccc-lifecycle:" + value)
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
  printf '%s' "$digest"
}

ccc_lifecycle_ref() { # <raw-id> -> first 16 chars of the lifecycle digest
  local digest=""
  digest="$(ccc_lifecycle_digest "${1:-}" 2>/dev/null)" || return 1
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

ccc_lifecycle_no_symlink_components() { # <path>
  local path="${1:-}" current part
  local -a parts=()
  [ -n "$path" ] || return 1
  case "$path" in
    /*) current="/" ;;
    *) current="." ;;
  esac
  IFS='/' read -r -a parts <<< "$path"
  for part in "${parts[@]}"; do
    case "$part" in ''|.) continue ;; ..) return 1 ;; esac
    if [ "$current" = "/" ]; then
      current="/$part"
    else
      current="$current/$part"
    fi
    [ ! -L "$current" ] || return 1
  done
}

ccc_lifecycle_prepare_private_dir() { # <dir>
  local directory="${1:-}" mode="" created=0
  ccc_lifecycle_no_symlink_components "$directory" || return 1
  if [ ! -e "$directory" ]; then
    mkdir -p "$directory" 2>/dev/null || return 1
    created=1
  fi
  [ -d "$directory" ] && [ ! -L "$directory" ] && [ -O "$directory" ] || return 1
  if [ "$created" = 1 ]; then
    chmod 700 "$directory" 2>/dev/null || return 1
  else
    # Never chmod an arbitrary existing parent supplied through an override.
    # Accept it only when it is already owner-only.
    mode="$(stat -c '%a' "$directory" 2>/dev/null \
      || stat -f '%Lp' "$directory" 2>/dev/null)" || return 1
    case "$mode" in ''|*[!0-7]*) return 1 ;; esac
    (( (8#$mode & 077) == 0 )) || return 1
  fi
}

ccc_lifecycle_append_line() { # <file> <already-body-free-line>
  local path="${1:-}" line="${2:-}" directory
  [ -n "$path" ] || return 1
  directory="$(dirname "$path")" || return 1
  ccc_lifecycle_prepare_private_dir "$directory" || return 1
  if [ -e "$path" ]; then
    [ -f "$path" ] && [ ! -L "$path" ] && [ -O "$path" ] || return 1
  else
    (set -o noclobber; : > "$path") 2>/dev/null || true
    [ -f "$path" ] && [ ! -L "$path" ] && [ -O "$path" ] || return 1
  fi
  chmod 600 "$path" 2>/dev/null || return 1
  printf '%s\n' "$line" >> "$path" 2>/dev/null
}
