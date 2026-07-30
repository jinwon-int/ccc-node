"""Agent-side registration CLI for durable external waits (#740).

When an agent wants to promise "I will continue once CI finishes", it must
register the wait and receive a ``wait_id`` — natural language alone never
creates a monitor. Registration binds the *current* conversation through
the bridge-published active-turn route; when no single fresh route exists
the command fails closed (exit 3) so the agent either keeps a foreground
``gh pr checks --watch`` or says plainly that auto-resume is unavailable.

Usage (inside an agent turn)::

    python -m telegram_bot.core.external_wait_cli register \
        --repo owner/name --pr 123 --head-sha abc1234 \
        --summary "merge when green"
    python -m telegram_bot.core.external_wait_cli list
    python -m telegram_bot.core.external_wait_cli cancel <wait_id>

Exit codes: 0 ok · 2 validation/usage · 3 route unavailable · 4 registry error.
Output is one compact JSON object per line, body-free by contract.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from telegram_bot.core.external_wait import (
    ExternalWaitRegistry,
    ExternalWaitValidationError,
    default_registry_path,
    resolve_active_route,
    validate_head_sha,
    validate_pr_number,
    validate_repo,
    validate_summary,
)
from telegram_bot.core.external_wait_monitor import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
)

RC_OK = 0
RC_USAGE = 2
RC_ROUTE_UNAVAILABLE = 3
RC_REGISTRY_ERROR = 4


def _home() -> Path:
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
                raise ExternalWaitValidationError(f"missing value for --{key}")
        else:
            args["_"].append(token)
    return args


def _cmd_register(home: Path, args: dict[str, Any]) -> int:
    route = resolve_active_route(home)
    if route is None:
        _emit(
            {
                "ok": False,
                "code": "route-unavailable",
                "message": (
                    "no single active conversation route; keep a foreground "
                    "watch or report that auto-resume is unavailable"
                ),
            }
        )
        return RC_ROUTE_UNAVAILABLE
    try:
        repo = validate_repo(str(args.get("repo", "")))
        pr_number = validate_pr_number(args.get("pr"))
        head_sha = validate_head_sha(str(args.get("head_sha", "")))
        summary = validate_summary(args.get("summary"))
        timeout = float(args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    except ExternalWaitValidationError as exc:
        _emit({"ok": False, "code": "validation", "message": str(exc)})
        return RC_USAGE
    except (TypeError, ValueError):
        _emit({"ok": False, "code": "validation", "message": "invalid numeric field"})
        return RC_USAGE

    registry = ExternalWaitRegistry(default_registry_path(home))
    wait_id = registry.register(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        user_id=int(route["user_id"]),
        chat_id=int(route["chat_id"]),
        session_id=route.get("session_id"),
        summary=summary,
        timeout_seconds=timeout,
        poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    if not wait_id:
        _emit({"ok": False, "code": "registry-error"})
        return RC_REGISTRY_ERROR
    _emit(
        {
            "ok": True,
            "wait_id": wait_id,
            "repo": repo,
            "pr": pr_number,
            "head_sha": head_sha,
            "timeout_seconds": int(timeout),
            "message": (
                f"watching GitHub checks; conversation resumes on terminal "
                f"state (wait_id={wait_id})"
            ),
        }
    )
    return RC_OK


def _cmd_list(home: Path) -> int:
    registry = ExternalWaitRegistry(default_registry_path(home))
    records = registry.records()
    _emit(
        {
            "ok": True,
            "waits": [
                {
                    "wait_id": rec.get("wait_id"),
                    "repo": rec.get("repo"),
                    "pr": rec.get("pr_number"),
                    "head_sha": str(rec.get("head_sha") or "")[:8],
                    "state": rec.get("state"),
                    "summary": rec.get("summary") or "",
                    # Whether the promise actually continued, not just whether
                    # the notification landed: a terminal wait with
                    # resumed=false is still owed an action by the owner.
                    "resumed": (rec.get("wake") or {}).get("resumed"),
                    "skip_reason": (rec.get("wake") or {}).get("skip_reason"),
                }
                for rec in sorted(records, key=lambda r: str(r.get("created_at") or ""))
            ],
        }
    )
    return RC_OK


def _cmd_cancel(home: Path, args: dict[str, Any]) -> int:
    positional = args.get("_") or []
    wait_id = positional[0] if positional else str(args.get("wait_id", ""))
    if not wait_id:
        _emit({"ok": False, "code": "validation", "message": "cancel requires a wait_id"})
        return RC_USAGE
    registry = ExternalWaitRegistry(default_registry_path(home))
    cancelled = registry.cancel(wait_id)
    _emit({"ok": bool(cancelled), "wait_id": wait_id, "cancelled": bool(cancelled)})
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
    except ExternalWaitValidationError as exc:
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
