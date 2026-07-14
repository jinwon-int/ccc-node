#!/usr/bin/env bash
# Portable mtime-based file pruning / selection for Claude hooks (#449).
#
# GNU `find -printf '%T@ %p\n'` and `ls -1t | xargs rm` are non-portable:
#   - busybox find (Termux/Android nodes) has no -printf, so the whole prune
#     pipeline was a silent no-op (stderr discarded) and state directories grew
#     unbounded — worst exactly on storage-constrained mobile nodes.
#   - `ls`/`find` text parsing breaks on filenames containing whitespace.
#
# These helpers delegate mtime handling to python3 (already required by the
# distill subsystem) for portable, whitespace-safe behavior. When python3 is
# unavailable they degrade to a non-fatal no-op — pruning is best-effort and
# callers already tolerate failure with `|| true`.

_MTIME_PRUNE_HAS_PY() { command -v python3 >/dev/null 2>&1; }

# prune_keep_newest <dir> <glob> <keep>
# Remove files directly under <dir> matching shell <glob>, keeping the <keep>
# most recent by mtime. Whitespace-safe. No-op when <dir> is missing, <keep> is
# not a non-negative integer, or python3 is unavailable.
prune_keep_newest() {
  local dir="${1:-}" glob="${2:-}" keep="${3:-}"
  [ -d "$dir" ] || return 0
  case "$keep" in ''|*[!0-9]*) return 0 ;; esac
  _MTIME_PRUNE_HAS_PY || return 0
  python3 - "$dir" "$glob" "$keep" <<'PY' 2>/dev/null || return 0
import os, sys, glob as globmod
d, pattern, keep = sys.argv[1], sys.argv[2], int(sys.argv[3])
paths = [p for p in globmod.glob(os.path.join(d, pattern)) if os.path.isfile(p)]
paths.sort(key=lambda p: (os.stat(p).st_mtime, p), reverse=True)
for p in paths[keep:]:
    try:
        os.remove(p)
    except OSError:
        pass
PY
}

# newest_file <dir> <glob>
# Print the path of the newest file (by mtime) directly under <dir> matching
# shell <glob>. Prints nothing when there is no match, <dir> is missing, or
# python3 is unavailable.
newest_file() {
  local dir="${1:-}" glob="${2:-}"
  [ -d "$dir" ] || return 0
  _MTIME_PRUNE_HAS_PY || return 0
  python3 - "$dir" "$glob" <<'PY' 2>/dev/null || return 0
import os, sys, glob as globmod
d, pattern = sys.argv[1], sys.argv[2]
paths = [p for p in globmod.glob(os.path.join(d, pattern)) if os.path.isfile(p)]
if paths:
    print(max(paths, key=lambda p: (os.stat(p).st_mtime, p)))
PY
}
