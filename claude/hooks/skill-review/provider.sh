#!/usr/bin/env bash
# skill-review/provider.sh — provider-neutral skill-autosave resolution (#643).
#
# The skill-autosave install pipeline (autoinstall.sh) is byte-for-byte the
# same for every provider: it gates a SKILL.md file and installs the passing
# draft into a skills directory. Only two things are provider-specific — which
# skills directory receives the install, and a compatibility screen for
# Claude-only couplings. This library isolates both so the rest of the pipeline
# stays provider-neutral.
#
# Source this file; it defines functions only and has no side effects.
#
#   CCC_SKILL_PROVIDER   claude | codex | piri | danso   (explicit; wins over auto-detect)
#   CLAUDE_SKILLS_DIR    Claude install target (default ~/.claude/skills)
#   CODEX_SKILLS_DIR     Codex install target  (default $CODEX_HOME/skills)
#   CODEX_HOME           Codex home            (default ~/.codex)
#   PIRI_SKILLS_DIR      Piri install target   (default $PIRI_CODING_AGENT_DIR/skills)
#   PIRI_CODING_AGENT_DIR Piri agent home      (default ~/.piri/agent)
#   DANSO_SKILLS_DIR     Danso install target  (default $CCC_DANSO_STATE_DIR/home/.pi/agent/skills)
#   CCC_DANSO_STATE_DIR  Danso bridge state dir (no login-HOME fallback: #1659)

# ccc_skill_provider — echo the active provider, one of: claude | codex | piri | danso.
#
# Explicit CCC_SKILL_PROVIDER always wins. When unset we auto-detect: a node
# with a Codex home but no Claude home/binary is a Codex node; everything else
# defaults to claude (the historical, back-compatible behavior). Piri and danso
# are explicit-only: bridge nodes commonly carry a ~/.piri/agent tree or a
# danso state dir for A2A workers while their interactive lane stays Claude, so
# a home-dir probe can never select them on its own.
ccc_skill_provider() {
  local p="${CCC_SKILL_PROVIDER:-}"
  case "$p" in
    codex) printf 'codex'; return 0 ;;
    piri) printf 'piri'; return 0 ;;
    danso) printf 'danso'; return 0 ;;
    claude) printf 'claude'; return 0 ;;
  esac
  local home="${HOME:-/root}"
  if [ -n "${CODEX_HOME:-}" ] || [ -d "$home/.codex" ]; then
    if [ ! -d "$home/.claude" ] && ! command -v claude >/dev/null 2>&1; then
      printf 'codex'; return 0
    fi
  fi
  printf 'claude'
}

# ccc_skills_dir <provider> — echo the install target directory for a provider.
# Env overrides win so tests and operators can redirect the write surface.
ccc_skills_dir() {
  local provider="$1" home="${HOME:-/root}"
  case "$provider" in
    codex)
      printf '%s' "${CODEX_SKILLS_DIR:-${CODEX_HOME:-$home/.codex}/skills}"
      ;;
    piri)
      printf '%s' "${PIRI_SKILLS_DIR:-${PIRI_CODING_AGENT_DIR:-$home/.piri/agent}/skills}"
      ;;
    danso)
      # #1659 contract, fail-closed: explicit DANSO_SKILLS_DIR wins, then the
      # bridge-fixed danso HOME (<CCC_DANSO_STATE_DIR>/home, danso_runtime.py).
      # No login-HOME fallback — ~/.pi/agent/skills is the INTERACTIVE danso/pi
      # root and is invisible to bridge sessions, so guessing it would point
      # installs at a tree the bridge danso never reads. Empty output = no
      # resolvable target; callers must treat that as a hard skip/failure.
      if [ -n "${DANSO_SKILLS_DIR:-}" ]; then
        printf '%s' "$DANSO_SKILLS_DIR"
      elif [ -n "${CCC_DANSO_STATE_DIR:-}" ]; then
        printf '%s' "${CCC_DANSO_STATE_DIR%/}/home/.pi/agent/skills"
      fi
      ;;
    *)
      printf '%s' "${CLAUDE_SKILLS_DIR:-${CCC_CLAUDE_DIR:-$home/.claude}/skills}"
      ;;
  esac
}

# ccc_ensure_skills_dir <dir> — create the skills directory owner-only (0700)
# and reject a symlinked target. Returns non-zero (creating nothing) when the
# leaf is a symlink so the caller can fail the install closed and a planted link
# can never redirect an install outside the owned skills tree. An existing
# regular directory is accepted as-is — perms are not tightened retroactively so
# a shared Claude ~/.claude/skills is left untouched. A missing directory is
# created under umask 077.
ccc_ensure_skills_dir() {
  local dir="$1"
  [ -n "$dir" ] || return 1
  if [ -L "$dir" ]; then
    return 1
  fi
  if [ -d "$dir" ]; then
    return 0
  fi
  if [ -e "$dir" ]; then
    # exists but is not a directory (regular file, fifo, ...) — refuse.
    return 1
  fi
  ( umask 077; mkdir -p "$dir" ) 2>/dev/null || return 1
  return 0
}
