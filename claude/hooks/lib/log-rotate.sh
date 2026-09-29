#!/usr/bin/env bash
# Size-based generation rotation for append-only hook logs (#1882).
#
# state/distill.log was appended on every SessionEnd / SessionStart drain and
# never rotated: one node reached 423 MB / 3.7M lines. The in-place byte-tail
# idiom (lifecycle-common.sh, load-memory.sh timing) keeps only a tail and
# discards the rest; this helper instead renames the live file into numbered
# generations so recent history survives one or two rotations for diagnosis.
#
#   <file>      live log (new appends recreate it)
#   <file>.1    previous generation  (<file>.1.gz once compressed)
#   <file>.N    oldest kept          (N = keep); anything older is removed
#
# Writers here append with `>> "$LOG"` per line, so after the rename the next
# append recreates <file>; a writer that still holds an fd finishes into .1.
# Compression runs detached (nice'd) so a hook never waits on it — the first
# rotation of a legacy multi-hundred-MB file must not stall SessionEnd.
# Best-effort throughout: every failure leaves the log appendable and returns 0.
# Concurrency: a losing racer's `mv` of the live file fails and it stops; the
# residual window (two rotations inside one gzip run) needs <max_bytes> of new
# log in seconds and at worst drops one old generation of a diagnostic log.

# ccc_rotate_log_if_large <file> <max_bytes> <keep>
ccc_rotate_log_if_large() {
  local path="${1:-}" max_bytes="${2:-}" keep="${3:-}" size staged i
  [ -n "$path" ] || return 0
  case "$max_bytes" in ''|*[!0-9]*) return 0 ;; esac
  case "$keep" in ''|*[!0-9]*) return 0 ;; esac
  [ "$max_bytes" -gt 0 ] && [ "$keep" -gt 0 ] || return 0
  [ -f "$path" ] && [ ! -L "$path" ] || return 0
  size="$(wc -c < "$path" 2>/dev/null | tr -d '[:space:]')"
  case "$size" in ''|*[!0-9]*) return 0 ;; esac
  [ "$size" -gt "$max_bytes" ] || return 0

  # Claim the live file under a unique name first; only one racer wins.
  staged="$path.rotating.${BASHPID:-$$}"
  mv -f -- "$path" "$staged" 2>/dev/null || return 0

  rm -f -- "$path.$keep" "$path.$keep.gz" 2>/dev/null
  for (( i = keep - 1; i >= 1; i-- )); do
    [ -e "$path.$i.gz" ] && mv -f -- "$path.$i.gz" "$path.$((i + 1)).gz" 2>/dev/null
    [ -e "$path.$i" ] && mv -f -- "$path.$i" "$path.$((i + 1))" 2>/dev/null
  done
  mv -f -- "$staged" "$path.1" 2>/dev/null || { rm -f -- "$staged" 2>/dev/null; return 0; }
  chmod 600 "$path.1" 2>/dev/null || true

  _ccc_log_rotate_compress "$path.1"
  return 0
}

# Detached gzip of one rotated generation. Disabled with
# CCC_LOG_ROTATE_GZIP=0; a no-op when gzip is unavailable (the generation then
# stays uncompressed and still counts toward <keep>).
_ccc_log_rotate_compress() {
  local target="${1:-}"
  case "${CCC_LOG_ROTATE_GZIP:-1}" in 0|false|FALSE|off|OFF|no|NO) return 0 ;; esac
  command -v gzip >/dev/null 2>&1 || return 0
  [ -f "$target" ] || return 0
  if [ "${CCC_LOG_ROTATE_GZIP_SYNC:-0}" = 1 ]; then
    gzip -f -- "$target" </dev/null >/dev/null 2>&1 || true
    return 0
  fi
  local nice_bin=""
  command -v nice >/dev/null 2>&1 && nice_bin="nice"
  if command -v setsid >/dev/null 2>&1; then
    if [ -n "$nice_bin" ]; then
      setsid nice -n 10 gzip -f -- "$target" </dev/null >/dev/null 2>&1 &
    else
      setsid gzip -f -- "$target" </dev/null >/dev/null 2>&1 &
    fi
  else
    ( if [ -n "$nice_bin" ]; then exec nice -n 10 gzip -f -- "$target"; fi
      exec gzip -f -- "$target" ) </dev/null >/dev/null 2>&1 &
  fi
  disown "$!" 2>/dev/null || true
  return 0
}
