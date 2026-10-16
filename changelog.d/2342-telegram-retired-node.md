- **feat(harness): Telegram-retired (Matrix-only) nodes via `~/.claude/frontends` (#2342).**
  A node that keeps only the Matrix frontend declares it in an operator-owned
  frontend list (`~/.claude/frontends`, one name per line, `#` comments; or
  `CCC_FRONTENDS`, which wins). Without `telegram` in it, `ccc-doctor`'s
  `bridge status` reports `telegram retired` as normal instead of the Telegram
  probe's `unavailable`, and warns `telegram retired but running` when the
  retired bridge has been revived. `scripts/termux-restart-frontends.sh`
  skips the Telegram restart and recovery arming in its default mode so a
  Termux self-update no longer brings a retired Telegram bridge back, and
  `--dry-run` prints `telegram=enabled|retired`. No list keeps the previous
  behaviour. `docs/matrix-frontend.md` documents the full retirement
  procedure (allowlist, unit, Termux restart/health commands, boot scripts,
  watchdogs, rollback).
