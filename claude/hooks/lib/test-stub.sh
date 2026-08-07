#!/usr/bin/env bash
# Shared test helpers — allocate private fixture roots and write executable
# stubs without ever truncating an unverified destination.
#
# Termux/Android has no /usr/bin/env, so a stub written with `#!/usr/bin/env bash`
# fails to exec; PATH then falls through to the REAL binary (the real
# `claude`/`curl`/`setsid`), which blocks on stdin/network and the hermetic test
# mass-fails (#472). Resolve the actual bash interpreter at write time so the
# stub execs on every platform (Termux, VPS, macOS).
#
# This file is sourced by the .test.sh suites; it must not change the caller's
# shell options.
#
# Usage:
#   TMP="$(ccc_test_tmpdir)" || exit 1
#   write_exec_stub "$TMP/bin/tool" <<'SH'
#   printf 'fixture\n'
#   SH
#
# `ccc_test_tmpdir` deliberately validates mktemp output instead of relying on
# `set -e` (many suites use `set -uo`). This prevents a failed/empty mktemp
# result from turning "$TMP/bin/tool" into "/bin/tool".
ccc_test_tmpdir_is_safe() {
  local root="${1:-}" mode

  [ -n "$root" ] || return 1
  case "$root" in
    /*) ;;
    *) return 1 ;;
  esac
  [ -d "$root" ] || return 1
  [ ! -L "$root" ] || return 1
  [ -O "$root" ] || return 1

  mode="$(stat -c '%a' -- "$root" 2>/dev/null || stat -f '%Lp' "$root" 2>/dev/null)" ||
    return 1
  [ "$mode" = "700" ]
}

ccc_test_tmpdir() {
  local root

  if [ "$#" -gt 1 ]; then
    printf 'ccc_test_tmpdir: expected zero or one template argument\n' >&2
    return 2
  fi
  if [ "$#" -eq 1 ]; then
    root="$(mktemp -d "$1")" || {
      printf 'ccc_test_tmpdir: mktemp failed\n' >&2
      return 1
    }
  else
    root="$(mktemp -d)" || {
      printf 'ccc_test_tmpdir: mktemp failed\n' >&2
      return 1
    }
  fi

  if ! ccc_test_tmpdir_is_safe "$root"; then
    printf 'ccc_test_tmpdir: rejected unsafe result\n' >&2
    return 1
  fi
  printf '%s\n' "$root"
}

_ccc_test_nlink() {
  stat -c '%h' -- "$1" 2>/dev/null || stat -f '%l' "$1" 2>/dev/null
}

# Drop harness environment inherited from the caller so a suite sees only the
# variables its own fixtures set (#1023).
#
# The hook suites control `CCC_STATE_DIR`, `CCC_CLAUDE_DIR`, etc. per command,
# but never neutralised the rest of the namespace. Inside a bridge-managed
# Claude session `CCC_BRIDGE_DISTILL_MANAGED=1` is exported into the shell, so
# `distill.sh` took its no-op guard and 15 side-effect assertions failed —
# flipping all of `validate-harness.sh` to FAIL on a node with no real defect.
# CI never exports the variable, so it only reproduced where operators actually
# run validation.
#
# Unsets every exported `CCC_*` / `NUNCHI_*` name except `CCC_TEST_*` (fixture
# plumbing such as `CCC_TEST_STUB_ROOT`) and any extra names passed as
# arguments. Call it immediately after sourcing this file, before the suite
# assigns its own fixture variables.
ccc_test_reset_hook_env() {
  local name keep preserved=" "

  for keep in "$@"; do
    preserved="${preserved}${keep} "
  done

  for name in $(compgen -e 2>/dev/null); do
    case "$name" in
      CCC_TEST_*) continue ;;
      CCC_*|NUNCHI_*) ;;
      *) continue ;;
    esac
    case "$preserved" in
      *" $name "*) continue ;;
    esac
    unset "$name"
  done
}

# Stub BODY is read from stdin (typically a quoted heredoc). The destination
# must be below the validated TMP/CCC_TEST_STUB_ROOT. Writes go to a fresh file
# in the same directory and are renamed into place, so an existing symlink or
# hardlink is never opened for truncation.
write_exec_stub() {
  local dest="${1:-}" root="${CCC_TEST_STUB_ROOT:-${TMP:-}}"
  local root_real parent parent_real nlink bash_path stage

  if ! ccc_test_tmpdir_is_safe "$root"; then
    printf 'write_exec_stub: unsafe or missing fixture root\n' >&2
    return 1
  fi
  case "$dest" in
    /*) ;;
    *)
      printf 'write_exec_stub: destination must be absolute\n' >&2
      return 1
      ;;
  esac

  parent="${dest%/*}"
  [ -n "$parent" ] || parent="/"
  if [ ! -d "$parent" ] || [ -L "$parent" ]; then
    printf 'write_exec_stub: destination parent is unsafe\n' >&2
    return 1
  fi
  root_real="$(cd -P -- "$root" 2>/dev/null && pwd)" || return 1
  parent_real="$(cd -P -- "$parent" 2>/dev/null && pwd)" || return 1
  case "$parent_real/" in
    "$root_real/"*) ;;
    *)
      printf 'write_exec_stub: destination escapes fixture root\n' >&2
      return 1
      ;;
  esac

  if [ -e "$dest" ] || [ -L "$dest" ]; then
    if [ -L "$dest" ] || [ ! -f "$dest" ]; then
      printf 'write_exec_stub: existing destination is unsafe\n' >&2
      return 1
    fi
    nlink="$(_ccc_test_nlink "$dest")" || {
      printf 'write_exec_stub: cannot verify destination link count\n' >&2
      return 1
    }
    if [ "$nlink" != "1" ]; then
      printf 'write_exec_stub: refusing multiply-linked destination\n' >&2
      return 1
    fi
  fi

  stage="$(mktemp "$parent/.ccc-test-stub.XXXXXX")" || {
    printf 'write_exec_stub: cannot allocate staging file\n' >&2
    return 1
  }
  if [ -L "$stage" ] || [ ! -f "$stage" ]; then
    rm -f -- "$stage"
    printf 'write_exec_stub: staging file is unsafe\n' >&2
    return 1
  fi
  nlink="$(_ccc_test_nlink "$stage")" || {
    rm -f -- "$stage"
    printf 'write_exec_stub: cannot verify staging link count\n' >&2
    return 1
  }
  if [ "$nlink" != "1" ]; then
    rm -f -- "$stage"
    printf 'write_exec_stub: staging file is multiply linked\n' >&2
    return 1
  fi

  bash_path="$(command -v bash || printf '/bin/bash')"
  if ! {
    printf '#!%s\n' "$bash_path"
    cat
  } > "$stage"; then
    rm -f -- "$stage"
    return 1
  fi
  if ! chmod +x "$stage" || ! mv -f -- "$stage" "$dest"; then
    rm -f -- "$stage"
    return 1
  fi
}
