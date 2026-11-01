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
    python -m telegram_bot.core.external_wait_cli register \
        --repo owner/name --pr 123 --head-sha def5678 \
        --summary "merge when green" --keep-previous 1
    python -m telegram_bot.core.external_wait_cli register \
        --repo owner/name --pr 123 --head-sha def5678 --source merge-queue \
        --summary "clean up the branch once it lands"
    python -m telegram_bot.core.external_wait_cli register \
        --repo owner/name --source actions-run --run-id 41058887647 \
        --summary "download the bundle and verify it"
    python -m telegram_bot.core.external_wait_cli register \
        --source local-task --task-id b6gw3sysl \
        --summary "nightly skill sweep (manual run)" [--timeout-seconds 1800]
    python -m telegram_bot.core.external_wait_cli register \
        --source detached-job --unit nightly-compare-20261101 \
        --log /root/.claude/state/nightly-compare-20261101.log \
        --summary "read the compare log and report" [--timeout-seconds 7200]
    python -m telegram_bot.core.external_wait_cli list
    python -m telegram_bot.core.external_wait_cli cancel <wait_id>

``--source merge-queue`` (#2118) watches an *enqueued* PR instead of its
check rollup: the wait ends ``merged`` when it lands, ``evicted`` when the
queue drops it (the speculative group run failed — the PR stays OPEN with its
approval and no check changes, so the default source never wakes), or
``closed``. An eviction records the failed merge-group run id for the
resumed turn. Register right after enqueueing.

``--source actions-run`` (#2590) watches one GitHub Actions workflow run by
``--run-id`` (a dispatched build, a release upload) instead of a PR. The run
is resolved through ``gh`` at registration — an unknown run fails closed — and
its workflow name, run number and head are recorded for display. The wait
ends ``success`` (conclusion success/neutral), ``cancelled``, or ``failure``
(any other conclusion, recorded for the resumed turn). A run waiting on an
environment approval stays open and the status line says so. ``--head-sha``
is optional; when given it must match the run's head.

``--source local-task`` (#2605) labels one of this session's own
run-in-background tasks by its harness task id so the conversation's status
line can name the work. Nothing polls it: the harness continues the session
when the task ends and the bridge closes the record from that frame (also when
the session goes away — ``lost``). It never wakes the conversation; the
deadline (default 6 h) only turns the line into ``⏰ … expired``.

``--source detached-job`` (#2605 layer 3) watches a ``systemd-run`` transient
unit launched by the ``bridge-safe-detached-run`` recipe: the monitor polls
the job's log for its ``EXIT=<n>`` marker (local file read; no GitHub) and
wakes the conversation like a CI wait — ``success`` for ``EXIT=0``,
``failure`` otherwise (exit code in the event), ``lost`` when the unit is gone
without a marker. ``detached_jobs.py register`` arms this automatically when
the bridge is present; the explicit form is for other launchers.

A new registration for the same PR at a different head supersedes this
conversation's older pending waits for that PR (#1110): a stale head must
never wake the session with an outdated rollup. ``--keep-previous`` keeps
the older watches when watching two heads is genuinely intended.

Exit codes: 0 ok · 2 validation/usage · 3 route unavailable · 4 registry error.
Output is one compact JSON object per line, body-free by contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from telegram_bot.core.external_wait import (
    SOURCE_GITHUB_ACTIONS_RUN,
    SOURCE_GITHUB_MERGE_QUEUE,
    SOURCE_DETACHED_JOB,
    SOURCE_GITHUB_PR_CHECKS,
    SOURCE_LOCAL_TASK,
    STATE_MONITORING,
    TERMINAL_SUPERSEDED,
    ExternalWaitRegistry,
    ExternalWaitValidationError,
    default_registry_path,
    describe_route_unavailable,
    resolve_active_route,
    validate_head_sha,
    validate_pr_number,
    validate_repo,
    validate_run_id,
    validate_run_label,
    validate_log_path,
    validate_summary,
    validate_task_id,
    validate_unit_name,
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


def resolve_full_head_sha(repo: str, head_sha: str) -> str:
    """Normalize a 7-39 char short SHA to the full 40-char head (#961).

    The monitor compares the recorded SHA against GitHub's 40-char
    ``headRefOid`` with exact equality, so a short SHA that passes format
    validation can never match — registration would return ``ok`` for a wait
    that supersedes on the first poll (the #949 silent promise loss). Resolve
    through ``gh`` at registration; when the short SHA does not resolve to a
    commit in the repo, registration fails closed instead of promising a
    watch that can never fire. Full 40-char SHAs pass through untouched.
    """
    if len(head_sha) == 40:
        return head_sha
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{head_sha}", "--jq", ".sha"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ExternalWaitValidationError(
            "short head SHA could not be resolved via gh (unavailable or timeout); "
            "pass the full 40-char SHA"
        )
    if proc.returncode != 0:
        raise ExternalWaitValidationError(
            "short head SHA does not resolve to a commit in the repo; "
            "pass the full 40-char SHA from the PR head"
        )
    full = proc.stdout.decode("utf-8", "replace").strip().lower()
    try:
        full = validate_head_sha(full)
    except ExternalWaitValidationError:
        full = ""
    if len(full) != 40 or not full.startswith(head_sha):
        raise ExternalWaitValidationError(
            "head SHA resolution returned an unexpected value; registration refused"
        )
    return full


def resolve_actions_run(repo: str, run_id: int) -> dict[str, Any]:
    """Look the run up through ``gh`` at registration (#2590), fail-closed.

    Returns ``{"name", "run_number", "head_sha", "status"}`` (body-free). A
    run that does not resolve would register a wait that can only end in a
    monitor error after three polls — refuse it up front instead, like an
    unresolvable short SHA (#961).
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/actions/runs/{int(run_id)}",
                "--jq",
                "{name: .name, run_number: .run_number, head_sha: .head_sha, status: .status}",
            ],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ExternalWaitValidationError("Actions run could not be resolved via gh (unavailable or timeout)")
    if proc.returncode != 0:
        raise ExternalWaitValidationError("Actions run does not resolve in the repo; check --repo and --run-id")
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        raise ExternalWaitValidationError("Actions run lookup returned an unexpected value; registration refused")
    head = str(payload.get("head_sha") or "").strip().lower()
    try:
        head = validate_head_sha(head) if head else ""
    except ExternalWaitValidationError:
        head = ""
    number = payload.get("run_number")
    return {
        "name": validate_run_label(payload.get("name")),
        "run_number": number if isinstance(number, int) and number > 0 else None,
        "head_sha": head,
        "status": str(payload.get("status") or "").strip().lower()[:24],
    }


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


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _supersede_stale_waits(
    registry: ExternalWaitRegistry,
    *,
    repo: str,
    pr_number: int,
    user_id: int,
    chat_id: int,
    head_sha: str,
) -> list[str]:
    """Terminal-supersede this conversation's other-head waits for the PR.

    Every push moves the PR head, but registering the new watch used to leave
    the old ones monitoring: each later fired its *stale* terminal rollup and
    resumed the conversation with an outdated head (2026-08-14: five stacked
    waits v3~v6 for one PR). Superseding mirrors the monitor's own moved-head
    terminal path — record and wake journal are preserved, the wait just
    stops polling and never resumes.

    Scoped to the same conversation and PR: another conversation's watch is
    never touched. A record already watching the new head (including a legacy
    short-SHA prefix of it) is left alone, so an idempotent re-registration
    never supersedes itself.
    """
    superseded: list[str] = []
    for rec in registry.records():
        if rec.get("state") != STATE_MONITORING:
            continue
        if str(rec.get("repo") or "") != repo:
            continue
        if int(rec.get("pr_number") or 0) != pr_number:
            continue
        if (
            int(rec.get("user_id") or 0) != user_id
            or int(rec.get("chat_id") or 0) != chat_id
        ):
            continue
        recorded = str(rec.get("head_sha") or "")
        if recorded == head_sha or (recorded and head_sha.startswith(recorded)):
            continue
        wait_id = str(rec.get("wait_id") or "")
        if registry.finish(wait_id, TERMINAL_SUPERSEDED):
            superseded.append(wait_id)
    return superseded


_SOURCE_ALIASES = {
    "": SOURCE_GITHUB_PR_CHECKS,
    "checks": SOURCE_GITHUB_PR_CHECKS,
    SOURCE_GITHUB_PR_CHECKS: SOURCE_GITHUB_PR_CHECKS,
    "merge-queue": SOURCE_GITHUB_MERGE_QUEUE,
    SOURCE_GITHUB_MERGE_QUEUE: SOURCE_GITHUB_MERGE_QUEUE,
    "actions-run": SOURCE_GITHUB_ACTIONS_RUN,
    SOURCE_GITHUB_ACTIONS_RUN: SOURCE_GITHUB_ACTIONS_RUN,
    "local-task": SOURCE_LOCAL_TASK,
    SOURCE_LOCAL_TASK: SOURCE_LOCAL_TASK,
    "detached-job": SOURCE_DETACHED_JOB,
    SOURCE_DETACHED_JOB: SOURCE_DETACHED_JOB,
}


def _source_of(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    if key not in _SOURCE_ALIASES:
        raise ExternalWaitValidationError(
            "source must be 'checks', 'merge-queue', 'actions-run', 'local-task' or 'detached-job'"
        )
    return _SOURCE_ALIASES[key]


def _register_detached_job(home: Path, args: dict[str, Any], route: dict[str, Any]) -> int:
    """``--source detached-job``: watch a transient unit's EXIT marker (#2605 layer 3)."""
    try:
        unit = validate_unit_name(args.get("unit"))
        log_path = validate_log_path(args.get("log") or args.get("log_path"))
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
        repo="",
        pr_number=None,
        head_sha="",
        user_id=int(route["user_id"]),
        chat_id=int(route["chat_id"]),
        session_id=route.get("session_id"),
        summary=summary,
        timeout_seconds=timeout,
        poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
        source=SOURCE_DETACHED_JOB,
        unit=unit,
        log_path=log_path,
    )
    if not wait_id:
        _emit({"ok": False, "code": "registry-error"})
        return RC_REGISTRY_ERROR
    _emit(
        {
            "ok": True,
            "wait_id": wait_id,
            "unit": unit,
            "log_path": log_path,
            "timeout_seconds": int(timeout),
            "source": SOURCE_DETACHED_JOB,
            "superseded": [],
            "message": (
                "watching the detached job's EXIT marker; conversation resumes when "
                f"it ends (wait_id={wait_id})"
            ),
        }
    )
    return RC_OK


def _register_local_task(home: Path, args: dict[str, Any], route: dict[str, Any]) -> int:
    """``--source local-task``: label one of this session's background tasks (#2605)."""
    try:
        task_id = validate_task_id(args.get("task_id"))
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
        repo="",
        pr_number=None,
        head_sha="",
        user_id=int(route["user_id"]),
        chat_id=int(route["chat_id"]),
        session_id=route.get("session_id"),
        summary=summary,
        timeout_seconds=timeout,
        poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
        source=SOURCE_LOCAL_TASK,
        task_id=task_id,
    )
    if not wait_id:
        _emit({"ok": False, "code": "registry-error"})
        return RC_REGISTRY_ERROR
    _emit(
        {
            "ok": True,
            "wait_id": wait_id,
            "task_id": task_id,
            "timeout_seconds": int(timeout),
            "source": SOURCE_LOCAL_TASK,
            "superseded": [],
            "message": (
                "labelled the background task on the conversation's status line; "
                "the harness resumes the session when it ends, the bridge closes "
                f"this record from that frame (wait_id={wait_id})"
            ),
        }
    )
    return RC_OK


def _register_actions_run(home: Path, args: dict[str, Any], route: dict[str, Any]) -> int:
    """``--source actions-run``: watch one workflow run by id (#2590)."""
    try:
        repo = validate_repo(str(args.get("repo", "")))
        run_id = validate_run_id(args.get("run_id"))
        summary = validate_summary(args.get("summary"))
        timeout = float(args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        wanted_head = str(args.get("head_sha", "") or "").strip()
        wanted_head = validate_head_sha(wanted_head) if wanted_head else ""
        run = resolve_actions_run(repo, run_id)
        head_sha = str(run.get("head_sha") or "")
        if wanted_head and not (head_sha and head_sha.startswith(wanted_head)):
            raise ExternalWaitValidationError("--head-sha does not match the run's head commit")
        head_sha = head_sha or wanted_head
    except ExternalWaitValidationError as exc:
        _emit({"ok": False, "code": "validation", "message": str(exc)})
        return RC_USAGE
    except (TypeError, ValueError):
        _emit({"ok": False, "code": "validation", "message": "invalid numeric field"})
        return RC_USAGE

    registry = ExternalWaitRegistry(default_registry_path(home))
    wait_id = registry.register(
        repo=repo,
        pr_number=None,
        head_sha=head_sha,
        user_id=int(route["user_id"]),
        chat_id=int(route["chat_id"]),
        session_id=route.get("session_id"),
        summary=summary,
        timeout_seconds=timeout,
        poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
        source=SOURCE_GITHUB_ACTIONS_RUN,
        run_id=run_id,
        run_label=run.get("name"),
        run_number=run.get("run_number"),
    )
    if not wait_id:
        _emit({"ok": False, "code": "registry-error"})
        return RC_REGISTRY_ERROR
    _emit(
        {
            "ok": True,
            "wait_id": wait_id,
            "repo": repo,
            "run_id": run_id,
            "run_number": run.get("run_number"),
            "workflow": run.get("name") or "",
            "head_sha": head_sha,
            "run_status": run.get("status") or "",
            "timeout_seconds": int(timeout),
            "source": SOURCE_GITHUB_ACTIONS_RUN,
            "superseded": [],
            "message": (
                "watching the Actions run; conversation resumes when it completes "
                f"(wait_id={wait_id})"
            ),
        }
    )
    return RC_OK


def _cmd_register(home: Path, args: dict[str, Any]) -> int:
    route = resolve_active_route(home)
    if route is None:
        _emit(
            {
                "ok": False,
                "code": "route-unavailable",
                **describe_route_unavailable(home),
                "message": (
                    "no single active conversation route; keep a foreground "
                    "watch or report that auto-resume is unavailable"
                ),
            }
        )
        return RC_ROUTE_UNAVAILABLE
    try:
        source = _source_of(args.get("source"))
        if source == SOURCE_GITHUB_ACTIONS_RUN:
            return _register_actions_run(home, args, route)
        if source == SOURCE_LOCAL_TASK:
            return _register_local_task(home, args, route)
        if source == SOURCE_DETACHED_JOB:
            return _register_detached_job(home, args, route)
    except ExternalWaitValidationError as exc:
        _emit({"ok": False, "code": "validation", "message": str(exc)})
        return RC_USAGE
    try:
        repo = validate_repo(str(args.get("repo", "")))
        pr_number = validate_pr_number(args.get("pr"))
        head_sha = resolve_full_head_sha(repo, validate_head_sha(str(args.get("head_sha", ""))))
        summary = validate_summary(args.get("summary"))
        timeout = float(args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        source = _source_of(args.get("source"))
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
        source=source,
    )
    if not wait_id:
        _emit({"ok": False, "code": "registry-error"})
        return RC_REGISTRY_ERROR
    superseded: list[str] = []
    if not _truthy(args.get("keep_previous")):
        superseded = _supersede_stale_waits(
            registry,
            repo=repo,
            pr_number=pr_number,
            user_id=int(route["user_id"]),
            chat_id=int(route["chat_id"]),
            head_sha=head_sha,
        )
    _emit(
        {
            "ok": True,
            "wait_id": wait_id,
            "repo": repo,
            "pr": pr_number,
            "head_sha": head_sha,
            "timeout_seconds": int(timeout),
            "source": source,
            "superseded": superseded,
            "message": (
                (
                    "watching the merge queue; conversation resumes when the PR "
                    f"lands, is dropped, or closes (wait_id={wait_id})"
                )
                if source == SOURCE_GITHUB_MERGE_QUEUE
                else (
                    f"watching GitHub checks; conversation resumes on terminal "
                    f"state (wait_id={wait_id})"
                )
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
                    "run_id": rec.get("run_id"),
                    "head_sha": str(rec.get("head_sha") or "")[:8],
                    "source": rec.get("source") or SOURCE_GITHUB_PR_CHECKS,
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
