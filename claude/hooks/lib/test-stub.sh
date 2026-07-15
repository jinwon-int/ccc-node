#!/usr/bin/env bash
# Shared test helper — write an executable stub with a platform-correct shebang.
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
#   write_exec_stub <dest>   # stub BODY on stdin, WITHOUT a shebang line
#     ... reads the stub body from stdin (typically a quoted heredoc) ...
write_exec_stub() {
  local dest="$1" bash_path
  bash_path="$(command -v bash || printf '/bin/bash')"
  {
    printf '#!%s\n' "$bash_path"
    cat
  } > "$dest"
  chmod +x "$dest"
}
