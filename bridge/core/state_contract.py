"""Working-state checkpoint contract: the one file a headless turn may write.

The harness asks every session to keep ``working-state.md`` current as
objective / progress / next step (Claude cheatsheet + ``checkpoint.sh``;
Codex/Piri via the materialized policy block, #1176).  The turns that need it
most — CI-wait / external_event continuations — have no approval route, so a
Claude ``can_use_tool`` escalation for that exact file used to fail closed
(#1045: ``denied-no-route`` with ``turn=none``, ``no-approval-callback`` /
``handler-deny`` with an active turn).  Proposal 2 (#1047) made the deny
visible; this module is proposal 1: a deliberately narrow allow predicate.

Scope, all conditions AND-ed:

* action is a structured file write — ``Write`` / ``Edit`` / ``MultiEdit``.
  ``Bash`` is never matched (a redirect target cannot be classified).
* the request carries an absolute ``file_path`` whose ``realpath`` equals the
  ``realpath`` of one **known contract file**: ``<state-dir>/working-state.md``
  for the node's default state dir (``CCC_STATE_DIR`` of the bridge process,
  else ``~/.claude/state``) and, when the session is audience-scoped, its
  scoped state dir.  Nothing else under ``state/`` qualifies.
* the contract file itself is not a symlink (a link pointing elsewhere makes
  the predicate refuse; the request then falls back to the normal route).
* the kill-switch is on (``CCC_STATE_CONTRACT_ALLOW``, default enabled).

A refusal here is never a deny — it only means "not the contract file", and
the caller continues with its existing fail-closed approval flow.  Nothing in
this module reads or logs request bodies.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import os
from pathlib import Path
from typing import Any

CONTRACT_FILE_NAME = "working-state.md"
CONTRACT_ACTIONS = frozenset({"Write", "Edit", "MultiEdit"})
ALLOW_REASON = "state-contract-allow"
KILL_SWITCH_ENV = "CCC_STATE_CONTRACT_ALLOW"
_OFF = frozenset({"0", "false", "off", "no"})


def state_contract_enabled(settings: Any = None, environ: Mapping[str, str] | None = None) -> bool:
    """Kill-switch. Settings attribute wins; env is the fallback; default on."""

    value = getattr(settings, "state_contract_allow_enabled", None) if settings is not None else None
    if isinstance(value, bool):
        return value
    env = os.environ if environ is None else environ
    raw = (env.get(KILL_SWITCH_ENV) or "").strip().lower()
    return raw not in _OFF


def default_state_dir(settings: Any = None, environ: Mapping[str, str] | None = None) -> Path:
    """The node's unscoped state dir: ``$CCC_STATE_DIR`` else ``<claude-root>/state``."""

    env = os.environ if environ is None else environ
    raw = (env.get("CCC_STATE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    settings_path = getattr(settings, "claude_settings_path", None) if settings is not None else None
    if settings_path:
        return Path(str(settings_path)).expanduser().parent / "state"
    return Path(env.get("HOME") or Path.home()).expanduser() / ".claude" / "state"


def contract_files(
    settings: Any = None,
    *,
    extra_state_dirs: Iterable[str | os.PathLike[str] | None] = (),
    include_default: bool = True,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Every path that counts as *the* contract file for this session.

    Order is stable (default first, then the session's extra dirs) and
    duplicates are dropped, so the log can stay body-free and deterministic.
    ``include_default=False`` is for a *shared*-audience session: its contract
    file is only the scoped one — the node's unscoped checkpoint is private
    input that a shared route must never write (mirrors the #1155 read gate).
    """

    seen: list[Path] = []
    default = (default_state_dir(settings, environ),) if include_default else ()
    for directory in (*default, *extra_state_dirs):
        if directory is None:
            continue
        text = str(directory).strip()
        if not text or "\x00" in text:
            continue
        candidate = Path(text).expanduser() / CONTRACT_FILE_NAME
        if candidate not in seen:
            seen.append(candidate)
    return tuple(seen)


def _requested_path(arguments: Mapping[str, Any] | None) -> Path | None:
    if not isinstance(arguments, Mapping):
        return None
    raw = arguments.get("file_path")
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return None
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        return None
    return path


def state_contract_allows(
    action: str | None,
    arguments: Mapping[str, Any] | None,
    *,
    candidates: Iterable[Path],
    enabled: bool = True,
) -> bool:
    """True only for a structured write to a known, non-symlinked contract file."""

    if not enabled or action not in CONTRACT_ACTIONS:
        return False
    requested = _requested_path(arguments)
    if requested is None:
        return False
    try:
        requested_real = os.path.realpath(requested)
    except (OSError, ValueError):
        return False
    for candidate in candidates:
        try:
            if os.path.islink(candidate):
                # A contract path that is itself a link diverges from the
                # contract; refuse and let the normal route decide.
                continue
            if os.path.realpath(candidate) == requested_real:
                return True
        except (OSError, ValueError):
            continue
    return False
