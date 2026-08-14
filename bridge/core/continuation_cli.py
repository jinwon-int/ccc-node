"""Agent-side registration CLI for the yield-and-continue queue (#1113).

When an agent has more work than fits in the current turn, it registers the
next bundle and ends the turn normally; the bridge monitor then starts that
bundle as a fresh autonomous turn. Natural language alone never creates a
continuation — the agent must register and receive a ``continuation_id``.
Registration binds the *current* conversation through the bridge-published
active-turn route; when no single fresh route exists the command fails closed
(exit 3) so the agent says plainly that auto-continue is unavailable.

Usage (inside an agent turn)::

    python -m telegram_bot.core.continuation_cli register \
        --prompt "<next bundle: what to do, where you left off>"
    python -m telegram_bot.core.continuation_cli list
    python -m telegram_bot.core.continuation_cli cancel <continuation_id>

Exit codes: 0 ok · 2 validation/usage · 3 route unavailable · 4 queue error.
Output is one compact JSON object per line, body-free by contract.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from telegram_bot.core.continuation import (
    ContinuationQueue,
    ContinuationValidationError,
    default_queue_path,
    validate_prompt,
)
from telegram_bot.core.external_wait import resolve_active_route

RC_OK = 0
RC_USAGE = 2
RC_ROUTE_UNAVAILABLE = 3
RC_QUEUE_ERROR = 4


def _home() -> Path:
    override = os.environ.get("CCC_CONTINUATION_HOME", "").strip()
    if override:
        return Path(override)
    external_wait_home = os.environ.get("CCC_EXTERNAL_WAIT_HOME", "").strip()
    if external_wait_home:
        return Path(external_wait_home).parent / "continuation"
    return Path.cwd() / ".telegram_bot" / "continuation"


def _route_home() -> Path:
    """The active-turn route is published under the external-wait home (#740)."""
    override = os.environ.get("CCC_EXTERNAL_WAIT_HOME", "").strip()
    if override:
        return Path(override)
    return Path.cwd() / ".telegram_bot" / "external-wait"


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _parse_args(argv: Sequence[str]) -> dict[str, Any]:
    """Tiny flag parser (no argparse dependency games in hook contexts)."""
    args: dict[str, Any] = {"_": []}
    it = iter(argv)
    for token in it:
        if token.startswith("--"):
            key = token[2:].replace("-", "_")
            try:
                args[key] = next(it)
            except StopIteration:
                raise ContinuationValidationError(f"missing value for --{key}")
        else:
            args["_"].append(token)
    return args


def _cmd_register(home: Path, args: dict[str, Any]) -> int:
    route = resolve_active_route(_route_home())
    if route is None:
        _emit(
            {
                "ok": False,
                "code": "route-unavailable",
                "message": (
                    "no single active conversation route; continue in this "
                    "turn or report that auto-continue is unavailable"
                ),
            }
        )
        return RC_ROUTE_UNAVAILABLE
    try:
        prompt = validate_prompt(args.get("prompt"))
    except ContinuationValidationError as exc:
        _emit({"ok": False, "code": "validation", "message": str(exc)})
        return RC_USAGE

    queue = ContinuationQueue(default_queue_path(home))
    continuation_id, replaced = queue.register(
        user_id=int(route["user_id"]),
        chat_id=int(route["chat_id"]),
        session_id=route.get("session_id"),
        prompt=prompt,
    )
    if not continuation_id:
        _emit({"ok": False, "code": "queue-error"})
        return RC_QUEUE_ERROR
    _emit(
        {
            "ok": True,
            "continuation_id": continuation_id,
            "replaced": replaced,
            "message": (
                "next bundle queued; end this turn normally and the bridge "
                f"starts it (continuation_id={continuation_id})"
            ),
        }
    )
    return RC_OK


def _cmd_list(home: Path) -> int:
    queue = ContinuationQueue(default_queue_path(home))
    _emit(
        {
            "ok": True,
            "continuations": [
                {
                    "continuation_id": rec.get("continuation_id"),
                    "user_id": rec.get("user_id"),
                    "chat_id": rec.get("chat_id"),
                    "state": rec.get("state"),
                    "prompt": str(rec.get("prompt") or "")[:120],
                    "last_error": rec.get("last_error"),
                }
                for rec in sorted(
                    queue.records(), key=lambda r: str(r.get("created_at") or "")
                )
            ],
        }
    )
    return RC_OK


def _cmd_cancel(home: Path, args: dict[str, Any]) -> int:
    positional = args.get("_") or []
    continuation_id = positional[0] if positional else str(args.get("continuation_id", ""))
    if not continuation_id:
        _emit(
            {
                "ok": False,
                "code": "validation",
                "message": "cancel requires a continuation_id",
            }
        )
        return RC_USAGE
    queue = ContinuationQueue(default_queue_path(home))
    cancelled = queue.cancel(continuation_id)
    _emit(
        {
            "ok": bool(cancelled),
            "continuation_id": continuation_id,
            "cancelled": bool(cancelled),
        }
    )
    return RC_OK if cancelled else RC_USAGE


def main(argv: Optional[Sequence[str]] = None) -> int:
    tokens = list(argv if argv is not None else sys.argv[1:])
    if not tokens or tokens[0] in {"-h", "--help"}:
        print(__doc__)
        return RC_USAGE if not tokens else RC_OK
    command, *rest = tokens
    home = _home()
    try:
        args = _parse_args(rest)
    except ContinuationValidationError as exc:
        _emit({"ok": False, "code": "validation", "message": str(exc)})
        return RC_USAGE
    if command == "register":
        return _cmd_register(home, args)
    if command == "list":
        return _cmd_list(home)
    if command == "cancel":
        return _cmd_cancel(home, args)
    _emit({"ok": False, "code": "validation", "message": f"unknown command: {command}"})
    return RC_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
